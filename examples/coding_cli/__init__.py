"""Pherix in front of a real coding CLI — the interception flagship.

This package is the *MCP-gateway* axis of coding-agent governance: a Pherix
gateway that exposes governed file / git / shell tools over MCP, so an
MCP-capable coding agent (Aider, a minimal MCP client, or a CLI whose built-ins
are disabled) does all its repo work *through* Pherix — every call a journalled
effect, reversible ones snapshot/rollback, irreversible ones (``git push``,
shell) gated, the obviously dangerous ones policy-denied.

It is the sibling of, not a duplicate of, ``examples/dogfood/coding`` — that
stream governs a CLI's *built-in* Edit/Bash via a PATH/filesystem sandbox
(because MCP cannot intercept built-ins); this stream governs the tools a CLI
calls *through* MCP. Both are real interception surfaces; ``FINDINGS.md`` records
which CLIs fit which.
"""

from __future__ import annotations

# The handshake identities the gateway grants the coding policy to. A client
# announcing one of these at ``initialize`` runs under :func:`coding_cli_policy`;
# anything else falls to the deny-all floor (an unrecognised agent is never
# unpoliced).
CODING_CLI_IDENTITIES = ("aider", "claude-code", "pherix-coding-cli")

__all__ = ["CODING_CLI_IDENTITIES"]
