"""TUI/dashboard/desktop ``/model local`` and ``/model primary``.

All three surfaces switch models through the gateway's ``config.set model``
RPC, so the runtime aliases have to be honoured there — including the
deferred path a mid-turn pick takes (stash now, apply at the next turn start).
They name a runtime, not a model: routing them through ``switch_model()``
would overwrite the ``_primary_runtime`` snapshot ``primary`` restores.

Parity targets: ``cli.py::_handle_runtime_model_alias`` (tests/cli) and
``gateway/slash_commands.py::_handle_runtime_model_alias`` (tests/gateway).
"""

from __future__ import annotations

import threading
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from tui_gateway import server


LOCAL_ENTRY = {
    "provider": "custom",
    "model": "/models/local-model",
    "base_url": "http://127.0.0.1:18765/v1",
    "api_key": "local",
    "api_mode": "chat_completions",
    "start_command": "/abs/serve.command",
}


def _agent(*, fallback_activated: bool = False, hold: bool = False):
    agent = SimpleNamespace(
        model="grok-4.2",
        provider="xai",
        requested_provider="xai",
        base_url="https://api.x.ai/v1",
        api_key="xai-key",
        api_mode="chat_completions",
        service_tier=None,
        request_overrides={},
        _fallback_chain=[dict(LOCAL_ENTRY)],
        _fallback_activated=fallback_activated,
        _hold_local_fallback=hold,
        _primary_runtime={"model": "grok-4.2", "provider": "xai"},
        # A MagicMock DB keeps the switch marker off disk.
        _session_db=MagicMock(),
        restore_calls=[],
        switch_calls=[],
    )

    def _restore(force=False):
        agent.restore_calls.append(force)
        agent.model = "grok-4.2"
        agent.provider = "xai"
        agent.base_url = "https://api.x.ai/v1"
        agent.api_key = "xai-key"
        agent._fallback_activated = False
        return True

    def _switch_model(**kwargs):
        agent.switch_calls.append(kwargs)
        agent.model = kwargs.get("new_model", agent.model)
        agent.provider = kwargs.get("new_provider", agent.provider)

    agent._restore_primary_runtime = _restore
    agent.switch_model = _switch_model
    return agent


def _go_local(agent):
    """What a successful activation leaves behind on the agent."""
    agent.model = LOCAL_ENTRY["model"]
    agent.provider = LOCAL_ENTRY["provider"]
    agent.base_url = LOCAL_ENTRY["base_url"]
    agent.api_key = LOCAL_ENTRY["api_key"]
    agent._fallback_activated = True
    agent._hold_local_fallback = True


def _activation(**overrides):
    outcome = {
        "ok": True,
        "already_active": False,
        "started": False,
        "entry": dict(LOCAL_ENTRY),
        "error": "",
    }
    outcome.update(overrides)
    return outcome


@pytest.fixture
def emits(monkeypatch):
    """Silence the switch's side effects; keep the emitted session.info."""
    seen = []
    monkeypatch.setattr(server, "_restart_slash_worker", lambda *a, **k: None)
    monkeypatch.setattr(server, "_persist_live_session_runtime", lambda *a, **k: None)
    monkeypatch.setattr(
        server, "_persist_live_session_system_prompt", lambda *a, **k: None
    )
    monkeypatch.setattr(
        server, "_session_info", lambda agent, *a: {"model": getattr(agent, "model", "")}
    )
    monkeypatch.setattr(server, "_emit", lambda *args: seen.append(args))
    return seen


@pytest.fixture
def session(request):
    sess = {
        "agent": None,
        "session_key": "session-key",
        "history": [{"role": "user", "content": "hello"}],
        "history_lock": threading.Lock(),
        "history_version": 0,
        "running": False,
    }
    server._sessions["sid"] = sess
    request.addfinalizer(lambda: server._sessions.pop("sid", None))
    return sess


def _config_set(value, **params):
    return server.handle_request(
        {
            "id": "1",
            "method": "config.set",
            "params": {"session_id": "sid", "key": "model", "value": value, **params},
        }
    )


def _markers(session):
    return [h for h in session["history"] if server._is_model_switch_marker(h)]


# ── /model local ─────────────────────────────────────────────────────────


def test_local_activates_the_endpoint_and_pins_the_session(session, emits, monkeypatch):
    agent = _agent()
    session["agent"] = agent

    def _activate(target):
        assert target is agent
        _go_local(target)
        return _activation(started=True)

    monkeypatch.setattr(
        "agent.chat_completion_helpers.activate_local_fallback", _activate
    )

    resp = _config_set("local")

    assert resp["result"]["value"] == LOCAL_ENTRY["model"]
    assert resp["result"]["scope"] == "session"
    assert "/model primary" in resp["result"]["warning"]
    assert "Started the local server." in resp["result"]["warning"]
    # The pin is what survives a rebuild/resume — the TUI's equivalent of the
    # gateway's session model override.
    assert session["model_override"] == {
        "model": LOCAL_ENTRY["model"],
        "provider": "custom",
        "base_url": LOCAL_ENTRY["base_url"],
        "api_key": "local",
        "api_mode": "chat_completions",
    }
    assert len(_markers(session)) == 1
    assert LOCAL_ENTRY["model"] in _markers(session)[0]["content"]
    assert ("session.info", "sid", {"model": LOCAL_ENTRY["model"]}) in emits


def test_local_never_goes_through_the_model_resolution_pipeline(
    session, emits, monkeypatch
):
    """switch_model() would overwrite the _primary_runtime snapshot that
    /model primary restores, stranding the session on the local endpoint."""
    agent = _agent()
    session["agent"] = agent
    monkeypatch.setattr(
        "hermes_cli.model_switch.switch_model",
        lambda *a, **k: pytest.fail("runtime aliases must not resolve as models"),
    )
    monkeypatch.setattr(
        "agent.chat_completion_helpers.activate_local_fallback",
        lambda target: (_go_local(target), _activation())[1],
    )

    assert _config_set("local")["result"]["value"] == LOCAL_ENTRY["model"]
    assert agent._primary_runtime == {"model": "grok-4.2", "provider": "xai"}
    assert agent.switch_calls == []


def test_local_reports_the_activation_error_and_changes_nothing(
    session, emits, monkeypatch
):
    agent = _agent()
    session["agent"] = agent
    monkeypatch.setattr(
        "agent.chat_completion_helpers.activate_local_fallback",
        lambda _target: _activation(
            ok=False, error="start_command does not exist: /abs/serve.command"
        ),
    )

    resp = _config_set("local")

    assert "does not exist" in resp["error"]["message"]
    assert agent.model == "grok-4.2"
    assert "model_override" not in session
    assert _markers(session) == []


def test_local_when_already_local_pins_without_rewriting_history(
    session, emits, monkeypatch
):
    """An automatic failover already put the session on the endpoint: pin it so
    a rebuild keeps it, but appending a switch marker for a no-op would rewrite
    the cached prompt prefix for nothing."""
    agent = _agent(fallback_activated=True, hold=True)
    _go_local(agent)
    session["agent"] = agent
    monkeypatch.setattr(
        "agent.chat_completion_helpers.activate_local_fallback",
        lambda _target: _activation(already_active=True),
    )

    resp = _config_set("local")

    assert resp["result"]["value"] == LOCAL_ENTRY["model"]
    assert session["model_override"]["model"] == LOCAL_ENTRY["model"]
    assert _markers(session) == []
    assert emits == []


def test_runtime_aliases_need_a_running_agent(emits):
    resp = server.handle_request(
        {"id": "1", "method": "config.set", "params": {"key": "model", "value": "local"}}
    )
    assert "need a running agent" in resp["error"]["message"]


# ── /model primary ───────────────────────────────────────────────────────


def test_primary_releases_the_hold_and_forces_the_restore(session, emits, monkeypatch):
    agent = _agent(fallback_activated=True, hold=True)
    _go_local(agent)
    session["agent"] = agent
    session["model_override"] = {"model": LOCAL_ENTRY["model"], "provider": "custom"}

    resp = _config_set("primary")

    assert resp["result"]["value"] == "grok-4.2"
    assert agent._hold_local_fallback is False
    # Forced: the primary is normally still inside its failover cooldown,
    # which is exactly why the session went local.
    assert agent.restore_calls == [True]
    # The pin has to go, or the next rebuild lands back on the endpoint.
    assert "model_override" not in session
    assert len(_markers(session)) == 1
    assert "grok-4.2" in _markers(session)[0]["content"]


def test_primary_reports_a_failed_restore(session, emits, monkeypatch):
    agent = _agent(fallback_activated=True, hold=True)
    _go_local(agent)
    agent._restore_primary_runtime = lambda force=False: False
    session["agent"] = agent

    resp = _config_set("primary")

    assert "Could not restore the primary model" in resp["error"]["message"]


def test_primary_is_a_noop_when_the_session_never_left_the_primary(
    session, emits, monkeypatch
):
    agent = _agent()
    session["agent"] = agent

    resp = _config_set("primary")

    assert resp["result"]["warning"] == "Already on the primary model."
    assert agent.restore_calls == []
    assert _markers(session) == []


def test_primary_switches_a_resumed_local_session_back_to_the_configured_model(
    session, emits, monkeypatch
):
    """A resumed session is rebuilt straight onto the endpoint from its stored
    override, so there is no fallback state to unwind — dropping the pin alone
    would leave the live agent local, which is what the gateway avoids by
    evicting and rebuilding the cached agent."""
    agent = _agent()
    _go_local(agent)
    agent._fallback_activated = False
    session["agent"] = agent
    session["model_override"] = {"model": LOCAL_ENTRY["model"], "provider": "custom"}
    monkeypatch.setattr(server, "_config_model_target", lambda: ("grok-4.2", "xai"))
    switches = []
    monkeypatch.setattr(
        server,
        "_apply_model_switch",
        lambda sid, sess, raw, **kw: switches.append((raw, kw)) or {"value": raw},
    )

    server._apply_runtime_model_alias("sid", session, "primary")

    assert "model_override" not in session
    assert switches == [
        (
            "grok-4.2 --provider xai",
            {
                "confirm_expensive_model": True,
                "pin_session_override": False,
                "persist_override": False,
                "allow_runtime_alias": False,
            },
        )
    ]


# ── mid-turn (deferred) switches ─────────────────────────────────────────


def test_alias_picked_mid_turn_is_deferred_and_names_its_target(
    session, emits, monkeypatch
):
    """A live swap can't run while a turn streams, so the pick is stashed for
    the next turn start. The UI reports the pending model until then — it must
    name the endpoint's model, not the word "local"."""
    session["agent"] = _agent()
    session["running"] = True
    monkeypatch.setattr(
        "agent.chat_completion_helpers.activate_local_fallback",
        lambda _target: pytest.fail("must not swap the agent mid-turn"),
    )

    resp = _config_set("local")

    assert resp["result"]["deferred"] is True
    assert resp["result"]["value"] == LOCAL_ENTRY["model"]
    assert session["pending_model_switch"]["raw"] == "local"
    assert session["pending_model_switch"]["display_model"] == LOCAL_ENTRY["model"]
    assert session["pending_model_switch"]["display_provider"] == "custom"
    assert server._session_info(session["agent"], session)["model"] is not None


def test_deferred_alias_applies_at_the_next_turn_start(session, emits, monkeypatch):
    agent = _agent()
    session["agent"] = agent
    session["running"] = True
    monkeypatch.setattr(
        "agent.chat_completion_helpers.activate_local_fallback",
        lambda target: (_go_local(target), _activation())[1],
    )

    _config_set("local")
    session["running"] = False
    server._apply_pending_model_switch("sid", session)

    assert agent.model == LOCAL_ENTRY["model"]
    assert session["model_override"]["model"] == LOCAL_ENTRY["model"]
    assert "pending_model_switch" not in session
    # No error was emitted — an exception here is swallowed into an error event.
    assert [e for e in emits if e[0] == "error"] == []


def test_deferred_primary_names_the_primary_runtime(session, emits):
    agent = _agent(fallback_activated=True, hold=True)
    _go_local(agent)
    session["agent"] = agent
    session["running"] = True

    resp = _config_set("primary")

    assert resp["result"]["deferred"] is True
    assert resp["result"]["value"] == "grok-4.2"
    assert session["pending_model_switch"]["display_provider"] == "xai"


# ── config sync must not resolve aliases ─────────────────────────────────


def test_a_configured_model_named_local_is_switched_to_as_a_model(
    session, emits, monkeypatch
):
    """`model.default` names a model. Resolving it as the runtime alias would
    let a config value silently pin every session to a local endpoint."""
    agent = _agent()
    session["agent"] = agent
    session["config_model_seen"] = ("old/model", "")
    monkeypatch.setattr(server, "_config_model_target", lambda: ("local", ""))
    monkeypatch.setattr(
        "agent.chat_completion_helpers.activate_local_fallback",
        lambda _target: pytest.fail("config sync must not resolve the runtime alias"),
    )
    monkeypatch.setattr(
        "hermes_cli.model_switch.switch_model",
        lambda **kwargs: SimpleNamespace(
            success=True,
            error_message="",
            new_model="local",
            target_provider="custom",
            api_key="k",
            base_url="http://example.invalid/v1",
            api_mode="chat_completions",
            model_info=None,
            warning_message="",
        ),
    )

    server._sync_agent_model_with_config("sid", session)

    assert agent.switch_calls and agent.switch_calls[0]["new_model"] == "local"
    # pin_session_override=False on the sync path: adopting config must not
    # pin the session against later config changes.
    assert "model_override" not in session
