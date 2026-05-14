from pherix.core.adapters.base import ResourceAdapter, SnapshotHandle
from pherix.core.effects import Effect


def test_snapshot_handle_defaults():
    h = SnapshotHandle(resource="sql", effect_index=2)
    assert h.resource == "sql"
    assert h.effect_index == 2
    assert h.payload == {}


def test_snapshot_handle_carries_json_payload():
    h = SnapshotHandle(resource="sql", effect_index=0, payload={"savepoint": "sp_0"})
    assert h.payload == {"savepoint": "sp_0"}


class _ConformingAdapter:
    name = "fake"

    def supports_rollback(self) -> bool:
        return True

    def snapshot(self, effect: Effect) -> SnapshotHandle:
        return SnapshotHandle(resource=self.name, effect_index=effect.index)

    def apply(self, effect, tool_fn):
        return tool_fn(**effect.args)

    def restore(self, handle: SnapshotHandle) -> None:
        pass


class _NotAnAdapter:
    name = "broken"

    def apply(self, effect, tool_fn):
        return None


def test_conforming_class_satisfies_protocol():
    assert isinstance(_ConformingAdapter(), ResourceAdapter)


def test_incomplete_class_fails_protocol():
    assert not isinstance(_NotAnAdapter(), ResourceAdapter)
