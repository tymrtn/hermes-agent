"""A clarify tap that arrives too late still delivers the user's decision.

The pending-clarify registry is process memory. Once ``clarify_timeout`` evicts an entry — or the
gateway restarts — the buttons are still sitting in the Telegram chat, and tapping one used to
answer "⚠️ This prompt expired — please /retry", discarding a decision the user had just made.

The keyboard outlives the registry, so the tapped label is still recoverable from the message
itself. These tests pin that the recovered answer re-enters the gateway as an ordinary inbound
message from the person who tapped, routed to the session a typed reply would have reached, and
that it happens exactly once per prompt.
"""

import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

_repo = str(Path(__file__).resolve().parents[2])
if _repo not in sys.path:
    sys.path.insert(0, _repo)

from gateway.config import PlatformConfig
from gateway.platforms.event import MessageType
from plugins.platforms.telegram.adapter import TelegramAdapter

_CHAT = -100777
_TAPPER = 4242
_QUESTION = "Which deploy target?"
_CHOICES = ["staging", "production"]


def _clear_clarify_registry():
    from tools import clarify_gateway as cm

    with cm._lock:
        cm._entries.clear()
        cm._session_index.clear()
        cm._notify_cbs.clear()


def _make_adapter():
    adapter = TelegramAdapter(PlatformConfig(enabled=True, token="test-token", extra={}))
    adapter._bot = AsyncMock()
    adapter._bot.username = "hermes_bot"  # a real bot's is a str; an AsyncMock child is a coroutine
    adapter._app = MagicMock()
    return adapter


def _keyboard(clarify_id):
    """The prompt's own inline keyboard — the only place a late tap's meaning survives."""
    rows = [[SimpleNamespace(text=label, callback_data=f"cl:{clarify_id}:{i}")]
            for i, label in enumerate(_CHOICES)]
    rows.append([SimpleNamespace(text="✏️ Other…", callback_data=f"cl:{clarify_id}:other")])
    return SimpleNamespace(inline_keyboard=rows)


def _prompt_message(clarify_id, *, reply_markup=True):
    """The bot's own clarify prompt, as python-telegram-bot hands it back on a tap."""
    return SimpleNamespace(
        message_id=900, text=f"❓ {_QUESTION}", caption=None, entities=[], caption_entities=[],
        message_thread_id=None, is_topic_message=False, chat_id=_CHAT,
        chat=SimpleNamespace(id=_CHAT, type="group", title="Deploys", is_forum=False),
        # The prompt is the BOT's message: from_user is the bot, not the person tapping.
        from_user=SimpleNamespace(id=1, full_name="Hermes Bot", first_name="Hermes", is_bot=True),
        reply_to_message=None, date=None, location=None, photo=None, video=None, audio=None,
        voice=None, document=None, sticker=None, media_group_id=None,
        reply_markup=_keyboard(clarify_id) if reply_markup else None)


def _user_message(text="deploy it"):
    """What a real typed message from the same person in the same chat looks like."""
    return SimpleNamespace(
        message_id=901, text=text, caption=None, entities=[], caption_entities=[],
        message_thread_id=None, is_topic_message=False, chat_id=_CHAT,
        chat=SimpleNamespace(id=_CHAT, type="group", title="Deploys", is_forum=False),
        from_user=SimpleNamespace(id=_TAPPER, full_name="Tap Per", first_name="Tap", is_bot=False),
        reply_to_message=None, date=None, location=None, photo=None, video=None, audio=None,
        voice=None, document=None, sticker=None, media_group_id=None, reply_markup=None)


def _tap(adapter, clarify_id, choice_token, *, reply_markup=True):
    """Drive a real button tap through the adapter's own callback dispatch; returns (query, sent)."""
    query = AsyncMock()
    query.data = f"cl:{clarify_id}:{choice_token}"
    query.message = _prompt_message(clarify_id, reply_markup=reply_markup)
    query.from_user = SimpleNamespace(id=_TAPPER, full_name="Tap Per", first_name="Tap", is_bot=False)
    query.answer = AsyncMock()
    query.edit_message_text = AsyncMock()
    update = MagicMock()
    update.callback_query = query
    delivered = []
    # The same entry point a typed message uses once the adapter has built its event.
    adapter._enqueue_text_event = delivered.append
    return query, update, delivered


async def _dispatch(adapter, update):
    with patch.dict(os.environ, {"TELEGRAM_ALLOWED_USERS": "*"}, clear=False):
        await adapter._handle_callback_query(update, MagicMock())


def _answer_text(query):
    assert query.answer.await_count == 1, f"expected one answer, got {query.answer.await_args_list}"
    return query.answer.await_args.kwargs.get("text", "")


class TestLateTapIsDelivered:

    def setup_method(self):
        _clear_clarify_registry()

    @pytest.mark.asyncio
    async def test_evicted_entry_delivers_the_tapped_choice_as_a_new_message(self):
        """clarify_timeout evicted the entry: the decision reaches the agent anyway."""
        adapter = _make_adapter()
        adapter._clarify_state["cid-evicted"] = "sk-old"  # state survived; the registry did not
        query, update, delivered = _tap(adapter, "cid-evicted", "1")

        await _dispatch(adapter, update)

        assert len(delivered) == 1
        event = delivered[0]
        assert _QUESTION in event.text
        assert "production" in event.text
        assert "delivered as a new message" in _answer_text(query)

    @pytest.mark.asyncio
    async def test_post_restart_tap_delivers_too(self):
        """The gateway restarted: _clarify_state is empty and the buttons are still live in chat."""
        adapter = _make_adapter()
        query, update, delivered = _tap(adapter, "cid-restart", "0")

        await _dispatch(adapter, update)

        assert len(delivered) == 1
        assert "staging" in delivered[0].text

    @pytest.mark.asyncio
    async def test_the_delivered_message_is_authored_by_whoever_tapped(self):
        """The prompt is the bot's message. A delivery that inherited its author would key the wrong
        session and be dropped as bot traffic, so the tapper has to replace it."""
        adapter = _make_adapter()
        query, update, delivered = _tap(adapter, "cid-author", "0")

        await _dispatch(adapter, update)

        source = delivered[0].source
        assert source.user_id == str(_TAPPER)
        assert source.is_bot is False

    @pytest.mark.asyncio
    async def test_it_lands_in_the_session_a_typed_reply_would_have(self):
        """Routing parity — the whole point is that the agent picks this up as the next turn."""
        adapter = _make_adapter()
        query, update, delivered = _tap(adapter, "cid-route", "0")

        await _dispatch(adapter, update)

        typed = adapter._build_message_event(_user_message(), MessageType.TEXT)
        assert adapter._event_session_key(delivered[0]) == adapter._event_session_key(typed)

    @pytest.mark.asyncio
    async def test_the_real_event_builder_runs_so_thread_routing_is_not_guessed(self):
        """Built through _build_message_event, not hand-assembled: chat/thread routing stays correct
        as that builder evolves."""
        adapter = _make_adapter()
        query, update, delivered = _tap(adapter, "cid-build", "1")

        await _dispatch(adapter, update)

        source = delivered[0].source
        assert source.chat_id == str(_CHAT)
        assert source.chat_type == "group"
        assert source.chat_name == "Deploys"


class TestLateTapHappensOnce:

    def setup_method(self):
        _clear_clarify_registry()

    @pytest.mark.asyncio
    async def test_a_second_tap_after_delivery_delivers_nothing(self):
        """Claim-once: the ring remembers the prompt whose answer already went through."""
        adapter = _make_adapter()
        _q1, update1, delivered1 = _tap(adapter, "cid-twice", "1")
        await _dispatch(adapter, update1)
        assert len(delivered1) == 1

        q2, update2, delivered2 = _tap(adapter, "cid-twice", "1")
        await _dispatch(adapter, update2)

        assert delivered2 == []
        assert "already been resolved" in _answer_text(q2)

    @pytest.mark.asyncio
    async def test_a_double_tap_on_a_normally_resolved_prompt_delivers_nothing(self):
        """The resolve path pops _clarify_state before the edit strips the keyboard. A tap racing
        into that window must not be mistaken for a post-restart tap."""
        from tools import clarify_gateway as cm

        adapter = _make_adapter()
        cm.register("cid-race", "sk-live", _QUESTION, list(_CHOICES))
        adapter._clarify_state["cid-race"] = "sk-live"
        _q1, update1, delivered1 = _tap(adapter, "cid-race", "0")
        await _dispatch(adapter, update1)
        assert delivered1 == []  # resolved the live prompt; nothing to re-deliver
        with cm._lock:
            assert cm._entries["cid-race"].response == "staging"

        q2, update2, delivered2 = _tap(adapter, "cid-race", "0")
        await _dispatch(adapter, update2)

        assert delivered2 == []
        assert "already been resolved" in _answer_text(q2)


class TestLateTapWithNothingToRecover:

    def setup_method(self):
        _clear_clarify_registry()

    @pytest.mark.asyncio
    async def test_other_on_an_expired_prompt_points_at_typing(self):
        """"Other" never had a label to recover, and with no clarify pending a typed message is
        simply the next turn — so say that rather than /retry."""
        adapter = _make_adapter()
        query, update, delivered = _tap(adapter, "cid-other", "other")

        await _dispatch(adapter, update)

        assert delivered == []
        assert "type your answer" in _answer_text(query).lower()

    @pytest.mark.asyncio
    async def test_a_prompt_with_no_keyboard_left_still_says_it_expired(self):
        """Nothing recoverable: the honest expiry notice, never a false confirmation."""
        adapter = _make_adapter()
        query, update, delivered = _tap(adapter, "cid-bare", "1", reply_markup=False)

        await _dispatch(adapter, update)

        assert delivered == []
        assert "expired" in _answer_text(query).lower()
