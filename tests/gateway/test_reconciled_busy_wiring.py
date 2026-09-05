"""Busy controls remain connected through the upstream callback and progress paths."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.busy_session_buttons import build_buttons_with_handles
from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import SendResult, SessionSource
from gateway.run_turn_runner import TurnRunner
from gateway.turn_context import TurnContext
from plugins.platforms.telegram.adapter import TelegramAdapter


@pytest.mark.asyncio
async def test_telegram_dispatcher_preserves_profile_and_topic_on_busy_tap():
    adapter = TelegramAdapter(PlatformConfig(enabled=True, token="fake-token"))
    adapter.gateway_runner = SimpleNamespace(_profile_name_for_source=lambda source: "research")
    adapter._is_callback_user_authorized = lambda *args, **kwargs: True
    adapter._busy_session_button_callback = AsyncMock(return_value="Stopped")
    key = "agent:research:telegram:dm:123:topic:456"
    keyboard = build_buttons_with_handles(key)
    adapter._busy_session_handles.update(keyboard.handle_map)
    stop = next(button for button in keyboard.buttons if button.primitive == "stop")
    query = SimpleNamespace(
        data=stop.callback_data, answer=AsyncMock(),
        from_user=SimpleNamespace(id=123, first_name="Tyler"),
        message=SimpleNamespace(chat_id=123, message_thread_id=456,
                                chat=SimpleNamespace(type="private")))

    await adapter._handle_callback_query(SimpleNamespace(callback_query=query), None)

    args = adapter._busy_session_button_callback.await_args.args
    assert args[:2] == (key, "stop")
    assert (args[2].profile, args[2].thread_id, args[2].user_id) == ("research", "456", "123")
    query.answer.assert_awaited_once_with(text="Stopped")


@pytest.mark.asyncio
async def test_progress_send_becomes_the_busy_control_anchor():
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="123")
    ctx = TurnContext(source=source, session_key="active-session")
    runner = SimpleNamespace(_tool_bubble_msg_ids={}, _ensure_busy_session_controls=AsyncMock())
    adapter = SimpleNamespace(
        send=AsyncMock(return_value=SendResult(success=True, message_id="42")),
        attach_busy_session_buttons=AsyncMock())
    turn = TurnRunner(runner, ctx)

    await turn._send_progress_text(SimpleNamespace(adapter=adapter), "Searching")

    assert runner._tool_bubble_msg_ids[ctx.session_key] == "42"
    args = runner._ensure_busy_session_controls.await_args.args
    assert args[0] == ctx.session_key and args[1].source is source
