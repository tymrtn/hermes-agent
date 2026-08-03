"""Gateway-boundary test for the dormant-turn sidecar wiring in ``gateway.run``.

Exercises ``GatewayRunner._maybe_append_dormant_turn_note`` against a real
``AsyncSessionStore``/``state.db`` (temp HERMES_HOME) and real ``MessageEvent``/
``SessionSource`` objects, using a lightweight ``self`` that supplies only the
one attribute the method reads (``async_session_store``). This proves the
admitted-turn boundary appends the note to the turn sidecar list and stays a
no-op when the feature is disabled — without spinning the full gateway. (AC10.)
"""

import asyncio
from datetime import datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

import gateway.run as gwrun
from gateway.config import GatewayConfig, Platform, SessionResetPolicy
from gateway.platforms.base import MessageEvent
from gateway.session import AsyncSessionStore, SessionSource, SessionStore


MADRID = ZoneInfo("Europe/Madrid")


@pytest.fixture
def fake_runner(tmp_path):
    store = SessionStore(
        sessions_dir=tmp_path,
        config=GatewayConfig(default_reset_policy=SessionResetPolicy(mode="none")),
    )
    return SimpleNamespace(async_session_store=AsyncSessionStore(store))


def _dm_event(when: datetime) -> MessageEvent:
    source = SessionSource(
        platform=Platform.TELEGRAM, chat_id="c", chat_type="dm", user_id="u1"
    )
    return MessageEvent(text="hi", source=source, timestamp=when)


def _call(runner, event):
    return asyncio.run(
        gwrun.GatewayRunner._maybe_append_dormant_turn_note(
            runner, event, event.source, []
        )
    )


def _enable(monkeypatch):
    monkeypatch.setenv("HERMES_TIMEZONE", "Europe/Madrid")
    import hermes_time

    hermes_time.reset_cache()
    monkeypatch.setattr(
        gwrun,
        "_load_gateway_config",
        lambda: {
            "timezone": "Europe/Madrid",
            "gateway": {
                "dormant_turn_context": {
                    "enabled": True,
                    "verified_user_ids": {"telegram": ["u1"]},
                }
            },
        },
    )


def test_boundary_disabled_by_default_is_noop(fake_runner, monkeypatch):
    monkeypatch.setattr(gwrun, "_load_gateway_config", lambda: {})
    notes: list = []
    event = _dm_event(datetime(2026, 4, 28, 13, 40, tzinfo=MADRID))
    asyncio.run(
        gwrun.GatewayRunner._maybe_append_dormant_turn_note(
            fake_runner, event, event.source, notes
        )
    )
    assert notes == []


def test_boundary_appends_note_after_long_gap(fake_runner, monkeypatch):
    _enable(monkeypatch)

    # First admitted turn (3 days ago): records the anchor, injects nothing.
    first = _dm_event(datetime(2026, 4, 25, 13, 40, tzinfo=MADRID))
    notes1: list = []
    asyncio.run(
        gwrun.GatewayRunner._maybe_append_dormant_turn_note(
            fake_runner, first, first.source, notes1
        )
    )
    assert notes1 == []

    # Second admitted turn (now): 3-day gap → strong reorientation note.
    second = _dm_event(datetime(2026, 4, 28, 13, 40, tzinfo=MADRID))
    notes2: list = []
    asyncio.run(
        gwrun.GatewayRunner._maybe_append_dormant_turn_note(
            fake_runner, second, second.source, notes2
        )
    )
    assert len(notes2) == 1
    assert notes2[0].startswith("[Time context]")
    assert "about 3 days" in notes2[0]


def test_boundary_group_event_is_noop(fake_runner, monkeypatch):
    _enable(monkeypatch)
    source = SessionSource(
        platform=Platform.TELEGRAM, chat_id="c", chat_type="group", user_id="u1"
    )
    event = MessageEvent(
        text="hi", source=source, timestamp=datetime(2026, 4, 28, 13, 40, tzinfo=MADRID)
    )
    notes: list = []
    asyncio.run(
        gwrun.GatewayRunner._maybe_append_dormant_turn_note(
            fake_runner, event, source, notes
        )
    )
    assert notes == []


def test_boundary_internal_event_is_noop(fake_runner, monkeypatch):
    _enable(monkeypatch)
    # Seed a prior anchor so a gap would otherwise classify as strong.
    prior = _dm_event(datetime(2026, 4, 25, 13, 40, tzinfo=MADRID))
    asyncio.run(
        gwrun.GatewayRunner._maybe_append_dormant_turn_note(
            fake_runner, prior, prior.source, []
        )
    )
    internal = _dm_event(datetime(2026, 4, 28, 13, 40, tzinfo=MADRID))
    internal.internal = True
    notes: list = []
    asyncio.run(
        gwrun.GatewayRunner._maybe_append_dormant_turn_note(
            fake_runner, internal, internal.source, notes
        )
    )
    assert notes == []
