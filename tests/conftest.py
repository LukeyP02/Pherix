import pytest

from pherix.core.tools import REGISTRY


@pytest.fixture(autouse=True)
def _clean_tool_registry():
    """The tool registry is process-global — isolate every test from it."""
    REGISTRY.clear()
    yield
    REGISTRY.clear()


@pytest.fixture(autouse=True)
def _isolate_journal(monkeypatch, tmp_path):
    """Pin the default journal location to a per-test tmp file.

    ``agent_txn(...)`` with no explicit ``audit=`` now opens
    ``AuditJournal.default()``, which reads ``$PHERIX_JOURNAL`` (falling back to
    the real ``~/.pherix/journal.db``). Every test gets its own tmp journal so
    the ~250 default call sites never touch the operator's real journal — test
    isolation, not a shared global file.
    """
    monkeypatch.setenv("PHERIX_JOURNAL", str(tmp_path / "journal.db"))
