import pytest


@pytest.fixture(autouse=True)
def _no_release_look(monkeypatch):
    """No test reaches GitHub's releases before a profile run; tests/test_update.py drives that look itself."""
    monkeypatch.setenv("KENNET_UPDATED", "test")
