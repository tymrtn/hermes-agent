"""Turn-admission lifecycle — the marker is set only after session
resolution AND inbound preprocessing succeed, immediately before the agent
turn. Pre-admission early returns (a failed delegated-session resolution, or
inbound preprocessing returning None) must NOT count as an admitted turn, so
deferred adapter side effects (e.g. Slack's staged thread-watermark commit)
never fire for a message that never reached the agent.
"""

import sys
import types
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

import gateway.run as gateway_run
from gateway.config import GatewayConfig, Platform
from gateway.platforms.base import MessageEvent
from gateway.session import SessionEntry, SessionSource


def _bootstrap(monkeypatch, tmp_path):
    """Minimal GatewayRunner that drives _handle_message_with_agent to the
    agent-run boundary (mirrors tests/gateway/test_42039_duplicate_user_message)."""
    fake_dotenv = types.ModuleType("dotenv")
    fake_dotenv.load_dotenv = lambda *args, **kwargs: None
    monkeypatch.setitem(sys.modules, "dotenv", fake_dotenv)

    config = GatewayConfig()
    runner = gateway_run.GatewayRunner(config)
    runner.adapters = {}
    runner._running_agents = {}
    runner._running_agents_ts = {}
    runner._pending_messages = {}
    runner._pending_approvals = {}
    runner._is_user_authorized = lambda _source: True
    runner._set_session_env = lambda _context: None
    runner._handle_active_session_busy_message = AsyncMock(return_value=False)
    runner._session_db = MagicMock()
    runner._recover_telegram_topic_thread_id = lambda _source: None
    runner._cache_session_source = lambda _key, _source: None
    runner._is_session_run_current = lambda _key, _gen: True
    runner._begin_session_run_generation = lambda _key: 1
    runner._reply_anchor_for_event = lambda _event: None
    runner._get_guild_id = lambda _event: None
    runner._should_send_voice_reply = lambda *_a, **_kw: False
    runner.hooks = MagicMock()
    runner.hooks.emit = AsyncMock()

    runner.session_store = MagicMock()
    runner.session_store.get_or_create_session.return_value = SessionEntry(
        session_key="agent:main:telegram:group:-1001:12345",
        session_id="sess-admit",
        created_at=datetime.now(),
        updated_at=datetime.now(),
        platform=Platform.TELEGRAM,
        chat_type="group",
    )
    runner.session_store.load_transcript.return_value = []
    runner.session_store.append_to_transcript = MagicMock()
    runner.session_store.has_platform_message_id.return_value = False
    runner.session_store.update_session = MagicMock()

    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    monkeypatch.setattr(
        gateway_run, "_resolve_runtime_agent_kwargs", lambda: {"api_key": "fake"}
    )
    monkeypatch.setattr(
        "agent.model_metadata.get_model_context_length",
        lambda *_args, **_kwargs: 100_000,
    )
    return runner


def _event(metadata=None):
    return MessageEvent(
        text="hello world",
        source=SessionSource(
            platform=Platform.TELEGRAM,
            chat_id="-1001",
            chat_type="group",
            user_id="12345",
        ),
        message_id="msg-admit",
        metadata=metadata or {},
    )


def _source():
    return SessionSource(
        platform=Platform.TELEGRAM,
        chat_id="-1001",
        chat_type="group",
        user_id="12345",
    )


_SESSION_KEY = "agent:main:telegram:group:-1001:12345"


@pytest.mark.asyncio
async def test_reaching_agent_turn_marks_admitted(monkeypatch, tmp_path):
    """A message that clears session resolution and inbound preprocessing and
    reaches the agent run is admitted before that run."""
    runner = _bootstrap(monkeypatch, tmp_path)

    async def _fake_run_agent(*_a, **kw):
        # The marker must already be set by the time the agent run begins.
        assert event.turn_admitted is True
        return {
            "final_response": "Hi!",
            "messages": [
                {"role": "user", "content": "hello world"},
                {"role": "assistant", "content": "Hi!"},
            ],
            "tools": [],
            "history_offset": 0,
            "last_prompt_tokens": 0,
        }

    runner._run_agent = AsyncMock(side_effect=_fake_run_agent)
    event = _event()

    await runner._handle_message_with_agent(event, _source(), _SESSION_KEY, 1)

    runner._run_agent.assert_awaited_once()
    assert event.turn_admitted is True


@pytest.mark.asyncio
async def test_preprocessing_none_does_not_admit(monkeypatch, tmp_path):
    """Inbound preprocessing returning None (e.g. the message reduced to
    nothing dispatchable) is a pre-admission early return: no admission, no
    agent run."""
    runner = _bootstrap(monkeypatch, tmp_path)
    runner._run_agent = AsyncMock()
    runner._prepare_profile_scoped_inbound_message_text = AsyncMock(
        return_value=None
    )
    event = _event()

    await runner._handle_message_with_agent(event, _source(), _SESSION_KEY, 1)

    runner._run_agent.assert_not_awaited()
    assert event.turn_admitted is False


@pytest.mark.asyncio
async def test_failed_delegated_session_resolution_does_not_admit(
    monkeypatch, tmp_path
):
    """A pinned delegated session that fails to resolve returns early before
    the agent turn — the message was never admitted."""
    runner = _bootstrap(monkeypatch, tmp_path)
    runner._run_agent = AsyncMock()
    runner._resolve_async_delegation_session = AsyncMock(return_value=None)
    event = _event(metadata={"gateway_session_id": "sess-delegated"})

    await runner._handle_message_with_agent(event, _source(), _SESSION_KEY, 1)

    runner._resolve_async_delegation_session.assert_awaited_once()
    runner._run_agent.assert_not_awaited()
    assert event.turn_admitted is False
