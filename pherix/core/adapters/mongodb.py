"""MongoAdapter — snapshot-the-touched-document reversibility over a document store.

Standalone MongoDB has no multi-document transaction (those need a replica set).
But the *document* an effect touches, addressed by ``_id``, is small enough to
copy. So reversibility is the filesystem-adapter machinery once more — capture
the **before-image** of each touched document in :meth:`snapshot`, write it back
in :meth:`restore`.

This document-snapshot design works on *any* MongoDB deployment, standalone
included, because it only uses ordinary CRUD (``find_one`` / ``replace_one`` /
``delete_one``). Where a replica-set deployment offers native multi-document
transactions, those are a strictly better atomicity story and a caller may layer
them on — but the base design here deliberately does **not** require them, so it
runs against the simplest possible server (and against ``mongomock`` offline).

Touched-document convention (route b)
-------------------------------------
The adapter learns which document(s) an effect touches from ``effect.args``:

- A single document: ``args["collection"]`` (collection name) + ``args["doc_id"]``
  (its ``_id``).
- Multiple documents in one effect: ``args["docs"]`` — a list of
  ``{"collection": <name>, "doc_id": <id>}`` mappings.

If no document is named, nothing is captured and restore is a no-op. A
before-image records, per (collection, _id): the prior document (existed) or an
*absent* marker (no such document — restore deletes whatever the effect
inserts).

Atomicity (honest)
------------------
On standalone Mongo a multi-document effect is not atomic at the server; the
journal backward-fold is what makes the *effect* atomic — :meth:`restore`
rewrites every captured document back regardless of how far ``apply`` got. A
single-document ``replace_one`` / ``delete_one`` is atomic at the document level
in MongoDB.

``pymongo`` is imported lazily inside methods so ``import pherix`` works with no
third-party packages installed. (The before-image is held as plain Python
mappings, so no import is actually needed in the hot path — the lazy import rule
is honoured regardless.)
"""

from __future__ import annotations

import copy
from typing import Any, Callable

from pherix.core.adapters.base import SnapshotHandle
from pherix.core.effects import Effect


class MongoAdapter:
    """``ResourceAdapter`` over a pymongo Database, reversible by doc-snapshot."""

    name = "mongodb"

    def __init__(self, db: Any):
        # ``db`` is a pymongo Database (``MongoClient().mydb``) or a compatible
        # fake (mongomock). Collections are addressed by name off it:
        # ``db[collection_name]``. We hold it as SQLiteAdapter holds its
        # connection; no import of pymongo happens here.
        self._db = db

    @property
    def db(self) -> Any:
        return self._db

    def supports_rollback(self) -> bool:
        return True

    # --- touched-document extraction ---------------------------------------

    @staticmethod
    def _touched_docs(effect: Effect) -> list[dict]:
        """``[{"collection": name, "doc_id": id}, ...]`` this effect touches.

        Union of the single-document form (``args["collection"]`` +
        ``args["doc_id"]``) and the multi-document form (``args["docs"]``),
        de-duplicated on (collection, doc_id), order-preserving.
        """
        targets: list[dict] = []
        coll = effect.args.get("collection")
        doc_id = effect.args.get("doc_id")
        if coll is not None and doc_id is not None:
            targets.append({"collection": coll, "doc_id": doc_id})
        for entry in effect.args.get("docs") or ():
            targets.append(
                {"collection": entry["collection"], "doc_id": entry["doc_id"]}
            )
        seen: set[tuple] = set()
        out: list[dict] = []
        for t in targets:
            sig = (t["collection"], t["doc_id"])
            if sig not in seen:
                seen.add(sig)
                out.append(t)
        return out

    # --- per-effect snapshot / apply / restore -----------------------------

    def snapshot(self, effect: Effect) -> SnapshotHandle:
        """Capture the before-image of every touched document.

        Each target becomes ``{"collection", "doc_id", "existed", "doc"}``: if
        ``find_one({"_id": doc_id})`` returns a document we deep-copy it into
        the payload (existed); otherwise ``existed`` is False and ``doc`` None.
        The deep-copy guards against the effect mutating the returned dict in
        place. Documents are assumed JSON-light (the audit journal serialises
        the payload); exotic BSON types (ObjectId etc.) are the caller's to
        keep representable, the same contract the SQL/FS adapters carry.
        """
        records: list[dict] = []
        for target in self._touched_docs(effect):
            coll = target["collection"]
            doc_id = target["doc_id"]
            existing = self._db[coll].find_one({"_id": doc_id})
            records.append(
                {
                    "collection": coll,
                    "doc_id": doc_id,
                    "existed": existing is not None,
                    "doc": copy.deepcopy(existing) if existing is not None else None,
                }
            )
        return SnapshotHandle(
            resource=self.name,
            effect_index=effect.index,
            payload={"docs": records},
        )

    def apply(self, effect: Effect, tool_fn: Callable[..., Any]) -> Any:
        # The pymongo Database is injected as the tool's first positional arg,
        # as SQLiteAdapter injects the connection. The @tool wrapper hides it
        # from the agent's call-site.
        return tool_fn(self._db, **effect.args)

    def restore(self, handle: SnapshotHandle) -> None:
        """Rewrite every captured document back to its before-image.

        existed → ``replace_one({"_id": id}, prior_doc, upsert=True)`` puts the
        prior document back (upsert handles the case where the effect deleted
        it); absent → ``delete_one({"_id": id})`` removes whatever the effect
        inserted (a no-op if nothing was inserted).
        """
        records: list[dict] = handle.payload["docs"]
        for record in records:
            coll = record["collection"]
            doc_id = record["doc_id"]
            if record["existed"]:
                # upsert=True so a document the effect deleted is re-created,
                # and one the effect merely modified is overwritten back.
                self._db[coll].replace_one(
                    {"_id": doc_id}, record["doc"], upsert=True
                )
            else:
                self._db[coll].delete_one({"_id": doc_id})
