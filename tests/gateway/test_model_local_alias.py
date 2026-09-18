"""Gateway ``/model local`` and ``/model primary``.

The alias handler runs before the model-resolution pipeline: "local" is a
runtime, not a model name, and sending it through ``switch_model()`` would
look it up in the current aggregator's catalog.

The handler is exercised on a stub runner that provides only what it touches
(session overrides, the persisted store, agent cache, eviction), plus one
routing test on the real ``_handle_model_command`` entry point.
"""

from unittest.mock import MagicMock

import threading

import pytest

from gateway.config import Platform
from gateway.platforms.base import MessageEvent, MessageType
from gateway.run import GatewayRunner
from gateway.session import SessionSource


LOCAL_ENTRY = {
    "provider": "custom",
    "model": "/models/local-model",
    "base_url": "http://127.0.0.1:18765/v1",
    "api_mode": "chat_completions",
    "api_key": "local",
    "start_command": "/abs/serve.command",
}
REMOTE_ENTRY = {
    "provider": "openrouter",
    "model": "z-ai/glm-4.7",
    "base_url": "https://openrouter.ai/api/v1",
}
SESSION_KEY = "telegram:12345"


class _StubStore:
    def __init__(self):
        self.overrides = []

    async def set_model_override(self, session_key, override):
        self.overrides.append((session_key, override))


class _StubRunner:
    """Only the surface ``_handle_runtime_model_alias`` actually uses."""

    # This tree already has the cached-agent accessor; the fork's duplicate is dropped.
    _cached_agent_for = GatewayRunner._cached_agent_for
    _handle_runtime_model_alias = GatewayRunner._handle_runtime_model_alias

    def __init__(self, cached_agent=None):
        self._session_model_overrides = {}
        self.async_session_store = _StubStore()
        self._agent_cache = {SESSION_KEY: (cached_agent, 0)} if cached_agent else {}
        # A real lock, because _cached_agent_for deliberately fails closed without one: a live
        # gateway always holds it, and lock-free reads are reserved for one documented exception.
        self._agent_cache_lock = threading.Lock()
        self.evicted = []

    def _evict_cached_agent(self, session_key):
        self.evicted.append(session_key)


@pytest.fixture
def config_with_local(monkeypatch):
    import gateway.run as gateway_run

    monkeypatch.setattr(
        gateway_run,
        "_load_gateway_config",
        lambda: {"fallback_providers": [dict(REMOTE_ENTRY), dict(LOCAL_ENTRY)]},
    )


@pytest.fixture
def config_without_local(monkeypatch):
    import gateway.run as gateway_run

    monkeypatch.setattr(
        gateway_run, "_load_gateway_config", lambda: {"fallback_providers": [dict(REMOTE_ENTRY)]}
    )


def _patch_ensure(monkeypatch, result):
    import agent.local_endpoint as local_endpoint

    calls = []

    def _ensure(entry):
        calls.append(entry)
        return result

    monkeypatch.setattr(local_endpoint, "ensure_local_endpoint", _ensure)
    return calls


@pytest.mark.asyncio
async def test_local_alias_pins_the_session_to_the_endpoint(monkeypatch, config_with_local):
    calls = _patch_ensure(monkeypatch, {"started": True, "ready": True, "error": ""})
    runner = _StubRunner()

    reply = await runner._handle_runtime_model_alias("local", SESSION_KEY)

    assert calls and calls[0]["start_command"] == "/abs/serve.command"
    override = runner._session_model_overrides[SESSION_KEY]
    assert override["model"] == "/models/local-model"
    assert override["provider"] == "custom"
    assert override["base_url"] == "http://127.0.0.1:18765/v1"
    assert override["api_mode"] == "chat_completions"
    assert override["api_key"] == "local"
    # Persisted so the pin survives a gateway restart, and the cached agent
    # is dropped so the next turn builds against the local endpoint.
    assert runner.async_session_store.overrides == [(SESSION_KEY, override)]
    assert runner.evicted == [SESSION_KEY]
    assert "/models/local-model" in reply
    assert "Started the local server." in reply
    assert "/model primary" in reply


@pytest.mark.asyncio
async def test_local_alias_omits_started_when_already_serving(monkeypatch, config_with_local):
    _patch_ensure(monkeypatch, {"started": False, "ready": True, "error": ""})
    reply = await _StubRunner()._handle_runtime_model_alias("local", SESSION_KEY)
    assert "Started the local server." not in reply
    assert "Local endpoint active" in reply


@pytest.mark.asyncio
async def test_local_alias_reports_a_failed_start_and_changes_nothing(
    monkeypatch, config_with_local
):
    _patch_ensure(
        monkeypatch,
        {"started": True, "ready": False, "error": "did not answer within 90s"},
    )
    runner = _StubRunner()

    reply = await runner._handle_runtime_model_alias("local", SESSION_KEY)

    assert "did not answer within 90s" in reply
    assert runner._session_model_overrides == {}
    assert runner.async_session_store.overrides == []
    assert runner.evicted == []


@pytest.mark.asyncio
async def test_local_alias_without_a_configured_endpoint(monkeypatch, config_without_local):
    runner = _StubRunner()
    reply = await runner._handle_runtime_model_alias("local", SESSION_KEY)
    assert "No local endpoint is configured" in reply
    assert runner._session_model_overrides == {}


@pytest.mark.asyncio
async def test_primary_alias_clears_the_pin(monkeypatch, config_with_local):
    runner = _StubRunner()
    runner._session_model_overrides[SESSION_KEY] = {"model": "/models/local-model"}

    reply = await runner._handle_runtime_model_alias("primary", SESSION_KEY)

    assert SESSION_KEY not in runner._session_model_overrides
    assert runner.async_session_store.overrides == [(SESSION_KEY, None)]
    assert runner.evicted == [SESSION_KEY]
    assert "Switched back to the primary model" in reply


@pytest.mark.asyncio
async def test_primary_alias_releases_an_automatic_hold(monkeypatch, config_with_local):
    """The auto-failover path sets the hold on the agent, not a session
    override — /model primary has to clear that too."""
    agent = MagicMock()
    agent._hold_local_fallback = True
    runner = _StubRunner(cached_agent=agent)

    reply = await runner._handle_runtime_model_alias("primary", SESSION_KEY)

    assert agent._hold_local_fallback is False
    assert runner.evicted == [SESSION_KEY]
    assert "Switched back to the primary model" in reply


@pytest.mark.asyncio
async def test_primary_alias_is_a_noop_when_already_primary(monkeypatch, config_with_local):
    runner = _StubRunner()
    reply = await runner._handle_runtime_model_alias("primary", SESSION_KEY)
    assert "Already on the primary model" in reply


@pytest.mark.asyncio
async def test_model_command_routes_aliases_before_the_switch_pipeline(monkeypatch):
    """`/model local` must never reach switch_model()."""
    runner = object.__new__(GatewayRunner)
    runner.adapters = {}
    runner._voice_mode = {}
    runner._session_model_overrides = {}
    runner._running_agents = {}

    seen = {}

    async def _fake_alias(alias, session_key, *, source=None):
        seen["alias"] = alias
        seen["session_key"] = session_key
        seen["source"] = source
        return "✅ routed"

    runner._handle_runtime_model_alias = _fake_alias
    monkeypatch.setattr(
        "hermes_cli.model_switch.switch_model",
        lambda **kw: pytest.fail("switch_model must not run for a runtime alias"),
    )

    event = MessageEvent(
        text="/model local",
        message_type=MessageType.TEXT,
        source=SessionSource(platform=Platform.TELEGRAM, chat_id="12345", chat_type="dm"),
    )
    reply = await runner._handle_model_command(event)

    assert reply == "✅ routed"
    assert seen["alias"] == "local"
    assert seen["session_key"]
    # The handler needs the source to consume a pending auto-reset before pinning.
    assert seen["source"] is not None


class _ResetStore(_StubStore):
    """A store whose session entry still carries a pending auto-reset."""

    def __init__(self):
        super().__init__()
        self.entry = MagicMock(was_auto_reset=True)

    async def get_or_create_session(self, source):
        return self.entry


@pytest.mark.asyncio
async def test_local_alias_consumes_a_pending_auto_reset(monkeypatch, config_with_local):
    """Otherwise the next message's conversation boundary wipes the pin we just installed (#48031)."""
    _patch_ensure(monkeypatch, {"started": False, "ready": True, "error": ""})
    runner = _StubRunner()
    runner.async_session_store = _ResetStore()
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="12345")

    reply = await runner._handle_runtime_model_alias("local", SESSION_KEY, source=source)

    assert "Local endpoint active" in reply
    assert runner.async_session_store.entry.was_auto_reset is False


class _FailingStore(_StubStore):
    """A store whose override write always fails."""

    async def set_model_override(self, session_key, override):
        raise RuntimeError("database is locked")


@pytest.mark.asyncio
async def test_local_alias_reports_a_pin_it_could_not_persist(monkeypatch, config_with_local):
    """A dropped write means the pin dies at the next restart; the user must hear about it."""
    _patch_ensure(monkeypatch, {"started": False, "ready": True, "error": ""})
    runner = _StubRunner()
    runner.async_session_store = _FailingStore()

    reply = await runner._handle_runtime_model_alias("local", SESSION_KEY)

    assert "Local endpoint active" in reply
    assert "could not be saved" in reply
    # The in-memory pin still applies to this gateway's next turn.
    assert runner._session_model_overrides[SESSION_KEY]["model"] == "/models/local-model"


@pytest.mark.asyncio
async def test_primary_alias_reports_an_override_it_could_not_clear(monkeypatch, config_with_local):
    """A dropped clear is rehydrated on the next turn, undoing the command silently."""
    runner = _StubRunner()
    runner._session_model_overrides[SESSION_KEY] = {"model": "/models/local-model"}
    runner.async_session_store = _FailingStore()

    reply = await runner._handle_runtime_model_alias("primary", SESSION_KEY)

    assert "could not be saved" in reply
