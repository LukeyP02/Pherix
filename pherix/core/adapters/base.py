"""The ResourceAdapter protocol — the seam that makes Pherix a system.

An adapter makes journal entries executable and reversible against a *class* of
real resource via ``snapshot -> apply -> restore``. ``core/adapters/`` never
imports ``core/tools.py``: the runtime resolves ``effect.tool`` to a callable and
hands it to ``apply`` as ``tool_fn``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Protocol, runtime_checkable

from pherix.core.effects import Effect


@dataclass
class SnapshotHandle:
    """Opaque handle to a captured before-state, returned by ``adapter.snapshot``.

    ``payload`` holds adapter-private, JSON-serialisable detail (e.g. the SQLite
    savepoint name) so the audit journal can persist it without special-casing.
    """

    resource: str
    effect_index: int
    payload: dict = field(default_factory=dict)


@runtime_checkable
class ResourceAdapter(Protocol):
    name: str

    def supports_rollback(self) -> bool:
        """Honesty flag — whether this resource can actually be restored."""
        ...

    def snapshot(self, effect: Effect) -> SnapshotHandle:
        """Capture the before-state for ``effect``, prior to applying it."""
        ...

    def apply(self, effect: Effect, tool_fn: Callable[..., Any]) -> Any:
        """Execute the effect by invoking the resolved tool callable."""
        ...

    def restore(self, handle: SnapshotHandle) -> None:
        """Restore the resource to the state captured by ``handle``."""
        ...
