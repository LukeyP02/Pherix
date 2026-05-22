"""The DevOps dogfood's tools, deploy target, and run logic.

Separated from ``__main__`` so the offline *mechanism test* can import
``build_tools`` / ``run_release`` and drive them with a mocked Anthropic client
— proving the *composition* (five tools across three adapters, unwound
atomically by a genuinely-failing smoke test) without a key or a network. The
mechanism test scripts an exact tool sequence; the **real-agent run**
(``python -m examples.dogfood.devops``) gives a real model a *goal* and lets it
decide — that run is the demo, and its outcome is genuine, not forced.

The agent's tool surface and wiring
-----------------------------------
  - ``add_column``    — resource ``"sql"``, reversible. ``ALTER TABLE`` adds a
    column; existing rows get ``NULL``. Rolls back via the SAVEPOINT.
  - ``backfill_column`` — resource ``"sql"``, reversible. ``UPDATE`` sets the
    column for *existing* rows. Rolls back via the SAVEPOINT.
  - ``write_config``  — resource ``"fs"``, reversible. Writes the release
    config file; restores from the per-effect copy-on-write backup.
  - ``deploy``        — resource ``"http"``, **irreversible**, declares the
    ``rollback_deploy`` compensator. Staged at stage-time, fired at commit.
  - ``smoke_test``    — resource ``"http"``, **irreversible**. Staged after
    ``deploy``; computes health from the *real* post-deploy state and RAISES
    if the release is unhealthy.

The genuine fault mode (no forced flag)
---------------------------------------
``DeployTarget`` no longer carries a ``healthy`` constant. ``smoke_test``
computes health by *reading the real state the agent actually produced*:

  1. the version the agent deployed matches the version it was asked to ship,
  2. the on-disk release config declares that version,
  3. the ``feature_flag`` column exists on ``accounts`` (the migration ran),
  4. **every existing account row has a non-NULL ``feature_flag``** — i.e. the
     agent did not just add the column but also *backfilled* it.

(4) is the trap, and it is a real migration anti-pattern: adding a column the
application reads for every row, without backfilling existing rows, leaves
``NULL``\\s the v2 app chokes on. A thorough agent backfills and the release
commits; a careless one skips it and the smoke test trips — and because
``smoke_test`` is irreversible and fires at commit-time *after* ``deploy``, that
trip lands in the engine's staged-fire loop and triggers the mixed-fold unwind:
the already-fired ``deploy`` is compensated by ``rollback_deploy`` and the
reversible migration/backfill/config restore from their snapshots. The terminal
state is ``ROLLED_BACK``. The agent never has to *decide* to roll back — the
engine does it the instant the genuine health check fails, which is the
guarantee a release pipeline actually wants. Whether a given real run commits or
unwinds depends on what the agent did, not on a constant — that variance is the
honest signal.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from pherix.core.adapters.filesystem import FilesystemAdapter, FsHandle
from pherix.core.adapters.http import HTTPAdapter
from pherix.core.adapters.postgres import PostgresAdapter
from pherix.core.adapters.sql import SQLiteAdapter, execute_isolated
from pherix.core.audit import AuditJournal
from pherix.core.policy import Policy
from pherix.core.tools import tool

from examples.dogfood.harness import AgentRun, run_agent

# The scratch schema the release runs against: an accounts table with two
# existing rows. Existing rows are exactly what makes the backfill trap genuine
# — a column added without backfilling leaves them NULL.
ACCOUNTS_SCHEMA = """
CREATE TABLE accounts (id INTEGER PRIMARY KEY, name TEXT);
INSERT INTO accounts (name) VALUES ('alice'), ('bob');
"""

# The same seed on PostgreSQL — SERIAL for the auto-increment PK (SQLite's
# INTEGER PRIMARY KEY is implicitly rowid; Postgres needs SERIAL). The two
# existing rows, and therefore the backfill trap, are identical.
ACCOUNTS_SCHEMA_PG = """
CREATE TABLE accounts (id SERIAL PRIMARY KEY, name TEXT);
INSERT INTO accounts (name) VALUES ('alice'), ('bob');
"""


@dataclass(frozen=True)
class SqlDialect:
    """The thin slice of SQL that actually differs between SQLite and Postgres.

    Everything else in this scenario is portable DDL/DML; only three things vary,
    so they live here behind one seam: the seed schema (PK syntax), the
    parameter placeholder (``?`` vs ``%s``), and how you ask the database which
    columns a table has (``PRAGMA table_info`` is SQLite-only). The mechanism
    test drives :data:`SQLITE`; the real-agent demo drives :data:`POSTGRES`
    against a genuine server. Same goal, same backfill trap, same atomic unwind —
    only the dialect under it changes, which is the whole "not a toy" point.
    """

    name: str
    schema: str
    placeholder: str

    def columns(self, conn: Any) -> list[str]:
        """The column names on the ``accounts`` table, read live from the DB."""
        if self.name == "sqlite":
            return [r[1] for r in conn.execute("PRAGMA table_info(accounts)")]
        cur = conn.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'accounts' AND table_schema = current_schema()"
        )
        return [r[0] for r in cur.fetchall()]


SQLITE = SqlDialect(name="sqlite", schema=ACCOUNTS_SCHEMA, placeholder="?")
POSTGRES = SqlDialect(name="postgres", schema=ACCOUNTS_SCHEMA_PG, placeholder="%s")


def dialect_for(backend: str) -> SqlDialect:
    """Map a backend name (``"sqlite"`` / ``"postgres"``) to its dialect."""
    if backend == "postgres":
        return POSTGRES
    if backend == "sqlite":
        return SQLITE
    raise ValueError(f"backend must be 'sqlite' or 'postgres', got {backend!r}")

# The version this dogfood ships. The smoke test checks the agent deployed and
# configured *this* version.
RELEASE_VERSION = "v2"

# The column v2 reads for every account. Named in the goal so a competent agent
# knows the target; the smoke test checks this exact name exists and is filled.
FLAG_COLUMN = "feature_flag"

# A *goal*, not a step list. The agent is told what a healthy v2 release means
# (including that the flag must exist for *every* account, existing rows
# included) and is left to decide the tools and order. A capable agent will
# migrate, backfill, configure, deploy and verify; a careless one will skip the
# backfill and the genuine smoke check will catch it.
SYSTEM = f"""You are a release engineer. Your job is to ship version \
{RELEASE_VERSION!r} of the accounts service safely, and to verify it is healthy \
before you declare success.

What {RELEASE_VERSION} needs to be healthy:
- The `accounts` table must have a `{FLAG_COLUMN}` column. The {RELEASE_VERSION} \
application reads this flag for EVERY account on startup and misbehaves if any \
existing account is missing a value for it.
- A release config for version {RELEASE_VERSION!r} must be written.
- Version {RELEASE_VERSION!r} must be deployed.

You have tools to alter the schema, backfill column values, write the release \
config, deploy a version, and run a post-deploy smoke test. Decide for yourself \
which tools to call and in what order — there is no fixed script. When you \
believe the release is healthy, run the smoke test to confirm it, then stop and \
report the outcome. If the smoke test reports a problem, you may try to fix it."""

TASK = (
    f"Ship release {RELEASE_VERSION} of the accounts service and confirm it is "
    "healthy."
)


class SmokeTestFailed(RuntimeError):
    """Raised by ``smoke_test`` when the deployed release is genuinely unhealthy.

    Carries the concrete list of problems the health check found (so the
    transcript and the journal record *why* it failed, not just *that* it did).
    Raised at fire-time (the staged-fire loop in ``commit()``), so the engine
    treats it as a mid-commit failure and runs the mixed-fold unwind — the whole
    release reverts.
    """

    def __init__(self, version: str, problems: list[str]):
        self.version = version
        self.problems = problems
        super().__init__(
            f"smoke test failed for {version!r}: " + "; ".join(problems)
        )


@dataclass
class DeployTarget:
    """A deterministic in-memory stand-in for a real deploy endpoint.

    A real DevOps agent would point ``deploy`` at k3d / a cloud API here. The
    dogfood keeps it in-process so the run is offline and the journal tells the
    whole story. There is **no** ``healthy`` flag: post-deploy health is
    *computed* from the real state the agent produced (schema, config, deployed
    version) by :func:`health_problems` — not stored as a constant.
    """

    deployed_version: str | None = None
    history: list[dict] = field(default_factory=list)

    def deploy(self, version: str) -> dict:
        self.deployed_version = version
        self.history.append({"action": "deploy", "version": version})
        return {"deployed": version}

    def rollback(self, version: str) -> None:
        self.history.append({"action": "rollback", "version": version})
        self.deployed_version = None

    def record_smoke(self, version: str) -> None:
        self.history.append({"action": "smoke", "version": version})


def health_problems(
    target: DeployTarget,
    conn: Any,
    fs_root: Path,
    version: str,
    dialect: SqlDialect = SQLITE,
) -> list[str]:
    """Compute, from the *real* post-deploy state, why the release is unhealthy.

    Returns an empty list iff the release is genuinely healthy. Each check reads
    actual state the agent produced — the deployed version, the on-disk config,
    and the live schema/rows on the transaction's own connection (the migration
    and backfill applied live inside the SAVEPOINT, so they are visible here at
    commit-time before the txn finalises). ``dialect`` decides how the column
    introspection is phrased (SQLite ``PRAGMA`` vs Postgres ``information_schema``).
    """
    problems: list[str] = []

    if target.deployed_version != version:
        problems.append(
            f"deployed version is {target.deployed_version!r}, expected {version!r}"
        )

    conf = fs_root / "release.conf"
    if not conf.exists():
        problems.append("release.conf is missing (config was not written)")
    elif f"version={version}" not in conf.read_text():
        problems.append(f"release.conf does not declare version={version}")

    columns = dialect.columns(conn)
    if FLAG_COLUMN not in columns:
        problems.append(
            f"accounts.{FLAG_COLUMN} column is missing (migration not applied)"
        )
    else:
        nulls = conn.execute(
            f"SELECT COUNT(*) FROM accounts WHERE {FLAG_COLUMN} IS NULL"
        ).fetchone()[0]
        if nulls:
            problems.append(
                f"{nulls} existing account row(s) have {FLAG_COLUMN} IS NULL "
                "(column added but not backfilled)"
            )

    return problems


def build_tools(
    target: DeployTarget, *, db_conn: Any, fs_root: Path, dialect: SqlDialect = SQLITE
) -> list[Callable[..., Any]]:
    """Register and return the agent's release tools (+ the compensator).

    ``db_conn`` / ``fs_root`` are the same connection and root the adapters are
    bound to; ``smoke_test`` closes over them to read the live post-deploy state
    when it fires at commit-time. ``dialect`` carries the SQL that differs between
    SQLite and Postgres (the param placeholder, the column introspection) so the
    same tools run unchanged on either backend. Tools must be registered *inside*
    a function
    (never at module top level): the ``REGISTRY`` is process-global and
    re-registering a name raises, so the test fixture clears it around each test
    and the caller registers fresh. The returned list is the agent's tool
    surface — ``rollback_deploy`` is not in it (the agent never calls a
    compensator directly; the engine fires it).
    """

    @tool(resource="sql")
    def add_column(conn, column: str) -> str:
        """Add a column to the accounts table (reversible schema migration).

        Existing rows get NULL for the new column — backfill separately if the
        application needs a value for every row.
        """
        execute_isolated(
            conn,
            f"ALTER TABLE accounts ADD COLUMN {_safe_ident(column)} TEXT",
            writes=[("accounts", "schema")],
        )
        return (
            f"added column {column!r} to accounts; existing rows are NULL for it"
        )

    @tool(resource="sql")
    def backfill_column(conn, column: str, value: str) -> str:
        """Set `column` to `value` for ALL existing accounts rows (reversible)."""
        cur = execute_isolated(
            conn,
            f"UPDATE accounts SET {_safe_ident(column)} = {dialect.placeholder}",
            (value,),
            writes=[("accounts", "rows")],
        )
        return f"backfilled {column!r} = {value!r} on {cur.rowcount} existing rows"

    @tool(resource="fs")
    def write_config(fs: FsHandle, version: str) -> str:
        """Write the release config file (reversible filesystem write)."""
        fs.write("release.conf", f"version={version}\n".encode())
        return f"wrote config for {version!r}"

    @tool(resource="http", reversible=False, injects_handle=False)
    def rollback_deploy(version: str) -> str:
        """Compensator for ``deploy``: tear the deployed version back down.

        Same args as ``deploy`` (the engine fires a compensator with the
        compensated effect's original args). Idempotency is the developer's
        responsibility — ``DeployTarget.rollback`` is a no-op if already torn
        down.
        """
        target.rollback(version)
        return f"rolled back deploy of {version!r}"

    @tool(
        resource="http",
        reversible=False,
        injects_handle=False,
        compensator="rollback_deploy",
    )
    def deploy(version: str) -> str:
        """Deploy a version (irreversible HTTP; compensated by rollback_deploy)."""
        out = target.deploy(version)
        return f"deployed {out['deployed']!r}"

    @tool(resource="http", reversible=False, injects_handle=False)
    def smoke_noop(version: str) -> str:
        """No-op compensator for ``smoke_test``.

        ``smoke_test`` needs *a* compensator only so it clears the commit-time
        gate and actually fires (the gate blocks any staged irreversible that is
        neither compensator-backed nor pre-approved — see ``runtime.py``). It is
        never actually invoked: a *failing* smoke test raises, so its effect ends
        ``FAILED`` (not ``APPLIED``), and ``_partial_unwind`` only compensates
        ``APPLIED`` effects. A *passing* smoke test commits, so again nothing to
        undo. The compensator exists purely to make the check fire rather than
        gate-block.
        """
        return f"smoke test for {version!r} was a no-op (nothing to undo)"

    @tool(
        resource="http",
        reversible=False,
        injects_handle=False,
        compensator="smoke_noop",
    )
    def smoke_test(version: str) -> str:
        """Verify the deployment against REAL post-deploy state; RAISES if unhealthy.

        Irreversible, so it is staged and fires at commit-time *after* ``deploy``
        (journal index order). Health is computed by :func:`health_problems` from
        the actual deployed version, the on-disk config, and the live schema/rows
        — not from any stored flag. If anything is wrong (most commonly: the
        ``feature_flag`` column was added but existing rows were never
        backfilled) it raises ``SmokeTestFailed`` mid-fire, and that raise lands
        in the engine's staged-fire loop and triggers the mixed-fold unwind.
        """
        target.record_smoke(version)
        problems = health_problems(target, db_conn, fs_root, version, dialect)
        if problems:
            raise SmokeTestFailed(version, problems)
        return f"smoke test passed for {version!r}: release is healthy"

    return [add_column, backfill_column, write_config, deploy, smoke_test]


def _safe_ident(name: str) -> str:
    """Reject anything but a plain identifier — column comes from the agent.

    SQLite cannot parameterise an identifier, so the column name is
    interpolated; constrain it to ``[A-Za-z_][A-Za-z0-9_]*`` so an agent (or a
    prompt-injected one) cannot smuggle SQL through the migration tool.
    """
    if not name or not name[0].isalpha() and name[0] != "_":
        raise ValueError(f"illegal column identifier {name!r}")
    if not all(c.isalnum() or c == "_" for c in name):
        raise ValueError(f"illegal column identifier {name!r}")
    return name


def run_release(
    *,
    conn,
    fs_root: Path,
    target: DeployTarget,
    policy: Policy | None = None,
    client_id: str | None = "devops-release",
    client: Any = None,
    audit: AuditJournal | None = None,
    model: str | None = None,
    system: str | None = None,
    task: str | None = None,
    api: str = "anthropic",
    base_url: str | None = None,
    backend: str = "sqlite",
) -> AgentRun:
    """Run the v2 release through a real (or mocked) agent.

    Builds the five tools, wires the three adapters, and drives the agent on the
    release *goal* (``system`` / ``task`` default to the goal-based prompt, but
    the mechanism test can override them). The outcome is genuine: if the agent
    produces a healthy release the txn commits; if it skips the backfill (or
    otherwise leaves the release inconsistent) the commit-time smoke check raises
    and the engine's mixed-fold unwind reverts everything — deploy compensated,
    reversibles restored, txn ``ROLLED_BACK``.

    ``smoke_test`` raising at fire-time is a *domain* commit-time refusal, so it
    is declared in ``commit_refusals``: the harness captures it onto
    ``AgentRun.error`` (just like the engine's own gate/isolation refusals) and
    returns the unwound run — its ``journal`` is the real ``ctx.txn.effects``, so
    the caller inspects the result rather than catching the exception. ``client``
    is injectable: the offline mechanism test passes a mock; a keyed real-agent
    run passes ``None`` and the harness builds the real client. ``api`` /
    ``base_url`` select the chat backend — leave them at the default for cloud
    Anthropic, or pass ``api="openai", base_url="http://localhost:11434/v1"``
    (and a local ``model``) to run the *same* release, with the *same* genuine
    outcome and unwind, against a local open-source model: the model-blindness
    proof.
    """
    audit = audit or AuditJournal.in_memory()
    dialect = dialect_for(backend)
    sql_adapter = (
        PostgresAdapter(conn) if backend == "postgres" else SQLiteAdapter(conn)
    )
    tools = build_tools(target, db_conn=conn, fs_root=fs_root, dialect=dialect)
    adapters = {
        "sql": sql_adapter,
        "fs": FilesystemAdapter(fs_root),
        "http": HTTPAdapter(),
    }
    kwargs: dict[str, Any] = dict(
        task=task or TASK,
        system=system or SYSTEM,
        tools=tools,
        adapters=adapters,
        policy=policy or Policy.allow_all(),
        client_id=client_id,
        client=client,
        audit=audit,
        commit_refusals=(SmokeTestFailed,),
        api=api,
        base_url=base_url,
    )
    if model is not None:
        kwargs["model"] = model
    return run_agent(**kwargs)
