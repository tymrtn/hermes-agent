"""CLI ``/model local`` and ``/model primary``.

Both swap the agent's runtime in place instead of going through
``model_switch.switch_model()`` (which would overwrite the primary-runtime
snapshot ``/model primary`` restores), so the CLI's own copy of the route has
to be re-synced from the agent afterwards.
"""

from unittest.mock import patch

import pytest

import cli as cli_module
from cli import HermesCLI


class _StubAgent:
    def __init__(self, fallback_activated=False):
        self.model = "grok-4.2"
        self.provider = "xai"
        self.requested_provider = "xai"
        self.base_url = "https://api.x.ai/v1"
        self.api_key = "xai-key"
        self.api_mode = "chat_completions"
        self._fallback_activated = fallback_activated
        self._hold_local_fallback = False
        self.restore_calls = []

    def go_local(self):
        self.model = "/models/local-model"
        self.provider = "custom"
        self.requested_provider = "custom"
        self.base_url = "http://127.0.0.1:18765/v1"
        self.api_key = "local"
        self._hold_local_fallback = True
        self._fallback_activated = True

    def _restore_primary_runtime(self, force=False):
        self.restore_calls.append(force)
        self.model = "grok-4.2"
        self.provider = "xai"
        self.base_url = "https://api.x.ai/v1"
        self.api_key = "xai-key"
        self._fallback_activated = False
        return True


def _make_cli(agent=None):
    cli = HermesCLI.__new__(HermesCLI)
    cli.agent = agent
    cli.model = "grok-4.2"
    cli.provider = "xai"
    cli.requested_provider = "xai"
    cli.api_key = "xai-key"
    cli.base_url = "https://api.x.ai/v1"
    cli.api_mode = "chat_completions"
    cli._explicit_api_key = None
    cli._explicit_base_url = None
    cli._pending_model_switch_note = None
    return cli


@pytest.fixture
def printed(monkeypatch):
    lines = []
    monkeypatch.setattr(cli_module, "_cprint", lambda text="", *a, **k: lines.append(str(text)))
    return lines


def test_local_switches_cli_state_to_the_endpoint(printed):
    agent = _StubAgent()
    cli = _make_cli(agent)

    def _activate(target_agent):
        target_agent.go_local()
        return {"ok": True, "already_active": False, "started": True, "entry": {}, "error": ""}

    with patch("agent.chat_completion_helpers.activate_local_fallback", _activate):
        cli._handle_runtime_model_alias("local")

    assert cli.model == "/models/local-model"
    assert cli.provider == "custom"
    assert cli.base_url == "http://127.0.0.1:18765/v1"
    # The next turn re-resolves credentials from these — leaving the old
    # provider's key here would undo the switch.
    assert cli._explicit_api_key == "local"
    assert cli._explicit_base_url == "http://127.0.0.1:18765/v1"

    output = "\n".join(printed)
    assert "Switched to local endpoint" in output
    assert "Started the local server." in output
    assert "/model primary" in output
    assert "switched from grok-4.2" in cli._pending_model_switch_note


def test_local_reports_already_active_without_restarting(printed):
    agent = _StubAgent()
    agent.go_local()
    cli = _make_cli(agent)

    with patch(
        "agent.chat_completion_helpers.activate_local_fallback",
        lambda a: {"ok": True, "already_active": True, "started": False, "entry": {}, "error": ""},
    ):
        cli._handle_runtime_model_alias("local")

    output = "\n".join(printed)
    assert "Already on local endpoint" in output
    assert "Started the local server." not in output


def test_local_failure_leaves_cli_state_alone(printed):
    cli = _make_cli(_StubAgent())

    with patch(
        "agent.chat_completion_helpers.activate_local_fallback",
        lambda a: {
            "ok": False,
            "already_active": False,
            "started": False,
            "entry": None,
            "error": "start_command does not exist: /abs/serve.command",
        },
    ):
        cli._handle_runtime_model_alias("local")

    assert cli.model == "grok-4.2"
    assert cli._pending_model_switch_note is None
    assert "does not exist" in "\n".join(printed)


def test_primary_releases_the_hold_and_forces_the_restore(printed):
    agent = _StubAgent()
    agent.go_local()
    cli = _make_cli(agent)
    cli.model = "/models/local-model"
    cli.provider = "custom"
    cli.base_url = "http://127.0.0.1:18765/v1"

    cli._handle_runtime_model_alias("primary")

    assert agent._hold_local_fallback is False
    # Forced: the primary is normally still inside its failover cooldown,
    # which is exactly why the session went local.
    assert agent.restore_calls == [True]
    assert cli.model == "grok-4.2"
    assert cli.provider == "xai"
    assert "Restored primary model" in "\n".join(printed)


def test_primary_is_a_noop_when_not_on_a_fallback(printed):
    agent = _StubAgent(fallback_activated=False)
    cli = _make_cli(agent)

    cli._handle_runtime_model_alias("primary")

    assert agent.restore_calls == []
    assert "Already on the primary model" in "\n".join(printed)


def test_primary_reports_a_failed_restore(printed):
    agent = _StubAgent(fallback_activated=True)
    agent._restore_primary_runtime = lambda force=False: False
    cli = _make_cli(agent)

    cli._handle_runtime_model_alias("primary")

    assert "Could not restore the primary model" in "\n".join(printed)


def test_runtime_aliases_need_a_running_agent(printed):
    cli = _make_cli(agent=None)
    cli._handle_runtime_model_alias("local")
    assert "need a running agent" in "\n".join(printed)
