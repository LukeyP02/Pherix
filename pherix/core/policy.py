"""Minimal capability policy — stage-time allow/deny list.

Slice 1 evaluates policy once, at stage-time (D6): when the runtime intercepts a
tool call, before it touches any resource. The real engine — capability grants,
spend caps, content-aware rules, commit-time re-evaluation — is Slice 6.
"""

from __future__ import annotations

from dataclasses import dataclass, field


class PolicyViolation(Exception):
    """Raised at stage-time when a tool call is denied by policy."""

    def __init__(self, tool: str, reason: str):
        self.tool = tool
        self.reason = reason
        super().__init__(f"policy denied tool {tool!r}: {reason}")


@dataclass
class Policy:
    """An allow-list and/or deny-list of tool names.

    - ``allow is None``  -> every tool is permitted unless deny-listed.
    - ``allow`` is a set -> only those tools are permitted (deny still applies).
    Deny always wins over allow.
    """

    allow: set[str] | None = None
    deny: set[str] = field(default_factory=set)

    def check(self, tool: str) -> None:
        """Raise :class:`PolicyViolation` if ``tool`` is not permitted."""
        if tool in self.deny:
            raise PolicyViolation(tool, "tool is deny-listed")
        if self.allow is not None and tool not in self.allow:
            raise PolicyViolation(tool, "tool is not in the allow-list")

    def permits(self, tool: str) -> bool:
        try:
            self.check(tool)
            return True
        except PolicyViolation:
            return False

    @classmethod
    def allow_all(cls) -> "Policy":
        return cls()
