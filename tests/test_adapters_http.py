"""HTTPAdapter — Slice 3's honest "I cannot undo" adapter."""

from __future__ import annotations

import pytest

from pherix.core.adapters.base import (
    ResourceAdapter,
    SnapshotHandle,
    TransactionalResourceAdapter,
)
from pherix.core.adapters.http import HTTPAdapter, IrreversibleAdapterError
from pherix.core.effects import Effect


def make_effect(**overrides):
    base = dict(
        txn_id="txn-1",
        index=0,
        tool="post_webhook",
        args={"url": "https://example.com", "body": "ping"},
        resource="http",
        reversible=False,
    )
    base.update(overrides)
    return Effect(**base)


def test_http_adapter_conforms_to_resource_adapter_protocol():
    assert isinstance(HTTPAdapter(), ResourceAdapter)


def test_http_adapter_does_not_conform_to_transactional_sub_protocol():
    # The runtime dispatches begin/commit/rollback by isinstance against
    # TransactionalResourceAdapter. HTTPAdapter has no transaction-scope
    # lifecycle (no third-party "BEGIN"), so it must NOT be auto-driven
    # through those calls — its presence in the adapter dict must not
    # alter txn-bracketing behaviour.
    assert not isinstance(HTTPAdapter(), TransactionalResourceAdapter)


def test_supports_rollback_is_false():
    assert HTTPAdapter().supports_rollback() is False


def test_snapshot_raises_irreversible_adapter_error():
    # The runtime should never call snapshot on a supports_rollback=False
    # adapter; this exception exists to make a routing bug fail loudly
    # rather than corrupt state silently.
    with pytest.raises(IrreversibleAdapterError):
        HTTPAdapter().snapshot(make_effect())


def test_restore_raises_irreversible_adapter_error():
    with pytest.raises(IrreversibleAdapterError):
        HTTPAdapter().restore(
            SnapshotHandle(resource="http", effect_index=0, payload={})
        )


def test_apply_invokes_tool_with_bound_args_no_handle():
    # HTTP tools declare injects_handle=False — the tool fires the real
    # HTTP call itself; the adapter passes effect.args as kwargs.
    calls: list[dict] = []

    def fake_tool(url, body):
        calls.append({"url": url, "body": body})
        return {"status": 200}

    result = HTTPAdapter().apply(make_effect(), fake_tool)
    assert calls == [{"url": "https://example.com", "body": "ping"}]
    assert result == {"status": 200}
