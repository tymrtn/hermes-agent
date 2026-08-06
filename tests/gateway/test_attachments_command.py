"""Tests for /attachments gateway slash command."""

import os
from unittest.mock import AsyncMock, MagicMock

import pytest
import yaml

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.base import MessageEvent, MessageType
from gateway.run import GatewayRunner
from gateway.session import SessionSource, build_session_key


class _FakeAdapter:
    def __init__(self):
        self._pending_messages = {}


class _FakeDeliveryRouter:
    def get_live_adapter(self, _source):
        return None


def _make_runner():
    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(
        platforms={Platform.TELEGRAM: PlatformConfig(enabled=True, token="***")}
    )
    runner.adapters = {Platform.TELEGRAM: _FakeAdapter()}
    runner._running_agents = {}
    runner._running_agents_ts = {}
    runner._session_run_generation = {}
    runner._pending_messages = {}
    runner._pending_approvals = {}
    runner._voice_mode = {}
    runner._background_tasks = set()
    runner._draining = False
    runner._restart_requested = False
    runner._restart_task_started = False
    runner._restart_detached = False
    runner._restart_via_service = False
    runner._restart_drain_timeout = 0.0
    runner._stop_task = None
    runner._exit_code = None
    runner._busy_input_mode = "interrupt"
    runner._update_runtime_status = MagicMock()
    runner._is_user_authorized = lambda _source: True
    runner._check_slash_access = lambda _source, _command: None
    runner._is_telegram_topic_root_lobby = lambda _source: False
    runner.hooks = MagicMock()
    runner.hooks.emit = AsyncMock()
    runner.hooks.emit_collect = AsyncMock(return_value=[])
    runner.session_store = MagicMock()
    runner.delivery_router = _FakeDeliveryRouter()
    return runner


def _make_event(text="/attachments status"):
    source = SessionSource(
        platform=Platform.TELEGRAM,
        chat_id="12345",
        chat_type="dm",
        user_id="u1",
    )
    return MessageEvent(text=text, message_type=MessageType.TEXT, source=source)


@pytest.fixture(autouse=True)
def clean_auto_attach_env(monkeypatch):
    monkeypatch.delenv("HERMES_AUTO_ATTACH_LOCAL_PATHS", raising=False)
    monkeypatch.setenv("HERMES_PROFILE", "scorandum")


@pytest.mark.asyncio
async def test_attachments_status_defaults_on_for_current_profile(tmp_path, monkeypatch):
    import gateway.run as run_mod

    monkeypatch.setattr(run_mod, "_hermes_home", tmp_path)
    (tmp_path / "config.yaml").write_text("gateway: {}\n")

    result = await _make_runner()._handle_attachments_command(_make_event("/attachments status"))

    assert "are on" in result
    assert "scorandum" in result


@pytest.mark.asyncio
async def test_attachments_off_saves_profile_config_and_runtime_env(tmp_path, monkeypatch):
    import gateway.run as run_mod

    monkeypatch.setattr(run_mod, "_hermes_home", tmp_path)
    (tmp_path / "config.yaml").write_text("gateway:\n  auto_attach_local_paths: true\n")

    result = await _make_runner()._handle_attachments_command(_make_event("/attachments off"))

    assert "disabled" in result
    assert os.environ["HERMES_AUTO_ATTACH_LOCAL_PATHS"] == "0"
    cfg = yaml.safe_load((tmp_path / "config.yaml").read_text())
    assert cfg["gateway"]["auto_attach_local_paths"] is False


@pytest.mark.asyncio
async def test_attachments_alias_dispatches_without_interrupt_during_active_session(tmp_path, monkeypatch):
    import gateway.run as run_mod

    monkeypatch.setattr(run_mod, "_hermes_home", tmp_path)
    (tmp_path / "config.yaml").write_text("gateway:\n  auto_attach_local_paths: false\n")
    runner = _make_runner()
    event = _make_event("/attach on")
    session_key = build_session_key(event.source)
    fake_agent = MagicMock()
    fake_agent.get_activity_summary.return_value = {"seconds_since_activity": 0}
    runner._running_agents[session_key] = fake_agent

    result = await runner._handle_message(event)

    assert "enabled" in result
    fake_agent.interrupt.assert_not_called()
    assert os.environ["HERMES_AUTO_ATTACH_LOCAL_PATHS"] == "1"
    cfg = yaml.safe_load((tmp_path / "config.yaml").read_text())
    assert cfg["gateway"]["auto_attach_local_paths"] is True


@pytest.mark.asyncio
async def test_attachments_multiplex_writes_source_profile_without_global_env_leak(
    tmp_path, monkeypatch
):
    import gateway.run as run_mod

    root_home = tmp_path / "root"
    profile_home = tmp_path / "profiles" / "scorandum"
    root_home.mkdir(parents=True)
    profile_home.mkdir(parents=True)
    (root_home / "config.yaml").write_text(
        "gateway:\n  auto_attach_local_paths: true\n"
    )
    (profile_home / "config.yaml").write_text(
        "gateway:\n  auto_attach_local_paths: true\n"
    )
    monkeypatch.setattr(run_mod, "_hermes_home", root_home)
    monkeypatch.setenv("HERMES_AUTO_ATTACH_LOCAL_PATHS", "1")

    runner = _make_runner()
    runner.config.multiplex_profiles = True
    runner._resolve_profile_home_for_source = lambda _source: profile_home
    event = _make_event("/attachments off")
    event.source.profile = "scorandum"

    result = await runner._handle_attachments_command(event)

    assert "scorandum" in result
    assert os.environ["HERMES_AUTO_ATTACH_LOCAL_PATHS"] == "1"
    assert yaml.safe_load((root_home / "config.yaml").read_text())["gateway"][
        "auto_attach_local_paths"
    ] is True
    assert yaml.safe_load((profile_home / "config.yaml").read_text())["gateway"][
        "auto_attach_local_paths"
    ] is False
