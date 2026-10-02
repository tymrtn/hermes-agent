"""Scheduled real compression of a live gateway conversation (``compress-session`` control verb).

A nightly cycle (Dream Cycle v3) must be able to hand the next day a COMPRESSED conversation —
the real handoff summary the compressor already writes (``## Key Decisions``, ``## Constraints &
Preferences``, ``## Blocked``, ``## Errors & Fixes``, the live user ask), not a reflection block
appended beside an untouched transcript and not a reset.

So this module adds no summarizer and no second history mutation path. It drives the EXISTING
manual-``/compress`` path (``GatewaySessionCommandsMixin._run_manual_compression`` →
``agent.conversation_compression_manual.compress_now``) from inside the gateway process, reached
over the gateway's own control socket. Consequences that are the whole point:

- No offline ``state.db`` rewrite of a live session: the owning process does the rotation and
  evicts its cached agent, so the in-memory copy can never diverge from the persisted transcript.
- A busy conversation DEFERS. A running turn or an in-flight compression is never interrupted and
  never forced; the caller is told it deferred and why. The reverse direction is just as important:
  the operation RESERVES the conversation (pending-sentinel slot + per-session_id turn lease) before
  it reads the history, so a message arriving mid-compression queues behind it instead of running a
  turn against a transcript about to be rotated away.
- Idempotent per cycle. The outcome is stamped into the routing entry's own metadata
  (``set_session_metadata``, which deliberately does not advance the activity clock), so a retry
  in the same cycle reports ``already_applied`` instead of compressing twice.
- Honest evidence. ``applied`` exists only where the rewrite COMMITTED — the rotation persisted and
  the routing entry was repointed, or the in-place archive ran. ``compress_now`` answering
  ``compressed`` is not enough: a refused (would-grow) candidate and a caught persistence failure
  both leave the transcript untouched under that status, and such an attempt must stay retryable.
  The before/after token and message counts come from that result. A request that was merely sent
  reports ``requested``/``deferred``, never ``applied``.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

#: Routing-entry metadata key holding the last scheduled-compression outcome for that conversation.
CYCLE_METADATA_KEY = "scheduled_compression"

#: Only recently-used conversations are candidates (7 days). An archived chat gains nothing from a
#: nightly rewrite and would spend an aux-model call per night.
DEFAULT_ACTIVE_MINUTES = 7 * 24 * 60
#: A real compression is one aux-model summarization call over a long transcript; the socket caller
#: waits this long before being told ``pending``.
DEFAULT_VERB_TIMEOUT = 240.0
#: One conversation per cycle by default: the ongoing thread. Raising it is an explicit caller choice.
DEFAULT_LIMIT = 1

# Bounded, derived-summary highlights for the morning receipt. Sections are read from the handoff
# the compressor itself produced — never from the raw transcript.
# Order matters: the first heading with content wins. ``Historical Task Snapshot`` is LAST because
# the compressor grounds it deterministically from the compacted turns, so it restates an old user
# turn rather than naming what is still open.
_OPEN_THREAD_HEADINGS = ("Blocked", "Active Task", "Active State", "Historical Task Snapshot")
_LEARNING_HEADINGS = ("Key Decisions", "Errors & Fixes", "Constraints & Preferences")
MAX_HIGHLIGHTS = 3
MAX_HIGHLIGHT_CHARS = 160

_STATUS_APPLIED = "applied"
_STATUS_ALREADY = "already_applied"
_HEADING_RE = re.compile(r"^##\s+(.+?)\s*$", re.MULTILINE)


@dataclass
class ManualCompressionOutcome:
    """What one manual compression did, plus the surface-facing reply text.

    ``status`` mirrors ``CompressResult.status`` (``compressed`` / ``lock_skipped`` /
    ``nothing_to_do``) with the gateway-only values ``no_provider`` and ``codex_app_server``.
    Token figures are ``estimate_request_tokens`` (transcript + system prompt + tool schemas), the
    same figure the user sees in the ``/compress`` reply.
    """

    reply: str
    status: str
    before_tokens: int = 0
    after_tokens: int = 0
    before_messages: int = 0
    after_messages: int = 0
    session_id: str = ""
    new_session_id: str = ""
    highlights: Dict[str, List[str]] = field(default_factory=dict)
    #: The rewrite really COMMITTED (rotation persisted + repointed, or in-place archive). False
    #: whenever ``status`` is ``compressed`` but the commit was a no-op — a refused would-grow
    #: candidate, or a persistence failure the compressor caught and rolled back (#44794).
    committed: bool = False

    @property
    def rotated(self) -> bool:
        return bool(self.new_session_id) and self.new_session_id != self.session_id

    @property
    def compressed(self) -> bool:
        return self.status == "compressed"


def session_ref(session_key: str) -> str:
    """Stable, non-identifying handle for a session key.

    The key embeds chat and user ids; a nightly report and a chat receipt do not need them to
    correlate runs, so consumers outside the gateway get this digest instead.
    """
    return hashlib.sha256((session_key or "").encode("utf-8")).hexdigest()[:12]


# ---------------------------------------------------------------------------
# Handoff highlights (bounded projection of the summary the compressor wrote)
# ---------------------------------------------------------------------------

def _summary_text(messages: Any) -> str:
    """Text of the LAST compaction-handoff message in *messages*, or ""."""
    from agent.context_compressor import is_compaction_summary_message

    if not isinstance(messages, list):
        return ""
    for message in reversed(messages):
        if not is_compaction_summary_message(message):
            continue
        content = message.get("content") if isinstance(message, dict) else message
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return "\n".join(
                part.get("text", "") for part in content
                if isinstance(part, dict) and isinstance(part.get("text"), str))
    return ""


def _sections(text: str) -> Dict[str, str]:
    """``## Heading`` -> body, for the handoff's own section headings."""
    out: Dict[str, str] = {}
    matches = list(_HEADING_RE.finditer(text or ""))
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        out[match.group(1).strip()] = text[match.end():end].strip()
    return out


def _bullets(body: str) -> List[str]:
    """Up to ``MAX_HIGHLIGHTS`` short lines from one section body; placeholders dropped."""
    lines: List[str] = []
    for raw in (body or "").splitlines():
        line = raw.strip().lstrip("-*•").strip()
        line = re.sub(r"^\d+[.)]\s*", "", line)
        if not line or line.lower() in {"none", "n/a", "none.", "(none)"} or line.startswith("["):
            continue
        lines.append(line[:MAX_HIGHLIGHT_CHARS])
        if len(lines) >= MAX_HIGHLIGHTS:
            break
    return lines


def handoff_highlights(messages: Any) -> Dict[str, List[str]]:
    """``{"open_threads": [...], "learnings": [...]}`` from the handoff summary, or ``{}``.

    Derived from the compaction summary only — the first heading in each group that actually has
    content wins, so a summary whose ``## Blocked`` is empty falls through to ``## Active State``
    rather than reporting an empty bullet.
    """
    sections = _sections(_summary_text(messages))
    if not sections:
        return {}
    out: Dict[str, List[str]] = {}
    for name, headings in (("open_threads", _OPEN_THREAD_HEADINGS), ("learnings", _LEARNING_HEADINGS)):
        for heading in headings:
            bullets = _bullets(sections.get(heading, ""))
            if bullets:
                out[name] = bullets
                break
    return out


# ---------------------------------------------------------------------------
# Eligibility
# ---------------------------------------------------------------------------

def key_namespace_for_profile(runner: Any, profile: str) -> Optional[str]:
    """The session-key namespace this gateway uses for *profile*, or None when it serves another.

    Mirrors ``SessionStore._resolve_profile_for_key``, which is the ONLY writer of that slot:
    multiplexed gateways namespace per profile (``agent:<name>``), and a single-profile gateway
    writes the legacy ``agent:main`` for its one profile. So on a single-profile gateway the
    namespace cannot discriminate — the request must name THIS gateway's own profile, resolved from
    its launch home, or it would compress a different profile's conversation.
    """
    from gateway.session import _session_key_namespace

    if getattr(getattr(runner, "config", None), "multiplex_profiles", False):
        return _session_key_namespace(profile)
    from gateway.status import _get_process_hermes_home, _profile_label_for_home
    own = _profile_label_for_home(_get_process_hermes_home()) or "default"
    return _session_key_namespace(None) if own == profile else None


def eligible_entries(entries: Any, *, namespace: str, session_keys: Optional[List[str]] = None,
                     limit: int = DEFAULT_LIMIT) -> List[Any]:
    """Routing entries in *namespace* that a scheduled compression may touch.

    Membership is read off the key's own namespace slot (``agent:<ns>:``), the same discriminator
    ``build_session_key`` writes — never ``os.environ``, which under multiplex holds the LAUNCH
    profile. ``origin`` is required: the manual-compression path routes and re-binds the topic
    through it, and an entry without one cannot be compressed in place.
    """
    prefix = namespace + ":"
    wanted = set(session_keys or ())
    picked = [
        entry for entry in (entries or [])
        if str(getattr(entry, "session_key", "")).startswith(prefix)
        and getattr(entry, "origin", None) is not None
        and (not wanted or getattr(entry, "session_key", "") in wanted)
    ]
    return picked if limit is None or limit <= 0 else picked[:limit]


def _already_done(marker: Any, cycle_id: str) -> bool:
    """True when this cycle already settled this conversation (idempotence)."""
    return (isinstance(marker, dict) and marker.get("cycle_id") == cycle_id
            and marker.get("status") == _STATUS_APPLIED)


# ---------------------------------------------------------------------------
# Reservation: this operation is a turn as far as the rest of the gateway is concerned
# ---------------------------------------------------------------------------

#: ``_acquire_session_lease`` sentinel: the lease is held by a turn we must not race.
_LEASE_CONTENDED = object()


def _claim_turn_slot(runner: Any, session_key: str) -> bool:
    """Reserve this conversation's running-turn slot, or False when a turn already holds it.

    The check and the claim must sit in ONE synchronous block: between them is exactly the window
    the review reproduced — an inbound message passing the admission guard and starting a turn while
    this operation reads the history it is about to rotate away. The claim is the same pending
    sentinel the boot auto-resume path installs (``run_startup.py::
    _schedule_resume_pending_sessions``), read by the same admission guard
    (``run_inbound.py::_hm_handle_running_session_message`` via ``_is_session_running``), so inbound
    QUEUES behind the compression instead of interrupting it: an interrupt is skipped for a
    sentinel-held slot, and ``/stop`` stays the escape hatch.
    """
    from gateway.run import _AGENT_PENDING_SENTINEL

    if runner._is_session_running(session_key):
        return False
    state = runner._session_state(session_key)
    state.turn.agent = _AGENT_PENDING_SENTINEL
    state.turn.started_ts = time.time()
    runner._persist_active_agents()
    return True


async def _acquire_session_lease(runner: Any, session_key: str, session_id: str):
    """Hold the per-session_id turn lease for the [read history → compress → commit] region.

    The slot claim above is keyed by ROUTING KEY; the transcript is owned by SESSION_ID, and
    ``switch_session`` makes that mapping many-to-one, so a second key resolving this same id would
    still start a turn. This is the lease every real turn takes for the same region
    (``run_turn.py::_hmwa_acquire_turn_lease``). Returns the token, None when unavailable, or
    ``_LEASE_CONTENDED`` when a turn holds it — fail closed, defer, retry next cycle.
    """
    from gateway.turn_lease import DEFAULT_LEASE_WAIT, TurnLeaseTimeoutError

    registry = getattr(runner, "_turn_leases", None)
    if registry is None or not session_id:
        return None
    from gateway.run import _float_env
    state = runner._peek_session_state(session_key)
    generation = int(getattr(getattr(state, "persistent", None), "run_generation", 0) or 0)
    try:
        return await registry.acquire(
            session_id, owner_key=session_key, generation=generation,
            timeout=_float_env("HERMES_TURN_LEASE_TIMEOUT", DEFAULT_LEASE_WAIT))
    except TurnLeaseTimeoutError:
        return _LEASE_CONTENDED


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------

async def _deferral_reason(runner: Any, session_key: str) -> Optional[str]:
    """Why this conversation must be left alone right now, or None.

    The running-turn case is answered by ``_claim_turn_slot`` (atomically, as part of claiming);
    this covers the compression lock, which may be held from outside this process.
    """
    check = getattr(runner, "_session_has_compression_in_flight", None)
    if callable(check) and await check(session_key):
        return "compression_in_flight"
    return None


async def _compress_one_entry(runner: Any, entry: Any, *, cycle_id: str, dry_run: bool,
                              include_highlights: bool) -> Dict[str, Any]:
    session_key = entry.session_key
    store = runner.async_session_store
    row: Dict[str, Any] = {
        "session_key": session_key, "session_ref": session_ref(session_key),
        "platform": getattr(getattr(entry, "platform", None), "value", None) or "",
        "chat_type": getattr(entry, "chat_type", "") or "",
        "session_id": entry.session_id,
    }

    marker = await store.get_session_metadata(session_key, CYCLE_METADATA_KEY)
    if _already_done(marker, cycle_id):
        return {**row, "status": _STATUS_ALREADY,
                "previous": {k: marker.get(k) for k in ("status", "at", "after_tokens")}}

    # Fail closed on a key the persisted origin no longer reproduces: the compression lock, the
    # agent-cache eviction and the topic re-bind all key off the SOURCE, so a mismatch would
    # compress this transcript while unlocking and evicting a different conversation.
    try:
        derived = runner._session_key_for_source(entry.origin)
    except Exception as exc:
        return {**row, "status": "failed", "reason": f"origin_unusable: {type(exc).__name__}: {exc}"}
    if derived != session_key:
        return {**row, "status": "failed", "reason": "origin_key_mismatch"}

    # Reserve the conversation BEFORE reading its history: everything from here to the commit must
    # be serialized against inbound turns, or an arriving message runs a turn whose parent session
    # this compression is about to close (rotation) or rewrite (in place).
    if not _claim_turn_slot(runner, session_key):
        return {**row, "status": "deferred", "reason": "turn_running"}
    try:
        deferred = await _deferral_reason(runner, session_key)
        if deferred:
            return {**row, "status": "deferred", "reason": deferred}
        lease = await _acquire_session_lease(runner, session_key, entry.session_id)
        if lease is _LEASE_CONTENDED:
            return {**row, "status": "deferred", "reason": "turn_lease_contended"}
        try:
            return await _compress_reserved_entry(
                runner, entry, row, cycle_id=cycle_id, dry_run=dry_run,
                include_highlights=include_highlights, lease=lease)
        finally:
            if lease is not None:
                runner._turn_leases.release(lease)
    finally:
        runner._release_running_agent_state(session_key)


async def _compress_reserved_entry(runner: Any, entry: Any, row: Dict[str, Any], *, cycle_id: str,
                                   dry_run: bool, include_highlights: bool, lease: Any
                                   ) -> Dict[str, Any]:
    """Read, compress and commit one RESERVED conversation (slot + turn lease already held)."""
    from agent.conversation_compression_manual import MIN_MESSAGES, parse_compress_args
    from gateway.session_transcript import TranscriptReadError

    session_key = entry.session_key
    store = runner.async_session_store
    try:
        history = await store.load_transcript(entry.session_id)
    except TranscriptReadError as exc:
        return {**row, "status": "failed", "reason": f"transcript_unreadable: {exc}"}
    if not history or len(history) < MIN_MESSAGES:
        return {**row, "status": "deferred", "reason": "too_short",
                "before_messages": len(history or [])}
    if dry_run:
        return {**row, "status": "eligible", "before_messages": len(history)}

    with runner._profile_scope_for_source(entry.origin):
        outcome = await runner._run_manual_compression(
            entry.origin, entry, history, parse_compress_args(""))
    # ``compress_now`` answering ``compressed`` is NOT proof of a rewrite: the compressor refuses a
    # candidate that would GROW the transcript, and a caught persistence failure restores the
    # original — both leave the id unrotated with nothing committed (#44794). Only an explicit
    # commit (rotation persisted and repointed, or an in-place archive) may be reported as applied,
    # so a no-op stays retryable instead of stamping the cycle done.
    if outcome.committed:
        status = _STATUS_APPLIED
    elif outcome.status in {"lock_skipped", "nothing_to_do"}:
        status = "deferred"
    else:
        status = "failed"
    result = {
        **row, "status": status,
        "before_tokens": outcome.before_tokens, "after_tokens": outcome.after_tokens,
        "before_messages": outcome.before_messages, "after_messages": outcome.after_messages,
        "new_session_id": outcome.new_session_id, "rotated": outcome.rotated,
    }
    if status != _STATUS_APPLIED:
        result["reason"] = ("rewrite_not_committed" if outcome.compressed else outcome.status)
    if include_highlights and outcome.highlights:
        result["highlights"] = outcome.highlights
    # The rotated id is the new serialization target for whatever runs next; follow it with the held
    # lease before the caller releases, exactly as a real turn does (``_rebind_turn_lease``).
    if lease is not None and outcome.rotated:
        runner._turn_leases.rebind(lease, outcome.new_session_id)
    # Stamp AFTER the rewrite persisted, so a crash mid-compression leaves the cycle retryable.
    await store.set_session_metadata(session_key, CYCLE_METADATA_KEY, {
        "cycle_id": cycle_id, "status": status, "detail": outcome.status, "at": int(time.time()),
        "before_tokens": outcome.before_tokens, "after_tokens": outcome.after_tokens,
    })
    return result


async def run_scheduled_compression(
    runner: Any, *, profile: str, cycle_id: str, limit: int = DEFAULT_LIMIT,
    active_minutes: Optional[int] = DEFAULT_ACTIVE_MINUTES, session_keys: Optional[List[str]] = None,
    dry_run: bool = False, include_highlights: bool = True,
) -> Dict[str, Any]:
    """Compress this cycle's eligible conversations for *profile* and report what actually happened."""
    store = getattr(runner, "async_session_store", None)
    if store is None:
        return {"ok": False, "error": "session store unavailable", "profile": profile,
                "cycle_id": cycle_id}
    namespace = key_namespace_for_profile(runner, profile)
    if namespace is None:
        return {"ok": False, "error": f"this gateway does not serve profile {profile!r}",
                "profile": profile, "cycle_id": cycle_id}
    entries = eligible_entries(await store.list_sessions(active_minutes), namespace=namespace,
                              session_keys=session_keys, limit=limit)
    sessions: List[Dict[str, Any]] = []
    for entry in entries:
        try:
            sessions.append(await _compress_one_entry(
                runner, entry, cycle_id=cycle_id, dry_run=dry_run,
                include_highlights=include_highlights))
        except Exception as exc:
            logger.warning("Scheduled compression failed for %s: %s", entry.session_key, exc,
                           exc_info=True)
            sessions.append({"session_key": entry.session_key,
                             "session_ref": session_ref(entry.session_key),
                             "status": "failed", "reason": f"{type(exc).__name__}: {exc}"})
    counts: Dict[str, int] = {}
    for row in sessions:
        counts[row["status"]] = counts.get(row["status"], 0) + 1
    return {"ok": True, "profile": profile, "cycle_id": cycle_id, "dry_run": dry_run,
            "eligible": len(entries), "counts": counts, "sessions": sessions,
            "gateway_pid": os.getpid(), "answered_at": time.time()}


def compress_session_verb(runner: Any, loop: Any):
    """Build the ``compress-session`` control-socket handler.

    The handler runs on the socket's executor thread; the work must happen on the gateway loop
    (session store, agent cache, adapters), so it is marshalled there and awaited. A cycle that
    outlives ``timeout`` is NOT abandoned — it keeps running on the loop and the caller is told
    ``pending``, exactly as ``rescan-profiles`` does, so a slow aux model cannot make a real
    compression look like a failure.
    """
    import concurrent.futures

    def _handler(params: Dict[str, Any]) -> Dict[str, Any]:
        profile = str(params.get("profile") or "").strip()
        cycle_id = str(params.get("cycle_id") or "").strip()
        if not profile or not cycle_id:
            return {"ok": False, "error": "compress-session requires 'profile' and 'cycle_id'"}
        keys = params.get("session_keys")
        kwargs: Dict[str, Any] = {
            "profile": profile, "cycle_id": cycle_id,
            "dry_run": bool(params.get("dry_run")),
            "include_highlights": params.get("include_highlights") is not False,
            "session_keys": [str(k) for k in keys] if isinstance(keys, list) else None,
        }
        for name, default in (("limit", DEFAULT_LIMIT), ("active_minutes", DEFAULT_ACTIVE_MINUTES)):
            with contextlib.suppress(TypeError, ValueError):
                kwargs[name] = int(params[name]) if params.get(name) is not None else default
        timeout = DEFAULT_VERB_TIMEOUT
        with contextlib.suppress(TypeError, ValueError):
            timeout = max(1.0, float(params.get("timeout") or DEFAULT_VERB_TIMEOUT))
        future = asyncio.run_coroutine_threadsafe(
            run_scheduled_compression(runner, **kwargs), loop)
        try:
            return future.result(timeout=timeout)
        except concurrent.futures.TimeoutError:
            return {"ok": True, "pending": True, "profile": profile, "cycle_id": cycle_id,
                    "counts": {}, "sessions": [], "eligible": None,
                    "note": "compression still running on the gateway loop"}

    return _handler

