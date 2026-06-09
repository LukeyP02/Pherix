"""The policy axis — JournalReader.policy(), the per-rule decision ledger.

contention() folds the journal per resource/key (where agents collide);
accountability() folds it per actor (on whose authority). This is the third
leg of the governance trio: a fold per **rule** — *what is the policy actually
doing across the whole journal?* reliability() already surfaces a flat
``denials`` reasons rollup; this generalises it: every recorded verdict (allow
*and* deny) grouped by the primitive ``(kind, rule_name)`` that emitted it, with
the allow/deny split, per-rule deny reasons, the stage-vs-commit phase split, and
the tool/resource reach off the joined effect — plus a journal-wide ``phases``
matrix (where in the stage→commit bracket the policy bites).

The tests pin a hand-built journal of transactions + effects + verdicts (written
through the real ``Effect`` / ``AuditJournal.record_verdicts`` so the rows are
byte-for-byte what the engine writes — the same approach the accountability /
contention suites use) so a drift in either the fold or the verdicts schema fails
loudly: the per-rule grouping and ranking (loudest denier first), the deny_rate
taken over a rule's OWN firings, the reasons ordering, the unnamed bucket that
collapses every rule_name-less verdict regardless of kind, the LEFT-JOIN
tolerance of a verdict whose effect row is absent, the journal-wide phase matrix,
the totals, plus the empty-journal zero and the NULL-tolerant degradation on a
journal that predates the ``verdicts`` table (``supported = false``).

Offline: a hand-seeded SQLite journal and a localhost server.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import pytest

from pherix.core.audit import AuditJournal
from pherix.core.effects import Effect, EffectStatus
from pherix.core.transaction import Transaction, TxnState
from pherix.inspector.reader import POLICY_CAVEAT, JournalReader
from pherix.inspector.seed import seed_demo_journal
from pherix.inspector.server import make_server


# --- a hand-built journal of recorded verdicts ------------------------------
#
# Four transactions exercise every dimension of the fold. Each effect is keyed
# by (txn, idx); each verdict joins to its effect by effect_index for the tool /
# resource it governed.
#
#   txn-1 (real)  e0 charge_card/http   stage  allow  rule  allow_all
#                                        commit deny   cap   Cap.daily_spend  "over daily cap"
#                 e1 send_email/smtp     stage  allow  rule  allow_all
#                                        stage  deny   rule  no_secrets       "body contains a secret"
#   txn-2 (real)  e0 charge_card/http    commit deny   cap   Cap.daily_spend  "over daily cap"
#                                        stage  allow  allowlist tools_allowlist
#   txn-3 (DRY)   e0 write_file/fs       stage  deny   rule  no_secrets       "path is sensitive"
#                                        stage  allow  allowlist tools_allowlist
#   txn-4 (real)  e0 query/sql           commit allow  rule  <unnamed>
#                                        commit deny   cap   <unnamed>         "anonymous cap"
#                 (no effect idx 5)      stage  allow  rule  orphan_rule  -> LEFT-JOIN miss
#
# A dry-run's verdicts (txn-3) are folded in — policy decisions are real
# regardless of dry-run, the same scope reliability()'s denial rollup uses.


def _eff(txn_id: str, idx: int, tool: str, resource: str) -> Effect:
    return Effect(
        txn_id=txn_id,
        index=idx,
        tool=tool,
        args={"i": idx},
        resource=resource,
        reversible=True,
        status=EffectStatus.APPLIED,
        ts=datetime(2025, 1, 1, tzinfo=timezone.utc),
    )


def _seed_policy(path: str) -> None:
    journal = AuditJournal(path)
    try:
        # txn-1 — two effects, both phases, a cap deny and a rule deny
        t1 = Transaction(txn_id="txn-1", state=TxnState.COMMITTED)
        t1.effects = [
            _eff("txn-1", 0, "charge_card", "http"),
            _eff("txn-1", 1, "send_email", "smtp"),
        ]
        journal.record_transaction(t1, dry_run=False)
        for e in t1.effects:
            journal.record_effect(e)
        journal.record_verdicts("txn-1", [
            {"effect_index": 0, "phase": "stage", "allow": True,
             "kind": "rule", "rule_name": "allow_all", "reason": None},
            {"effect_index": 0, "phase": "commit", "allow": False,
             "kind": "cap", "rule_name": "Cap.daily_spend",
             "reason": "over daily cap"},
            {"effect_index": 1, "phase": "stage", "allow": True,
             "kind": "rule", "rule_name": "allow_all", "reason": None},
            {"effect_index": 1, "phase": "stage", "allow": False,
             "kind": "rule", "rule_name": "no_secrets",
             "reason": "body contains a secret"},
        ])

        # txn-2 — the cap denies a second time (same reason), an allowlist allows
        t2 = Transaction(txn_id="txn-2", state=TxnState.ROLLED_BACK)
        t2.effects = [_eff("txn-2", 0, "charge_card", "http")]
        journal.record_transaction(t2, dry_run=False)
        for e in t2.effects:
            journal.record_effect(e)
        journal.record_verdicts("txn-2", [
            {"effect_index": 0, "phase": "commit", "allow": False,
             "kind": "cap", "rule_name": "Cap.daily_spend",
             "reason": "over daily cap"},
            {"effect_index": 0, "phase": "stage", "allow": True,
             "kind": "allowlist", "rule_name": "tools_allowlist",
             "reason": None},
        ])

        # txn-3 — a DRY run; its verdicts still fold in
        t3 = Transaction(txn_id="txn-3", state=TxnState.COMMITTED)
        t3.effects = [_eff("txn-3", 0, "write_file", "fs")]
        journal.record_transaction(t3, dry_run=True)
        for e in t3.effects:
            journal.record_effect(e)
        journal.record_verdicts("txn-3", [
            {"effect_index": 0, "phase": "stage", "allow": False,
             "kind": "rule", "rule_name": "no_secrets",
             "reason": "path is sensitive"},
            {"effect_index": 0, "phase": "stage", "allow": True,
             "kind": "allowlist", "rule_name": "tools_allowlist",
             "reason": None},
        ])

        # txn-4 — two unnamed (rule_name=None) verdicts of different kinds that
        # MUST collapse into one bucket, plus an orphan verdict whose effect row
        # is absent (effect_index 5) — the LEFT JOIN keeps it, tool/resource NULL.
        t4 = Transaction(txn_id="txn-4", state=TxnState.COMMITTED)
        t4.effects = [_eff("txn-4", 0, "query", "sql")]
        journal.record_transaction(t4, dry_run=False)
        for e in t4.effects:
            journal.record_effect(e)
        journal.record_verdicts("txn-4", [
            {"effect_index": 0, "phase": "commit", "allow": True,
             "kind": "rule", "rule_name": None, "reason": None},
            {"effect_index": 0, "phase": "commit", "allow": False,
             "kind": "cap", "rule_name": None, "reason": "anonymous cap"},
            {"effect_index": 5, "phase": "stage", "allow": True,
             "kind": "rule", "rule_name": "orphan_rule", "reason": None},
        ])
    finally:
        journal.close()


@pytest.fixture
def reader(tmp_path: Path):
    path = str(tmp_path / "policy.db")
    _seed_policy(path)
    r = JournalReader(path)
    yield r
    r.close()


def _by_rule(pol: dict) -> dict:
    """Index the named rules by (kind, rule) for direct assertions."""
    return {(r["kind"], r["rule"]): r for r in pol["rules"]}


# --- supported + totals -----------------------------------------------------


def test_supported_and_totals(reader: JournalReader):
    """Fails against the prior commit: there was no policy() at all.

    Eleven verdicts across four transactions: 6 allow, 5 deny, over 5 named
    rules (the unnamed bucket is not a 'rule')."""
    pol = reader.policy()
    assert pol["supported"] is True
    assert pol["scope"] == {"spans": "all_verdicts"}
    assert pol["caveat"] == POLICY_CAVEAT
    assert pol["totals"] == {
        "rules": 5,
        "verdicts": 11,
        "allowed": 6,
        "denied": 5,
        "deny_rate": 5 / 11,
    }


# --- per-rule grouping + ranking --------------------------------------------


def test_rules_ranked_loudest_denier_first(reader: JournalReader):
    """Ranked by denied desc, then fired desc, then kind, then rule name.

    Both top rules denied twice; among them the ``cap`` sorts before the
    ``rule`` on kind. The two never-denying rules tie on fired=2 and sort by
    kind (``allowlist`` before ``rule``); the orphan (fired=1) sorts last."""
    order = [(r["kind"], r["rule"]) for r in reader.policy()["rules"]]
    assert order == [
        ("cap", "Cap.daily_spend"),
        ("rule", "no_secrets"),
        ("allowlist", "tools_allowlist"),
        ("rule", "allow_all"),
        ("rule", "orphan_rule"),
    ]


def test_cap_rule_record(reader: JournalReader):
    """The cap fired twice, denied both, at the commit phase, on one tool."""
    cap = _by_rule(reader.policy())[("cap", "Cap.daily_spend")]
    assert cap["fired"] == 2
    assert cap["allowed"] == 0
    assert cap["denied"] == 2
    assert cap["deny_rate"] == 1.0          # over its OWN firings, not all traffic
    assert cap["by_phase"] == {"stage": 0, "commit": 2}
    assert cap["reasons"] == [{"reason": "over daily cap", "count": 2}]
    assert cap["tools"] == ["charge_card"]
    assert cap["resources"] == ["http"]
    assert cap["txns"] == 2                  # txn-1 + txn-2


def test_allowing_rule_has_no_reasons_and_full_reach(reader: JournalReader):
    """A rule that only ever allowed carries an empty reasons list and a
    zero deny_rate, but still reports the tools/resources it governed."""
    allow_all = _by_rule(reader.policy())[("rule", "allow_all")]
    assert allow_all["fired"] == 2
    assert allow_all["denied"] == 0
    assert allow_all["deny_rate"] == 0.0
    assert allow_all["reasons"] == []
    assert allow_all["by_phase"] == {"stage": 2, "commit": 0}
    assert allow_all["tools"] == ["charge_card", "send_email"]
    assert allow_all["resources"] == ["http", "smtp"]
    assert allow_all["txns"] == 1            # both verdicts are in txn-1


def test_reasons_ordered_commonest_then_name(reader: JournalReader):
    """no_secrets denied twice for two distinct reasons (one each) — tied on
    count, they fall back to alphabetical so the order is deterministic."""
    no_secrets = _by_rule(reader.policy())[("rule", "no_secrets")]
    assert no_secrets["denied"] == 2
    assert no_secrets["by_phase"] == {"stage": 2, "commit": 0}
    assert no_secrets["reasons"] == [
        {"reason": "body contains a secret", "count": 1},
        {"reason": "path is sensitive", "count": 1},
    ]
    # spans a real txn and a dry-run txn — dry-run decisions are folded in
    assert no_secrets["txns"] == 2
    assert no_secrets["tools"] == ["send_email", "write_file"]
    assert no_secrets["resources"] == ["fs", "smtp"]


# --- the unnamed bucket -----------------------------------------------------


def test_unnamed_bucket_collapses_regardless_of_kind(reader: JournalReader):
    """Two rule_name-less verdicts — one ``rule``, one ``cap`` — collapse into a
    single unnamed bucket (kind None), surfaced as a governance gap not dropped.
    It is NOT counted among ``totals.rules``."""
    pol = reader.policy()
    unnamed = pol["unnamed"]
    assert unnamed is not None
    assert unnamed["kind"] is None
    assert unnamed["rule"] is None
    assert unnamed["fired"] == 2
    assert unnamed["allowed"] == 1
    assert unnamed["denied"] == 1
    assert unnamed["deny_rate"] == 0.5
    assert unnamed["by_phase"] == {"stage": 0, "commit": 2}
    assert unnamed["reasons"] == [{"reason": "anonymous cap", "count": 1}]
    assert unnamed["tools"] == ["query"]
    assert unnamed["resources"] == ["sql"]
    # the unnamed bucket is never one of the named rules
    assert (None, None) not in _by_rule(pol)


# --- LEFT-JOIN tolerance ----------------------------------------------------


def test_orphan_verdict_counts_without_tool_or_resource(reader: JournalReader):
    """A verdict whose effect row is absent (effect_index 5) still counts toward
    its rule's firings — the LEFT JOIN keeps it — but contributes no tool or
    resource, so those sets stay empty rather than the verdict being dropped."""
    orphan = _by_rule(reader.policy())[("rule", "orphan_rule")]
    assert orphan["fired"] == 1
    assert orphan["allowed"] == 1
    assert orphan["tools"] == []
    assert orphan["resources"] == []
    assert orphan["txns"] == 1


# --- the journal-wide phase matrix ------------------------------------------


def test_phase_matrix_where_policy_bites(reader: JournalReader):
    """Where in the stage→commit bracket the policy allows and denies, over the
    whole journal: stage allows 5 / denies 2; commit allows 1 / denies 3. Both
    phases always present; the four cells reconcile to the verdict total."""
    phases = reader.policy()["phases"]
    assert phases == {
        "stage": {"allow": 5, "deny": 2},
        "commit": {"allow": 1, "deny": 3},
    }
    cells = sum(v for ph in phases.values() for v in ph.values())
    assert cells == reader.policy()["totals"]["verdicts"] == 11


# --- empty + NULL-tolerant degradation --------------------------------------


def test_empty_journal_is_present_and_zero(tmp_path: Path):
    """A journal with the verdicts table but no rows: supported True, every
    collection empty and the phase matrix all-zero — never a crash."""
    path = str(tmp_path / "empty.db")
    AuditJournal(path).close()  # creates the schema incl. an empty verdicts table
    with JournalReader(path) as r:
        pol = r.policy()
    assert pol["supported"] is True
    assert pol["rules"] == []
    assert pol["unnamed"] is None
    assert pol["phases"] == {
        "stage": {"allow": 0, "deny": 0},
        "commit": {"allow": 0, "deny": 0},
    }
    assert pol["totals"] == {
        "rules": 0, "verdicts": 0, "allowed": 0, "denied": 0, "deny_rate": 0.0,
    }


def test_pre_verdicts_journal_degrades(tmp_path: Path):
    """A journal written before the verdicts table existed must still yield a
    full (empty) policy payload rather than crashing — ``supported`` is False so
    a console can say 'this journal predates verdict recording' rather than 'the
    policy denied nothing'."""
    path = str(tmp_path / "ancient.db")
    con = sqlite3.connect(path)
    con.executescript(
        """
        CREATE TABLE transactions (txn_id TEXT PRIMARY KEY, state TEXT NOT NULL,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            replayed_from TEXT, dry_run INTEGER NOT NULL DEFAULT 0, client_id TEXT);
        CREATE TABLE effects (txn_id TEXT NOT NULL, idx INTEGER NOT NULL,
            effect_id TEXT NOT NULL, tool TEXT NOT NULL, resource TEXT NOT NULL,
            reversible INTEGER NOT NULL, status TEXT NOT NULL, args TEXT NOT NULL,
            snapshot TEXT, result TEXT, read_keys TEXT NOT NULL DEFAULT '[]',
            write_keys TEXT NOT NULL DEFAULT '[]', ts TEXT NOT NULL,
            PRIMARY KEY (txn_id, idx));
        """
    )
    con.commit()
    con.close()

    with JournalReader(path) as r:
        pol = r.policy()
    assert pol == {
        "caveat": POLICY_CAVEAT,
        "scope": {"spans": "all_verdicts"},
        "supported": False,
        "rules": [],
        "unnamed": None,
        "phases": {
            "stage": {"allow": 0, "deny": 0},
            "commit": {"allow": 0, "deny": 0},
        },
        "totals": {
            "rules": 0, "verdicts": 0, "allowed": 0, "denied": 0,
            "deny_rate": 0.0,
        },
    }


def test_seed_journal_policy_is_well_formed(tmp_path: Path):
    """The shipped demo journal must produce a well-formed policy payload — the
    shape is pinned so a future seed change updates this deliberately. Every
    named rule reconciles allowed + denied to fired, and deny_rate matches."""
    path = str(tmp_path / "demo.db")
    seed_demo_journal(path)
    with JournalReader(path) as r:
        pol = r.policy()
    assert pol["supported"] is True
    assert set(pol) == {
        "caveat", "scope", "supported", "rules", "unnamed", "phases", "totals",
    }
    for rec in pol["rules"]:
        assert rec["allowed"] + rec["denied"] == rec["fired"]
        assert rec["deny_rate"] == (
            rec["denied"] / rec["fired"] if rec["fired"] else 0.0
        )
    # the totals reconcile to the per-rule + unnamed fold
    fold = pol["rules"] + ([pol["unnamed"]] if pol["unnamed"] else [])
    assert pol["totals"]["verdicts"] == sum(r["fired"] for r in fold)
    assert pol["totals"]["denied"] == sum(r["denied"] for r in fold)


# --- over the wire ----------------------------------------------------------


@pytest.fixture
def server(tmp_path: Path):
    db = str(tmp_path / "policy.db")
    _seed_policy(db)
    httpd = make_server(db, host="127.0.0.1", port=0)  # 0 → OS picks a free port
    port = httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        httpd.shutdown()
        thread.join(timeout=5)
        httpd.reader.close()
        httpd.server_close()


def _get_json(url: str):
    with urllib.request.urlopen(url, timeout=5) as resp:
        assert "application/json" in resp.headers.get("Content-Type", "")
        return resp.status, json.loads(resp.read())


def test_api_policy_round_trip(server: str):
    status, data = _get_json(server + "/api/policy")
    assert status == 200
    assert set(data) >= {"supported", "rules", "unnamed", "phases", "totals"}
    assert data["totals"]["verdicts"] == 11
    # the loudest denier survives the round trip, ranked first
    top = data["rules"][0]
    assert (top["kind"], top["rule"]) == ("cap", "Cap.daily_spend")
    assert top["denied"] == 2
    assert data["phases"] == {
        "stage": {"allow": 5, "deny": 2},
        "commit": {"allow": 1, "deny": 3},
    }
