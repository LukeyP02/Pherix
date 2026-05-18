"""Effect: one journalled tool call.

An Effect is a single entry in a Transaction's append-only effect journal.
``read_keys`` / ``write_keys`` slots exist from day one (Slice 4 isolation)
but carry no logic in Slice 1. ``compensator`` is wired up in Slice 3:
when an effect is irreversible and fails mid-commit, the runtime invokes
the named compensator to undo it (a semantic left-inverse — Pherix does
not verify the property; the developer asserts it).

Effect args must be deterministically serialisable so the idempotency key
(``effect_id``) is stable across runs and the audit journal can faithfully
persist them. Supported in args (and in snapshots / results via the audit
journal): anything natively JSON-serialisable (str / int / float / bool /
list / dict / None), plus ``bytes`` (encoded as base64), ``datetime`` (ISO
8601), and any ``@dataclass`` instance (recursively ``asdict``-ed).
Anything else raises :class:`EffectArgsError` at Effect construction —
silent ``str()`` coercion would let two distinct non-serialisable objects
collide on the same effect_id, which is exactly the bug we don't want at
the idempotency boundary (a Slice 1 review follow-up).
"""

from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import asdict, dataclass, field, is_dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any


class EffectStatus(Enum):
    STAGED = "staged"
    APPLIED = "applied"
    COMPENSATED = "compensated"
    GATED = "gated"
    FAILED = "failed"


class EffectArgsError(ValueError):
    """Raised when Effect args contain a value Pherix cannot journal."""


def strict_json_default(obj: Any) -> Any:
    """JSON default fn that supports bytes / datetime / dataclass; raises otherwise.

    Used by :func:`compute_effect_id` and the audit journal — both want a
    deterministic, lossless representation. Silent ``str(obj)`` coercion is
    forbidden: it lets two distinct non-serialisable objects collide on the
    same effect_id and produces a lossy audit row. If a tool wants to pass an
    exotic type, the developer converts it to a supported shape at the call
    site (e.g. base64-encode a numpy array, ``.isoformat()`` a custom date).
    """
    if isinstance(obj, (bytes, bytearray)):
        # base64 is deterministic and content-addressed: identical bytes
        # produce identical strings, so identical args produce identical
        # effect_ids.
        return f"<bytes:b64:{base64.b64encode(bytes(obj)).decode('ascii')}>"
    if is_dataclass(obj) and not isinstance(obj, type):
        return asdict(obj)
    if isinstance(obj, datetime):
        return obj.isoformat()
    raise TypeError(
        f"Pherix cannot journal {type(obj).__name__!r} ({obj!r}). "
        f"Supported types: native JSON, bytes, datetime, dataclass instances."
    )


@dataclass(frozen=True)
class StagedResult:
    """Sentinel returned to the agent when a staged irreversible tool is called.

    Slice 3 / D1: staged effects exist in a partial order with respect to commit
    time — temporally posterior, by construction. The agent receives the
    deterministic ``effect_id`` so it can carry the reference around (e.g.
    pass it to ``approve_irreversible``), but the real return value of the
    underlying tool only exists *after* commit fires the effect, and lands in
    the audit journal then. The agent therefore cannot branch on the result
    within the same transaction — that's the partial-order property as a
    constraint on agent code.
    """

    effect_id: str

    def __repr__(self) -> str:
        return f"StagedResult(effect_id={self.effect_id!r})"


def compute_effect_id(txn_id: str, index: int, tool: str, args: dict) -> str:
    """Idempotency key = stable hash of (txn_id, index, tool, sorted args).

    Raises :class:`EffectArgsError` if any arg is not deterministically
    serialisable. The error fires at Effect construction so the developer
    sees it where the bad call originated, not later when commit runs.
    """
    try:
        payload = json.dumps(
            {"txn_id": txn_id, "index": index, "tool": tool, "args": args},
            sort_keys=True,
            default=strict_json_default,
        )
    except TypeError as exc:
        raise EffectArgsError(
            f"tool {tool!r} got non-journal-able args: {exc} "
            f"Effect args must be deterministically serialisable so the "
            f"idempotency key (effect_id) is stable across runs."
        ) from exc
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


@dataclass
class Effect:
    txn_id: str
    index: int
    tool: str
    args: dict
    resource: str
    reversible: bool
    effect_id: str = ""
    read_keys: list[tuple] = field(default_factory=list)
    write_keys: list[tuple] = field(default_factory=list)
    status: EffectStatus = EffectStatus.STAGED
    snapshot: Any = None
    result: Any = None
    compensator: str | None = None
    ts: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def __post_init__(self) -> None:
        if not self.effect_id:
            self.effect_id = compute_effect_id(
                self.txn_id, self.index, self.tool, self.args
            )
