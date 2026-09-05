"""Regression tests for clarify replies while a gateway session is busy."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import (
    BasePlatformAdapter,
    MessageEvent,
    MessageType,
    SendResult,
)
from gateway.session import SessionSource, build_session_key


class _ClarifyBypassAdapter(BasePlatformAdapter):
    def __init__(self):
        super().__init__(PlatformConfig(enabled=True, token="test"), Platform.TELEGRAM)

    async def connect(self):
        return True

    async def disconnect(self):
        pass

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        return SendResult(success=True, message_id="text")

    async def get_chat_info(self, chat_id):
        return {"id": chat_id, "type": "private"}


def _event(text="custom answer", message_type=MessageType.TEXT):
    return MessageEvent(
        text=text,
        message_type=message_type,
        source=SessionSource(
            platform=Platform.TELEGRAM,
            chat_id="12345",
            chat_type="private",
            user_id="user1",
        ),
        message_id="msg1",
    )


def _clear_clarify_state():
    from tools import clarify_gateway as cm

    with cm._lock:
        cm._entries.clear()
        cm._session_index.clear()
        cm._notify_cbs.clear()


@pytest.mark.asyncio
async def test_active_session_routes_typed_choice_clarify_reply_to_runner_not_busy_queue():
    """Typed text must resolve a pending choice clarify even while the agent is busy.

    Telegram button clarifies keep the adapter session active while the agent
    thread blocks on ``wait_for_response``.  If the adapter only bypasses for
    entries already marked ``awaiting_text``, typed replies to the visible
    multi-choice prompt are handled as busy follow-ups and the clarify wait is
    never resolved.
    """
    _clear_clarify_state()
    from tools import clarify_gateway as cm

    adapter = _ClarifyBypassAdapter()
    adapter._message_handler = AsyncMock(return_value="")
    adapter._busy_session_handler = AsyncMock(return_value=True)
    event = _event("None of those are valid options")
    session_key = build_session_key(
        event.source,
        group_sessions_per_user=adapter.config.extra.get("group_sessions_per_user", True),
        thread_sessions_per_user=adapter.config.extra.get("thread_sessions_per_user", False),
    )
    adapter._active_sessions[session_key] = asyncio.Event()
    cm.register("clarify-1", session_key, "Pick one", ["A", "B"])

    await adapter.handle_message(event)

    adapter._message_handler.assert_awaited_once_with(event)
    adapter._busy_session_handler.assert_not_awaited()
    assert adapter._pending_messages == {}


@pytest.mark.asyncio
async def test_active_session_bypass_uses_profile_namespaced_key_under_multiplex():
    """Regression for issue #82975: under a named-profile multiplex, the
    adapter's clarify bypass lookup must use the SAME profile-namespaced
    session key that the runner registers pending clarifies under
    (SessionStore._generate_session_key() includes
    profile=self._resolve_profile_for_key(source)), not the legacy
    unnamespaced key. Otherwise the lookup misses, and a user's answer to
    a pending clarify is routed to the busy-session queue instead of
    resolving it -- the turn then hangs until the clarify's 3600s timeout."""
    _clear_clarify_state()
    from tools import clarify_gateway as cm

    adapter = _ClarifyBypassAdapter()
    adapter._message_handler = AsyncMock(return_value="")
    adapter._busy_session_handler = AsyncMock(return_value=True)
    event = _event("None of those are valid options")

    # A session_store configured for profile multiplexing, matching what
    # the runner's SessionStore._generate_session_key() actually produces.
    session_store = MagicMock()
    session_store._resolve_profile_for_key.return_value = "ops"
    adapter._session_store = session_store

    profile_namespaced_key = build_session_key(
        event.source,
        group_sessions_per_user=adapter.config.extra.get("group_sessions_per_user", True),
        thread_sessions_per_user=adapter.config.extra.get("thread_sessions_per_user", False),
        profile="ops",
    )
    # Sanity: the profile-namespaced key really is different from the
    # legacy unnamespaced one -- otherwise this test wouldn't distinguish
    # the fixed behavior from the bug.
    legacy_key = build_session_key(
        event.source,
        group_sessions_per_user=adapter.config.extra.get("group_sessions_per_user", True),
        thread_sessions_per_user=adapter.config.extra.get("thread_sessions_per_user", False),
    )
    assert profile_namespaced_key != legacy_key

    adapter._active_sessions[profile_namespaced_key] = asyncio.Event()
    # The runner registers the pending clarify under its own
    # profile-namespaced key, exactly as it would in a real multiplexed
    # deployment.
    cm.register("clarify-1", profile_namespaced_key, "Pick one", ["A", "B"])

    await adapter.handle_message(event)

    adapter._message_handler.assert_awaited_once_with(event)
    adapter._busy_session_handler.assert_not_awaited()
    assert adapter._pending_messages == {}




@pytest.mark.asyncio
async def test_accepted_clarify_reply_marks_turn_admitted():
    """An authorized, resolved clarify reply must carry the runner's
    turn-admission marker so adapter-side deferred commits (e.g. Slack
    thread watermarks) know the message was actually consumed."""
    _clear_clarify_state()
    from gateway.run import GatewayRunner
    from tools import clarify_gateway as cm

    adapter = _ClarifyBypassAdapter()
    event = _event("the missing details")
    assert event.turn_admitted is False

    runner = GatewayRunner.__new__(GatewayRunner)
    runner._startup_restore_in_progress = False
    runner._scale_to_zero_note_real_inbound = lambda: None
    runner._is_user_authorized = lambda source: True
    runner._session_key_for_source = lambda source: "clarify-admit"
    runner._adapter_for_source = lambda source: adapter
    runner._update_prompt_pending = {}

    cm.register("clarify-admit-1", "clarify-admit", "What is missing?", None)
    try:
        with patch("hermes_cli.plugins.invoke_hook", return_value=[]):
            result = await runner._handle_message(event)
    finally:
        _clear_clarify_state()

    assert result == ""
    assert event.turn_admitted is True



@pytest.mark.asyncio
async def test_accepted_clarify_reply_with_context_drops_staged_watermark():
    """A clarify reply may carry freshly-fetched channel_context that the
    clarify resolver never delivers to the waiting agent (only the reply text
    is passed through). Committing the staged thread-watermark would mark that
    unseen backfill consumed — silently dropping it from every future refresh.
    The staged commit must be dropped so the context stays refresh-eligible."""
    _clear_clarify_state()
    from gateway.platforms.base import STAGED_WATERMARK_COMMIT_KEY
    from gateway.run import GatewayRunner
    from tools import clarify_gateway as cm

    adapter = _ClarifyBypassAdapter()
    event = MessageEvent(
        text="the missing details",
        message_type=MessageType.TEXT,
        source=SessionSource(
            platform=Platform.TELEGRAM,
            chat_id="12345",
            chat_type="private",
            user_id="user1",
        ),
        message_id="msg1",
        channel_context="[Recent channel messages]\n[Alice] unseen backfill",
        metadata={STAGED_WATERMARK_COMMIT_KEY: {"watermark_ts": "123.456"}},
    )

    runner = GatewayRunner.__new__(GatewayRunner)
    runner._startup_restore_in_progress = False
    runner._scale_to_zero_note_real_inbound = lambda: None
    runner._is_user_authorized = lambda source: True
    runner._session_key_for_source = lambda source: "clarify-ctx"
    runner._adapter_for_source = lambda source: adapter
    runner._update_prompt_pending = {}

    cm.register("clarify-ctx-1", "clarify-ctx", "What is missing?", None)
    try:
        with patch("hermes_cli.plugins.invoke_hook", return_value=[]):
            result = await runner._handle_message(event)
    finally:
        _clear_clarify_state()

    # Reply still accepted and admitted — only the unseen-context commit is cut.
    assert result == ""
    assert event.turn_admitted is True
    assert STAGED_WATERMARK_COMMIT_KEY not in event.metadata



@pytest.mark.asyncio
async def test_accepted_clarify_reply_without_context_keeps_staged_watermark():
    """A clarify reply with NO recovered channel_context has nothing unseen —
    its staged watermark must survive so the thread watermark still advances
    (steady-state) and the reply isn't re-injected later."""
    _clear_clarify_state()
    from gateway.platforms.base import STAGED_WATERMARK_COMMIT_KEY
    from gateway.run import GatewayRunner
    from tools import clarify_gateway as cm

    adapter = _ClarifyBypassAdapter()
    event = MessageEvent(
        text="the missing details",
        message_type=MessageType.TEXT,
        source=SessionSource(
            platform=Platform.TELEGRAM,
            chat_id="12345",
            chat_type="private",
            user_id="user1",
        ),
        message_id="msg1",
        channel_context=None,
        metadata={STAGED_WATERMARK_COMMIT_KEY: {"watermark_ts": "123.456"}},
    )

    runner = GatewayRunner.__new__(GatewayRunner)
    runner._startup_restore_in_progress = False
    runner._scale_to_zero_note_real_inbound = lambda: None
    runner._is_user_authorized = lambda source: True
    runner._session_key_for_source = lambda source: "clarify-noctx"
    runner._adapter_for_source = lambda source: adapter
    runner._update_prompt_pending = {}

    cm.register("clarify-noctx-1", "clarify-noctx", "What is missing?", None)
    try:
        with patch("hermes_cli.plugins.invoke_hook", return_value=[]):
            result = await runner._handle_message(event)
    finally:
        _clear_clarify_state()

    assert result == ""
    assert event.turn_admitted is True
    assert STAGED_WATERMARK_COMMIT_KEY in event.metadata



@pytest.mark.asyncio
async def test_active_session_routes_voice_clarify_reply_to_runner_not_busy_queue():
    """Transcribed voice replies must resolve clarify instead of pausing behind busy handling."""
    _clear_clarify_state()
    from tools import clarify_gateway as cm

    adapter = _ClarifyBypassAdapter()
    adapter._message_handler = AsyncMock(return_value="")
    adapter._busy_session_handler = AsyncMock(return_value=True)
    event = _event("Use the second option", MessageType.VOICE)
    session_key = build_session_key(
        event.source,
        group_sessions_per_user=adapter.config.extra.get("group_sessions_per_user", True),
        thread_sessions_per_user=adapter.config.extra.get("thread_sessions_per_user", False),
    )
    adapter._active_sessions[session_key] = asyncio.Event()
    cm.register("clarify-voice", session_key, "Pick one", ["A", "B"])

    await adapter.handle_message(event)

    adapter._message_handler.assert_awaited_once_with(event)
    adapter._busy_session_handler.assert_not_awaited()
    assert adapter._pending_messages == {}



@pytest.mark.asyncio
async def test_gateway_clarify_reply_resumes_typing_before_returning_empty_ack():
    """A clarify answer must re-enable the active run's typing indicator.

    Clarify pauses typing while waiting so Slack's Assistant API does not
    disable the compose box. The typed answer is intercepted by the gateway
    and returns an empty acknowledgment instead of starting a second run; that
    interception path must therefore resume the original run's indicator.
    """
    _clear_clarify_state()
    from gateway.run import GatewayRunner
    from tools import clarify_gateway as cm

    adapter = _ClarifyBypassAdapter()
    adapter.pause_typing_for_chat("12345")
    event = _event("the missing details")

    runner = GatewayRunner.__new__(GatewayRunner)
    runner._startup_restore_in_progress = False
    runner._scale_to_zero_note_real_inbound = lambda: None
    runner._is_user_authorized = lambda source: True
    runner._session_key_for_source = lambda source: "clarify-session"
    runner._adapter_for_source = lambda source: adapter
    runner._update_prompt_pending = {}

    cm.register("clarify-2", "clarify-session", "What is missing?", None)

    with patch("hermes_cli.plugins.invoke_hook", return_value=[]):
        result = await runner._handle_message(event)

    assert result == ""
    assert "12345" not in adapter._typing_paused



@pytest.mark.asyncio
async def test_session_resolution_stage_does_not_admit_turn():
    """Turn admission is deferred until session resolution AND inbound
    preprocessing succeed. A failure during early session resolution (here,
    Telegram topic recovery) must leave the message UNadmitted, so a deferred
    adapter commit never fires for a turn that never reached the agent.

    (The positive path — reaching the agent run marks the turn admitted — is
    covered end-to-end in tests/gateway/test_turn_admission_lifecycle.py.)"""
    from gateway.run import GatewayRunner

    event = _event("run the report")
    assert event.turn_admitted is False

    runner = GatewayRunner.__new__(GatewayRunner)

    def _stop_here(source):
        raise RuntimeError("stop-before-admission")

    # Raising in the first session-resolution step proves admission has NOT
    # yet been marked at this early stage.
    runner._recover_telegram_topic_thread_id = _stop_here

    with pytest.raises(RuntimeError, match="stop-before-admission"):
        await GatewayRunner._handle_message_with_agent(
            runner, event, event.source, "session-k", 1
        )

    assert event.turn_admitted is False



@pytest.mark.asyncio
async def test_unauthorized_clarify_reply_with_context_not_admitted():
    """An unauthorized sender's reply is rejected before the clarify-accept
    path — it is never admitted, so its staged watermark commit never fires
    (the deferred commit is gated on turn_admitted downstream)."""
    _clear_clarify_state()
    from gateway.platforms.base import STAGED_WATERMARK_COMMIT_KEY
    from gateway.run import GatewayRunner

    event = MessageEvent(
        text="the missing details",
        message_type=MessageType.TEXT,
        source=SessionSource(
            platform=Platform.TELEGRAM,
            chat_id="G-1",
            chat_type="group",
            user_id="intruder",
        ),
        message_id="msg-x",
        channel_context="[Recent channel messages]\n[Alice] unseen backfill",
        metadata={STAGED_WATERMARK_COMMIT_KEY: {"watermark_ts": "123.456"}},
    )

    runner = GatewayRunner.__new__(GatewayRunner)
    runner._startup_restore_in_progress = False
    runner._scale_to_zero_note_real_inbound = lambda: None
    runner._is_user_authorized = lambda source: False

    with patch("hermes_cli.plugins.invoke_hook", return_value=[]):
        result = await runner._handle_message(event)

    assert result is None
    assert event.turn_admitted is False



@pytest.mark.asyncio
async def test_unauthorized_message_is_not_turn_admitted():
    """Gateway authorization rejection returns None (classified as a SUCCESS
    outcome by the base lifecycle) — the admission marker must stay unset so
    deferred adapter commits treat the message as rejected."""
    from gateway.run import GatewayRunner

    event = MessageEvent(
        text="hello",
        message_type=MessageType.TEXT,
        source=SessionSource(
            platform=Platform.TELEGRAM,
            chat_id="G-1",
            chat_type="group",
            user_id="intruder",
        ),
        message_id="msg-x",
    )

    runner = GatewayRunner.__new__(GatewayRunner)
    runner._startup_restore_in_progress = False
    runner._scale_to_zero_note_real_inbound = lambda: None
    runner._is_user_authorized = lambda source: False

    with patch("hermes_cli.plugins.invoke_hook", return_value=[]):
        result = await runner._handle_message(event)

    assert result is None
    assert event.turn_admitted is False
