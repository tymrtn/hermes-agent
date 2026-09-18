"""Tests for the cleanup_progress retention window (held progress bubbles).

``cleanup_progress`` alone deletes a turn's bubbles as soon as its final response lands.
``cleanup_progress_idle_seconds`` / ``cleanup_progress_max_exchanges`` instead HOLD them in a
session-scoped registry (gateway/run_meta_cleanup.py) until the session goes quiet or N tracked
turns have passed, and ``cleanup_progress_on_reset`` flushes the hold when the session rotates.

The lifecycle contract under concurrent turns: a delete armed by turn N must never fire against
ids turn N+1 is still accumulating, and a turn that contributes no bubbles of its own must still
re-arm the hold it paused rather than stranding it forever.
"""

import asyncio
import importlib
from types import SimpleNamespace

import pytest

from gateway.config import Platform
from gateway.session import SessionSource
from gateway.turn_context import TurnContext

from tests.gateway.test_run_cleanup_progress import (
    CleanupCaptureAdapter,
    NoDeleteAdapter,
    ProgressAgent,
    _fire_post_delivery_cb,
    _install_fakes,
    _make_runner,
)

SESSION_KEY = "agent:main:telegram:dm:42"


def _bare_runner():
    """A GatewayRunner with no __init__ — the hold registry is created lazily on first use."""
    GatewayRunner = importlib.import_module("gateway.run").GatewayRunner
    return object.__new__(GatewayRunner)


async def _wait_for_deletes(adapter, count, *, tries=40):
    for _ in range(tries):
        if len(adapter.deleted) >= count:
            return
        await asyncio.sleep(0.01)


def _held(runner):
    return getattr(runner, "_meta_cleanup_pending", {})


# ---------------------------------------------------------------------------
# Retention window: idle / exchange thresholds
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_idle_window_delays_deletion_until_quiet():
    """An idle window keeps bubbles readable, then deletes them after the quiet period."""
    adapter, runner = CleanupCaptureAdapter(), _bare_runner()

    runner._schedule_meta_message_cleanup(
        session_key=SESSION_KEY, chat_id="42", adapter=adapter, message_ids=["m1", "m2"],
        loop=asyncio.get_running_loop(), idle_seconds=0.05, max_exchanges=0,
    )

    await asyncio.sleep(0)
    assert adapter.deleted == []

    await _wait_for_deletes(adapter, 2)
    assert adapter.deleted == [
        {"chat_id": "42", "message_id": "m1"},
        {"chat_id": "42", "message_id": "m2"},
    ]
    assert _held(runner) == {}


@pytest.mark.asyncio
async def test_exchange_threshold_deletes_before_the_idle_timer():
    """Reaching max_exchanges deletes the accumulated bubbles without waiting out the idle timer."""
    adapter, runner = CleanupCaptureAdapter(), _bare_runner()
    loop = asyncio.get_running_loop()

    runner._schedule_meta_message_cleanup(
        session_key=SESSION_KEY, chat_id="42", adapter=adapter, message_ids=["m1"],
        loop=loop, idle_seconds=60, max_exchanges=2,
    )
    await asyncio.sleep(0)
    assert adapter.deleted == []

    # m1 repeats: a turn re-reporting an id it already handed over must not delete it twice.
    runner._schedule_meta_message_cleanup(
        session_key=SESSION_KEY, chat_id="42", adapter=adapter, message_ids=["m2", "m1"],
        loop=loop, idle_seconds=60, max_exchanges=2,
    )

    await _wait_for_deletes(adapter, 2)
    assert adapter.deleted == [
        {"chat_id": "42", "message_id": "m1"},
        {"chat_id": "42", "message_id": "m2"},
    ]


@pytest.mark.asyncio
async def test_exchange_only_mode_never_falls_back_to_immediate_delete():
    """With no idle window, an unmet exchange limit holds without scheduling anything."""
    adapter, runner = CleanupCaptureAdapter(), _bare_runner()
    loop = asyncio.get_running_loop()

    runner._schedule_meta_message_cleanup(
        session_key=SESSION_KEY, chat_id="42", adapter=adapter, message_ids=["m1"],
        loop=loop, idle_seconds=0, max_exchanges=2,
    )
    await asyncio.sleep(0.03)
    assert adapter.deleted == []
    assert _held(runner)[SESSION_KEY]["future"] is None

    runner._schedule_meta_message_cleanup(
        session_key=SESSION_KEY, chat_id="42", adapter=adapter, message_ids=["m2"],
        loop=loop, idle_seconds=0, max_exchanges=2,
    )
    await _wait_for_deletes(adapter, 2)
    assert [d["message_id"] for d in adapter.deleted] == ["m1", "m2"]


@pytest.mark.asyncio
async def test_hold_skips_adapters_without_delete_support():
    """An adapter that inherits the base no-op delete_message is never held."""
    adapter, runner = NoDeleteAdapter(), _bare_runner()

    runner._schedule_meta_message_cleanup(
        session_key=SESSION_KEY, chat_id="42", adapter=adapter, message_ids=["m1"],
        loop=asyncio.get_running_loop(), idle_seconds=0.01, max_exchanges=0,
    )
    await asyncio.sleep(0.05)
    assert _held(runner) == {}
    assert adapter.deleted == []


# ---------------------------------------------------------------------------
# Lifecycle contract under concurrent / overlapping turns
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pause_cancels_a_pending_idle_delete():
    """A fresh turn pauses the hold, so an idle delete cannot land mid-conversation."""
    adapter, runner = CleanupCaptureAdapter(), _bare_runner()
    loop = asyncio.get_running_loop()

    runner._schedule_meta_message_cleanup(
        session_key=SESSION_KEY, chat_id="42", adapter=adapter, message_ids=["m1"],
        loop=loop, idle_seconds=0.05, max_exchanges=0,
    )
    runner._pause_meta_cleanup_for_session(SESSION_KEY)

    await asyncio.sleep(0.08)
    assert adapter.deleted == []
    assert _held(runner)[SESSION_KEY]["ids"] == ["m1"]

    runner._schedule_meta_message_cleanup(
        session_key=SESSION_KEY, chat_id="42", adapter=adapter, message_ids=["m2"],
        loop=loop, idle_seconds=0, max_exchanges=0,
    )
    await _wait_for_deletes(adapter, 2)
    assert [d["message_id"] for d in adapter.deleted] == ["m1", "m2"]


@pytest.mark.asyncio
async def test_a_superseded_timer_never_deletes_the_next_turns_bubbles():
    """The token guard: turn N's in-flight delete must lose to turn N+1's re-arm, not race it.

    Turn N arms a short idle delete. Before it fires, turn N+1 re-arms with a long one. The old
    coroutine wakes up holding a stale token and must return without touching either id.
    """
    adapter, runner = CleanupCaptureAdapter(), _bare_runner()
    loop = asyncio.get_running_loop()

    runner._schedule_meta_message_cleanup(
        session_key=SESSION_KEY, chat_id="42", adapter=adapter, message_ids=["old"],
        loop=loop, idle_seconds=0.02, max_exchanges=0,
    )
    stale_future = _held(runner)[SESSION_KEY]["future"]
    # Re-arm WITHOUT letting the first timer be cancelled, so the stale coroutine really does wake.
    _held(runner)[SESSION_KEY]["future"] = None
    runner._schedule_meta_message_cleanup(
        session_key=SESSION_KEY, chat_id="42", adapter=adapter, message_ids=["fresh"],
        loop=loop, idle_seconds=5, max_exchanges=0,
    )

    await asyncio.sleep(0.08)
    assert adapter.deleted == []
    assert _held(runner)[SESSION_KEY]["ids"] == ["old", "fresh"]
    assert stale_future is not _held(runner)[SESSION_KEY]["future"]


@pytest.mark.asyncio
async def test_resume_rearms_a_paused_hold_with_no_new_ids():
    """A progress-free follow-up turn must not strand ids the previous turn handed over."""
    adapter, runner = CleanupCaptureAdapter(), _bare_runner()
    loop = asyncio.get_running_loop()

    runner._schedule_meta_message_cleanup(
        session_key=SESSION_KEY, chat_id="42", adapter=adapter, message_ids=["m1"],
        loop=loop, idle_seconds=60, max_exchanges=0,
    )
    runner._pause_meta_cleanup_for_session(SESSION_KEY)
    assert runner._has_paused_meta_cleanup(SESSION_KEY) is True

    runner._resume_paused_meta_cleanup_for_session(
        SESSION_KEY, loop=loop, idle_seconds=0.01, max_exchanges=0,
    )
    await _wait_for_deletes(adapter, 1)

    assert adapter.deleted == [{"chat_id": "42", "message_id": "m1"}]
    assert _held(runner) == {}


@pytest.mark.asyncio
async def test_resume_is_a_no_op_when_nothing_is_held():
    """Resume must not invent an entry for a session that never held anything."""
    runner = _bare_runner()
    runner._resume_paused_meta_cleanup_for_session(
        SESSION_KEY, loop=asyncio.get_running_loop(), idle_seconds=0.01,
    )
    assert _held(runner) == {}
    assert runner._has_paused_meta_cleanup(SESSION_KEY) is False


# ---------------------------------------------------------------------------
# Reset flush
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reset_flush_deletes_held_bubbles_immediately():
    """/new deletes the held bubbles before the session key rotates away from them."""
    adapter, runner = CleanupCaptureAdapter(), _bare_runner()

    runner._schedule_meta_message_cleanup(
        session_key=SESSION_KEY, chat_id="42", adapter=adapter, message_ids=["m1", "m2"],
        loop=asyncio.get_running_loop(), idle_seconds=60, max_exchanges=0,
    )
    await runner._flush_meta_cleanup_for_session(SESSION_KEY)

    assert adapter.deleted == [
        {"chat_id": "42", "message_id": "m1"},
        {"chat_id": "42", "message_id": "m2"},
    ]
    assert _held(runner) == {}


@pytest.mark.asyncio
async def test_reset_flush_respects_cleanup_progress_on_reset_false(monkeypatch):
    """Opting out of the reset flush leaves the hold — and its ids — intact."""
    gateway_run = importlib.import_module("gateway.run")
    adapter, runner = CleanupCaptureAdapter(), _bare_runner()

    runner._schedule_meta_message_cleanup(
        session_key=SESSION_KEY, chat_id="42", adapter=adapter, message_ids=["m1"],
        loop=asyncio.get_running_loop(), idle_seconds=60, max_exchanges=0,
    )
    monkeypatch.setattr(
        gateway_run, "_load_gateway_config",
        lambda: {"display": {"platforms": {"telegram": {"cleanup_progress_on_reset": False}}}},
    )

    await runner._flush_meta_cleanup_for_session(SESSION_KEY, platform_key="telegram")

    assert adapter.deleted == []
    assert _held(runner)[SESSION_KEY]["ids"] == ["m1"]


@pytest.mark.asyncio
async def test_reset_flush_on_an_unheld_session_is_a_no_op():
    """Every /new hits this path; a runner that never held anything must not blow up."""
    runner = _bare_runner()
    await runner._flush_meta_cleanup_for_session(SESSION_KEY)
    await runner._flush_meta_cleanup_for_session(None)
    assert _held(runner) == {}


# ---------------------------------------------------------------------------
# Turn wiring: which path a turn takes
# ---------------------------------------------------------------------------


def _turn_ctx(adapter, *, idle=0.0, exchanges=0, ids=("b1", "b2")):
    return TurnContext(
        source=SessionSource(platform=Platform.TELEGRAM, chat_id="42"),
        session_key=SESSION_KEY, _cleanup_progress=True, _cleanup_msg_ids=list(ids),
        _cleanup_idle_seconds=idle, _cleanup_max_exchanges=exchanges,
    )


@pytest.mark.asyncio
async def test_no_retention_window_keeps_the_immediate_delete_path():
    """Default config is byte-for-byte the upstream behavior: delete now, hold nothing."""
    adapter, runner = CleanupCaptureAdapter(), _bare_runner()
    ctx = _turn_ctx(adapter)

    runner._run_agent_schedule_bubble_cleanup({"final_response": "ok"}, adapter, ctx)
    cb = adapter.pop_post_delivery_callback(SESSION_KEY)
    await _fire_post_delivery_cb(cb)
    await _wait_for_deletes(adapter, 2)

    assert [d["message_id"] for d in adapter.deleted] == ["b1", "b2"]
    assert _held(runner) == {}


@pytest.mark.asyncio
async def test_retention_window_holds_the_turns_bubbles_instead_of_deleting():
    adapter, runner = CleanupCaptureAdapter(), _bare_runner()
    ctx = _turn_ctx(adapter, idle=5)

    runner._run_agent_schedule_bubble_cleanup({"final_response": "ok"}, adapter, ctx)
    await _fire_post_delivery_cb(adapter.pop_post_delivery_callback(SESSION_KEY))
    await asyncio.sleep(0.02)

    assert adapter.deleted == []
    assert _held(runner)[SESSION_KEY]["ids"] == ["b1", "b2"]


@pytest.mark.asyncio
async def test_a_failed_turn_rearms_the_hold_it_paused():
    """A failed run keeps its own bubbles as breadcrumbs but must not strand the earlier hold."""
    adapter, runner = CleanupCaptureAdapter(), _bare_runner()
    loop = asyncio.get_running_loop()

    runner._schedule_meta_message_cleanup(
        session_key=SESSION_KEY, chat_id="42", adapter=adapter, message_ids=["earlier"],
        loop=loop, idle_seconds=60, max_exchanges=0,
    )
    runner._pause_meta_cleanup_for_session(SESSION_KEY)

    ctx = _turn_ctx(adapter, idle=0.01, ids=("failed-bubble",))
    runner._run_agent_schedule_bubble_cleanup({"final_response": "", "failed": True}, adapter, ctx)
    await _fire_post_delivery_cb(adapter.pop_post_delivery_callback(SESSION_KEY))
    await _wait_for_deletes(adapter, 1)

    assert [d["message_id"] for d in adapter.deleted] == ["earlier"]


@pytest.mark.asyncio
async def test_a_failed_turn_with_nothing_held_registers_no_callback():
    adapter, runner = CleanupCaptureAdapter(), _bare_runner()
    ctx = _turn_ctx(adapter, idle=5)

    runner._run_agent_schedule_bubble_cleanup({"final_response": "", "failed": True}, adapter, ctx)

    assert adapter.pop_post_delivery_callback(SESSION_KEY) is None
    assert _held(runner) == {}


@pytest.mark.asyncio
async def test_run_agent_end_to_end_holds_bubbles_when_configured(monkeypatch, tmp_path):
    """Config → turn context → hold registry, through the real _run_agent path."""
    adapter = CleanupCaptureAdapter()
    runner = _make_runner(adapter)
    gateway_run = _install_fakes(monkeypatch, ProgressAgent, cleanup_on=True)
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    monkeypatch.setattr(gateway_run, "_load_gateway_config", lambda: {
        "display": {"platforms": {"telegram": {
            "cleanup_progress": True, "cleanup_progress_idle_seconds": 30,
        }}}
    })

    result = await runner._run_agent(
        message="hello", context_prompt="", history=[],
        source=SessionSource(platform=Platform.TELEGRAM, chat_id="-1001"),
        session_id="sess-1", session_key=SESSION_KEY,
    )
    assert result["final_response"] == "done"

    await _fire_post_delivery_cb(adapter.pop_post_delivery_callback(SESSION_KEY))
    await asyncio.sleep(0.02)

    assert adapter.deleted == []
    assert _held(runner)[SESSION_KEY]["ids"], "progress bubbles should be held, not dropped"


# ---------------------------------------------------------------------------
# Background-review notices
# ---------------------------------------------------------------------------


def _bg_runner_ctx(adapter, *, idle=0.0, exchanges=0):
    from gateway.run_turn_runner import TurnRunner
    runner = _bare_runner()
    ctx = TurnContext(
        source=SessionSource(platform=Platform.TELEGRAM, chat_id="42"),
        session_key=SESSION_KEY, _cleanup_progress=True,
        _cleanup_idle_seconds=idle, _cleanup_max_exchanges=exchanges,
    )
    ctx._status_adapter, ctx._status_chat_id = adapter, "42"
    ctx._status_thread_metadata = None
    ctx._run_still_current = lambda: True
    ctx._loop_for_step = asyncio.get_event_loop()
    turn_runner = object.__new__(TurnRunner)
    turn_runner._ctx, turn_runner._runner = ctx, runner
    turn_runner._schedule = lambda coro, msg: asyncio.ensure_future(coro)
    return runner, turn_runner


@pytest.mark.asyncio
async def test_background_review_notice_is_held_when_a_window_is_configured():
    """"💾 Memory updated" lands after the turn's own cleanup, so only the hold can retire it."""
    adapter = CleanupCaptureAdapter()
    runner, turn_runner = _bg_runner_ctx(adapter, idle=5)

    send, release = turn_runner._make_bg_review_callbacks()
    release()
    send("💾 Memory updated")
    for _ in range(40):
        await asyncio.sleep(0.01)
        if _held(runner):
            break

    assert [s["content"] for s in adapter.sent] == ["💾 Memory updated"]
    assert _held(runner)[SESSION_KEY]["ids"] == [adapter.sent[0]["message_id"]]
    # A notice is not a turn: it restarts the quiet window without counting toward max_exchanges.
    assert _held(runner)[SESSION_KEY]["exchanges"] == 0


@pytest.mark.asyncio
async def test_background_review_notice_is_left_alone_without_a_window():
    adapter = CleanupCaptureAdapter()
    runner, turn_runner = _bg_runner_ctx(adapter)

    send, release = turn_runner._make_bg_review_callbacks()
    release()
    send("💾 Memory updated")
    await asyncio.sleep(0.05)

    assert [s["content"] for s in adapter.sent] == ["💾 Memory updated"]
    assert _held(runner) == {}
    assert adapter.deleted == []


# ---------------------------------------------------------------------------
# Config resolution
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("raw,expected", [(None, 0), (0, 0), ("45", 45.0), (12.5, 12.5), (-3, 0.0), ("nope", 0.0)])
def test_idle_seconds_resolves_to_a_non_negative_float(raw, expected):
    from gateway.display_config import resolve_display_setting
    cfg = {} if raw is None else {"display": {"platforms": {"telegram": {"cleanup_progress_idle_seconds": raw}}}}
    assert resolve_display_setting(cfg, "telegram", "cleanup_progress_idle_seconds") == expected


@pytest.mark.parametrize("raw,expected", [(None, 0), ("3", 3), (-1, 0), ("nope", 0)])
def test_max_exchanges_resolves_to_a_non_negative_int(raw, expected):
    from gateway.display_config import resolve_display_setting
    cfg = {} if raw is None else {"display": {"platforms": {"telegram": {"cleanup_progress_max_exchanges": raw}}}}
    assert resolve_display_setting(cfg, "telegram", "cleanup_progress_max_exchanges") == expected


@pytest.mark.parametrize("raw,expected", [(None, True), ("off", False), ("no", False), (False, False), ("yes", True)])
def test_on_reset_defaults_to_true_and_normalises_yaml_tokens(raw, expected):
    from gateway.display_config import resolve_display_setting
    cfg = {} if raw is None else {"display": {"platforms": {"telegram": {"cleanup_progress_on_reset": raw}}}}
    assert resolve_display_setting(cfg, "telegram", "cleanup_progress_on_reset") is expected


def test_retention_window_reads_the_platform_override():
    from gateway.display_config import resolve_display_setting
    runner = _bare_runner()
    disp = SimpleNamespace(
        user_config={"display": {"platforms": {"telegram": {
            "cleanup_progress_idle_seconds": 90, "cleanup_progress_max_exchanges": 4,
        }}}},
        platform_key="telegram", resolve_display_setting=resolve_display_setting,
    )
    assert runner._cleanup_retention_window(disp) == (90.0, 4)
    assert runner._cleanup_retention_window(
        SimpleNamespace(user_config={}, platform_key="telegram",
                        resolve_display_setting=resolve_display_setting)
    ) == (0.0, 0)


def test_new_keys_are_platform_overrideable():
    from gateway.display_config import OVERRIDEABLE_KEYS
    assert {
        "cleanup_progress_idle_seconds", "cleanup_progress_max_exchanges", "cleanup_progress_on_reset",
    } <= OVERRIDEABLE_KEYS
