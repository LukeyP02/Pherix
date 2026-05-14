import pytest

from pherix.core.tools import REGISTRY, active_txn, tool


def test_tool_registers_in_registry():
    @tool(resource="sql")
    def insert_user(conn, name):
        return name

    assert "insert_user" in REGISTRY
    spec = REGISTRY.get("insert_user")
    assert spec.resource == "sql"
    assert spec.reversible is True
    assert spec.injects_handle is True


def test_duplicate_registration_raises():
    @tool(resource="sql")
    def dup(conn, name):
        return name

    with pytest.raises(ValueError):

        @tool(resource="sql", name="dup")
        def dup2(conn, name):
            return name


def test_custom_name_and_reversible_flag():
    @tool(resource="http", reversible=False, name="post_webhook")
    def _post(url):
        return url

    spec = REGISTRY.get("post_webhook")
    assert spec.reversible is False
    assert spec.resource == "http"


def test_call_outside_txn_runs_raw_function():
    @tool(resource="sql")
    def echo(conn, name):
        return f"{conn}:{name}"

    # No active transaction -> transparent passthrough; caller supplies conn.
    assert echo("CONN", name="bob") == "CONN:bob"


def test_call_inside_txn_delegates_to_context():
    calls = []

    class FakeCtx:
        def record_tool_call(self, name, args, kwargs):
            calls.append((name, args, kwargs))
            return "journalled"

    @tool(resource="sql")
    def insert_user(conn, name):
        raise AssertionError("raw fn must not run inside a transaction")

    token = active_txn.set(FakeCtx())
    try:
        result = insert_user(name="bob")
    finally:
        active_txn.reset(token)

    assert result == "journalled"
    assert calls == [("insert_user", (), {"name": "bob"})]


def test_bind_args_strips_injected_handle():
    @tool(resource="sql")
    def insert_user(conn, name, role="member"):
        return name

    spec = REGISTRY.get("insert_user")
    assert spec.bind_args((), {"name": "bob"}) == {"name": "bob", "role": "member"}
    assert spec.bind_args(("bob", "admin"), {}) == {"name": "bob", "role": "admin"}


def test_bind_args_keeps_handle_when_not_injected():
    @tool(resource="http", injects_handle=False)
    def ping(url):
        return url

    spec = REGISTRY.get("ping")
    assert spec.bind_args((), {"url": "x"}) == {"url": "x"}


def test_wrapper_exposes_tool_spec():
    @tool(resource="sql")
    def insert_user(conn, name):
        return name

    assert insert_user.tool_spec is REGISTRY.get("insert_user")


def test_unregistered_tool_absent_from_registry():
    assert "never_registered" not in REGISTRY
