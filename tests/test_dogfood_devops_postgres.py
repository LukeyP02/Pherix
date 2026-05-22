"""Postgres-backed mechanism test for the DevOps dogfood — the real-server lane.

The DevOps demo is **Postgres-only** (SQLite alone reads as a toy), so the
backfill trap and the atomic unwind must hold against a genuine server's real
``SAVEPOINT`` / ``ROLLBACK TO SAVEPOINT`` — not SQLite's. This test drives the
*same* canned tool sequences as ``test_dogfood_devops.py`` (mocked client, no
key, no network to a model) but routes the resource effects through a real
:class:`pherix.PostgresAdapter` against the server named by ``PHERIX_PG_DSN`` /
``DATABASE_URL``.

It is **skipped** when no DSN is configured (or ``psycopg`` is missing), so CI —
which has no Postgres — stays green; the operator runs it against a local
``createdb pherix_dogfood`` to prove the Postgres lane end to end. The two
assertions that matter and that SQLite cannot make for us: after a tripped smoke
check the new column is *gone* (a real Postgres savepoint reverted the
migration), and after a healthy release it *persists with no NULLs* (a real
Postgres commit).
"""

import os
from types import SimpleNamespace as NS

import pytest

from pherix.core.effects import EffectStatus
from pherix.core.transaction import TxnState

from examples.dogfood.devops.scenario import (
    ACCOUNTS_SCHEMA_PG,
    DeployTarget,
    run_release,
)

_DSN = os.environ.get("PHERIX_PG_DSN") or os.environ.get("DATABASE_URL")
_HAS_PSYCOPG = False
if _DSN:
    try:
        import psycopg  # noqa: F401

        _HAS_PSYCOPG = True
    except ImportError:
        _HAS_PSYCOPG = False

pytestmark = pytest.mark.skipif(
    not (_DSN and _HAS_PSYCOPG),
    reason="needs a Postgres DSN (PHERIX_PG_DSN / DATABASE_URL) and psycopg",
)


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
    return _FakeClient(
        [
            _resp(_tool_use("t1", "add_column", {"column": "feature_flag"}), stop_reason="tool_use"),
            _resp(_tool_use("t2", "write_config", {"version": "v2"}), stop_reason="tool_use"),
            _resp(_tool_use("t3", "deploy", {"version": "v2"}), stop_reason="tool_use"),
            _resp(_tool_use("t4", "smoke_test", {"version": "v2"}), stop_reason="tool_use"),
            _resp(_text("shipped"), stop_reason="end_turn"),
        ]
    )


def _full_release_client():
    return _FakeClient(
        [
            _resp(_tool_use("t1", "add_column", {"column": "feature_flag"}), stop_reason="tool_use"),
            _resp(
                _tool_use("t2", "backfill_column", {"column": "feature_flag", "value": "off"}),
                stop_reason="tool_use",
            ),
            _resp(_tool_use("t3", "write_config", {"version": "v2"}), stop_reason="tool_use"),
            _resp(_tool_use("t4", "deploy", {"version": "v2"}), stop_reason="tool_use"),
            _resp(_tool_use("t5", "smoke_test", {"version": "v2"}), stop_reason="tool_use"),
            _resp(_text("shipped"), stop_reason="end_turn"),
        ]
    )


def _pg_columns(conn):
    cur = conn.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_name = 'accounts' AND table_schema = current_schema()"
    )
    return [r[0] for r in cur.fetchall()]


def test_postgres_unwind_reverts_the_migration(tmp_path):
    """Skipping the backfill trips the smoke check; the real Postgres savepoint
    rolls the schema migration back so the new column is gone afterwards."""
    from examples.dogfood.infra import scratch_postgres, temp_tree

    with scratch_postgres(ACCOUNTS_SCHEMA_PG) as db, temp_tree() as tree:
        target = DeployTarget()
        assert "feature_flag" not in _pg_columns(db.conn)

        run = run_release(
            conn=db.conn,
            fs_root=tree,
            target=target,
            client=_no_backfill_client(),
            backend="postgres",
        )

        assert run.final_state is TxnState.ROLLED_BACK
        assert run.error is not None
        assert "feature_flag IS NULL" in str(run.error)
        # The genuine Postgres assertion: ROLLBACK TO SAVEPOINT undid the ALTER.
        assert "feature_flag" not in _pg_columns(db.conn)
        # Deploy fired then was compensated; config restored.
        assert target.deployed_version is None
        assert not (tree / "release.conf").exists()
        effects = {e.tool: e for e in run.journal}
        assert effects["deploy"].status is EffectStatus.COMPENSATED
        assert effects["smoke_test"].status is EffectStatus.FAILED
        assert effects["add_column"].status is EffectStatus.COMPENSATED


def test_postgres_healthy_release_commits(tmp_path):
    """A migrated AND backfilled release passes the smoke check and commits to
    real Postgres: the column persists and no existing row is left NULL."""
    from examples.dogfood.infra import scratch_postgres, temp_tree

    with scratch_postgres(ACCOUNTS_SCHEMA_PG) as db, temp_tree() as tree:
        target = DeployTarget()
        run = run_release(
            conn=db.conn,
            fs_root=tree,
            target=target,
            client=_full_release_client(),
            backend="postgres",
        )

        assert run.final_state is TxnState.COMMITTED
        assert run.error is None
        assert "feature_flag" in _pg_columns(db.conn)
        nulls = db.conn.execute(
            "SELECT COUNT(*) FROM accounts WHERE feature_flag IS NULL"
        ).fetchone()[0]
        assert nulls == 0
        assert target.deployed_version == "v2"
        assert all(e.status is EffectStatus.APPLIED for e in run.journal)
