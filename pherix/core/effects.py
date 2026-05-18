"""Effect: one journalled tool call.

An Effect is a single entry in a Transaction's append-only effect journal.
``read_keys`` / ``write_keys`` slots exist from day one (Slice 4 isolation)
but carry no logic in Slice 1. ``compensator`` is wired up in Slice 3:
when an effect is irreversible and fails mid-commit, the runtime invokes
the named compensator to undo it (a semantic left-inverse — Pherix does
not verify the property; the developer asserts it).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


class EffectStatus(Enum):
    STAGED = "staged"
    APPLIED = "applied"
    COMPENSATED = "compensated"
    GATED = "gated"
    FAILED = "failed"


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
    """Idempotency key = stable hash of (txn_id, index, tool, sorted args)."""
    payload = json.dumps(
        {"txn_id": txn_id, "index": index, "tool": tool, "args": args},
        sort_keys=True,
        default=str,
    )
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
