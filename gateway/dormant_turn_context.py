"""Dormant-turn time/location context — pure policy + formatter.

When a verified 1:1 DM resumes after a long quiet gap, the agent has no
in-band signal that wall-clock time has moved: the transcript replays the
prior turn as if it were seconds ago. This module decides whether to inject a
small orienting note ("it's been ~3 days; it is now Tuesday…") onto the
*current* user message via the existing gateway turn-sidecar channel, and
renders that note. It is deliberately free of any gateway, DB, or ambient-clock
dependency so the whole policy is unit-testable with explicit epochs and an
explicit ``tzinfo``.

Design boundaries (V1):
- Profile-local, opt-in (``gateway.dormant_turn_context.enabled``, default off).
- Verified 1:1 DM only. Native adapters require the sender in explicit profile
  config; relay-delivered events qualify on their upstream-authorized trust
  flag. Everything else fails closed.
- Canonical platform sender identity only (``user_id_alt`` then ``user_id``,
  with WhatsApp alias canonicalization). Chat IDs never affect identity.
- Manual location only, with provenance. Never inferred from IP/OS/device.
- The gap is measured between *event* timestamps of admitted real user turns,
  not processing time. Malformed / future-skewed timestamps fail closed.

The gateway calls :func:`compute_dormant_turn_note`, which combines the audience
gate, the atomic principal-activity read-and-replace (through the injected
``SessionStore`` async facade), and the formatter. The synchronous store helper
lives in ``gateway.session.SessionStore`` so the event loop is never blocked.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional, Tuple

from gateway.config import Platform
from gateway.message_timestamps import coerce_message_timestamp
from gateway.whatsapp_identity import canonical_whatsapp_identifier

logger = logging.getLogger(__name__)

# Gap-classification layer labels.
NONE = "none"
SOFT = "soft"
STRONG = "strong"

# The only scope V1 supports. Any other value disables the feature.
_SCOPE_VERIFIED_DM = "verified_dm"

# WhatsApp platform value — kept as a plain string so this module stays a leaf
# (callers pass ``platform.value``, never the Platform enum).
_PLATFORM_WHATSAPP = "whatsapp"

# Known platform tokens for validating ``verified_user_ids`` keys. A key that is
# not a real platform value fails closed (that entry is dropped) so an id can
# never be authorized on an unknown/misspelled platform.
_KNOWN_PLATFORMS = frozenset(p.value for p in Platform)

# Seconds an event timestamp may lead real processing time before it is treated
# as clock skew / spoof. Beyond this the event is REJECTED (no injection and no
# activity write) rather than clamped, so a spoofed future timestamp can never
# invent a gap or poison the stored anchor.
_FUTURE_SKEW_TOLERANCE_S = 300.0

_DEFAULT_IDLE_AFTER_S = 3600
_DEFAULT_REORIENT_AFTER_S = 86400
_DEFAULT_LOCATION_FRESH_S = 86400


@dataclass(frozen=True)
class DormantTurnConfig:
    """Validated, profile-local dormant-turn configuration.

    Only produced by :func:`resolve_config`, which returns ``None`` (feature
    off) for a disabled, unknown-scope, or malformed config — so a live config
    object always means "enabled and well-formed".
    """

    enabled: bool
    idle_after_seconds: int
    reorient_after_seconds: int
    scope: str
    # Platform-scoped allow-list as a frozenset of ``(platform, sender_id)``
    # pairs, so an id is only ever authorized on the platform it was configured
    # under. Built from the ``{platform: [ids]}`` config mapping.
    verified_user_ids: frozenset
    # Top-level ``timezone`` clock authority (resolved from user_config). The
    # location city is only disclosed when ``location_timezone`` matches this.
    clock_timezone: str
    city: str
    location_timezone: str
    location_updated_at: str
    location_fresh_for_seconds: int


def _coerce_positive_int(value: Any, default: int) -> Optional[int]:
    """Coerce a config value to a positive int, or ``None`` when malformed.

    ``None``/absent falls back to ``default``. A present-but-uninterpretable
    value (string, negative, non-numeric) returns ``None`` so the caller can
    fail the whole config closed rather than silently substituting a default.
    """
    if value is None:
        return default
    if isinstance(value, bool):  # bool is an int subclass — reject explicitly.
        return None
    try:
        ivalue = int(value)
    except (TypeError, ValueError):
        return None
    return ivalue if ivalue > 0 else None


def _coerce_verified_user_ids(value: Any) -> frozenset:
    """Normalize the ``{platform: [ids]}`` allow-list to ``(platform, id)`` pairs.

    Absent → empty. Each platform key must be a known platform value and its
    value a list/tuple/set of ids; an unknown platform key, or a value that is
    not a list of ids (e.g. a bare string), fails closed — that entry is dropped
    so an id can never be authorized on the wrong or a nonexistent platform. A
    wholly non-mapping ``verified_user_ids`` is rejected by the caller before
    this runs.
    """
    if not isinstance(value, dict):
        return frozenset()
    pairs = set()
    for platform_key, ids in value.items():
        if not isinstance(platform_key, str):
            continue
        platform = platform_key.strip().lower()
        if platform not in _KNOWN_PLATFORMS:
            continue
        if not isinstance(ids, (list, tuple, set, frozenset)):
            continue
        for raw_id in ids:
            text = str(raw_id).strip()
            if text:
                pairs.add((platform, text))
    return frozenset(pairs)


def resolve_config(user_config: Optional[dict]) -> Optional[DormantTurnConfig]:
    """Resolve ``gateway.dormant_turn_context`` into a config, or ``None`` (off).

    Returns ``None`` — feature disabled — when the block is absent, not opted
    in, uses an unknown/invalid scope, or is otherwise malformed. Never raises.
    """
    if not isinstance(user_config, dict):
        return None
    gw = user_config.get("gateway")
    if not isinstance(gw, dict):
        return None
    raw = gw.get("dormant_turn_context")
    if not isinstance(raw, dict):
        return None
    # Privacy-sensitive opt-in: require an EXACT boolean ``True``. ``bool(...)``
    # would fail OPEN on a truthy non-bool — YAML ``enabled: "false"`` parses to
    # the string ``"false"`` (truthy), and ``enabled: 1`` to an int — so anything
    # that is not literally ``True`` must fail closed and leave the feature off.
    if raw.get("enabled", False) is not True:
        return None

    scope = raw.get("scope", _SCOPE_VERIFIED_DM)
    if not isinstance(scope, str) or scope.strip() != _SCOPE_VERIFIED_DM:
        return None

    idle_after = _coerce_positive_int(raw.get("idle_after_seconds"), _DEFAULT_IDLE_AFTER_S)
    reorient_after = _coerce_positive_int(
        raw.get("reorient_after_seconds"), _DEFAULT_REORIENT_AFTER_S
    )
    if idle_after is None or reorient_after is None:
        return None
    # A reorient threshold at or below the idle threshold would make the soft
    # layer empty; treat that as a misconfiguration and fail closed.
    if reorient_after <= idle_after:
        return None

    # verified_user_ids must be a ``{platform: [ids]}`` mapping when present;
    # a legacy flat list (or any non-mapping) is a schema error — fail closed.
    raw_verified = raw.get("verified_user_ids")
    if raw_verified is not None and not isinstance(raw_verified, dict):
        return None

    location = raw.get("location")
    if location is None:
        location = {}
    if not isinstance(location, dict):
        return None
    fresh_for = _coerce_positive_int(
        location.get("fresh_for_seconds"), _DEFAULT_LOCATION_FRESH_S
    )
    if fresh_for is None:
        return None

    def _clean_str(value: Any) -> str:
        return str(value).strip() if isinstance(value, str) else ""

    try:
        return DormantTurnConfig(
            enabled=True,
            idle_after_seconds=idle_after,
            reorient_after_seconds=reorient_after,
            scope=_SCOPE_VERIFIED_DM,
            verified_user_ids=_coerce_verified_user_ids(raw_verified),
            # Top-level clock authority (sibling of ``gateway``), NOT a gateway
            # key. Empty when unset — the location city then stays hidden.
            clock_timezone=_clean_str(user_config.get("timezone")),
            city=_clean_str(location.get("city")),
            location_timezone=_clean_str(location.get("timezone")),
            location_updated_at=_clean_str(location.get("updated_at")),
            location_fresh_for_seconds=fresh_for,
        )
    except Exception:  # pragma: no cover — defensive; construction is total.
        return None


def resolve_clock_tz(config: DormantTurnConfig):
    """Return the ``ZoneInfo`` for the config's clock authority, or ``None``.

    The note's ``It is now …`` timestamp renders in the SAME top-level
    ``timezone`` clock authority that gates the location clause, so the render
    zone and the location gate can never disagree. ``None`` — config timezone
    unset or invalid — means server-local, matching ``build_dormant_turn_note``'s
    ``tz=None`` path. Because the zone is derived from the (profile-local)
    resolved config rather than a process-cached ambient clock, a multiplexed
    gateway renders each profile's own zone instead of whichever profile warmed
    the cache first.
    """
    return _valid_zone(config.clock_timezone)


# ---------------------------------------------------------------------------
# Principal identity
# ---------------------------------------------------------------------------


def canonical_principal_sender(
    platform: str, user_id_alt: Any, user_id: Any
) -> str:
    """Return the canonical platform sender id, or ``""`` when absent.

    Sender preference is ``user_id_alt`` (stable platform id — Signal UUID,
    Feishu union_id) then ``user_id``. On WhatsApp the value is canonicalized
    across phone/LID alias forms so the same human maps to one identity. Chat
    IDs are never consulted — identity is the person, not the conversation.
    """
    raw = user_id_alt if (user_id_alt not in (None, "")) else user_id
    text = str(raw).strip() if raw is not None else ""
    if not text:
        return ""
    if platform == _PLATFORM_WHATSAPP:
        try:
            return canonical_whatsapp_identifier(text) or text
        except Exception:
            return text
    return text


def principal_hash(profile: Optional[str], platform: str, sender: str) -> str:
    """Canonical principal hash: ``sha256(profile NUL platform NUL sender)``.

    ``profile`` is normalized so ``None``/``""``/``"default"`` collapse to the
    single default namespace (matching gateway session-key semantics). Distinct
    platforms and profiles hash apart; chat id is never an input.
    """
    prof = (profile or "").strip() or "default"
    payload = f"{prof}\x00{platform}\x00{sender}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


# ---------------------------------------------------------------------------
# Audience gating — verified 1:1 DM only, platform-neutral, fail closed
# ---------------------------------------------------------------------------


def _native_sender_verified(source: Any, sender: str, config: DormantTurnConfig) -> bool:
    """Whether a native-adapter sender is in THIS platform's allow-list.

    Only the current platform's configured ids are consulted (the allow-list is
    a set of ``(platform, id)`` pairs), so an id configured under one platform
    can never authorize a same-valued id on another. Matches the canonical
    sender or the raw ``user_id``; on WhatsApp the configured ids for this
    platform are also canonicalized so a phone-format entry matches a
    LID-delivered sender.
    """
    verified = config.verified_user_ids
    if not verified:
        return False
    platform = getattr(getattr(source, "platform", None), "value", "") or ""
    if not platform:
        return False
    candidates = {sender}
    raw_user_id = str(getattr(source, "user_id", "") or "").strip()
    if raw_user_id:
        candidates.add(raw_user_id)
    if any((platform, c) in verified for c in candidates):
        return True
    if platform == _PLATFORM_WHATSAPP:
        try:
            canon_verified = {
                canonical_whatsapp_identifier(vid)
                for (vplat, vid) in verified
                if vplat == _PLATFORM_WHATSAPP
            }
        except Exception:
            canon_verified = set()
        if sender in canon_verified:
            return True
    return False


def audience_verdict(
    source: Any, *, is_internal: bool, config: Optional[DormantTurnConfig]
) -> Tuple[bool, str]:
    """Return ``(eligible, canonical_sender)`` for the dormant-turn feature.

    Eligible only for a real, admitted, verified 1:1 DM turn. Fails closed for
    internal/synthetic events, non-DM audiences (group/channel/thread/forum/
    unknown), bot senders, missing senders, disabled/invalid config, and
    native-adapter senders absent from the explicit allow-list. Relay-delivered
    DMs qualify on their upstream-authorized trust flag alone.
    """
    platform = getattr(getattr(source, "platform", None), "value", "") or ""
    sender = canonical_principal_sender(
        platform,
        getattr(source, "user_id_alt", None),
        getattr(source, "user_id", None),
    )
    if is_internal:
        return (False, sender)
    if config is None or not config.enabled:
        return (False, sender)
    if config.scope != _SCOPE_VERIFIED_DM:
        return (False, sender)
    if getattr(source, "chat_type", None) != "dm":
        return (False, sender)
    if getattr(source, "is_bot", False):
        return (False, sender)
    if not sender:
        return (False, sender)
    if getattr(source, "delivered_via_upstream_relay", False):
        # The relay connector authenticates the socket per-instance and resolves
        # the owner binding upstream before delivery, so a relay DM is already
        # authorized as this instance's bound user.
        return (True, sender)
    return (_native_sender_verified(source, sender, config), sender)


# ---------------------------------------------------------------------------
# Gap classification + timestamp sanitation
# ---------------------------------------------------------------------------


def classify_gap(gap_seconds: float, config: DormantTurnConfig) -> str:
    """Classify an elapsed gap into ``NONE`` / ``SOFT`` / ``STRONG``."""
    if gap_seconds < config.idle_after_seconds:
        return NONE
    if gap_seconds < config.reorient_after_seconds:
        return SOFT
    return STRONG


def sanitize_event_epoch(
    event_ts: Any, now_epoch: float, *, tz=None
) -> Optional[float]:
    """Coerce an event timestamp to epoch seconds, or ``None`` to reject it.

    Returns ``None`` — reject, so the caller neither injects nor writes the
    activity anchor — when the value cannot be interpreted OR when it is more
    than ``_FUTURE_SKEW_TOLERANCE_S`` ahead of ``now_epoch`` (clock skew /
    spoof). A future timestamp is not clamped to now: inventing an anchor from a
    spoofed value would let it reset the gap and poison later computations. A
    small within-tolerance lead is kept as sent (harmless).
    """
    epoch = coerce_message_timestamp(event_ts, tz=tz)
    if epoch is None:
        return None
    if epoch > now_epoch + _FUTURE_SKEW_TOLERANCE_S:
        return None
    return epoch


# ---------------------------------------------------------------------------
# Formatter
# ---------------------------------------------------------------------------


def _format_elapsed(gap_seconds: float) -> str:
    """Render a rounded, human elapsed duration (e.g. ``about 2 hours``)."""
    if gap_seconds < 86400:
        hours = max(1, round(gap_seconds / 3600))
        unit = "hour" if hours == 1 else "hours"
        return f"about {hours} {unit}"
    if gap_seconds < 14 * 86400:
        days = max(1, round(gap_seconds / 86400))
        unit = "day" if days == 1 else "days"
        return f"about {days} {unit}"
    weeks = max(2, round(gap_seconds / (7 * 86400)))
    return f"about {weeks} weeks"


def _valid_zone(name: str):
    if not name:
        return None
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(name)
    except Exception:
        return None


def _location_clause(
    config: DormantTurnConfig, current_epoch: float, layer: str, tz
) -> str:
    """Render the manual-location clause, or ``""`` when it must be omitted.

    A city renders only when ALL hold: a city name; a valid IANA location
    timezone that MATCHES the top-level ``timezone`` clock authority (operators
    set both together); and a parseable, non-future ``updated_at``. Within the
    freshness window it renders with explicit manual/current provenance and an
    as-of timestamp; when stale it renders only in the strong (>=24h) layer with
    explicit last-known/as-of wording, and is omitted from the soft layer.
    Anything partial/invalid/mismatched omits the city entirely — location is
    never inferred, only what the user manually set (and clock-consistent) shows.
    """
    city = config.city
    if not city:
        return ""
    loc_tz = config.location_timezone
    if not loc_tz or _valid_zone(loc_tz) is None:
        return ""
    # Disclose the city only when its timezone is the same as the top-level
    # clock authority; a mismatch means the record is out of sync with the
    # clock the note renders in, so we withhold it.
    if loc_tz != config.clock_timezone:
        return ""
    updated_epoch = coerce_message_timestamp(config.location_updated_at)
    if updated_epoch is None:
        return ""
    age = current_epoch - updated_epoch
    if age < 0:
        # ``updated_at`` in the future — reject rather than treat as fresh.
        return ""

    if age <= config.location_fresh_for_seconds:
        as_of = _format_as_of(updated_epoch, tz, with_time=True)
        return f"The user's location is {city} (manually set, current as of {as_of})."
    # Stale: only the strong reorientation layer may surface a last-known city.
    if layer != STRONG:
        return ""
    as_of = _format_as_of(updated_epoch, tz, with_time=False)
    return f"The user's last recorded location was {city} (manually set, as of {as_of})."


def _format_as_of(updated_epoch: float, tz, *, with_time: bool) -> str:
    """Render the location ``as of`` timestamp in the clock timezone."""
    if tz is not None:
        dt = datetime.fromtimestamp(updated_epoch, tz=tz)
    else:
        dt = datetime.fromtimestamp(updated_epoch).astimezone()
    if with_time:
        return dt.strftime("%Y-%m-%d %H:%M %Z")
    return dt.strftime("%Y-%m-%d")


def build_dormant_turn_note(
    current_epoch: float,
    prior_epoch: Optional[float],
    tz,
    config: DormantTurnConfig,
) -> Optional[str]:
    """Build the dormant-turn context note, or ``None`` when none should inject.

    ``None`` when there is no prior real turn (first turn), when the gap is
    non-positive (out-of-order/duplicate), or when the gap is below the idle
    threshold. ``tz`` is the top-level clock authority (may be ``None`` →
    server-local); the current timestamp is rendered in it.
    """
    if prior_epoch is None:
        return None
    gap = current_epoch - prior_epoch
    if gap <= 0:
        return None
    layer = classify_gap(gap, config)
    if layer == NONE:
        return None
    return _render_note(current_epoch, gap, tz, config, layer)


def _render_note(
    current_epoch: float, gap: float, tz, config: DormantTurnConfig, layer: str
) -> str:
    if tz is not None:
        stamp_dt = datetime.fromtimestamp(current_epoch, tz=tz)
    else:
        stamp_dt = datetime.fromtimestamp(current_epoch).astimezone()

    elapsed = _format_elapsed(gap)
    if layer == STRONG:
        stamp = stamp_dt.strftime("%A, %d %B %Y at %H:%M %Z")
    else:
        stamp = stamp_dt.strftime("%a %Y-%m-%d %H:%M %Z")

    # The anchor is per-principal (this user, across chats), not per-conversation
    # — word it that way so the model never assumes a single-thread scope.
    sentence = (
        f"[Time context] It's been {elapsed} since the previous admitted message "
        f"from this user. It is now {stamp}."
    )
    location = _location_clause(config, current_epoch, layer, tz)
    if location:
        sentence = f"{sentence} {location}"
    return sentence


# ---------------------------------------------------------------------------
# Gateway orchestrator — audience + atomic activity read-and-replace + render
# ---------------------------------------------------------------------------


async def compute_dormant_turn_note(
    async_session_store: Any,
    *,
    source: Any,
    is_internal: bool,
    profile: Optional[str],
    event_ts: Any,
    now_epoch: float,
    tz,
    config: Optional[DormantTurnConfig],
) -> Optional[str]:
    """End-to-end dormant-turn note computation for the gateway admission path.

    Returns the note string to append to the turn sidecar, or ``None``. The
    principal-activity anchor is read-and-replaced atomically through the
    injected ``SessionStore`` async facade (off the event loop) so concurrent
    turns can't race, and an out-of-order older event can never regress the
    stored anchor. Ineligible/malformed turns neither inject nor touch the
    anchor.
    """
    if config is None or not config.enabled:
        return None
    eligible, sender = audience_verdict(source, is_internal=is_internal, config=config)
    if not eligible or not sender:
        return None

    current_epoch = sanitize_event_epoch(event_ts, now_epoch, tz=tz)
    if current_epoch is None:
        return None

    platform = getattr(getattr(source, "platform", None), "value", "") or ""
    key = principal_hash(profile, platform, sender)

    try:
        prior_epoch = await async_session_store.record_principal_activity(
            key, current_epoch
        )
    except Exception:
        logger.debug("dormant-turn activity read-and-replace failed", exc_info=True)
        return None

    return build_dormant_turn_note(current_epoch, prior_epoch, tz, config)
