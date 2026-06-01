"""The Pherix library API surface — what wrapping an agent's tool layer needs.

``frontends/`` stays thin; ``core/`` knows nothing about how it is driven. That
is what lets the MCP gateway front-end (Slice 8) bolt onto the same core with no
rewrite.

Slice 3 adds the irreversible lane: ``HTTPAdapter`` (the honest "I cannot
roll back" adapter), ``StagedResult`` (the sentinel agents receive from
staged tool calls), and the gate-related errors ``GateBlocked`` /
``CompensatorNotRegistered``. ``approve_irreversible`` lives on the
``TxnContext`` yielded by :func:`agent_txn`, not as a standalone function:
approval is *per-transaction*, not global.

Slice 4 adds isolation: the resolution policies (:class:`Abort`,
:class:`Retry`, :class:`Serialize`), the :class:`IsolationConflict`
exception, the in-process :data:`JournalRegistry` arbitration substrate,
and :func:`run_txn` — the Pherix-driven entry point that makes
:class:`Retry` mechanically honest by owning the callable Pherix may
re-invoke on conflict.
"""

from typing import Any, Callable

from pherix.core.adapters.base import (
    ResourceAdapter,
    SnapshotHandle,
    TransactionalResourceAdapter,
    VersionedResourceAdapter,
)
from pherix.core.adapters.dynamodb import DynamoDBAdapter
from pherix.core.adapters.elasticsearch import ElasticsearchAdapter
from pherix.core.adapters.filesystem import FilesystemAdapter
from pherix.core.adapters.gcs import GCSAdapter
from pherix.core.adapters.http import HTTPAdapter, IrreversibleAdapterError
from pherix.core.adapters.memory import MemoryAdapter, MemoryHandle
from pherix.core.adapters.messagequeue import (
    Broker,
    MQAdapter,
    publish_tool,
    tombstone_compensator,
)
from pherix.core.adapters.mongodb import MongoAdapter
from pherix.core.adapters.mysql import MySQLAdapter
from pherix.core.adapters.postgres import PostgresAdapter
from pherix.core.adapters.redis import RedisAdapter
from pherix.core.adapters.rest import RESTAdapter, graphql_tool, rest_tool
from pherix.core.adapters.s3 import S3Adapter
from pherix.core.adapters.sql import SQLiteAdapter
from pherix.core.memory import (
    MemoryTools,
    memory_byte_cap,
    no_pii,
    register_memory_tools,
)
from pherix.core.audit import AuditJournal
from pherix.core.dry_run import DryRunResult, dry_run
from pherix.core.effects import StagedResult
from pherix.core.isolation import (
    REGISTRY as JournalRegistry,
    Abort,
    Conflict,
    IsolationConflict,
    Retry,
    Serialize,
    _RetrySignal,
    _in_run_txn,
)
from pherix.core.envelope import DurableCap, EnvelopeStore, day_period
from pherix.core.policy import (
    Allow,
    Cap,
    Deny,
    Policy,
    PolicyContext,
    PolicyRule,
    PolicyVerdict,
    PolicyViolation,
    refund_if_paid,
    sql_reader,
)
from pherix.core.recovery import (
    EffectRecovery,
    RecoveryReport,
    TxnRecovery,
    recover,
)
from pherix.core.replay import (
    EffectOutcome,
    ReplayDivergence,
    ReplayResult,
    replay,
)
from pherix.core.runtime import CompensatorNotRegistered, GateBlocked, agent_txn
from pherix.core.tools import acting_as, tool


def run_txn(
    fn: Callable[[Any], None],
    adapters: dict[str, Any],
    *,
    policy: Policy | None = None,
    audit: AuditJournal | None = None,
    isolation: Any = None,
) -> None:
    """Pherix-driven transactional run — the entry point for :class:`Retry`.

    ``fn(ctx)`` receives the :class:`TxnContext` and runs the agent body
    exactly as it would inside ``with agent_txn(...) as ctx``. The
    difference is that Pherix now owns the callable: under
    ``isolation=Retry(N)``, if the commit-time diff flags a conflict
    Pherix rolls back, opens a fresh transaction, and re-invokes ``fn``
    up to ``N`` times.

    Use this entry point when the resolution policy is :class:`Retry`.
    Re-entering a ``with agent_txn(...)`` block from outside is
    mechanically impossible (the body is not a callable Pherix owns), so
    with the context-manager form :class:`Retry` degrades to
    :class:`Abort` and the first conflict raises :class:`IsolationConflict`
    immediately. The :data:`_in_run_txn` contextvar is what selects
    between the two paths.

    Default ``isolation=Abort()`` matches :func:`agent_txn`; ``run_txn``
    with ``Abort`` is exactly equivalent to ``with agent_txn(...) as
    ctx: fn(ctx)``.

    Idempotency caveat: every replay re-invokes ``fn`` from the top
    against a fresh :class:`TxnContext`. Effects routed through Pherix
    (SQL via :func:`execute_isolated`, FS via :class:`FsHandle`, HTTP
    staged effects) are unwound between attempts. Side effects that
    bypass Pherix's seam — appending to a module-level list, raw
    ``open()`` writes, unwrapped HTTP requests, mutating closure
    variables — fire on *every* attempt. Design ``fn`` so a replay is
    safe to repeat, or move the side effect through a Pherix tool.
    """
    isolation = isolation if isolation is not None else Abort()
    max_attempts = isolation.max_attempts if isinstance(isolation, Retry) else 1
    last_conflicts: list[Conflict] = []
    # Flag the retry loop so Retry.resolve raises the internal
    # _RetrySignal (which we catch) instead of the public
    # IsolationConflict (which would short-circuit the loop). The
    # contextvar is reset on every exit path, including exceptions.
    token = _in_run_txn.set(True)
    try:
        for _ in range(max_attempts):
            try:
                with agent_txn(
                    adapters, policy=policy, audit=audit, isolation=isolation
                ) as ctx:
                    fn(ctx)
                return
            except _RetrySignal as sig:
                last_conflicts = sig.conflicts
                continue
        # Exhausted: convert the last retry signal into a real
        # IsolationConflict so the caller sees a familiar exception type.
        raise IsolationConflict(last_conflicts)
    finally:
        _in_run_txn.reset(token)


__all__ = [
    "agent_txn",
    "run_txn",
    "replay",
    "dry_run",
    "DryRunResult",
    "ReplayResult",
    "ReplayDivergence",
    "EffectOutcome",
    "tool",
    "acting_as",
    "Policy",
    "PolicyContext",
    "PolicyRule",
    "PolicyVerdict",
    "PolicyViolation",
    "Allow",
    "Deny",
    "Cap",
    "SQLiteAdapter",
    "FilesystemAdapter",
    "HTTPAdapter",
    # governed memory — adapter + policy, not a new axis
    "MemoryAdapter",
    "MemoryHandle",
    "MemoryTools",
    "register_memory_tools",
    "no_pii",
    "memory_byte_cap",
    # adapter-compensator-base: reversible backends (snapshot/savepoint lane)
    "PostgresAdapter",
    "MySQLAdapter",
    "MongoAdapter",
    "S3Adapter",
    "RedisAdapter",
    "DynamoDBAdapter",
    "GCSAdapter",
    "ElasticsearchAdapter",
    # adapter-compensator-base: irreversible transports (staged/compensated lane)
    "RESTAdapter",
    "rest_tool",
    "graphql_tool",
    "MQAdapter",
    "Broker",
    "publish_tool",
    "tombstone_compensator",
    "AuditJournal",
    "ResourceAdapter",
    "TransactionalResourceAdapter",
    "VersionedResourceAdapter",
    "SnapshotHandle",
    "StagedResult",
    "GateBlocked",
    "CompensatorNotRegistered",
    "IrreversibleAdapterError",
    "Abort",
    "Retry",
    "Serialize",
    "IsolationConflict",
    "Conflict",
    "JournalRegistry",
    # #7 world-state-aware policy
    "sql_reader",
    "refund_if_paid",
    # #10 longitudinal envelope
    "DurableCap",
    "EnvelopeStore",
    "day_period",
    # #9 crash-consistent recovery
    "recover",
    "RecoveryReport",
    "TxnRecovery",
    "EffectRecovery",
]
