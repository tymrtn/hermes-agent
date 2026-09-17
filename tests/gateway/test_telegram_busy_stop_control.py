"""Telegram busy-run Stop control on the ``/queue`` acknowledgement.

The smallest per-message busy affordance on a messaging platform: while
``busy_input_mode`` is explicitly configured to ``queue``, the
``/queue`` acknowledgement carries one "Stop current run" button whose
callback_data is an opaque token bound to the originating run and to the
authorized source that queued it.  A tap runs the SAME ``/stop`` lifecycle a
typed ``/stop`` runs; stale, replayed and foreign taps are inert.

Behavioral throughout: every tap goes through the real
``_handle_callback_query`` dispatch, and every stop is observed on the running
agent and the session lock rather than on a mocked primitive.
"""

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import SessionSource, build_session_key, unauthorized_action_notice
from gateway.platforms.event import MessageEvent, MessageType

_UNAUTHORIZED = unauthorized_action_notice(Platform.TELEGRAM)
_OWNER = "owner-1"
_CHAT = "555"


# ---------------------------------------------------------------------------
# Harness — a real TelegramAdapter wired to a bare GatewayRunner
# ---------------------------------------------------------------------------

def _make_runner(busy_mode="queue"):
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner.config = MagicMock()
    runner.config.multiplex_profiles = False
    runner.session_store = None
    runner.adapters = {}
    runner._draining = False
    runner._external_drain_active = False
    runner.hooks = MagicMock()
    runner.hooks.emit = AsyncMock()
    runner._is_user_authorized = lambda source: source.user_id == _OWNER
    runner._busy_input_mode = busy_mode
    runner._busy_text_mode = "queue" if busy_mode == "queue" else "interrupt"
    return runner


def _make_adapter(runner):
    from plugins.platforms.telegram.adapter import TelegramAdapter

    adapter = TelegramAdapter(PlatformConfig(enabled=True, token="test-token", extra={}))
    adapter._bot = AsyncMock()
    adapter._app = MagicMock()
    sent = MagicMock()
    sent.message_id = 9001
    adapter._bot.send_message = AsyncMock(return_value=sent)
    adapter.gateway_runner = runner
    adapter._message_handler = runner._handle_message
    runner.adapters[Platform.TELEGRAM] = adapter
    return adapter


def _source(user_id=_OWNER):
    return SessionSource(
        platform=Platform.TELEGRAM, user_id=user_id, chat_id=_CHAT,
        user_name="Owner", chat_type="dm",
    )


def _start_run(runner, adapter, source, agent=None):
    """Put ``source``'s session into a running turn — runner slot plus the adapter's own
    active-session guard, as a real in-flight turn holds both. Returns (session_key, agent)."""
    session_key = build_session_key(source)
    assert session_key == adapter._event_session_key(
        MessageEvent(text="x", message_type=MessageType.TEXT, source=source))
    agent = agent or MagicMock()
    state = runner._session_state(session_key)
    state.turn.agent = agent
    state.turn.started_ts = time.time()
    runner._begin_session_run_generation(session_key)
    adapter._active_sessions[session_key] = asyncio.Event()
    return session_key, agent


async def _queue(runner, source, text="/queue write the report"):
    """Send a ``/queue`` through the real busy command resolution path."""
    return await runner._handle_message(
        MessageEvent(text=text, message_type=MessageType.TEXT, source=source, message_id="1001")
    )


def _stop_control_call(adapter):
    """The ``send_message`` kwargs of the buttoned /queue ack, or None."""
    for call in adapter._bot.send_message.call_args_list:
        if call.kwargs.get("reply_markup") is not None:
            return call.kwargs
    return None


def _control_tokens(adapter):
    """Every Stop button ``callback_data`` the adapter has rendered, oldest ack first."""
    from plugins.platforms.telegram import adapter as tg_adapter

    rendered = []
    for call in adapter._bot.send_message.call_args_list:
        rows = getattr(call.kwargs.get("reply_markup"), "inline_keyboard", None)
        if isinstance(rows, (list, tuple)):  # real python-telegram-bot
            rendered.extend(b.callback_data for row in rows for b in row)
    if not rendered:
        # tests/gateway/conftest.py mocks the telegram package, so the markup has no readable
        # structure — read what the adapter handed the button constructor instead.
        rendered = [c.kwargs.get("callback_data") for c in tg_adapter.InlineKeyboardButton.call_args_list]
    tokens = [d for d in rendered if str(d).startswith("bs:")]
    assert tokens, f"no Stop button was rendered, got {rendered}"
    return tokens


def _control_token(adapter):
    """The single Stop button's ``callback_data`` on the buttoned /queue ack."""
    from plugins.platforms.telegram import adapter as tg_adapter

    kwargs = _stop_control_call(adapter)
    assert kwargs is not None, "no buttoned /queue acknowledgement was sent"
    rows = getattr(kwargs["reply_markup"], "inline_keyboard", None)
    if isinstance(rows, (list, tuple)):  # real python-telegram-bot
        assert len(rows) == 1 and len(rows[0]) == 1, f"expected exactly one button, got {rows}"
        return rows[0][0].callback_data
    # tests/gateway/conftest.py mocks the telegram package, so the markup has no readable
    # structure — read what the adapter handed the button constructor instead.
    built = [c.kwargs.get("callback_data") for c in tg_adapter.InlineKeyboardButton.call_args_list]
    stop = [d for d in built if str(d).startswith("bs:")]
    assert len(stop) == 1, f"expected exactly one Stop button, got {built}"
    return stop[0]


@pytest.fixture(autouse=True)
def _reset_button_constructor():
    """The mocked telegram package is module-global; keep per-test button calls separate."""
    from plugins.platforms.telegram import adapter as tg_adapter

    reset = getattr(tg_adapter.InlineKeyboardButton, "reset_mock", None)
    if reset is not None:
        reset()
    yield


def _tap(adapter, token, user_id=_OWNER):
    """Drive a button tap through the real callback dispatch; returns the query."""
    query = AsyncMock()
    query.data = token
    query.message = MagicMock()
    query.message.chat_id = int(_CHAT)
    query.message.chat.type = "private"
    query.message.message_thread_id = None
    query.message.text = "Queued for the next turn."
    query.from_user = MagicMock()
    query.from_user.id = user_id
    query.from_user.first_name = "Owner"
    query.answer = AsyncMock()
    query.edit_message_text = AsyncMock()
    update = MagicMock()
    update.callback_query = query
    return query, update


def _answer_text(query):
    assert query.answer.await_count == 1, f"expected one answer, got {query.answer.await_args_list}"
    return query.answer.await_args.kwargs.get("text", "")


# ---------------------------------------------------------------------------
# Render
# ---------------------------------------------------------------------------

class TestQueueAckRendersStopControl:

    @pytest.mark.asyncio
    async def test_queue_mode_ack_carries_one_bound_stop_button(self):
        """Configured queue mode: the /queue ack is sent with one short-token Stop button."""
        runner = _make_runner("queue")
        adapter = _make_adapter(runner)
        source = _source()
        session_key, _agent = _start_run(runner, adapter, source)

        reply = await _queue(runner, source)

        # The buttoned ack is the acknowledgement; nothing is left for the inline-reply send.
        assert reply == ""
        kwargs = _stop_control_call(adapter)
        assert "Queued" in kwargs["text"]
        token = _control_token(adapter)
        assert token.startswith("bs:")
        assert len(token.encode()) <= 64
        # The follow-up still went through the FIFO exactly as a plain /queue does.
        assert runner._queue_depth(session_key, adapter=adapter) == 1

    @pytest.mark.asyncio
    async def test_default_interrupt_mode_renders_no_button(self):
        """#40590's interrupt default is untouched: no control, plain text ack."""
        runner = _make_runner("interrupt")
        adapter = _make_adapter(runner)
        source = _source()
        _start_run(runner, adapter, source)

        reply = await _queue(runner, source)

        assert "Queued for the next turn." in str(reply)
        assert _stop_control_call(adapter) is None

    @pytest.mark.asyncio
    async def test_adapter_without_button_support_gets_the_plain_ack(self):
        """The base template method is a no-op default: non-button adapters are unchanged."""
        from gateway.platforms.base import BasePlatformAdapter, SendResult

        class _Stub(BasePlatformAdapter):
            name = "stub"
            platform = Platform.TELEGRAM

            def __init__(self):
                self._pending_messages = {}
                self.sent = []

            async def connect(self, *, is_reconnect=False): pass
            async def disconnect(self): pass
            async def send(self, chat_id, content, **kw):
                self.sent.append(content)
                return SendResult(success=True, message_id="1")
            async def edit(self, *a, **k): return SendResult(success=False)
            async def get_history(self, *a, **k): return []
            async def get_chat_info(self, *a, **k): return {}

        assert _Stub.supports_busy_stop_control() is False
        runner = _make_runner("queue")
        stub = _Stub()
        runner.adapters[Platform.TELEGRAM] = stub
        source = _source()
        state = runner._session_state(build_session_key(source))
        state.turn.agent, state.turn.started_ts = MagicMock(), time.time()
        runner._begin_session_run_generation(build_session_key(source))

        reply = await _queue(runner, source)

        assert "Queued for the next turn." in str(reply)
        assert stub.sent == []

    @pytest.mark.asyncio
    async def test_adapter_that_does_not_implement_the_hook_still_gets_an_ack(self):
        """Not every adapter in the wild is a BasePlatformAdapter subclass. Probing for the hook
        must never turn a working /queue into an AttributeError."""
        runner = _make_runner("queue")
        bare = MagicMock()
        bare._pending_messages = {}
        runner.adapters[Platform.TELEGRAM] = bare
        source = _source()
        state = runner._session_state(build_session_key(source))
        state.turn.agent, state.turn.started_ts = MagicMock(), time.time()

        reply = await _queue(runner, source)

        assert "Queued for the next turn." in str(reply)


# ---------------------------------------------------------------------------
# Tap — the full /stop lifecycle
# ---------------------------------------------------------------------------

class TestStopControlTap:

    @pytest.mark.asyncio
    async def test_tap_runs_the_full_stop_lifecycle(self, monkeypatch):
        """A tap stops the run exactly as a typed /stop does: agent interrupted, lock released."""
        runner = _make_runner("queue")
        adapter = _make_adapter(runner)
        source = _source()
        session_key, agent = _start_run(runner, adapter, source)
        monkeypatch.setattr(adapter, "_is_callback_user_authorized", lambda *a, **k: True)
        assert await _queue(runner, source) == ""
        token = _control_token(adapter)

        query, update = _tap(adapter, token)
        await adapter._handle_callback_query(update, MagicMock())

        assert agent.interrupt.call_args_list == [(("Stop requested",), {})]
        assert runner._is_session_running(session_key) is False
        # The tail of _dispatch_active_session_command: the reply is delivered, then the adapter's
        # own session guard is released and the queue drained — a bare _busy_stop_command would
        # leave both behind.
        sent = [c.kwargs.get("text", "") for c in adapter._bot.send_message.call_args_list]
        assert any("Stopped" in text for text in sent), sent  # MarkdownV2-escaped on the wire
        assert session_key not in adapter._active_sessions
        assert "Stopped" in _answer_text(query)
        # The spent button is stripped and the acknowledgement it rode on is kept.
        edited = query.edit_message_text.await_args.kwargs["text"]
        assert "Queued for the next turn" in edited and "Stopped" in edited

    @pytest.mark.asyncio
    async def test_tap_leaves_the_queue_exactly_where_typed_stop_leaves_it(self, monkeypatch):
        """Parity, not a softer stop: the button is the same command, so it treats the follow-up
        it was attached to exactly as typing /stop treats it."""
        async def _run(stop):
            runner = _make_runner("queue")
            adapter = _make_adapter(runner)
            source = _source()
            session_key, _agent = _start_run(runner, adapter, source)
            monkeypatch.setattr(adapter, "_is_callback_user_authorized", lambda *a, **k: True)
            await _queue(runner, source)
            await stop(runner, adapter, source, session_key)
            return (runner._queue_depth(session_key, adapter=adapter),
                    adapter._pending_messages.get(session_key))

        async def _typed(runner, adapter, source, _session_key):
            await adapter.handle_message(MessageEvent(
                text="/stop", message_type=MessageType.TEXT, source=source, message_id="1002"))

        async def _tapped(_runner, adapter, _source, _session_key):
            _query, update = _tap(adapter, _control_token(adapter))
            await adapter._handle_callback_query(update, MagicMock())

        assert await _run(_tapped) == await _run(_typed)

    @pytest.mark.asyncio
    async def test_tap_discards_only_the_follow_up_due_to_run_next(self, monkeypatch):
        """The documented pending-input contract, pinned at depth > 1.

        ``/stop`` clears the queued message that was about to run and leaves the ones behind it
        (the runner drops the pending slot, never the FIFO overflow). The button inherits that
        exactly, so the user guide can promise it.
        """
        runner = _make_runner("queue")
        adapter = _make_adapter(runner)
        source = _source()
        session_key, _agent = _start_run(runner, adapter, source)
        monkeypatch.setattr(adapter, "_is_callback_user_authorized", lambda *a, **k: True)
        for prompt in ("first", "second", "third"):
            await _queue(runner, source, f"/queue {prompt}")
        assert runner._queue_depth(session_key, adapter=adapter) == 3

        # Three acks, three tokens; tap the first one's.
        _query, update = _tap(adapter, _control_tokens(adapter)[0])
        await adapter._handle_callback_query(update, MagicMock())

        assert runner._queue_depth(session_key, adapter=adapter) == 2
        survivors = [e.text for e in runner._overflow_queue(session_key)]
        assert survivors == ["second", "third"]

    @pytest.mark.asyncio
    async def test_replayed_tap_is_inert(self, monkeypatch):
        """Claim-once: the second tap of the same token stops nothing."""
        runner = _make_runner("queue")
        adapter = _make_adapter(runner)
        source = _source()
        _start_run(runner, adapter, source)
        monkeypatch.setattr(adapter, "_is_callback_user_authorized", lambda *a, **k: True)
        await _queue(runner, source)
        token = _control_token(adapter)

        first, first_update = _tap(adapter, token)
        await adapter._handle_callback_query(first_update, MagicMock())
        # A NEW run starts; the stale token must not reach it.
        _session_key, second_agent = _start_run(runner, adapter, source, agent=MagicMock())
        replay, replay_update = _tap(adapter, token)
        await adapter._handle_callback_query(replay_update, MagicMock())

        second_agent.interrupt.assert_not_called()
        assert "already" in _answer_text(replay).lower()

    @pytest.mark.asyncio
    async def test_tap_after_that_run_ended_is_inert(self, monkeypatch):
        """Generation-scoped: a token minted for run N never stops run N+1."""
        runner = _make_runner("queue")
        adapter = _make_adapter(runner)
        source = _source()
        session_key, first_agent = _start_run(runner, adapter, source)
        monkeypatch.setattr(adapter, "_is_callback_user_authorized", lambda *a, **k: True)
        await _queue(runner, source)
        token = _control_token(adapter)

        # That run finishes and a different one takes the slot.
        runner._release_running_agent_state(session_key)
        _session_key, successor = _start_run(runner, adapter, source, agent=MagicMock())
        query, update = _tap(adapter, token)
        await adapter._handle_callback_query(update, MagicMock())

        first_agent.interrupt.assert_not_called()
        successor.interrupt.assert_not_called()
        assert runner._is_session_running(session_key) is True
        assert "finished" in _answer_text(query).lower()

    @pytest.mark.asyncio
    async def test_tap_by_another_user_is_refused_and_the_control_survives(self, monkeypatch):
        """#17775 in button form: only the source that queued it may spend the control."""
        runner = _make_runner("queue")
        adapter = _make_adapter(runner)
        source = _source()
        session_key, agent = _start_run(runner, adapter, source)
        monkeypatch.setattr(adapter, "_is_callback_user_authorized", lambda *a, **k: True)
        await _queue(runner, source)
        token = _control_token(adapter)

        intruder, intruder_update = _tap(adapter, token, user_id="intruder-9")
        await adapter._handle_callback_query(intruder_update, MagicMock())

        agent.interrupt.assert_not_called()
        assert runner._is_session_running(session_key) is True
        assert _answer_text(intruder) == _UNAUTHORIZED
        intruder.edit_message_text.assert_not_awaited()  # the owner's button stays live

        owner, owner_update = _tap(adapter, token)
        await adapter._handle_callback_query(owner_update, MagicMock())
        assert agent.interrupt.call_args_list == [(("Stop requested",), {})]

    @pytest.mark.asyncio
    async def test_unauthorized_callback_user_never_reaches_the_control(self, monkeypatch):
        """The existing callback allowlist gate runs before the token is even looked up."""
        runner = _make_runner("queue")
        adapter = _make_adapter(runner)
        source = _source()
        session_key, agent = _start_run(runner, adapter, source)
        monkeypatch.setattr(adapter, "_is_callback_user_authorized", lambda *a, **k: True)
        await _queue(runner, source)
        token = _control_token(adapter)

        monkeypatch.setattr(adapter, "_is_callback_user_authorized", lambda *a, **k: False)
        query, update = _tap(adapter, token)
        await adapter._handle_callback_query(update, MagicMock())

        agent.interrupt.assert_not_called()
        assert runner._is_session_running(session_key) is True
        assert _answer_text(query) == _UNAUTHORIZED

    @pytest.mark.asyncio
    async def test_a_drifted_session_key_answers_honestly_instead_of_stopped(self, monkeypatch):
        """handle_message fails closed when the source no longer derives the token's session, and
        drops the event silently. Answering "Stopped" there would confirm a stop that never ran."""
        runner = _make_runner("queue")
        adapter = _make_adapter(runner)
        source = _source()
        session_key, agent = _start_run(runner, adapter, source)
        monkeypatch.setattr(adapter, "_is_callback_user_authorized", lambda *a, **k: True)
        await _queue(runner, source)
        token = _control_token(adapter)

        monkeypatch.setattr(adapter, "_event_session_key", lambda _event: "some:other:session")
        query, update = _tap(adapter, token)
        await adapter._handle_callback_query(update, MagicMock())

        agent.interrupt.assert_not_called()
        assert runner._is_session_running(session_key) is True
        assert "Stopped" not in _answer_text(query)
        assert "finished" in _answer_text(query).lower()

    @pytest.mark.asyncio
    async def test_a_failed_stop_never_claims_the_run_was_stopped(self, monkeypatch):
        """Fail loud: if the stop raises, say so rather than answering a false confirmation."""
        runner = _make_runner("queue")
        adapter = _make_adapter(runner)
        source = _source()
        _start_run(runner, adapter, source)
        monkeypatch.setattr(adapter, "_is_callback_user_authorized", lambda *a, **k: True)
        await _queue(runner, source)
        token = _control_token(adapter)

        async def _boom(*_a, **_k):
            raise RuntimeError("adapter is gone")

        runner._resolve_busy_stop_control = _boom
        query, update = _tap(adapter, token)
        await adapter._handle_callback_query(update, MagicMock())

        assert "Couldn't stop" in _answer_text(query)
        assert "Stopped" not in _answer_text(query)
        query.edit_message_text.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_slash_access_denial_blocks_the_tap(self, monkeypatch):
        """The control carries no authority /stop does not: admin-gated /stop denies the tap too."""
        runner = _make_runner("queue")
        adapter = _make_adapter(runner)
        source = _source()
        session_key, agent = _start_run(runner, adapter, source)
        monkeypatch.setattr(adapter, "_is_callback_user_authorized", lambda *a, **k: True)
        await _queue(runner, source)
        token = _control_token(adapter)

        runner._check_slash_access = lambda src, cmd: "⛔ /stop is admin-only here." if cmd == "stop" else None
        query, update = _tap(adapter, token)
        await adapter._handle_callback_query(update, MagicMock())

        agent.interrupt.assert_not_called()
        assert runner._is_session_running(session_key) is True
        assert "admin-only" in _answer_text(query)
