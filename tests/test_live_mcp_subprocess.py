"""Live subprocess parity — the honest gap Slice 8 flagged, closed.

Slice 8 proved the MCP wire correct two ways: by inspection, and through the
in-process :class:`InProcessMCPClient` stub (which hands a request dict straight
to ``MCPServer.handle`` — no I/O). What it never did is run the gateway as a
*real child process* and speak real newline-delimited JSON-RPC over a real
stdin/stdout pipe. That boundary is where framing bugs, flush bugs, import-order
bugs, and registry-population bugs actually live.

This test spawns ``python -m pherix.frontends.proxy <config>`` with
``subprocess.Popen`` (stdin/stdout pipes), runs the full tool-call subset over
the pipe — ``initialize`` / ``tools/list`` / a committed ``tools/call`` / a
policy-denied ``tools/call`` / a dry-run ``tools/call`` — and asserts **parity**
with the same operations run through the in-process stub against an
identically-built gateway. Same tool list, same committed/denied/dry-run
outcomes. A temp ON-DISK SQLite file lets the parent verify the child's commit
actually landed in the database.

Fully offline: the gateway *dispatches* tools, it never calls an LLM. No key,
no network, no ``anthropic`` import on this path.

Robustness: every pipe read is bounded by a timeout (a hung child fails the
test loudly instead of blocking the suite), and the child is always terminated
in a ``finally`` block. Clean EOF shutdown is asserted — closing stdin makes the
child's ``serve_stdio`` loop return and the process exit 0.
"""

from __future__ import annotations

import json
import os
import queue
import sqlite3
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

import pytest

from pherix.core.adapters.sql import SQLiteAdapter
from pherix.core.audit import AuditJournal
from pherix.core.policy import Policy
from pherix.core.tools import REGISTRY, tool
from pherix.frontends.proxy import InProcessMCPClient, PherixGateway
from pherix.frontends.proxy.server import POLICY_VIOLATION

# The child reads this env var to find the on-disk DB the parent created, so the
# parent can re-open the same file and verify the committed row.
_DB_ENV = "PHERIX_TEST_DB"

# A self-contained config module the child imports. Importing it (a) registers
# the @tool functions into the child's global REGISTRY and (b) wires the
# adapters/policies. It mirrors exactly what the in-process gateway below builds,
# so parity is a like-for-like comparison.
_CONFIG_SOURCE = '''
import os
import sqlite3

from pherix.core.adapters.sql import SQLiteAdapter
from pherix.core.audit import AuditJournal
from pherix.core.policy import Policy
from pherix.core.tools import tool
from pherix.frontends.proxy import PherixGateway

DB_ENV = "PHERIX_TEST_DB"


@tool(resource="sql")
def insert_user(conn, name):
    """Insert a user row by name."""
    conn.execute("INSERT INTO users (name) VALUES (?)", (name,))
    return name


def build_gateway() -> PherixGateway:
    path = os.environ[DB_ENV]
    conn = sqlite3.connect(path, isolation_level=None)
    audit = AuditJournal(path + ".audit")
    return PherixGateway(
        adapters={"sql": SQLiteAdapter(conn)},
        policies={
            "claude-code": Policy.allow_all(),
            "blocked-bot": Policy(deny={"insert_user"}),
        },
        default_policy=Policy(allow=set()),
        audit=audit,
    )
'''


def _seed_db() -> str:
    """Create a throwaway on-disk SQLite file with the users schema; return path."""
    fd, path = tempfile.mkstemp(suffix=".db", prefix="pherix_live_")
    os.close(fd)
    conn = sqlite3.connect(path, isolation_level=None)
    conn.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT)")
    conn.close()
    return path


def _names(path: str) -> list[str]:
    conn = sqlite3.connect(path, isolation_level=None)
    try:
        return [r[0] for r in conn.execute("SELECT name FROM users ORDER BY id")]
    finally:
        conn.close()


def _write_config(tmp_dir: Path) -> Path:
    cfg = tmp_dir / "live_gateway_config.py"
    cfg.write_text(_CONFIG_SOURCE)
    return cfg


class _LiveClient:
    """Drives a real child process over JSON-RPC, with timeouts on every read.

    A reader thread drains stdout into a queue so a single hung response cannot
    block the test forever — ``_recv`` pops with a timeout and fails loudly on
    starvation. ``send`` writes one compact JSON line and flushes.
    """

    def __init__(self, proc: subprocess.Popen):
        self._proc = proc
        self._next_id = 0
        self._q: "queue.Queue[str]" = queue.Queue()
        self._reader = threading.Thread(target=self._drain, daemon=True)
        self._reader.start()

    def _drain(self) -> None:
        assert self._proc.stdout is not None
        for line in self._proc.stdout:
            self._q.put(line)

    def _recv(self, timeout: float = 10.0) -> dict:
        try:
            line = self._q.get(timeout=timeout)
        except queue.Empty:
            raise AssertionError(
                "no response from gateway subprocess within timeout — "
                "the child may have hung or crashed; "
                f"returncode={self._proc.poll()!r}"
            )
        return json.loads(line)

    def _send(self, method: str, params: dict) -> dict:
        assert self._proc.stdin is not None
        self._next_id += 1
        request = {
            "jsonrpc": "2.0",
            "id": self._next_id,
            "method": method,
            "params": params,
        }
        self._proc.stdin.write(json.dumps(request, separators=(",", ":")) + "\n")
        self._proc.stdin.flush()
        resp = self._recv()
        assert resp.get("id") == self._next_id, (
            f"id mismatch: sent {self._next_id}, got {resp.get('id')!r}"
        )
        return resp

    def _notify(self, method: str, params: dict) -> None:
        assert self._proc.stdin is not None
        request = {"jsonrpc": "2.0", "method": method, "params": params}
        self._proc.stdin.write(json.dumps(request, separators=(",", ":")) + "\n")
        self._proc.stdin.flush()

    # -- the same surface InProcessMCPClient exposes -----------------------

    def initialize(self, identity: str | None = None) -> dict:
        params: dict = {"protocolVersion": "2025-06-18"}
        if identity is not None:
            params["clientInfo"] = {"name": identity, "version": "0.0.0"}
        resp = self._send("initialize", params)
        self._notify("notifications/initialized", {})
        return resp

    def list_tools(self) -> dict:
        return self._send("tools/list", {})

    def call_tool(
        self, name: str, arguments: dict | None = None, *, dry_run: bool = False
    ) -> dict:
        params: dict = {"name": name, "arguments": arguments or {}}
        if dry_run:
            params["_pherix_dry_run"] = True
        return self._send("tools/call", params)


def _structured(resp: dict) -> dict:
    """The MCP structuredContent payload of a successful tools/call envelope."""
    return resp["result"]["structuredContent"]


def _is_tool_error(resp: dict) -> bool:
    return bool(resp["result"]["isError"])


def _build_in_process_gateway(db_path: str) -> tuple[PherixGateway, AuditJournal]:
    """Build a gateway identical to the child's, but in-process.

    Registers the SAME tool inside the function (never at module top level — the
    autouse fixture clears REGISTRY around each test). Mirrors the child config's
    adapters, policies and default exactly so the parity comparison is fair.
    """

    @tool(resource="sql")
    def insert_user(conn, name):
        """Insert a user row by name."""
        conn.execute("INSERT INTO users (name) VALUES (?)", (name,))
        return name

    conn = sqlite3.connect(db_path, isolation_level=None)
    audit = AuditJournal.in_memory()
    gw = PherixGateway(
        adapters={"sql": SQLiteAdapter(conn)},
        policies={
            "claude-code": Policy.allow_all(),
            "blocked-bot": Policy(deny={"insert_user"}),
        },
        default_policy=Policy(allow=set()),
        audit=audit,
    )
    return gw, audit


def test_subprocess_parity_with_in_process_stub():
    """Spawn the real child, run the subset, assert parity with the stub."""
    # Two separate DBs so the in-process commit and the child commit don't
    # interfere — parity is on the *shape* of outcomes, not a shared row set.
    child_db = _seed_db()
    inproc_db = _seed_db()
    tmp_dir = Path(tempfile.mkdtemp(prefix="pherix_cfg_"))
    cfg_path = _write_config(tmp_dir)

    # -- in-process reference run (the stub) -------------------------------
    gw, _audit = _build_in_process_gateway(inproc_db)
    stub = InProcessMCPClient(gw)
    stub.initialize("claude-code")
    ref_tools = sorted(t["name"] for t in stub.tool_descriptors())
    ref_commit = stub.structured_of(stub.call_tool("insert_user", {"name": "alice"}))
    # A denied call needs the deny-listed identity — a fresh stub session.
    denied_stub = InProcessMCPClient(gw)
    denied_stub.initialize("blocked-bot")
    ref_denied = denied_stub.structured_of(
        denied_stub.call_tool("insert_user", {"name": "nope"})
    )
    ref_dry = stub.structured_of(
        stub.call_tool("insert_user", {"name": "ghost"}, dry_run=True)
    )

    # -- live subprocess run -----------------------------------------------
    env = dict(os.environ)
    env[_DB_ENV] = child_db
    # PYTHONPATH must include both the repo (for `pherix`) and the cfg dir if we
    # passed a bare module name; here we pass the file path, so only the cfg dir
    # is strictly needed — but keeping the repo root explicit is harmless and
    # makes the spawn order-independent.
    repo_root = str(Path(__file__).resolve().parents[1])
    env["PYTHONPATH"] = os.pathsep.join(
        [repo_root, str(tmp_dir), env.get("PYTHONPATH", "")]
    )
    proc = subprocess.Popen(
        [sys.executable, "-m", "pherix.frontends.proxy", str(cfg_path)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
        cwd=repo_root,
    )
    try:
        live = _LiveClient(proc)

        init = live.initialize("claude-code")
        assert init["result"]["serverInfo"]["name"] == "pherix-gateway"

        live_tools = sorted(t["name"] for t in live.list_tools()["result"]["tools"])
        # PARITY 1 — identical tool list across the process boundary.
        assert live_tools == ref_tools == ["insert_user"]

        live_commit = _structured(live.call_tool("insert_user", {"name": "alice"}))
        # PARITY 2 — committed outcome matches the stub.
        assert live_commit["committed"] is True
        assert ref_commit["committed"] is True
        assert live_commit["result"] == ref_commit["result"] == "alice"
        # The child's commit actually landed on disk (real boundary, real DB).
        assert _names(child_db) == ["alice"]

        # A denied call: re-init as the deny-listed identity over the SAME pipe.
        # (A second initialize on one session is allowed by the handler; it just
        # re-records identity, which is what selects the deny policy.)
        live.initialize("blocked-bot")
        live_denied = live.call_tool("insert_user", {"name": "nope"})
        # PARITY 3 — a policy denial is a tool-level refusal (isError content),
        # NOT a JSON-RPC error, on both sides.
        assert _is_tool_error(live_denied) is True
        assert _structured(live_denied)["code"] == POLICY_VIOLATION
        assert ref_denied["code"] == POLICY_VIOLATION
        # Nothing new committed for the denied identity.
        assert _names(child_db) == ["alice"]

        # Back to the allowed identity for the dry-run.
        live.initialize("claude-code")
        live_dry = _structured(
            live.call_tool("insert_user", {"name": "ghost"}, dry_run=True)
        )
        # PARITY 4 — a dry-run reports clean and does NOT persist, on both sides.
        assert live_dry["dry_run"] is True
        assert ref_dry["dry_run"] is True
        assert live_dry["dry_run_result"]["is_clean"] is True
        assert ref_dry["dry_run_result"]["is_clean"] is True
        live_dry_tools = [e["tool"] for e in live_dry["dry_run_result"]["journal"]]
        assert "insert_user" in live_dry_tools
        # Dry-run left the world untouched — only 'alice' from the commit above.
        assert _names(child_db) == ["alice"]

        # -- clean EOF shutdown --------------------------------------------
        assert proc.stdin is not None
        proc.stdin.close()  # EOF: serve_stdio's loop ends, the child exits 0.
        returncode = proc.wait(timeout=10)
        assert returncode == 0, (
            f"child exited {returncode}, stderr: {proc.stderr.read()!r}"
        )
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)
        for db in (child_db, inproc_db):
            for suffix in ("", "-wal", "-shm", "-journal", ".audit"):
                try:
                    os.unlink(db + suffix)
                except FileNotFoundError:
                    pass


def test_subprocess_bad_config_exits_nonzero():
    """A config with no build_gateway() factory fails fast, before serving."""
    tmp_dir = Path(tempfile.mkdtemp(prefix="pherix_badcfg_"))
    bad = tmp_dir / "bad_config.py"
    bad.write_text("X = 1  # no build_gateway() here\n")
    repo_root = str(Path(__file__).resolve().parents[1])
    proc = subprocess.run(
        [sys.executable, "-m", "pherix.frontends.proxy", str(bad)],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        cwd=repo_root,
        timeout=15,
    )
    assert proc.returncode == 2
    assert "build_gateway" in proc.stderr


def test_subprocess_clean_eof_with_no_requests():
    """Closing stdin immediately (no requests) is a clean exit-0 shutdown."""
    db = _seed_db()
    tmp_dir = Path(tempfile.mkdtemp(prefix="pherix_eof_"))
    cfg_path = _write_config(tmp_dir)
    env = dict(os.environ)
    env[_DB_ENV] = db
    repo_root = str(Path(__file__).resolve().parents[1])
    env["PYTHONPATH"] = os.pathsep.join([repo_root, env.get("PYTHONPATH", "")])
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "pherix.frontends.proxy", str(cfg_path)],
            stdin=subprocess.DEVNULL,  # immediate EOF
            capture_output=True,
            text=True,
            cwd=repo_root,
            env=env,
            timeout=15,
        )
        assert proc.returncode == 0, f"stderr: {proc.stderr!r}"
    finally:
        for suffix in ("", "-wal", "-shm", "-journal", ".audit"):
            try:
                os.unlink(db + suffix)
            except FileNotFoundError:
                pass
