"""Tests for the pure dormant-turn time/location context policy/formatter.

The module under test is profile-local, opt-in, verified-1:1-DM only. These
tests exercise the pure policy (config resolution, principal identity,
platform-scoped audience gating, gap classification) and the formatter
(soft/strong layers, manual location provenance gated on the clock-authority
timezone) with no gateway, DB, or clock dependency — an explicit ``tz`` and
explicit epochs are always passed so the suite is deterministic under
``TZ=UTC`` (scripts/run_tests.sh).
"""

import hashlib
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from gateway.config import Platform
from gateway.session import SessionSource
from gateway.dormant_turn_context import (
    NONE,
    SOFT,
    STRONG,
    audience_verdict,
    build_dormant_turn_note,
    canonical_principal_sender,
    classify_gap,
    principal_hash,
    resolve_config,
    sanitize_event_epoch,
)


MADRID = ZoneInfo("Europe/Madrid")


def _madrid_epoch(y, mo, d, h, mi, s=0):
    return datetime(y, mo, d, h, mi, s, tzinfo=MADRID).timestamp()


# Tue 2026-04-28 13:40:00 Madrid (CEST, UTC+2).
NOW = _madrid_epoch(2026, 4, 28, 13, 40, 0)


def _cfg(*, clock_tz="Europe/Madrid", **dtc_over):
    dtc = {"enabled": True, "scope": "verified_dm"}
    dtc.update(dtc_over)
    cfg_dict = {"gateway": {"dormant_turn_context": dtc}}
    if clock_tz is not None:
        cfg_dict["timezone"] = clock_tz
    return resolve_config(cfg_dict)


def _source(**over):
    kw = dict(platform=Platform.TELEGRAM, chat_id="c1", chat_type="dm", user_id="123")
    kw.update(over)
    return SessionSource(**kw)


# ---------------------------------------------------------------------------
# resolve_config — defaults, opt-in gate, scope validation, malformed → None
# ---------------------------------------------------------------------------


def test_resolve_config_disabled_by_default():
    assert resolve_config(None) is None
    assert resolve_config({}) is None
    assert resolve_config({"gateway": {}}) is None
    assert resolve_config({"gateway": {"dormant_turn_context": {}}}) is None
    assert (
        resolve_config({"gateway": {"dormant_turn_context": {"enabled": False}}})
        is None
    )


def test_resolve_config_defaults_when_enabled():
    cfg = resolve_config({"gateway": {"dormant_turn_context": {"enabled": True}}})
    assert cfg is not None
    assert cfg.enabled is True
    assert cfg.idle_after_seconds == 3600
    assert cfg.reorient_after_seconds == 86400
    assert cfg.scope == "verified_dm"
    assert cfg.verified_user_ids == frozenset()
    assert cfg.clock_timezone == ""
    assert cfg.city == ""
    assert cfg.location_timezone == ""
    assert cfg.location_updated_at == ""
    assert cfg.location_fresh_for_seconds == 86400


def test_resolve_config_unknown_scope_disables():
    assert (
        resolve_config(
            {"gateway": {"dormant_turn_context": {"enabled": True, "scope": "all_dms"}}}
        )
        is None
    )


def test_resolve_config_malformed_disables():
    # Non-numeric idle window → fail closed.
    assert (
        resolve_config(
            {
                "gateway": {
                    "dormant_turn_context": {
                        "enabled": True,
                        "idle_after_seconds": "soon",
                    }
                }
            }
        )
        is None
    )
    # dormant_turn_context not a mapping → fail closed.
    assert (
        resolve_config({"gateway": {"dormant_turn_context": ["enabled"]}}) is None
    )


def test_resolve_config_rejects_flat_verified_user_ids_list():
    # The legacy permissive global-list shorthand is a schema error now.
    assert (
        resolve_config(
            {
                "gateway": {
                    "dormant_turn_context": {
                        "enabled": True,
                        "verified_user_ids": ["123"],
                    }
                }
            }
        )
        is None
    )


def test_resolve_config_parses_location_and_platform_scoped_ids():
    cfg = resolve_config(
        {
            "timezone": "Europe/Madrid",
            "gateway": {
                "dormant_turn_context": {
                    "enabled": True,
                    "idle_after_seconds": 60,
                    "reorient_after_seconds": 600,
                    "verified_user_ids": {
                        "telegram": ["123456789"],
                        "signal": ["uuid-1", "uuid-2"],
                    },
                    "location": {
                        "city": "Madrid",
                        "timezone": "Europe/Madrid",
                        "updated_at": "2026-04-28T12:40:00+02:00",
                        "fresh_for_seconds": 3600,
                    },
                }
            },
        }
    )
    assert cfg is not None
    assert cfg.idle_after_seconds == 60
    assert cfg.reorient_after_seconds == 600
    assert cfg.verified_user_ids == frozenset(
        {("telegram", "123456789"), ("signal", "uuid-1"), ("signal", "uuid-2")}
    )
    assert cfg.clock_timezone == "Europe/Madrid"
    assert cfg.city == "Madrid"
    assert cfg.location_timezone == "Europe/Madrid"
    assert cfg.location_updated_at == "2026-04-28T12:40:00+02:00"
    assert cfg.location_fresh_for_seconds == 3600


def test_resolve_config_drops_unknown_or_malformed_platform_entries():
    cfg = _cfg(
        verified_user_ids={
            "nosuchplatform": ["x"],  # unknown platform → dropped
            "telegram": "123",  # value not a list → dropped
            "signal": ["ok"],  # valid
        }
    )
    assert cfg is not None
    assert cfg.verified_user_ids == frozenset({("signal", "ok")})


# ---------------------------------------------------------------------------
# Principal identity — canonical sender + hash
# ---------------------------------------------------------------------------


def test_canonical_sender_prefers_alt_then_user_id():
    assert canonical_principal_sender("telegram", "alt1", "u1") == "alt1"
    assert canonical_principal_sender("telegram", None, "u1") == "u1"
    assert canonical_principal_sender("telegram", "", "u1") == "u1"
    assert canonical_principal_sender("telegram", None, None) == ""


def test_canonical_sender_whatsapp_canonicalizes():
    assert (
        canonical_principal_sender("whatsapp", "60123456789@s.whatsapp.net", None)
        == "60123456789"
    )
    assert (
        canonical_principal_sender("whatsapp", "60123456789:47@s.whatsapp.net", None)
        == "60123456789"
    )


def test_principal_hash_matches_spec_formula():
    expected = hashlib.sha256(b"default\x00telegram\x00123").hexdigest()
    assert principal_hash("default", "telegram", "123") == expected


def test_principal_hash_normalizes_default_profile():
    h = principal_hash("default", "telegram", "123")
    assert principal_hash(None, "telegram", "123") == h
    assert principal_hash("", "telegram", "123") == h


def test_principal_hash_separates_platforms_and_profiles():
    base = principal_hash("default", "telegram", "123")
    assert principal_hash("default", "discord", "123") != base
    assert principal_hash("coder", "telegram", "123") != base
    assert principal_hash("default", "telegram", "123") == base


# ---------------------------------------------------------------------------
# Audience gating — platform-scoped verified 1:1 DM only, fail closed
# ---------------------------------------------------------------------------


def test_audience_native_dm_requires_verified_sender_on_this_platform():
    cfg = _cfg(verified_user_ids={"telegram": ["123"]})
    ok, sender = audience_verdict(_source(user_id="123"), is_internal=False, config=cfg)
    assert ok is True and sender == "123"
    ok, _ = audience_verdict(_source(user_id="456"), is_internal=False, config=cfg)
    assert ok is False


def test_audience_verified_id_is_platform_scoped():
    # An id allowed on Telegram must NOT authorize the same id on Signal.
    cfg = _cfg(verified_user_ids={"telegram": ["shared"]})
    tg = _source(platform=Platform.TELEGRAM, user_id="shared")
    sig = _source(platform=Platform.SIGNAL, user_id="shared")
    assert audience_verdict(tg, is_internal=False, config=cfg)[0] is True
    assert audience_verdict(sig, is_internal=False, config=cfg)[0] is False


def test_audience_relay_dm_qualifies_without_verified_list():
    cfg = _cfg(verified_user_ids={})
    src = _source(
        platform=Platform.DISCORD,
        user_id="999",
        delivered_via_upstream_relay=True,
    )
    ok, sender = audience_verdict(src, is_internal=False, config=cfg)
    assert ok is True and sender == "999"


def test_audience_native_dm_without_relay_and_unlisted_fails_closed():
    cfg = _cfg(verified_user_ids={})
    ok, _ = audience_verdict(_source(user_id="999"), is_internal=False, config=cfg)
    assert ok is False


@pytest.mark.parametrize("chat_type", ["group", "channel", "thread", "forum", "wat"])
def test_audience_non_dm_fails_closed(chat_type):
    cfg = _cfg(verified_user_ids={"telegram": ["123"]})
    src = _source(chat_type=chat_type, delivered_via_upstream_relay=True)
    ok, _ = audience_verdict(src, is_internal=False, config=cfg)
    assert ok is False


def test_audience_bot_sender_fails_closed():
    cfg = _cfg(verified_user_ids={"telegram": ["123"]})
    ok, _ = audience_verdict(
        _source(is_bot=True, delivered_via_upstream_relay=True),
        is_internal=False,
        config=cfg,
    )
    assert ok is False


def test_audience_missing_sender_fails_closed():
    cfg = _cfg(verified_user_ids={"telegram": ["123"]})
    src = _source(user_id=None, user_id_alt=None, delivered_via_upstream_relay=True)
    ok, sender = audience_verdict(src, is_internal=False, config=cfg)
    assert ok is False and sender == ""


def test_audience_internal_event_fails_closed():
    cfg = _cfg(verified_user_ids={"telegram": ["123"]})
    ok, _ = audience_verdict(_source(user_id="123"), is_internal=True, config=cfg)
    assert ok is False


def test_audience_platform_neutral_telegram_and_signal():
    # AC8: at least Telegram + one non-Telegram source, no adapter branching.
    cfg = _cfg(verified_user_ids={"telegram": ["tg-1"], "signal": ["sig-uuid"]})
    tg = _source(platform=Platform.TELEGRAM, user_id="tg-1")
    sig = _source(platform=Platform.SIGNAL, user_id_alt="sig-uuid", user_id="+15551234")
    assert audience_verdict(tg, is_internal=False, config=cfg)[0] is True
    assert audience_verdict(sig, is_internal=False, config=cfg)[0] is True


# ---------------------------------------------------------------------------
# Gap classification — boundaries at idle / reorient thresholds (AC1)
# ---------------------------------------------------------------------------


def test_classify_gap_boundaries():
    cfg = _cfg()
    assert classify_gap(3599, cfg) == NONE
    assert classify_gap(3600, cfg) == SOFT
    assert classify_gap(86399, cfg) == SOFT
    assert classify_gap(86400, cfg) == STRONG


# ---------------------------------------------------------------------------
# Event-timestamp sanitation — malformed / future skew (reject, don't clamp)
# ---------------------------------------------------------------------------


def test_sanitize_event_epoch_coerces_and_rejects_malformed():
    assert sanitize_event_epoch(None, NOW) is None
    assert sanitize_event_epoch("not-a-time", NOW) is None
    assert sanitize_event_epoch(NOW - 100, NOW) == NOW - 100
    dt = datetime(2026, 4, 28, 13, 38, 0, tzinfo=MADRID)
    assert sanitize_event_epoch(dt, NOW) == dt.timestamp()


def test_sanitize_event_epoch_within_tolerance_kept_as_sent():
    # A small within-tolerance future lead is harmless and kept.
    assert sanitize_event_epoch(NOW + 100, NOW) == NOW + 100


def test_sanitize_event_epoch_rejects_future_skew():
    # A timestamp far in the future (clock skew / spoof) is REJECTED (None), not
    # clamped, so it can never invent a gap or poison the anchor.
    assert sanitize_event_epoch(NOW + 10 * 86400, NOW) is None


# ---------------------------------------------------------------------------
# Note building — first turn / no gap suppression, soft & strong layers
# ---------------------------------------------------------------------------


def test_note_first_turn_no_prior_no_injection():
    assert build_dormant_turn_note(NOW, None, MADRID, _cfg()) is None


def test_note_out_of_order_or_subthreshold_no_injection():
    assert build_dormant_turn_note(NOW, NOW + 100, MADRID, _cfg()) is None
    assert build_dormant_turn_note(NOW, NOW - 1800, MADRID, _cfg()) is None


def test_note_soft_layer_shape():
    prior = NOW - 2 * 3600  # 2 hours
    note = build_dormant_turn_note(NOW, prior, MADRID, _cfg())
    assert note is not None
    assert note.startswith("[Time context]")
    assert "about 2 hours" in note
    assert "It is now" in note
    assert "CEST" in note
    # Principal-scoped wording (gap 6): the user, not "this conversation".
    assert "from this user" in note
    assert "conversation" not in note
    # Soft layer must not carry a full weekday-name reorientation date.
    assert "Tuesday" not in note


def test_note_strong_layer_shape():
    prior = NOW - 3 * 86400  # 3 days
    note = build_dormant_turn_note(NOW, prior, MADRID, _cfg())
    assert note is not None
    assert "about 3 days" in note
    assert "Tuesday" in note
    assert "2026" in note
    assert "CEST" in note
    assert "from this user" in note
    assert "conversation" not in note


# ---------------------------------------------------------------------------
# Location provenance (AC5) + clock-authority timezone gate (gap 5)
# ---------------------------------------------------------------------------


def _loc_note(prior_gap, *, clock_tz="Europe/Madrid", **loc):
    location = {
        "city": "Madrid",
        "timezone": "Europe/Madrid",
        "updated_at": "2026-04-28T12:40:00+02:00",  # 1h before NOW → fresh
        "fresh_for_seconds": 86400,
    }
    location.update(loc)
    cfg = _cfg(location=location, clock_tz=clock_tz)
    return build_dormant_turn_note(NOW, NOW - prior_gap, MADRID, cfg)


def test_location_fresh_renders_manual_current_provenance_soft():
    note = _loc_note(2 * 3600)  # soft layer
    assert note is not None
    assert "Madrid" in note
    assert "manually set" in note
    assert "current as of 2026-04-28" in note


def test_location_stale_omitted_in_soft_layer():
    note = _loc_note(2 * 3600, updated_at="2026-04-18T13:40:00+02:00")
    assert note is not None
    assert "Madrid" not in note


def test_location_stale_last_known_in_strong_layer():
    note = _loc_note(3 * 86400, updated_at="2026-04-18T13:40:00+02:00")
    assert note is not None
    assert "Madrid" in note
    assert "manually set" in note
    assert "as of 2026-04-18" in note
    assert "current as of" not in note


def test_location_fresh_current_provenance_strong():
    note = _loc_note(3 * 86400)
    assert note is not None
    assert "current as of 2026-04-28" in note


def test_location_omitted_when_timezone_invalid():
    note = _loc_note(2 * 3600, timezone="Not/AZone")
    assert note is not None
    assert "Madrid" not in note


def test_location_omitted_when_timezone_differs_from_clock_authority():
    # gap 5: a valid location timezone that is NOT the top-level clock authority
    # must withhold the city.
    note = _loc_note(2 * 3600, timezone="America/New_York")  # clock stays Madrid
    assert note is not None
    assert "Madrid" not in note


def test_location_omitted_when_updated_at_missing_or_invalid():
    assert "Madrid" not in _loc_note(2 * 3600, updated_at="")
    assert "Madrid" not in _loc_note(2 * 3600, updated_at="garbage")


def test_location_omitted_when_updated_at_in_future():
    # gap 4: a future updated_at is rejected rather than treated as fresh.
    note = _loc_note(2 * 3600, updated_at="2026-04-28T15:40:00+02:00")  # 2h ahead
    assert note is not None
    assert "Madrid" not in note


def test_location_omitted_when_city_missing():
    note = _loc_note(2 * 3600, city="")
    assert note is not None
    assert "Madrid" not in note
