import pytest

from pherix.core.tools import REGISTRY


@pytest.fixture(autouse=True)
def _clean_tool_registry():
    """The tool registry is process-global — isolate every test from it."""
    REGISTRY.clear()
    yield
    REGISTRY.clear()
