"""Wake broker: bounded, read-only wake packet construction (Phase 3).

Contract (design §8):
- Built once per genuinely new/reset session, before the first model call;
  the caller binds packet id/hash/project to session metadata and never
  rebuilds mid-session.
- Thread selection is bounded and lane-scoped: an activated project
  surfaces only that project's nonterminal threads; with no activated
  project a tiny global due-thread lane applies. Both lanes query a
  SQL-bounded candidate window BEFORE any tracker refresh (broker
  contract), so an oversized backlog can never trigger an unbounded sweep.
- At most three threads, each with a task-SSOT ref, next action, and source
  freshness date; approval-gated items are marked, tracker outages mark
  threads stale (never close them).
- At most one unambiguously activated project with a <=900-char map excerpt
  and a <=400-char project-context skill excerpt. Every activation tier
  requires a fresh `active` registry record; explicit task refs resolve
  through the read-only task adapter to the task's canonical project (never
  provider/board inference alone), and a task whose canonical project
  contradicts the registry terminally abstains.
- Component budgets (threads / map / skill) plus a hard combined cap:
  total packet text <=1600 chars, excluding existing hot memory.
- Everything here is read-only: continuity store opens with mode=ro,
  trackers via the read-only adapters, project docs and context skills via
  confined reads below the explicit profile root.
- Every emitted string passes the fail-closed sanitizer (secretguard +
  email/phone redaction + per-field caps), and to_dict() re-sanitizes and
  hard-caps its serialized output.
- Fail closed: a missing store yields no packet; a corrupt/unowned/
  out-of-root store or any unexpected error yields a neutral packet
  (notice + handles only); a secret-pattern hit suppresses content down to
  the neutral packet. build_wake_packet never raises.

Like the rest of the package, nothing here resolves live profile paths on
its own — the caller names the store, project-docs home, skills home,
tracker roots, and (optionally) the confinement root explicitly. Callers
resolve the profile root once at their boundary; anything at or below it
reached through a symlink is refused.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from pathlib import Path

from .broker import (MAP_EXCERPT_BUDGET, ActivationDecision,
                     ActivationEvidence, ThreadSnapshot,
                     collect_project_threads, load_project_context,
                     load_registry, project_rows_to_registry,
                     resolve_project_activation)
from .canonical import sha256_hex, stable_id
from .context_skill import SKILL_EXCERPT_BUDGET
from .contracts import is_iso_datetime, parse_iso_datetime
from .errors import DreamCycleError
from .project_docs import assert_confined_read_target
from .sanitize import (WITHHELD, cap_serialized, has_raw_pii, sanitize_date,
                       sanitize_identifier, sanitize_text)
from .secretguard import scan_content
from .store import (ContinuityStore, assert_store_confined,
                    inspect_store_identity)

logger = logging.getLogger(__name__)

WAKE_SCHEMA_VERSION = 1
THREAD_LIMIT = 3
PACKET_BUDGET = 1600
NOTICE_BUDGET = 700
TITLE_CHARS = 80
NEXT_ACTION_CHARS = 120
# Bounded candidate windows per lane (queried before tracker refresh).
PROJECT_LANE_CANDIDATES = 12
GLOBAL_DUE_LANE_CANDIDATES = 6
# Hard cap on the serialized to_dict() payload (text budget + metadata).
PACKET_DICT_BUDGET = PACKET_BUDGET + 900

_HANDLES_LINE = ("On-demand: continuity_lookup(project=... | thread_id=... | "
                 "query=...), session_search.")

__all__ = [
    "WAKE_SCHEMA_VERSION", "THREAD_LIMIT", "MAP_EXCERPT_BUDGET",
    "SKILL_EXCERPT_BUDGET", "PACKET_BUDGET", "WakeInputs", "WakePacket",
    "ActivationDecision", "build_wake_packet", "resolve_project_activation",
    "project_rows_to_registry",
]


@dataclass(frozen=True)
class WakeInputs:
    """Session evidence handed to the broker by the gateway. All optional
    fields default to 'no evidence' so absence can never activate a project."""

    profile: str
    owner: str
    now: str                                # ISO-8601 datetime
    first_message: str = ""
    workspace_path: str | None = None
    session_project_id: str | None = None   # existing session binding, if any

    def __post_init__(self) -> None:
        if not self.profile or not self.profile.strip():
            raise DreamCycleError("WakeInputs.profile is required")
        if not self.owner or not self.owner.strip():
            raise DreamCycleError("WakeInputs.owner is required")
        if not is_iso_datetime(self.now):
            raise DreamCycleError(f"WakeInputs.now must be ISO-8601, got {self.now!r}")


@dataclass(frozen=True)
class WakePacket:
    packet_id: str
    content_hash: str                       # "sha256:<hex>" over text
    profile: str
    built_at: str
    project_id: str | None
    project_method: str                     # activation tier or abstention reason
    text: str
    degraded: bool                          # neutral/fallback packet
    thread_ids: tuple[str, ...] = ()
    tracker_stale: bool = False

    def to_dict(self) -> dict[str, Any]:
        # Every field re-passes the fail-closed sanitizers here so a packet
        # reconstructed from persisted/corrupt state still cannot emit a raw
        # secret or an unbounded value. The text keeps its newlines (it is
        # assembled from sanitized parts), so it is re-scanned and capped
        # rather than run through the whitespace-collapsing text sanitizer.
        text = str(self.text)[:PACKET_BUDGET]
        if scan_content(text) or has_raw_pii(text):
            text = WITHHELD
        payload = {
            "schema_version": WAKE_SCHEMA_VERSION,
            "packet_id": sanitize_identifier(self.packet_id, 200),
            "content_hash": sanitize_identifier(self.content_hash, 80),
            "profile": sanitize_text(self.profile, 40),
            "built_at": sanitize_date(self.built_at),
            "project_id": (sanitize_identifier(self.project_id) or None
                           if self.project_id is not None else None),
            "project_method": sanitize_identifier(self.project_method, 40),
            "degraded": bool(self.degraded),
            "thread_ids": [sanitize_identifier(t) for t in self.thread_ids][:THREAD_LIMIT],
            "tracker_stale": bool(self.tracker_stale),
            "text": text,
        }
        return cap_serialized(payload, PACKET_DICT_BUDGET)


@dataclass(frozen=True)
class _ThreadLine:
    thread_id: str
    text: str
    stale: bool


def _thread_line(snapshot: ThreadSnapshot) -> _ThreadLine:
    thread = snapshot.row
    ref = thread["external_task_ref"] or ""
    safe_ref = sanitize_identifier(ref) if ref else "no-task-ref"
    fresh = (thread["last_disposition_date"]
             or (thread["updated_at"] or "")[:10] or "unknown")
    notes = [f"as of {sanitize_identifier(fresh, 40)}"]
    if snapshot.stale:
        notes.append("tracker unavailable — status stale")
    if snapshot.gated:
        notes.append("approval-gated")
    return _ThreadLine(
        thread_id=thread["thread_id"],
        text=(f"- {sanitize_text(thread['title'], TITLE_CHARS)} "
              f"[{safe_ref}] — "
              f"next: {sanitize_text(thread['normalized_next_action'], NEXT_ACTION_CHARS)} "
              f"({sanitize_identifier(thread['state'], 30)}; {'; '.join(notes)})"),
        stale=snapshot.stale,
    )


def _neutral_text(profile: str, built_at: str, reason: str) -> str:
    return "\n".join([
        f"[Continuity wake packet — profile {sanitize_text(profile, 40)}, "
        f"built {built_at}]",
        f"Continuity data unavailable this session ({reason}). "
        "No threads or project loaded.",
        _HANDLES_LINE,
    ])


def _finalize(*, profile: str, built_at: str, text: str, project_id: str | None,
              method: str, degraded: bool, thread_ids: tuple[str, ...] = (),
              tracker_stale: bool = False) -> WakePacket:
    text = text[:PACKET_BUDGET]
    return WakePacket(
        packet_id=stable_id("dream-cycle-v3-wake", profile, built_at,
                            sha256_hex(text)),
        content_hash="sha256:" + sha256_hex(text),
        profile=profile,
        built_at=built_at,
        project_id=project_id,
        project_method=method,
        text=text,
        degraded=degraded,
        thread_ids=thread_ids,
        tracker_stale=tracker_stale,
    )


def _confined_home(confine_root: Path, home: Path | None, what: str
                   ) -> tuple[Path | None, str | None]:
    """Drop an optional read home that is not confined below the profile
    root (symlinked anchors included) — fail closed with an honest notice."""
    if home is None:
        return None, None
    try:
        assert_confined_read_target(confine_root, home, what=what)
    except DreamCycleError as exc:
        logger.warning("wake: refusing %s: %s", what, exc)
        return None, f"{what} not confined to profile root; content withheld"
    return home, None


def build_wake_packet(*, store_path: Path | str,
                      projects_home: Path | str | None,
                      kanban_root: Path | str | None,
                      inputs: WakeInputs,
                      todoist_export_path: Path | str | None = None,
                      skills_home: Path | str | None = None,
                      confine_root: Path | str | None = None
                      ) -> WakePacket | None:
    """Build one bounded wake packet. Returns None when no owned continuity
    store exists (nothing to say); returns a neutral degraded packet on any
    corrupt store, out-of-root store, privacy hit, or unexpected failure.
    Never raises, never writes.

    When *confine_root* is given, the store, project-docs home, and skills
    home must all live below it with no symlink crossing — a cross-profile
    symlink (file or anchor directory) is refused before any byte of
    another profile's data is read. *kanban_root* is the shared Hermes root
    (task SSOT is shared across profiles by design) and is deliberately not
    confined.
    """
    store_path = Path(store_path)
    built_at = inputs.now
    projects_home = Path(projects_home) if projects_home else None
    skills_home = Path(skills_home) if skills_home else None
    confinement_notices: list[str] = []
    try:
        if confine_root is not None:
            root = Path(confine_root)
            assert_store_confined(store_path, root)
            projects_home, notice = _confined_home(root, projects_home,
                                                   "project docs home")
            if notice:
                confinement_notices.append(notice)
            skills_home, notice = _confined_home(root, skills_home,
                                                 "skills home")
            if notice:
                confinement_notices.append(notice)
        identity = inspect_store_identity(store_path)
    except DreamCycleError as exc:
        logger.warning("wake: refusing continuity store %s: %s", store_path, exc)
        return _finalize(profile=inputs.profile, built_at=built_at,
                         text=_neutral_text(inputs.profile, built_at,
                                            "store not owned/readable"),
                         project_id=None, method="abstain_store", degraded=True)
    if identity == "fresh":
        return None

    try:
        with ContinuityStore(store_path, read_only=True) as store:
            now = parse_iso_datetime(inputs.now)
            registry = load_registry(store)
            decision = resolve_project_activation(
                registry=registry,
                evidence=ActivationEvidence(
                    message=inputs.first_message or "",
                    workspace_path=inputs.workspace_path,
                    session_project_id=inputs.session_project_id),
                now=now,
                kanban_root=kanban_root)
            kanban_root_path = Path(kanban_root) if kanban_root else None
            todoist_path = (Path(todoist_export_path)
                            if todoist_export_path else None)
            todoist_home = Path(confine_root) if confine_root else None
            if decision.project_id is not None:
                # Project lane: only the activated project's threads.
                snapshots, refresh = collect_project_threads(
                    store, now=now, owner=inputs.owner,
                    project_id=decision.project_id, limit=THREAD_LIMIT,
                    kanban_root=kanban_root_path,
                    todoist_export_path=todoist_path,
                    todoist_confine_home=todoist_home,
                    candidate_limit=PROJECT_LANE_CANDIDATES)
            else:
                # Global lane: tiny, due-threads-only window.
                snapshots, refresh = collect_project_threads(
                    store, now=now, owner=inputs.owner, due_only=True,
                    limit=THREAD_LIMIT, kanban_root=kanban_root_path,
                    todoist_export_path=todoist_path,
                    todoist_confine_home=todoist_home,
                    candidate_limit=GLOBAL_DUE_LANE_CANDIDATES)
            sensitive_project_ids = {
                p["project_id"] for p in registry
                if p.get("sensitivity_policy") != "normal"
            }
            # A project's task titles, refs, and next actions are project
            # details. Sensitive projects are lookup-only in every lane,
            # including the global due-thread fallback.
            visible_snapshots = [s for s in snapshots
                                 if s.row["project_id"] not in sensitive_project_ids]
            withheld_threads = len(visible_snapshots) != len(snapshots)
            snapshots = visible_snapshots
            thread_lines = [_thread_line(s) for s in snapshots]
    except DreamCycleError as exc:
        logger.warning("wake: continuity store read failed: %s", exc)
        return _finalize(profile=inputs.profile, built_at=built_at,
                         text=_neutral_text(inputs.profile, built_at,
                                            "store read failed"),
                         project_id=None, method="abstain_store", degraded=True)
    except Exception:
        logger.exception("wake: unexpected failure building packet")
        return _finalize(profile=inputs.profile, built_at=built_at,
                         text=_neutral_text(inputs.profile, built_at,
                                            "internal error"),
                         project_id=None, method="abstain_error", degraded=True)

    header = (f"[Continuity wake packet — profile "
              f"{sanitize_text(inputs.profile, 40)}, built {built_at}]")
    notice_bits = confinement_notices + list(refresh.warnings)
    if refresh.outage and not refresh.warnings:
        notice_bits.append("task tracker partially unavailable; thread "
                           "status may be stale")

    excerpt = ""
    skill_excerpt_line = ""
    project_parts: list[str] = []
    if decision.project_id is not None:
        active = next((p for p in registry
                       if p["project_id"] == decision.project_id), None)
        name = active["canonical_name"] if active else decision.project_id
        project_parts.append(f"Active project: {sanitize_text(name, 80)} "
                             f"({sanitize_identifier(decision.project_id)}) "
                             f"[{decision.method}]")
        if active is not None and active.get("sensitivity_policy") != "normal":
            # Sensitive/legal/medical/financial/credentials projects never get
            # auto-injected context (design §11 privacy compliance);
            # details stay behind the explicit lookup handle.
            project_parts.append(
                "Project details withheld (sensitivity policy); "
                "use continuity_lookup(project=...).")
        elif active is not None:
            context = load_project_context(active,
                                           projects_home=projects_home,
                                           skills_home=skills_home)
            if context.skill.loaded:
                skill_excerpt_line = (
                    f"Project context (skill "
                    f"{sanitize_identifier(context.skill.skill_id)}): "
                    f"{context.skill.excerpt}")
                project_parts.append(skill_excerpt_line)
            elif context.skill.state != "unconfigured":
                notice_bits.append(context.skill.warning
                                   or "project context skill unavailable")
            excerpt = context.map_excerpt
            if excerpt:
                project_parts.append(excerpt)
    else:
        project_parts.append(
            "No project auto-activated "
            f"({decision.method.replace('abstain_', 'reason: ')}).")

    notice = ("; ".join(sanitize_text(n, 160) for n in notice_bits[:3])
              )[:NOTICE_BUDGET]

    parts = [header]
    if notice:
        parts.append(f"Warning: {notice}.")
    if thread_lines:
        parts.append("Open threads (owned, max 3):")
        parts.extend(line.text for line in thread_lines)
    elif withheld_threads:
        parts.append("Open thread details withheld (sensitivity policy); "
                     "use continuity_lookup(project=...).")
    elif decision.project_id is not None:
        parts.append("No open project threads.")
    else:
        parts.append("No due owned threads.")
    parts.extend(project_parts)
    parts.append(_HANDLES_LINE)

    text = "\n".join(parts)
    # Combined-cap trim order: map excerpt, then skill excerpt, then threads.
    if len(text) > PACKET_BUDGET and excerpt:
        text = text.replace("\n" + excerpt, "", 1)
    if len(text) > PACKET_BUDGET and skill_excerpt_line:
        text = text.replace("\n" + skill_excerpt_line, "", 1)
    while len(text) > PACKET_BUDGET and thread_lines:
        text = text.replace("\n" + thread_lines[-1].text, "", 1)
        thread_lines = thread_lines[:-1]

    if scan_content(text) or has_raw_pii(text) or WITHHELD in text:
        # Over-exclusion is the intended failure direction: one secret- or
        # PII-shaped string anywhere (caught by the whole-text scan OR
        # already withheld field-by-field) suppresses everything but the
        # neutral notice.
        logger.warning("wake: secret pattern detected in packet; degrading "
                       "to neutral packet")
        return _finalize(profile=inputs.profile, built_at=built_at,
                         text=_neutral_text(inputs.profile, built_at,
                                            "content withheld (privacy)"),
                         project_id=None, method="abstain_privacy",
                         degraded=True)

    return _finalize(
        profile=inputs.profile, built_at=built_at, text=text,
        project_id=decision.project_id, method=decision.method,
        degraded=False,
        thread_ids=tuple(line.thread_id for line in thread_lines),
        tracker_stale=refresh.outage or any(line.stale for line in thread_lines),
    )
