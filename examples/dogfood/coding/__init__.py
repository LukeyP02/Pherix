"""Coding dogfood — the agent-agnostic sandbox.

A coding CLI (Claude Code / Cursor / Gemini CLI / an open-source agent) uses
*built-in* Edit/Write/Bash tools that MCP cannot intercept. So this dogfood does
NOT build a custom coding agent. It builds a **sandbox**: an environment an
out-of-box CLI runs *inside*, where its filesystem and shell calls are routed
through Pherix — journalled, policy-gated, audited. Build once, govern
everything; it works for any CLI, which a Claude-Code-only hook would not.

See :mod:`examples.dogfood.coding.sandbox` for the mechanism and ``README.md``
for the philosophy + the manual capstone protocol.
"""

from examples.dogfood.coding.sandbox import (
    RouteOutcome,
    Sandbox,
    coding_policy,
    sandbox_env,
    write_shims,
)

__all__ = [
    "RouteOutcome",
    "Sandbox",
    "coding_policy",
    "sandbox_env",
    "write_shims",
]
