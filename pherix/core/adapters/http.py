"""HTTPAdapter — the irreversible adapter (Slice 3).

The point of this adapter is to be *honest*: an external HTTP call (charge a
card, send an email, fire a webhook) cannot be snapshotted, so it cannot be
rolled back via the snapshot-restore engine that the SQL and filesystem
adapters use. ``supports_rollback() -> False`` is the runtime's signal to
push the effect down the staging lane: the tool does not execute at
stage-time; it is recorded as intent and deferred to ``commit()``.

This module never imports ``core/tools.py``: the runtime resolves the tool
to a callable and hands it to :meth:`apply` as ``tool_fn``.
"""

from __future__ import annotations

from typing import Any, Callable

from pherix.core.adapters.base import SnapshotHandle
from pherix.core.effects import Effect


class IrreversibleAdapterError(RuntimeError):
    """Raised if snapshot/restore is invoked on an adapter that cannot roll back.

    This should never happen if the runtime is routing effects correctly —
    a ``supports_rollback() -> False`` adapter must be staged, not
    snapshot-and-applied live. The exception exists to make a routing bug
    fail loudly rather than corrupt state silently.
    """


class HTTPAdapter:
    """``ResourceAdapter`` over an external HTTP service (Slice 3, irreversible).

    Conforms to :class:`ResourceAdapter` only — *not* to
    :class:`TransactionalResourceAdapter`: a third-party HTTP service has no
    transaction-scope lifecycle Pherix can drive. The tool itself owns the
    HTTP call (using whatever client it likes); the adapter is the seam that
    tells the runtime "I cannot undo what this does" via
    ``supports_rollback() -> False``.
    """

    name = "http"

    def supports_rollback(self) -> bool:
        return False

    def snapshot(self, effect: Effect) -> SnapshotHandle:
        raise IrreversibleAdapterError(
            "HTTPAdapter.snapshot() must not be called: irreversible effects "
            "are staged at stage-time and fired at commit-time. The runtime "
            "should never request a snapshot from a non-reversible adapter."
        )

    def apply(self, effect: Effect, tool_fn: Callable[..., Any]) -> Any:
        # No handle is injected — HTTP tools declare ``injects_handle=False``
        # in their @tool decorator. The tool fires the real HTTP call itself.
        return tool_fn(**effect.args)

    def restore(self, handle: SnapshotHandle) -> None:
        raise IrreversibleAdapterError(
            "HTTPAdapter.restore() must not be called: there is no "
            "before-state to restore. Irreversible effects are unwound via "
            "their registered compensator, not via snapshot/restore."
        )

    # --- versioning (Slice 4) ----------------------------------------------
    # HTTPAdapter does NOT conform to VersionedResourceAdapter: an external
    # HTTP service has no notion of "version" Pherix can read honestly, and
    # the runtime already isolates irreversible effects by construction (the
    # staging lane defers their fire to commit, so two pre-commit stages of
    # the same effect cannot race). These methods exist only to make an
    # accidental call fail loudly — they are not part of the sub-protocol's
    # behavioural contract. The contract: the runtime gates isolation work
    # on ``adapter.supports_rollback()``; HTTPAdapter returns False there.

    def read_version(self, key: tuple) -> object:
        raise IrreversibleAdapterError(
            "HTTPAdapter.read_version() must not be called: irreversible "
            "effects are isolated-by-construction via staging — they defer "
            "fire to commit, so two pre-commit stages of the same effect "
            "cannot race. There is no version to read."
        )

    def write_version(self, key: tuple) -> object:
        raise IrreversibleAdapterError(
            "HTTPAdapter.write_version() must not be called: see read_version."
        )
