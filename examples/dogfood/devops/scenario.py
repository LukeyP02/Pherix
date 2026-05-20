"""The DevOps dogfood's tools, deploy target, and run logic.

Separated from ``__main__`` so the offline test can import ``build_tools`` /
``run_release`` and drive them with a mocked Anthropic client — proving the
*composition* (four tools wired across three adapters, unwound atomically by a
failing smoke test) without a key or a network.

The four tools and their wiring:

  - ``run_migration`` — resource ``"sql"``, reversible. Alters the scratch
    schema; rolls back via the SAVEPOINT the SQLiteAdapter takes.
  - ``write_config`` — resource ``"fs"``, reversible. Writes a release config
    file; restores from the per-effect copy-on-write backup.
  - ``deploy`` — resource ``"http"``, **irreversible**, declares the
    ``rollback_deploy`` compensator. Staged at stage-time, fired at commit.
  - ``smoke_test`` — resource ``"http"``, **irreversible**, no compensator.
    Staged after ``deploy``; engineered to FAIL, and a failing smoke test
    *raises* at fire-time.

Why a raising smoke test is the unwind trigger
----------------------------------------------
Both ``deploy`` and ``smoke_test`` are irreversible, so the engine *stages*
them and fires them at ``commit()`` in journal index order (``runtime.py``,
the ``for e in staged`` fold). ``deploy`` fires first and succeeds — the
release is now live. ``smoke_test`` fires next and *raises* because the
deployment is bad. A mid-fire raise in the staged-fire loop drops straight
into ``TxnContext._partial_unwind``: it walks the journal backward, invoking
``rollback_deploy`` for the already-fired (APPLIED, irreversible) deploy and
restoring the reversible SQL + FS effects from their snapshots. The terminal
state is ``ROLLED_BACK`` (clean unwind — the compensator exists and succeeds).

This makes the smoke test the *gate on the irreversible effect*: the deploy
only "counts" if the post-deploy check passes. The agent never has to *decide*
to roll back — the engine does it the instant the check fails, which is the
guarantee a human operator actually wants from a release pipeline.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from pherix.core.adapters.filesystem import FilesystemAdapter, FsHandle
from pherix.core.adapters.http import HTTPAdapter
from pherix.core.adapters.sql import SQLiteAdapter, execute_isolated
from pherix.core.audit import AuditJournal
from pherix.core.policy import Policy
from pherix.core.tools import tool

from examples.dogfood.harness import AgentRun, run_agent

SYSTEM = """You are a release engineer. You ship a release in this exact order:
1. run_migration — apply the schema change.
2. write_config — write the release config file.
3. deploy — deploy the new version.
4. smoke_test — verify the deployment is healthy.
Call the tools one at a time, in that order. Do not skip the smoke test.
After smoke_test, stop and report the outcome."""

TASK = (
    "Ship release v2: add a `feature_flag` column to the `accounts` table, "
    "write the config for version 'v2', deploy version 'v2', then run the "
    "smoke test against it."
)


class SmokeTestFailed(RuntimeError):
    """Raised by ``smoke_test`` when the deployed release is unhealthy.

    Raised at fire-time (the staged-fire loop in ``commit()``), so the engine
    treats it as a mid-commit failure and runs the mixed-fold unwind — the
    whole release reverts. This is the dogfood's whole point.
    """


@dataclass
class DeployTarget:
    """A deterministic in-memory stand-in for a real deploy endpoint.

    A real DevOps agent would point ``deploy`` at k3d / a cloud API here. The
    dogfood keeps it in-process so the run is offline and the journal tells the
    whole story. ``healthy`` is forced ``False`` so the smoke test always fails
    — the engineered failure that drives the atomic unwind.
    """

    deployed_version: str | None = None
    healthy: bool = False
    history: list[dict] = field(default_factory=list)

    def deploy(self, version: str) -> dict:
        self.deployed_version = version
        self.history.append({"action": "deploy", "version": version})
        return {"deployed": version}

    def rollback(self, version: str) -> None:
        self.history.append({"action": "rollback", "version": version})
        self.deployed_version = None

    def smoke(self, version: str) -> bool:
        self.history.append({"action": "smoke", "version": version})
        return self.healthy


def build_tools(target: DeployTarget) -> list[Callable[..., Any]]:
    """Register and return the four release tools (+ the compensator).

    Tools must be registered *inside* a function (never at module top level):
    the ``REGISTRY`` is process-global and re-registering a name raises, so the
    test fixture clears it around each test and the caller registers fresh.
    The returned list is the agent's tool surface — ``rollback_deploy`` is not
    in it (the agent never calls a compensator directly; the engine fires it).
    """

    @tool(resource="sql")
    def run_migration(conn, column: str) -> str:
        """Add a column to the accounts table (reversible schema migration)."""
        execute_isolated(
            conn,
            f"ALTER TABLE accounts ADD COLUMN {_safe_ident(column)} TEXT",
            writes=[("accounts", "schema")],
        )
        return f"migrated: added column {column!r}"

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
        gate and actually fires (the gate blocks any staged irreversible that
        is neither compensator-backed nor pre-approved — see ``runtime.py``).
        It is never actually invoked: a *failing* smoke test raises, so its
        effect ends ``FAILED`` (not ``APPLIED``), and ``_partial_unwind`` only
        compensates ``APPLIED`` effects. A *passing* smoke test commits, so
        again nothing to undo. The compensator exists purely to make the check
        fire rather than gate-block.
        """
        return f"smoke test for {version!r} was a no-op (nothing to undo)"

    @tool(
        resource="http",
        reversible=False,
        injects_handle=False,
        compensator="smoke_noop",
    )
    def smoke_test(version: str) -> str:
        """Verify the deployment; RAISES if the release is unhealthy.

        Irreversible, so it is staged and fires at commit-time *after*
        ``deploy`` (journal index order). The ``smoke_noop`` compensator lets
        it clear the gate and fire. A failing check raises ``SmokeTestFailed``
        mid-fire — that raise lands in the engine's staged-fire loop and
        triggers the mixed-fold unwind: the already-fired ``deploy`` is
        compensated by ``rollback_deploy``, and the reversible migration +
        config restore from their snapshots.
        """
        if not target.smoke(version):
            raise SmokeTestFailed(
                f"smoke test failed for {version!r}: deployment is unhealthy"
            )
        return f"smoke test passed for {version!r}"

    return [run_migration, write_config, deploy, smoke_test]


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
) -> AgentRun:
    """Run the failing-smoke release through a real (or mocked) agent.

    Builds the four tools, wires the three adapters, and drives the agent on
    the release task. The smoke test fails and raises at commit-time, which
    triggers the engine's mixed-fold unwind: deploy is compensated, the
    reversible migration + config restore, and the txn lands ``ROLLED_BACK``.

    ``smoke_test`` raising at fire-time is a *domain* commit-time refusal, so it
    is declared in ``commit_refusals``: the harness captures it onto
    ``AgentRun.error`` (just like the engine's own gate/isolation refusals) and
    returns the unwound run — its ``journal`` is the real ``ctx.txn.effects``,
    so the caller inspects the result rather than catching the exception.
    ``client`` is injectable: the offline test passes a mock; a keyed run
    passes ``None`` and the harness builds the real Anthropic client.
    """
    audit = audit or AuditJournal.in_memory()
    tools = build_tools(target)
    adapters = {
        "sql": SQLiteAdapter(conn),
        "fs": FilesystemAdapter(fs_root),
        "http": HTTPAdapter(),
    }
    kwargs: dict[str, Any] = dict(
        task=TASK,
        system=SYSTEM,
        tools=tools,
        adapters=adapters,
        policy=policy or Policy.allow_all(),
        client_id=client_id,
        client=client,
        audit=audit,
        commit_refusals=(SmokeTestFailed,),
    )
    if model is not None:
        kwargs["model"] = model
    return run_agent(**kwargs)
