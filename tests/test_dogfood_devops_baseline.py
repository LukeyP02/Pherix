"""Baseline ("before") mechanism test for the DevOps dogfood — ungoverned vs governed.

NOT a real-agent run. A mock client emits the *careless* release sequence
(``add_column -> write_config -> deploy -> smoke_test``, no backfill) and we assert
the contrast that the before/after demo films: run **ungoverned** the damage
persists (the column is added, existing rows are NULL, and v2 stays deployed —
nothing unwinds when the smoke check trips); run **governed** the same sequence
unwinds atomically (column reverted, deploy compensated). Same input, two worlds,
one query in each. Offline, deterministic, no key, no anthropic import.
"""

from types import SimpleNamespace as NS

from pherix.core.transaction import TxnState

from examples.dogfood.capture import devops_before_after
from examples.dogfood.devops.scenario import DeployTarget, run_release
from examples.dogfood.infra import scratch_sqlite, temp_tree

SCHEMA = """
CREATE TABLE accounts (id INTEGER PRIMARY KEY, name TEXT);
INSERT INTO accounts (name) VALUES ('alice'), ('bob');
"""


def _resp(*blocks, stop_reason):
    return NS(content=list(blocks), stop_reason=stop_reason)


def _tool_use(use_id, name, inp):
    return NS(type="tool_use", id=use_id, name=name, input=inp)


def _text(text):
    return NS(type="text", text=text)


class _FakeClient:
    def __init__(self, responses):
        self._responses = list(responses)
        self._i = 0
        self.messages = self

    def create(self, **kwargs):
        resp = self._responses[self._i]
        self._i += 1
        return resp


def _no_backfill_client():
    """add_column -> write_config -> deploy -> smoke_test (existing rows left NULL)."""
    return _FakeClient(
        [
            _resp(_tool_use("t1", "add_column", {"column": "feature_flag"}), stop_reason="tool_use"),
            _resp(_tool_use("t2", "write_config", {"version": "v2"}), stop_reason="tool_use"),
            _resp(_tool_use("t3", "deploy", {"version": "v2"}), stop_reason="tool_use"),
            _resp(_tool_use("t4", "smoke_test", {"version": "v2"}), stop_reason="tool_use"),
            _resp(_text("release shipped"), stop_reason="end_turn"),
        ]
    )


def _columns(conn):
    return [r[1] for r in conn.execute("PRAGMA table_info(accounts)")]


def test_ungoverned_release_ships_broken_and_nothing_unwinds():
    """The before world: the careless release fires straight at the resources and
    the damage persists — column added, existing rows NULL, deploy live."""
    with scratch_sqlite(SCHEMA) as db, temp_tree() as tree:
        target = DeployTarget()

        run = run_release(
            conn=db.conn,
            fs_root=tree,
            target=target,
            client=_no_backfill_client(),
            governed=False,
        )

        # No transaction at all — the ungoverned shape.
        assert run.governed is False
        assert run.final_state is None
        assert run.txn_id is None
        assert run.journal == []

        # The damage persisted: column present, existing rows NULL, config on disk.
        assert "feature_flag" in _columns(db.conn)
        nulls = db.conn.execute(
            "SELECT COUNT(*) FROM accounts WHERE feature_flag IS NULL"
        ).fetchone()[0]
        assert nulls == 2
        assert (tree / "release.conf").read_text() == "version=v2\n"

        # The deploy fired and STAYED — nothing compensated it.
        actions = [h["action"] for h in target.history]
        assert "deploy" in actions
        assert "rollback" not in actions
        assert target.deployed_version == "v2"


def test_before_after_devops_contrast():
    """The same careless agent, both worlds: before corrupts, after is clean."""
    ba = devops_before_after(
        client_before=_no_backfill_client(),
        client_after=_no_backfill_client(),
    )

    # Before: damage persists (NULL rows + live deploy).
    assert ba.before.harmed is True
    assert ba.before.proof["feature_flag_column"] is True
    assert ba.before.proof["rows_with_null_flag"] == 2
    assert ba.before.proof["deployed_version"] == "v2"

    # After: the governed run unwound — column gone, nothing deployed.
    assert ba.after.harmed is False
    assert ba.after.proof["feature_flag_column"] is False
    assert ba.after.proof["deployed_version"] is None


def test_governed_path_still_unwinds_the_careless_release():
    """Guard: governed=True (the default) is unchanged — the careless release
    trips the commit-time smoke check and reverts."""
    with scratch_sqlite(SCHEMA) as db, temp_tree() as tree:
        target = DeployTarget()
        run = run_release(
            conn=db.conn,
            fs_root=tree,
            target=target,
            client=_no_backfill_client(),
        )
        assert run.final_state is TxnState.ROLLED_BACK
        assert "feature_flag IS NULL" in str(run.error)
        assert "feature_flag" not in _columns(db.conn)
        assert target.deployed_version is None
