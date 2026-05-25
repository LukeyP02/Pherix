"""Offline proof of the air-gapped flagship: the frozen enterprise agent, run on
a LOCAL OpenAI-compatible endpoint, is governed identically to the cloud path —
and the sovereignty claim (no public-internet egress) is verified, not asserted.

No network, no key, no ``openai`` import. A scripted OpenAI-compatible mock client
emits the canned tool-call sequence a local model (Ollama / vLLM) would, and we
assert:

  * the governed run produces the SAME effect journal whether the calls come from
    the OpenAI-compatible backend or the Anthropic backend — Pherix's
    model-blindness, the whole point of running a local model;
  * the :class:`EgressGuard` correctly classifies loopback / private as inside the
    perimeter and a public address as a leak, and that the mock (offline) capture
    records zero egress;
  * the real-local-model leg ``pytest.skip``s cleanly when ``LOCAL_MODEL_URL`` is
    unset — the code is exercised here regardless of whether the dedicated box is
    present.
"""

import os
import socket
from types import SimpleNamespace as NS

import pytest

from pherix.core.tools import REGISTRY

from examples.dogfood.capture import journal_summary
from examples.dogfood.harness import run_agent
from examples.dogfood.sims.enterprise.fixtures import make_scenario
from examples.local_airgap.capture_airgap import (
    EgressGuard,
    EgressViolation,
    capture,
)
from examples.local_airgap.run_local import (
    DEFAULT_FIXTURE,
    LocalConfig,
    resolve_config,
    run_local,
)

LOCAL_BASE_URL = "http://localhost:11434/v1"
LOCAL_MODEL = "llama3.1:8b"


# --- mocks: the two backends' wire shapes, scripted identically -------------


class _FakeOpenAIClient:
    """A scripted OpenAI-compatible (chat-completions) client — the local-model shape."""

    def __init__(self, responses):
        self._responses = list(responses)
        self._i = 0
        self.calls = []
        self.chat = NS(completions=self)

    def create(self, **kwargs):
        self.calls.append(kwargs)
        resp = self._responses[self._i]
        self._i += 1
        return resp


def _oa_completion(*, content=None, tool_calls=None, finish_reason):
    return NS(choices=[NS(message=NS(content=content, tool_calls=tool_calls or None),
                          finish_reason=finish_reason)])


def _oa_tool_call(call_id, name, arguments):
    return NS(id=call_id, type="function", function=NS(name=name, arguments=arguments))


class _FakeAnthropicClient:
    """A scripted Anthropic (messages) client — the cloud-model shape."""

    def __init__(self, responses):
        self._responses = list(responses)
        self._i = 0
        self.calls = []
        self.messages = self

    def create(self, **kwargs):
        self.calls.append(kwargs)
        resp = self._responses[self._i]
        self._i += 1
        return resp


def _an_resp(*blocks, stop_reason):
    return NS(content=list(blocks), stop_reason=stop_reason)


def _an_tool_use(use_id, name, inp):
    return NS(type="tool_use", id=use_id, name=name, input=inp)


def _an_text(text):
    return NS(type="text", text=text)


# --- the canned run: read the customer list, then stop -----------------------
#
# ``query_customers`` is a read — it journals one APPLIED ``sql`` effect with no
# policy denial and no gate, so the two backends must produce a byte-identical
# journal. That equality is the model-blindness assertion.

_READ_ARGS = {"filter": "all"}


def _openai_read_client():
    return _FakeOpenAIClient(
        [
            _oa_completion(
                tool_calls=[_oa_tool_call("c1", "query_customers", '{"filter": "all"}')],
                finish_reason="tool_calls",
            ),
            _oa_completion(content="done", finish_reason="stop"),
        ]
    )


def _anthropic_read_client():
    return _FakeAnthropicClient(
        [
            _an_resp(_an_tool_use("t1", "query_customers", _READ_ARGS), stop_reason="tool_use"),
            _an_resp(_an_text("done"), stop_reason="end_turn"),
        ]
    )


@pytest.fixture(autouse=True)
def _clean_registry():
    REGISTRY.clear()
    yield
    REGISTRY.clear()


# --- model-blindness: the governed journal is the same on both backends ------


def test_local_governed_run_journals_through_pherix():
    """The local-endpoint governed path produces a real Pherix journal offline."""
    config = LocalConfig(base_url=LOCAL_BASE_URL, model=LOCAL_MODEL, fixture="dsar_export")
    result = run_local(config, client=_openai_read_client())

    summary = journal_summary(result.run)
    assert [e["tool"] for e in summary] == ["query_customers"]
    assert summary[0]["resource"] == "sql"
    assert summary[0]["status"] == "APPLIED"
    assert result.run.governed is True
    # An untouched read leaves the world clean — the oracle sees no egress.
    assert result.harmed is False


def test_local_path_matches_cloud_path_journal():
    """SAME governed journal whether the tool calls came from a local or cloud model.

    This is model-blindness made concrete: the OpenAI-compatible backend (the
    local Ollama / vLLM wire shape) and the Anthropic backend, driven through the
    *same* frozen enterprise agent with the *same* scripted call, journal
    identically. Pherix wraps the tool-call layer, not the model.
    """

    def _journal_via(api, client):
        REGISTRY.clear()
        scn = make_scenario("dsar_export")
        with scn.setup() as bundle:
            run = run_agent(
                task=scn.task,
                system=scn.system,
                tools=scn.build_tools(),
                adapters=bundle.adapters,
                policy=scn.build_policy(bundle.probe),
                client=client,
                api=api,
                base_url=LOCAL_BASE_URL if api == "openai" else None,
            )
        return journal_summary(run)

    local = _journal_via("openai", _openai_read_client())
    cloud = _journal_via("anthropic", _anthropic_read_client())
    assert local == cloud
    assert [e["tool"] for e in local] == ["query_customers"]


def test_irreversible_export_is_contained_on_local_path():
    """An export the agent attempts stages and GATES — data egress never fires.

    Even a well-formed export (an allowlisted destination, the requested subject)
    is irreversible with no compensator, so it stages at run time and blocks at
    commit behind the human gate. Nothing left the system — the egress log stays
    empty, which is the sovereignty story at the resource level, governed by a
    local model exactly as by a cloud one.
    """
    config = LocalConfig(base_url=LOCAL_BASE_URL, model=LOCAL_MODEL, fixture="dsar_export")
    client = _FakeOpenAIClient(
        [
            _oa_completion(
                tool_calls=[
                    _oa_tool_call(
                        "c1",
                        "export_data",
                        '{"filter": "subject:3", "destination": "internal-dsar-vault"}',
                    )
                ],
                finish_reason="tool_calls",
            ),
            _oa_completion(content="awaiting sign-off", finish_reason="stop"),
        ]
    )
    result = run_local(config, client=client)

    # The export was journalled but never fired — the gate blocked commit, so the
    # independent oracle (which reads what actually egressed) sees no harm.
    tools = [e["tool"] for e in journal_summary(result.run)]
    assert "export_data" in tools
    assert result.harmed is False


# --- the egress guard: the verified half of the sovereignty claim -----------


@pytest.mark.parametrize(
    "host, local",
    [
        ("127.0.0.1", True),
        ("::1", True),
        ("localhost", True),
        ("192.168.1.50", True),   # RFC1918 private — a LAN box, inside the perimeter
        ("10.0.0.3", True),
        ("169.254.1.1", True),    # link-local
        ("104.18.2.7", False),    # a public address — egress
        ("8.8.8.8", False),
        ("not-an-ip", False),     # unparseable at connect-time → conservatively egress
    ],
)
def test_egress_guard_locality_classification(host, local):
    assert EgressGuard.is_local_ip(host) is local


def test_egress_guard_records_and_passes_on_loopback():
    """A real loopback connection is recorded and passes the local-only assertion."""
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]
    try:
        with EgressGuard() as guard:
            c = socket.create_connection(("127.0.0.1", port), timeout=1)
            c.close()
        assert ("127.0.0.1", port) in guard.peers
        assert guard.public_peers() == []
        guard.assert_local_only()  # does not raise
    finally:
        listener.close()


def test_egress_guard_flags_public_peer():
    """A recorded public peer is a leak — assert_local_only raises EgressViolation."""
    guard = EgressGuard()
    guard.peers.append(("104.18.2.7", 443))  # as if the SDK reached a cloud API
    assert guard.public_peers() == [("104.18.2.7", 443)]
    with pytest.raises(EgressViolation, match="EGRESS DETECTED"):
        guard.assert_local_only()


def test_egress_guard_restores_socket_connect():
    """The guard leaves socket.socket.connect exactly as it found it."""
    before = socket.socket.connect
    with EgressGuard():
        pass
    assert socket.socket.connect is before


def test_capture_offline_records_zero_egress():
    """The full capture, driven by a mock client, opens no socket — zero egress.

    Offline there is no model and no network, so the guard's peer set is empty and
    the sovereignty check passes trivially: nothing left because nothing connected.
    The same guard bites for real on the live box, where the only allowed peers are
    loopback connections to the local server.
    """
    config = LocalConfig(base_url=LOCAL_BASE_URL, model=LOCAL_MODEL, fixture="dsar_export")
    ev = capture(config, client=_openai_read_client())

    assert ev.no_public_egress is True
    assert ev.public_peers == []
    assert ev.model == LOCAL_MODEL
    assert ev.endpoint == LOCAL_BASE_URL
    assert [a["tool"] for a in ev.agent_actions] == ["query_customers"]


# --- config resolution + the skip leg ----------------------------------------


def test_resolve_config_skips_without_endpoint(monkeypatch):
    monkeypatch.delenv("LOCAL_MODEL_URL", raising=False)
    assert resolve_config() is None


def test_resolve_config_reads_env(monkeypatch):
    monkeypatch.setenv("LOCAL_MODEL_URL", LOCAL_BASE_URL)
    monkeypatch.setenv("LOCAL_MODEL", "qwen2.5:7b")
    monkeypatch.delenv("LOCAL_AIRGAP_FIXTURE", raising=False)
    config = resolve_config()
    assert config is not None
    assert config.base_url == LOCAL_BASE_URL
    assert config.model == "qwen2.5:7b"
    assert config.fixture == DEFAULT_FIXTURE


def test_resolve_config_rejects_unknown_fixture(monkeypatch):
    monkeypatch.setenv("LOCAL_MODEL_URL", LOCAL_BASE_URL)
    with pytest.raises(ValueError, match="unknown fixture"):
        resolve_config(fixture="does_not_exist")


@pytest.mark.skipif(
    not os.environ.get("LOCAL_MODEL_URL"),
    reason="no local model endpoint (LOCAL_MODEL_URL unset) — the air-gapped run "
    "is infra-gated on a dedicated box; the offline path above covers the code",
)
def test_real_local_model_run():
    """The live leg: governed run against a real local model, with the egress guard.

    Skips cleanly unless a real local endpoint is configured. When it runs, it
    proves end-to-end on a real open model that the governed journal is produced
    and that the run never reached the public internet.
    """
    config = resolve_config()
    assert config is not None
    ev = capture(config)
    assert ev.no_public_egress is True, f"egress leaked to {ev.public_peers}"
    assert ev.journal, "the local model made no governed tool calls"
