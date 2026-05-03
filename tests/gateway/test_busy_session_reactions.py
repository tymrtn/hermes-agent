"""Tests for busy-session router emoji reactions and ack-mode wiring.

Covers the new gateway primitives (steer/queue/stop/drop), the reaction
emission hook on BasePlatformAdapter, and the busy_ack_mode dispatch.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.busy_session import (
    BusySessionDecision,
    REACTION_DROP,
    REACTION_QUEUE,
    REACTION_STEER,
    REACTION_STOP,
    normalize_busy_ack_mode,
    normalize_busy_input_mode,
    reaction_for,
)


# --- BusySessionDecision dataclass ------------------------------------------


def test_busy_session_decision_immutable():
    d = BusySessionDecision(action="steer", reason="default_steer", reaction=REACTION_STEER)
    with pytest.raises((AttributeError, Exception)):
        d.action = "stop"  # frozen


def test_busy_session_decision_default_fields():
    d = BusySessionDecision(action="queue", reason="x")
    assert d.message is None
    assert d.reaction is None
    assert d.merge_text is False
    assert d.debounce_ack is False


def test_reaction_for_known_actions():
    assert reaction_for("queue") == REACTION_QUEUE
    assert reaction_for("steer") == REACTION_STEER
    assert reaction_for("stop") == REACTION_STOP
    assert reaction_for("drop") == REACTION_DROP


def test_reaction_for_interrupt_now_has_glyph():
    """Round 2 (Hermes-canonical + Telegram-compat):
    - interrupt = ⚡ (Hermes's canonical glyph, also accepted as Telegram reaction)
    - halt/stop = 🙊 (Telegram whitelist substitute for Hermes's 🛑)
    - steer = ✍️ (Telegram whitelist substitute for Hermes's ⏩)
    """
    assert reaction_for("interrupt") == "⚡"
    assert reaction_for("halt") == "\U0001f64a"  # 🙊
    assert reaction_for("steer") == "👍"
    assert reaction_for("garbage") is None


# --- Mode normalization -----------------------------------------------------


def test_normalize_busy_input_mode_default_is_queue():
    """Round 2: default flipped from steer to queue. User explicitly chooses
    via inline-keyboard buttons on the active tool bubble."""
    assert normalize_busy_input_mode("") == "queue"
    assert normalize_busy_input_mode(None) == "queue"
    assert normalize_busy_input_mode("garbage") == "queue"


def test_normalize_busy_input_mode_recognized_values():
    assert normalize_busy_input_mode("steer") == "steer"
    assert normalize_busy_input_mode("queue") == "queue"
    assert normalize_busy_input_mode("interrupt") == "interrupt"
    assert normalize_busy_input_mode("STEER") == "steer"
    assert normalize_busy_input_mode("  Queue ") == "queue"


def test_normalize_busy_ack_mode_default_is_reaction():
    assert normalize_busy_ack_mode("") == "reaction"
    assert normalize_busy_ack_mode(None) == "reaction"
    assert normalize_busy_ack_mode("garbage") == "reaction"


def test_normalize_busy_ack_mode_recognized_values():
    assert normalize_busy_ack_mode("reaction") == "reaction"
    assert normalize_busy_ack_mode("text") == "text"
    assert normalize_busy_ack_mode("both") == "both"
    assert normalize_busy_ack_mode("BOTH") == "both"


# --- BasePlatformAdapter.set_busy_reaction default no-op --------------------


def test_base_adapter_set_busy_reaction_default_returns_false():
    """Adapters without override return False (no-op default).

    BasePlatformAdapter declares connect/disconnect/get_chat_info/send as
    abstract; we provide minimal stubs to instantiate.
    """
    from gateway.platforms.base import BasePlatformAdapter

    class Stub(BasePlatformAdapter):
        def __init__(self):
            pass

        async def connect(self):
            return True

        async def disconnect(self):
            return None

        async def get_chat_info(self, chat_id):
            return {}

        async def send(self, *args, **kwargs):
            return None

    stub = Stub()
    fake_event = MagicMock()
    result = asyncio.run(stub.set_busy_reaction(fake_event, REACTION_QUEUE))
    assert result is False


# --- Router decision tree (via _route_busy_session_event) -------------------


def _make_runner(*, draining=False, queue_during_drain=True, busy_input_mode="steer",
                  busy_reactions_enabled=True, busy_ack_mode="reaction",
                  running_agents_ts=None):
    """Build a minimally-mocked GatewayRunner for routing tests."""
    from gateway.run import GatewayRunner

    runner = GatewayRunner.__new__(GatewayRunner)
    runner._draining = draining
    runner._queue_during_drain_enabled = lambda: queue_during_drain
    runner._busy_input_mode = busy_input_mode
    runner._busy_reactions_enabled = busy_reactions_enabled
    runner._busy_ack_mode = busy_ack_mode
    runner._running_agents_ts = running_agents_ts or {}
    runner._busy_ack_ts = {}
    runner._status_action_gerund = lambda: "restarting"
    return runner


def _make_event(*, text="hello", message_type=None, platform=None, chat_id="123",
                 message_id="456", thread_id=None):
    from gateway.platforms.base import MessageEvent, MessageType, SessionSource
    from gateway.config import Platform

    if message_type is None:
        message_type = MessageType.TEXT
    if platform is None:
        platform = Platform.TELEGRAM

    source = SessionSource(
        platform=platform,
        chat_id=chat_id,
        chat_type="private",
        chat_name=None,
        user_id="u1",
        user_name="User",
        thread_id=thread_id,
    )
    return MessageEvent(
        text=text,
        message_type=message_type,
        source=source,
        raw_message=None,
        message_id=message_id,
    )


def test_router_drain_queue_returns_queue_action():
    runner = _make_runner(draining=True, queue_during_drain=True)
    event = _make_event(text="hello")
    decision = runner._route_busy_session_event(event, "k", running_agent=MagicMock())
    assert decision.action == "queue"
    assert decision.reaction == REACTION_QUEUE
    assert decision.reason == "draining"


def test_router_drain_no_queue_returns_drop():
    runner = _make_runner(draining=True, queue_during_drain=False)
    event = _make_event(text="hello")
    decision = runner._route_busy_session_event(event, "k", running_agent=MagicMock())
    assert decision.action == "drop"
    assert decision.reaction == REACTION_DROP
    assert decision.reason == "draining_drop"


def test_router_halt_phrase_english():
    runner = _make_runner()
    event = _make_event(text="stop")
    decision = runner._route_busy_session_event(event, "k", running_agent=MagicMock())
    assert decision.action == "halt"
    assert decision.reaction == "\U0001f64a"  # 🛑
    assert decision.reason == "halt_phrase_en"


def test_router_halt_phrase_spanish():
    runner = _make_runner()
    event = _make_event(text="alto")  # 'para' deliberately excluded — too ambiguous
    decision = runner._route_busy_session_event(event, "k", running_agent=MagicMock())
    assert decision.action == "halt"
    assert decision.reaction == "\U0001f64a"
    assert "halt_phrase_" in decision.reason


def test_router_spanish_para_does_not_halt():
    """Regression: 'para' alone (Spanish preposition 'for') must not halt.
    With the new default it routes to queue (user gets ⏳ + buttons)."""
    runner = _make_runner()
    event = _make_event(text="para")
    decision = runner._route_busy_session_event(event, "k", running_agent=MagicMock())
    assert decision.action == "queue"


def test_router_empty_message_is_halt_universal():
    runner = _make_runner()
    event = _make_event(text="")
    decision = runner._route_busy_session_event(event, "k", running_agent=MagicMock())
    assert decision.action == "halt"
    assert decision.reason == "halt_phrase_universal"


def test_router_slash_halt_command():
    runner = _make_runner()
    event = _make_event(text="/stop")
    decision = runner._route_busy_session_event(event, "k", running_agent=MagicMock())
    assert decision.action == "halt"
    assert decision.reason == "halt_phrase_slash"


def test_router_default_queue_for_normal_text():
    """New default: ambiguous text routes to queue, SILENTLY (no auto-reaction).
    Reactions only swap in after the user explicitly picks via inline-keyboard
    buttons on the active tool bubble; default-queue is signaled by the
    'Queueing — pick to override' banner row."""
    runner = _make_runner()  # default mode = queue
    event = _make_event(text="by the way also include the docs")
    decision = runner._route_busy_session_event(event, "k", running_agent=MagicMock())
    assert decision.action == "queue"
    assert decision.reaction is None  # silent default — buttons signal queue
    assert decision.reason == "default_queue_pending_choice"


def test_router_legacy_interrupt_mode_returns_stop():
    runner = _make_runner(busy_input_mode="interrupt")
    event = _make_event(text="some long thoughtful follow-up text here")
    decision = runner._route_busy_session_event(event, "k", running_agent=MagicMock())
    assert decision.action == "stop"
    assert decision.reason == "legacy_interrupt_mode"


def test_router_queue_mode_returns_queue():
    runner = _make_runner(busy_input_mode="queue")
    event = _make_event(text="some long thoughtful follow-up text here")
    decision = runner._route_busy_session_event(event, "k", running_agent=MagicMock())
    assert decision.action == "queue"
    assert decision.reason == "busy_input_mode_queue"


def test_router_long_text_with_halt_buried_queues():
    """Halt-phrase length cap prevents false-positives on long messages.
    With the new default that means queue (was steer before round 2)."""
    runner = _make_runner()
    event = _make_event(text="we should stop including Bob in the email today")
    decision = runner._route_busy_session_event(event, "k", running_agent=MagicMock())
    assert decision.action == "queue"


def test_router_agent_pending_sentinel_queues():
    from gateway.run import _AGENT_PENDING_SENTINEL
    runner = _make_runner()
    event = _make_event(text="hi")
    decision = runner._route_busy_session_event(
        event, "k", running_agent=_AGENT_PENDING_SENTINEL
    )
    assert decision.action == "queue"
    assert decision.reason == "agent_pending"


def test_router_media_followup_queues():
    from gateway.platforms.base import MessageType
    runner = _make_runner()
    event = _make_event(text="", message_type=MessageType.PHOTO)
    decision = runner._route_busy_session_event(event, "k", running_agent=MagicMock())
    assert decision.action == "queue"
    assert decision.reason == "media_followup"


# --- Reaction emission via _emit_busy_session_signals -----------------------


def test_emit_signals_calls_set_busy_reaction_when_enabled():
    runner = _make_runner(busy_reactions_enabled=True, busy_ack_mode="reaction")
    decision = BusySessionDecision(
        action="steer", reason="default_steer", reaction=REACTION_STEER
    )
    event = _make_event(text="hi")
    adapter = MagicMock()
    adapter.set_busy_reaction = AsyncMock(return_value=True)
    adapter._send_with_retry = AsyncMock()

    asyncio.run(runner._emit_busy_session_signals(decision, event, "k", adapter, MagicMock()))

    adapter.set_busy_reaction.assert_awaited_once_with(event, REACTION_STEER)
    adapter._send_with_retry.assert_not_awaited()


def test_emit_signals_skips_reaction_when_disabled():
    runner = _make_runner(busy_reactions_enabled=False, busy_ack_mode="text")
    decision = BusySessionDecision(
        action="queue", reason="busy_input_mode_queue",
        message="⏳ queued.", reaction=REACTION_QUEUE,
    )
    event = _make_event(text="hi")
    adapter = MagicMock()
    adapter.set_busy_reaction = AsyncMock(return_value=True)
    adapter._send_with_retry = AsyncMock()

    asyncio.run(runner._emit_busy_session_signals(decision, event, "k", adapter, MagicMock()))

    adapter.set_busy_reaction.assert_not_awaited()
    adapter._send_with_retry.assert_awaited()


def test_emit_signals_text_mode_skips_reaction_emits_text():
    runner = _make_runner(busy_reactions_enabled=True, busy_ack_mode="text")
    decision = BusySessionDecision(
        action="queue", reason="busy_input_mode_queue",
        message="⏳ queued.", reaction=REACTION_QUEUE,
    )
    event = _make_event(text="hi")
    adapter = MagicMock()
    adapter.set_busy_reaction = AsyncMock(return_value=True)
    adapter._send_with_retry = AsyncMock()

    asyncio.run(runner._emit_busy_session_signals(decision, event, "k", adapter, MagicMock()))

    adapter.set_busy_reaction.assert_not_awaited()
    adapter._send_with_retry.assert_awaited()


def test_emit_signals_reaction_mode_skips_text():
    runner = _make_runner(busy_reactions_enabled=True, busy_ack_mode="reaction")
    decision = BusySessionDecision(
        action="queue", reason="busy_input_mode_queue",
        message="⏳ queued.", reaction=REACTION_QUEUE,
    )
    event = _make_event(text="hi")
    adapter = MagicMock()
    adapter.set_busy_reaction = AsyncMock(return_value=True)
    adapter._send_with_retry = AsyncMock()

    asyncio.run(runner._emit_busy_session_signals(decision, event, "k", adapter, MagicMock()))

    adapter.set_busy_reaction.assert_awaited_once()
    adapter._send_with_retry.assert_not_awaited()


def test_emit_signals_both_mode_emits_reaction_and_text():
    runner = _make_runner(busy_reactions_enabled=True, busy_ack_mode="both")
    decision = BusySessionDecision(
        action="queue", reason="busy_input_mode_queue",
        message="⏳ queued.", reaction=REACTION_QUEUE,
    )
    event = _make_event(text="hi")
    adapter = MagicMock()
    adapter.set_busy_reaction = AsyncMock(return_value=True)
    adapter._send_with_retry = AsyncMock()

    asyncio.run(runner._emit_busy_session_signals(decision, event, "k", adapter, MagicMock()))

    adapter.set_busy_reaction.assert_awaited_once()
    adapter._send_with_retry.assert_awaited()


def test_stop_phrase_does_not_queue_event_for_replay():
    """Regression: a stop phrase must NOT be re-prompted as the next turn.

    Pre-fix bug: stop fired interrupt AND queued the event, so after the
    interrupt the agent saw 'stop' as the next user prompt, producing a
    spurious follow-up turn. Codex pass-2 blocker.
    """
    from gateway.run import GatewayRunner

    runner = _make_runner(busy_input_mode="steer")
    decision = BusySessionDecision(
        action="stop", reason="stop_phrase_en", reaction=REACTION_STOP,
    )
    event = _make_event(text="stop")
    adapter = MagicMock()
    adapter._pending_messages = {}
    running_agent = MagicMock()

    asyncio.run(runner._apply_busy_session_decision(
        decision, event, "k", adapter, running_agent,
    ))

    # Agent must be interrupted but the event must NOT be queued.
    running_agent.interrupt.assert_called_once()
    assert "k" not in adapter._pending_messages, (
        "stop_phrase_* reasons must not queue the event for replay; "
        "user said stop, agent halts, no spurious follow-up turn"
    )


def test_legacy_interrupt_mode_still_queues_for_replay():
    """Backwards-compat: busy_input_mode=interrupt preserves the old
    behavior where the follow-up text becomes the next turn.
    """
    runner = _make_runner(busy_input_mode="interrupt")
    decision = BusySessionDecision(
        action="stop", reason="legacy_interrupt_mode", reaction=REACTION_STOP,
    )
    event = _make_event(text="please refactor this differently")
    adapter = MagicMock()
    adapter._pending_messages = {}
    running_agent = MagicMock()

    asyncio.run(runner._apply_busy_session_decision(
        decision, event, "k", adapter, running_agent,
    ))

    running_agent.interrupt.assert_called_once()
    # legacy_interrupt_mode keeps the event in pending so the post-interrupt
    # turn drives the new direction.
    assert "k" in adapter._pending_messages


def test_handle_button_tap_steer_pops_from_adapter_pending_messages():
    """Codex pass-1 BLOCKER fix: steer button must NOT double-deliver.

    Setup: event was queued (lives in adapter._pending_messages AND in
    runner._pending_followups). After [Steer] tap, runner.steer() injects
    mid-stream — but if we leave the event in adapter._pending_messages,
    the post-run drain processes it as a fresh next turn → duplicate
    delivery. Test guards against that regression.
    """
    from collections import deque
    runner = _make_runner()
    runner._pending_followups = {"k": deque()}
    runner._tool_bubble_msg_ids = {"k": 12345}
    e = _make_event(text="also include vector dbs")
    runner._pending_followups["k"].append(e)

    adapter = MagicMock()
    adapter.set_busy_reaction = AsyncMock(return_value=True)
    adapter.clear_busy_session_buttons = AsyncMock(return_value=True)
    adapter._pending_messages = {"k": [e]}  # also queued for next-turn drain
    running_agent = MagicMock()
    runner._running_agents = {"k": running_agent}

    asyncio.run(runner.handle_busy_session_button_tap("k", "steer", adapter))

    running_agent.steer.assert_called_once()
    # CRITICAL: the queued event was popped so post-run drain won't replay it.
    assert "k" not in adapter._pending_messages, (
        "steer button must remove the queued event from adapter._pending_messages "
        "or the user gets duplicate delivery (steered AND replayed as next turn)"
    )


def test_release_running_agent_state_clears_round2_state():
    """Codex pass-1 fix: _release_running_agent_state must clear the new
    _pending_followups and _tool_bubble_msg_ids maps so they don't leak
    across turns/sessions."""
    from collections import deque
    runner = _make_runner()
    runner._running_agents = {"k": MagicMock()}
    runner._running_agents_ts = {"k": 1.0}
    runner._busy_ack_ts = {"k": 1.0}
    runner._pending_followups = {"k": deque([_make_event(text="hi")])}
    runner._tool_bubble_msg_ids = {"k": 555}

    runner._release_running_agent_state("k")

    assert "k" not in runner._running_agents
    assert "k" not in runner._running_agents_ts
    assert "k" not in runner._busy_ack_ts
    assert "k" not in runner._pending_followups
    assert "k" not in runner._tool_bubble_msg_ids


def test_handle_button_tap_drains_pending_followups_and_steers():
    """Round 2: tapping [Steer] applies steer to ALL pending followups
    together — texts concatenated, primitive applied once, every queued
    message's ⏳ swaps to 👌."""
    from collections import deque
    runner = _make_runner()
    runner._pending_followups = {"k": deque()}
    runner._tool_bubble_msg_ids = {"k": 12345}
    e1 = _make_event(text="also include vector dbs")
    e2 = _make_event(text="and reflect on tradeoffs")
    runner._pending_followups["k"].append(e1)
    runner._pending_followups["k"].append(e2)

    adapter = MagicMock()
    adapter.set_busy_reaction = AsyncMock(return_value=True)
    adapter.attach_busy_session_buttons = AsyncMock(return_value=True)
    adapter.clear_busy_session_buttons = AsyncMock(return_value=True)
    running_agent = MagicMock()
    runner._running_agents = {"k": running_agent}

    result = asyncio.run(runner.handle_busy_session_button_tap("k", "steer", adapter))
    assert result is True

    # Steer was called once with concatenated text.
    running_agent.steer.assert_called_once()
    call_text = running_agent.steer.call_args[0][0]
    assert "vector dbs" in call_text and "reflect on tradeoffs" in call_text

    # Both events got their ⏳ swapped to 👌.
    assert adapter.set_busy_reaction.await_count == 2

    # Pending deque is drained.
    assert "k" not in runner._pending_followups

    # Buttons cleared from tool bubble.
    adapter.clear_busy_session_buttons.assert_awaited_once()


def test_handle_button_tap_halt_does_not_replay():
    """Halt should stop without replaying user text — agent.interrupt() with
    no message argument."""
    from collections import deque
    runner = _make_runner()
    runner._pending_followups = {"k": deque()}
    runner._tool_bubble_msg_ids = {"k": 999}
    runner._pending_followups["k"].append(_make_event(text="oops bad direction"))

    adapter = MagicMock()
    adapter.set_busy_reaction = AsyncMock(return_value=True)
    adapter.clear_busy_session_buttons = AsyncMock(return_value=True)
    adapter._pending_messages = {"k": [object()]}  # something stored
    running_agent = MagicMock()
    runner._running_agents = {"k": running_agent}

    asyncio.run(runner.handle_busy_session_button_tap("k", "halt", adapter))

    running_agent.interrupt.assert_called_once_with()  # no args = halt-only
    # Adapter pending_messages cleared so no replay.
    assert "k" not in adapter._pending_messages


def test_handle_button_tap_interrupt_replays_text():
    """Interrupt replays the concatenated text as next-turn input."""
    from collections import deque
    runner = _make_runner()
    runner._pending_followups = {"k": deque()}
    runner._tool_bubble_msg_ids = {"k": 999}
    runner._pending_followups["k"].append(_make_event(text="actually do X instead"))

    adapter = MagicMock()
    adapter.set_busy_reaction = AsyncMock(return_value=True)
    adapter.clear_busy_session_buttons = AsyncMock(return_value=True)
    running_agent = MagicMock()
    runner._running_agents = {"k": running_agent}

    asyncio.run(runner.handle_busy_session_button_tap("k", "interrupt", adapter))

    running_agent.interrupt.assert_called_once()
    msg_arg = running_agent.interrupt.call_args[0][0]
    assert "actually do X" in msg_arg


def test_handle_button_tap_no_pending_returns_false():
    """Race: tap arrives after queue caught up naturally. No-op + clear stale."""
    runner = _make_runner()
    runner._pending_followups = {}
    runner._tool_bubble_msg_ids = {"k": 999}

    adapter = MagicMock()
    adapter.clear_busy_session_buttons = AsyncMock(return_value=True)

    result = asyncio.run(runner.handle_busy_session_button_tap("k", "steer", adapter))
    assert result is False
    # Stale buttons cleared even though no work to do.
    adapter.clear_busy_session_buttons.assert_awaited_once()


def test_emit_signals_reaction_failure_does_not_raise():
    runner = _make_runner(busy_reactions_enabled=True, busy_ack_mode="reaction")
    decision = BusySessionDecision(
        action="steer", reason="default_steer", reaction=REACTION_STEER
    )
    event = _make_event(text="hi")
    adapter = MagicMock()
    adapter.set_busy_reaction = AsyncMock(side_effect=Exception("network down"))
    adapter._send_with_retry = AsyncMock()

    # Must not raise.
    asyncio.run(runner._emit_busy_session_signals(decision, event, "k", adapter, MagicMock()))
