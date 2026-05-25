"""Run the frozen enterprise agent on a LOCAL open model, governed.

    LOCAL_MODEL_URL=http://localhost:11434/v1 \
    LOCAL_MODEL=llama3.1:8b \
    python -m examples.local_airgap.run_local            # default fixture: dsar_export
    python -m examples.local_airgap.run_local ledger_recon

This is the cloud-Claude run from the enterprise sim with one bit flipped — the
backend seam points at a local OpenAI-compatible server (Ollama / vLLM /
LM Studio) instead of ``api.anthropic.com``. Everything Pherix does behind the
seam is byte-identical: same frozen agent (system prompt, toolset, enterprise
policy), same ``run_agent`` governed path, same effect journal. That identity is
the model-blindness proof — and because the model is now *inside the perimeter*,
so is the entire run.

Configuration is read from the environment so the live leg is infra-gated, never
code-gated:

  * ``LOCAL_MODEL_URL`` — the local endpoint (e.g. ``http://localhost:11434/v1``).
    **Unset → the run SKIPS** with a clear message (the dedicated ~16GB box that
    hosts the local model is not always present; the code is still exercised
    offline by ``tests/test_local_airgap.py`` with a mock client).
  * ``LOCAL_MODEL`` — the model id (default ``llama3.1:8b``, a common Ollama tag).
  * ``LOCAL_AIRGAP_FIXTURE`` — which enterprise situation to run (default
    ``dsar_export`` — the data-egress story is the sharpest sovereignty demo).

The agent is reused unchanged from :mod:`examples.dogfood.sims.enterprise`; this
module never defines tools, prompts, or policy of its own.
"""

from __future__ import annotations

import os
import socket
import sys
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

from pherix.core.audit import AuditJournal
from pherix.core.tools import REGISTRY

from examples.dogfood.capture import journal_summary, verdict_for
from examples.dogfood.harness import AgentRun, run_agent
from examples.dogfood.sims.enterprise.fixtures import FIXTURE_NAMES, make_scenario

# The data-egress fixture is the most evocative perimeter story: a data-subject
# access request, where the agent can export regulated PII — exactly the action
# a regulated buyer most wants governed, run on a model that itself never leaves
# the building.
DEFAULT_FIXTURE = "dsar_export"
DEFAULT_MODEL = "llama3.1:8b"


@dataclass
class LocalConfig:
    """The resolved local-endpoint configuration for one air-gapped run."""

    base_url: str
    model: str
    fixture: str


@dataclass
class LocalRunResult:
    """The product of one air-gapped run — the governed run plus the oracle's read.

    ``run`` is the :class:`~examples.dogfood.harness.AgentRun` (its journal is the
    Pherix side of the story). ``harmed`` / ``proof`` are the enterprise fixture's
    *independent* harm oracle, read off the live resource after the run — included
    for completeness, though the flagship's claim is the governed journal + the
    egress containment, not the harm rate (that is the sim suite's job).
    """

    config: LocalConfig
    run: AgentRun
    harmed: bool
    proof: dict


def resolve_config(
    *,
    fixture: str | None = None,
    base_url: str | None = None,
    model: str | None = None,
) -> LocalConfig | None:
    """Resolve the run config from args + environment, or ``None`` to skip.

    ``None`` means *no local endpoint is configured* (``LOCAL_MODEL_URL`` unset and
    no ``base_url`` passed) — the caller should skip cleanly, never fail. An
    unknown fixture name raises, because that is a typo the operator wants told.
    """
    base_url = base_url or os.environ.get("LOCAL_MODEL_URL")
    if not base_url:
        return None
    fixture = fixture or os.environ.get("LOCAL_AIRGAP_FIXTURE", DEFAULT_FIXTURE)
    if fixture not in FIXTURE_NAMES:
        raise ValueError(
            f"unknown fixture {fixture!r}; choose one of {FIXTURE_NAMES}"
        )
    model = model or os.environ.get("LOCAL_MODEL", DEFAULT_MODEL)
    return LocalConfig(base_url=base_url, model=model, fixture=fixture)


def _host_port(base_url: str) -> tuple[str, int]:
    """Pull (host, port) out of an OpenAI-compatible base URL for a probe.

    Ports default per scheme (https → 443, http → 80) when the URL omits one, so
    a bare ``https://host/v1`` still probes the right socket.
    """
    parsed = urlparse(base_url)
    host = parsed.hostname or "localhost"
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    return host, port


def endpoint_reachable(base_url: str, *, timeout: float = 2.0) -> bool:
    """Is the local endpoint accepting connections? A fast TCP probe, no SDK.

    We open and immediately close a socket to ``(host, port)``. This is the only
    thing that distinguishes "skip — no box" from "run". It hits the *local*
    endpoint only, so it stays inside the perimeter (the egress guard in
    :mod:`capture_airgap` sees it as a loopback/private connection, not egress).
    """
    host, port = _host_port(base_url)
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def run_local(
    config: LocalConfig,
    *,
    client: Any = None,
    audit: AuditJournal | None = None,
) -> LocalRunResult:
    """Run the frozen enterprise agent against ``config``'s local endpoint, governed.

    This mirrors the governed arm of the sim runner exactly — ``REGISTRY.clear()``,
    a fresh ``setup()`` bundle, ``build_tools()`` + ``build_policy(probe)``, then
    ``run_agent`` with ``api="openai"`` and the local ``base_url`` — so the path a
    local model takes through Pherix is the same one cloud Claude takes. The harm
    oracle is read while the bundle is still live (the probe reads the real DB).

    ``client`` is an OpenAI-compatible client; left ``None`` the harness builds the
    real ``openai`` SDK pointed at ``base_url``. Tests inject a mock so the whole
    thing runs offline with no model and no network.
    """
    REGISTRY.clear()
    scn = make_scenario(config.fixture)
    audit = audit or AuditJournal.in_memory()
    with scn.setup() as bundle:
        tools = scn.build_tools()
        run = run_agent(
            task=scn.task,
            system=scn.system,
            tools=tools,
            adapters=bundle.adapters,
            policy=scn.build_policy(bundle.probe),
            isolation=scn.isolation,
            commit_refusals=scn.commit_refusals,
            client=client,
            audit=audit,
            client_id=f"local-airgap-{config.fixture}",
            api="openai",
            base_url=config.base_url,
            model=config.model,
        )
        harmed, proof = scn.harm_oracle(bundle.probe)
    return LocalRunResult(config=config, run=run, harmed=harmed, proof=proof)


# --- CLI ---------------------------------------------------------------------


_SKIP_MESSAGE = (
    "SKIPPED — no local model endpoint.\n"
    "  Set LOCAL_MODEL_URL to an OpenAI-compatible local server and re-run, e.g.\n"
    "    LOCAL_MODEL_URL=http://localhost:11434/v1 LOCAL_MODEL=llama3.1:8b \\\n"
    "      python -m examples.local_airgap.run_local\n"
    "  (Ollama: `ollama serve` + `ollama pull llama3.1:8b`. vLLM: serve on :8000.)\n"
    "  The code is exercised offline regardless by tests/test_local_airgap.py."
)


def render(result: LocalRunResult) -> str:
    """The operator-facing summary of one air-gapped run."""
    cfg = result.run
    lines = [
        "AIR-GAPPED GOVERNED RUN",
        f"  endpoint : {result.config.base_url}   (local — data stays inside)",
        f"  model    : {result.config.model}   (open weights, runs offline)",
        f"  fixture  : {result.config.fixture}",
        f"  verdict  : {verdict_for(cfg)}   (turns={cfg.turns}, stop={cfg.stop_reason})",
        f"  harm     : {'HARMED' if result.harmed else 'no harm'}   proof={result.proof}",
        "  journal (the Pherix side — every governed tool call):",
    ]
    summary = journal_summary(cfg)
    if not summary:
        lines.append("    (empty — the model made no tool calls)")
    for e in summary:
        lines.append(
            f"    [{e['index']}] {e['tool']} on {e['resource']} "
            f"-> {e['status']}  (reversible={e['reversible']})  {e['args']}"
        )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    fixture = argv[0] if argv else None

    config = resolve_config(fixture=fixture)
    if config is None:
        print(_SKIP_MESSAGE)
        return 0

    if not endpoint_reachable(config.base_url):
        print(
            f"SKIPPED — {config.base_url} is configured but not reachable.\n"
            "  Start the local model server (e.g. `ollama serve`) and re-run."
        )
        return 0

    result = run_local(config)
    print(render(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
