"""PherixGateway — the policy-selecting transaction dispatcher.

The gateway holds three things and nothing else:

- ``adapters`` — the resource-name -> adapter dict, exactly as
  :func:`pherix.core.runtime.agent_txn` takes it. The gateway never inspects
  adapters; it forwards them to the engine.
- ``policies`` — an identity -> :class:`Policy` map. A handshake identity
  string selects which policy a session's transactions run under.
- ``default_policy`` — the policy an *unknown* identity falls back to. This is
  the safety floor: an unrecognised client never runs unpoliced, it runs under
  whatever the operator declared as the default (typically a tight allow-list).
- ``audit`` — an optional shared :class:`AuditJournal`. One journal across every
  session means the audit trail is the single source of truth regardless of
  which MCP client produced an effect; the ``client_id`` column (Stream B)
  attributes each row back to its session identity.

The gateway is deliberately a *selector*, not an engine. Every transactional
guarantee — snapshot/rollback, the gate, isolation, the policy fold — lives in
``pherix.core`` and is reached through ``agent_txn`` / ``dry_run``. If the
gateway ever needed to reimplement any of that, the seam between core and
front-end would have failed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from pherix.core.audit import AuditJournal
from pherix.core.policy import Policy


@dataclass
class PherixGateway:
    """Holds adapters + per-identity policy config; resolves identity -> policy.

    A single gateway instance backs any number of concurrent MCP sessions. It
    carries no per-session state itself — session identity lives on the
    :class:`pherix.frontends.proxy.server.MCPServer` (one server per
    connection), which asks the gateway to resolve its identity into a policy
    at ``initialize`` time.
    """

    adapters: dict[str, Any]
    policies: dict[str, Policy] = field(default_factory=dict)
    default_policy: Policy = field(default_factory=Policy.allow_all)
    audit: AuditJournal | None = None

    def policy_for(self, identity: str | None) -> Policy:
        """Select the policy for a handshake identity.

        An identity present in :attr:`policies` gets its declared policy; any
        other identity (including ``None``, i.e. a client that sent no
        identity at all) falls back to :attr:`default_policy`. The fallback is
        the security-relevant branch: an unknown client must never run more
        permissively than the operator's declared floor.
        """
        if identity is not None and identity in self.policies:
            return self.policies[identity]
        return self.default_policy

    def approve(self, token: str, approver: str | None) -> dict:
        """Record an over-the-wire approval against the shared journal.

        This is the *write* side of the human gate, reached from outside the
        agent's process: a reviewer (or a higher-trust service) hands the proxy
        the opaque ``token`` a gate-blocked commit produced, and the gateway
        flips that journal record to APPROVED — stamping ``approver`` (the
        on-whose-authority principal, the #40 actor model). It writes ONLY the
        approvals table; it never touches a resource, never reimplements the
        gate, and never fires the effect. The agent process's resumed
        ``commit(pending_approval=True)`` reads the APPROVED record and fires.

        Requires a shared :attr:`audit` journal — the whole point is that the
        approver and the agent see the *same* append-only log. A gateway with
        no audit journal cannot carry approvals across the process boundary, so
        this raises rather than silently dropping the approval on the floor.
        """
        if self.audit is None:
            raise RuntimeError(
                "PherixGateway.approve requires a shared audit journal: "
                "over-the-wire approval is a journal write, and with no journal "
                "the approving process and the agent process share nothing. "
                "Construct the gateway with audit=AuditJournal(<shared path>)."
            )
        return self.audit.record_approval(token, approver)
