"""Runner integration tests for busy-session controls.

Covers:
- Halt-phrase pre-flight (multilingual stop intent triggers immediate
  interrupt + 🙊 reaction without falling through to mode dispatch).
- Button-tap dispatch (steer / interrupt / stop) and the contract that
  one tap acts on ALL pending follow-ups.
- Multi follow-up text concatenation in the order received.
- Reaction lifecycle (👍 / ⚡ / 🙊 emitted on each follow-up).
- Control-bubble fallback when no tool bubble exists.
- Queue-mode voice notes transcribed at arrival (before the queue ack)
  rather than when the pending queue drains.

Adapter is mocked; we exercise GatewayRunner methods directly to keep
the surface tight.  These complement the platform-neutral wire-format
tests in ``tests/gateway/test_busy_session_buttons.py``.
"""

from __future__ import annotations

import asyncio
import sys
import time
import types
from unittest.mock import AsyncMock, MagicMock

import pytest

# Stub telegram so gateway.run imports cleanly on a bare CI runner.
_tg = types.ModuleType("telegram")
_tg.constants = types.ModuleType("telegram.constants")
_ct = MagicMock()
_ct.SUPERGROUP = "supergroup"
_ct.GROUP = "group"
_ct.PRIVATE = "private"
_tg.constants.ChatType = _ct
sys.modules.setdefault("telegram", _tg)
sys.modules.setdefault("telegram.constants", _tg.constants)
sys.modules.setdefault("telegram.ext", types.ModuleType("telegram.ext"))

from gateway.busy_session_buttons import (
    PRIMITIVE_INTERRUPT,
    PRIMITIVE_STEER,
    PRIMITIVE_STOP,
    REACTION_INTERRUPT,
    REACTION_STEER,
    REACTION_STOP,
)
from gateway.platforms.base import (
    MessageEvent,
    MessageType,
    SessionSource,
    build_session_key,
)


def _make_event(text="hello", chat_id="123", platform_val="telegram", message_id="msg1"):
    source = SessionSource(
        platform=MagicMock(value=platform_val),
        chat_id=chat_id,
        chat_type="private",
        user_id="user1",
    )
    return MessageEvent(
        text=text,
        message_type=MessageType.TEXT,
        source=source,
        message_id=message_id,
    )


def _make_voice_event(
    path="/tmp/voice_one.ogg",
    text="",
    message_id="v1",
    message_type=MessageType.VOICE,
    media_type="audio/ogg",
    source=None,
):
    source = source or SessionSource(
        platform=MagicMock(value="telegram"),
        chat_id="123",
        chat_type="private",
        user_id="user1",
    )
    return MessageEvent(
        text=text,
        message_type=message_type,
        source=source,
        message_id=message_id,
        media_urls=[path],
        media_types=[media_type],
    )


def _install_fake_stt(runner, transcripts_by_path, gate=None):
    """Stub the STT boundary, keeping the real cache/echo-ledger machinery.

    Returns the list of audio-path batches passed to each transcription call,
    so tests can assert how many STT requests were actually issued.  ``gate``
    is an ``asyncio.Event`` the fake blocks on, so a test can hold a
    transcription in flight while a second caller arrives.
    """
    calls: list[list[str]] = []

    async def _enrich(user_text, audio_paths, cleanup_managed_audio=True):
        calls.append(list(audio_paths))
        if gate is not None:
            await gate.wait()
        transcripts = [transcripts_by_path[p] for p in audio_paths]
        prefix = "\n\n".join(
            f'[Voice message transcript]: "{t}"' for t in transcripts
        )
        if user_text:
            return f"{prefix}\n\n{user_text}", transcripts
        return prefix, transcripts

    runner._enrich_message_with_transcription = _enrich
    return calls


def _make_priority_source():
    """A real-Platform source so _handle_message can resolve the adapter."""
    from gateway.config import Platform

    return SessionSource(
        platform=Platform.TELEGRAM,
        chat_id="c1",
        chat_type="dm",
        user_id="u1",
        user_name="tester",
    )


def _make_priority_runner():
    """Runner wired to reach the inline PRIORITY busy block in _handle_message.

    ``_handle_active_session_busy_message`` is the adapter-dispatched busy
    handler; ``_handle_message`` carries its own independent busy block behind
    ``if _quick_key in self._running_agents``.  Mirrors the harness in
    tests/gateway/test_priority_path_compression_demotion_56391.py.
    """
    from datetime import datetime

    from gateway.config import GatewayConfig, Platform, PlatformConfig
    from gateway.run import GatewayRunner
    from gateway.session import SessionEntry

    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(
        platforms={Platform.TELEGRAM: PlatformConfig(enabled=True, token="***")}
    )
    adapter = _make_adapter()
    runner.adapters = {Platform.TELEGRAM: adapter}
    runner._voice_mode = {}
    runner.hooks = types.SimpleNamespace(emit=AsyncMock(), loaded_hooks=False)

    sk = build_session_key(_make_priority_source())
    session_store = MagicMock()
    session_store.get_or_create_session.return_value = SessionEntry(
        session_key=sk,
        session_id="sess-1",
        created_at=datetime.now(),
        updated_at=datetime.now(),
        platform=Platform.TELEGRAM,
        chat_type="dm",
    )
    session_store.load_transcript.return_value = []
    session_store.has_any_sessions.return_value = True
    runner.session_store = session_store

    runner._running_agents = {}
    runner._running_agents_ts = {}
    runner._pending_messages = {}
    runner._pending_approvals = {}
    runner._session_db = None
    runner._is_user_authorized = lambda _source: True
    runner._draining = False
    runner._busy_input_mode = "queue"
    runner._agent_has_active_subagents = lambda _agent: False

    agent = MagicMock()
    agent.get_activity_summary.return_value = {
        "seconds_since_activity": 0.0,
        "last_activity_desc": "api_call",
        "api_call_count": 1,
        "max_iterations": 60,
    }
    runner._running_agents[sk] = agent
    # Past the Telegram follow-up grace window so the event reaches the
    # PRIORITY queue branch rather than the earlier grace-period branch.
    runner._running_agents_ts[sk] = time.time() - 120
    return runner, adapter, sk


async def _drain_pending(runner, adapter, event, session_key):
    """Prepare the queued event the way the post-run drain site does."""
    queued = adapter._pending_messages[session_key]
    text, _ = await runner._transcribe_and_echo_pending_voice(
        queued,
        adapter,
        event.source,
        queued.text or "",
        log_context="Voice-drain",
    )
    return queued, text


def _record_chat_sends(adapter):
    """Capture transcript echoes and busy acks in a single ordered log."""
    sent: list[tuple[str, str]] = []

    async def _echo(_chat_id, content, **_kwargs):
        sent.append(("echo", content))

    async def _ack(**kwargs):
        sent.append(("ack", kwargs.get("content") or ""))

    adapter.send = AsyncMock(side_effect=_echo)
    adapter._send_with_retry = AsyncMock(side_effect=_ack)
    return sent


def _make_runner():
    from gateway.run import GatewayRunner, _AGENT_PENDING_SENTINEL

    runner = object.__new__(GatewayRunner)
    runner._running_agents = {}
    runner._running_agents_ts = {}
    runner._pending_messages = {}
    runner._busy_ack_ts = {}
    runner._busy_ack_tool_bubble_defer_seconds = 0.0
    runner._draining = False
    runner.adapters = {}
    runner.config = MagicMock()
    runner.session_store = None
    runner.hooks = MagicMock()
    runner.hooks.emit = AsyncMock()
    runner.pairing_store = MagicMock()
    runner.pairing_store.is_approved.return_value = True
    runner._is_user_authorized = lambda _source: True
    runner._busy_input_mode = "queue"

    # New-state fields exercised by busy-session-buttons.
    runner._tool_bubble_msg_ids = {}
    runner._busy_control_bubble_ids = {}
    runner._pending_followups = {}
    runner._session_run_generation = {}

    # _interrupt_and_clear_session needs these.
    runner._invalidate_session_run_generation = MagicMock()
    runner._release_running_agent_state = MagicMock(return_value=True)
    runner._is_session_run_current = MagicMock(return_value=True)

    return runner, _AGENT_PENDING_SENTINEL


def _make_adapter():
    adapter = MagicMock()
    adapter._pending_messages = {}
    adapter._text_debounce = {}

    async def _queue_text_debounce(sk, event):
        adapter._text_debounce[sk] = types.SimpleNamespace(event=event, task=None)

    async def _flush_text_debounce_now(sk):
        state = adapter._text_debounce.pop(sk, None)
        if state is None:
            return False
        adapter._pending_messages[sk] = state.event
        return True

    adapter._queue_text_debounce = AsyncMock(side_effect=_queue_text_debounce)
    adapter._flush_text_debounce_now = AsyncMock(side_effect=_flush_text_debounce_now)
    adapter._discard_text_debounce = MagicMock(
        side_effect=lambda sk: adapter._text_debounce.pop(sk, None)
    )
    adapter._send_with_retry = AsyncMock()
    adapter.send = AsyncMock()
    adapter.set_busy_reaction = AsyncMock(return_value=True)
    adapter.attach_busy_session_buttons = AsyncMock(return_value=True)
    adapter.clear_busy_session_buttons = AsyncMock(return_value=True)
    adapter.send_or_update_busy_control_bubble = AsyncMock(return_value="ctrl_msg_id")
    adapter.delete_busy_control_bubble = AsyncMock(return_value=True)
    # get_pending_message has consume-and-discard semantics on real adapters.
    adapter.get_pending_message = MagicMock(
        side_effect=lambda sk: adapter._pending_messages.pop(sk, None)
    )
    adapter.interrupt_session_activity = AsyncMock()
    adapter.edit_message = AsyncMock(return_value=MagicMock(success=True))
    return adapter


# ---------------------------------------------------------------------------
# Halt-phrase pre-flight
# ---------------------------------------------------------------------------


class TestHaltPhrasePreflight:
    @pytest.mark.asyncio
    async def test_english_stop_word_triggers_immediate_halt(self):
        from gateway.run import GatewayRunner

        runner, _ = _make_runner()
        adapter = _make_adapter()
        event = _make_event(text="stop")
        sk = build_session_key(event.source)
        runner.adapters[event.source.platform] = adapter

        agent = MagicMock()
        runner._running_agents[sk] = agent
        adapter._pending_messages[sk] = "leftover"

        result = await GatewayRunner._handle_active_session_busy_message(runner, event, sk)

        assert result is True
        # Halt phrase routes through the full _interrupt_and_clear_session
        # path so the chat unlocks even when the agent is wedged inside a
        # tool — same behavior as /stop and the [Stop] button.
        agent.interrupt.assert_called_once_with("Stop requested")
        adapter.set_busy_reaction.assert_awaited_with(event, REACTION_STOP)
        # Pending slot cleared — halt does NOT replay text as next turn.
        assert sk not in adapter._pending_messages

    @pytest.mark.asyncio
    async def test_japanese_halt_phrase_matches(self):
        from gateway.run import GatewayRunner

        runner, _ = _make_runner()
        adapter = _make_adapter()
        event = _make_event(text="止まれ")
        sk = build_session_key(event.source)
        runner.adapters[event.source.platform] = adapter
        runner._running_agents[sk] = MagicMock()

        result = await GatewayRunner._handle_active_session_busy_message(runner, event, sk)

        assert result is True
        adapter.set_busy_reaction.assert_awaited_with(event, REACTION_STOP)

    @pytest.mark.asyncio
    async def test_long_message_with_stop_word_does_not_halt(self):
        """The conservative length cap is the main false-positive defense."""
        from gateway.run import GatewayRunner

        runner, _ = _make_runner()
        adapter = _make_adapter()
        long_text = "we should stop including Bob in the email today"
        event = _make_event(text=long_text)
        sk = build_session_key(event.source)
        runner.adapters[event.source.platform] = adapter

        agent = MagicMock()
        runner._running_agents[sk] = agent

        await GatewayRunner._handle_active_session_busy_message(runner, event, sk)

        # Mode is "queue" — agent is NOT interrupted.
        agent.interrupt.assert_not_called()


# ---------------------------------------------------------------------------
# Early correction auto-interrupt
# ---------------------------------------------------------------------------


class TestEarlyCorrectionAutoInterrupt:
    @pytest.mark.asyncio
    async def test_text_followup_within_ten_seconds_interrupts_even_in_queue_mode(self):
        """A near-immediate second message is usually a typo correction.

        It should interrupt the active turn automatically instead of waiting
        behind the queued-mode button flow, and Telegram should acknowledge the
        interrupt with a lightning reaction instead of a chat bubble.
        """
        from gateway.run import GatewayRunner

        runner, _ = _make_runner()
        runner._busy_input_mode = "queue"
        runner._busy_text_mode = "queue"
        adapter = _make_adapter()
        event = _make_event(text="correction: use the other branch")
        sk = build_session_key(event.source)
        runner.adapters[event.source.platform] = adapter

        agent = MagicMock()
        runner._running_agents[sk] = agent
        runner._running_agents_ts[sk] = time.time() - 9.5

        result = await GatewayRunner._handle_active_session_busy_message(runner, event, sk)

        assert result is True
        agent.interrupt.assert_called_once_with("correction: use the other branch")
        assert adapter._pending_messages[sk] is event
        adapter.set_busy_reaction.assert_awaited_once_with(event, REACTION_INTERRUPT)
        adapter._send_with_retry.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_text_followup_after_ten_seconds_stays_queued_in_queue_mode(self):
        """After the correction window, queue-mode text should show controls."""
        from gateway.run import GatewayRunner

        runner, _ = _make_runner()
        runner._busy_input_mode = "queue"
        runner._busy_text_mode = "queue"
        adapter = _make_adapter()
        event = _make_event(text="additional context for later")
        sk = build_session_key(event.source)
        runner.adapters[event.source.platform] = adapter

        agent = MagicMock()
        runner._running_agents[sk] = agent
        runner._running_agents_ts[sk] = time.time() - 10.5
        runner._tool_bubble_msg_ids[sk] = "tool-msg"

        result = await GatewayRunner._handle_active_session_busy_message(runner, event, sk)

        assert result is True
        agent.interrupt.assert_not_called()
        adapter.set_busy_reaction.assert_not_awaited()
        # The runner stages queue/debounce before awaiting control UI, so a
        # completing active turn or immediate button tap cannot outrun it.
        adapter._queue_text_debounce.assert_awaited_once_with(sk, event)
        assert adapter._text_debounce[sk].event is event
        assert runner._pending_followups[sk] == [event]
        adapter.attach_busy_session_buttons.assert_awaited_once_with(sk, "tool-msg")

    @pytest.mark.asyncio
    async def test_queue_tracking_snapshots_events_before_adapter_mutation(self):
        from gateway.run import GatewayRunner

        runner, _ = _make_runner()
        runner._busy_input_mode = "queue"
        runner._busy_text_mode = "queue"
        adapter = _make_adapter()
        event = _make_event(text="one", message_id="m1")
        sk = build_session_key(event.source)
        runner.adapters[event.source.platform] = adapter
        runner._running_agents[sk] = MagicMock()
        runner._running_agents_ts[sk] = time.time() - 20

        assert await GatewayRunner._handle_active_session_busy_message(runner, event, sk) is True
        event.text = "one\ntwo"
        event.message_id = "m2"

        tracked = runner._pending_followups[sk][0]
        assert tracked.text == "one"
        assert tracked.message_id == "m1"

    @pytest.mark.asyncio
    async def test_queue_mode_attaches_existing_controls_when_ack_disabled(self, monkeypatch):
        from gateway.run import GatewayRunner

        monkeypatch.setenv("HERMES_GATEWAY_BUSY_ACK_ENABLED", "false")
        runner, _ = _make_runner()
        runner._busy_input_mode = "queue"
        runner._busy_text_mode = "queue"
        adapter = _make_adapter()
        event = _make_event(text="later", message_id="m1")
        sk = build_session_key(event.source)
        runner.adapters[event.source.platform] = adapter
        runner._running_agents[sk] = MagicMock()
        runner._running_agents_ts[sk] = time.time() - 20
        runner._tool_bubble_msg_ids[sk] = "tool-msg"

        assert await GatewayRunner._handle_active_session_busy_message(runner, event, sk) is True
        adapter.attach_busy_session_buttons.assert_awaited_once_with(sk, "tool-msg")


# ---------------------------------------------------------------------------
# Button-tap dispatch
# ---------------------------------------------------------------------------


class TestButtonTap:
    @pytest.mark.asyncio
    async def test_steer_tap_routes_to_running_agent_steer(self):
        runner, _ = _make_runner()
        adapter = _make_adapter()
        event = _make_event(text="also include vector DBs")
        sk = build_session_key(event.source)
        runner.adapters[event.source.platform] = adapter

        agent = MagicMock()
        agent.steer = MagicMock(return_value=True)
        runner._running_agents[sk] = agent
        runner._pending_followups[sk] = [event]
        adapter._pending_messages[sk] = event  # would normally be replayed

        toast = await runner._handle_busy_session_button_tap(
            sk, PRIMITIVE_STEER, event.source
        )

        agent.steer.assert_called_once_with("also include vector DBs")
        # Steer landed inside the run, so the queued copy must be cleared
        # to avoid double-processing it as next turn.
        assert sk not in adapter._pending_messages
        adapter.set_busy_reaction.assert_awaited_with(event, REACTION_STEER)
        assert "Steered" in toast or REACTION_STEER in toast

    @pytest.mark.asyncio
    async def test_steer_can_be_rearmed_and_used_twice_in_same_run(self):
        from gateway.run import GatewayRunner

        runner, _ = _make_runner()
        runner._busy_text_mode = "queue"
        adapter = _make_adapter()
        first = _make_event(text="first correction", message_id="m1")
        second = _make_event(text="second correction", message_id="m2")
        second.source = first.source
        sk = build_session_key(first.source)
        runner.adapters[first.source.platform] = adapter

        agent = MagicMock()
        agent.steer = MagicMock(return_value=True)
        runner._running_agents[sk] = agent

        runner._tool_bubble_msg_ids[sk] = "tool-msg-1"
        assert await GatewayRunner._handle_active_session_busy_message(
            runner, first, sk
        ) is True
        await runner._handle_busy_session_button_tap(sk, PRIMITIVE_STEER, first.source)

        # The active run continues and reaches another tool, which creates a
        # fresh control anchor for a later correction in the same run.
        runner._tool_bubble_msg_ids[sk] = "tool-msg-2"
        assert await GatewayRunner._handle_active_session_busy_message(
            runner, second, sk
        ) is True
        await runner._handle_busy_session_button_tap(sk, PRIMITIVE_STEER, second.source)

        assert [call.args[0] for call in agent.steer.call_args_list] == [
            "first correction",
            "second correction",
        ]
        adapter.attach_busy_session_buttons.assert_any_await(sk, "tool-msg-1")
        adapter.attach_busy_session_buttons.assert_any_await(sk, "tool-msg-2")
        assert sk not in runner._pending_followups

    @pytest.mark.asyncio
    async def test_steer_flushes_staged_debounce_and_uses_consolidated_text(self):
        runner, _ = _make_runner()
        adapter = _make_adapter()
        first = _make_event(text="one", message_id="m1")
        second = _make_event(text="two", message_id="m2")
        consolidated = _make_event(text="one\ntwo", message_id="m2")
        sk = build_session_key(first.source)
        runner.adapters[first.source.platform] = adapter

        async def flush(_session_key):
            adapter._pending_messages[sk] = consolidated
            return True

        adapter._flush_text_debounce_now = AsyncMock(side_effect=flush)
        agent = MagicMock()
        agent.steer = MagicMock(return_value=True)
        runner._running_agents[sk] = agent
        runner._pending_followups[sk] = [first, second]

        await runner._handle_busy_session_button_tap(sk, PRIMITIVE_STEER, first.source)

        adapter._flush_text_debounce_now.assert_awaited_once_with(sk)
        agent.steer.assert_called_once_with("one\ntwo")
        assert sk not in adapter._pending_messages

    @pytest.mark.asyncio
    async def test_steer_in_shared_session_reports_retry_without_losing_controls(self):
        runner, _ = _make_runner()
        adapter = _make_adapter()
        event = _make_event(text="my correction", message_id="m1")
        sk = build_session_key(event.source)
        runner.adapters[event.source.platform] = adapter
        adapter._text_debounce[sk] = types.SimpleNamespace(event=event, task=MagicMock())
        adapter._pending_messages[sk] = _make_event(
            text="other sender", message_id="m0"
        )
        adapter._flush_text_debounce_now = AsyncMock(return_value=False)
        agent = MagicMock()
        agent.steer = MagicMock(return_value=True)
        runner._running_agents[sk] = agent
        runner._pending_followups[sk] = [event]

        toast = await runner._handle_busy_session_button_tap(
            sk, PRIMITIVE_STEER, event.source
        )

        assert "try again" in toast.lower()
        agent.steer.assert_not_called()
        assert runner._pending_followups[sk] == [event]
        assert sk in adapter._text_debounce
        adapter.clear_busy_session_buttons.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_interrupt_tap_calls_running_agent_interrupt_with_text(self):
        runner, _ = _make_runner()
        adapter = _make_adapter()
        event = _make_event(text="completely different task: refactor module X")
        sk = build_session_key(event.source)
        runner.adapters[event.source.platform] = adapter

        agent = MagicMock()
        runner._running_agents[sk] = agent
        runner._pending_followups[sk] = [event]

        await runner._handle_busy_session_button_tap(
            sk, PRIMITIVE_INTERRUPT, event.source
        )

        agent.interrupt.assert_called_once_with(
            "completely different task: refactor module X"
        )
        adapter.set_busy_reaction.assert_awaited_with(event, REACTION_INTERRUPT)

    @pytest.mark.asyncio
    async def test_interrupt_tap_replaces_pending_slot_with_joined_text(self):
        """The post-run drain promotes ``adapter._pending_messages[sk]`` as
        the next-turn prompt.  Because that slot is single-slot text-
        replacing, an interrupt tap with multiple follow-ups must
        rewrite its text to the joined version, otherwise the next turn
        only sees the last follow-up."""
        runner, _ = _make_runner()
        adapter = _make_adapter()
        e1 = _make_event(text="first", message_id="m1")
        e2 = _make_event(text="second", message_id="m2")
        sk = build_session_key(e1.source)
        runner.adapters[e1.source.platform] = adapter

        # Simulate the upstream busy-handler outcome: only the latest
        # follow-up is in adapter._pending_messages, but both are tracked
        # in runner._pending_followups for the button-tap path.
        adapter._pending_messages[sk] = e2
        runner._running_agents[sk] = MagicMock()
        runner._pending_followups[sk] = [e1, e2]

        await runner._handle_busy_session_button_tap(
            sk, PRIMITIVE_INTERRUPT, e1.source
        )

        # The pending event's text now carries BOTH messages so the next
        # turn replay sees the full conversation, not just "second".
        assert "first" in adapter._pending_messages[sk].text
        assert "second" in adapter._pending_messages[sk].text

    @pytest.mark.asyncio
    async def test_stop_tap_runs_full_clear_session_path(self):
        runner, _ = _make_runner()
        adapter = _make_adapter()
        event = _make_event(text="please halt")
        sk = build_session_key(event.source)
        runner.adapters[event.source.platform] = adapter

        agent = MagicMock()
        runner._running_agents[sk] = agent
        runner._pending_followups[sk] = [event]
        adapter._pending_messages[sk] = "should be cleared"

        await runner._handle_busy_session_button_tap(
            sk, PRIMITIVE_STOP, event.source
        )

        agent.interrupt.assert_called_once()
        # _interrupt_and_clear_session pops the pending slot.
        assert sk not in adapter._pending_messages
        adapter.set_busy_reaction.assert_awaited_with(event, REACTION_STOP)

    @pytest.mark.asyncio
    async def test_stop_flushes_and_clears_staged_debounce(self):
        runner, _ = _make_runner()
        adapter = _make_adapter()
        event = _make_event(text="do not replay", message_id="m1")
        sk = build_session_key(event.source)
        runner.adapters[event.source.platform] = adapter

        async def flush(_session_key):
            adapter._pending_messages[sk] = event
            return True

        adapter._flush_text_debounce_now = AsyncMock(side_effect=flush)
        runner._running_agents[sk] = MagicMock()
        runner._pending_followups[sk] = [event]

        await runner._handle_busy_session_button_tap(sk, PRIMITIVE_STOP, event.source)

        adapter._flush_text_debounce_now.assert_awaited_once_with(sk)
        assert sk not in adapter._pending_messages

    @pytest.mark.asyncio
    async def test_unknown_primitive_returns_unknown_action(self):
        runner, _ = _make_runner()
        adapter = _make_adapter()
        event = _make_event()
        sk = build_session_key(event.source)
        runner.adapters[event.source.platform] = adapter

        toast = await runner._handle_busy_session_button_tap(
            sk, "fortify", event.source
        )

        assert "Unknown" in toast

    @pytest.mark.asyncio
    async def test_cross_user_tap_in_shared_chat_is_rejected(self):
        """Per-user session keys: another authorized user in the chat
        must not be able to control someone else's run via a visible
        button.  Reject if the tapping user's session_key doesn't match
        the target session_key."""
        runner, _ = _make_runner()
        adapter = _make_adapter()
        event = _make_event(text="x")
        sk_owner = build_session_key(event.source)
        runner.adapters[event.source.platform] = adapter
        runner._running_agents[sk_owner] = MagicMock()
        runner._pending_followups[sk_owner] = [event]

        # A different authorized user clicks the button.  Their source
        # has a different user_id and therefore a different session_key.
        from gateway.platforms.base import SessionSource as _SS
        other = _SS(
            platform=event.source.platform,
            chat_id="123",
            chat_type="private",
            user_id="user2",  # different from event.source.user_id == "user1"
        )

        toast = await runner._handle_busy_session_button_tap(
            sk_owner, PRIMITIVE_INTERRUPT, other
        )

        assert "isn't your session" in toast.lower() or "not your session" in toast.lower()
        # And the agent was not interrupted on behalf of the wrong user.
        runner._running_agents[sk_owner].interrupt.assert_not_called()


class TestMultipleFollowUps:
    @pytest.mark.asyncio
    async def test_two_followups_get_concatenated_and_each_reacted(self):
        runner, _ = _make_runner()
        adapter = _make_adapter()
        e1 = _make_event(text="first follow up", message_id="m1")
        e2 = _make_event(text="second follow up", message_id="m2")
        sk = build_session_key(e1.source)
        runner.adapters[e1.source.platform] = adapter

        agent = MagicMock()
        agent.steer = MagicMock(return_value=True)
        runner._running_agents[sk] = agent
        runner._pending_followups[sk] = [e1, e2]

        await runner._handle_busy_session_button_tap(
            sk, PRIMITIVE_STEER, e1.source
        )

        agent.steer.assert_called_once()
        passed = agent.steer.call_args.args[0]
        assert "first follow up" in passed
        assert "second follow up" in passed
        # First in list, first in joined text — preserves arrival order.
        assert passed.index("first follow up") < passed.index("second follow up")

        # Each follow-up gets its own reaction.
        reaction_targets = [
            call.args[0] for call in adapter.set_busy_reaction.await_args_list
        ]
        assert e1 in reaction_targets
        assert e2 in reaction_targets

    @pytest.mark.asyncio
    async def test_pending_followups_cleared_after_tap(self):
        runner, _ = _make_runner()
        adapter = _make_adapter()
        e1 = _make_event(text="x")
        sk = build_session_key(e1.source)
        runner.adapters[e1.source.platform] = adapter
        runner._pending_followups[sk] = [e1]
        runner._running_agents[sk] = MagicMock()

        await runner._handle_busy_session_button_tap(
            sk, PRIMITIVE_INTERRUPT, e1.source
        )
        assert sk not in runner._pending_followups


class TestAckAnchorAndToolBubbleAnchor:
    @pytest.mark.asyncio
    async def test_no_tool_bubble_does_not_send_standalone_message(self):
        """No tool bubble → _ensure_busy_session_controls is a no-op.

        The upstream busy-ack message becomes the keyboard anchor via
        ``_anchor_busy_session_buttons_to_ack`` (called separately from
        the busy handler after the ack send).  This avoids the duplicate
        "/queue'd ..." + standalone-control message pair that the
        previous design produced.
        """
        runner, _ = _make_runner()
        adapter = _make_adapter()
        event = _make_event(text="hello before any tool")
        sk = build_session_key(event.source)
        runner.adapters[event.source.platform] = adapter

        await runner._ensure_busy_session_controls(sk, event)

        adapter.attach_busy_session_buttons.assert_not_called()
        adapter.send_or_update_busy_control_bubble.assert_not_called()

    @pytest.mark.asyncio
    async def test_deferred_queue_ack_anchors_to_tool_bubble_and_suppresses_ack(self):
        """If the first tool bubble appears during the short defer window,
        controls move to that bubble instead of sending a separate queue
        notice above it.
        """
        from gateway.run import GatewayRunner

        runner, _ = _make_runner()
        adapter = _make_adapter()
        event = _make_event(text="follow-up just before first tool")
        sk = build_session_key(event.source)
        runner.adapters[event.source.platform] = adapter
        runner._running_agents[sk] = MagicMock()
        runner._busy_ack_tool_bubble_defer_seconds = 0.01

        task = asyncio.create_task(
            GatewayRunner._handle_active_session_busy_message(runner, event, sk)
        )
        await asyncio.sleep(0)
        runner._tool_bubble_msg_ids[sk] = "tool_bubble_99"

        assert await task is True
        adapter._send_with_retry.assert_not_called()
        adapter.attach_busy_session_buttons.assert_awaited_with(sk, "tool_bubble_99")

    @pytest.mark.asyncio
    async def test_tool_bubble_present_attaches_keyboard_directly(self):
        runner, _ = _make_runner()
        adapter = _make_adapter()
        event = _make_event(text="mid-tool follow up")
        sk = build_session_key(event.source)
        runner.adapters[event.source.platform] = adapter
        runner._tool_bubble_msg_ids[sk] = "bubble_42"
        runner._busy_control_bubble_ids[sk] = ["ack_old"]

        await runner._ensure_busy_session_controls(sk, event)

        adapter.attach_busy_session_buttons.assert_awaited_once_with(sk, "bubble_42")
        adapter.clear_busy_session_buttons.assert_awaited_with(sk, "ack_old")
        assert sk not in runner._busy_control_bubble_ids
        adapter.send_or_update_busy_control_bubble.assert_not_called()

    @pytest.mark.asyncio
    async def test_anchor_to_ack_attaches_keyboard_to_ack_message(self):
        runner, _ = _make_runner()
        adapter = _make_adapter()
        event = _make_event()
        sk = build_session_key(event.source)
        runner.adapters[event.source.platform] = adapter

        await runner._anchor_busy_session_buttons_to_ack(sk, event, "ack_msg_77")

        adapter.attach_busy_session_buttons.assert_awaited_once_with(sk, "ack_msg_77")
        assert runner._busy_control_bubble_ids[sk] == ["ack_msg_77"]

    @pytest.mark.asyncio
    async def test_anchor_to_ack_replaces_previous_ack_anchor(self):
        """Long turns crossing the 30s ack-cooldown may produce another
        ack, but only the newest ack may keep a live keyboard.
        """
        runner, _ = _make_runner()
        adapter = _make_adapter()
        event = _make_event()
        sk = build_session_key(event.source)
        runner.adapters[event.source.platform] = adapter

        await runner._anchor_busy_session_buttons_to_ack(sk, event, "ack_1")
        await runner._anchor_busy_session_buttons_to_ack(sk, event, "ack_2")
        await runner._anchor_busy_session_buttons_to_ack(sk, event, "ack_3")

        assert runner._busy_control_bubble_ids[sk] == ["ack_3"]
        adapter.clear_busy_session_buttons.assert_any_await(sk, "ack_1")
        adapter.clear_busy_session_buttons.assert_any_await(sk, "ack_2")

    @pytest.mark.asyncio
    async def test_anchor_to_ack_uses_tool_bubble_if_one_exists(self):
        runner, _ = _make_runner()
        adapter = _make_adapter()
        event = _make_event()
        sk = build_session_key(event.source)
        runner.adapters[event.source.platform] = adapter
        runner._tool_bubble_msg_ids[sk] = "bubble_9"

        await runner._anchor_busy_session_buttons_to_ack(sk, event, "ack_should_not_get_buttons")

        adapter.attach_busy_session_buttons.assert_awaited_once_with(sk, "bubble_9")
        assert sk not in runner._busy_control_bubble_ids


class TestAckTextUpdateAfterTap:
    """A button tap must rewrite the ack body so it doesn't keep saying
    'Queued for the next turn...' after the user picked Steer / Interrupt
    / Stop."""

    @pytest.mark.asyncio
    async def test_steer_tap_rewrites_ack_text(self):
        runner, _ = _make_runner()
        adapter = _make_adapter()
        event = _make_event(text="hello")
        sk = build_session_key(event.source)
        runner.adapters[event.source.platform] = adapter
        agent = MagicMock()
        agent.steer = MagicMock(return_value=True)
        runner._running_agents[sk] = agent
        runner._pending_followups[sk] = [event]
        runner._busy_control_bubble_ids[sk] = ["ack_77"]

        await runner._handle_busy_session_button_tap(
            sk, PRIMITIVE_STEER, event.source
        )

        edit_calls = [
            c.kwargs for c in adapter.edit_message.await_args_list
        ]
        assert any("Steered" in (kw.get("content") or "") for kw in edit_calls)
        # Keyboard cleared before edit so the keyboard re-attach in
        # edit_message() doesn't immediately re-add it.
        adapter.clear_busy_session_buttons.assert_any_await(sk, "ack_77")

    @pytest.mark.asyncio
    async def test_interrupt_tap_rewrites_ack_text(self):
        runner, _ = _make_runner()
        adapter = _make_adapter()
        event = _make_event(text="redirect")
        sk = build_session_key(event.source)
        runner.adapters[event.source.platform] = adapter
        runner._running_agents[sk] = MagicMock()
        runner._pending_followups[sk] = [event]
        runner._busy_control_bubble_ids[sk] = ["ack_91"]

        await runner._handle_busy_session_button_tap(
            sk, PRIMITIVE_INTERRUPT, event.source
        )

        edit_calls = [
            c.kwargs for c in adapter.edit_message.await_args_list
        ]
        assert any("Interrupted" in (kw.get("content") or "") for kw in edit_calls)

    @pytest.mark.asyncio
    async def test_stop_tap_rewrites_ack_text(self):
        runner, _ = _make_runner()
        adapter = _make_adapter()
        event = _make_event(text="halt")
        sk = build_session_key(event.source)
        runner.adapters[event.source.platform] = adapter
        runner._running_agents[sk] = MagicMock()
        runner._pending_followups[sk] = [event]
        runner._busy_control_bubble_ids[sk] = ["ack_55"]

        await runner._handle_busy_session_button_tap(
            sk, PRIMITIVE_STOP, event.source
        )

        edit_calls = [
            c.kwargs for c in adapter.edit_message.await_args_list
        ]
        assert any("Stopped" in (kw.get("content") or "") for kw in edit_calls)


class TestBusyQueueVoiceTranscription:
    """Queue-mode voice notes must be transcribed when they arrive.

    Steer and interrupt already pre-transcribe because they need the text
    inside the running turn.  Queue mode used to store the raw event, so a
    voice note sent while the agent was mid-tool produced only "Queued for
    the next turn" — no 🎙️ echo, and no transcript at all until the turn
    finished and the pending queue drained.
    """

    @pytest.mark.asyncio
    async def test_transcript_is_echoed_before_the_ack_and_reused_by_the_drain(self):
        from gateway.run import GatewayRunner

        runner, _ = _make_runner()
        runner._busy_input_mode = "queue"
        adapter = _make_adapter()
        sent = _record_chat_sends(adapter)
        stt_calls = _install_fake_stt(runner, {"/tmp/voice_one.ogg": "ship it tomorrow"})
        event = _make_voice_event()
        sk = build_session_key(event.source)
        runner.adapters[event.source.platform] = adapter
        runner._running_agents[sk] = MagicMock()

        assert await GatewayRunner._handle_active_session_busy_message(
            runner, event, sk
        ) is True

        assert stt_calls == [["/tmp/voice_one.ogg"]]
        assert sent[0] == ("echo", '🎙️ "ship it tomorrow"')
        assert sent[1][0] == "ack"
        assert "Queued for the next turn" in sent[1][1]

        queued, drained = await _drain_pending(runner, adapter, event, sk)

        assert queued is event
        assert "ship it tomorrow" in drained
        # The drain must not pay for STT again or echo the note a second time.
        assert stt_calls == [["/tmp/voice_one.ogg"]]
        assert [kind for kind, _ in sent].count("echo") == 1

    @pytest.mark.asyncio
    async def test_audio_file_attachment_is_not_auto_transcribed(self):
        """MessageType.AUDIO is a file attachment, never an STT input."""
        from gateway.run import GatewayRunner

        runner, _ = _make_runner()
        runner._busy_input_mode = "queue"
        adapter = _make_adapter()
        sent = _record_chat_sends(adapter)
        stt_calls = _install_fake_stt(runner, {"/tmp/song.m4a": "should never run"})
        event = _make_voice_event(
            path="/tmp/song.m4a",
            message_type=MessageType.AUDIO,
            media_type="audio/mp4",
        )
        sk = build_session_key(event.source)
        runner.adapters[event.source.platform] = adapter
        runner._running_agents[sk] = MagicMock()

        assert await GatewayRunner._handle_active_session_busy_message(
            runner, event, sk
        ) is True

        assert stt_calls == []
        assert [kind for kind, _ in sent] == ["ack"]
        assert adapter._pending_messages[sk] is event

    @pytest.mark.asyncio
    async def test_failed_transcription_still_queues_the_event_and_caption(self):
        from gateway.run import GatewayRunner

        runner, _ = _make_runner()
        runner._busy_input_mode = "queue"
        adapter = _make_adapter()
        sent = _record_chat_sends(adapter)

        attempts = []

        async def _boom(*_args, **_kwargs):
            attempts.append(1)
            raise RuntimeError("stt backend down")

        runner._enrich_message_with_transcription = _boom
        event = _make_voice_event(text="see attached note")
        sk = build_session_key(event.source)
        runner.adapters[event.source.platform] = adapter
        runner._running_agents[sk] = MagicMock()

        assert await GatewayRunner._handle_active_session_busy_message(
            runner, event, sk
        ) is True

        assert attempts == [1]
        queued = adapter._pending_messages[sk]
        assert queued is event
        assert queued.text == "see attached note"
        assert queued.media_urls == ["/tmp/voice_one.ogg"]
        assert [kind for kind, _ in sent] == ["ack"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "demotion, ack_fragment",
        [
            ("subagents", "Subagent working"),
            ("compression", "Compressing context"),
            ("steer-fallback", "Queued for the next turn"),
        ],
    )
    async def test_every_demotion_to_queue_transcribes_the_voice_note(
        self, demotion, ack_fragment
    ):
        """#30170 / #56391 demotions and steer-fallback all park the message."""
        from gateway.run import GatewayRunner

        runner, _ = _make_runner()
        adapter = _make_adapter()
        sent = _record_chat_sends(adapter)
        stt_calls = _install_fake_stt(runner, {"/tmp/voice_one.ogg": "check the logs"})
        event = _make_voice_event()
        sk = build_session_key(event.source)
        runner.adapters[event.source.platform] = adapter
        agent = MagicMock()
        runner._running_agents[sk] = agent

        runner._busy_input_mode = "steer" if demotion == "steer-fallback" else "interrupt"
        runner._agent_has_active_subagents = lambda _agent: demotion == "subagents"
        runner._session_has_compression_in_flight = AsyncMock(
            return_value=demotion == "compression"
        )
        agent.steer = MagicMock(return_value=False)

        assert await GatewayRunner._handle_active_session_busy_message(
            runner, event, sk
        ) is True

        agent.interrupt.assert_not_called()
        assert stt_calls == [["/tmp/voice_one.ogg"]]
        assert sent[0] == ("echo", '🎙️ "check the logs"')
        assert ack_fragment in sent[-1][1]
        assert adapter._pending_messages[sk] is event

    @pytest.mark.asyncio
    async def test_second_voice_note_merged_into_head_echoes_only_the_new_one(self):
        """New audio invalidates the cache; note 1 must not be echoed twice."""
        from gateway.run import GatewayRunner

        runner, _ = _make_runner()
        runner._busy_input_mode = "queue"
        adapter = _make_adapter()
        sent = _record_chat_sends(adapter)
        stt_calls = _install_fake_stt(
            runner,
            {"/tmp/voice_one.ogg": "first note", "/tmp/voice_two.ogg": "second note"},
        )
        first = _make_voice_event()
        second = _make_voice_event(
            path="/tmp/voice_two.ogg", message_id="v2", source=first.source
        )
        sk = build_session_key(first.source)
        runner.adapters[first.source.platform] = adapter
        runner._running_agents[sk] = MagicMock()

        await GatewayRunner._handle_active_session_busy_message(runner, first, sk)
        await GatewayRunner._handle_active_session_busy_message(runner, second, sk)

        echoes = [content for kind, content in sent if kind == "echo"]
        assert echoes == ['🎙️ "first note"', '🎙️ "second note"']
        assert stt_calls == [
            ["/tmp/voice_one.ogg"],
            ["/tmp/voice_one.ogg", "/tmp/voice_two.ogg"],
        ]

        queued, drained = await _drain_pending(runner, adapter, first, sk)

        assert queued.media_urls == ["/tmp/voice_one.ogg", "/tmp/voice_two.ogg"]
        assert "first note" in drained and "second note" in drained
        assert [content for kind, content in sent if kind == "echo"] == echoes

    @pytest.mark.asyncio
    async def test_text_followup_merged_into_a_queued_voice_note_keeps_the_transcript(self):
        """A caption merge changes no audio, so it must not re-run STT.

        ``merge_pending_message_event`` folds the follow-up text into the
        queued voice event.  Only the caption changed, so the cached
        transcript still describes the audio — the drain has to re-join it
        with the new text instead of transcribing the same note again.
        """
        from gateway.run import GatewayRunner

        runner, _ = _make_runner()
        runner._busy_input_mode = "queue"
        adapter = _make_adapter()
        sent = _record_chat_sends(adapter)
        stt_calls = _install_fake_stt(runner, {"/tmp/voice_one.ogg": "ship it tomorrow"})
        voice = _make_voice_event()
        follow_up = _make_event(text="and also check the logs")
        follow_up.source = voice.source
        sk = build_session_key(voice.source)
        runner.adapters[voice.source.platform] = adapter
        runner._running_agents[sk] = MagicMock()
        runner._running_agents_ts[sk] = time.time() - 60

        await GatewayRunner._handle_active_session_busy_message(runner, voice, sk)
        # Exercise the immediate merge path; ordinary text remains staged in
        # the candidate's debounce buffer (covered by the busy-button tests).
        await GatewayRunner._handle_active_session_busy_message(
            runner, follow_up, sk, force_busy_ack=True,
        )

        queued, drained = await _drain_pending(runner, adapter, voice, sk)

        assert queued is voice
        assert queued.text == "and also check the logs"
        assert stt_calls == [["/tmp/voice_one.ogg"]]
        assert "ship it tomorrow" in drained
        assert "and also check the logs" in drained
        assert [kind for kind, _ in sent].count("echo") == 1

    @pytest.mark.asyncio
    async def test_arrival_and_drain_share_one_in_flight_transcription(self):
        """Concurrent callers must await one STT call, not race two.

        Arrival-time preparation and the pending drain can both reach the
        event before either has populated the cache.
        """
        runner, _ = _make_runner()
        adapter = _make_adapter()
        sent = _record_chat_sends(adapter)
        gate = asyncio.Event()
        stt_calls = _install_fake_stt(
            runner, {"/tmp/voice_one.ogg": "ship it tomorrow"}, gate=gate
        )
        event = _make_voice_event()
        runner.adapters[event.source.platform] = adapter

        async def _prepare(log_context):
            return await runner._transcribe_and_echo_pending_voice(
                event, adapter, event.source, "", log_context=log_context
            )

        arrival = asyncio.create_task(_prepare("Busy-queue"))
        drain = asyncio.create_task(_prepare("Voice-drain"))
        for _ in range(5):
            await asyncio.sleep(0)
        gate.set()
        results = await asyncio.gather(arrival, drain)

        assert stt_calls == [["/tmp/voice_one.ogg"]]
        assert [kind for kind, _ in sent] == ["echo"]
        assert all("ship it tomorrow" in text for text, _ in results)

    @pytest.mark.asyncio
    async def test_priority_path_queue_transcribes_and_caches_for_the_drain(self):
        """_handle_message has its own busy block; it must prepare voice too."""
        runner, adapter, sk = _make_priority_runner()
        sent = _record_chat_sends(adapter)
        stt_calls = _install_fake_stt(runner, {"/tmp/voice_one.ogg": "roll it back"})
        event = _make_voice_event(source=_make_priority_source())

        assert await runner._handle_message(event) is None

        assert stt_calls == [["/tmp/voice_one.ogg"]]
        assert [kind for kind, _ in sent] == ["echo"]
        assert sent[0][1] == '🎙️ "roll it back"'
        assert adapter._pending_messages[sk] is event

        queued, drained = await _drain_pending(runner, adapter, event, sk)

        assert queued is event
        assert "roll it back" in drained
        assert stt_calls == [["/tmp/voice_one.ogg"]]
        assert [kind for kind, _ in sent].count("echo") == 1


class TestCleanup:
    @pytest.mark.asyncio
    async def test_clear_busy_session_controls_tears_everything_down(self):
        """Cleanup detaches the keyboard from BOTH anchors and forgets state.

        The control-bubble entry is the ack message (real chat history we
        don't own), so cleanup detaches the keyboard but does NOT delete
        the message itself.
        """
        runner, _ = _make_runner()
        adapter = _make_adapter()
        event = _make_event()
        sk = build_session_key(event.source)
        runner.adapters[event.source.platform] = adapter
        runner._tool_bubble_msg_ids[sk] = "bubble_1"
        runner._busy_control_bubble_ids[sk] = ["ack_1"]
        runner._pending_followups[sk] = [event]

        await runner._clear_busy_session_controls(sk, event.source)

        clear_calls = [c.args for c in adapter.clear_busy_session_buttons.await_args_list]
        assert (sk, "bubble_1") in clear_calls
        assert (sk, "ack_1") in clear_calls
        # Cleanup must NOT delete the ack message — it's part of chat history.
        adapter.delete_busy_control_bubble.assert_not_called()
        assert sk not in runner._tool_bubble_msg_ids
        assert sk not in runner._busy_control_bubble_ids
        assert sk not in runner._pending_followups
