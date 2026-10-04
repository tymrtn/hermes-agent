"""Ask the live gateway to really compress the conversation, and record what it really did.

The sibling `daily_compression` module compacts the continuity STORE: deterministic bookkeeping
over rows the collector already wrote, metadata-only by construction. It never touched the
conversation, so a receipt calling it "compression" overstated it.

This module closes that gap without adding a second summarizer. It is a CLIENT of the gateway's
own `compress-session` control verb (`gateway/scheduled_compression.py`), which drives the
EXISTING manual-`/compress` path inside the process that owns the session. Everything here is
therefore request + honest accounting:

- No transcript is read, rewritten, or digested here; no `state.db` is opened.
- No gateway answer means NOT APPLIED. There is no offline fallback, and `claim` is derived only
  from per-session `applied` rows carrying before/after evidence — never from the fact a request
  was sent, and never from a previously stored request.
- Highlights are the bounded sections of the handoff the compressor itself wrote, passed through
  the shared sanitizers (secretguard + PII redaction + caps) like every other emitted string.
- Nothing from here enters the continuity store. It belongs to the run report, whose job is
  operational evidence, not durable continuity.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .sanitize import sanitize_identifier, sanitize_text

CONVERSATION_COMPRESSION_SCHEMA_VERSION = 1

#: Client wait. Must exceed the verb's own wait (240s) or a real compression that is still running
#: reads back as an unreachable gateway.
DEFAULT_TIMEOUT_SECONDS = 300.0
DEFAULT_LIMIT = 1

HIGHLIGHT_CHARS = 160
#: Per-key caps for the handoff projection the gateway returns. ``latest_reply`` is the retained
#: final assistant text (two sentences), so it gets more room than a bullet.
HIGHLIGHT_LIMITS = {"open_threads": 3, "learnings": 3, "active_task": 1, "open_items": 4,
                    "corrections": 3, "latest_reply": 1}
HIGHLIGHT_CHAR_LIMITS = {"latest_reply": 280}
MAX_SESSION_ROWS = 6

# `claim` — the one field a receipt may quote. Each value is a statement about what HAPPENED.
CLAIM_APPLIED = "applied"          # at least one conversation was really compressed
CLAIM_DEFERRED = "deferred"        # the gateway answered and deliberately left every session alone
CLAIM_ALREADY = "already_applied"  # this cycle had already compressed them (idempotent rerun)
CLAIM_NOTHING_ELIGIBLE = "nothing_eligible"
CLAIM_UNAVAILABLE = "unavailable"  # no gateway answered; nothing was attempted
CLAIM_FAILED = "failed"

_STATUS_KEYS = ("applied", "already_applied", "deferred", "eligible", "failed")

__all__ = [
    "CONVERSATION_COMPRESSION_SCHEMA_VERSION", "DEFAULT_TIMEOUT_SECONDS", "DEFAULT_LIMIT",
    "CLAIM_APPLIED", "CLAIM_DEFERRED", "CLAIM_ALREADY", "CLAIM_NOTHING_ELIGIBLE",
    "CLAIM_UNAVAILABLE", "CLAIM_FAILED", "control_home_candidates",
    "classify_conversation_compression", "request_conversation_compression",
    "conversation_compression_status",
]


def control_home_candidates(profile: str, *, gateway_home: str | os.PathLike | None = None,
                            home: Path | None = None) -> list[Path]:
    """Homes whose control socket may own *profile*, most specific first.

    A profile is served either by its own single-profile gateway (socket under the profile home) or
    by a multiplexer running from the default home (one socket, `profile` selects the namespace).
    An explicit *gateway_home* short-circuits the guess.

    The base is the CONFIGURED Hermes root (`get_default_hermes_root`), never a hardcoded
    `~/.hermes`: an installation whose `HERMES_HOME` points elsewhere — including a Windows native
    home, which is not under `~` at all — would otherwise be probed at a path no gateway listens on
    and the cycle would report `unavailable` while its own gateway was answering.
    """
    if gateway_home:
        return [Path(gateway_home).expanduser()]
    if home is not None:
        base = Path(home)
    else:
        from hermes_constants import get_default_hermes_root  # noqa: PLC0415
        base = get_default_hermes_root()
    candidates = [base / "profiles" / profile, base] if profile not in ("", "default") else [base]
    seen: list[Path] = []
    for candidate in candidates:
        if candidate not in seen:
            seen.append(candidate)
    return seen


def _highlights(rows: Sequence[Mapping[str, Any]]) -> dict[str, list[str]]:
    """Merge the applied sessions' handoff bullets, sanitized and bounded."""
    merged: dict[str, list[str]] = {}
    for row in rows:
        raw = row.get("highlights")
        if not isinstance(raw, Mapping):
            continue
        for name, limit in HIGHLIGHT_LIMITS.items():
            values = raw.get(name)
            if not isinstance(values, list):
                continue
            bucket = merged.setdefault(name, [])
            for value in values:
                text = sanitize_text(value, HIGHLIGHT_CHAR_LIMITS.get(name, HIGHLIGHT_CHARS))
                if text and text not in bucket and len(bucket) < limit:
                    bucket.append(text)
    return {k: v for k, v in merged.items() if v}


def _int(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _session_row(row: Mapping[str, Any]) -> dict[str, Any]:
    """One session's evidence, identity-free: the gateway's digest handle, never the session key."""
    out: dict[str, Any] = {
        "session_ref": sanitize_identifier(row.get("session_ref"), 32),
        "platform": sanitize_identifier(row.get("platform"), 32),
        "chat_type": sanitize_identifier(row.get("chat_type"), 32),
        "status": sanitize_identifier(row.get("status"), 32),
    }
    if row.get("reason"):
        out["reason"] = sanitize_text(row.get("reason"), 120)
    for key in ("before_tokens", "after_tokens", "before_messages", "after_messages"):
        if key in row:
            out[key] = _int(row.get(key))
    if row.get("rotated") is not None:
        out["rotated"] = bool(row.get("rotated"))
    return out


def _has_evidence(row: Mapping[str, Any]) -> bool:
    """An `applied` row must carry real before/after figures. A status word alone is a claim."""
    return _int(row.get("before_tokens")) > 0 and _int(row.get("before_messages")) > 0 \
        and _int(row.get("after_messages")) > 0


def classify_conversation_compression(response: Mapping[str, Any] | None, *, profile: str,
                                      cycle_id: str, requested: bool = True,
                                      unavailable_reason: str = "") -> dict[str, Any]:
    """Turn one gateway answer into the report section. Pure: no I/O, no clock.

    `claim` is the only load-bearing field, and it is `applied` ONLY for rows the gateway reported
    as applied AND that carry before/after evidence. Absent, pending, or evidence-free answers
    degrade to `unavailable` / `deferred` — a request is never reported as an outcome.
    """
    section: dict[str, Any] = {
        "schema_version": CONVERSATION_COMPRESSION_SCHEMA_VERSION,
        "enabled": True,
        "requested": bool(requested),
        "profile": sanitize_identifier(profile, 40),
        "cycle_id": sanitize_identifier(cycle_id, 40),
        "evidence_source": "gateway_control_socket",
        "gateway_answered": response is not None,
    }
    if response is None:
        return {**section, "claim": CLAIM_UNAVAILABLE, "counts": {},
                "reason": sanitize_text(unavailable_reason or "no gateway answered "
                                        "'compress-session' for this profile", 160),
                "sessions": [], "highlights": {}}
    if response.get("ok") is False:
        return {**section, "claim": CLAIM_FAILED, "counts": {},
                "reason": sanitize_text(response.get("error") or "gateway refused the request", 160),
                "sessions": [], "highlights": {}}

    raw_rows = [r for r in (response.get("sessions") or []) if isinstance(r, Mapping)]
    applied = [r for r in raw_rows if r.get("status") == "applied" and _has_evidence(r)]
    unproven = [r for r in raw_rows if r.get("status") == "applied" and not _has_evidence(r)]
    counts = {key: sum(1 for r in raw_rows if r.get("status") == key) for key in _STATUS_KEYS}
    counts = {k: v for k, v in counts.items() if v}
    if unproven:
        counts["applied_without_evidence"] = len(unproven)

    if applied:
        claim = CLAIM_APPLIED
    elif any(r.get("status") == "failed" for r in raw_rows):
        claim = CLAIM_FAILED
    elif any(r.get("status") == "already_applied" for r in raw_rows):
        claim = CLAIM_ALREADY
    elif any(r.get("status") == "deferred" for r in raw_rows) or response.get("pending"):
        claim = CLAIM_DEFERRED
    else:
        claim = CLAIM_NOTHING_ELIGIBLE

    section.update({
        "claim": claim,
        "pending": bool(response.get("pending")),
        "eligible": _int(response.get("eligible")),
        "counts": counts,
        "tokens": {
            "before": sum(_int(r.get("before_tokens")) for r in applied),
            "after": sum(_int(r.get("after_tokens")) for r in applied),
        },
        "messages": {
            "before": sum(_int(r.get("before_messages")) for r in applied),
            "after": sum(_int(r.get("after_messages")) for r in applied),
        },
        "sessions": [_session_row(r) for r in raw_rows[:MAX_SESSION_ROWS]],
        "highlights": _highlights(applied),
    })
    if response.get("pending"):
        section["reason"] = ("gateway still compressing when the client stopped waiting; "
                            "outcome unknown to this run")
    return section


def request_conversation_compression(
    *, profile: str, cycle_id: str, limit: int = DEFAULT_LIMIT,
    gateway_home: str | os.PathLike | None = None, home: Path | None = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS, dry_run: bool = False,
    client: Callable[..., Mapping[str, Any] | None] | None = None,
) -> dict[str, Any]:
    """Request one real compression cycle and return the classified report section.

    Tries each candidate home's control socket until one answers; a home with no gateway is not an
    error, it is simply not the owner. Never raises: a failed request is recorded as
    `unavailable`/`failed` so the cycle reports the truth instead of dying on it.
    """
    if client is None:
        from gateway.control_socket import compress_gateway_session as client  # noqa: PLC0415
    tried: list[str] = []
    for candidate in control_home_candidates(profile, gateway_home=gateway_home, home=home):
        tried.append(str(candidate))
        try:
            response = client(candidate, profile=profile, cycle_id=cycle_id, limit=limit,
                              dry_run=dry_run, timeout=timeout)
        except Exception as exc:  # a broken socket is "not applied", never a failed cycle
            return classify_conversation_compression(
                None, profile=profile, cycle_id=cycle_id,
                unavailable_reason=f"control socket error at {candidate}: "
                                   f"{type(exc).__name__}: {exc}")
        if response is not None:
            return classify_conversation_compression(response, profile=profile, cycle_id=cycle_id)
    return classify_conversation_compression(
        None, profile=profile, cycle_id=cycle_id,
        unavailable_reason="no gateway answered 'compress-session' at " + ", ".join(tried))


def conversation_compression_status(section: Mapping[str, Any] | None) -> str:
    """One status-line token: the claim plus the counts that justify it. ``off`` when disabled."""
    if not isinstance(section, Mapping) or not section.get("enabled"):
        return "off"
    counts = section.get("counts") if isinstance(section.get("counts"), Mapping) else {}
    detail = ",".join(f"{k}={v}" for k, v in sorted(counts.items())) or "none"
    return f"{section.get('claim', CLAIM_FAILED)}[{detail}]"
