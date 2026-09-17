"""Regression tests: safe slash commands dispatch mid-agent-run.

When an agent is running, the gateway's running-agent guard rejects most
slash commands with "⏳ Agent is running — /{cmd} can't run mid-turn"
(PR #12334). A small allowlist bypasses that and actually dispatches:

  * /yolo — toggles the session yolo flag; useful to pre-approve a
    pending approval prompt without waiting for the agent to finish.
  * /verbose — cycles the per-platform tool-progress display mode;
    affects the ongoing stream.
  * /model with no target — answers which model is running and what else is
    available. A /model that would SWITCH stays refused: the switch swaps
    model, client and api_mode on the cached agent in place.

Commands whose handlers say "takes effect on next message" stay on the
catch-all by design:

  * /fast — writes config.yaml only
  * /reasoning — writes config.yaml only

These tests lock in both behaviors so the allowlist doesn't silently
grow or shrink.
"""

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.event import MessageEvent
from gateway.session import SessionEntry, SessionSource, build_session_key


def _make_source() -> SessionSource:
    return SessionSource(
        platform=Platform.TELEGRAM,
        user_id="u1",
        chat_id="c1",
        user_name="tester",
        chat_type="dm",
    )


def _make_event(text: str) -> MessageEvent:
    return MessageEvent(text=text, source=_make_source(), message_id="m1")


def _make_runner():
    """Minimal GatewayRunner with an active running agent for this session."""
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(
        platforms={Platform.TELEGRAM: PlatformConfig(enabled=True, token="***")}
    )
    adapter = MagicMock()
    adapter.send = AsyncMock()
    runner.adapters = {Platform.TELEGRAM: adapter}
    runner._voice_mode = {}
    runner.hooks = SimpleNamespace(emit=AsyncMock(), loaded_hooks=False)

    session_entry = SessionEntry(
        session_key=build_session_key(_make_source()),
        session_id="sess-1",
        created_at=datetime.now(),
        updated_at=datetime.now(),
        platform=Platform.TELEGRAM,
        chat_type="dm",
    )
    runner.session_store = MagicMock()
    runner.session_store.get_or_create_session.return_value = session_entry
    runner.session_store.load_transcript.return_value = []
    runner.session_store.has_any_sessions.return_value = True
    runner.session_store.append_to_transcript = MagicMock()
    runner.session_store.rewrite_transcript = MagicMock()
    runner.session_store.update_session = MagicMock()
    runner._running_agents = {}
    runner._running_agents_ts = {}
    runner._pending_messages = {}
    runner._pending_approvals = {}
    runner._session_db = None
    runner._reasoning_config = None
    runner._provider_routing = {}
    runner._fallback_model = None
    runner._show_reasoning = False
    runner._service_tier = None
    runner._is_user_authorized = lambda _source: True
    runner._set_session_env = lambda _context: None
    runner._should_send_voice_reply = lambda *_args, **_kwargs: False
    runner._send_voice_reply = AsyncMock()
    runner._capture_gateway_honcho_if_configured = lambda *args, **kwargs: None
    runner._emit_gateway_run_progress = AsyncMock()

    # Simulate agent actively running for this session so the guard fires.
    # Note: the stale-eviction branch calls agent.get_activity_summary() and
    # compares seconds_since_activity against HERMES_AGENT_TIMEOUT. Return a
    # dict with recent activity so the eviction path doesn't clear our
    # fake running agent before the toggle guard runs.
    import time
    sk = build_session_key(_make_source())
    agent_mock = MagicMock()
    agent_mock.get_activity_summary.return_value = {
        "seconds_since_activity": 0.0,
        "last_activity_desc": "api_call",
        "api_call_count": 1,
        "max_iterations": 60,
    }
    runner._running_agents[sk] = agent_mock
    runner._running_agents_ts[sk] = time.time()
    return runner


@pytest.mark.asyncio
async def test_model_listing_dispatches_mid_run():
    """Bare /model mid-run answers which model is running, rather than "wait or /stop first"."""
    runner = _make_runner()
    seen = {}

    async def _listing(event, *, allow_picker=True):
        seen["allow_picker"] = allow_picker
        return "Current model: opus"

    runner._handle_model_command = _listing

    result = await runner._handle_message(_make_event("/model"))

    assert result == "Current model: opus"
    # The picker is suppressed mid-run: a tap on it commits the switch this path refuses.
    assert seen["allow_picker"] is False


@pytest.mark.parametrize("args", ["gpt-5.5", "--provider openai", "--reasoning high"])
@pytest.mark.asyncio
async def test_model_switch_stays_refused_mid_run(args):
    """_switch_cached_agent_model swaps model, client and api_mode on the cached agent IN PLACE, so
    committing a switch mid-turn moves the running request onto another model — and, when api_mode
    changes, another transport. Same hazard /codex-runtime is refused for."""
    runner = _make_runner()
    runner._handle_model_command = AsyncMock()

    result = await runner._handle_message(_make_event(f"/model {args}"))

    runner._handle_model_command.assert_not_awaited()
    assert "wait or /stop first" in str(result)


@pytest.mark.asyncio
async def test_listing_without_the_picker_sends_no_picker(monkeypatch):
    """The suppression is real, not just a flag: allow_picker=False must not reach the adapter."""
    from gateway.slash_commands_model import _ModelSwitchContext
    from hermes_cli import model_switch as model_switch_mod

    class _PickerAdapter:
        def __init__(self):
            self.picker_calls = 0

        async def send_model_picker(self, **kwargs):
            self.picker_calls += 1
            return SimpleNamespace(success=True)

    monkeypatch.setattr(model_switch_mod, "list_authenticated_providers", lambda **kw: [])
    runner = _make_runner()
    adapter = _PickerAdapter()
    runner._adapter_for_source = lambda _source: adapter
    ctx = _ModelSwitchContext(
        session_key="sk", source=_make_source(), config_path=None, persist_global=False,
        current_model="claude-opus-5", current_provider="anthropic")

    reply = await runner._model_listing_reply(_make_event("/model"), ctx, None, allow_picker=False)

    assert adapter.picker_calls == 0
    assert "claude-opus-5" in str(reply)


@pytest.mark.asyncio
async def test_yolo_dispatches_mid_run(monkeypatch):
    """/yolo mid-run must dispatch to its handler, not hit the catch-all."""
    runner = _make_runner()
    runner._handle_yolo_command = AsyncMock(return_value="⚡ YOLO mode **ON** for this session")

    result = await runner._handle_message(_make_event("/yolo"))

    runner._handle_yolo_command.assert_awaited_once()
    assert result == "⚡ YOLO mode **ON** for this session"
    assert "can't run mid-turn" not in (result or "")


@pytest.mark.asyncio
async def test_verbose_dispatches_mid_run(monkeypatch):
    """/verbose mid-run must dispatch to its handler, not hit the catch-all."""
    runner = _make_runner()
    runner._handle_verbose_command = AsyncMock(return_value="tool progress: new")

    result = await runner._handle_message(_make_event("/verbose"))

    runner._handle_verbose_command.assert_awaited_once()
    assert result == "tool progress: new"
    assert "can't run mid-turn" not in (result or "")


@pytest.mark.asyncio
async def test_fresh_ancient_turn_remains_controllable(monkeypatch):
    """Total turn age must not evict an agent with fresh activity.

    A long autonomous turn can legitimately run beyond the emergency wall TTL.
    Evicting it solely by age forgets the control handle while its session-ID
    lease remains held, so /steer and /stop fall into the lease waiter instead
    of reaching the live agent.
    """
    import time

    runner = _make_runner()
    sk = build_session_key(_make_source())
    agent = runner._running_agents[sk]
    agent.get_activity_summary.return_value = {
        "seconds_since_activity": 3.0,
        "last_activity_desc": "receiving stream response",
        "api_call_count": 1186,
        "max_iterations": 2000,
    }
    runner._running_agents_ts[sk] = time.time() - 31_471
    runner._handle_verbose_command = AsyncMock(return_value="tool progress: new")
    monkeypatch.setenv("HERMES_AGENT_TIMEOUT", "1800")

    result = await runner._handle_message(_make_event("/verbose"))

    runner._handle_verbose_command.assert_awaited_once()
    assert runner._running_agents[sk] is agent
    assert result == "tool progress: new"


