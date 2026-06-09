"""Pherix per-feature demo — AUTHORITY TIERS: the SAME irreversible purge,
attempted by two actors, decided purely by trust tier.

    python examples/demos/authority.py

This is the **authority** axis made concrete. There is one real, irreversible
action — ``purge_records()``: a hard ``DELETE`` of every row in a real SQLite
table, with **no compensator** (there is no semantic inverse for "the rows are
gone"). The agent body runs the *identical* call twice. The only thing that
differs between the two runs is the **actor** stamped on the effect:

  * once as ``actor="untrusted-subagent"``  — a low-authority principal,
  * once as ``actor="admin"``               — a high-authority principal.

A single :class:`TrustTiers` policy governs both, composing the two dual
authority primitives over the engine's existing twice-evaluated (stage +
commit) policy fold:

  * ``untrusted_gates_irreversible(tiers)`` — *tightens* the gate: an actor
    below ``trusted`` may not let an irreversible effect through. For the
    untrusted sub-agent this is a stage-time ``Deny`` → ``PolicyViolation`` →
    the purge **never fires** and the txn unwinds. The rows are intact.
  * ``admin_auto_approves(tiers)`` — *relaxes* the gate: an admin's authority
    *is* the approval. The admin's purge clears the commit-time human gate with
    **no ``approve_irreversible`` call and no human in the loop** — it fires.
    The rows are gone.

Same tool, same args, same engine, same policy. The divergence is attributable
purely to ``effect.actor``. That is the whole pitch of the authority axis:
*who* an effect runs on whose authority is a first-class, commit-time policy
input — not an afterthought a guardrail bolts on.

The mental model (for the maths reader): the irreversible lane *stages* an
effect (records intent, hands the agent a ``StagedResult`` placeholder) and
fires it only at ``commit`` — the forward fold over the journal. The gate is a
predicate that the forward fold must clear before it fires a compensator-less
irreversible. ``untrusted_gates_irreversible`` is a ``Deny`` rule evaluated
*before* the fold even reaches the gate (stage-time, then again at commit-time
— TOCTOU-safe); ``admin_auto_approves`` is the ``Policy.auto_approve`` seam the
gate consults *at* the fold. One actor's effect is denied at the door; the
other's authority unlocks it. No knowledge of *what the DELETE meant* is needed
— the tiers decide on authority alone.

------------------------------------------------------------------------------
Built to the same five-part skeleton as the template ``undo.py``:

  1. TOOLS    — ``purge_records``: the real, irreversible DELETE. It is an
                ``http``-resource tool (``supports_rollback() -> False``) so the
                runtime forces it down the staged/gated lane — Pherix is honest
                that a hard delete cannot be snapshotted away. The tool body
                runs a genuine ``DELETE`` against the real connection it closes
                over.
  2. SEED     — build a *real* SQLite ``records`` table with realistic scale.
  3. SCENARIO — the agent body: one plain call to ``purge_records()``. The same
                body is driven twice, under two different actors.
  4. NARRATE  — print before -> attempt -> after off the REAL table for BOTH
                actors, plus each transaction's journal (with its actor). Every
                number comes from the database; the asserts pin the mechanic.
  5. EMIT     — persist BOTH transactions' journals to one SQLite file (so the
                inspector can render/animate them) AND dump a clip-source JSON
                derived from the same journal.

Run it, then animate it two ways:

    # live console — polls the journal and animates effects landing/blocking
    python -m pherix.inspector --db examples/demos/.out/authority.db
    # then open http://127.0.0.1:8765

    # clip-source JSON for the player (printed path, also under .out/)
    examples/demos/.out/authority.clip.json
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

# Run as `python examples/demos/authority.py` with no editable install: put the
# repo root on the path before importing pherix. (Repo root is three levels up:
# examples/demos/authority.py -> examples/demos -> examples -> repo root.)
_REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO_ROOT))

from pherix import (  # noqa: E402
    AuditJournal,
    HTTPAdapter,
    Policy,
    PolicyViolation,
    agent_txn,
    tool,
)
from pherix.core.policy import (  # noqa: E402
    TrustTiers,
    admin_auto_approves,
    untrusted_gates_irreversible,
)

# Where this run's evidence lands. Gitignored (examples/ + *.db), regenerated
# every run — these are evidence, not source.
OUT_DIR = Path(__file__).resolve().parent / ".out"
JOURNAL_DB = OUT_DIR / "authority.db"
CLIP_JSON = OUT_DIR / "authority.clip.json"

N_RECORDS = 10_000

# The two principals whose authority decides the SAME action's fate.
UNTRUSTED_ACTOR = "untrusted-subagent"
ADMIN_ACTOR = "admin"


# --- 1. TOOLS ---------------------------------------------------------------
#
# The single irreversible action. It is registered on the ``http`` resource
# (``HTTPAdapter.supports_rollback() -> False``) so the runtime forces it down
# the staged/gated lane — exactly as it would for any effect that cannot be
# snapshotted away. There is NO compensator: a hard DELETE has no semantic
# left-inverse, so the ONLY thing that can let it fire is clearing the gate.
# ``injects_handle=False`` because an http-resource tool receives no adapter
# handle; the connection is the one this module closes over at build time.


def make_purge_records(conn: sqlite3.Connection):
    """Build the ``purge_records`` tool bound to a real connection.

    The tool body runs a genuine ``DELETE FROM records`` against ``conn`` and
    returns the row count it removed. Closing over ``conn`` (instead of taking
    an injected handle) is the http-lane idiom — the effect is irreversible, so
    Pherix never snapshots it; when it fires, it fires against the real table.
    """

    @tool(resource="http", reversible=False, injects_handle=False)
    def purge_records(table: str) -> int:
        cur = conn.execute(f"DELETE FROM {table}")
        return cur.rowcount

    return purge_records


# --- 2. SEED ----------------------------------------------------------------


def seed_records(conn: sqlite3.Connection, n: int) -> None:
    """Build a realistic ``records`` table: ``n`` rows of audit-log-like data."""
    conn.execute("DROP TABLE IF EXISTS records")
    conn.execute(
        "CREATE TABLE records ("
        "  id INTEGER PRIMARY KEY,"
        "  owner_id INTEGER NOT NULL,"
        "  kind TEXT NOT NULL,"
        "  payload TEXT NOT NULL"
        ")"
    )
    kinds = ("audit", "metric", "event", "trace")
    rows = [
        (i, i % 500, kinds[i % len(kinds)], f"payload-{i}")
        for i in range(1, n + 1)
    ]
    conn.executemany(
        "INSERT INTO records (id, owner_id, kind, payload) VALUES (?, ?, ?, ?)",
        rows,
    )


def count_records(conn: sqlite3.Connection) -> int:
    """The fact the narrative reads off the real table — never hard-coded."""
    return conn.execute("SELECT COUNT(*) FROM records").fetchone()[0]


# --- 3. SCENARIO ------------------------------------------------------------


def agent_body(purge_records) -> None:
    """The agent's plan: 'purge the records table'. Identical for both actors —
    a plain function calling one irreversible tool; no model, no API key, never
    transaction-aware. The actor is supplied by the surrounding ``agent_txn``;
    the body does not know or care which principal it runs as.
    """
    purge_records(table="records")


# --- 4. NARRATE -------------------------------------------------------------


def narrate(journal: AuditJournal, txn_id: str) -> None:
    """Print one transaction's journal effect-by-effect: tool, actor, status —
    straight off the persisted journal. The actor on each effect is what the
    policy branched on."""
    record = journal.get_transaction(txn_id)
    print(f"    journal  txn={txn_id}  final state = {record['state']}")
    effects = journal.get_effects(txn_id)
    if not effects:
        print(
            "      (empty — the irreversible purge was DENIED at stage-time, "
            "before any effect was journalled: nothing was touched)"
        )
        return
    for e in effects:
        reversible = "reversible" if e["reversible"] else "irreversible"
        print(
            f"      [{e['idx']}] {e['tool']}({e['args']})"
            f"  actor={e['actor']!r}  {reversible}  ->  {e['status']}"
        )
    print(
        "      (STAGED/GATED = never fired; APPLIED = fired live at commit. "
        "The actor decided which.)"
    )


# --- 5. EMIT (clip-source) --------------------------------------------------
#
# Two animate paths share one source of truth: the persisted journal.
#   (a) the inspector reads JOURNAL_DB directly and animates BOTH txns live;
#   (b) emit_clip_source distils the journals into a small player-ready dict —
#       the same {title, situation, events, verdict} shape the template emits,
#       read straight off the journal instead of an agent transcript.


def emit_clip_source(
    journal: AuditJournal,
    *,
    untrusted_txn_id: str,
    admin_txn_id: str,
    title: str,
    situation: str,
    before: int,
    after: int,
) -> dict:
    """Distil the two transactions' journals into a player-ready clip-source.

    Events walk each txn's journal in order: the untrusted attempt (staged then
    gated — never fired) followed by the admin attempt (staged then applied —
    fired by authority). Statuses come from the journal; nothing is invented.
    """

    def _events_for(
        txn_id: str, actor: str, phase: str, *, denied_tool: str | None = None
    ) -> list[dict]:
        effs = journal.get_effects(txn_id)
        evs: list[dict] = [{"k": "phase", "text": phase}]
        # A stage-denied irreversible leaves NOTHING in the journal (the engine
        # evaluates policy before journalling). Surface that as an explicit
        # "denied" event so the player still shows the blocked attempt.
        if not effs and denied_tool is not None:
            evs.append(
                {
                    "k": "denied",
                    "tool": denied_tool,
                    "actor": actor,
                    "args": {"table": "records"},
                    "status": "DENIED (stage-time — never journalled)",
                }
            )
            return evs
        for e in effs:
            fired = e["status"] == "APPLIED"
            evs.append(
                {
                    "k": "applied" if fired else "staged",
                    "idx": e["idx"],
                    "tool": e["tool"],
                    "res": e["resource"],
                    "actor": e["actor"],
                    "args": e["args"],
                    "status": e["status"],
                }
            )
        return evs

    events: list[dict] = [{"k": "say", "text": situation}]
    events += _events_for(
        untrusted_txn_id,
        UNTRUSTED_ACTOR,
        f"actor={UNTRUSTED_ACTOR!r} (untrusted) attempts purge_records — "
        "forced gate denies it",
        denied_tool="purge_records",
    )
    events += _events_for(
        admin_txn_id,
        ADMIN_ACTOR,
        f"actor={ADMIN_ACTOR!r} (admin) attempts the IDENTICAL purge — "
        "authority auto-clears the gate",
    )

    untrusted_state = journal.get_transaction(untrusted_txn_id)["state"]
    admin_state = journal.get_transaction(admin_txn_id)["state"]
    return {
        "title": title,
        "tab": "authority",
        "situation": situation,
        "events": events,
        "before": {"records": before},
        "after": {"records": after},
        "verdict": {
            "kind": "authority",
            "big": "DECIDED BY AUTHORITY",
            "narr": (
                f"Same purge_records, two actors. Untrusted txn -> "
                f"{untrusted_state} (records intact); admin txn -> "
                f"{admin_state} ({before - after:,} rows purged). The actor "
                "alone decided."
            ),
        },
    }


# --- the run ----------------------------------------------------------------


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    # Fresh journal every run so the inspector shows only this run's story.
    for sib in (
        JOURNAL_DB,
        Path(str(JOURNAL_DB) + "-wal"),
        Path(str(JOURNAL_DB) + "-shm"),
    ):
        sib.unlink(missing_ok=True)

    # The real backend. isolation_level=None hands transaction control to the
    # adapter where it participates; the purge runs against this same table.
    conn = sqlite3.connect(":memory:", isolation_level=None)
    seed_records(conn, N_RECORDS)
    purge_records = make_purge_records(conn)

    before = count_records(conn)
    print("PHERIX — AUTHORITY TIERS (same irreversible purge, two actors)")
    print("=" * 70)
    print(f"\n  seeded {before:,} real records")

    # One TrustTiers map governs both runs. Composing the two dual primitives
    # gives the full authority ladder: untrusted -> forced gate (Deny),
    # admin -> auto-approved (the authority IS the approval).
    tiers = TrustTiers(admin=[ADMIN_ACTOR], untrusted=[UNTRUSTED_ACTOR])
    policy = Policy.with_rules(
        rules=[untrusted_gates_irreversible(tiers)],
        auto_approve=admin_auto_approves(tiers),
    )

    # The journal persists to a SQLite file so the inspector can render both
    # transactions.
    journal = AuditJournal(str(JOURNAL_DB))

    # --- ATTEMPT 1: the untrusted sub-agent. The forced-gate rule DENIES the
    # irreversible purge at stage-time; it never fires; the txn unwinds. ------
    print(f"\n  ATTEMPT 1 — actor={UNTRUSTED_ACTOR!r} (untrusted)")
    print("    agent runs its plan: 'purge the records table'")
    untrusted_blocked = False
    untrusted_violation = ""
    untrusted_txn_id: str | None = None
    try:
        with agent_txn(
            {"http": HTTPAdapter()},
            policy=policy,
            audit=journal,
            actor=UNTRUSTED_ACTOR,
        ) as untrusted_txn:
            agent_body(purge_records)
    except PolicyViolation as exc:
        untrusted_blocked = True
        untrusted_violation = str(exc)
        print(f"    -> BLOCKED by policy: {exc}")
    untrusted_txn_id = untrusted_txn.txn_id
    after_untrusted = count_records(conn)
    print(f"    records after untrusted attempt : {after_untrusted:,}")

    # --- ATTEMPT 2: the admin. SAME tool, SAME args. The auto-approve seam
    # clears the gate on authority alone — NO approve_irreversible call. ------
    print(f"\n  ATTEMPT 2 — actor={ADMIN_ACTOR!r} (admin)")
    print("    agent runs the IDENTICAL plan: 'purge the records table'")
    with agent_txn(
        {"http": HTTPAdapter()},
        policy=policy,
        audit=journal,
        actor=ADMIN_ACTOR,
    ) as admin_txn:
        agent_body(purge_records)
        # NOTE: no admin_txn.approve_irreversible(...) — the admin authority
        # clears the gate by itself.
    admin_txn_id = admin_txn.txn_id
    after_admin = count_records(conn)
    print("    -> FIRED: the admin authority auto-cleared the gate")
    print(f"    records after admin attempt     : {after_admin:,}")

    # --- THE PROOF: numbers off the real table + in-code asserts ------------
    print("\n  RESULT")
    print(f"    before                         : {before:,} records")
    print(
        f"    untrusted purge BLOCKED        : "
        f"{untrusted_blocked} (records intact = {after_untrusted == before})"
    )
    print(
        f"    admin purge FIRED              : "
        f"{after_admin == 0} ({before - after_admin:,} rows purged)"
    )

    # The untrusted actor's irreversible purge was DENIED — it never fired, so
    # every row is still there and the transaction did not commit.
    assert untrusted_blocked, (
        "untrusted actor's irreversible purge must be BLOCKED by the forced gate"
    )
    assert after_untrusted == before, (
        "untrusted purge never fired — all records must remain intact"
    )
    from pherix.core.transaction import TxnState  # local: narrative-only symbol

    assert untrusted_txn.txn.state is not TxnState.COMMITTED, (
        "the untrusted transaction must NOT have committed"
    )

    # The admin actor's IDENTICAL purge auto-cleared the gate purely by
    # authority — it fired, and the table is now empty.
    assert after_admin == 0, (
        "admin actor's purge must AUTO-CLEAR the gate and fire — records gone"
    )
    assert admin_txn.txn.state is TxnState.COMMITTED, (
        "the admin transaction must have committed (gate auto-cleared)"
    )

    # Same action, two actors, opposite fate — proven off the real journals.
    #
    # The untrusted attempt was denied at STAGE-time: the engine evaluates
    # policy *before* the effect is journalled, so a denied irreversible leaves
    # NOTHING in the journal — "no resource is touched, no audit row written".
    # That empty journal is itself the proof the purge never reached the table.
    # The admin attempt fired, so its purge_records effect is journalled APPLIED.
    untrusted_effects = journal.get_effects(untrusted_txn_id)
    admin_effects = journal.get_effects(admin_txn_id)
    assert untrusted_effects == [], (
        "the untrusted purge was denied at stage-time — nothing journalled, "
        "the table never touched"
    )
    assert len(admin_effects) == 1, "the admin attempt journalled one effect"
    admin_effect = admin_effects[0]

    # The denial was about the SAME tool the admin ran — captured straight from
    # the PolicyViolation the engine raised on the untrusted actor.
    assert "purge_records" in untrusted_violation, (
        "the untrusted denial must be about the purge_records tool"
    )
    assert UNTRUSTED_ACTOR in untrusted_violation, (
        "the denial must name the untrusted actor it gated on"
    )
    assert admin_effect["tool"] == "purge_records"
    # ``args`` is persisted as a JSON string in the journal — parse to compare.
    assert json.loads(admin_effect["args"]) == {"table": "records"}, (
        "the admin ran the SAME action+args the untrusted actor was denied"
    )
    assert admin_effect["actor"] == ADMIN_ACTOR
    assert admin_effect["status"] == "APPLIED", (
        "the admin effect must have fired"
    )
    print(
        "    SAME action, SAME args, different actor -> opposite outcome  : True"
    )

    # --- NARRATE both transactions off the real journal ---------------------
    print(f"\n  JOURNAL — untrusted attempt (actor={UNTRUSTED_ACTOR!r})")
    narrate(journal, untrusted_txn_id)
    print(f"\n  JOURNAL — admin attempt (actor={ADMIN_ACTOR!r})")
    narrate(journal, admin_txn_id)

    # --- EMIT: clip-source JSON (the alternative animate path) --------------
    clip = emit_clip_source(
        journal,
        untrusted_txn_id=untrusted_txn_id,
        admin_txn_id=admin_txn_id,
        title="Authority tiers — same purge, two actors, opposite fate",
        situation=(
            "An agent attempts the SAME irreversible purge_records() twice — "
            f"once as {UNTRUSTED_ACTOR!r}, once as {ADMIN_ACTOR!r}. A trust-tier "
            "policy gates the untrusted actor and auto-approves the admin, "
            "purely by authority."
        ),
        before=before,
        after=after_admin,
    )
    CLIP_JSON.write_text(json.dumps(clip, indent=2))

    journal.close()
    conn.close()

    print("\n  ANIMATE THIS RUN")
    print("    live console (polls the journal, animates both attempts):")
    print(f"      python -m pherix.inspector --db {JOURNAL_DB}")
    print("      # then open http://127.0.0.1:8765")
    print("    clip-source JSON (player payload):")
    print(f"      {CLIP_JSON}")


if __name__ == "__main__":
    main()
