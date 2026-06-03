"""Pherix flagship demo — LINEAGE + provenance: which reads informed a write.

    python examples/demos/lineage.py

Every feature in Pherix is a *traversal of the same journal*. UNDO folds it
backward. LINEAGE folds it forward and *diffs the read/write keys*: it answers
"this write was informed by these reads, with these verdicts" straight off the
persisted journal — no model run, nothing recomputed from the live world.

The scenario is a real deploy decision. A config table tracks ``deploy_target``,
bumped three times to version 3 (``staging`` -> ``canary`` -> ``production``),
each bump a real committed transaction. A budget table holds a spend ceiling.
Then a deploy agent, in *one* atomic transaction, **READS config v3**, **READS
the budget**, and — informed by exactly those two reads — **WRITES a
``deployments`` record**. Every read/write goes through ``execute_isolated``
against a real SQLite backend, so the journal records genuine
``(resource, key, version)`` keys; the version side-table genuinely reaches 3.

The lineage fold then builds the causal graph off that journal:

  * ``informs`` — co-transactional ordering: the config read at idx 0 and the
    budget read at idx 1 both precede the deploy write at idx 2 in the same
    atomic unit, so each *informs* it. Honest ordering — a read happened before
    a write — NOT proven value-flow through the model.
  * ``produces`` — version-grounded read-after-write: the deploy read observed
    config at *exactly version 3*, and the journal proves version 3 was produced
    by the third config-bump transaction. The version counter ties reader to
    writer. This is the strong claim — a fact the journal can prove.

result-provenance attaches ``informed_by`` to the deploy write's chain: the
reads (config v3 -> ``produced_by`` the v3 writer; budget -> external origin)
that informed its result.

Honest scope (carried *with* the data as ``caveat``): this is **action
provenance** — which reads preceded the write, version-grounded where the
counters line up — NOT **data lineage** through a model. Pherix cannot see that
a value the agent read actually shaped the value it later wrote; only that the
journal records the read before the write. Full data lineage through the model
is out of scope and not claimed.

The mental model (for the maths reader): the journal is an append-only sequence
of effects, each carrying its read/write keys as a set of ``(resource, key,
version)`` triples. ``lineage`` is the *diff* operation over that sequence — it
folds the keys into a causal partial order under happens-before. ``produces`` is
the edge where a read's version equals a write's post-version (provable);
``informs`` is the edge where a read precedes a write inside one atomic unit
(ordering only). The relation is *derived, never stored* — recomputable from the
journal alone, the same way commit and rollback are.

------------------------------------------------------------------------------
This file follows the UNDO template — the same five-part skeleton:

  1. TOOLS    — @tool functions: the real side-effecting operations. Reads and
                writes go through ``execute_isolated`` so the journal records
                genuine ``(resource, key, version)`` keys, the substrate the
                lineage fold diffs over. The agent body is never txn-aware.
  2. SEED     — build a *real* SQLite backend: config + budget + deployments.
  3. SCENARIO — the agent body: bump config to v3 (three real commits), then in
                one transaction read config v3 + budget and write the deploy.
  4. NARRATE  — print the read->write causal chain off the REAL journal via
                JournalReader, plus the in-code asserts proving it's real.
  5. EMIT     — persist the journal to a SQLite file (so the inspector can
                render/animate it) AND dump a clip-source JSON (the alternative
                animate path). Both are derived from the same journal.

Run it, then animate it two ways:

    # live console — polls the journal, renders the lineage graph
    python -m pherix.inspector --db examples/demos/.out/lineage.db
    # then open http://127.0.0.1:8765  (lineage view: GET /api/lineage)

    # clip-source JSON for the player (printed path, also under .out/)
    examples/demos/.out/lineage.clip.json
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

# Run as `python examples/demos/lineage.py` with no editable install: put the
# repo root on the path before importing pherix. (Repo root is three levels up:
# examples/demos/lineage.py -> examples/demos -> examples -> repo root.)
_REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO_ROOT))

from pherix import AuditJournal, SQLiteAdapter, agent_txn, tool  # noqa: E402
from pherix.core.adapters.sql import execute_isolated  # noqa: E402
from pherix.inspector.reader import JournalReader  # noqa: E402

# Where this run's evidence lands. Gitignored (examples/ + *.db), regenerated
# every run — these are evidence, not source.
OUT_DIR = Path(__file__).resolve().parent / ".out"
JOURNAL_DB = OUT_DIR / "lineage.db"
CLIP_JSON = OUT_DIR / "lineage.clip.json"

CONFIG_KEY = "deploy_target"
DEPLOY_TARGETS = ("staging", "canary", "production")  # three bumps -> version 3
BUDGET_CENTS = 5_000_000  # the spend ceiling the deploy decision reads
DEPLOY_ID = "dep-2026-001"


# --- 1. TOOLS ---------------------------------------------------------------
#
# The real side-effecting operations. ``conn`` is injected by the SQL adapter at
# apply-time and hidden from the agent's call-site. Reads and writes route
# through ``execute_isolated`` with explicit ``reads`` / ``writes`` key lists —
# the helper records each as a ``(resource, key, version)`` triple into the
# active Effect, bumping the real version side-table on every write. Those
# triples ARE the substrate the lineage fold diffs over: no keys, no lineage.


@tool(resource="sql")
def set_config(conn: sqlite3.Connection, key: str, value: str) -> str:
    """Set a config value — a real UPSERT, journalled as a write of
    ``("config", key)``. Each call bumps the key's version counter by one, so
    three calls take ``deploy_target`` to version 3."""
    execute_isolated(
        conn,
        "INSERT INTO config (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
        writes=[("config", key)],
    )
    return value


@tool(resource="sql")
def read_config(conn: sqlite3.Connection, key: str) -> str:
    """Read a config value — journalled as a read of ``("config", key)`` at the
    key's *current* version. Reading after three bumps records version 3, the
    exact version the third write produced (the tie that anchors ``produces``)."""
    cur = execute_isolated(
        conn,
        "SELECT value FROM config WHERE key = ?",
        (key,),
        reads=[("config", key)],
    )
    return cur.fetchone()[0]


@tool(resource="sql")
def read_budget(conn: sqlite3.Connection) -> int:
    """Read the spend ceiling — journalled as a read of ``("budget", 1)``. The
    budget was seeded before the journal began, so its value has no producing
    write *in this journal* — the fold honestly flags it external, not a gap."""
    cur = execute_isolated(
        conn,
        "SELECT cents FROM budget WHERE id = 1",
        (),
        reads=[("budget", 1)],
    )
    return cur.fetchone()[0]


@tool(resource="sql")
def record_deploy(
    conn: sqlite3.Connection, deploy_id: str, target: str, budget_cents: int
) -> str:
    """Write the deploy decision — journalled as a write of
    ``("deployments", deploy_id)``. The values come from the two reads above; in
    the journal this write is preceded by both reads in the same atomic
    transaction, which is exactly what makes them *inform* it."""
    execute_isolated(
        conn,
        "INSERT INTO deployments (id, target, budget_cents) VALUES (?, ?, ?)",
        (deploy_id, target, budget_cents),
        writes=[("deployments", deploy_id)],
    )
    return deploy_id


# --- 2. SEED ----------------------------------------------------------------


def seed_backend(conn: sqlite3.Connection) -> None:
    """Build the real backend: a config table, a budget table (with a ceiling),
    and an empty deployments table for the decision to land in."""
    conn.execute("DROP TABLE IF EXISTS config")
    conn.execute("DROP TABLE IF EXISTS budget")
    conn.execute("DROP TABLE IF EXISTS deployments")
    conn.execute("CREATE TABLE config (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    conn.execute("CREATE TABLE budget (id INTEGER PRIMARY KEY, cents INTEGER NOT NULL)")
    conn.execute(
        "CREATE TABLE deployments ("
        "  id TEXT PRIMARY KEY,"
        "  target TEXT NOT NULL,"
        "  budget_cents INTEGER NOT NULL"
        ")"
    )
    conn.execute("INSERT INTO budget (id, cents) VALUES (1, ?)", (BUDGET_CENTS,))


def config_version(adapter: SQLiteAdapter, key: str) -> int:
    """The real version counter for ``("config", key)`` off the side-table —
    the fact the narrative reads, never hard-coded. The side-table keys on the
    full ``(table, pk)`` tuple the tools declared, so we read it with that same
    key — exactly what ``execute_isolated`` recorded as the write key."""
    return adapter.read_version(("config", key))


def config_value(conn: sqlite3.Connection, key: str) -> str:
    return conn.execute("SELECT value FROM config WHERE key = ?", (key,)).fetchone()[0]


def deployment_row(conn: sqlite3.Connection, deploy_id: str) -> tuple:
    return conn.execute(
        "SELECT target, budget_cents FROM deployments WHERE id = ?", (deploy_id,)
    ).fetchone()


# --- 3. SCENARIO ------------------------------------------------------------


def bump_config_to_v3(adapters: dict, journal: AuditJournal) -> list[str]:
    """The setup the deploy decision will read: drive ``deploy_target`` to
    version 3 through three *real, separately committed* transactions
    (staging -> canary -> production). Each commit persists a write of
    ``("config", "deploy_target")`` to the journal, so the v3 producer is a
    real node a later ``produces`` edge can point back to.

    Returns the per-bump transaction ids (for the narrative)."""
    txn_ids: list[str] = []
    for target in DEPLOY_TARGETS:
        with agent_txn(adapters, audit=journal, actor="release-bot") as txn:
            set_config(key=CONFIG_KEY, value=target)
        txn_ids.append(txn.txn_id)
    return txn_ids


def deploy_agent_body() -> tuple[str, int, str]:
    """The agent's plan: 'deploy to whatever config says, recording the budget'.
    A plain function calling tools in sequence — no model, no API key, never
    transaction-aware. It READS config (v3) and the budget, THEN WRITES the
    deploy record informed by both. Returns ``(target, budget, deploy_id)`` for
    the narrative."""
    target = read_config(key=CONFIG_KEY)  # idx 0 — reads config at version 3
    budget = read_budget()  # idx 1 — reads the spend ceiling
    deploy_id = record_deploy(  # idx 2 — the write, informed by both reads
        deploy_id=DEPLOY_ID, target=target, budget_cents=budget
    )
    return target, budget, deploy_id


# --- 4. NARRATE -------------------------------------------------------------


def _find_chain(lineage: dict, *, writes_key: str) -> dict:
    """The one chain in the lineage whose writer touched ``writes_key`` — the
    deploy write's provenance chain (the headline view)."""
    for chain in lineage["chains"]:
        if any(w["key"][0] == writes_key for w in chain["writes"]):
            return chain
    raise AssertionError(f"no chain writes to {writes_key!r}")


def narrate(reader: JournalReader, deploy_txn_id: str) -> dict:
    """Print the read->write causal chain straight off the persisted journal,
    via JournalReader.lineage — the same fold the inspector's /api/lineage
    serves. Returns the lineage dict so the caller can assert on it."""
    lineage = reader.lineage(deploy_txn_id)
    chain = _find_chain(lineage, writes_key="deployments")

    print("\n  LINEAGE  (folded from the journal's read/write keys)")
    print(f"    deploy write : {chain['tool']}  ->  {chain['status']}")
    print("    writes       : "
          + ", ".join(f"{w['key']} v{w['version']}" for w in chain["writes"]))
    print("    informed_by  (the reads that preceded this write):")
    for inf in chain["informed_by"]:
        if inf["produced_by"]:
            origin = f"produced_by {inf['produced_by']} (version-grounded)"
        else:
            origin = "external origin (pre-journal / elsewhere)"
        print(
            f"      - [{inf['idx']}] {inf['tool']} read "
            f"{inf['resource']}:{inf['key']} v{inf['version']}  <-  {origin}"
        )

    produces = [e for e in lineage["edges"] if e["kind"] == "produces"]
    informs = [e for e in lineage["edges"] if e["kind"] == "informs"]
    print("\n    edges")
    for e in produces:
        print(f"      produces : {e['from']} -> {e['to']}  "
              f"({e['key']} v{e['version']})   <- version-grounded")
    for e in informs:
        print(f"      informs  : {e['from']} -> {e['to']}  "
              f"({e['key']})   <- co-transactional ordering")
    return lineage


# --- 5. EMIT (clip-source) --------------------------------------------------
#
# Two animate paths share one source of truth: the persisted journal.
#   (a) the inspector reads JOURNAL_DB directly and serves /api/lineage;
#   (b) emit_clip_source distils the same lineage fold into a small
#       player-ready dict — read straight off the journal, no model run needed.


def emit_clip_source(
    reader: JournalReader,
    deploy_txn_id: str,
    lineage: dict,
    *,
    title: str,
    situation: str,
) -> dict:
    """Distil the deploy transaction's lineage into a player-ready clip-source.

    Events walk the deploy transaction's effects in order (the reads, then the
    write), then append the lineage edges (produces / informs) the fold derived.
    This is the lineage-native sibling of undo.py's clip-source: it needs no
    transcript because the journal already *is* the ordered record, and the
    fold already *is* the provenance."""
    timeline = reader.get_timeline(deploy_txn_id)
    chain = _find_chain(lineage, writes_key="deployments")

    events: list[dict] = [{"k": "say", "text": situation}]
    for e in timeline["effects"]:
        kind = "applied" if e["reversible"] else "staged"
        events.append(
            {
                "k": kind,
                "idx": e["idx"],
                "tool": e["tool"],
                "res": e["resource"],
                "args": e["args"],
                "reads": e["read_keys"],
                "writes": e["write_keys"],
            }
        )

    events.append(
        {"k": "phase", "text": "lineage() — folding the journal into read->write edges"}
    )
    for ed in lineage["edges"]:
        events.append(
            {
                "k": "edge",
                "kind": ed["kind"],
                "from": ed["from"],
                "to": ed["to"],
                "key": ed["key"],
                "version": ed["version"],
            }
        )

    produces = [e for e in lineage["edges"] if e["kind"] == "produces"]
    informs = [e for e in lineage["edges"] if e["kind"] == "informs"]
    return {
        "title": title,
        "tab": "lineage",
        "situation": situation,
        "events": events,
        "chain": {
            "node": chain["node"],
            "tool": chain["tool"],
            "writes": chain["writes"],
            "informed_by": chain["informed_by"],
        },
        "caveat": lineage["caveat"],
        "verdict": {
            "kind": "provenance",
            "big": "DEPLOY WRITE — PROVENANCE BUILT",
            "narr": (
                f"deploy write informed by {len(chain['informed_by'])} read(s); "
                f"{len(produces)} version-grounded produces edge(s), "
                f"{len(informs)} co-transactional informs edge(s) — "
                f"all folded from the journal, none stored."
            ),
        },
    }


# --- the run ----------------------------------------------------------------


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    # Fresh journal every run so the inspector shows only this run's story.
    for sib in (JOURNAL_DB, Path(str(JOURNAL_DB) + "-wal"), Path(str(JOURNAL_DB) + "-shm")):
        sib.unlink(missing_ok=True)

    # The real backend. isolation_level=None hands all BEGIN/SAVEPOINT/COMMIT/
    # ROLLBACK control to the adapter — Pherix owns the transaction bracket.
    conn = sqlite3.connect(":memory:", isolation_level=None)
    seed_backend(conn)

    print("PHERIX — LINEAGE + provenance (which reads informed a write)")
    print("=" * 70)
    print(f"\n  seeded real backend: config + budget (ceiling {BUDGET_CENTS:,}c) + deployments")

    # The journal persists to a SQLite file so the inspector can render it.
    journal = AuditJournal(str(JOURNAL_DB))
    adapter = SQLiteAdapter(conn)
    adapters = {"sql": adapter}

    # --- setup: bump config to v3, three real commits ---
    print("\n  release-bot bumps deploy_target three times (each a real commit):")
    bump_txn_ids = bump_config_to_v3(adapters, journal)
    for target, txn_id in zip(DEPLOY_TARGETS, bump_txn_ids):
        v = adapter.read_version(("config", CONFIG_KEY)) if target == DEPLOY_TARGETS[-1] else None
        marker = f"   <- version {v}" if v is not None else ""
        print(f"    set deploy_target = {target!r}   (txn {txn_id}){marker}")
    cfg_v = config_version(adapter, CONFIG_KEY)
    print(f"  config now : {CONFIG_KEY} = {config_value(conn, CONFIG_KEY)!r}  at version {cfg_v}")
    assert cfg_v == 3, "config must be at version 3 after three real bumps"

    # --- the deploy decision: read config v3 + budget, then write ---
    print("\n  deploy-agent runs its plan: 'deploy to whatever config says'")
    with agent_txn(adapters, audit=journal, actor="deploy-agent") as deploy_txn:
        target, budget, deploy_id = deploy_agent_body()
        print(f"    read config  : deploy_target = {target!r}  (at version {cfg_v})")
        print(f"    read budget  : {budget:,}c")
        print(f"    wrote deploy : {deploy_id} -> target={target!r}, budget={budget:,}c")
    deploy_txn_id = deploy_txn.txn_id

    # The deploy actually landed in the real table.
    row = deployment_row(conn, deploy_id)
    assert row == (target, budget), "the deploy write must have landed in the real table"

    # --- NARRATE off the REAL journal via JournalReader ---
    journal.close()  # flush; reopen read-only through the reader
    reader = JournalReader(str(JOURNAL_DB))
    lineage = narrate(reader, deploy_txn_id)

    # --- THE PROOF: asserts off the real journal's lineage fold ---
    #
    # 1. result-provenance: the deploy write's chain is informed_by BOTH the
    #    config-v3 read AND the budget read.
    chain = _find_chain(lineage, writes_key="deployments")
    informed_keys = {(i["resource"], tuple(i["key"]), i["version"]) for i in chain["informed_by"]}
    # Keys are the full ``(table, pk)`` tuples the tools declared, normalised by
    # the reader to lists; we compare against the same tuples here.
    config_read = ("sql", ("config", CONFIG_KEY), 3)
    budget_read = ("sql", ("budget", 1), 0)  # budget seeded pre-journal -> version 0
    assert config_read in informed_keys, (
        f"deploy write must be informed_by the config-v3 read; got {informed_keys}"
    )
    assert budget_read in informed_keys, (
        f"deploy write must be informed_by the budget read; got {informed_keys}"
    )

    # 2. the config-v3 read in the chain is version-grounded to the v3 writer
    #    (produced_by a real node), not invented.
    config_inf = next(
        i for i in chain["informed_by"] if i["key"] == ["config", CONFIG_KEY]
    )
    assert config_inf["version"] == 3, "the informing config read must be at version 3"
    assert config_inf["produced_by"] is not None, (
        "the config-v3 read must be produced_by a real writer node, not external"
    )

    # 3. a version-grounded `produces` edge links the config-v3 read to the
    #    write's chain (the v3 writer -> the deploy read of v3).
    produces = [
        e for e in lineage["edges"]
        if e["kind"] == "produces"
        and e["key"] == ["config", CONFIG_KEY]
        and e["version"] == 3
    ]
    assert len(produces) == 1, f"expected exactly one config-v3 produces edge; got {produces}"
    # the edge lands on the deploy transaction's read effect (idx 0)...
    assert produces[0]["to"] == f"{deploy_txn_id}#0", (
        "the produces edge must land on the deploy transaction's config read"
    )
    # ...and originates from the exact v3 writer that produced the read's value.
    assert produces[0]["from"] == config_inf["produced_by"], (
        "the produces edge's source must be the same writer that produced the config-v3 read"
    )

    # 4. both reads `informs` the write (co-transactional ordering inside the
    #    deploy transaction).
    informs = {(e["key"][0], e["to"]) for e in lineage["edges"] if e["kind"] == "informs"}
    assert ("config", chain["node"]) in informs, "config read must inform the deploy write"
    assert ("budget", chain["node"]) in informs, "budget read must inform the deploy write"

    n_produces = len([e for e in lineage["edges"] if e["kind"] == "produces"])
    n_informs = len([e for e in lineage["edges"] if e["kind"] == "informs"])
    print("\n  RESULT")
    print(f"    deploy write informed_by {len(chain['informed_by'])} read(s): config v3 + budget")
    print(f"    {n_produces} version-grounded produces edge(s), {n_informs} informs edge(s)")
    print("    every edge folded from the real journal — none stored.  asserts PASS.")

    # The honest-scope caveat travels with the data.
    print("\n  SCOPE (carried as lineage['caveat']):")
    print(f"    {lineage['caveat']}")

    # EMIT: clip-source JSON (the alternative animate path).
    clip = emit_clip_source(
        reader,
        deploy_txn_id,
        lineage,
        title="Lineage — which reads informed a deploy write",
        situation=(
            "A deploy agent reads config (version 3) and the budget, then writes a "
            "deployment record informed by both."
        ),
    )
    CLIP_JSON.write_text(json.dumps(clip, indent=2))

    reader.close()
    conn.close()

    print("\n  ANIMATE THIS RUN")
    print("    live console (renders the lineage graph from the journal):")
    print(f"      python -m pherix.inspector --db {JOURNAL_DB}")
    print("      # then open http://127.0.0.1:8765   (lineage: GET /api/lineage)")
    print("    clip-source JSON (player payload):")
    print(f"      {CLIP_JSON}")


if __name__ == "__main__":
    main()
