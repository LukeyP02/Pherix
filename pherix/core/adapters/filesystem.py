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

import hashlib
import shutil
import tempfile
import uuid
from pathlib import Path
from typing import Any, Callable

from pherix.core.adapters.base import SnapshotHandle
from pherix.core.effects import Effect

# Sentinel returned by ``read_version`` when the path does not exist.
# Using a non-None marker means the commit-time isolation diff can
# distinguish "I read this file as absent" from a sha256 hash via a
# plain ``!=`` comparison — a later create then correctly conflicts.
_FS_MISSING = "__missing__"


class FsHandle:
    """The per-effect filesystem handle injected as the first arg of FS tools.

    Resolves every relative path against ``root``, rejects anything that escapes
    it (``..`` segments, absolute paths outside root, symlinks pointing
    elsewhere), and records first-touch backups into the effect's backup
    subdirectory. Subsequent touches of the same path are pass-through writes
    — the pre-effect state is already captured.

    Slice 4: every ``read`` records a read_key ``(path, content-hash)`` and
    every ``write`` / ``delete`` records a write_key into the bound Effect.
    Recording is a no-op when ``effect`` is ``None`` (the handle still
    functions for raw unit tests outside ``agent_txn``). Per-handle dedupe
    sets ensure re-reading or re-writing the same path inside one effect
    does not bloat the journal — the first read's version is the one the
    agent's logic branched on, which is what the commit-time isolation
    diff needs to compare against.
    """

    def __init__(
        self,
        root: Path,
        backup_dir: Path,
        touched: dict[str, dict],
        effect: Any = None,
        adapter: "FilesystemAdapter | None" = None,
    ):
        # ``root`` is pre-resolved by the adapter; storing it resolved means
        # every safe-path check compares like-for-like.
        self._root = root
        self._backup_dir = backup_dir
        self._touched = touched
        # Slice 4 isolation: the handle records into ``effect.read_keys`` /
        # ``effect.write_keys``. ``adapter`` supplies ``read_version`` for
        # content-hashing on read. Both may be None — the handle then
        # short-circuits recording and behaves exactly as in Slice 2.
        self._effect = effect
        self._adapter = adapter
        self._recorded_reads: set[str] = set()
        self._recorded_writes: set[str] = set()

    # --- public API (tool-facing) -------------------------------------------

    def write(self, rel_path: str, data: bytes) -> None:
        target = self._safe_path(rel_path)
        self._record_first_touch(rel_path, target)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        self._record_write_key(rel_path)

    def read(self, rel_path: str) -> bytes:
        target = self._safe_path(rel_path)
        # Reads do not trigger backups — they don't change state.
        data = target.read_bytes()
        self._record_read_key(rel_path)
        return data

    def delete(self, rel_path: str) -> None:
        target = self._safe_path(rel_path)
        self._record_first_touch(rel_path, target)
        # If the file didn't exist pre-effect, the record above will have
        # captured "existed: False" and we still want a hard error here —
        # the agent asked us to delete something that wasn't there.
        target.unlink()
        self._record_write_key(rel_path)

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

    # --- Slice 4 isolation recording ----------------------------------------

    def _record_read_key(self, rel_path: str) -> None:
        # ``effect`` / ``adapter`` are None outside an agent_txn — recording
        # is a no-op so the handle stays usable for raw unit tests.
        if self._effect is None or self._adapter is None:
            return
        if rel_path in self._recorded_reads:
            return
        version = self._adapter.read_version((rel_path,))
        self._effect.read_keys.append(("fs", (rel_path,), version))
        self._recorded_reads.add(rel_path)

    def _record_write_key(self, rel_path: str) -> None:
        """Append a write triple `(resource, key, version_after_my_write)`.

        Slice 4 P3: the post-write version is the content-hash AFTER our write
        landed. The runtime's commit-time diff uses this as our "expected
        current" for the key — if the live version differs from it, someone
        else moved the resource after we wrote, which is a real cross-txn
        conflict (not a self-bump artefact).

        Unlike read recording, writes are NOT deduplicated: re-writing the
        same path twice in one effect produces two entries, the last one
        carries the freshest post-write version. The diff folds via
        `last_my_write[(resource, key)]` to pick the most recent.
        """
        if self._effect is None or self._adapter is None:
            return
        # Re-hash AFTER the write has landed on disk so the version we record
        # is the version the adapter would report on `read_version` right
        # now. `read_version` happens to be the same logic, so we reuse it.
        version_after = self._adapter.read_version((rel_path,))
        self._effect.write_keys.append(("fs", (rel_path,), version_after))
        self._recorded_writes.add(rel_path)


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
        # Slice 4: pass the active Effect (and self) so the FsHandle can
        # record read_keys / write_keys automatically. ``active_effect`` is
        # set by the runtime around ``adapter.apply``; outside an
        # ``agent_txn`` it is None and the handle skips recording. Local
        # import to avoid a cycle (tools.py is otherwise free of adapter
        # imports).
        from pherix.core.tools import active_effect

        return FsHandle(
            root=self._root,
            backup_dir=Path(snapshot.payload["backup_dir"]),
            touched=snapshot.payload["touched"],
            effect=active_effect.get(),
            adapter=self,
        )

    # --- versioning (Slice 4 — VersionedResourceAdapter) -------------------

    def _resolve_versioned(self, key: tuple) -> Path:
        # Mirror FsHandle._safe_path's containment check so version lookups
        # cannot be tricked into reading content outside the root.
        if len(key) != 1:
            raise ValueError(
                f"FilesystemAdapter version key must be a 1-tuple "
                f"(rel_path,); got {key!r}"
            )
        rel_path = key[0]
        candidate = Path(rel_path)
        if candidate.is_absolute():
            raise ValueError(f"path {rel_path!r} is outside root {self._root}")
        resolved = (self._root / candidate).resolve()
        if not resolved.is_relative_to(self._root):
            raise ValueError(f"path {rel_path!r} is outside root {self._root}")
        return resolved

    def read_version(self, key: tuple) -> str:
        path = self._resolve_versioned(key)
        if not path.exists():
            return _FS_MISSING
        # Slice 4 cases hold small files; reading the whole file is fine.
        # A streaming hash is a trivial swap if the test corpus grows.
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def write_version(self, key: tuple) -> str:
        # Compute from on-disk content *after* the write — no cache.
        return self.read_version(key)
