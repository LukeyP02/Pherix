# Pointing a real MCP client at the Pherix gateway

This guide is for an operator who has Pherix-wrapped tools and wants a real MCP
client (Claude Code, Cursor, an SDK) to call them — with every call wrapped in a
Pherix transaction: snapshotted, policy-checked, audited, and rolled back on
failure. The agent needs zero Pherix code; it just speaks MCP.

## The mental model

A real MCP client launches an MCP *server* as a child process and talks
newline-delimited JSON-RPC 2.0 over its stdin/stdout. The Pherix gateway is that
server. It is a thin dispatcher, not a new engine: every tool call it receives is
forwarded into `agent_txn` (or `dry_run`) on the same core the library uses. The
client's handshake identity selects which `Policy` the session runs under.

```
MCP client  ──spawns──▶  python -m pherix.frontends.proxy <config>
   │                                    │
   │  newline-delimited JSON-RPC 2.0    │  PherixGateway → agent_txn → adapters
   └──────────── stdio ─────────────────┘                       → audit + policy
```

## Step 1 — write a `build_gateway()` config

The gateway needs *live Python objects* a static config file cannot carry:
adapters bound to open connections, `Policy` instances (which hold rule
*callables*), and your registered `@tool` functions (the global tool registry is
populated by import side-effect, so the tool modules must be imported in the
gateway's process). So the config is a **Python module exposing a single
zero-argument factory** `build_gateway() -> PherixGateway`.

Importing the config module is what registers your `@tool` functions — so
`tools/list` enumerates exactly the tools that module defined.

```python
# my_gateway_config.py
import sqlite3

from pherix.core.adapters.sql import SQLiteAdapter
from pherix.core.audit import AuditJournal
from pherix.core.policy import Policy
from pherix.core.tools import tool
from pherix.frontends.proxy import PherixGateway


@tool(resource="sql")
def insert_user(conn, name):
    """Insert a user row by name."""
    conn.execute("INSERT INTO users (name) VALUES (?)", (name,))
    return name


def build_gateway() -> PherixGateway:
    # autocommit (isolation_level=None) so the Pherix adapter owns every
    # BEGIN / SAVEPOINT / COMMIT / ROLLBACK.
    conn = sqlite3.connect("app.db", isolation_level=None)
    return PherixGateway(
        adapters={"sql": SQLiteAdapter(conn)},
        # Identity → Policy. The handshake identity (the MCP client's name)
        # selects the session policy.
        policies={"claude-code": Policy.allow_all()},
        # The safety floor: an unrecognised client never runs unpoliced — it
        # runs under default_policy. A tight default (deny-all here) is the
        # secure choice.
        default_policy=Policy(allow=set()),
        # One shared audit journal across every session; the client_id column
        # attributes each effect back to the calling identity.
        audit=AuditJournal("audit.db"),
    )
```

The factory contract:

- it takes **no arguments** and returns a `PherixGateway`;
- it runs **once** at gateway startup, in the gateway process;
- importing its module **registers your `@tool` functions** into the global
  registry — that import is mandatory, which is why the config is Python.

## Step 2 — run the gateway

```bash
# By file path:
python -m pherix.frontends.proxy ./my_gateway_config.py

# By importable module name (if it's on sys.path / an installed package):
python -m pherix.frontends.proxy my_pkg.gateway_config
```

`<config>` is resolved as an **importable dotted module name** first, then as a
**filesystem path to a `.py` file**. A bare name is tried as a module and falls
back to a same-named file on disk; a path-looking string (`./x.py`, `a/b.py`) is
loaded as a file directly.

The process serves MCP over stdio and exits cleanly (code 0) when stdin reaches
EOF — i.e. when the client closes the pipe. A bad config (missing
`build_gateway`, import error, factory raised) exits non-zero with a diagnostic
on **stderr** *before* any JSON-RPC is served, so the launching client sees the
failure immediately rather than a silent dead pipe.

## Step 3 — point Claude Code at it

Claude Code reads MCP server definitions from its settings (`mcpServers`). Add a
stdio server entry pointing at the module:

```json
{
  "mcpServers": {
    "pherix": {
      "command": "python",
      "args": ["-m", "pherix.frontends.proxy", "/abs/path/to/my_gateway_config.py"],
      "env": {
        "PYTHONPATH": "/abs/path/to/your/project"
      }
    }
  }
}
```

Notes:

- Use an **absolute path** to the config file (the client sets the child's cwd,
  which may not be your project root).
- `PYTHONPATH` must include the directory where `pherix` (and your config's
  imports) are importable. If you `pip install -e .` your project, `pherix` is on
  the path already and you can drop the `env` block.
- The server name (`"pherix"` above) is cosmetic. The **handshake identity** the
  client sends — Claude Code sends `clientInfo.name = "claude-code"` — is what
  the gateway maps to a `Policy` via `policies={...}`. To grant Claude Code more
  than the default floor, add a `"claude-code"` entry to `policies`.

The same shape works for any stdio MCP client (Cursor, the MCP Python SDK's
stdio client, a Goose/Cline config) — only the settings file format differs. The
gateway is client-agnostic; it speaks the tool-call subset (`initialize`,
`ping`, `tools/list`, `tools/call`) of plain JSON-RPC 2.0.

## What the client sees

- **`tools/list`** — your registered `@tool` functions, each with the
  injected-handle parameter (e.g. the SQL `conn`) hidden, so the agent only sees
  the call-site args.
- **`tools/call`** — the tool runs inside a Pherix transaction. A clean run
  commits and returns `{"committed": true, "result": ...}`. A policy denial /
  gate block / isolation conflict / raised tool body comes back as a *successful*
  JSON-RPC response with `isError: true` and a machine-readable `pherix_error`
  code — so the agent reads the refusal and adapts, rather than seeing a
  transport fault. Nothing committed in that case.
- **Dry-run** — a client that sets `params["_pherix_dry_run"] = true` on a
  `tools/call` gets a speculative report (`would_have_fired`, policy verdicts,
  per-resource `state_diff`) and the world is left untouched.

## Verifying it works without a client

The live subprocess parity test (`tests/test_live_mcp_subprocess.py`) spawns
`python -m pherix.frontends.proxy <config>` as a real child, speaks real
newline-delimited JSON-RPC over its pipe, and asserts the committed / denied /
dry-run outcomes match the in-process reference. It is fully offline (the gateway
dispatches tools; it never calls an LLM) — run it with:

```bash
python -m pytest -q tests/test_live_mcp_subprocess.py
```

That test doubles as a copy-pasteable example of the JSON-RPC envelopes a client
exchanges with the gateway.
