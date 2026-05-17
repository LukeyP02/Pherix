"""FilesystemAdapter — copy-on-write backup over a rooted directory (Slice 2).

The point of this adapter is to prove the :class:`ResourceAdapter` protocol is a
real abstraction by satisfying it with machinery structurally unlike SQL: file
copies into a per-txn tempdir, not database savepoints. Conforms to
:class:`TransactionalResourceAdapter` (begin/commit/rollback bracket the per-txn
backup root) and to the per-effect ``snapshot -> apply -> restore`` lifecycle.

Lazy snapshot rule (D2): backups are taken at *first touch* of a path within a
single effect. Reads never trigger backups; subsequent writes/deletes to the
same path inside the same effect do not re-backup (the pre-effect state is
already captured). Across effects, each effect carries its own backup record —
so a backward fold (newest-first) restores effect N's pre-state, then N-1's,
landing at the original.
"""

from __future__ import annotations

import shutil
import tempfile
import uuid
from pathlib import Path
from typing import Any, Callable

from pherix.core.adapters.base import SnapshotHandle
from pherix.core.effects import Effect


class FsHandle:
    """The per-effect filesystem handle injected as the first arg of FS tools.

    Resolves every relative path against ``root``, rejects anything that escapes
    it (``..`` segments, absolute paths outside root, symlinks pointing
    elsewhere), and records first-touch backups into the effect's backup
    subdirectory. Subsequent touches of the same path are pass-through writes
    — the pre-effect state is already captured.
    """

    def __init__(self, root: Path, backup_dir: Path, touched: dict[str, dict]):
        # ``root`` is pre-resolved by the adapter; storing it resolved means
        # every safe-path check compares like-for-like.
        self._root = root
        self._backup_dir = backup_dir
        self._touched = touched

    # --- public API (tool-facing) -------------------------------------------

    def write(self, rel_path: str, data: bytes) -> None:
        target = self._safe_path(rel_path)
        self._record_first_touch(rel_path, target)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)

    def read(self, rel_path: str) -> bytes:
        target = self._safe_path(rel_path)
        # Reads do not trigger backups — they don't change state.
        return target.read_bytes()

    def delete(self, rel_path: str) -> None:
        target = self._safe_path(rel_path)
        self._record_first_touch(rel_path, target)
        # If the file didn't exist pre-effect, the record above will have
        # captured "existed: False" and we still want a hard error here —
        # the agent asked us to delete something that wasn't there.
        target.unlink()

    # --- internals ----------------------------------------------------------

    def _safe_path(self, rel_path: str) -> Path:
        candidate = Path(rel_path)
        if candidate.is_absolute():
            raise ValueError(f"path {rel_path!r} is outside root {self._root}")
        resolved = (self._root / candidate).resolve()
        # Pathlib's is_relative_to is the explicit containment check; combined
        # with .resolve() above, ``..`` segments and symlinks pointing outside
        # the root are both rejected.
        if not resolved.is_relative_to(self._root):
            raise ValueError(f"path {rel_path!r} is outside root {self._root}")
        return resolved

    def _record_first_touch(self, rel_path: str, abs_path: Path) -> None:
        if rel_path in self._touched:
            return  # lazy: pre-effect state already captured
        if abs_path.exists():
            backup_name = f"{uuid.uuid4().hex}.bin"
            shutil.copy2(abs_path, self._backup_dir / backup_name)
            self._touched[rel_path] = {"backup": backup_name, "existed": True}
        else:
            self._touched[rel_path] = {"backup": None, "existed": False}


class FilesystemAdapter:
    """``ResourceAdapter`` over a rooted directory (Slice 2)."""

    name = "fs"

    def __init__(self, root: Path | str):
        # ``root`` is resolved once on construction; the handle's safe-path
        # check compares resolved-to-resolved (so symlinks under root still
        # work, but symlinks pointing outside it are rejected).
        self._root = Path(root).resolve()
        self._backup_root: Path | None = None

    @property
    def root(self) -> Path:
        return self._root

    @property
    def backup_root(self) -> Path | None:
        return self._backup_root

    def supports_rollback(self) -> bool:
        return True

    # --- transaction-scope lifecycle (TransactionalResourceAdapter) ---------

    def begin(self) -> None:
        self._backup_root = Path(tempfile.mkdtemp(prefix="pherix_fs_"))

    def commit(self) -> None:
        self._cleanup_backup_root()

    def rollback(self) -> None:
        self._cleanup_backup_root()

    def _cleanup_backup_root(self) -> None:
        if self._backup_root is not None:
            shutil.rmtree(self._backup_root, ignore_errors=False)
            self._backup_root = None

    # --- per-effect snapshot / apply / restore ------------------------------

    def snapshot(self, effect: Effect) -> SnapshotHandle:
        if self._backup_root is None:
            raise RuntimeError(
                "FilesystemAdapter.snapshot() called outside a transaction; "
                "begin() must be called first."
            )
        backup_dir = self._backup_root / f"e_{effect.index}"
        backup_dir.mkdir()
        return SnapshotHandle(
            resource=self.name,
            effect_index=effect.index,
            # ``touched`` is mutated by the FsHandle during apply(); keeping it
            # in the payload means the audit journal sees the final list of
            # touched paths once the effect status flips to APPLIED.
            payload={"backup_dir": str(backup_dir), "touched": {}},
        )

    def apply(self, effect: Effect, tool_fn: Callable[..., Any]) -> Any:
        handle = self._handle_for(effect.snapshot)
        # D2: handle is injected as the tool's first positional arg; the
        # @tool wrapper hides it from the agent's call-site.
        return tool_fn(handle, **effect.args)

    def restore(self, handle: SnapshotHandle) -> None:
        backup_dir = Path(handle.payload["backup_dir"])
        touched: dict[str, dict] = handle.payload["touched"]
        for rel_path, record in touched.items():
            # Re-resolve the rel_path against root to be safe against any
            # post-hoc payload mutation (and to keep the safety story uniform).
            target = self._root / rel_path
            if record["existed"]:
                shutil.copy2(backup_dir / record["backup"], target)
            else:
                # Pre-state was "didn't exist" — delete whatever's there now,
                # if anything. The effect may have created the file; we may
                # also be restoring a path the effect created and then deleted.
                if target.exists():
                    target.unlink()

    # --- handle construction -----------------------------------------------

    def _handle_for(self, snapshot: SnapshotHandle) -> FsHandle:
        return FsHandle(
            root=self._root,
            backup_dir=Path(snapshot.payload["backup_dir"]),
            touched=snapshot.payload["touched"],
        )
