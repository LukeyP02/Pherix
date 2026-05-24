"""Rate-limit / overload backoff for the harness model call (offline, deterministic).

The last real batch crashed when a single 429 from the provider killed the whole
run. The harness now wraps each model request in exponential-backoff retry. These
tests prove it backend-agnostically with mocks — no SDK import, no network, no
key, and ``time.sleep`` patched so the backoff path is instant:

  * a transient overload (by exception *type name*, and by HTTP ``status_code``)
    is retried and the run then succeeds;
  * a non-rate-limit error is NOT retried — a real bug must surface immediately;
  * an overload that outlives the retry budget re-raises rather than spinning.
"""

from types import SimpleNamespace as NS

import pytest

from examples.dogfood import harness
from examples.dogfood.harness import _create_with_backoff, _is_rate_limit, run_agent


# A stand-in for the SDKs' ``RateLimitError`` — matched by type name, not import.
class RateLimitError(Exception):
    pass


def _end(text="done"):
    return NS(content=[NS(type="text", text=text)], stop_reason="end_turn")


class _FlakyClient:
    """Raises ``exc`` for the first ``fail_times`` calls, then returns ``resp``."""

    def __init__(self, exc, fail_times, resp):
        self._exc = exc
        self._fail_times = fail_times
        self._resp = resp
        self.calls = 0
        self.messages = self

    def create(self, **kwargs):
        self.calls += 1
        if self.calls <= self._fail_times:
            raise self._exc
        return self._resp


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    """Make the backoff instant + record the sleeps the retry loop requested."""
    slept: list[float] = []
    monkeypatch.setattr(harness.time, "sleep", lambda s: slept.append(s))
    return slept


def test_is_rate_limit_matches_type_name_and_status():
    assert _is_rate_limit(RateLimitError("slow down"))
    assert _is_rate_limit(type("OverloadedError", (Exception,), {})())
    assert _is_rate_limit(NS(status_code=429))  # any object carrying 429
    assert _is_rate_limit(NS(status_code=529))
    assert not _is_rate_limit(ValueError("a real bug"))
    assert not _is_rate_limit(NS(status_code=400))


def test_backoff_retries_then_succeeds(_no_sleep):
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] <= 2:
            raise RateLimitError("429")
        return "ok"

    assert _create_with_backoff(flaky, base_delay=0.5) == "ok"
    assert calls["n"] == 3
    # Two retries -> two backoff sleeps, exponential: 0.5, 1.0.
    assert _no_sleep == [0.5, 1.0]


def test_backoff_does_not_retry_other_errors(_no_sleep):
    def boom():
        raise ValueError("a real bug, not a rate limit")

    with pytest.raises(ValueError):
        _create_with_backoff(boom)
    assert _no_sleep == []  # never slept — surfaced immediately


def test_backoff_reraises_after_budget(_no_sleep):
    def always():
        raise RateLimitError("429 forever")

    with pytest.raises(RateLimitError):
        _create_with_backoff(always, max_retries=3, base_delay=0.1)
    assert len(_no_sleep) == 3  # slept before each of the 3 retries, then gave up


def test_run_agent_survives_a_transient_overload(_no_sleep):
    """End-to-end: a 429 mid-run is retried and the agent loop completes."""
    client = _FlakyClient(RateLimitError("429"), fail_times=1, resp=_end())
    run = run_agent(
        task="do nothing",
        system="you are idle",
        tools=[],
        adapters={},
        client=client,
    )
    assert client.calls == 2  # one failure, one success
    assert run.stop_reason == "end_turn"
    assert _no_sleep == [1.0]  # one retry at the default base delay
