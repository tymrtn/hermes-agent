"""Tests for Telegram inline keyboard clarify buttons.

Mirrors test_telegram_approval_buttons.py for the new ``send_clarify`` and
``cl:`` callback dispatch added in feat/clarify-gateway-buttons.
"""

import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Ensure the repo root is importable
# ---------------------------------------------------------------------------
_repo = str(Path(__file__).resolve().parents[2])
if _repo not in sys.path:
    sys.path.insert(0, _repo)


# ---------------------------------------------------------------------------
# Minimal Telegram mock so TelegramAdapter can be imported (mirrors
# test_telegram_approval_buttons.py)
# ---------------------------------------------------------------------------
from plugins.platforms.telegram.adapter import TelegramAdapter
from gateway.config import PlatformConfig


def _make_adapter(extra=None):
    config = PlatformConfig(enabled=True, token="test-token", extra=extra or {})
    adapter = TelegramAdapter(config)
    adapter._bot = AsyncMock()
    adapter._app = MagicMock()
    return adapter


def _clear_clarify_state():
    from tools import clarify_gateway as cm
    with cm._lock:
        cm._entries.clear()
        cm._session_index.clear()
        cm._notify_cbs.clear()


def _patch_keyboard(monkeypatch):
    import gateway.platforms.telegram as telegram_mod

    def button(text=None, callback_data=None, **kwargs):
        return SimpleNamespace(text=text, callback_data=callback_data, kwargs=kwargs)

    def markup(rows):
        return SimpleNamespace(inline_keyboard=rows)

    monkeypatch.setattr(telegram_mod, "InlineKeyboardButton", button)
    monkeypatch.setattr(telegram_mod, "InlineKeyboardMarkup", markup)


# ===========================================================================
# send_clarify — render
# ===========================================================================

class TestTelegramSendClarify:
    """Verify the rendered prompt has buttons or none, and stores state."""

    def setup_method(self):
        _clear_clarify_state()

    @pytest.mark.asyncio
    async def test_multi_choice_renders_choice_text_in_buttons_not_body(self, monkeypatch):
        """Choices belong in Telegram button labels, not duplicated in prompt text."""
        _patch_keyboard(monkeypatch)
        adapter = _make_adapter()
        mock_msg = MagicMock()
        mock_msg.message_id = 100
        adapter._bot.send_message = AsyncMock(return_value=mock_msg)

        result = await adapter.send_clarify(
            chat_id="12345",
            question="Which option?",
            choices=["alpha", "beta", "gamma"],
            clarify_id="cid1",
            session_key="sk1",
        )

        assert result.success is True
        assert result.message_id == "100"

        kwargs = adapter._bot.send_message.call_args[1]
        assert kwargs["chat_id"] == 12345
        assert "Which option?" in kwargs["text"]
        assert "1. alpha" not in kwargs["text"]
        assert "2. beta" not in kwargs["text"]
        assert "3. gamma" not in kwargs["text"]
        markup = kwargs["reply_markup"]
        labels = [row[0].text for row in markup.inline_keyboard]
        assert labels[:4] == [
            "1. alpha",
            "2. beta",
            "3. gamma",
            "✏️ Other (type answer)",
        ]
        assert "cid1" in adapter._clarify_state
        assert adapter._clarify_state["cid1"] == "sk1"

    @pytest.mark.asyncio
    async def test_multi_choice_does_not_include_busy_session_controls(self, monkeypatch):
        """Clarify prompts are their own input surface, not a busy-control surface."""
        _patch_keyboard(monkeypatch)
        adapter = _make_adapter({"busy_buttons": True})
        mock_msg = MagicMock()
        mock_msg.message_id = 100
        adapter._bot.send_message = AsyncMock(return_value=mock_msg)

        result = await adapter.send_clarify(
            chat_id="12345",
            question="Which option?",
            choices=["alpha", "beta"],
            clarify_id="cid-busy",
            session_key="telegram:12345:dm",
        )

        assert result.success is True
        rows = adapter._bot.send_message.call_args[1]["reply_markup"].inline_keyboard
        labels = [[button.text for button in row] for row in rows]
        assert labels == [
            ["1. alpha"],
            ["2. beta"],
            ["✏️ Other (type answer)"],
        ]
        callbacks = [button.callback_data for row in rows for button in row]
        assert not any(callback.startswith("bs:") for callback in callbacks)

    @pytest.mark.asyncio
    async def test_open_ended_does_not_include_busy_session_controls(self, monkeypatch):
        """Open-ended clarify prompts should accept chat replies without extra buttons."""
        _patch_keyboard(monkeypatch)
        adapter = _make_adapter({"busy_buttons": True})
        mock_msg = MagicMock()
        mock_msg.message_id = 101
        adapter._bot.send_message = AsyncMock(return_value=mock_msg)

        result = await adapter.send_clarify(
            chat_id="12345",
            question="What is your name?",
            choices=None,
            clarify_id="cid2",
            session_key="telegram:12345:dm",
        )

        assert result.success is True
        kwargs = adapter._bot.send_message.call_args[1]
        assert "What is your name?" in kwargs["text"]
        assert "reply_markup" not in kwargs
        assert adapter._clarify_state["cid2"] == "telegram:12345:dm"

    @pytest.mark.asyncio
    async def test_not_connected(self):
        adapter = _make_adapter()
        adapter._bot = None
        result = await adapter.send_clarify(
            chat_id="12345",
            question="?",
            choices=["a"],
            clarify_id="cid3",
            session_key="sk3",
        )
        assert result.success is False

    @pytest.mark.asyncio
    async def test_long_choice_truncated_in_button_label(self, monkeypatch):
        """Long choices still live in buttons, with labels shortened for mobile."""
        _patch_keyboard(monkeypatch)
        adapter = _make_adapter()
        mock_msg = MagicMock()
        mock_msg.message_id = 102
        adapter._bot.send_message = AsyncMock(return_value=mock_msg)

        long_choice = "x" * 200
        result = await adapter.send_clarify(
            chat_id="12345",
            question="?",
            choices=[long_choice],
            clarify_id="cid4",
            session_key="sk4",
        )
        assert result.success is True
        kwargs = adapter._bot.send_message.call_args[1]
        assert long_choice not in kwargs["text"]
        label = kwargs["reply_markup"].inline_keyboard[0][0].text
        assert label.startswith("1. ")
        assert label.endswith("…")
        assert len(label) <= 64

    @pytest.mark.asyncio
    async def test_html_escapes_question(self):
        adapter = _make_adapter()
        mock_msg = MagicMock()
        mock_msg.message_id = 103
        adapter._bot.send_message = AsyncMock(return_value=mock_msg)

        await adapter.send_clarify(
            chat_id="12345",
            question="<script>alert(1)</script>",
            choices=["x"],
            clarify_id="cid5",
            session_key="sk5",
        )
        kwargs = adapter._bot.send_message.call_args[1]
        # Must NOT contain raw <script> — html.escape should have neutralized
        assert "<script>" not in kwargs["text"]
        assert "&lt;script&gt;" in kwargs["text"]


# ===========================================================================
# Callback dispatch — _handle_callback_query routing for cl:* prefixes
# ===========================================================================

class TestTelegramClarifyCallback:
    """Verify clicking a button resolves the clarify primitive."""

    def setup_method(self):
        _clear_clarify_state()

    @pytest.mark.asyncio
    async def test_numeric_choice_resolves_with_choice_text(self):
        from tools import clarify_gateway as cm

        adapter = _make_adapter()
        # Pre-register a clarify entry so the callback can look up the choice text
        cm.register("cidA", "sk-cb", "Pick", ["red", "green", "blue"])
        adapter._clarify_state["cidA"] = "sk-cb"

        query = AsyncMock()
        query.data = "cl:cidA:1"  # green
        query.message = MagicMock()
        query.message.chat_id = 12345
        query.message.text = "Pick"
        query.from_user = MagicMock()
        query.from_user.id = "777"
        query.from_user.first_name = "Tester"
        query.answer = AsyncMock()
        query.edit_message_text = AsyncMock()

        update = MagicMock()
        update.callback_query = query
        context = MagicMock()

        with patch.dict(os.environ, {"TELEGRAM_ALLOWED_USERS": "*"}, clear=False):
            await adapter._handle_callback_query(update, context)

        # State popped
        assert "cidA" not in adapter._clarify_state
        # Wait shouldn't be needed — resolve_gateway_clarify is sync.
        # The entry's response should be set.
        # We test by reading the entry's response directly.
        with cm._lock:
            entry = cm._entries.get("cidA")
        # Entry might be popped by wait_for_response, but here we never
        # called wait — so it's still in _entries with response set.
        assert entry is not None
        assert entry.response == "green"
        assert entry.event.is_set()
        query.answer.assert_called_once()
        query.edit_message_text.assert_called_once()


    @pytest.mark.asyncio
    async def test_unauthorized_user_rejected(self):
        from tools import clarify_gateway as cm

        adapter = _make_adapter()
        cm.register("cidC", "sk-auth", "Pick", ["a", "b"])
        adapter._clarify_state["cidC"] = "sk-auth"

        # Hook up a runner that says NOT authorized
        class _DenyRunner:
            async def _handle_message(self, event):
                return None
            def _is_user_authorized(self, source):
                return False

        adapter._message_handler = _DenyRunner()._handle_message

        query = AsyncMock()
        query.data = "cl:cidC:0"
        query.message = MagicMock()
        query.message.chat_id = 12345
        query.message.chat.type = "private"
        query.message.text = "Pick"
        query.from_user = MagicMock()
        query.from_user.id = "999"
        query.from_user.first_name = "Mallory"
        query.answer = AsyncMock()
        query.edit_message_text = AsyncMock()

        update = MagicMock()
        update.callback_query = query
        context = MagicMock()

        await adapter._handle_callback_query(update, context)

        # Must not resolve, must answer with not-authorized message
        with cm._lock:
            entry = cm._entries.get("cidC")
        assert entry is not None
        assert not entry.event.is_set()
        query.answer.assert_called_once()
        assert "not authorized" in query.answer.call_args[1]["text"].lower()
        # State preserved
        assert adapter._clarify_state["cidC"] == "sk-auth"

    @pytest.mark.asyncio
    async def test_numeric_choice_expired_notifies_user(self):
        """Late tap after the entry was evicted (timeout) or the gateway
        restarted must surface an expiry notice, not a misleading ✓."""
        adapter = _make_adapter()
        # _clarify_state still maps the id (timeout eviction does not pop it),
        # but the clarify primitive entry is gone → resolve returns False.
        adapter._clarify_state["cidExpired"] = "sk-expired"

        query = AsyncMock()
        query.data = "cl:cidExpired:0"
        query.message = MagicMock()
        query.message.chat_id = 12345
        query.message.text = "Pick"
        query.from_user = MagicMock()
        query.from_user.id = "777"
        query.from_user.first_name = "Tester"
        query.answer = AsyncMock()
        query.edit_message_text = AsyncMock()

        update = MagicMock()
        update.callback_query = query
        context = MagicMock()

        with patch.dict(os.environ, {"TELEGRAM_ALLOWED_USERS": "*"}, clear=False):
            await adapter._handle_callback_query(update, context)

        # User is told the prompt expired — not a misleading checkmark.
        answer_text = query.answer.call_args[1]["text"].lower()
        assert "expired" in answer_text
        edit_text = query.edit_message_text.call_args[1]["text"].lower()
        assert "expired" in edit_text or "session reset" in edit_text
        assert "/retry" in edit_text

    @pytest.mark.asyncio
    async def test_other_button_expired_notifies_user(self):
        """Tapping 'Other' after the entry was evicted must tell the user the
        prompt expired instead of silently entering text-capture mode."""
        adapter = _make_adapter()
        # No clarify primitive entry → mark_awaiting_text returns False.
        adapter._clarify_state["cidOtherExpired"] = "sk-other-expired"

        query = AsyncMock()
        query.data = "cl:cidOtherExpired:other"
        query.message = MagicMock()
        query.message.chat_id = 12345
        query.message.text = "Pick"
        query.from_user = MagicMock()
        query.from_user.id = "777"
        query.from_user.first_name = "Tester"
        query.answer = AsyncMock()
        query.edit_message_text = AsyncMock()

        update = MagicMock()
        update.callback_query = query
        context = MagicMock()

        with patch.dict(os.environ, {"TELEGRAM_ALLOWED_USERS": "*"}, clear=False):
            await adapter._handle_callback_query(update, context)

        answer_text = query.answer.call_args[1]["text"].lower()
        assert "expired" in answer_text
        # State popped so a subsequent typed message is not mis-captured.
        assert "cidOtherExpired" not in adapter._clarify_state

    @pytest.mark.asyncio
    async def test_numeric_choice_expired_with_keyboard_injects_late_reply(self):
        """Late tap after entry eviction must deliver the recovered choice as
        a fresh inbound message instead of discarding the decision."""
        adapter = _make_adapter()
        adapter._clarify_state["cidLate"] = "sk-late"
        # No clarify registry entry → resolve_gateway_clarify returns False.

        fake_event = MagicMock()
        adapter._build_message_event = MagicMock(return_value=fake_event)
        adapter._enqueue_text_event = MagicMock()

        keyboard = SimpleNamespace(
            inline_keyboard=[
                [SimpleNamespace(text="red", callback_data="cl:cidLate:0")],
                [SimpleNamespace(text="green", callback_data="cl:cidLate:1")],
            ]
        )
        query = AsyncMock()
        query.data = "cl:cidLate:1"
        query.message = MagicMock()
        query.message.chat_id = 12345
        query.message.text = "Pick a color"
        query.message.reply_markup = keyboard
        query.from_user = MagicMock()
        query.from_user.id = "777"
        query.from_user.first_name = "Tester"
        query.answer = AsyncMock()
        query.edit_message_text = AsyncMock()

        update = MagicMock()
        update.callback_query = query
        context = MagicMock()

        with patch.dict(os.environ, {"TELEGRAM_ALLOWED_USERS": "*"}, clear=False):
            await adapter._handle_callback_query(update, context)

        # The recovered choice went into a synthetic inbound message.
        adapter._enqueue_text_event.assert_called_once_with(fake_event)
        shim = adapter._build_message_event.call_args[0][0]
        assert "green" in shim.text
        assert "Pick a color" in shim.text
        # The ack tells the user the answer was delivered, and the keyboard
        # is stripped so it cannot be tapped again.
        answer_text = query.answer.call_args[1]["text"]
        assert "delivered" in answer_text.lower()
        assert query.edit_message_text.call_args[1]["reply_markup"] is None

    @pytest.mark.asyncio
    async def test_post_restart_tap_injects_late_reply(self):
        """After a gateway restart _clarify_state is empty but the keyboard
        survives in chat — the tap must still deliver the decision."""
        adapter = _make_adapter()
        # No _clarify_state entry at all (restart wiped it).

        fake_event = MagicMock()
        adapter._build_message_event = MagicMock(return_value=fake_event)
        adapter._enqueue_text_event = MagicMock()

        keyboard = SimpleNamespace(
            inline_keyboard=[
                [SimpleNamespace(text="ship it", callback_data="cl:cidRestart:0")],
            ]
        )
        query = AsyncMock()
        query.data = "cl:cidRestart:0"
        query.message = MagicMock()
        query.message.chat_id = 12345
        query.message.text = "Deploy now?"
        query.message.reply_markup = keyboard
        query.from_user = MagicMock()
        query.from_user.id = "777"
        query.from_user.first_name = "Tester"
        query.answer = AsyncMock()
        query.edit_message_text = AsyncMock()

        update = MagicMock()
        update.callback_query = query
        context = MagicMock()

        with patch.dict(os.environ, {"TELEGRAM_ALLOWED_USERS": "*"}, clear=False):
            await adapter._handle_callback_query(update, context)

        adapter._enqueue_text_event.assert_called_once_with(fake_event)
        shim = adapter._build_message_event.call_args[0][0]
        assert "ship it" in shim.text
        answer_text = query.answer.call_args[1]["text"]
        assert "delivered" in answer_text.lower()

    @pytest.mark.asyncio
    async def test_double_tap_after_resolve_not_reinjected(self):
        """A second tap racing in after a successful resolve must not deliver
        a duplicate answer."""
        adapter = _make_adapter()
        adapter._recently_resolved_clarifies.append("cidDouble")
        adapter._enqueue_text_event = MagicMock()

        keyboard = SimpleNamespace(
            inline_keyboard=[
                [SimpleNamespace(text="yes", callback_data="cl:cidDouble:0")],
            ]
        )
        query = AsyncMock()
        query.data = "cl:cidDouble:0"
        query.message = MagicMock()
        query.message.chat_id = 12345
        query.message.text = "Sure?"
        query.message.reply_markup = keyboard
        query.from_user = MagicMock()
        query.from_user.id = "777"
        query.from_user.first_name = "Tester"
        query.answer = AsyncMock()

        update = MagicMock()
        update.callback_query = query
        context = MagicMock()

        with patch.dict(os.environ, {"TELEGRAM_ALLOWED_USERS": "*"}, clear=False):
            await adapter._handle_callback_query(update, context)

        adapter._enqueue_text_event.assert_not_called()
        assert "already" in query.answer.call_args[1]["text"].lower()

    @pytest.mark.asyncio
    async def test_inject_late_reply_builds_real_event(self):
        """The SimpleNamespace shim must survive the real _build_message_event."""
        adapter = _make_adapter()
        adapter._enqueue_text_event = MagicMock()

        chat = SimpleNamespace(
            id=12345, type="private", title=None, full_name="Tyler", is_forum=False
        )
        user = SimpleNamespace(id=777, full_name="Tyler", is_bot=False)
        message = SimpleNamespace(
            chat=chat,
            from_user=user,
            text="❓ Deploy now?",
            message_id=42,
            message_thread_id=None,
            is_topic_message=False,
            reply_to_message=None,
            quote=None,
            forward_origin=None,
            is_automatic_forward=False,
            reply_markup=None,
        )
        query = SimpleNamespace(message=message, from_user=user, data="cl:x:0")

        ok = await adapter._inject_late_clarify_reply(query, "ship it")

        assert ok is True
        adapter._enqueue_text_event.assert_called_once()
        event = adapter._enqueue_text_event.call_args[0][0]
        assert "ship it" in event.text
        assert "Deploy now?" in event.text
        assert event.source.chat_id == "12345"

    @pytest.mark.asyncio
    async def test_invalid_choice_token(self):
        from tools import clarify_gateway as cm

        adapter = _make_adapter()
        cm.register("cidD", "sk-inv", "Q?", ["a"])
        adapter._clarify_state["cidD"] = "sk-inv"

        query = AsyncMock()
        query.data = "cl:cidD:not-a-number"
        query.message = MagicMock()
        query.message.chat_id = 12345
        query.message.text = "Q?"
        query.from_user = MagicMock()
        query.from_user.id = "777"
        query.from_user.first_name = "Tester"
        query.answer = AsyncMock()

        update = MagicMock()
        update.callback_query = query
        context = MagicMock()

        with patch.dict(os.environ, {"TELEGRAM_ALLOWED_USERS": "*"}, clear=False):
            await adapter._handle_callback_query(update, context)

        with cm._lock:
            entry = cm._entries.get("cidD")
        assert entry is not None
        assert not entry.event.is_set()
        query.answer.assert_called_once()
        assert "invalid" in query.answer.call_args[1]["text"].lower()


# ===========================================================================
# Base adapter fallback render — text numbered list
# ===========================================================================

class TestBaseAdapterClarifyFallback:
    """Adapters without button overrides should render numbered text."""

    @pytest.mark.asyncio
    async def test_numbered_text_fallback(self):
        from gateway.platforms.base import BasePlatformAdapter, SendResult

        # Subclass just enough to instantiate
        class _Stub(BasePlatformAdapter):
            name = "stub"

            def __init__(self):
                # Skip base __init__ — we're not exercising it
                self.sent: list = []

            async def connect(self, *, is_reconnect: bool = False): pass
            async def disconnect(self): pass
            async def send(self, chat_id, content, **kw):
                self.sent.append({"chat_id": chat_id, "content": content})
                return SendResult(success=True, message_id="1")
            async def edit(self, *a, **k): return SendResult(success=False)
            async def get_history(self, *a, **k): return []
            async def get_chat_info(self, *a, **k): return {}

        adapter = _Stub()

        result = await adapter.send_clarify(
            chat_id="c",
            question="Pick a fruit",
            choices=["apple", "banana"],
            clarify_id="x",
            session_key="s",
        )
        assert result.success is True
        assert len(adapter.sent) == 1
        text = adapter.sent[0]["content"]
        assert "Pick a fruit" in text
        assert "1." in text and "apple" in text
        assert "2." in text and "banana" in text

    @pytest.mark.asyncio
    async def test_open_ended_fallback_renders_question_only(self):
        from gateway.platforms.base import BasePlatformAdapter, SendResult

        class _Stub(BasePlatformAdapter):
            name = "stub"
            def __init__(self):
                self.sent: list = []
            async def connect(self, *, is_reconnect: bool = False): pass
            async def disconnect(self): pass
            async def send(self, chat_id, content, **kw):
                self.sent.append(content)
                return SendResult(success=True, message_id="1")
            async def edit(self, *a, **k): return SendResult(success=False)
            async def get_history(self, *a, **k): return []
            async def get_chat_info(self, *a, **k): return {}

        adapter = _Stub()
        await adapter.send_clarify(
            chat_id="c",
            question="Free form?",
            choices=None,
            clarify_id="x",
            session_key="s",
        )
        assert "Free form?" in adapter.sent[0]
        # No numbered list — choices were empty
        assert "1." not in adapter.sent[0]

class TestTelegramEditMessageBusyControls:
    """Regression: streaming edits must preserve busy-session controls."""

    @pytest.mark.asyncio
    async def test_nonfinal_edit_reattaches_busy_keyboard(self):
        adapter = _make_adapter()
        keyboard = object()
        adapter._busy_session_button_map["sk1"] = "200"
        adapter._build_busy_session_keyboard = MagicMock(return_value=keyboard)
        adapter._bot.edit_message_text = AsyncMock()

        result = await adapter.edit_message("12345", "200", "working", finalize=False)

        assert result.success is True
        kwargs = adapter._bot.edit_message_text.call_args.kwargs
        assert kwargs["reply_markup"] is keyboard
        adapter._build_busy_session_keyboard.assert_called_once_with("sk1")
