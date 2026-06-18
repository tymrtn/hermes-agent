"""Tests for Telegram Bot API 10.1 rich-message fast path."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.config import PlatformConfig
from gateway.platforms.telegram import TelegramAdapter


class FakeRichBot:
    def __init__(self):
        self._post = AsyncMock(return_value={"message_id": 4242})
        self.send_message = AsyncMock(return_value=SimpleNamespace(message_id=111))


@pytest.fixture()
def rich_adapter():
    config = PlatformConfig(enabled=True, token="fake-token")
    adapter = TelegramAdapter(config)
    adapter._bot = FakeRichBot()
    return adapter


@pytest.mark.asyncio
async def test_send_uses_rich_message_for_markdown_table(rich_adapter):
    content = "| A | B |\n|---|---|\n| 1 | 2 |"

    result = await rich_adapter.send("123", content, reply_to="99", metadata={"notify": True})

    assert result.success is True
    assert result.message_id == "4242"
    rich_adapter._bot._post.assert_awaited_once()
    endpoint, = rich_adapter._bot._post.call_args.args
    payload = rich_adapter._bot._post.call_args.kwargs["data"]
    assert endpoint == "sendRichMessage"
    assert payload["chat_id"] == 123
    assert payload["rich_message"] == {"markdown": content}
    assert payload["reply_parameters"] == {"message_id": 99}
    # notify=True means no disable_notification kwarg.
    assert "disable_notification" not in payload
    rich_adapter._bot.send_message.assert_not_called()


@pytest.mark.asyncio
async def test_send_rich_message_falls_back_to_markdown_send(rich_adapter):
    rich_adapter._bot._post.side_effect = RuntimeError("rich parse failed")
    content = "| A | B |\n|---|---|\n| 1 | 2 |"

    result = await rich_adapter.send("123", content, metadata={"notify": True})

    assert result.success is True
    assert result.message_id == "111"
    rich_adapter._bot._post.assert_awaited_once()
    rich_adapter._bot.send_message.assert_awaited_once()
    sent = rich_adapter._bot.send_message.call_args.kwargs["text"]
    assert "• B: 2" in sent
    assert "|---|" not in sent


@pytest.mark.asyncio
async def test_long_message_rich_failure_only_tries_rich_once(rich_adapter):
    rich_adapter._bot._post.side_effect = RuntimeError("rich unavailable")
    content = "x" * 5000

    result = await rich_adapter.send("123", content, metadata={"notify": True})

    assert result.success is True
    rich_adapter._bot._post.assert_awaited_once()
    assert rich_adapter._bot.send_message.await_count >= 2


@pytest.mark.asyncio
async def test_send_plain_short_message_uses_existing_send_message_path(rich_adapter):
    result = await rich_adapter.send("123", "plain hello", metadata={"notify": True})

    assert result.success is True
    assert result.message_id == "111"
    rich_adapter._bot._post.assert_not_called()
    rich_adapter._bot.send_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_rich_message_mode_can_be_disabled():
    config = PlatformConfig(enabled=True, token="fake-token", extra={"rich_messages": False})
    adapter = TelegramAdapter(config)
    adapter._bot = FakeRichBot()
    content = "| A | B |\n|---|---|\n| 1 | 2 |"

    result = await adapter.send("123", content, metadata={"notify": True})

    assert result.success is True
    assert result.message_id == "111"
    adapter._bot._post.assert_not_called()
    adapter._bot.send_message.assert_awaited_once()
