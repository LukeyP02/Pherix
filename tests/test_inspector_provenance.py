"""The inspector's provenance trace — transitive upstream ancestry, tested hard.

``lineage()`` folds the journal into a flat, one-hop read→write graph;
``undo_impact()`` reads one hop *forward* (who consumed a txn). ``provenance()``
is the missing direction taken to its conclusion: walk the version-grounded
``produces`` relation BACKWARD across transactions, transitively, from one
anchor to the origins — the chain of prior agent actions that fed a
transaction's inputs (Wedge #3, decision provenance).

Every assertion here is a pure traversal of the same persisted ``read_keys`` /
``write_keys`` + version counters everything else folds over — nothing is
recomputed from the live world. The dedicated journals pin: a multi-hop chain
(transitivity + depth layers), the cross-transaction-only ``produces`` rule
(intra-txn read-after-write is lineage's territory, never an ancestor), the
two external-input boundaries (unproduced version / versionless read), the
shared-producer cycle/dedup guard, the honest caveat, and the degenerate
inputs. Fully offline.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from pherix.core.audit import AuditJournal
from pherix.core.effects import Effect, EffectStatus
from pherix.core.transaction import Transaction, TxnState
from pherix.inspector.reader import PROVENANCE_CAVEAT, JournalReader
from pherix.inspector.seed import seed_demo_journal


# --- fixtures ---------------------------------------------------------------


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _write_journal(path: str, txns: list[Transaction]) -> None:
    j = AuditJournal(path)
    try:
        for t in txns:
            j.record_transaction(t)
            for e in t.effects:
                j.record_effect(e)
    finally:
        j.close()


def _eff(txn_id, idx, tool, *, reads=None, writes=None, resource="sql",
         status=EffectStatus.APPLIED, reversible=True) -> Effect:
    return Effect(
        txn_id=txn_id, index=idx, tool=tool, args={}, resource=resource,
        reversible=reversible, status=status,
        read_keys=reads or [], write_keys=writes or [], ts=_now(),
    )


def _txn(txn_id, effects, state=TxnState.COMMITTED) -> Transaction:
    t = Transaction(txn_id=txn_id, state=state)
    t.effects = effects
    return t


def _PK(key, version):  # a versioned sql key triple
    return ["sql", ["prices", key], version]


@pytest.fixture
def chain_db(tmp_path: Path) -> str:
    """A 4-transaction price-derivation chain, each hop version-grounded:

        ingest  : —                    → writes prices/sku1 v1   (origin)
        markup  : reads prices/sku1 v1 → writes prices/sku1 v2
        tax     : reads prices/sku1 v2 → writes prices/sku1 v3
        publish : reads prices/sku1 v3 → writes catalog/sku1 v9  (anchor)

    Three produces hops, so provenance(publish) reaches depth 3.
    """
    ingest = _txn("t-ingest", [
        _eff("t-ingest", 0, "ingest", writes=[_PK("sku1", 1)]),
    ])
    markup = _txn("t-markup", [
        _eff("t-markup", 0, "read_price", reads=[_PK("sku1", 1)]),
        _eff("t-markup", 1, "set_price", writes=[_PK("sku1", 2)]),
    ])
    tax = _txn("t-tax", [
        _eff("t-tax", 0, "read_price", reads=[_PK("sku1", 2)]),
        _eff("t-tax", 1, "set_price", writes=[_PK("sku1", 3)]),
    ])
    publish = _txn("t-publish", [
        _eff("t-publish", 0, "read_price", reads=[_PK("sku1", 3)]),
        _eff("t-publish", 1, "publish", writes=[["sql", ["catalog", "sku1"], 9]]),
    ])
    path = str(tmp_path / "chain.db")
    _write_journal(path, [ingest, markup, tax, publish])
    return path


def _produces(prov, frm, to):
    return [e for e in prov["edges"]
            if e["kind"] == "produces" and e["from"] == frm and e["to"] == to]


def _informs(prov, frm, to):
    return [e for e in prov["edges"]
            if e["kind"] == "informs" and e["from"] == frm and e["to"] == to]


# --- the core: transitive multi-hop ancestry --------------------------------


def test_provenance_traces_full_chain_transitively(chain_db: str):
    """publish's input traces back through tax → markup → ingest: three
    produces hops, each ancestor a *prior* transaction's write, depths 1/2/3."""
    with JournalReader(chain_db) as r:
        prov = r.provenance("t-publish")

    assert prov is not None
    by_node = {a["node"]: a for a in prov["ancestors"]}
    # the writers, never the read effects, are the ancestors
    assert set(by_node) == {"t-tax#1", "t-markup#1", "t-ingest#0"}
    assert by_node["t-tax#1"]["depth"] == 1
    assert by_node["t-markup#1"]["depth"] == 2
    assert by_node["t-ingest#0"]["depth"] == 3
    assert prov["scope"]["max_depth"] == 3
    assert prov["scope"]["ancestor_count"] == 3

    # the produces backbone, one hop per transaction boundary
    assert _produces(prov, "t-tax#1", "t-publish#0")
    assert _produces(prov, "t-markup#1", "t-tax#0")
    assert _produces(prov, "t-ingest#0", "t-markup#0")
    # the informs link inside each ancestor txn (read → its own write)
    assert _informs(prov, "t-tax#0", "t-tax#1")
    assert _informs(prov, "t-markup#0", "t-markup#1")


def test_provenance_origin_flagged_inputs_consumed(chain_db: str):
    """Only ingest is an origin (its inputs leave the journal / it read
    nothing); each ancestor reports the version it produced + who read it."""
    with JournalReader(chain_db) as r:
        prov = r.provenance("t-publish")
    by_node = {a["node"]: a for a in prov["ancestors"]}

    assert by_node["t-ingest#0"]["is_origin"] is True
    assert by_node["t-markup#1"]["is_origin"] is False
    assert by_node["t-tax#1"]["is_origin"] is False
    assert prov["totals"]["origin_ancestors"] == 1

    produced = by_node["t-markup#1"]["produced"]
    assert produced == [{"resource": "sql", "key": ["prices", "sku1"],
                         "version": 2, "consumed_by": "t-tax#0"}]
    # the whole chain is version-grounded — nothing leaves the journal here
    assert prov["external_inputs"] == []


def test_provenance_ancestors_ordered_nearest_first(chain_db: str):
    """Ancestors come back depth-ascending (nearest cause first)."""
    with JournalReader(chain_db) as r:
        depths = [a["depth"] for a in r.provenance("t-publish")["ancestors"]]
    assert depths == sorted(depths)
    assert depths == [1, 2, 3]


# --- the cross-transaction rule: intra-txn RAW is NOT ancestry ---------------


def test_provenance_intra_txn_read_after_write_is_not_an_ancestor(tmp_path: Path):
    """The anchor writes K v5 then reads K v5 in a later effect. That is the
    anchor's own read-after-write (lineage's territory) — a produces hop must
    cross a txn boundary, so it yields no ancestor and no external input."""
    anchor = _txn("t-self", [
        _eff("t-self", 0, "set_price", writes=[_PK("a", 5)]),
        _eff("t-self", 1, "read_price", reads=[_PK("a", 5)]),
    ])
    path = str(tmp_path / "self.db")
    _write_journal(path, [anchor])
    with JournalReader(path) as r:
        prov = r.provenance("t-self")
    assert prov["ancestors"] == []
    assert prov["external_inputs"] == []
    assert prov["scope"]["max_depth"] == 0


# --- the two external-input boundaries --------------------------------------


def test_provenance_unproduced_version_is_an_external_root(tmp_path: Path):
    """A read whose version has no producing write in this journal is the honest
    boundary — surfaced as an 'unproduced' external input, the ancestor that
    consumed nothing further is still an origin."""
    markup = _txn("t-markup", [
        _eff("t-markup", 0, "read_price", reads=[_PK("sku1", 1)]),  # v1: unproduced
        _eff("t-markup", 1, "set_price", writes=[_PK("sku1", 2)]),
    ])
    publish = _txn("t-publish", [
        _eff("t-publish", 0, "read_price", reads=[_PK("sku1", 2)]),
        _eff("t-publish", 1, "publish", writes=[["sql", ["catalog", "x"], 9]]),
    ])
    path = str(tmp_path / "ext.db")
    _write_journal(path, [markup, publish])
    with JournalReader(path) as r:
        prov = r.provenance("t-publish")

    ext = prov["external_inputs"]
    assert len(ext) == 1
    assert ext[0]["node"] == "t-markup#0"
    assert ext[0]["resource"] == "sql"
    assert ext[0]["key"] == ["prices", "sku1"]
    assert ext[0]["version"] == 1
    assert ext[0]["reason"] == "unproduced"
    # markup's write is reached (depth 1) and, with no further upstream, an origin
    by_node = {a["node"]: a for a in prov["ancestors"]}
    assert by_node["t-markup#1"]["is_origin"] is True


def test_provenance_versionless_read_is_external_not_a_crash(tmp_path: Path):
    """A versionless read (e.g. the filesystem adapter) cannot anchor a
    version-grounded hop — it is a 'versionless' external input, never an
    exception."""
    anchor = _txn("t-fs", [
        _eff("t-fs", 0, "read_manifest", resource="fs",
             reads=[["fs", ["deploy/manifest.json"]]]),  # 2-tuple → version None
    ])
    path = str(tmp_path / "fs.db")
    _write_journal(path, [anchor])
    with JournalReader(path) as r:
        prov = r.provenance("t-fs")
    assert prov["ancestors"] == []
    ext = prov["external_inputs"]
    assert len(ext) == 1
    assert ext[0]["reason"] == "versionless"
    assert ext[0]["version"] is None
    assert ext[0]["resource"] == "fs"


# --- cycle / dedup guard ----------------------------------------------------


def test_provenance_shared_producer_expanded_once(tmp_path: Path):
    """Two anchor effects read the SAME upstream version. The producer is one
    ancestor (expanded once), and it records both reads under ``produced``."""
    ingest = _txn("t-ingest", [
        _eff("t-ingest", 0, "ingest", writes=[_PK("sku1", 1)]),
    ])
    anchor = _txn("t-diamond", [
        _eff("t-diamond", 0, "read_a", reads=[_PK("sku1", 1)]),
        _eff("t-diamond", 1, "read_b", reads=[_PK("sku1", 1)]),
        _eff("t-diamond", 2, "publish", writes=[["sql", ["catalog", "y"], 9]]),
    ])
    path = str(tmp_path / "diamond.db")
    _write_journal(path, [ingest, anchor])
    with JournalReader(path) as r:
        prov = r.provenance("t-diamond")

    ancestors = [a for a in prov["ancestors"] if a["node"] == "t-ingest#0"]
    assert len(ancestors) == 1  # expanded exactly once despite two readers
    consumed = {p["consumed_by"] for p in ancestors[0]["produced"]}
    assert consumed == {"t-diamond#0", "t-diamond#1"}
    assert len(_produces(prov, "t-ingest#0", "t-diamond#0")) == 1
    assert len(_produces(prov, "t-ingest#0", "t-diamond#1")) == 1


def test_provenance_depth_is_shortest_path_through_a_diamond(tmp_path: Path):
    """A write reachable by both a short (direct) and a long (via a sibling) path
    gets its SHORTEST hop depth, and its own ancestor inherits that minimum — a
    BFS guarantee a depth-first walk would overstate.

        ingest : —                     → prices/p v1   (origin, depth 3? no: 2)
        norm   : reads prices/p v1     → prices/n v2
        markup : reads prices/n v2     → prices/m v3
        anchor : reads prices/n v2 (direct → norm@depth1)
                 reads prices/m v3 (→ markup@depth1, which re-reaches norm@depth2)
    """
    ingest = _txn("t-ingest", [
        _eff("t-ingest", 0, "ingest", writes=[_PK("p", 1)]),
    ])
    norm = _txn("t-norm", [
        _eff("t-norm", 0, "read_p", reads=[_PK("p", 1)]),
        _eff("t-norm", 1, "set_n", writes=[_PK("n", 2)]),
    ])
    markup = _txn("t-markup", [
        _eff("t-markup", 0, "read_n", reads=[_PK("n", 2)]),
        _eff("t-markup", 1, "set_m", writes=[_PK("m", 3)]),
    ])
    anchor = _txn("t-anchor", [
        _eff("t-anchor", 0, "read_n_direct", reads=[_PK("n", 2)]),
        _eff("t-anchor", 1, "read_m", reads=[_PK("m", 3)]),
        _eff("t-anchor", 2, "publish", writes=[["sql", ["catalog", "z"], 9]]),
    ])
    path = str(tmp_path / "diamond_depth.db")
    _write_journal(path, [ingest, norm, markup, anchor])
    with JournalReader(path) as r:
        prov = r.provenance("t-anchor")

    by_node = {a["node"]: a for a in prov["ancestors"]}
    assert by_node["t-norm#1"]["depth"] == 1     # the direct (short) path wins
    assert by_node["t-markup#1"]["depth"] == 1
    assert by_node["t-ingest#0"]["depth"] == 2    # 1 + the shortest depth of norm
    assert prov["scope"]["max_depth"] == 2


# --- honest scope + degenerate inputs ---------------------------------------


def test_provenance_caveat_documents_compounding_boundary(chain_db: str):
    """The scope statement travels with the payload and is explicit that the
    data-lineage boundary compounds with depth."""
    with JournalReader(chain_db) as r:
        prov = r.provenance("t-publish")
    assert prov["caveat"] == PROVENANCE_CAVEAT
    low = PROVENANCE_CAVEAT.lower()
    assert "transitive" in low
    assert "not" in low and "data lineage" in low
    assert "external_inputs" in low
    assert "produces" in low and "informs" in low


def test_provenance_unknown_transaction_is_none(chain_db: str):
    """A txn that isn't in the journal returns None (the HTTP layer → 404)."""
    with JournalReader(chain_db) as r:
        assert r.provenance("t-nope") is None


def test_provenance_writes_only_anchor_has_empty_trace(tmp_path: Path):
    """An anchor that read nothing has no ancestry — the fold invents none —
    but still returns a well-formed payload with its writes."""
    anchor = _txn("t-write", [
        _eff("t-write", 0, "ingest", writes=[_PK("sku1", 1)]),
    ])
    path = str(tmp_path / "writeonly.db")
    _write_journal(path, [anchor])
    with JournalReader(path) as r:
        prov = r.provenance("t-write")
    assert prov["ancestors"] == []
    assert prov["edges"] == []
    assert prov["external_inputs"] == []
    assert prov["target"]["read_count"] == 0
    assert prov["scope"]["max_depth"] == 0
    assert prov["target"]["writes"] == [
        {"resource": "sql", "key": ["prices", "sku1"], "version": 1,
         "by_idx": 0, "by_tool": "ingest"}
    ]


# --- against the real seeded journal ----------------------------------------


def test_provenance_over_seed_journal_reads_are_external(tmp_path: Path):
    """Every seed read observes a version written before the journal began, so
    provenance over a seed txn bottoms out immediately in external inputs and
    has no in-journal ancestor."""
    db = str(tmp_path / "demo.db")
    seed_demo_journal(db)
    with JournalReader(db) as r:
        prov = r.provenance("txn-clean-deploy01")

    assert prov["ancestors"] == []
    ext = prov["external_inputs"]
    assert any(e["resource"] == "sql" and e["key"] == ["releases", "current"]
               and e["version"] == 11 and e["reason"] == "unproduced" for e in ext)
    # the anchor's writes still ride in the target panel
    target_keys = {(w["resource"], tuple(w["key"]), w["version"])
                   for w in prov["target"]["writes"]}
    assert ("sql", ("releases", "current"), 12) in target_keys
