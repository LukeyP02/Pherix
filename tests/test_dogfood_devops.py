"""Offline proof that the DevOps dogfood unwinds a failing release atomically.

No key, no network, no ``anthropic`` import: a mock client emits the canned
release sequence (migrate -> write_config -> deploy -> smoke_test) and we assert
the *composition* the dogfood wires together — three adapters, an irreversible
deploy with a compensator, and a smoke test engineered to fail — produces an
atomic unwind. This tests the dogfood's wiring, not the engine (the engine's
mixed-fold has its own unit tests); if any tool is mis-wired (wrong resource,
missing compensator, smoke test that doesn't raise) this test fails.

The smoke test is irreversible, so it is *staged* and fires at commit-time,
after the (also-staged) deploy. Its raise lands in the engine's staged-fire
loop and triggers ``_partial_unwind`` — exactly the production trigger, driven
here by a scripted model instead of a live one.
"""

from types import SimpleNamespace as NS

from pherix.core.audit import AuditJournal
from pherix.core.effects import EffectStatus
from pherix.core.transaction import TxnState

from examples.dogfood.devops.scenario import DeployTarget, run_release
from examples.dogfood.infra import scratch_sqlite, temp_tree

SCHEMA = """
CREATE TABLE accounts (id INTEGER PRIMARY KEY, name TEXT);
INSERT INTO accounts (name) VALUES ('alice');
"""


def _resp(*blocks, stop_reason):
    return NS(content=list(blocks), stop_reason=stop_reason)


def _tool_use(use_id, name, inp):
    return NS(type="tool_use", id=use_id, name=name, input=inp)


def _text(text):
    return NS(type="text", text=text)


class _FakeClient:
    """A scripted Anthropic-compatible client emitting the canned release."""

    def __init__(self, responses):
        self._responses = list(responses)
        self._i = 0
        self.messages = self

    def create(self, **kwargs):
        resp = self._responses[self._i]
        self._i += 1
        return resp


def _release_client():
    """migrate -> write_config -> deploy -> smoke_test, one tool per turn."""
    return _FakeClient(
        [
            _resp(
                _tool_use("t1", "run_migration", {"column": "feature_flag"}),
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
            # the unwind) happens when the with-block exits, before we'd ever
            # consume it. Present so the loop has something if it runs on.
            _resp(_text("release shipped"), stop_reason="end_turn"),
        ]
    )


def _columns(conn):
    return [r[1] for r in conn.execute("PRAGMA table_info(accounts)")]


def test_failing_smoke_test_unwinds_the_whole_release():
    audit = AuditJournal.in_memory()
    with scratch_sqlite(SCHEMA) as db, temp_tree() as tree:
        target = DeployTarget()  # healthy=False -> smoke test fails

        assert "feature_flag" not in _columns(db.conn)
        assert not (tree / "release.conf").exists()

        run = run_release(
            conn=db.conn,
            fs_root=tree,
            target=target,
            client=_release_client(),
            audit=audit,
        )

        # Headline: the release did not commit — it unwound atomically.
        assert run.final_state in (TxnState.ROLLED_BACK, TxnState.STUCK)
        assert run.final_state is TxnState.ROLLED_BACK  # compensator succeeds
        assert run.error is not None  # commit-time refusal captured, not raised

        # The reversible effects are gone: migration rolled back, config restored.
        assert "feature_flag" not in _columns(db.conn)
        assert not (tree / "release.conf").exists()

        # The deploy fired (it is irreversible and ran at commit) and was then
        # compensated — the target shows both actions, ending torn-down.
        actions = [h["action"] for h in target.history]
        assert "deploy" in actions
        assert "rollback" in actions
        assert target.deployed_version is None

        # The journal tells the same story: four effects, deploy COMPENSATED,
        # smoke_test FAILED (it raised), reversibles COMPENSATED.
        effects = {e.tool: e for e in run.journal}
        assert set(effects) == {
            "run_migration",
            "write_config",
            "deploy",
            "smoke_test",
        }
        assert effects["deploy"].status is EffectStatus.COMPENSATED
        assert effects["smoke_test"].status is EffectStatus.FAILED
        assert effects["run_migration"].status is EffectStatus.COMPENSATED
        assert effects["write_config"].status is EffectStatus.COMPENSATED


def test_deploy_is_irreversible_with_a_compensator_and_smoke_is_gated_after_it():
    """Wiring guard: deploy stages (irreversible) and carries rollback_deploy;
    both deploy and smoke_test are irreversible so they fire post-body."""
    audit = AuditJournal.in_memory()
    with scratch_sqlite(SCHEMA) as db, temp_tree() as tree:
        target = DeployTarget()
        run = run_release(
            conn=db.conn,
            fs_root=tree,
            target=target,
            client=_release_client(),
            audit=audit,
        )
        deploy = next(e for e in run.journal if e.tool == "deploy")
        smoke = next(e for e in run.journal if e.tool == "smoke_test")
        migration = next(e for e in run.journal if e.tool == "run_migration")
        config = next(e for e in run.journal if e.tool == "write_config")
        # deploy: irreversible, real teardown compensator.
        assert deploy.reversible is False
        assert deploy.compensator == "rollback_deploy"
        # smoke: irreversible; a no-op compensator only so it clears the gate
        # and fires (a failing check then raises and triggers the unwind).
        assert smoke.reversible is False
        assert smoke.compensator == "smoke_noop"
        # migration + config: reversible (SQL savepoint / FS backup).
        assert migration.reversible is True
        assert migration.resource == "sql"
        assert config.reversible is True
        assert config.resource == "fs"


def test_offline_run_imports_no_anthropic():
    """The dogfood run path must stay offline — assert no anthropic import."""
    import sys

    assert "anthropic" not in sys.modules
