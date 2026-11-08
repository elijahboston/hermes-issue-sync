import sys
import types

import pytest

from issue_sync.secrets import get_secret


@pytest.fixture
def no_hermes(monkeypatch):
    # ``None`` in sys.modules makes ``import agent.secret_scope`` raise ImportError.
    monkeypatch.setitem(sys.modules, "agent", None)
    monkeypatch.delitem(sys.modules, "agent.secret_scope", raising=False)


@pytest.fixture
def hermes_scope(monkeypatch):
    """Install a stand-in ``agent.secret_scope`` whose ``get_secret`` is replaceable per test."""
    agent = types.ModuleType("agent")
    scope = types.ModuleType("agent.secret_scope")
    agent.secret_scope = scope
    monkeypatch.setitem(sys.modules, "agent", agent)
    monkeypatch.setitem(sys.modules, "agent.secret_scope", scope)
    return scope


def test_outside_hermes_reads_environment(no_hermes, monkeypatch):
    monkeypatch.setenv("TS_TEST_KEY", "from-env")
    assert get_secret("TS_TEST_KEY") == "from-env"
    monkeypatch.delenv("TS_TEST_KEY")
    assert get_secret("TS_TEST_KEY") is None


def test_inside_hermes_uses_scoped_lookup_not_environment(hermes_scope, monkeypatch):
    monkeypatch.setenv("TS_TEST_KEY", "from-env")
    hermes_scope.get_secret = lambda name: {"TS_TEST_KEY": "from-scope"}.get(name)
    assert get_secret("TS_TEST_KEY") == "from-scope"
    hermes_scope.get_secret = lambda name: None
    assert get_secret("TS_TEST_KEY") is None  # a scoped miss never falls back to os.environ


def test_scope_errors_propagate(hermes_scope, monkeypatch):
    class UnscopedSecretError(RuntimeError):
        pass

    def fail(name):
        raise UnscopedSecretError(name)

    monkeypatch.setenv("TS_TEST_KEY", "from-env")
    hermes_scope.get_secret = fail
    with pytest.raises(UnscopedSecretError):
        get_secret("TS_TEST_KEY")
