"""Mechanism test (mocked client, deterministic, CI) for the DevOps dogfood.

This is NOT a real-agent dogfood. It is a *mechanism test*: no key, no network,
no ``anthropic`` import. A mock client emits a canned tool sequence and we assert
the *composition* the dogfood wires together — five tools across three adapters,
an irreversible deploy with a compensator, and a smoke test that computes health
from real state — behaves correctly given that exact sequence. The genuinely
autonomous version (a real model given a goal, with a genuine outcome) is the
real-agent run, ``python -m examples.dogfood.devops``; that is the demo, this is
the regression guard for the wiring underneath it.

Two scripted paths prove both branches of the genuine fault mode:

- **Skip the backfill** (``add_column -> write_config -> deploy -> smoke_test``):
  the smoke test reads the live rows, finds the new ``feature_flag`` is NULL for
  existing accounts, and raises. That raise lands in the engine's staged-fire
  loop and triggers ``_partial_unwind`` — the whole release reverts.
- **Do the backfill** (``add_column -> backfill_column -> write_config ->
  deploy -> smoke_test``): the smoke test finds a consistent, fully-backfilled
  release and passes, so the transaction commits.

The smoke test is irreversible, so it is *staged* and fires at commit-time after
the (also-staged) deploy — exactly the production trigger, driven here by a
scripted client instead of a live one.
"""

from types import SimpleNamespace as NS

from pherix.core.audit import AuditJournal
from pherix.core.effects import EffectStatus
from pherix.core.transaction import TxnState

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
    """A scripted Anthropic-compatible client emitting a canned release."""

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
            _resp(
                _tool_use("t1", "add_column", {"column": "feature_flag"}),
                stop_reason="tool_use",
            ),
            _resp(
                _tool_use("t2", "write_config", {"version": "v2"}),
                stop_reason="tool_use",
            ),
            _resp(
                _tool_use("t3", "deploy", {"version": "v2"}),
                stop_reason="tool_use",
            ),
            _resp(
                _tool_use("t4", "smoke_test", {"version": "v2"}),
                stop_reason="tool_use",
            ),
            # The model would emit this after seeing results — but commit (and
            # the unwind) happens when the with-block exits, before we'd consume
            # it. Present so the loop has something if it runs on.
            _resp(_text("release shipped"), stop_reason="end_turn"),
        ]
    )


def _full_release_client():
    """add_column -> backfill_column -> write_config -> deploy -> smoke_test (healthy)."""
    return _FakeClient(
        [
            _resp(
                _tool_use("t1", "add_column", {"column": "feature_flag"}),
                stop_reason="tool_use",
            ),
            _resp(
                _tool_use(
                    "t2", "backfill_column", {"column": "feature_flag", "value": "off"}
                ),
                stop_reason="tool_use",
            ),
            _resp(
                _tool_use("t3", "write_config", {"version": "v2"}),
                stop_reason="tool_use",
            ),
            _resp(
                _tool_use("t4", "deploy", {"version": "v2"}),
                stop_reason="tool_use",
            ),
            _resp(
                _tool_use("t5", "smoke_test", {"version": "v2"}),
                stop_reason="tool_use",
            ),
            _resp(_text("release shipped"), stop_reason="end_turn"),
        ]
    )


def _columns(conn):
    return [r[1] for r in conn.execute("PRAGMA table_info(accounts)")]


def test_skipping_the_backfill_unwinds_the_whole_release():
    """A migration that adds the flag but never backfills it trips the genuine
    smoke check, and the whole release unwinds atomically."""
    audit = AuditJournal.in_memory()
    with scratch_sqlite(SCHEMA) as db, temp_tree() as tree:
        target = DeployTarget()

        assert "feature_flag" not in _columns(db.conn)
        assert not (tree / "release.conf").exists()

        run = run_release(
            conn=db.conn,
            fs_root=tree,
            target=target,
            client=_no_backfill_client(),
            audit=audit,
        )

        # Headline: the release did not commit — it unwound atomically.
        assert run.final_state is TxnState.ROLLED_BACK  # compensator succeeds
        assert run.error is not None  # commit-time refusal captured, not raised
        # The smoke failure names the genuine problem (unbackfilled rows).
        assert "feature_flag IS NULL" in str(run.error)

        # The reversible effects are gone: migration rolled back, config restored.
        assert "feature_flag" not in _columns(db.conn)
        assert not (tree / "release.conf").exists()

        # The deploy fired (irreversible, ran at commit) and was then compensated.
        actions = [h["action"] for h in target.history]
        assert "deploy" in actions
        assert "rollback" in actions
        assert target.deployed_version is None

        # The journal tells the same story: deploy COMPENSATED, smoke_test FAILED,
        # reversibles COMPENSATED.
        effects = {e.tool: e for e in run.journal}
        assert set(effects) == {
            "add_column",
            "write_config",
            "deploy",
            "smoke_test",
        }
        assert effects["deploy"].status is EffectStatus.COMPENSATED
        assert effects["smoke_test"].status is EffectStatus.FAILED
        assert effects["add_column"].status is EffectStatus.COMPENSATED
        assert effects["write_config"].status is EffectStatus.COMPENSATED


def test_a_healthy_release_commits():
    """When the agent migrates AND backfills, the smoke check passes against real
    state and the release commits — the genuine success branch."""
    audit = AuditJournal.in_memory()
    with scratch_sqlite(SCHEMA) as db, temp_tree() as tree:
        target = DeployTarget()

        run = run_release(
            conn=db.conn,
            fs_root=tree,
            target=target,
            client=_full_release_client(),
            audit=audit,
        )

        # Headline: a genuinely healthy release commits, no unwind, no error.
        assert run.final_state is TxnState.COMMITTED
        assert run.error is None

        # The release persisted: column present, every row backfilled, config on disk.
        assert "feature_flag" in _columns(db.conn)
        nulls = db.conn.execute(
            "SELECT COUNT(*) FROM accounts WHERE feature_flag IS NULL"
        ).fetchone()[0]
        assert nulls == 0
        assert (tree / "release.conf").read_text() == "version=v2\n"

        # The deploy fired and stayed (no compensation).
        actions = [h["action"] for h in target.history]
        assert "deploy" in actions
        assert "rollback" not in actions
        assert target.deployed_version == "v2"

        # Every effect is APPLIED.
        effects = {e.tool: e for e in run.journal}
        assert set(effects) == {
            "add_column",
            "backfill_column",
            "write_config",
            "deploy",
            "smoke_test",
        }
        assert all(e.status is EffectStatus.APPLIED for e in run.journal)


def test_deploy_is_irreversible_with_a_compensator_and_smoke_fires_after_it():
    """Wiring guard: deploy stages (irreversible) and carries rollback_deploy;
    both deploy and smoke_test are irreversible so they fire post-body."""
    audit = AuditJournal.in_memory()
    with scratch_sqlite(SCHEMA) as db, temp_tree() as tree:
        target = DeployTarget()
        run = run_release(
            conn=db.conn,
            fs_root=tree,
            target=target,
            client=_no_backfill_client(),
            audit=audit,
        )
        deploy = next(e for e in run.journal if e.tool == "deploy")
        smoke = next(e for e in run.journal if e.tool == "smoke_test")
        add_col = next(e for e in run.journal if e.tool == "add_column")
        config = next(e for e in run.journal if e.tool == "write_config")
        # deploy: irreversible, real teardown compensator.
        assert deploy.reversible is False
        assert deploy.compensator == "rollback_deploy"
        # smoke: irreversible; a no-op compensator only so it clears the gate
        # and fires (a failing check then raises and triggers the unwind).
        assert smoke.reversible is False
        assert smoke.compensator == "smoke_noop"
        # add_column + config: reversible (SQL savepoint / FS backup).
        assert add_col.reversible is True
        assert add_col.resource == "sql"
        assert config.reversible is True
        assert config.resource == "fs"


def test_offline_run_imports_no_anthropic():
    """The dogfood run path must stay offline — assert no anthropic import."""
    import sys

    assert "anthropic" not in sys.modules
