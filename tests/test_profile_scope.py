"""ensure_profile_scope: bind the profile's secret scope only when Hermes fired a hook without one."""

import sys
import types
from contextvars import ContextVar

import pytest

from issue_sync import secrets


class UnscopedSecretError(RuntimeError):
    pass


@pytest.fixture
def fake_hermes(monkeypatch, tmp_path):
    """Minimal stand-ins for the three Hermes seams, failing closed like the real ones."""
    scope: ContextVar = ContextVar("scope", default=None)
    state = {"multiplex": True, "owner_calls": []}

    ss = types.ModuleType("agent.secret_scope")
    ss.current_secret_scope = lambda: scope.get()
    ss.is_multiplex_active = lambda: state["multiplex"]
    ss.set_secret_scope = lambda secrets_, profile_home=None: scope.set(dict(secrets_))
    ss.reset_secret_scope = lambda token: scope.reset(token)

    def get_secret(name, default=None):
        bound = scope.get()
        if bound is None:
            if state["multiplex"]:
                raise UnscopedSecretError(name)
            return default
        return bound.get(name, default)

    ss.get_secret = get_secret
    hc = types.ModuleType("hermes_constants")
    hc.get_hermes_home = lambda: tmp_path
    lp = types.ModuleType("tui_gateway.launch_profile_policy")

    def owner_secret_scope(home):
        state["owner_calls"].append(home)
        return {"LINEAR_API_KEY": "from-profile"}

    lp.owner_secret_scope = owner_secret_scope
    for name, mod in {"agent": types.ModuleType("agent"), "agent.secret_scope": ss,
                      "hermes_constants": hc, "tui_gateway": types.ModuleType("tui_gateway"),
                      "tui_gateway.launch_profile_policy": lp}.items():
        monkeypatch.setitem(sys.modules, name, mod)
    state["scope"] = scope
    return state


def test_unscoped_multiplex_hook_gets_profile_scope(fake_hermes):
    with pytest.raises(UnscopedSecretError):
        secrets.get_secret("LINEAR_API_KEY")
    with secrets.ensure_profile_scope():
        assert secrets.get_secret("LINEAR_API_KEY") == "from-profile"
    assert fake_hermes["scope"].get() is None  # restored afterwards


def test_existing_scope_is_left_alone(fake_hermes):
    token = fake_hermes["scope"].set({"LINEAR_API_KEY": "hermes-bound"})
    try:
        with secrets.ensure_profile_scope():
            assert secrets.get_secret("LINEAR_API_KEY") == "hermes-bound"
    finally:
        fake_hermes["scope"].reset(token)
    assert fake_hermes["owner_calls"] == []


def test_single_profile_gateway_is_a_no_op(fake_hermes):
    fake_hermes["multiplex"] = False
    with secrets.ensure_profile_scope():
        assert fake_hermes["scope"].get() is None
    assert fake_hermes["owner_calls"] == []


def test_scope_restored_when_body_raises(fake_hermes):
    with pytest.raises(ValueError):
        with secrets.ensure_profile_scope():
            raise ValueError("boom")
    assert fake_hermes["scope"].get() is None


def test_outside_hermes_is_a_no_op(monkeypatch):
    monkeypatch.setitem(sys.modules, "agent.secret_scope", None)  # import raises ImportError
    with secrets.ensure_profile_scope():
        pass
