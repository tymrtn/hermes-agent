"""Multiplex profile-isolation coverage for the dormant-turn gateway boundary.

Regression coverage for the P2 leak: ``_maybe_append_dormant_turn_note`` used to
read ``_load_gateway_config()`` and the process-cached ``get_timezone()`` WITHOUT
entering the message source's effective profile. A default-owned adapter routed
to ``source.profile`` therefore resolved the DEFAULT profile's dormant allow-list
and location, and whichever profile warmed the timezone cache first — a
cross-profile leak of a privacy-sensitive, opt-in feature.

These tests write two real profile ``config.yaml`` files (different verified ids,
timezones, and locations), drive the real ``GatewayRunner`` boundary method
against a real ``state.db`` anchor, and prove the note resolves the routed
profile's config AND timezone — and that the two profiles cannot cross-leak.
"""

import asyncio
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

import gateway.run as gwrun
from gateway.config import GatewayConfig, Platform, SessionResetPolicy
from gateway.platforms.base import MessageEvent
from gateway.session import AsyncSessionStore, SessionSource, SessionStore

# 3-day gap → strong reorientation layer. Both in the past vs. wall-clock now.
FIRST = datetime(2026, 4, 25, 4, 40, tzinfo=timezone.utc)
SECOND = datetime(2026, 4, 28, 4, 40, tzinfo=timezone.utc)  # 13:40 in Tokyo (JST)

_PROFILE_A_YAML = """\
timezone: America/New_York
gateway:
  dormant_turn_context:
    enabled: true
    verified_user_ids:
      telegram:
      - userA
    location:
      city: New York
      timezone: America/New_York
      updated_at: '2026-04-28'
"""

_PROFILE_B_YAML = """\
timezone: Asia/Tokyo
gateway:
  dormant_turn_context:
    enabled: true
    verified_user_ids:
      telegram:
      - userB
    location:
      city: Tokyo
      timezone: Asia/Tokyo
      updated_at: '2026-04-28'
"""


@pytest.fixture
def profiles(tmp_path):
    """Two real profile homes with distinct dormant config, plus a runner.

    ``profA`` doubles as the active/default profile (its verified id is ``userA``,
    its clock is New York); ``profB`` is the routed-to profile (``userB``, Tokyo).
    Priming ``hermes_time`` with New York simulates the stale process cache the
    fix must bypass.
    """
    home_a = tmp_path / "profA"
    home_b = tmp_path / "profB"
    home_a.mkdir()
    home_b.mkdir()
    (home_a / "config.yaml").write_text(_PROFILE_A_YAML, encoding="utf-8")
    (home_b / "config.yaml").write_text(_PROFILE_B_YAML, encoding="utf-8")
    dirs = {"profA": home_a, "profB": home_b, "default": home_a}

    store = SessionStore(
        sessions_dir=tmp_path / "state",
        config=GatewayConfig(default_reset_policy=SessionResetPolicy(mode="none")),
    )
    runner = MagicMock(spec=gwrun.GatewayRunner)
    runner.config = MagicMock(multiplex_profiles=True, profile_routes=[])
    runner.async_session_store = AsyncSessionStore(store)
    runner._resolve_profile_home_for_source = (
        gwrun.GatewayRunner._resolve_profile_home_for_source.__get__(runner)
    )
    runner._profile_name_for_source = (
        gwrun.GatewayRunner._profile_name_for_source.__get__(runner)
    )
    return runner, dirs


def _dm(user_id: str, profile: str, when: datetime) -> MessageEvent:
    source = SessionSource(
        platform=Platform.TELEGRAM,
        chat_id="c",
        chat_type="dm",
        user_id=user_id,
        profile=profile,
    )
    return MessageEvent(text="hi", source=source, timestamp=when)


def _second_turn_note(runner, dirs, *, user_id, profile, monkeypatch):
    """Run first (anchor) + second (note) turns for a principal; return the note.

    All profile-dir lookups resolve through the patched ``hermes_cli.profiles``
    helpers so the boundary enters the real ``_profile_runtime_scope`` for the
    routed profile.
    """
    # Point the PROCESS home (the unscoped default read) at profA — the "default
    # profile" the buggy unscoped load leaked. profA verifies ``userA`` in New
    # York, so a userA-routed-to-profB turn that reads profA would wrongly inject
    # a note; the fix must read profB and withhold it.
    monkeypatch.setattr(gwrun, "_hermes_home", dirs["default"])
    # Prime the process-cached timezone with the WRONG (default-profile) zone so a
    # passing test proves the boundary ignores the cache and reads profile config.
    monkeypatch.setenv("HERMES_TIMEZONE", "America/New_York")
    import hermes_time

    hermes_time.reset_cache()
    hermes_time.get_timezone()  # cache New York process-wide

    with patch(
        "hermes_cli.profiles.get_profile_dir", side_effect=lambda name: dirs[name]
    ), patch("hermes_cli.profiles.profile_exists", return_value=True), patch(
        "hermes_cli.profiles.get_active_profile_name", return_value="profA"
    ):
        first = _dm(user_id, profile, FIRST)
        asyncio.run(
            gwrun.GatewayRunner._maybe_append_dormant_turn_note(
                runner, first, first.source, []
            )
        )
        second = _dm(user_id, profile, SECOND)
        notes: list = []
        asyncio.run(
            gwrun.GatewayRunner._maybe_append_dormant_turn_note(
                runner, second, second.source, notes
            )
        )
    return notes


def test_source_profile_resolves_its_own_config_and_timezone(profiles, monkeypatch):
    """A default-owned adapter routed to ``profB`` reads profB's config + clock.

    ``userB`` is verified ONLY in profB's allow-list, so a note appearing at all
    proves profB's config was read (not the default profile's, which lists only
    ``userA``). The Tokyo ``JST`` stamp and Tokyo location prove profB's timezone
    and location were used, not the process-cached New York zone.
    """
    runner, dirs = profiles
    notes = _second_turn_note(
        runner, dirs, user_id="userB", profile="profB", monkeypatch=monkeypatch
    )
    assert len(notes) == 1
    note = notes[0]
    assert "about 3 days" in note
    assert "Tuesday" in note
    assert "JST" in note  # profB clock authority (Asia/Tokyo), not cached NY
    assert "Tokyo" in note  # profB manual location
    # Nothing from the default/other profile leaked in.
    assert "New York" not in note
    assert "EDT" not in note and "EST" not in note


def test_active_profile_uses_its_own_config_and_timezone(profiles, monkeypatch):
    """Symmetric proof for the default/active profile (profA): NY clock + location."""
    runner, dirs = profiles
    notes = _second_turn_note(
        runner, dirs, user_id="userA", profile="profA", monkeypatch=monkeypatch
    )
    assert len(notes) == 1
    note = notes[0]
    assert "EDT" in note  # America/New_York in late April
    assert "New York" in note
    assert "JST" not in note
    assert "Tokyo" not in note


@pytest.mark.parametrize(
    "user_id,profile",
    [
        ("userA", "profB"),  # profA's verified id must not authorize under profB
        ("userB", "profA"),  # profB's verified id must not authorize under profA
    ],
)
def test_profiles_do_not_cross_leak_allow_lists(profiles, monkeypatch, user_id, profile):
    """A sender verified in one profile is NOT authorized when routed to the other.

    Each profile's ``verified_user_ids`` stays isolated to that profile's config;
    resolving the wrong profile would (pre-fix) authorize the sender and inject a
    note. Post-fix the note is withheld — no cross-profile allow-list bleed.
    """
    runner, dirs = profiles
    notes = _second_turn_note(
        runner, dirs, user_id=user_id, profile=profile, monkeypatch=monkeypatch
    )
    assert notes == []
