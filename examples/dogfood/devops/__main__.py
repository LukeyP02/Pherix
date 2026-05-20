"""Run the DevOps dogfood end to end against a real model.

    python -m examples.dogfood.devops

Needs an Anthropic key in ``.env`` at the repo root (see ``.env.example``) and
``pip install -e '.[dogfood]'``. Two phases:

  1. **Dry-run preview.** Fold the release forward against a snapshot, then
     discard it — printing the migration's structured ``state_diff`` (the rows
     the schema change *would* touch) and the irreversibles that *would* fire,
     with nothing committed. The "what will this release do?" view, free.

  2. **The real release.** A real agent runs migration → config → deploy →
     smoke_test. The smoke test is engineered to fail; the failure fires at
     commit and the engine's mixed-fold unwind reverts everything — migration
     savepoint rolled back, config restored, deploy compensated. We print the
     journal and prove the world is back to its pre-release state.

The deploy target and HTTP layer are an in-memory fake, so no real network
call escapes — but every adapter does real work (real SQLite SAVEPOINT, real
on-disk file backup) so the unwind is genuine, not simulated.
"""

from __future__ import annotations

from examples.dogfood.devops.scenario import (
    DeployTarget,
    build_tools,
    run_release,
)
from examples.dogfood.harness import run_agent
from examples.dogfood.infra import scratch_sqlite, temp_tree
from pherix.core.adapters.filesystem import FilesystemAdapter
from pherix.core.adapters.http import HTTPAdapter
from pherix.core.adapters.sql import SQLiteAdapter
from pherix.core.audit import AuditJournal
from pherix.core.policy import Policy
from pherix.core.tools import REGISTRY

SCHEMA = """
CREATE TABLE accounts (id INTEGER PRIMARY KEY, name TEXT);
INSERT INTO accounts (name) VALUES ('alice'), ('bob');
"""


def _banner(title: str) -> None:
    print()
    print("=" * 72)
    print(title)
    print("=" * 72)


def _print_journal(audit: AuditJournal, txn_id: str) -> None:
    record = audit.get_transaction(txn_id)
    print(f"  txn {txn_id}  state={record['state']}  "
          f"client_id={record.get('client_id')}")
    for e in audit.get_effects(txn_id):
        print(
            f"    [{e['idx']}] {e['resource']:>4}  "
            f"{e['tool']}({e['args']}) -> {e['status']}"
        )


def preview_release(audit: AuditJournal) -> None:
    """Phase 1 — a dry-run that prints the migration's state_diff, then discards."""
    _banner("1. DRY-RUN PREVIEW — what the release would do (nothing committed)")
    REGISTRY.clear()
    with scratch_sqlite(SCHEMA) as db, temp_tree() as tree:
        target = DeployTarget()
        tools = build_tools(target)
        adapters = {
            "sql": SQLiteAdapter(db.conn),
            "fs": FilesystemAdapter(tree),
            "http": HTTPAdapter(),
        }
        run = run_agent(
            task=(
                "Preview release v2: add a `feature_flag` column to `accounts`, "
                "write the config for 'v2', deploy 'v2', then smoke-test it."
            ),
            system=(
                "You are a release engineer previewing a release. Call, in "
                "order: run_migration, write_config, deploy, smoke_test. One "
                "tool at a time, then stop."
            ),
            tools=tools,
            adapters=adapters,
            policy=Policy.allow_all(),
            client_id="devops-preview",
            mode="dry_run",
            audit=audit,
        )
        result = run.dry_run_result
        print(f"  journal materialised: {len(result.journal)} effects "
              f"(state={run.final_state.name})")
        sql_diff = result.state_diff.get("sql", {})
        fs_diff = result.state_diff.get("fs", {})
        print(f"  SQL state_diff:  rows_added={sql_diff.get('rows_added')} "
              f"rows_modified={sql_diff.get('rows_modified')}")
        print(f"  FS state_diff:   files_added={fs_diff.get('files_added')} "
              f"files_modified={fs_diff.get('files_modified')}")
        print(f"  would_have_fired (irreversibles): "
              f"{[e.tool for e in result.would_have_fired]}")
        print("  -> nothing committed; deploy target untouched: "
              f"history={target.history}")


def real_release(audit: AuditJournal) -> None:
    """Phase 2 — the real release; the failing smoke test unwinds everything."""
    _banner("2. REAL RELEASE — engineered smoke failure unwinds atomically")
    REGISTRY.clear()
    with scratch_sqlite(SCHEMA) as db, temp_tree() as tree:
        target = DeployTarget()  # healthy=False → smoke test will fail

        def accounts_columns():
            return [r[1] for r in db.conn.execute("PRAGMA table_info(accounts)")]

        def config_exists():
            return (tree / "release.conf").exists()

        print(f"  before: accounts columns = {accounts_columns()}")
        print(f"  before: release.conf exists = {config_exists()}")
        print(f"  before: deploy history = {target.history}")

        run = run_release(
            conn=db.conn,
            fs_root=tree,
            target=target,
            client=None,  # real Anthropic client (needs a key)
            audit=audit,
        )

        print()
        print(f"  agent ran {run.turns} turns; stop_reason={run.stop_reason}")
        print(f"  commit-time error: {type(run.error).__name__ if run.error else None}"
              f" — {run.error}")
        print(f"  final txn state = {run.final_state.name}")
        print()
        print(f"  after:  accounts columns = {accounts_columns()}  "
              f"(feature_flag gone — migration rolled back)")
        print(f"  after:  release.conf exists = {config_exists()}  "
              f"(config restored)")
        print(f"  after:  deploy history = {target.history}  "
              f"(deploy fired, then compensated)")
        print()
        _banner("THE JOURNAL — the whole release, then its unwind")
        _print_journal(audit, run.txn_id)


def main() -> None:
    audit = AuditJournal.in_memory()
    preview_release(audit)
    real_release(audit)
    print()
    print("DevOps dogfood done.")


if __name__ == "__main__":
    main()
