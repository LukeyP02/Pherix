"""Pherix flagship demo — GATE + APPROVE-OVER-THE-WIRE: an irreversible wire,
held at the gate, then cleared from outside the agent's process.

    python examples/demos/gate.py

This is the second pillar of the pitch, the mirror of UNDO. Undo answers "what
about the wrong write we CAN take back?" — snapshot, then ``ROLLBACK TO
SAVEPOINT``. This demo answers the harder half: **"what about the action we
CANNOT take back?"** An external wire transfer — ``POST`` to a bank, charge a
card, fire a webhook — has no before-state to restore. There is no inverse the
database can run. So Pherix does the only honest thing: it refuses to let the
effect *happen at all* until someone with authority says so.

The scenario is a real SQLite **treasury**. The agent does some legitimate,
fully-reversible bookkeeping (a journal entry moving money between two internal
ledger accounts — executed live, behind a real ``SAVEPOINT``). Then it attempts
the dangerous step: ``wire_funds(dest, 480_000)`` — $4,800.00 leaving the
building over an irreversible HTTP-style tool with **no compensator**. Because
the HTTP adapter reports ``supports_rollback() -> False``, that effect never
runs live: it is *staged* as intent. At ``commit()`` it hits **the gate** and
blocks (``GateBlocked``) — the wire has NOT fired.

Then the gate is cleared **over the wire**: a reviewer ("cfo"), in a *different*
process, hands the proxy/MCP gateway the opaque token the gate produced and
calls ``approve(token, approver="cfo")``. That writes one APPROVED row to the
shared journal — it touches no resource and fires nothing. Back in the agent
process, a resumed ``commit(pending_approval=True)`` reads the journalled
approval, the gate clears, and *now* the wire fires — exactly once.

The proof is a real in-process side-effect recorder standing in for the external
bank: every actual wire appends to it. The asserts read that recorder, not a
flag we set — **before approval it is empty (zero calls); after approval it holds
exactly one wire**; and the journal records "cfo" as the approver.

The mental model (for the maths reader): the journal is an append-only sequence
of effects; ``commit`` is a forward fold over it. A reversible effect folds
through immediately (its adapter triple ``(snapshot, apply, restore)`` makes it
undoable). An irreversible effect with no semantic inverse cannot fold *unless*
a predicate clears it — and that predicate is *approval*. ``commit`` is
therefore a **guarded fold**: it pauses at the first unapproved irreversible.
"Over the wire" means the approval predicate is satisfied by a row another
process wrote to the shared journal — the resume is just the same fold, re-run,
now that the guard is true. TOCTOU safety survives because policy is
re-evaluated on every resumed fold (a revocation between approval and resume
still wins).

------------------------------------------------------------------------------
SAME FIVE-PART SKELETON as undo.py (the template):

  1. TOOLS    — @tool functions: ``post_ledger_entry`` (reversible, sql) and
                ``wire_funds`` (irreversible, http, no compensator). The wire
                tool calls a real in-process recorder — the external bank.
  2. SEED     — build a *real* SQLite treasury: accounts + balances.
  3. SCENARIO — the agent body: reversible bookkeeping, then the wire attempt.
                A plain function calling tools; no model, never txn-aware.
  4. NARRATE  — print before -> staged -> blocked -> approved -> fired off the
                REAL treasury + the journal. Every number read from state.
  5. EMIT     — persist the journal to a SQLite file (inspector-animatable) AND
                dump a clip-source JSON. Both derived from the same journal.

Run it, then animate it two ways:

    # live console — polls the journal and animates the gate clearing
    python -m pherix.inspector --db examples/demos/.out/gate.db
    # then open http://127.0.0.1:8765

    # clip-source JSON for the player (printed path, also under .out/)
    examples/demos/.out/gate.clip.json
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

# Run as `python examples/demos/gate.py` with no editable install: put the repo
# root on the path before importing pherix. (Repo root is three levels up:
# examples/demos/gate.py -> examples/demos -> examples -> repo root.)
_REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO_ROOT))

from pherix import (  # noqa: E402
    AuditJournal,
    GateBlocked,
    HTTPAdapter,
    InProcessMCPClient,
    PendingApproval,
    PherixGateway,
    Policy,
    SQLiteAdapter,
    agent_txn,
    tool,
)
from pherix.core.transaction import TxnState  # noqa: E402

# Where this run's evidence lands. Gitignored (examples/ + *.db), regenerated
# every run — these are evidence, not source.
OUT_DIR = Path(__file__).resolve().parent / ".out"
JOURNAL_DB = OUT_DIR / "gate.db"
CLIP_JSON = OUT_DIR / "gate.clip.json"

# The dangerous step: $4,800.00 leaving the building, irreversibly.
WIRE_DEST = "acme-supplier-bank"
WIRE_CENTS = 480_000


# --- the external bank (a REAL in-process side-effect recorder) -------------
#
# This stands in for the external service ``wire_funds`` POSTs to. It is the
# heart of the "no fakery" requirement: the asserts read THIS, not a flag the
# demo flips. ``calls`` is appended to ONLY when the wire genuinely executes —
# so "zero calls before approval" is a real measurement of whether the
# irreversible effect happened, and "exactly one after" proves it fired once.


class ExternalBank:
    """The irreversible side-effect sink. Every real wire lands here."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def send_wire(self, dest: str, amount_cents: int) -> dict:
        # In production this is an HTTP POST to a payment rail — unsnapshot-able,
        # un-rollback-able. Here it appends to a list so the demo can prove,
        # by reading the list, exactly when (and how often) it ran.
        confirmation = f"wire-{len(self.calls) + 1:04d}"
        self.calls.append(
            {"dest": dest, "amount_cents": amount_cents, "confirmation": confirmation}
        )
        return {"confirmation": confirmation, "dest": dest, "amount_cents": amount_cents}


BANK = ExternalBank()


# --- 1. TOOLS ---------------------------------------------------------------
#
# Two tools, two lanes. ``post_ledger_entry`` touches the SQL treasury and is
# reversible (snapshot/restore via SAVEPOINT). ``wire_funds`` is the
# irreversible HTTP-style effect: no handle injected, no compensator — it fires
# the real external call itself, and the runtime can only stage + gate it.


@tool(resource="sql")
def post_ledger_entry(
    conn: sqlite3.Connection, src: str, dest: str, amount_cents: int
) -> dict:
    """Move ``amount_cents`` between two internal ledger accounts.

    This is the *reversible* bookkeeping: a double-entry transfer inside our own
    treasury, executed live behind a real ``SAVEPOINT``. Parameterised SQL
    throughout. If anything downstream goes wrong, ``rollback()`` restores both
    balances exactly — no guessing.
    """
    conn.execute(
        "UPDATE accounts SET balance_cents = balance_cents - ? WHERE name = ?",
        (amount_cents, src),
    )
    conn.execute(
        "UPDATE accounts SET balance_cents = balance_cents + ? WHERE name = ?",
        (amount_cents, dest),
    )
    return {"src": src, "dest": dest, "amount_cents": amount_cents}


@tool(resource="http", reversible=False, injects_handle=False)
def wire_funds(dest: str, amount_cents: int) -> dict:
    """Send a real external wire — irreversible, no compensator.

    The HTTP adapter reports ``supports_rollback() -> False``, so this never
    runs at stage-time: it is recorded as intent and deferred to ``commit()``,
    where it meets the gate. Only when the gate clears does the body run — and
    when it does, it fires the genuine external call (``BANK.send_wire``). There
    is no inverse: once this returns, $4,800 has left the building.
    """
    return BANK.send_wire(dest, amount_cents)


# --- 2. SEED ----------------------------------------------------------------


def seed_treasury(conn: sqlite3.Connection) -> None:
    """Build a real ``accounts`` treasury with starting balances (in cents)."""
    conn.execute("DROP TABLE IF EXISTS accounts")
    conn.execute(
        "CREATE TABLE accounts ("
        "  id INTEGER PRIMARY KEY,"
        "  name TEXT NOT NULL UNIQUE,"
        "  balance_cents INTEGER NOT NULL"
        ")"
    )
    rows = [
        (1, "operating", 2_500_000),   # $25,000.00
        (2, "payroll", 1_200_000),     # $12,000.00
        (3, "escrow", 800_000),        # $8,000.00
    ]
    conn.executemany(
        "INSERT INTO accounts (id, name, balance_cents) VALUES (?, ?, ?)", rows
    )


def balances(conn: sqlite3.Connection) -> dict[str, int]:
    """The fact the narrative reads off the real table — never hard-coded."""
    return {
        row[0]: row[1]
        for row in conn.execute(
            "SELECT name, balance_cents FROM accounts ORDER BY name"
        ).fetchall()
    }


def balance_of(conn: sqlite3.Connection, name: str) -> int:
    return conn.execute(
        "SELECT balance_cents FROM accounts WHERE name = ?", (name,)
    ).fetchone()[0]


# --- 3. SCENARIO ------------------------------------------------------------


def reversible_bookkeeping() -> dict:
    """The agent's safe first step: rebalance internal accounts.

    Moves $4,800 from operating into escrow to fund the upcoming supplier
    payment. Fully reversible — it touches only our own ledger.
    """
    return post_ledger_entry(src="operating", dest="escrow", amount_cents=WIRE_CENTS)


def attempt_wire() -> PendingApproval | dict:
    """The agent's dangerous second step: wire the escrowed funds out.

    A plain tool call; the agent does not know it is irreversible. The runtime
    routes it to the HTTP adapter, sees it cannot roll back, and stages it.
    """
    return wire_funds(dest=WIRE_DEST, amount_cents=WIRE_CENTS)


# --- 4. NARRATE -------------------------------------------------------------


def _dollars(cents: int) -> str:
    return f"${cents / 100:,.2f}"


def _fmt_balances(b: dict[str, int]) -> str:
    return ", ".join(f"{k}={_dollars(v)}" for k, v in b.items())


def narrate(journal: AuditJournal, txn_id: str) -> None:
    """Print the journal effect-by-effect and the approval trail — straight off
    the persisted journal, nothing hard-coded."""
    record = journal.get_transaction(txn_id)
    print(f"\n  journal  txn={txn_id}  final state = {record['state']}")
    for e in journal.get_effects(txn_id):
        reversible = "reversible" if e["reversible"] else "irreversible"
        print(
            f"    [{e['idx']}] {e['tool']}({e['args']})"
            f"  {reversible}  ->  {e['status']}"
        )
    for a in journal.get_approvals(txn_id):
        who = a["approver"] if a["approver"] is not None else "(none)"
        print(
            f"    approval  effect={a['effect_id']}  status={a['status']}"
            f"  approver={who}"
        )
    print(
        "    (STAGED = recorded as intent, never ran live; APPLIED = fired at "
        "commit AFTER the gate cleared)"
    )


# --- 5. EMIT (clip-source) --------------------------------------------------
#
# Two animate paths share one source of truth: the persisted journal.
#   (a) the inspector reads JOURNAL_DB directly and animates it live;
#   (b) emit_clip_source distils the same journal into a small player-ready dict
#       — the same {title, situation, events, verdict} shape undo.py emits, but
#       walking the gate/approve story off the journal (no model run needed).


def emit_clip_source(
    journal: AuditJournal,
    txn_id: str,
    *,
    title: str,
    situation: str,
    before: dict[str, int],
    after: dict[str, int],
    approver: str,
) -> dict:
    """Distil one transaction's journal into a player-ready clip-source dict.

    Events walk the journal in order (reversible applied, irreversible staged),
    then append the gate-block, the over-the-wire approval, and the fire — each
    derived from the journal's effect statuses and approval rows, never from a
    transcript.
    """
    effects = journal.get_effects(txn_id)
    record = journal.get_transaction(txn_id)
    approvals = journal.get_approvals(txn_id)

    events: list[dict] = [{"k": "say", "text": situation}]
    for e in effects:
        kind = "applied" if e["reversible"] else "staged"
        events.append(
            {
                "k": kind,
                "idx": e["idx"],
                "tool": e["tool"],
                "res": e["resource"],
                "args": e["args"],
            }
        )

    # The gate: any irreversible effect that needed an approval blocked first.
    gated = [e["idx"] for e in effects if not e["reversible"]]
    if gated:
        events.append(
            {"k": "phase", "text": "commit() — gate blocks the irreversible wire"}
        )
        events.append({"k": "gate", "idxs": gated})

    # The over-the-wire approval (read off the journal's approval rows).
    for a in approvals:
        if a["status"] == "APPROVED":
            events.append(
                {
                    "k": "approve",
                    "effect_id": a["effect_id"],
                    "approver": a["approver"],
                }
            )

    # The fire: any irreversible that ended APPLIED ran after the gate cleared.
    fired = [e["idx"] for e in effects if not e["reversible"] and e["status"] == "APPLIED"]
    if fired:
        events.append({"k": "phase", "text": "resume commit() — the wire fires"})
        events.append({"k": "fire", "idxs": fired})

    committed = record["state"] == "COMMITTED"
    return {
        "title": title,
        "tab": "gate",
        "situation": situation,
        "events": events,
        "before": before,
        "after": after,
        "verdict": {
            "kind": "gated-then-approved" if committed else "gated",
            "big": "GATED → APPROVED OVER WIRE → FIRED" if committed else "GATED",
            "narr": (
                f"{_dollars(WIRE_CENTS)} wire staged, gate blocked it (0 calls), "
                f"approved over the wire by '{approver}', then fired exactly once."
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
    seed_treasury(conn)

    before = balances(conn)
    print("PHERIX — GATE (the irreversible wire, held then cleared over the wire)")
    print("=" * 70)
    print("\n  seeded a real SQLite treasury")
    print(f"  before  : {_fmt_balances(before)}")
    print(f"  wire    : {_dollars(WIRE_CENTS)} -> {WIRE_DEST} (irreversible, no undo)")

    # The journal persists to a SQLite file so the inspector can render it.
    journal = AuditJournal(str(JOURNAL_DB))
    adapters = {"sql": SQLiteAdapter(conn), "http": HTTPAdapter()}

    # ----------------------------------------------------------------------
    # PROBE: the hard gate, in its own throwaway transaction. Prove that the
    # default commit() genuinely raises GateBlocked and the wire does NOT fire —
    # the strongest containment property — BEFORE we run the resumable
    # approve-over-wire path. (A single txn cannot both hard-gate-to-rollback
    # AND resume, so the hard-gate proof gets its own txn against a probe
    # journal; the treasury connection is untouched.)
    print("\n  PROBE — does the hard gate really block?")
    probe_journal = AuditJournal(":memory:")
    calls_at_probe = len(BANK.calls)
    gate_raised = False
    with agent_txn({"http": HTTPAdapter()}, audit=probe_journal) as probe_txn:
        wire_funds(dest=WIRE_DEST, amount_cents=WIRE_CENTS)  # stage only
        try:
            probe_txn.commit()  # default hard gate
        except GateBlocked as exc:
            gate_raised = True
            print(f"    commit() raised GateBlocked: {len(exc.needs_approval)} effect(s) need approval")
    assert gate_raised, "default commit() must raise GateBlocked on an unapproved wire"
    assert len(BANK.calls) == calls_at_probe, "the wire must NOT fire when the gate blocks"
    assert probe_txn.txn.state is TxnState.ROLLED_BACK, "hard gate unwinds the txn"
    probe_journal.close()
    print(f"    -> blocked, txn rolled back, external wires fired so far: {len(BANK.calls)}")

    # ----------------------------------------------------------------------
    # THE REAL SCENARIO: reversible bookkeeping, then the wire — held at the
    # gate, approved over the wire, then fired. Run on the persisted journal.
    print("\n  agent runs its plan: 'fund escrow, then wire the supplier'")
    with agent_txn(adapters, audit=journal, actor="treasury-agent") as txn:
        # Step 1: reversible bookkeeping — executes LIVE behind a SAVEPOINT.
        entry = reversible_bookkeeping()
        mid = balances(conn)
        print(
            f"  -> posted ledger entry {_dollars(entry['amount_cents'])} "
            f"{entry['src']} -> {entry['dest']} (live, reversible)"
        )
        print(f"  mid     : {_fmt_balances(mid)}")

        # Step 2: the irreversible wire — STAGED, not run.
        staged = attempt_wire()
        calls_before_gate = len(BANK.calls)
        print(f"\n  -> wire_funds STAGED (effect_id={staged.effect_id}); it has NOT run")
        print(f"     external wires fired so far: {calls_before_gate}")
        assert calls_before_gate == 0, "staging must not fire the wire"

        # Step 3: commit() — the gate blocks the unapproved irreversible.
        pending = txn.commit(pending_approval=True)
        assert len(pending) == 1, "exactly one staged irreversible should gate"
        assert isinstance(pending[0], PendingApproval)
        assert pending[0].effect_id == staged.effect_id
        token = pending[0].token
        print("\n  commit() -> GATE BLOCKED: the wire is held, not fired")
        print(f"     external wires fired so far: {len(BANK.calls)}")
        assert len(BANK.calls) == 0, "the gate must NOT fire the wire before approval"
        assert txn.txn.state is TxnState.OPEN, "gated txn stays open for resume"

        # Step 4: APPROVE OVER THE WIRE. A reviewer ('cfo') in another process
        # reaches the proxy/MCP gateway — its OWN journal connection on the SAME
        # on-disk DB — and records the approval. The gateway writes one row; it
        # touches no resource and fires nothing.
        gateway = PherixGateway(
            adapters={"http": HTTPAdapter()},
            default_policy=Policy.allow_all(),
            audit=AuditJournal(str(JOURNAL_DB)),  # same DB file, different conn
        )
        client = InProcessMCPClient(gateway)
        client.initialize("cfo-review-service")
        resp = client.approve(token, approver="cfo")
        row = client.result_of(resp)
        print(
            f"\n  approve OVER THE WIRE via the gateway: "
            f"approver={row['approver']!r}, status={row['status']}"
        )
        assert row["approver"] == "cfo", "the approver must be recorded as 'cfo'"
        assert row["status"] == "APPROVED"
        assert len(BANK.calls) == 0, "the gateway write alone must fire nothing"

        # Step 5: resume commit() — the journalled approval clears the gate.
        resumed = txn.commit(pending_approval=True)
        assert resumed == [], "a cleared gate leaves no pending handles"
        print("\n  resume commit() -> gate cleared by the journalled approval; the wire FIRES")

    after = balances(conn)
    print(f"\n  after   : {_fmt_balances(after)}")

    # THE PROOF — every number read off real post-state, never hard-coded.
    print("\n  RESULT")
    print(f"    external wires fired total : {len(BANK.calls)}")
    assert len(BANK.calls) == 1, "the wire must fire EXACTLY once, after approval"
    wire = BANK.calls[0]
    print(
        f"    the one wire : {_dollars(wire['amount_cents'])} -> {wire['dest']} "
        f"(confirmation {wire['confirmation']})"
    )
    assert wire == {
        "dest": WIRE_DEST,
        "amount_cents": WIRE_CENTS,
        "confirmation": "wire-0001",
    }, "the fired wire must be the exact staged effect"

    # The approver is recorded on the journal as 'cfo'.
    txn_id = txn.txn_id
    approvals = journal.get_approvals(txn_id)
    assert len(approvals) == 1, "exactly one approval row"
    assert approvals[0]["status"] == "APPROVED"
    assert approvals[0]["approver"] == "cfo", "journal must record approver 'cfo'"
    print(f"    journal approver recorded  : {approvals[0]['approver']!r}")

    # The treasury balances moved by exactly the bookkeeping (operating->escrow);
    # the wire left escrow funded but is an EXTERNAL effect, so internal totals
    # are conserved by the reversible leg.
    internal_total_before = sum(before.values())
    internal_total_after = sum(after.values())
    assert internal_total_before == internal_total_after, (
        "the reversible leg conserves internal funds; the wire is external"
    )
    print(
        f"    internal funds conserved   : "
        f"{_dollars(internal_total_before)} -> {_dollars(internal_total_after)}"
    )

    narrate(journal, txn_id)

    # EMIT: clip-source JSON (the alternative animate path).
    clip = emit_clip_source(
        journal,
        txn_id,
        title="Gate an irreversible wire, approve it over the wire",
        situation=(
            f"Agent wires {_dollars(WIRE_CENTS)} out; the gate holds it until "
            f"'cfo' approves over the wire."
        ),
        before=before,
        after=after,
        approver="cfo",
    )
    CLIP_JSON.write_text(json.dumps(clip, indent=2))

    journal.close()
    conn.close()

    print("\n  ANIMATE THIS RUN")
    print("    live console (polls the journal, animates the gate clearing):")
    print(f"      python -m pherix.inspector --db {JOURNAL_DB}")
    print("      # then open http://127.0.0.1:8765")
    print("    clip-source JSON (player payload):")
    print(f"      {CLIP_JSON}")


if __name__ == "__main__":
    main()
