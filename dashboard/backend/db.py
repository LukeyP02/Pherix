"""Control-plane data layer — a parameterised-SQL store over SQLite.

This is the *control plane*'s own database, distinct from any agent's local audit
journal (``pherix/core/audit.py``). It holds org-scale metadata plus the *ingested,
retained* journal aggregated across many hosts. SQLite for v1; every query is
parameterised and the schema is written so a Postgres swap is mechanical (no
SQLite-only SQL on the hot paths — ``INSERT OR IGNORE`` is the one idiom to port,
called out where used).

Three groups of tables:

* **identity** — ``orgs``, ``users``, ``api_keys``
* **fleet + policy** — ``agents``, ``policies``, ``policy_versions``, ``policy_assignments``
* **retained journal** — ``ingest_transactions``, ``ingest_effects``, ``ingest_verdicts``;
  these mirror the audit-journal row shapes but are multi-tenant (every row carries
  ``org_id`` + ``agent_id``), carry a ``session_id`` for timeline grouping, and a
  monotonic ``seq`` per org so audit search can page deterministically.

Tenant isolation is by construction: every org-scoped query takes ``org_id`` and
filters on it; the store never returns a row from another org.
"""

from __future__ import annotations

import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator

_SCHEMA = """
-- identity ------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS orgs (
    org_id     TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS users (
    user_id    TEXT PRIMARY KEY,
    org_id     TEXT NOT NULL REFERENCES orgs(org_id),
    ref        TEXT NOT NULL,            -- opaque SSO reference (email / group / sub)
    role       TEXT,                     -- opaque label the enterprise assigns
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS api_keys (
    key_id       TEXT PRIMARY KEY,
    org_id       TEXT NOT NULL REFERENCES orgs(org_id),
    key_hash     TEXT NOT NULL UNIQUE,   -- sha256 of the plaintext; plaintext never stored
    name         TEXT,
    created_at   TEXT NOT NULL,
    last_used_at TEXT,
    revoked      INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_keys_org ON api_keys(org_id);

-- fleet + policy ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS agents (
    agent_id   TEXT NOT NULL,
    org_id     TEXT NOT NULL REFERENCES orgs(org_id),
    name       TEXT NOT NULL,
    owner      TEXT NOT NULL,            -- opaque SSO reference — the accountable party
    created_at TEXT NOT NULL,
    PRIMARY KEY (org_id, agent_id)
);
CREATE TABLE IF NOT EXISTS policies (
    policy_id  TEXT NOT NULL,
    org_id     TEXT NOT NULL REFERENCES orgs(org_id),
    name       TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (org_id, policy_id)
);
CREATE TABLE IF NOT EXISTS policy_versions (
    org_id     TEXT NOT NULL,
    policy_id  TEXT NOT NULL,
    version    INTEGER NOT NULL,         -- 1-based, monotonic per policy
    spec       TEXT NOT NULL,            -- PolicySpec JSON (opaque to the control plane)
    created_at TEXT NOT NULL,
    PRIMARY KEY (org_id, policy_id, version)
);
-- An agent pulls exactly one assigned policy. version NULL = "always latest".
CREATE TABLE IF NOT EXISTS policy_assignments (
    org_id      TEXT NOT NULL,
    agent_id    TEXT NOT NULL,
    policy_id   TEXT NOT NULL,
    version     INTEGER,                 -- NULL pins to latest at pull time
    assigned_at TEXT NOT NULL,
    PRIMARY KEY (org_id, agent_id)
);

-- retained journal (multi-tenant, append-only) ------------------------------
-- seq is a per-org monotonic cursor: ingest assigns it, audit search pages by it.
CREATE TABLE IF NOT EXISTS ingest_cursor (
    org_id   TEXT PRIMARY KEY,
    next_seq INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS ingest_transactions (
    org_id      TEXT NOT NULL,
    agent_id    TEXT NOT NULL,
    txn_id      TEXT NOT NULL,
    session_id  TEXT,                    -- supplied by the SDK; groups a session timeline
    host        TEXT,                    -- provenance: which host shipped it
    state       TEXT NOT NULL,
    created_at  TEXT,
    updated_at  TEXT,
    dry_run     INTEGER NOT NULL DEFAULT 0,
    client_id   TEXT,
    seq         INTEGER NOT NULL,
    ingested_at TEXT NOT NULL,
    PRIMARY KEY (org_id, agent_id, txn_id)
);
CREATE INDEX IF NOT EXISTS idx_txn_session ON ingest_transactions(org_id, session_id);
CREATE INDEX IF NOT EXISTS idx_txn_seq     ON ingest_transactions(org_id, seq);
CREATE TABLE IF NOT EXISTS ingest_effects (
    org_id      TEXT NOT NULL,
    agent_id    TEXT NOT NULL,
    txn_id      TEXT NOT NULL,
    idx         INTEGER NOT NULL,
    effect_id   TEXT NOT NULL,
    tool        TEXT NOT NULL,
    resource    TEXT NOT NULL,
    reversible  INTEGER NOT NULL,
    status      TEXT NOT NULL,
    args        TEXT,                    -- JSON, PII redacted client-side before ingest
    result      TEXT,
    ts          TEXT,
    seq         INTEGER NOT NULL,
    ingested_at TEXT NOT NULL,
    PRIMARY KEY (org_id, agent_id, txn_id, idx)
);
CREATE INDEX IF NOT EXISTS idx_eff_tool ON ingest_effects(org_id, tool);
CREATE INDEX IF NOT EXISTS idx_eff_seq  ON ingest_effects(org_id, seq);
CREATE TABLE IF NOT EXISTS ingest_verdicts (
    org_id       TEXT NOT NULL,
    agent_id     TEXT NOT NULL,
    txn_id       TEXT NOT NULL,
    effect_index INTEGER NOT NULL,
    seq_in_txn   INTEGER NOT NULL,
    phase        TEXT NOT NULL,
    allow        INTEGER NOT NULL,
    kind         TEXT NOT NULL,
    rule_name    TEXT,
    reason       TEXT,
    seq          INTEGER NOT NULL,
    ingested_at  TEXT NOT NULL,
    PRIMARY KEY (org_id, agent_id, txn_id, seq_in_txn)
);
CREATE INDEX IF NOT EXISTS idx_verdict_allow ON ingest_verdicts(org_id, allow);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:24]}"


class Store:
    """SQLite-backed control-plane store.

    A connection is opened per operation (WAL mode), so the store is safe to share
    across FastAPI's threadpool without a global lock. ``path`` of ``":memory:"``
    uses a shared-cache URI so the in-memory DB survives across those per-op
    connections within one process — handy for tests.
    """

    def __init__(self, path: str):
        if path == ":memory:":
            # A bare ":memory:" gives each connection its OWN db; shared-cache makes
            # them see one db for the process lifetime. We pin a sentinel connection
            # open so the shared db is not torn down between operations. The cache
            # name is unique per Store instance so two in-memory stores in one
            # process (e.g. across tests) stay isolated rather than colliding.
            self._uri = f"file:pherix_cp_mem_{uuid.uuid4().hex}?mode=memory&cache=shared"
            self._pin: sqlite3.Connection | None = sqlite3.connect(
                self._uri, uri=True
            )
        else:
            self._uri = path
            self._pin = None
        with self._connect() as conn:
            conn.executescript(_SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._uri, uri=self._uri.startswith("file:"))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        if not self._uri.startswith("file:pherix_cp_mem"):
            conn.execute("PRAGMA journal_mode = WAL")
        return conn

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        conn = self._connect()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def close(self) -> None:
        if self._pin is not None:
            self._pin.close()
            self._pin = None

    # -- cursor -----------------------------------------------------------------

    def _next_seq(self, conn: sqlite3.Connection, org_id: str, count: int) -> int:
        """Reserve ``count`` monotonic seq values for an org; return the first.

        Uses an upsert on ``ingest_cursor`` so concurrent ingests never reuse a
        seq. Returns the first reserved value (callers number 0..count-1 from it).
        """
        conn.execute(
            "INSERT INTO ingest_cursor (org_id, next_seq) VALUES (?, ?) "
            "ON CONFLICT(org_id) DO UPDATE SET next_seq = next_seq + ?",
            (org_id, 1 + count, count),
        )
        row = conn.execute(
            "SELECT next_seq FROM ingest_cursor WHERE org_id = ?", (org_id,)
        ).fetchone()
        return int(row["next_seq"]) - count

    def high_water(self, org_id: str) -> int:
        """The largest seq the control plane has assigned for this org (0 if none)."""
        with self._tx() as conn:
            row = conn.execute(
                "SELECT next_seq FROM ingest_cursor WHERE org_id = ?", (org_id,)
            ).fetchone()
            return (int(row["next_seq"]) - 1) if row else 0

    # -- orgs -------------------------------------------------------------------

    def create_org(self, name: str) -> dict:
        org_id = _new_id("org")
        with self._tx() as conn:
            conn.execute(
                "INSERT INTO orgs (org_id, name, created_at) VALUES (?, ?, ?)",
                (org_id, name, _now()),
            )
        return {"org_id": org_id, "name": name}

    def get_org(self, org_id: str) -> dict | None:
        with self._tx() as conn:
            row = conn.execute(
                "SELECT * FROM orgs WHERE org_id = ?", (org_id,)
            ).fetchone()
            return dict(row) if row else None

    # -- api keys ---------------------------------------------------------------

    def insert_key(self, org_id: str, key_hash: str, name: str | None) -> dict:
        key_id = _new_id("key")
        with self._tx() as conn:
            conn.execute(
                "INSERT INTO api_keys (key_id, org_id, key_hash, name, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (key_id, org_id, key_hash, name, _now()),
            )
        return {"key_id": key_id, "org_id": org_id, "name": name}

    def resolve_key(self, key_hash: str) -> dict | None:
        """Map a key hash to its (non-revoked) org; stamp last_used_at."""
        with self._tx() as conn:
            row = conn.execute(
                "SELECT key_id, org_id, revoked FROM api_keys WHERE key_hash = ?",
                (key_hash,),
            ).fetchone()
            if row is None or row["revoked"]:
                return None
            conn.execute(
                "UPDATE api_keys SET last_used_at = ? WHERE key_id = ?",
                (_now(), row["key_id"]),
            )
            return {"key_id": row["key_id"], "org_id": row["org_id"]}

    def list_keys(self, org_id: str) -> list[dict]:
        with self._tx() as conn:
            rows = conn.execute(
                "SELECT key_id, name, created_at, last_used_at, revoked "
                "FROM api_keys WHERE org_id = ? ORDER BY created_at",
                (org_id,),
            ).fetchall()
            return [dict(r) for r in rows]

    def revoke_key(self, org_id: str, key_id: str) -> bool:
        with self._tx() as conn:
            cur = conn.execute(
                "UPDATE api_keys SET revoked = 1 WHERE org_id = ? AND key_id = ?",
                (org_id, key_id),
            )
            return cur.rowcount > 0

    # -- users ------------------------------------------------------------------

    def create_user(self, org_id: str, ref: str, role: str | None) -> dict:
        user_id = _new_id("usr")
        created_at = _now()
        with self._tx() as conn:
            conn.execute(
                "INSERT INTO users (user_id, org_id, ref, role, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (user_id, org_id, ref, role, created_at),
            )
        return {
            "user_id": user_id, "ref": ref, "role": role, "created_at": created_at,
        }

    def list_users(self, org_id: str) -> list[dict]:
        with self._tx() as conn:
            rows = conn.execute(
                "SELECT user_id, ref, role, created_at FROM users "
                "WHERE org_id = ? ORDER BY created_at",
                (org_id,),
            ).fetchall()
            return [dict(r) for r in rows]

    # -- agents -----------------------------------------------------------------

    def create_agent(self, org_id: str, name: str, owner: str) -> dict:
        agent_id = _new_id("agt")
        created_at = _now()
        with self._tx() as conn:
            conn.execute(
                "INSERT INTO agents (agent_id, org_id, name, owner, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (agent_id, org_id, name, owner, created_at),
            )
        return {
            "agent_id": agent_id, "name": name, "owner": owner,
            "created_at": created_at,
        }

    def get_agent(self, org_id: str, agent_id: str) -> dict | None:
        with self._tx() as conn:
            row = conn.execute(
                "SELECT agent_id, name, owner, created_at FROM agents "
                "WHERE org_id = ? AND agent_id = ?",
                (org_id, agent_id),
            ).fetchone()
            return dict(row) if row else None

    def list_agents(self, org_id: str) -> list[dict]:
        with self._tx() as conn:
            rows = conn.execute(
                "SELECT agent_id, name, owner, created_at FROM agents "
                "WHERE org_id = ? ORDER BY created_at",
                (org_id,),
            ).fetchall()
            return [dict(r) for r in rows]

    # -- policies ---------------------------------------------------------------

    def create_policy(self, org_id: str, name: str) -> dict:
        policy_id = _new_id("pol")
        created_at = _now()
        with self._tx() as conn:
            conn.execute(
                "INSERT INTO policies (policy_id, org_id, name, created_at) "
                "VALUES (?, ?, ?, ?)",
                (policy_id, org_id, name, created_at),
            )
        return {"policy_id": policy_id, "name": name, "created_at": created_at}

    def get_policy(self, org_id: str, policy_id: str) -> dict | None:
        with self._tx() as conn:
            row = conn.execute(
                "SELECT policy_id, name, created_at FROM policies "
                "WHERE org_id = ? AND policy_id = ?",
                (org_id, policy_id),
            ).fetchone()
            return dict(row) if row else None

    def list_policies(self, org_id: str) -> list[dict]:
        with self._tx() as conn:
            rows = conn.execute(
                "SELECT policy_id, name, created_at FROM policies "
                "WHERE org_id = ? ORDER BY created_at",
                (org_id,),
            ).fetchall()
            return [dict(r) for r in rows]

    def add_policy_version(self, org_id: str, policy_id: str, spec: str) -> dict:
        """Append a new version (1-based, monotonic). Returns {version, created_at}."""
        created_at = _now()
        with self._tx() as conn:
            row = conn.execute(
                "SELECT COALESCE(MAX(version), 0) AS v FROM policy_versions "
                "WHERE org_id = ? AND policy_id = ?",
                (org_id, policy_id),
            ).fetchone()
            version = int(row["v"]) + 1
            conn.execute(
                "INSERT INTO policy_versions (org_id, policy_id, version, spec, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (org_id, policy_id, version, spec, created_at),
            )
            return {"version": version, "created_at": created_at}

    def get_policy_version(
        self, org_id: str, policy_id: str, version: int | None
    ) -> dict | None:
        """Fetch a specific version, or the latest when ``version`` is None."""
        with self._tx() as conn:
            if version is None:
                row = conn.execute(
                    "SELECT version, spec, created_at FROM policy_versions "
                    "WHERE org_id = ? AND policy_id = ? "
                    "ORDER BY version DESC LIMIT 1",
                    (org_id, policy_id),
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT version, spec, created_at FROM policy_versions "
                    "WHERE org_id = ? AND policy_id = ? AND version = ?",
                    (org_id, policy_id, version),
                ).fetchone()
            return dict(row) if row else None

    def list_policy_versions(self, org_id: str, policy_id: str) -> list[dict]:
        with self._tx() as conn:
            rows = conn.execute(
                "SELECT version, created_at FROM policy_versions "
                "WHERE org_id = ? AND policy_id = ? ORDER BY version",
                (org_id, policy_id),
            ).fetchall()
            return [dict(r) for r in rows]

    def assign_policy(
        self, org_id: str, agent_id: str, policy_id: str, version: int | None
    ) -> None:
        with self._tx() as conn:
            conn.execute(
                "INSERT INTO policy_assignments "
                "(org_id, agent_id, policy_id, version, assigned_at) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(org_id, agent_id) DO UPDATE SET "
                "policy_id = excluded.policy_id, version = excluded.version, "
                "assigned_at = excluded.assigned_at",
                (org_id, agent_id, policy_id, version, _now()),
            )

    def get_assignment(self, org_id: str, agent_id: str) -> dict | None:
        with self._tx() as conn:
            row = conn.execute(
                "SELECT policy_id, version, assigned_at FROM policy_assignments "
                "WHERE org_id = ? AND agent_id = ?",
                (org_id, agent_id),
            ).fetchone()
            return dict(row) if row else None

    # -- journal ingest (append-only, idempotent) -------------------------------

    def ingest(
        self,
        org_id: str,
        agent_id: str,
        host: str | None,
        transactions: list[dict],
        effects: list[dict],
        verdicts: list[dict],
    ) -> dict:
        """Append a batch of journal rows. Idempotent: re-shipping a row whose
        primary key already exists is silently skipped (``INSERT OR IGNORE`` —
        the one SQLite idiom to port to ``ON CONFLICT DO NOTHING`` on Postgres).

        Returns accepted/skipped counts and the org's new high-water seq.
        """
        total = len(transactions) + len(effects) + len(verdicts)
        accepted = 0
        now = _now()
        with self._tx() as conn:
            base = self._next_seq(conn, org_id, total) if total else self.high_water(org_id)
            seq = base
            for t in transactions:
                cur = conn.execute(
                    "INSERT OR IGNORE INTO ingest_transactions "
                    "(org_id, agent_id, txn_id, session_id, host, state, created_at, "
                    "updated_at, dry_run, client_id, seq, ingested_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        org_id, agent_id, t["txn_id"], t.get("session_id"), host,
                        t["state"], t.get("created_at"), t.get("updated_at"),
                        int(bool(t.get("dry_run", False))), t.get("client_id"),
                        seq, now,
                    ),
                )
                accepted += cur.rowcount
                seq += 1
            for e in effects:
                cur = conn.execute(
                    "INSERT OR IGNORE INTO ingest_effects "
                    "(org_id, agent_id, txn_id, idx, effect_id, tool, resource, "
                    "reversible, status, args, result, ts, seq, ingested_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        org_id, agent_id, e["txn_id"], e["idx"], e["effect_id"],
                        e["tool"], e["resource"], int(bool(e.get("reversible", False))),
                        e["status"], e.get("args"), e.get("result"), e.get("ts"),
                        seq, now,
                    ),
                )
                accepted += cur.rowcount
                seq += 1
            for v in verdicts:
                cur = conn.execute(
                    "INSERT OR IGNORE INTO ingest_verdicts "
                    "(org_id, agent_id, txn_id, effect_index, seq_in_txn, phase, "
                    "allow, kind, rule_name, reason, seq, ingested_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        org_id, agent_id, v["txn_id"], v["effect_index"],
                        v["seq_in_txn"], v["phase"], int(bool(v["allow"])),
                        v.get("kind", "rule"), v.get("rule_name"), v.get("reason"),
                        seq, now,
                    ),
                )
                accepted += cur.rowcount
                seq += 1
        return {
            "accepted": accepted,
            "skipped": total - accepted,
            "cursor": self.high_water(org_id),
        }

    # -- cross-host audit search ------------------------------------------------

    def search_effects(
        self,
        org_id: str,
        *,
        agent_id: str | None = None,
        session_id: str | None = None,
        tool: str | None = None,
        status: str | None = None,
        resource: str | None = None,
        since: str | None = None,
        until: str | None = None,
        limit: int = 200,
    ) -> list[dict]:
        """Fold every ingested effect for an org through a filter — cross-host,
        because rows from all agents/hosts land in one store. session_id is joined
        from the parent transaction so callers can filter a whole session."""
        clauses = ["e.org_id = ?"]
        params: list[Any] = [org_id]
        if agent_id:
            clauses.append("e.agent_id = ?"); params.append(agent_id)
        if tool:
            clauses.append("e.tool = ?"); params.append(tool)
        if status:
            clauses.append("e.status = ?"); params.append(status)
        if resource:
            clauses.append("e.resource = ?"); params.append(resource)
        if since:
            clauses.append("e.ts >= ?"); params.append(since)
        if until:
            clauses.append("e.ts <= ?"); params.append(until)
        if session_id:
            clauses.append("t.session_id = ?"); params.append(session_id)
        params.append(min(max(int(limit), 1), 1000))
        with self._tx() as conn:
            rows = conn.execute(
                "SELECT e.agent_id, e.txn_id, e.idx, e.effect_id, e.tool, e.resource, "
                "e.reversible, e.status, e.args, e.result, e.ts, e.seq, "
                "t.session_id, t.host "
                "FROM ingest_effects e "
                "LEFT JOIN ingest_transactions t "
                "  ON t.org_id = e.org_id AND t.agent_id = e.agent_id "
                "  AND t.txn_id = e.txn_id "
                f"WHERE {' AND '.join(clauses)} "
                "ORDER BY e.seq DESC LIMIT ?",
                params,
            ).fetchall()
            return [dict(r) for r in rows]

    def search_verdicts(
        self,
        org_id: str,
        *,
        allow: bool | None = None,
        agent_id: str | None = None,
        rule_name: str | None = None,
        limit: int = 200,
    ) -> list[dict]:
        """Search recorded policy decisions across the fleet — e.g. allow=False
        answers 'every denied effect last week'."""
        clauses = ["org_id = ?"]
        params: list[Any] = [org_id]
        if allow is not None:
            clauses.append("allow = ?"); params.append(int(allow))
        if agent_id:
            clauses.append("agent_id = ?"); params.append(agent_id)
        if rule_name:
            clauses.append("rule_name = ?"); params.append(rule_name)
        params.append(min(max(int(limit), 1), 1000))
        with self._tx() as conn:
            rows = conn.execute(
                "SELECT agent_id, txn_id, effect_index, phase, allow, kind, "
                "rule_name, reason, seq FROM ingest_verdicts "
                f"WHERE {' AND '.join(clauses)} "
                "ORDER BY seq DESC LIMIT ?",
                params,
            ).fetchall()
            return [dict(r) for r in rows]

    def session_timeline(self, org_id: str, session_id: str) -> dict | None:
        """Group every transaction + effect of a session into an ordered timeline —
        the observational 'session replay' the dashboard renders. Returns None when
        the session has no ingested transactions."""
        with self._tx() as conn:
            txns = conn.execute(
                "SELECT agent_id, txn_id, host, state, created_at, updated_at, "
                "dry_run, client_id, seq FROM ingest_transactions "
                "WHERE org_id = ? AND session_id = ? ORDER BY seq",
                (org_id, session_id),
            ).fetchall()
            if not txns:
                return None
            txn_ids = [t["txn_id"] for t in txns]
            placeholders = ",".join("?" for _ in txn_ids)
            effects = conn.execute(
                "SELECT agent_id, txn_id, idx, tool, resource, reversible, status, "
                "args, result, ts, seq FROM ingest_effects "
                f"WHERE org_id = ? AND txn_id IN ({placeholders}) "
                "ORDER BY seq",
                (org_id, *txn_ids),
            ).fetchall()
        return {
            "session_id": session_id,
            "transactions": [dict(t) for t in txns],
            "effects": [dict(e) for e in effects],
        }
