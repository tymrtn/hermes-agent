"""Start-on-fallback for local endpoints, and the hold that keeps them.

Covers the three behaviors that make a local MLX/llama server a usable
fallback target:

1. ``try_activate_fallback`` brings the server up before building a client,
   and skips the entry (marking it unavailable) when it cannot.
2. Activating a startable local entry HOLDS it across turns —
   ``restore_primary_runtime`` stops bouncing back to a primary that is
   still failing.
3. ``/model local`` / ``/model primary`` drive the same machinery explicitly:
   the hold is released only on request, and the restore is forced past the
   failover cooldown.

No network and no real subprocess: ``ensure_local_endpoint`` is patched at
the seam it is imported into.
"""

from unittest.mock import MagicMock, patch

import pytest

from run_agent import AIAgent


LOCAL_ENTRY = {
    "provider": "custom",
    "model": "/models/local-model",
    "base_url": "http://127.0.0.1:18765/v1",
    "api_mode": "chat_completions",
    "api_key": "local",
    "start_command": "/abs/serve.command",
}
REMOTE_ENTRY = {
    "provider": "openrouter",
    "model": "z-ai/glm-4.7",
    "base_url": "https://openrouter.ai/api/v1",
}


def _make_agent(fallback_model=None):
    with (
        patch('model_tools.get_tool_definitions', return_value=[]),
        patch('model_tools.check_toolset_requirements', return_value={}),
        patch('agent.process_bootstrap.OpenAI'),
    ):
        agent = AIAgent(
            api_key="test-key",
            base_url="https://api.x.ai/v1",
            provider="xai",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
            fallback_model=fallback_model,
        )
        agent.client = MagicMock()
        return agent


def _mock_client(base_url="http://127.0.0.1:18765/v1", api_key="local"):
    client = MagicMock()
    client.base_url = base_url
    client.api_key = api_key
    return client


def _ready(started=False):
    return {"started": started, "ready": True, "error": ""}


def _unready(error="connection refused"):
    return {"started": False, "ready": False, "error": error}


@pytest.fixture
def resolved_client():
    """Route every fallback client build through a stub."""
    with patch(
        "agent.auxiliary_client.resolve_provider_client",
        side_effect=lambda provider, model=None, **kw: (
            _mock_client(kw.get("explicit_base_url") or "https://openrouter.ai/api/v1"),
            model,
        ),
    ):
        yield


# ── start-on-fallback ────────────────────────────────────────────────────


class TestStartOnFallback:
    def test_start_command_entry_is_ensured_before_activation(self, resolved_client):
        agent = _make_agent(fallback_model=[dict(LOCAL_ENTRY)])
        with patch(
            "agent.chat_completion_helpers.ensure_local_endpoint",
            return_value=_ready(started=True),
        ) as ensure:
            assert agent._try_activate_fallback() is True

        ensure.assert_called_once()
        assert ensure.call_args[0][0]["start_command"] == "/abs/serve.command"
        assert agent.model == "/models/local-model"
        assert agent.base_url == "http://127.0.0.1:18765/v1"

    def test_entry_without_start_command_is_not_ensured(self, resolved_client):
        agent = _make_agent(fallback_model=[dict(REMOTE_ENTRY)])
        with patch(
            "agent.chat_completion_helpers.ensure_local_endpoint"
        ) as ensure:
            assert agent._try_activate_fallback() is True
        ensure.assert_not_called()

    def test_failed_start_skips_to_next_entry(self, resolved_client):
        """A local endpoint that will not come up must not eat the chain."""
        agent = _make_agent(fallback_model=[dict(LOCAL_ENTRY), dict(REMOTE_ENTRY)])
        with patch(
            "agent.chat_completion_helpers.ensure_local_endpoint",
            return_value=_unready("launched /abs/serve.command but ... did not answer within 90s"),
        ):
            assert agent._try_activate_fallback() is True

        assert agent.model == "z-ai/glm-4.7"
        assert agent.provider == "openrouter"
        assert agent._hold_local_fallback is False

    def test_failed_start_marks_entry_unavailable(self, resolved_client):
        agent = _make_agent(fallback_model=[dict(LOCAL_ENTRY), dict(REMOTE_ENTRY)])
        with patch(
            "agent.chat_completion_helpers.ensure_local_endpoint",
            return_value=_unready(),
        ) as ensure:
            agent._try_activate_fallback()
            # Rewind the chain: the suppressed entry must not be retried.
            agent._fallback_index = 0
            agent._try_activate_fallback()

        assert ensure.call_count == 1

    def test_failed_start_alone_in_chain_returns_false(self, resolved_client):
        agent = _make_agent(fallback_model=[dict(LOCAL_ENTRY)])
        with patch(
            "agent.chat_completion_helpers.ensure_local_endpoint",
            return_value=_unready(),
        ):
            assert agent._try_activate_fallback() is False
        assert agent._fallback_activated is False

    def test_notice_names_the_endpoint_and_the_way_back(self, resolved_client):
        agent = _make_agent(fallback_model=[dict(LOCAL_ENTRY)])
        with patch(
            "agent.chat_completion_helpers.ensure_local_endpoint",
            return_value=_ready(started=True),
        ):
            agent._try_activate_fallback()

        notice = "\n".join(agent._pending_fallback_notice or [])
        assert "http://127.0.0.1:18765/v1" in notice
        assert "/model primary" in notice
        assert "Started the local server." in notice

    def test_notice_omits_started_when_server_was_already_up(self, resolved_client):
        agent = _make_agent(fallback_model=[dict(LOCAL_ENTRY)])
        with patch(
            "agent.chat_completion_helpers.ensure_local_endpoint",
            return_value=_ready(started=False),
        ):
            agent._try_activate_fallback()

        assert "Started the local server." not in "\n".join(agent._pending_fallback_notice or [])
        assert "/model primary" in "\n".join(agent._pending_fallback_notice or [])

    def test_remote_fallback_keeps_the_plain_notice(self, resolved_client):
        agent = _make_agent(fallback_model=[dict(REMOTE_ENTRY)])
        agent._try_activate_fallback()
        assert "Model fallback" in "\n".join(agent._pending_fallback_notice or [])
        assert "/model primary" not in "\n".join(agent._pending_fallback_notice or [])


# ── cross-turn hold ──────────────────────────────────────────────────────


class TestLocalFallbackHold:
    def test_local_activation_sets_the_hold(self, resolved_client):
        agent = _make_agent(fallback_model=[dict(LOCAL_ENTRY)])
        with patch(
            "agent.chat_completion_helpers.ensure_local_endpoint",
            return_value=_ready(),
        ):
            agent._try_activate_fallback()
        assert agent._hold_local_fallback is True

    def test_remote_activation_does_not_set_the_hold(self, resolved_client):
        agent = _make_agent(fallback_model=[dict(REMOTE_ENTRY)])
        agent._try_activate_fallback()
        assert agent._hold_local_fallback is False

    def test_hold_blocks_the_per_turn_restore(self, resolved_client):
        agent = _make_agent(fallback_model=[dict(LOCAL_ENTRY)])
        with patch(
            "agent.chat_completion_helpers.ensure_local_endpoint",
            return_value=_ready(),
        ):
            agent._try_activate_fallback()

        assert agent._restore_primary_runtime() is False
        assert agent.model == "/models/local-model"
        assert agent.provider == "custom"

    def test_hold_keeps_the_chain_index_stable(self, resolved_client):
        """A blocked restore must not rewind the chain — otherwise the walk
        replays from the top on the next failure."""
        agent = _make_agent(fallback_model=[dict(REMOTE_ENTRY), dict(LOCAL_ENTRY)])
        with patch(
            "agent.chat_completion_helpers.ensure_local_endpoint",
            return_value=_ready(),
        ):
            agent._try_activate_fallback()  # remote
            agent._try_activate_fallback()  # local -> hold
        index_while_held = agent._fallback_index

        assert agent._restore_primary_runtime() is False
        assert agent._fallback_index == index_while_held == 2

    def test_release_lets_the_restore_run_again(self, resolved_client):
        agent = _make_agent(fallback_model=[dict(LOCAL_ENTRY)])
        primary_model = agent.model
        with patch(
            "agent.chat_completion_helpers.ensure_local_endpoint",
            return_value=_ready(),
        ):
            agent._try_activate_fallback()

        assert agent._release_local_fallback_hold() is True
        assert agent._hold_local_fallback is False
        assert agent._restore_primary_runtime() is True
        assert agent.model == primary_model
        assert agent.provider == "xai"

    def test_release_reports_when_no_hold_was_set(self):
        agent = _make_agent()
        assert agent._release_local_fallback_hold() is False

    def test_forced_restore_bypasses_the_hold_and_the_cooldown(self, resolved_client):
        """``/model primary`` must work while the primary is still benched —
        that cooldown is exactly why the session is on the local endpoint."""
        import time as _time

        agent = _make_agent(fallback_model=[dict(LOCAL_ENTRY)])
        primary_model = agent.model
        with patch(
            "agent.chat_completion_helpers.ensure_local_endpoint",
            return_value=_ready(),
        ):
            agent._try_activate_fallback()
        agent._rate_limited_until = _time.monotonic() + 3600

        assert agent._restore_primary_runtime() is False
        assert agent._restore_primary_runtime(force=True) is True
        assert agent.model == primary_model


# ── /model local ─────────────────────────────────────────────────────────


class TestActivateLocalFallback:
    def test_switches_to_the_local_entry(self, resolved_client):
        agent = _make_agent(fallback_model=[dict(REMOTE_ENTRY), dict(LOCAL_ENTRY)])
        with patch(
            "agent.chat_completion_helpers.ensure_local_endpoint",
            return_value=_ready(started=True),
        ):
            outcome = agent._activate_local_fallback()

        assert outcome["ok"] is True
        assert outcome["started"] is True
        assert outcome["already_active"] is False
        assert agent.model == "/models/local-model"
        assert agent.base_url == "http://127.0.0.1:18765/v1"
        assert agent._hold_local_fallback is True

    def test_preserves_the_primary_runtime_snapshot(self, resolved_client):
        """The snapshot is what /model primary restores — an explicit local
        switch must not overwrite it with the local endpoint."""
        agent = _make_agent(fallback_model=[dict(LOCAL_ENTRY)])
        primary_before = dict(agent._primary_runtime)

        with patch(
            "agent.chat_completion_helpers.ensure_local_endpoint",
            return_value=_ready(),
        ):
            agent._activate_local_fallback()

        assert agent._primary_runtime["model"] == primary_before["model"]
        assert agent._primary_runtime["provider"] == "xai"

    def test_does_not_consume_the_automatic_chain_position(self, resolved_client):
        agent = _make_agent(fallback_model=[dict(REMOTE_ENTRY), dict(LOCAL_ENTRY)])
        with patch(
            "agent.chat_completion_helpers.ensure_local_endpoint",
            return_value=_ready(),
        ):
            agent._activate_local_fallback()

        assert agent._fallback_index == 0
        assert agent._fallback_chain == [REMOTE_ENTRY, LOCAL_ENTRY]

    def test_reports_when_nothing_local_is_configured(self):
        agent = _make_agent(fallback_model=[dict(REMOTE_ENTRY)])
        outcome = agent._activate_local_fallback()
        assert outcome["ok"] is False
        assert "No local endpoint is configured" in outcome["error"]

    def test_reports_when_the_endpoint_will_not_start(self, resolved_client):
        agent = _make_agent(fallback_model=[dict(LOCAL_ENTRY)])
        with patch(
            "agent.chat_completion_helpers.ensure_local_endpoint",
            return_value=_unready("start_command does not exist: /abs/serve.command"),
        ):
            outcome = agent._activate_local_fallback()

        assert outcome["ok"] is False
        assert "does not exist" in outcome["error"]
        assert agent._hold_local_fallback is False
        assert agent.provider == "xai"

    def test_holds_when_already_on_the_local_endpoint(self, resolved_client):
        agent = _make_agent(fallback_model=[dict(LOCAL_ENTRY)])
        with patch(
            "agent.chat_completion_helpers.ensure_local_endpoint",
            return_value=_ready(),
        ):
            agent._activate_local_fallback()
            agent._release_local_fallback_hold()
            outcome = agent._activate_local_fallback()

        assert outcome["ok"] is True
        assert outcome["already_active"] is True
        assert agent._hold_local_fallback is True

    def test_clears_a_previous_automatic_suppression(self, resolved_client):
        """An entry auto-skipped earlier in the session must still be
        reachable explicitly once the operator fixes it."""
        agent = _make_agent(fallback_model=[dict(LOCAL_ENTRY)])
        with patch(
            "agent.chat_completion_helpers.ensure_local_endpoint",
            return_value=_unready(),
        ):
            agent._try_activate_fallback()
        assert agent._unavailable_fallback_keys

        with patch(
            "agent.chat_completion_helpers.ensure_local_endpoint",
            return_value=_ready(started=True),
        ):
            outcome = agent._activate_local_fallback()

        assert outcome["ok"] is True
        assert agent.model == "/models/local-model"

    def test_does_not_leave_failover_chatter_behind(self, resolved_client):
        """Nothing failed — the buffered "primary model failed" line and the
        one-shot notice both belong to the automatic path."""
        agent = _make_agent(fallback_model=[dict(LOCAL_ENTRY)])
        agent._retry_status_buffer = []
        with patch(
            "agent.chat_completion_helpers.ensure_local_endpoint",
            return_value=_ready(),
        ):
            agent._activate_local_fallback()

        assert agent._retry_status_buffer == []
        assert agent._pending_fallback_notice is None
