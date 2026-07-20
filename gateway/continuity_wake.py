"""Gateway seam for the Dream Cycle v3 wake broker (Phase 3).

Prompt-cache invariant: the packet is built at most once per durable
session — only when the caller sees a session whose first model call is
still pending and no packet binding exists yet. The packet id/hash/project
and the rendered text are bound to the SessionEntry AND persisted by
durable session_id in state.db, and every later turn re-appends the
*stored* text verbatim after validation. Nothing is ever rebuilt from the
continuity store mid-session, so an active session stays stable even while
the Dream Cycle mutates the store; the next /new or reset picks up fresh
state, and /resume, /branch, compression rotation, and crash recovery
restore the original binding instead of rebuilding.

Profile isolation: every path (store, project docs, kanban root) derives
from an explicit profile home passed by the caller — never from ambient
state read at packet time — and the store must resolve below that
profile's dream-cycle-v3 home with no symlink crossing.

Fail closed: any failure here returns None and the session starts exactly
as it does today; corrupt persisted metadata is dropped, never injected.
Hot USER.md/MEMORY.md injection is untouched — the packet rides the
per-turn session context prompt (ephemeral system content), not the cached
memory system prompt.
"""
import hashlib
import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from hermes_constants import get_hermes_home

logger = logging.getLogger(__name__)

# Mirrors dream_cycle_v3.wake.PACKET_BUDGET without importing the package —
# validation must work (and fail closed) even where the package cannot load.
WAKE_PACKET_TEXT_LIMIT = 1600
_HASH_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
WAKE_STATE_SCHEMA_VERSION = 1
# How many parent_session_id hops a restore may walk. Compression rotation
# and /branch both set parent_session_id on the child row, so the packet
# bound before a crash in the publication window is one hop up.
_PARENT_CHAIN_LIMIT = 10


def _profile_name_for_home(home: Path) -> str:
    """Profile identity from a resolved home (<root>/profiles/<name> in
    profile mode, 'default' otherwise)."""
    if home.parent.name == "profiles":
        return home.name
    return "default"


def _active_profile_name() -> str:
    """Profile identity of the ambient HERMES_HOME (single-profile callers)."""
    return _profile_name_for_home(get_hermes_home())


def validate_wake_binding(packet_id: Any, content_hash: Any, project_id: Any,
                          text: Any) -> bool:
    """Strict fail-closed validation of a persisted wake binding: types,
    identifier shape, text budget, and the content hash over the text."""
    try:
        if not isinstance(packet_id, str) or not packet_id or len(packet_id) > 200:
            return False
        if not isinstance(content_hash, str) or not _HASH_RE.match(content_hash):
            return False
        if project_id is not None and (not isinstance(project_id, str)
                                       or len(project_id) > 200):
            return False
        if (not isinstance(text, str) or not text
                or len(text) > WAKE_PACKET_TEXT_LIMIT):
            return False
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        return content_hash == f"sha256:{digest}"
    except Exception:
        return False


def build_wake_packet_for_session(first_message: str,
                                  workspace_path: str | None = None,
                                  profile_home: Path | None = None,
                                  session_project_id: str | None = None):
    """Build a bounded wake packet for a genuinely new session, or None.

    Read-only over the owned continuity store, explicit project-docs home
    and tracker roots — all bound to *profile_home* (the source profile's
    resolved home; the active HERMES_HOME when not multiplexing). Never
    raises.
    """
    try:
        from dream_cycle_v3.wake import WakeInputs, build_wake_packet
        from tools.continuity_tool import (continuity_projects_home,
                                           continuity_skills_home,
                                           continuity_store_path,
                                           kanban_root,
                                           resolved_profile_home,
                                           todoist_export_path)

        home = resolved_profile_home(profile_home)
        profile = _profile_name_for_home(home)
        return build_wake_packet(
            store_path=continuity_store_path(home),
            projects_home=continuity_projects_home(home),
            skills_home=continuity_skills_home(home),
            kanban_root=kanban_root(home),
            todoist_export_path=todoist_export_path(home),
            # Anchor confinement at the profile home so a symlinked
            # dream-cycle-v3 directory (not just a symlinked db file) is
            # refused as a cross-profile crossing.
            confine_root=home,
            inputs=WakeInputs(
                profile=profile,
                # The profile home is the isolation boundary. Profile names
                # are not continuity owners (test/clone profiles commonly
                # consume threads owned by their source personality), so an
                # inferred profile-name owner filter would silently hide
                # valid state. Callers with a configured owner can add that
                # explicit filter in a future contract revision.
                owner=None,
                now=datetime.now(timezone.utc).isoformat(),
                first_message=first_message or "",
                workspace_path=workspace_path,
                session_project_id=session_project_id,
            ),
        )
    except Exception:
        logger.exception("wake packet construction failed (session starts "
                         "without one)")
        return None


def bind_wake_packet(session_entry, packet) -> None:
    """Record the packet binding on the session entry (id/hash/project/text).
    The caller persists the entry via the session store."""
    session_entry.wake_packet_id = packet.packet_id
    session_entry.wake_packet_hash = packet.content_hash
    session_entry.wake_packet_project_id = packet.project_id
    session_entry.wake_packet_text = packet.text


def ensure_wake_packet(session_entry, *, is_new_session: bool,
                       first_message: str,
                       workspace_path: str | None = None,
                       profile_home: Path | None = None,
                       session_db=None) -> bool:
    """Bind a wake packet at most once per session.

    Builds only when the caller attests this session's first model call is
    still pending AND the entry carries no packet binding yet — an
    already-bound entry is never rebuilt, however the continuity store has
    changed since. Returns True when a packet binding landed on the entry
    (the caller persists the entry). Never raises.

    With *session_db*, the durable state.db record for the entry's
    session_id (binding OR attempted-none sentinel, parent chain included)
    is the once-per-session marker — the same lifecycle the API/TUI/ACP
    surfaces use. A first-call attempt that produces no packet durably
    persists the sentinel, so a crash/replay against a stale routing store
    or a cross-surface retry can never rebuild against future continuity
    state; a durable binding is restored onto the entry instead of rebuilt,
    and corrupt durable state is terminal. Without *session_db* the legacy
    in-memory gate (routing store only) applies unchanged.
    """
    if getattr(session_entry, "wake_packet_id", None):
        return False
    if session_db is not None and getattr(session_entry, "session_id", None):
        state, binding = ensure_wake_state_for_session_id(
            session_db, session_entry.session_id,
            is_new_session=is_new_session,
            first_message=first_message,
            workspace_path=workspace_path,
            profile_home=profile_home,
            create_source="gateway")
        if state == "bound":
            return apply_wake_binding(session_entry, binding)
        return False
    if not is_new_session:
        return False
    packet = build_wake_packet_for_session(first_message, workspace_path,
                                           profile_home)
    if packet is None:
        return False
    bind_wake_packet(session_entry, packet)
    return True


def validated_wake_text(session_entry) -> Optional[str]:
    """The stored packet text if the binding validates, else None.

    Corrupt metadata (wrong types, oversize text, hash mismatch) is dropped
    from the entry so it can never be injected — fail closed.
    """
    packet_id = getattr(session_entry, "wake_packet_id", None)
    text = getattr(session_entry, "wake_packet_text", None)
    if not packet_id and not text:
        return None
    if validate_wake_binding(packet_id,
                             getattr(session_entry, "wake_packet_hash", None),
                             getattr(session_entry, "wake_packet_project_id",
                                     None),
                             text):
        return text
    logger.warning("wake: dropping corrupt persisted packet binding for "
                   "session %s", getattr(session_entry, "session_id", "?"))
    session_entry.wake_packet_id = None
    session_entry.wake_packet_hash = None
    session_entry.wake_packet_project_id = None
    session_entry.wake_packet_text = None
    return None


# ---------------------------------------------------------------------------
# Durable per-session_id persistence (state.db `sessions.wake_packet_json`)
#
# The column holds one of:
#   - a full validated binding (packet id/hash/project/text), or
#   - the explicit attempted-none sentinel {"schema_version":1,"state":"none"}
#     recorded when the session's one first-call attempt produced no packet
#     (no store, degraded-to-nothing, validation failure). The sentinel is
#     the durable first-call marker: a later request on the same session —
#     however "new" it looks to the surface (empty history, replayed
#     conversation) — can never opportunistically bind.
#   - the pre-first-call pending sentinel
#     {"schema_version":1,"state":"pending"}, written when a surface
#     durably persists a session row BEFORE its first model-bound user
#     prompt (ACP creates rows at session creation; the TUI persists on
#     /title and similar pre-prompt intents). It is the durable form of the
#     surfaces' in-memory "wake still deferred" markers: after a process
#     restart the surface can no longer attest newness, so the sentinel is
#     what keeps the session bind-eligible for exactly one first-call
#     attempt — whose outcome (binding or attempted-none) replaces it via
#     the CAS. History-bearing sessions never carry it, so they can never
#     become eligible through restart recovery.
# ---------------------------------------------------------------------------

WAKE_STATE_NONE = "none"
_NONE_SENTINEL_JSON = json.dumps({"schema_version": WAKE_STATE_SCHEMA_VERSION,
                                  "state": WAKE_STATE_NONE})
WAKE_STATE_PENDING = "pending"
_PENDING_SENTINEL_JSON = json.dumps(
    {"schema_version": WAKE_STATE_SCHEMA_VERSION,
     "state": WAKE_STATE_PENDING})


def wake_binding_to_json(session_entry) -> Optional[str]:
    """Serialize a validated entry binding for state.db, or None."""
    if validated_wake_text(session_entry) is None:
        return None
    return json.dumps({
        "schema_version": WAKE_STATE_SCHEMA_VERSION,
        "packet_id": session_entry.wake_packet_id,
        "content_hash": session_entry.wake_packet_hash,
        "project_id": session_entry.wake_packet_project_id,
        "text": session_entry.wake_packet_text,
    })


def wake_state_from_json(raw: Any) -> tuple[str, Optional[dict]]:
    """Classify a persisted wake_packet_json value.

    Returns ('bound', binding) for a validated binding, ('none', None) for
    the explicit attempted-none sentinel, ('pending', None) for the
    pre-first-call pending sentinel, ('absent', None) ONLY for a truly
    missing value (NULL/empty), and ('corrupt', None) for anything present
    but malformed, oversized, mistyped, or hash-invalid. Corrupt is a
    distinct terminal state: the session HAD durable wake state, so it can
    never be injected AND never rebuilt over — fail closed means no packet,
    not a fresh bind into an existing transcript.
    """
    if raw is None or raw == "":
        return "absent", None
    if not isinstance(raw, str) or len(raw) > 64_000:
        return "corrupt", None
    try:
        data = json.loads(raw)
    except ValueError:
        return "corrupt", None
    if not isinstance(data, dict):
        return "corrupt", None
    if data.get("schema_version") != WAKE_STATE_SCHEMA_VERSION:
        return "corrupt", None
    if data.get("state") == WAKE_STATE_NONE and "packet_id" not in data:
        return "none", None
    if data.get("state") == WAKE_STATE_PENDING and "packet_id" not in data:
        return WAKE_STATE_PENDING, None
    if not validate_wake_binding(data.get("packet_id"),
                                 data.get("content_hash"),
                                 data.get("project_id"), data.get("text")):
        return "corrupt", None
    return "bound", data


def wake_binding_from_json(raw: Any) -> Optional[dict]:
    """Parse + validate a persisted binding; sentinel/corrupt data returns
    None."""
    state, binding = wake_state_from_json(raw)
    return binding if state == "bound" else None


def load_wake_record_for_session(session_db, session_id: str
                                 ) -> tuple[str, Optional[str], Optional[dict]]:
    """Durable wake record for *session_id* as (state, raw, binding),
    walking the parent_session_id chain so compression children, branches,
    and forks inherit the state that produced their transcript. Any
    non-absent record — binding, attempted-none sentinel, pre-first-call
    pending sentinel, or present-but-corrupt bytes — terminates the walk
    (that transcript's session already had durable wake state; children
    must not skip past it to a bound ancestor). ``raw`` is the exact stored
    value at the terminating hop, so callers that must copy a terminal
    record verbatim (fork inheritance, rotation materialization) never
    launder corrupt into absent.

    Fail closed, never raises: exhausting the chain limit with hops left
    reads as terminal ('exhausted' — the transcript descends from durable
    wake state we could not reach, so it must neither inject nor rebuild),
    and a read failure reads as ('unavailable' — retryable, not
    bind-eligible). Only a walk that genuinely ends at a rootless NULL
    reads as 'absent'.
    """
    try:
        current = session_id
        for _ in range(_PARENT_CHAIN_LIMIT):
            if not current:
                return "absent", None, None
            raw = session_db.get_session_wake_packet(current)
            state, binding = wake_state_from_json(raw)
            if state != "absent":
                return state, raw, binding
            row = session_db.get_session(current)
            if not row:
                return "absent", None, None
            current = row.get("parent_session_id")
        if current:
            logger.warning(
                "wake: parent chain for session %s exceeds %d hops; failing "
                "closed (no packet, no rebuild)", session_id,
                _PARENT_CHAIN_LIMIT)
            return "exhausted", None, None
        return "absent", None, None
    except Exception:
        logger.debug("wake: durable state load failed for %s", session_id,
                     exc_info=True)
        return "unavailable", None, None


def load_wake_state_for_session(session_db, session_id: str
                                ) -> tuple[str, Optional[dict]]:
    """The (state, binding) view of load_wake_record_for_session."""
    state, _raw, binding = load_wake_record_for_session(session_db,
                                                        session_id)
    return state, binding


def load_wake_binding_for_session(session_db, session_id: str
                                  ) -> Optional[dict]:
    """The durable binding for *session_id* (parent chain included), or
    None when the state is absent or attempted-none."""
    state, binding = load_wake_state_for_session(session_db, session_id)
    return binding if state == "bound" else None


# States that end a session's wake lifecycle (or prove it never had one):
# a surface that ran an attempt and saw one of these may consume its
# in-memory deferral marker. 'pending' (attempt not run or not persisted)
# and 'unavailable' (storage failure) are retryable — the marker must
# survive, or durable pending outlives the surface's memory of it and a
# restart rearms a history-bearing transcript.
_WAKE_CONCLUDED_STATES = frozenset(
    {"bound", "none", "corrupt", "absent", "exhausted"})


def wake_attempt_concluded(state: str) -> bool:
    """True when *state* is a settled wake outcome for the session (bound,
    terminal, or confirmed-ineligible); False for retryable states."""
    return state in _WAKE_CONCLUDED_STATES


def materialize_wake_record_for_child(session_db, child_session_id: str,
                                      parent_session_id: str,
                                      create_source: str | None = None
                                      ) -> bool:
    """Copy the parent chain's terminating wake record VERBATIM onto a
    freshly rotated/branched child row (only-if-absent CAS), so inheritance
    holds by direct record instead of an ever-growing parent-chain walk —
    a chain deeper than the walk limit would otherwise fail closed and
    lose the binding. Terminal none/corrupt/pending records copy as-is
    (never laundered into absent); an existing child record is never
    overwritten. Never raises; returns True when the copy landed."""
    try:
        if session_db is None or not child_session_id or not parent_session_id:
            return False
        state, raw, _binding = load_wake_record_for_session(
            session_db, parent_session_id)
        if raw is None or state in ("absent", "unavailable", "exhausted"):
            return False
        return bool(session_db.set_session_wake_packet(
            child_session_id, raw, create_source=create_source,
            only_if_absent=True))
    except Exception:
        logger.debug("wake: materialization skipped for child %s of %s",
                     child_session_id, parent_session_id, exc_info=True)
        return False


def mark_wake_pending_for_session_id(session_db, session_id: str,
                                     create_source: str | None = None
                                     ) -> bool:
    """Durably mark *session_id* as still awaiting its first model-bound
    prompt (the pre-first-call pending sentinel). Written when a surface
    persists a session row before any prompt, so a restart between row
    creation and first prompt cannot strand the session packetless: the
    first real prompt after the restart still gets the session's one bind
    attempt. CAS'd only-if-absent — an existing binding or terminal
    none/corrupt record is never overwritten, so a history-bearing session
    can never be re-marked eligible. Without *create_source* no row is ever
    created (callers with a no-empty-row invariant mark only rows they just
    created themselves). Never raises; returns True when the sentinel
    landed.
    """
    try:
        if session_db is None or not session_id:
            return False
        return bool(session_db.set_session_wake_packet(
            session_id, _PENDING_SENTINEL_JSON, create_source=create_source,
            only_if_absent=True))
    except Exception:
        logger.debug("wake: pending marker skipped for %s", session_id,
                     exc_info=True)
        return False


def apply_wake_binding(session_entry, binding: Optional[dict]) -> bool:
    """Copy a validated durable binding onto a reconstructed entry."""
    if not binding:
        return False
    session_entry.wake_packet_id = binding["packet_id"]
    session_entry.wake_packet_hash = binding["content_hash"]
    session_entry.wake_packet_project_id = binding["project_id"]
    session_entry.wake_packet_text = binding["text"]
    return True


# ---------------------------------------------------------------------------
# Shared session-start hook for non-gateway surfaces (API server, TUI, ACP)
# ---------------------------------------------------------------------------

def ensure_wake_state_for_session_id(session_db, session_id: str, *,
                                     is_new_session: bool,
                                     first_message: str = "",
                                     workspace_path: str | None = None,
                                     profile_home: Path | None = None,
                                     create_source: str | None = None
                                     ) -> tuple[str, Optional[dict]]:
    """The shared once-per-session wake lifecycle, keyed by durable state.db
    session_id. Returns the FINAL durable state as (state, binding).

    An existing validated binding wins (old sessions are never rebuilt); an
    explicit attempted-none sentinel means this session already consumed
    its one first-call attempt and stays packetless forever;
    present-but-corrupt durable state is terminal too — it can never be
    injected AND never rebuilt over, however "new" the session looks to the
    surface. The pre-first-call pending sentinel is the durable form of the
    caller's newness attestation: a session durably marked pending (its row
    was persisted before any prompt) gets its one bind attempt at the next
    real first message even when the surface, after a restart, can no
    longer attest ``is_new_session`` itself. Otherwise, only when the
    caller attests this is the session's actual first model-bound user
    message, one packet build is attempted and the outcome — binding or
    sentinel — is persisted under the session_id via a compare-and-set
    (only-if-absent, pending-sentinel replaceable — compared against the
    exact stored bytes the parser classified as pending) write, so
    concurrent first requests cannot install competing packets: exactly one
    outcome wins, and every caller sees the persisted winner.

    Never raises. States split into settled and retryable (see
    wake_attempt_concluded): a storage/build failure reads as
    ('unavailable', None) — no packet this turn, but the session's one
    attempt is NOT consumed, so callers must keep their deferral markers
    and durable pending state stays authoritative.
    """
    try:
        if session_db is None or not session_id:
            return "absent", None
        state, raw, binding = load_wake_record_for_session(session_db,
                                                           session_id)
        if state == "bound" or state == "none":
            return state, binding
        if state in ("corrupt", "exhausted"):
            logger.warning("wake: %s durable wake state for session %s; "
                           "failing closed (no packet, no rebuild)",
                           state, session_id)
            return state, None
        if state == "unavailable":
            # Storage failed; the attempt is not consumed (retryable).
            return state, None
        pending = state == WAKE_STATE_PENDING
        if pending and not is_new_session:
            try:
                if session_db.get_messages(session_id, include_inactive=True):
                    return (finalize_pending_wake_as_none(
                        session_db, session_id,
                        create_source=create_source), None)
            except Exception:
                # Failure to prove an empty transcript is fail-closed. Keep
                # pending retryable rather than injecting into an established
                # conversation.
                return "unavailable", None
        if not pending and not is_new_session:
            return "absent", None
        packet = build_wake_packet_for_session(first_message, workspace_path,
                                               profile_home)
        if packet is None or not validate_wake_binding(
                packet.packet_id, packet.content_hash,
                packet.project_id, packet.text):
            # Durable first-call marker: this session attempted and got
            # nothing; later calls must not bind against future state.
            value, outcome = _NONE_SENTINEL_JSON, ("none", None)
        else:
            binding = {
                "packet_id": packet.packet_id,
                "content_hash": packet.content_hash,
                "project_id": packet.project_id,
                "text": packet.text,
            }
            value, outcome = json.dumps({
                "schema_version": WAKE_STATE_SCHEMA_VERSION, **binding,
            }), ("bound", binding)
        # The CAS treats exactly the bytes we classified as pending as
        # replaceable — the parser's semantics and the storage layer's
        # raw-bytes compare agree even for noncanonical/legacy sentinels.
        won = session_db.set_session_wake_packet(
            session_id, value, create_source=create_source,
            only_if_absent=True,
            treat_as_absent=raw if pending else None,
            require_no_messages=pending)
        if won:
            return outcome
        if pending:
            finalize_pending_wake_as_none(
                session_db, session_id, create_source=create_source)
        # A concurrent first call won the compare-and-set: discard our
        # build and serve the persisted winner so every in-flight request
        # injects the same bytes (or nothing, if the winner was none/
        # corrupt).
        return load_wake_state_for_session(session_db, session_id)
    except Exception:
        logger.exception("wake: session-start hook failed (session starts "
                         "without a packet; the attempt is not consumed)")
        return "unavailable", None


def finalize_pending_wake_as_none(session_db, session_id: str, *,
                                  create_source: str | None = None) -> str:
    """Consume an obsolete pending sentinel without building a packet.

    A pending record is valid only before a session's first model-bound
    prompt. If a surface discovers transcript history after an unavailable
    first-turn read, settle it as ``none`` rather than allow a later restore
    to inject new system-prompt bytes into that conversation. The CAS keeps a
    concurrent binding or terminal result authoritative.
    """
    try:
        state, raw, _binding = load_wake_record_for_session(session_db,
                                                            session_id)
        if state != WAKE_STATE_PENDING or raw is None:
            return state
        won = session_db.set_session_wake_packet(
            session_id, _NONE_SENTINEL_JSON, create_source=create_source,
            only_if_absent=True, treat_as_absent=raw)
        if won:
            return "none"
        state, _raw, _binding = load_wake_record_for_session(session_db,
                                                              session_id)
        return state
    except Exception:
        logger.exception("wake: failed to finalize stale pending state")
        return "unavailable"


def ensure_wake_text_for_session_id(session_db, session_id: str, *,
                                    is_new_session: bool,
                                    first_message: str = "",
                                    workspace_path: str | None = None,
                                    profile_home: Path | None = None,
                                    create_source: str | None = None
                                    ) -> Optional[str]:
    """The stable packet text for this session under the shared lifecycle
    (see ensure_wake_state_for_session_id), or None for the none/corrupt/
    absent states."""
    state, binding = ensure_wake_state_for_session_id(
        session_db, session_id,
        is_new_session=is_new_session,
        first_message=first_message,
        workspace_path=workspace_path,
        profile_home=profile_home,
        create_source=create_source)
    return binding["text"] if state == "bound" else None
