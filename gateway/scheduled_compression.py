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
  turn against a transcript about to be rotated away. The lease is the MUTATION fence and outlives
  a ``/stop``: /stop may free the slot (admission, typing, the park), but the summarization runs on
  an executor thread that no interrupt or cancellation stops, so the lease is given back only once
  that worker has really finished — a successor turn waits on it rather than loading a transcript
  still being rewritten.
- Idempotent per cycle. The outcome is stamped into the routing entry's own metadata
  (``set_session_metadata``, which deliberately does not advance the activity clock), so a retry
  in the same cycle reports ``already_applied`` instead of compressing twice.
- Honest evidence. ``applied`` exists only where the rewrite COMMITTED — the rotation persisted and
  the routing entry was repointed, or the in-place archive ran. ``compress_now`` answering
  ``compressed`` is not enough: a refused (would-grow) candidate and a caught persistence failure
  both leave the transcript untouched under that status, and such an attempt must stay retryable.
  The before/after token and message counts come from that result. A request that was merely sent
  reports ``requested``/``deferred``, never ``applied``.
- Reconciled handoff. The compressor keeps the latest user message and everything after it
  verbatim, and by contract that tail WINS over the summary — so the handoff's ``## Blocked`` /
  ``## Active State`` lag exactly the exchange where work usually finishes. The morning brief reads
  those sections, so the scheduled run lets its one summarizer call see the retained tail as
  reference (``reconcile_handoff_with_tail``); otherwise the brief reports a blocker the final
  exchange already resolved. The tail stays verbatim; its last assistant text is ``latest_reply``.
  The same call writes ``## Daily Threads`` (``agent/context_compressor_daily_threads.py``), the
  structured thread lines the brief groups by priority.
- Nothing new, nothing summarized. A new cycle id is a new date, not new conversation. A commit stamps
  the ACTIVITY FINGERPRINT of the persisted transcript, and a later cycle whose transcript still has
  it answers ``unchanged`` before reserving anything and without a model call; re-summarizing an
  unchanged handoff only paraphrases it. The fingerprint skips compaction handoffs and synthetic user
  rows, the mirrored cron brief among them, so the cycle's own rewrite and its own delivered brief
  never count as new conversation.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
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
#: Brief sections, each read from ONE handoff heading so a consumer knows what a line is: an open
#: item is something the handoff lists as blocked, never an Active State fact relabelled.
MAX_OPEN_ITEMS = 4
MAX_CORRECTIONS = 3
MAX_LATEST_REPLY_CHARS = 280
MAX_DAILY_THREADS = 12
MAX_UNFORMATTED_THREAD_LINES = 3
_ACTIVE_TASK_RE = re.compile(r"^\*{0,2}active task:?\*{0,2}:?\s*", re.IGNORECASE)


_STATUS_APPLIED = "applied"
_STATUS_ALREADY = "already_applied"
_STATUS_UNCHANGED = "unchanged"
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
    highlights: Dict[str, Any] = field(default_factory=dict)
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

def _message_text(message: Any) -> str:
    content = message.get("content") if isinstance(message, dict) else message
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            part.get("text", "") for part in content
            if isinstance(part, dict) and isinstance(part.get("text"), str))
    return ""


def _last_summary_index(messages: Any) -> Optional[int]:
    """Index of the LAST compaction-handoff message in *messages*, or None."""
    from agent.context_compressor import is_compaction_summary_message

    if not isinstance(messages, list):
        return None
    for index in range(len(messages) - 1, -1, -1):
        if is_compaction_summary_message(messages[index]):
            return index
    return None


def _clip(text: str, limit: int) -> str:
    """*text* cut at a word boundary to at most *limit* characters, with an ellipsis when cut. A
    first word longer than *limit* leaves only the ellipsis, never part of the word."""
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    cut = text[:limit - 1]
    if text[len(cut)] != " ":
        cut = cut.rsplit(" ", 1)[0] if " " in cut else ""
    # Not "-": a credential's last character, and the client's secret scan still has to see it.
    return cut.rstrip(" ,;:") + "…"


def _latest_reply(messages: Any, after: int) -> str:
    """The newest assistant text retained verbatim after the handoff — the conversation's own last
    word, which by the compressor's contract supersedes the summary. First two sentences, clipped."""
    for message in reversed(messages[after + 1:]):
        if not isinstance(message, dict) or message.get("role") != "assistant":
            continue
        text = " ".join(_message_text(message).split())
        if text:
            sentences = re.split(r"(?<=[.!?])\s+", text)
            return _clip(" ".join(sentences[:2]), MAX_LATEST_REPLY_CHARS)
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
        lines.append(_clip(line, MAX_HIGHLIGHT_CHARS))
        if len(lines) >= MAX_HIGHLIGHTS:
            break
    return lines


def _top_level_bullets(body: str, limit: int) -> List[str]:
    """Up to *limit* top-level list items of one section body. Nested sub-bullets are detail of the
    item above them, not items of their own."""
    items: List[str] = []
    for raw in (body or "").splitlines():
        match = re.match(r"^(?:[-*•]|\d+[.)])\s+(.*)$", raw)
        if not match:
            continue
        item = match.group(1).strip().rstrip(":").strip()
        if not item or item.lower() in {"none", "n/a", "none.", "(none)"} or item.startswith("["):
            continue
        items.append(_clip(item, MAX_HIGHLIGHT_CHARS))
        if len(items) >= limit:
            break
    return items


def _active_task(sections: Dict[str, str]) -> str:
    """The handoff's own statement of the current task: ``## Active Task`` when the template has
    it, else the ``Active task:`` line the lean template writes under ``## Active State``."""
    for item in _top_level_bullets(sections.get("Active Task", ""), 1):
        return item
    for raw in (sections.get("Active State") or "").splitlines():
        line = raw.strip().lstrip("-*•").strip()
        if _ACTIVE_TASK_RE.match(line):
            return _clip(_ACTIVE_TASK_RE.sub("", line), MAX_HIGHLIGHT_CHARS)
    return ""


def _daily_threads(sections: Dict[str, str]) -> Optional[Dict[str, Any]]:
    """The ``## Daily Threads`` lines as structured threads (clipped, bounded), or None when the
    handoff has no such section — a handoff written before it existed, which the brief must call
    out rather than dress up as prioritized threads."""
    from agent.context_compressor_daily_threads import DAILY_THREADS_HEADING, parse_daily_threads

    body = next((text for heading, text in sections.items()
                 if heading.lower() == DAILY_THREADS_HEADING.lower()), None)
    if body is None:
        return None
    threads, unformatted = parse_daily_threads(body)
    for thread in threads:
        for name in ("title", "state", "next"):
            if name in thread:
                thread[name] = _clip(thread[name], MAX_HIGHLIGHT_CHARS)
        if "dependency" in thread:
            thread["dependency"]["detail"] = _clip(thread["dependency"]["detail"], MAX_HIGHLIGHT_CHARS)
    return {"threads": threads[:MAX_DAILY_THREADS],
            "unformatted": [_clip(line, MAX_HIGHLIGHT_CHARS)
                            for line in unformatted[:MAX_UNFORMATTED_THREAD_LINES]],
            "unformatted_total": len(unformatted)}


def handoff_highlights(messages: Any) -> Dict[str, Any]:
    """Bounded projection of the handoff the compressor wrote, plus the retained final reply.

    ``open_threads``/``learnings``: the first heading in each group that actually has content wins,
    so a summary whose ``## Blocked`` is empty falls through to ``## Active State`` rather than
    reporting an empty bullet. The brief sections each come from exactly one heading:
    ``active_task``, ``open_items`` (``## Blocked``), ``corrections`` (``## Errors & Fixes``), and
    ``latest_reply`` — the newest assistant text after the handoff, which the summary cannot reflect.
    """
    index = _last_summary_index(messages)
    sections = _sections(_message_text(messages[index])) if index is not None else {}
    if not sections:
        return {}
    out: Dict[str, List[str]] = {}
    for name, headings in (("open_threads", _OPEN_THREAD_HEADINGS), ("learnings", _LEARNING_HEADINGS)):
        for heading in headings:
            bullets = _bullets(sections.get(heading, ""))
            if bullets:
                out[name] = bullets
                break
    brief = {
        "active_task": [t for t in (_active_task(sections),) if t],
        "open_items": _top_level_bullets(sections.get("Blocked", ""), MAX_OPEN_ITEMS),
        "corrections": _top_level_bullets(sections.get("Errors & Fixes", ""), MAX_CORRECTIONS),
        "latest_reply": [t for t in (_latest_reply(messages, index),) if t],
    }
    out.update({name: items for name, items in brief.items() if items})
    daily_threads = _daily_threads(sections)
    if daily_threads is not None:
        out["daily_threads"] = daily_threads
    return out


def activity_fingerprint(messages: Any) -> str:
    """Digest of the conversation's own turns: every user/assistant/tool row in order, minus
    compaction handoffs and synthetic user rows (runtime scaffolding and the mirrored cron brief).

    Read it from STORED rows (``load_transcript(..., repair_alternation=False)``): the replay repair
    merges a mirrored brief into the next real user turn, and a merged row can be neither skipped
    nor kept without losing one of them. A rewrite that only replaced old turns with a handoff, or a
    cron delivery appended to the chat, leaves it unchanged; any new exchange changes it."""
    from agent.context_compressor import (ContextCompressor, _synthetic_user_row,
                                          is_compaction_summary_message)

    turns = []
    for message in messages if isinstance(messages, list) else ():
        role = message.get("role") if isinstance(message, dict) else None
        if role not in ("user", "assistant", "tool") or is_compaction_summary_message(message):
            continue
        text = _message_text(message)
        if role == "user" and (_synthetic_user_row(text)
                               or ContextCompressor._is_synthetic_compression_user_turn(message)):
            continue
        turns.append([role, text])
    return hashlib.sha256(json.dumps(turns, ensure_ascii=False, separators=(",", ":"))
                          .encode("utf-8")).hexdigest()


def reconcile_handoff_with_tail(compressor: Any, stored_rows: List[Dict[str, Any]]) -> bool:
    """Let *compressor*'s summarizer see the verbatim tail so the handoff's state sections reflect
    it; False if it is not the built-in ``ContextCompressor`` (a plugin context engine keeps its own
    summary policy).

    Only ever applied to the throwaway agent a scheduled compression builds, so no live agent's
    compression changes. Same single summarizer call; the tail is added to its input, not summarized.
    Daily Threads codes are verified against *stored_rows*, the conversation as stored: replay
    merges a mirrored brief into the user rows around it, which would make the brief's own codes
    read as the user's, or the user's reply to it as the brief.
    """
    from agent.context_compressor import ContextCompressor

    if not isinstance(compressor, ContextCompressor):
        return False
    compressor.reconcile_with_retained_tail = True
    compressor.code_evidence_rows = stored_rows
    return True


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


def _unchanged_since_compression(marker: Any, fingerprint: str) -> bool:
    """True when the last scheduled compression COMMITTED and nothing was said since. A marker
    from before fingerprints existed, or from a failed or deferred attempt, never matches: those
    conversations stay eligible."""
    return (isinstance(marker, dict) and marker.get("status") == _STATUS_APPLIED
            and marker.get("activity_fingerprint") == fingerprint)


async def _stored_rows(store: Any, session_id: str) -> List[Dict[str, Any]]:
    return await store.load_transcript(session_id, repair_alternation=False)


def _settled_without_compressing(row: Dict[str, Any], marker: Any, cycle_id: str,
                                 current: List[Dict[str, Any]],
                                 include_highlights: bool) -> Optional[Dict[str, Any]]:
    """The answer for a conversation this cycle must not compress, or None when it should.

    Already compressed in this cycle, or nothing said since the last committed compression. The
    fingerprint is always the conversation as it stands now: anything said since this cycle's
    compression is new context for the brief, though it is not compressed twice."""
    fingerprint = activity_fingerprint(current)
    if _already_done(marker, cycle_id):
        out = {**row, "status": _STATUS_ALREADY, "activity_fingerprint": fingerprint,
               "previous": {k: marker.get(k) for k in ("status", "at", "after_tokens")}}
    elif _unchanged_since_compression(marker, fingerprint):
        out = {**row, "status": _STATUS_UNCHANGED, "activity_fingerprint": fingerprint}
    else:
        return None
    if include_highlights:
        # A retry whose first attempt never delivered still needs the handoff it would have shown.
        out["highlights"] = handoff_highlights(current)
    return out


# ---------------------------------------------------------------------------
# Reservation: this operation is a turn as far as the rest of the gateway is concerned
# ---------------------------------------------------------------------------

#: ``_acquire_session_lease`` sentinel: the lease is held by a turn we must not race.
_LEASE_CONTENDED = object()
#: ``_acquire_session_lease`` sentinel: the lease became ours only AFTER /stop (or an eviction)
#: invalidated the reservation we were waiting under. Nothing has been read yet; give it straight
#: back and defer.
_LEASE_STALE = object()


@dataclass
class _Reservation:
    """This operation's claim on one conversation, and the handles that make it releasable exactly
    once by its OWN tail: the run generation that owns the turn slot, the lease token held for the
    whole mutation region, and the event an inbound task parks on until the slot is given back.

    The lease token lives HERE rather than in the session's ``turn.lease_tokens``, and that is the
    fencing decision, not bookkeeping. ``turn.lease_tokens`` is swept by ``_drop_turn_slot`` for
    every generation older than the current one — correct for a turn, whose agent is interrupted and
    whose coroutine is the only thing that can still write, and WRONG for this operation, whose
    summarization runs on an executor thread that no interrupt and no cancellation can stop. A
    ``/stop`` arriving mid-compression would otherwise hand the canonical lease to a successor turn
    while this operation's worker was still able to rotate or archive the very transcript that
    successor is loading. Held privately, the token is released by exactly one site — this
    operation's own tail, after the worker has truly stopped — and every registry call on it is
    identity-checked against the current holder, so a successor's lease can never be released or
    rebound by us either.
    """

    generation: int
    released: Any  # asyncio.Event
    token: Any = None


def _claim_turn_slot(runner: Any, session_key: str) -> Optional[_Reservation]:
    """Reserve this conversation's running-turn slot, or None when a turn already holds it.

    The check and the claim must sit in ONE synchronous block: between them is exactly the window
    the review reproduced — an inbound message passing the admission guard and starting a turn while
    this operation reads the history it is about to rotate away. The claim is the same pending
    sentinel the boot auto-resume path installs (``run_startup.py::
    _schedule_resume_pending_sessions``), read by the same admission guard
    (``run_inbound.py::_hm_handle_running_session_message`` via ``_is_session_running``), so inbound
    QUEUES behind the compression instead of interrupting it: an interrupt is skipped for a
    sentinel-held slot, and ``/stop`` stays the escape hatch.

    Two things make the claim a real turn's claim rather than a lookalike:

    - ``turn.reservation`` — a sentinel slot held by a reservation has NO turn behind it, so nothing
      would ever drain the adapter's pending slot; inbound parks on this event instead of queueing
      into a slot the adapter's own in-band drain re-dispatches immediately (a tight redispatch loop).
    - a fresh RUN GENERATION, exactly as ``_handle_message`` takes after its own sentinel claim. Every
      release/rebind this operation performs is guarded by it, so a ``/stop`` that clears this
      sentinel mid-flight and a successor turn that claims the slot in that window keep their own
      turn slot — this operation's tail can only ever free its own. The mutation LEASE is
      deliberately not covered by that handoff; see ``_Reservation``.
    """
    from gateway.run import _AGENT_PENDING_SENTINEL

    if runner._is_session_running(session_key):
        return None
    # A turn frees its running slot when the agent returns, but holds its turn lease until the
    # outer handler has shaped, delivered and persisted the reply. Claiming in that gap would bump
    # the generation under it (the turn then drops its reply as stale) and the turn's unconditional
    # cleanup would clear this reservation, so a held lease is a running turn here.
    peeked = runner._peek_session_state(session_key)
    if peeked is not None and peeked.turn.lease_tokens:
        return None
    state = runner._session_state(session_key)
    state.turn.agent = _AGENT_PENDING_SENTINEL
    state.turn.started_ts = time.time()
    state.turn.reservation = asyncio.Event()
    reservation = _Reservation(runner._begin_session_run_generation(session_key),
                               state.turn.reservation)
    runner._persist_active_agents()
    return reservation


async def _acquire_session_lease(runner: Any, session_key: str, session_id: str,
                                 reservation: _Reservation) -> Any:
    """Hold the per-session_id turn lease for the [read history → compress → commit] region.

    The slot claim above is keyed by ROUTING KEY; the transcript is owned by SESSION_ID, and
    ``switch_session`` makes that mapping many-to-one, so a second key resolving this same id would
    still start a turn. This is the lease every real turn takes for the same region
    (``run_turn.py::_hmwa_acquire_turn_lease``) — the canonical serialization point a successor turn
    waits on — and the token is parked on the reservation, released once by this operation's tail
    after its worker has stopped (see ``_Reservation``).

    ``acquire`` AWAITS under contention, and /stop can invalidate this reservation in that window.
    Winning the lease afterwards would mean compressing on behalf of a reservation that no longer
    owns the conversation, so the generation is re-checked the moment the lease is ours and before
    anything is read: a stale acquisition gives the lease straight back and defers.

    Returns True when the region is ours (held, or no registry to hold), ``_LEASE_CONTENDED`` when a
    turn holds it, ``_LEASE_STALE`` when the reservation died while we waited. Both sentinels fail
    closed: defer, retry next cycle.
    """
    from gateway.turn_lease import DEFAULT_LEASE_WAIT, TurnLeaseTimeoutError

    registry = getattr(runner, "_turn_leases", None)
    if registry is None or not session_id:
        return runner._is_session_run_current(session_key, reservation.generation) or _LEASE_STALE
    from gateway.run import _float_env
    try:
        token = await registry.acquire(
            session_id, owner_key=session_key, generation=reservation.generation,
            timeout=_float_env("HERMES_TURN_LEASE_TIMEOUT", DEFAULT_LEASE_WAIT))
    except TurnLeaseTimeoutError:
        return _LEASE_CONTENDED
    reservation.token = token
    if not runner._is_session_run_current(session_key, reservation.generation):
        _release_reservation_lease(runner, reservation)
        return _LEASE_STALE
    return True


def _release_reservation_lease(runner: Any, reservation: _Reservation) -> None:
    """Give back the mutation lease — the ONLY site that does. Identity-checked by the registry, so a
    token the compression no longer holds (or a successor's) is a safe no-op; idempotent."""
    registry, token = getattr(runner, "_turn_leases", None), reservation.token
    reservation.token = None
    if registry is None or token is None:
        return
    try:
        registry.release(token)
    except Exception:
        logger.debug("Failed to release the scheduled compression's turn lease", exc_info=True)


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------

def _finish_cancelled_worker(runner: Any, reservation: _Reservation, done: Any) -> None:
    """Release the mutation lease of a compression whose caller was cancelled mid-flight, once the
    worker itself finishes. Also retrieves the result so a failure is logged rather than surfacing as
    a never-retrieved task exception."""
    with contextlib.suppress(asyncio.CancelledError):
        if (exc := done.exception()) is not None:
            logger.warning("Scheduled compression finished after its caller was cancelled: %s", exc,
                           exc_info=exc)
    _release_reservation_lease(runner, reservation)


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

    from gateway.session_transcript import TranscriptReadError

    marker = await store.get_session_metadata(session_key, CYCLE_METADATA_KEY)
    if _already_done(marker, cycle_id):
        try:
            current = await _stored_rows(store, entry.session_id)
        except TranscriptReadError:
            # No fingerprint, so the cycle cannot go quiet on a conversation it could not read.
            return {**row, "status": _STATUS_ALREADY,
                    "previous": {k: marker.get(k) for k in ("status", "at", "after_tokens")}}
        return _settled_without_compressing(row, marker, cycle_id, current, include_highlights)

    # Fail closed on a key the persisted origin no longer reproduces: the compression lock, the
    # agent-cache eviction and the topic re-bind all key off the SOURCE, so a mismatch would
    # compress this transcript while unlocking and evicting a different conversation.
    try:
        derived = runner._session_key_for_source(entry.origin)
    except Exception as exc:
        return {**row, "status": "failed", "reason": f"origin_unusable: {type(exc).__name__}: {exc}"}
    if derived != session_key:
        return {**row, "status": "failed", "reason": "origin_key_mismatch"}

    # Nothing said since the last committed compression: answer before reserving the conversation
    # and before any model call. Read-only; a turn that lands after this read is tomorrow's news.
    try:
        current = await _stored_rows(store, entry.session_id)
    except TranscriptReadError as exc:
        return {**row, "status": "failed", "reason": f"transcript_unreadable: {exc}"}
    settled = _settled_without_compressing(row, marker, cycle_id, current, include_highlights)
    if settled is not None:
        return settled

    # Reserve the conversation BEFORE reading its history: everything from here to the commit must
    # be serialized against inbound turns, or an arriving message runs a turn whose parent session
    # this compression is about to close (rotation) or rewrite (in place).
    reservation = _claim_turn_slot(runner, session_key)
    if reservation is None:
        return {**row, "status": "deferred", "reason": "turn_running"}
    try:
        deferred = await _deferral_reason(runner, session_key)
        if deferred:
            return {**row, "status": "deferred", "reason": deferred}
        lease = await _acquire_session_lease(runner, session_key, entry.session_id, reservation)
        if lease is _LEASE_CONTENDED:
            return {**row, "status": "deferred", "reason": "turn_lease_contended"}
        if lease is _LEASE_STALE:
            # /stop (or an eviction) took the conversation while we waited out the contention. Not a
            # line of history has been read; defer before reading any.
            return {**row, "status": "deferred", "reason": "reservation_invalidated"}
        # The mutation region runs as its OWN task behind a shield. Cancelling this coroutine (gateway
        # shutdown, a cancelled caller) does not stop the summarization: it runs on an executor thread,
        # and the in-place archive it performs is a transcript write. So cancellation must never be
        # allowed to reach the lease release while that thread can still write — it unwinds only after
        # the worker has truly stopped.
        worker = asyncio.ensure_future(_compress_reserved_entry(
            runner, entry, row, cycle_id=cycle_id, dry_run=dry_run,
            include_highlights=include_highlights, reservation=reservation))
        try:
            return await asyncio.shield(worker)
        finally:
            if worker.done():
                # Identity-checked by the registry, so even a /stop that handed the slot to a
                # successor cannot make this free the successor's lease.
                _release_reservation_lease(runner, reservation)
            else:
                # We were cancelled, the worker was not. Awaiting it here would just be cancelled
                # again, so the lease TRAVELS WITH THE WORKER: the release fires when the worker
                # (and with it the executor thread that can still write) has actually finished.
                worker.add_done_callback(
                    lambda done: _finish_cancelled_worker(runner, reservation, done))
    finally:
        # Generation-guarded, exactly as a real turn's finalizer (``run_inbound.py::_handle_message``):
        # /stop can clear this sentinel while the compression is still unwinding and a successor turn
        # may already own the slot — an unconditional release would clear ITS live turn, leaving busy
        # routing and interruption disabled for a turn that is actually running.
        runner._release_running_agent_state(session_key, run_generation=reservation.generation)
        # A release the generation guard refused did not reach ``turn.clear()``, so nothing set this;
        # an inbound task parked on a reservation that is over must not wait out its whole cap.
        reservation.released.set()


async def _compress_reserved_entry(runner: Any, entry: Any, row: Dict[str, Any], *, cycle_id: str,
                                   dry_run: bool, include_highlights: bool,
                                   reservation: _Reservation) -> Dict[str, Any]:
    """Read, compress and commit one RESERVED conversation (slot + turn lease already held)."""
    from agent.conversation_compression_manual import MIN_MESSAGES, parse_compress_args
    from gateway.session_transcript import TranscriptReadError

    session_key = entry.session_key
    store = runner.async_session_store
    try:
        history = await store.load_transcript(entry.session_id)
        stored = await _stored_rows(store, entry.session_id)
    except TranscriptReadError as exc:
        return {**row, "status": "failed", "reason": f"transcript_unreadable: {exc}"}
    # Read again now that the conversation is ours: a concurrent request may have compressed it
    # between this request's unreserved read and its reservation.
    settled = _settled_without_compressing(
        row, await store.get_session_metadata(session_key, CYCLE_METADATA_KEY), cycle_id, stored,
        include_highlights)
    if settled is not None:
        return settled
    if not history or len(history) < MIN_MESSAGES:
        # Settled, not pending: the same short transcript tomorrow is still nothing new.
        return {**row, "status": "deferred", "reason": "too_short",
                "before_messages": len(history or []),
                "activity_fingerprint": activity_fingerprint(stored)}
    if dry_run:
        return {**row, "status": "eligible", "before_messages": len(history)}

    with runner._profile_scope_for_source(entry.origin):
        outcome = await runner._run_manual_compression(
            entry.origin, entry, history, parse_compress_args(""), reconcile_rows=stored)
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
    # lease before the caller releases, exactly as a real turn does. The registry rebinds only on the
    # token's own identity (holder check), so a rotation racing a /stop + successor can never pull the
    # successor's lease onto this id.
    if outcome.rotated and reservation.token is not None:
        registry = getattr(runner, "_turn_leases", None)
        if registry is not None:
            with contextlib.suppress(Exception):
                registry.rebind(reservation.token, outcome.new_session_id)
    marker = {"cycle_id": cycle_id, "status": status, "detail": outcome.status, "at": int(time.time()),
              "before_tokens": outcome.before_tokens, "after_tokens": outcome.after_tokens}
    if status == _STATUS_APPLIED:
        # Fingerprint what was PERSISTED, read back the way the next cycle will read it. Unreadable
        # means no fingerprint: the next cycle compresses again instead of trusting a guess.
        with contextlib.suppress(TranscriptReadError):
            marker["activity_fingerprint"] = result["activity_fingerprint"] = activity_fingerprint(
                await _stored_rows(store, outcome.new_session_id or entry.session_id))
    # Stamp AFTER the rewrite persisted, so a crash mid-compression leaves the cycle retryable.
    await store.set_session_metadata(session_key, CYCLE_METADATA_KEY, marker)
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

