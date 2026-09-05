"""Regression tests for #62034 — pending multi-choice clarify prompts must not
swallow unrelated thread follow-up messages.

When a NATIVE interactive multi-choice clarify (buttons rendered,
``awaiting_text=False``) is pending, the gateway text-intercept used to
consume ANY non-command message in the session as the clarify answer —
arbitrary prose vanished into clarify resolution and the agent appeared to
ignore the user's thread messages.

After the fix (``tools/clarify_gateway._coerce_text_response`` rejects
arbitrary prose for native multi-choice prompts):

  * numeric selections ("2") and exact choice labels still resolve, and
  * arbitrary prose falls through the intercept and continues as a normal
    message-handling turn.

Open-ended clarifies, explicit "Other" text-capture mode, and the base
adapter's numbered-text fallback (which flips ``awaiting_text`` at send time)
keep accepting free text.
"""

import threading
import time

from unittest.mock import patch

import pytest

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import (
    BasePlatformAdapter,
    MessageEvent,
    MessageType,
    SendResult,
)
from gateway.session import SessionSource


SESSION_KEY = "agent:main:slack:dm:D123:1111.2222"


class _StubAdapter(BasePlatformAdapter):
    def __init__(self):
        super().__init__(PlatformConfig(enabled=True, token="test"), Platform.SLACK)

    async def connect(self, *, is_reconnect: bool = False):
        return True

    async def disconnect(self):
        pass

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        return SendResult(success=True, message_id="m1")

    async def get_chat_info(self, chat_id):
        return {"id": chat_id, "type": "im"}


class _FellThroughIntercept(Exception):
    """Sentinel: _handle_message got PAST the clarify text-intercept."""


def _event(text, *, chat_type="dm", user_id="U1"):
    return MessageEvent(
        text=text,
        message_type=MessageType.TEXT,
        source=SessionSource(
            platform=Platform.SLACK,
            chat_id="D123",
            chat_type=chat_type,
            user_id=user_id,
            thread_id="1111.2222",
        ),
        message_id="msg1",
    )


def _clear_clarify_state():
    from tools import clarify_gateway as cm

    with cm._lock:
        cm._entries.clear()
        cm._session_index.clear()
        cm._notify_cbs.clear()


def _make_runner(adapter):
    from gateway.run import GatewayRunner

    runner = GatewayRunner.__new__(GatewayRunner)
    runner._startup_restore_in_progress = False
    runner._scale_to_zero_note_real_inbound = lambda: None
    runner._is_user_authorized = lambda source: True
    runner._session_key_for_source = lambda source: SESSION_KEY
    runner._adapter_for_source = lambda source: adapter
    runner._update_prompt_pending = {}
    return runner


async def _dispatch(runner, event):
    """Run _handle_message with a tripwire installed AFTER the clarify
    intercept (the slash-confirm pending lookup is the next statement), so a
    raised ``_FellThroughIntercept`` proves the message was NOT swallowed."""
    import tools.slash_confirm as slash_confirm_mod

    def _tripwire(_key):
        raise _FellThroughIntercept()

    with patch("hermes_cli.plugins.invoke_hook", return_value=[]), \
            patch.object(slash_confirm_mod, "get_pending", _tripwire):
        return await runner._handle_message(event)


@pytest.mark.asyncio
async def test_thread_prose_not_swallowed_by_native_multi_choice_clarify():
    """Arbitrary prose during a pending button-clarify continues as a normal turn."""
    _clear_clarify_state()
    from tools import clarify_gateway as cm

    adapter = _StubAdapter()
    runner = _make_runner(adapter)
    # Native interactive multi-choice prompt: awaiting_text stays False.
    entry = cm.register("cl-native", SESSION_KEY, "Pick a UI variant", ["buttons", "dropdown"])
    assert entry.awaiting_text is False

    with pytest.raises(_FellThroughIntercept):
        await _dispatch(runner, _event("just checking the visual UI, no need to pass any data"))

    # The prose is not accepted as the answer, but the clarify must be
    # released before normal busy routing so redirect-to-steer can drain.
    with cm._lock:
        entry = cm._entries.get("cl-native")
    assert entry is not None
    assert not entry.event.is_set()
    assert entry.response is None
    _clear_clarify_state()


@pytest.mark.asyncio
async def test_thread_prose_does_not_overwrite_concurrent_button_choice():
    """A button result that wins the race remains the clarify response."""
    _clear_clarify_state()
    from tools import clarify_gateway as cm

    adapter = _StubAdapter()
    runner = _make_runner(adapter)
    entry = cm.register(
        "cl-button-race",
        SESSION_KEY,
        "Pick a UI variant",
        ["buttons", "dropdown"],
    )
    assert cm.resolve_gateway_clarify("cl-button-race", "buttons") is True

    with pytest.raises(_FellThroughIntercept):
        await _dispatch(runner, _event("one more unrelated thought"))

    assert entry.event.is_set()
    assert entry.response == "buttons"
    _clear_clarify_state()


@pytest.mark.asyncio
async def test_native_multi_select_out_of_range_keeps_clarify_pending():
    """Out-of-range multi-select numbers must not cancel the pending prompt."""
    _clear_clarify_state()
    from tools import clarify_gateway as cm

    adapter = _StubAdapter()
    runner = _make_runner(adapter)
    entry = cm.register(
        "cl-ms-oor",
        SESSION_KEY,
        "Pick some targets",
        ["staging", "prod", "canary"],
        multi_select=True,
    )
    assert entry.awaiting_text is False

    result = await _dispatch(runner, _event("99"))

    assert result == ""
    with cm._lock:
        still = cm._entries.get("cl-ms-oor")
    assert still is not None
    assert not still.event.is_set()
    assert still.response is None
    _clear_clarify_state()


@pytest.mark.asyncio
async def test_native_multi_select_bad_comma_list_keeps_clarify_pending():
    """Unrecognised comma-lists are retryable selection attempts, not prose."""
    _clear_clarify_state()
    from tools import clarify_gateway as cm

    adapter = _StubAdapter()
    runner = _make_runner(adapter)
    entry = cm.register(
        "cl-ms-bad",
        SESSION_KEY,
        "Pick some targets",
        ["staging", "prod", "canary"],
        multi_select=True,
    )
    assert entry.awaiting_text is False

    result = await _dispatch(runner, _event("1,99"))

    assert result == ""
    with cm._lock:
        still = cm._entries.get("cl-ms-bad")
    assert still is not None
    assert not still.event.is_set()
    assert still.response is None
    _clear_clarify_state()


@pytest.mark.asyncio
async def test_native_multi_select_prose_releases_clarify_before_routing():
    """Free prose on multi-select still breaks the redirect/steer deadlock."""
    _clear_clarify_state()
    from tools import clarify_gateway as cm

    adapter = _StubAdapter()
    runner = _make_runner(adapter)
    cm.register(
        "cl-ms-prose",
        SESSION_KEY,
        "Pick some targets",
        ["staging", "prod"],
        multi_select=True,
    )

    with pytest.raises(_FellThroughIntercept):
        await _dispatch(
            runner,
            _event("just checking the visual UI, no need to pass any data"),
        )

    with cm._lock:
        entry = cm._entries.get("cl-ms-prose")
    assert entry is not None
    assert not entry.event.is_set()
    assert entry.response is None
    _clear_clarify_state()


@pytest.mark.asyncio
async def test_prose_still_accepted_after_other_flips_text_capture():
    """After the user taps 'Other', free text IS the answer — must resolve."""
    _clear_clarify_state()
    from tools import clarify_gateway as cm

    adapter = _StubAdapter()
    runner = _make_runner(adapter)
    cm.register("cl-other", SESSION_KEY, "Pick a UI variant", ["buttons", "dropdown"])
    assert cm.mark_awaiting_text("cl-other") is True

    result = await _dispatch(runner, _event("a carousel actually"))

    assert result == ""
    with cm._lock:
        entry = cm._entries.get("cl-other")
    assert entry is not None
    assert entry.event.is_set()
    assert entry.response == "a carousel actually"
    _clear_clarify_state()



@pytest.mark.asyncio
async def test_draining_gateway_does_not_prequeue_or_claim_clarify_prose():
    _clear_clarify_state()
    from tools import clarify_gateway as cm

    adapter = _StubAdapter()
    adapter._pending_messages = {}
    runner = _make_busy_runner(adapter)
    runner._draining = True
    entry = cm.register("cl-drain", SESSION_KEY, "Pick", ["A", "B"])

    with pytest.raises(_FellThroughIntercept):
        await _dispatch(runner, _event("new request during restart"))

    assert adapter._pending_messages == {}
    assert entry.superseding is False
    assert not entry.event.is_set()
    _clear_clarify_state()



@pytest.mark.asyncio
async def test_internal_event_queues_without_superseding_or_interrupting_clarify():
    """Synthetic completions cascade silently while the user prompt stays pending."""
    _clear_clarify_state()
    from tools import clarify_gateway as cm

    adapter = _StubAdapter()
    adapter._pending_messages = {}
    runner = _make_busy_runner(adapter)
    cm.register("cl-internal", SESSION_KEY, "Pick a UI variant", ["buttons", "dropdown"])

    event = _event("Background process completed")
    event.internal = True
    result = await _dispatch_full(runner, event)

    assert result is None
    with cm._lock:
        entry = cm._entries.get("cl-internal")
    assert entry is not None
    assert not entry.event.is_set()
    # The completion is preserved as a cascading next turn, not discarded.
    assert list(adapter._pending_messages) == [SESSION_KEY]
    _clear_clarify_state()



@pytest.mark.asyncio
async def test_invalid_numeric_selection_keeps_native_choice_clarify_pending():
    """A mistyped/out-of-range numbered selection ("3" for a 2-choice prompt)
    is a selection attempt, not prose: keep the clarify pending for retry —
    do NOT supersede it and do NOT queue the reply as a turn."""
    _clear_clarify_state()
    from tools import clarify_gateway as cm

    adapter = _StubAdapter()
    adapter._pending_messages = {}
    runner = _make_busy_runner(adapter)

    cm.register("cl-oob", SESSION_KEY, "Pick a UI variant", ["buttons", "dropdown"])

    result = await _dispatch_full(runner, _event("3"))

    # Intercepted silently; the buttons stay visible for a valid retry.
    assert result == ""
    # The clarify is still pending and its waiter was NOT released.
    with cm._lock:
        entry = cm._entries.get("cl-oob")
    assert entry is not None
    assert not entry.event.is_set()
    # The invalid selection was not queued as a follow-up turn.
    assert adapter._pending_messages == {}

    # A subsequent valid selection still resolves the same pending clarify.
    result2 = await _dispatch_full(runner, _event("1"))
    assert result2 == ""
    with cm._lock:
        entry = cm._entries.get("cl-oob")
    assert entry is not None
    assert entry.event.is_set()
    assert entry.response == "buttons"
    _clear_clarify_state()



@pytest.mark.asyncio
async def test_prose_does_not_clobber_button_resolved_clarify(monkeypatch):
    """Race: a button tap resolved the clarify (event+response set) but the
    waiter has not cleaned it up when a prose follow-up arrives. The supersede
    must NOT overwrite the real button answer with the empty sentinel."""
    _clear_clarify_state()
    from tools import clarify_gateway as cm

    monkeypatch.setenv("HERMES_GATEWAY_BUSY_ACK_ENABLED", "false")

    adapter = _StubAdapter()
    adapter._pending_messages = {}
    runner = _make_busy_runner(adapter)

    cm.register("cl-race", SESSION_KEY, "Pick a UI variant", ["buttons", "dropdown"])
    # Button tap resolves the entry; no waiter has run cleanup yet, so it is
    # still listed in the session index.
    assert cm.resolve_gateway_clarify("cl-race", "buttons") is True

    result = await _dispatch_full(runner, _event("actually never mind, different topic"))

    assert result is None
    # The real button answer must be preserved, not clobbered with "".
    with cm._lock:
        entry = cm._entries.get("cl-race")
    assert entry is not None
    assert entry.event.is_set()
    assert entry.response == "buttons"
    # The prose still lands as a follow-up turn.
    assert list(adapter._pending_messages.keys()) == [SESSION_KEY]
    _clear_clarify_state()



@pytest.mark.asyncio
async def test_prose_supersedes_native_choice_clarify_and_queues_once(monkeypatch):
    """Arbitrary prose during a pending button-clarify cancels the clarify
    (unblocking the waiting agent thread) and enters busy handling exactly
    once — it must NOT wait behind the blocked clarify (2026-07-29 regression).
    """
    _clear_clarify_state()
    from tools import clarify_gateway as cm

    # Isolate the queue/supersede behavior from busy-ack rendering (which is
    # covered by test_busy_session_ack.py) so the test stays hermetic.
    monkeypatch.setenv("HERMES_GATEWAY_BUSY_ACK_ENABLED", "false")

    adapter = _StubAdapter()
    adapter._pending_messages = {}
    runner = _make_busy_runner(adapter)

    # Native interactive multi-choice prompt: awaiting_text stays False.
    entry = cm.register("cl-native", SESSION_KEY, "Pick a UI variant", ["buttons", "dropdown"])
    assert entry.awaiting_text is False

    original_busy_handler = runner._handle_active_session_busy_message

    async def _assert_claim_precedes_busy(*args, **kwargs):
        # A button tap at the old vulnerable interleaving must lose to the
        # already-claimed prose handoff; the waiter is still blocked here.
        assert entry.superseding is True
        assert not entry.event.is_set()
        assert cm.resolve_gateway_clarify("cl-native", "buttons") is False
        return await original_busy_handler(*args, **kwargs)

    monkeypatch.setattr(
        runner, "_handle_active_session_busy_message", _assert_claim_precedes_busy
    )

    # A REAL agent worker thread blocked on wait_for_response, exactly as the
    # gateway clarify callback blocks it in production.
    captured = {}

    def _agent_thread():
        captured["response"] = cm.wait_for_response("cl-native", timeout=3600.0)

    worker = threading.Thread(target=_agent_thread, daemon=True)
    worker.start()
    time.sleep(0.05)  # let the thread enter the wait loop

    result = await _dispatch_full(
        runner, _event("just checking the visual UI, no need to pass any data")
    )

    # Consumed by busy handling — no cold-start second turn was dispatched.
    assert result == ""

    # The clarify was superseded: the waiting thread unblocked with the empty
    # sentinel and the entry was dropped.
    worker.join(timeout=2.0)
    assert not worker.is_alive()
    assert captured["response"] == ""
    with cm._lock:
        assert cm._entries.get("cl-native") is None

    # The prose was queued exactly once for the next turn (busy queue mode),
    # not swallowed and not duplicated.
    assert list(adapter._pending_messages.keys()) == [SESSION_KEY]
    assert getattr(runner, "_queued_events", {}) in ({}, {SESSION_KEY: []})

    # No second agent turn was started for this session.
    assert list(runner._running_agents.keys()) == [SESSION_KEY]
    _clear_clarify_state()



@pytest.mark.asyncio
async def test_queue_rejection_leaves_clarify_pending_and_reports_retry(monkeypatch):
    _clear_clarify_state()
    from tools import clarify_gateway as cm

    adapter = _StubAdapter()
    adapter._pending_messages = {}
    runner = _make_busy_runner(adapter)
    entry = cm.register("cl-full", SESSION_KEY, "Pick", ["A", "B"])

    monkeypatch.setattr(runner, "_queue_or_replace_pending_event", lambda *_: False)
    result = await _dispatch_full(runner, _event("new unrelated request"))

    assert "Queue is full" in result
    assert not entry.event.is_set()
    assert entry.superseding is False
    assert cm.get_pending_for_session(SESSION_KEY, include_choice_prompts=True) is entry
    _clear_clarify_state()



@pytest.mark.asyncio
async def test_unauthorized_prose_cannot_supersede_pending_clarify():
    """An unauthorized sender's prose must not cancel a pending clarify."""
    _clear_clarify_state()
    from tools import clarify_gateway as cm

    adapter = _StubAdapter()
    adapter._pending_messages = {}
    runner = _make_busy_runner(adapter)
    runner._is_user_authorized = lambda source: False

    cm.register("cl-auth", SESSION_KEY, "Pick a UI variant", ["buttons", "dropdown"])

    # Non-DM so the unauthorized path silently drops instead of entering the
    # DM pairing flow.
    result = await _dispatch_full(
        runner, _event("cancel that please", chat_type="channel", user_id="intruder")
    )

    assert result is None
    # The clarify must still be pending and unresolved.
    with cm._lock:
        entry = cm._entries.get("cl-auth")
    assert entry is not None
    assert not entry.event.is_set()
    # Nothing was queued on the unauthorized sender's behalf.
    assert adapter._pending_messages == {}
    _clear_clarify_state()



async def _dispatch_full(runner, event):
    """Run _handle_message with only the plugin hook stubbed — used for the
    supersede path, which must reach the busy handler rather than fall through
    to the slash-confirm lookup."""
    with patch("hermes_cli.plugins.invoke_hook", return_value=[]):
        return await runner._handle_message(event)



class _StubAgent:
    """A running agent with recent activity so the staleness sweep in
    _handle_message does not evict it before the busy path runs."""

    def get_activity_summary(self):
        return {
            "seconds_since_activity": 0,
            "api_call_count": 1,
            "max_iterations": 60,
            "current_tool": None,
        }



def _make_busy_runner(adapter):
    """A runner whose session is actively running an agent turn (busy),
    configured for busy_input_mode=queue like the production gateway."""
    runner = _make_runner(adapter)
    runner._running_agents = {SESSION_KEY: _StubAgent()}
    runner._running_agents_ts = {SESSION_KEY: time.time()}
    runner._busy_input_mode = "queue"
    runner._busy_text_mode = "queue"
    runner._busy_ack_ts = {}
    runner._draining = False
    return runner

