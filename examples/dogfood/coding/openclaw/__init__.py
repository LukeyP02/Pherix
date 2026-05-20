"""OpenClaw + Pherix: governing a local-first agent on both interception surfaces.

OpenClaw (Peter Steinberger's open-source, local-first Node agent) reaches the
outside world two ways, and Pherix governs each with a different mechanism:

  * **MCP domain tools** — OpenClaw consumes MCP servers from its registry. The
    Pherix gateway *is* an MCP server, so any tool OpenClaw calls through MCP is
    journalled, policy-checked, gated and audited. That is :mod:`gateway_config`
    here + the ``openclaw.json`` registration snippet (Part B1).
  * **Built-in file / bash** — OpenClaw's own ``read``/``write``/``edit`` and
    ``bash`` actions never travel over MCP, so MCP cannot intercept them. Those
    are governed at the *environment* level by the Pherix coding sandbox (the
    CoW filesystem root + the ``git``/``sh`` PATH shims) — that is
    :mod:`launcher` here (Part B2).

Both surfaces drive the *same* Pherix core, and because Pherix never calls the
model, the identical governance holds whether OpenClaw runs cloud Claude or a
local open-source model on Ollama/vLLM. That model-blind + deployment-blind
combination — a local agent, a local model, an air-gapped box, fully governed —
is the configuration no cloud vendor can serve, and the air-gapped capstone
(``docs/operator/airgapped-capstone.md``) is where it is demonstrated.
"""

# The handshake identity OpenClaw's MCP client presents (``clientInfo.name``).
# The gateway maps this string to a Policy; an identity NOT in the policy map
# falls back to the deny-floor. If a given OpenClaw build presents a different
# name, set it here (and in the policy map in ``gateway_config``) — confirm the
# real value from the gateway's audit rows, whose ``client_id`` is this identity.
OPENCLAW_IDENTITY = "openclaw"

__all__ = ["OPENCLAW_IDENTITY"]
