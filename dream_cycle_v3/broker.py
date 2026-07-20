"""Canonical continuity broker shared by wake and continuity_lookup (Phase 3).

One home for the read-path logic both surfaces must agree on:

- registry loading (stored project rows -> typed registry dicts, including
  ``context_skill_id``);
- registry freshness validation (a stale/inactive record abstains at every
  activation tier);
- project activation precedence over typed evidence (task ref / explicit
  project id / workspace path / message / session binding), with terminal
  abstention on any collision, staleness, or task-authority conflict;
- explicit project reference resolution for retrieval (id, unique canonical
  name, or unique alias — same matching primitives as activation);
- bounded thread candidate collection: a SQL-bounded candidate set is
  selected FIRST (per owner/project lane), only those candidates' task refs
  are refreshed against the task SSOT, and every returned thread carries
  the same tracker provenance (live/snapshot/stale + collector age) in wake
  and lookup;
- project context assembly (confined map/context doc excerpt + confined
  context-skill excerpt) under explicit per-component budgets.

Everything here is read-only and fail-closed, like the rest of the Phase 3
read paths. Callers pass explicit roots; nothing resolves live profile
paths on its own.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .adapters.kanban import read_kanban_task_project
from .context_skill import SKILL_EXCERPT_BUDGET, SkillLoad, load_context_skill
from .contracts import parse_iso_datetime
from .errors import DreamCycleError
from .kanban_layout import kanban_db_path
from .project_docs import read_project_doc_sections
from .routing import TASK_REF_RE, _ref_matches_project
from .sanitize import sanitize_text
from .tracker_refresh import RefreshResult, refresh_refs

# A registry record older than this is stale evidence at EVERY activation
# tier (design §8: "stale registry record ... is an abstention").
REGISTRY_STALE_AFTER_DAYS = 30
# Bounded candidate window queried per lane BEFORE any tracker refresh —
# thread selection never loads or refreshes the full backlog.
THREAD_CANDIDATE_LIMIT = 12
MAP_EXCERPT_BUDGET = 900

_WORD_BOUNDARY = r"(?<![\w-]){token}(?![\w-])"
# Nonterminal states minus 'stale' (mirrors store.TERMINAL_THREAD_STATES
# plus the wake rule that stale threads never surface).
_SELECTABLE_STATES = ("observed", "triaged", "queued", "active", "blocked",
                      "waiting")


def _parse_json_column(raw: object) -> list:
    import json
    if not isinstance(raw, str):
        return []
    try:
        value = json.loads(raw)
    except ValueError:
        return []
    return value if isinstance(value, list) else []


def project_rows_to_registry(rows: list[Any]) -> list[dict[str, Any]]:
    """Reshape stored project rows into the dict shape routing/broker expect."""
    registry = []
    for row in rows:
        registry.append({
            "project_id": row["project_id"],
            "canonical_name": row["canonical_name"],
            "sensitivity_policy": row["sensitivity_policy"],
            "aliases": _parse_json_column(row["aliases"]),
            "canonical_paths": _parse_json_column(row["canonical_paths"]),
            "repositories": _parse_json_column(row["repositories"]),
            "status": row["status"],
            "owner": _row_get(row, "owner"),
            "last_verified_at": row["last_verified_at"],
            "context_skill_id": _row_get(row, "context_skill_id"),
            "task_ssot": {"provider": row["task_provider"],
                          "locator": row["task_locator"]},
        })
    return registry


def _row_get(row: Any, key: str):
    try:
        return row[key]
    except (KeyError, IndexError):
        return None


def load_registry(store) -> list[dict[str, Any]]:
    rows = list(store._conn.execute(
        "SELECT * FROM projects ORDER BY project_id"))
    return project_rows_to_registry(rows)


def _as_utc(dt: datetime) -> datetime:
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def registry_record_fresh(project: dict[str, Any], now: datetime) -> bool:
    if project["status"] != "active":
        return False
    try:
        verified = _as_utc(parse_iso_datetime(project["last_verified_at"]))
    except (ValueError, TypeError):
        return False
    return _as_utc(now) - verified <= timedelta(days=REGISTRY_STALE_AFTER_DAYS)


# ---------------------------------------------------------------------------
# Activation (wake) and reference resolution (lookup)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ActivationDecision:
    project_id: str | None
    method: str        # explicit_ref | workspace_path | session_binding |
                       # alias | abstain_ambiguous | abstain_stale |
                       # abstain_conflict | abstain_no_evidence


@dataclass(frozen=True)
class ActivationEvidence:
    """Typed session evidence for project activation. Absent fields can
    never activate a project."""

    message: str = ""
    workspace_path: str | None = None
    session_project_id: str | None = None


def _kanban_ref_evidence(ref: str, registry: list[dict[str, Any]],
                         kanban_root: Path | None
                         ) -> tuple[dict[str, str], bool]:
    """Canonical task->project evidence for one kanban ref.

    Returns (matches, conflict). The task itself is the authority: read its
    project_id from the board (read-only). A task whose project_id names a
    registry project matches exactly that project; a project_id ABSENT from
    the registry is contradictory authority — conflict=True, and the caller
    must terminally abstain rather than fall through to weaker tiers. A task
    with no project_id proves only membership of its board, so board-locator
    matching applies. An unreadable board or missing task proves nothing —
    no match (never provider/board inference for a task we could not find).
    """
    parts = ref.split(":", 2)
    if len(parts) != 3 or not parts[1] or not parts[2]:
        return {}, False
    board, item_id = parts[1], parts[2]
    if kanban_root is None:
        return {}, False
    db_path = kanban_db_path(kanban_root, board)
    if db_path is None:
        return {}, False
    status, task_project_id = read_kanban_task_project(db_path, item_id)
    if status != "ok":
        return {}, False
    if task_project_id:
        matches = {p["project_id"]: ref for p in registry
                   if p["project_id"] == task_project_id}
        if not matches:
            # The task's canonical project is not in the registry at all:
            # explicit authority contradicts the registry. Terminal.
            return {}, True
        return matches, False
    # Task exists on the board but carries no canonical project: the
    # registry's board binding may speak, now that membership is proven.
    return ({p["project_id"]: ref for p in registry
             if _ref_matches_project(ref, p)}, False)


def resolve_project_activation(*, registry: list[dict[str, Any]],
                               evidence: ActivationEvidence,
                               now: datetime,
                               kanban_root: Path | str | None = None
                               ) -> ActivationDecision:
    """Design §8 precedence. A collision, stale record, or task-authority
    conflict at any tier is a terminal abstention — contradictory evidence
    never falls through to a weaker tier. Zero matches at a tier falls
    through. Every tier's winning record must be active and freshly
    verified."""
    message = evidence.message or ""
    root = Path(kanban_root) if kanban_root else None

    def _decide(matches: dict[str, str], method: str
                ) -> ActivationDecision | None:
        if len(matches) > 1:
            return ActivationDecision(None, "abstain_ambiguous")
        if len(matches) == 1:
            (pid,) = matches
            record = next((p for p in registry if p["project_id"] == pid), None)
            if record is None or not registry_record_fresh(record, now):
                return ActivationDecision(None, "abstain_stale")
            return ActivationDecision(pid, method)
        return None

    # Tier 1: explicit task reference or explicit project id in the message.
    matches: dict[str, str] = {}
    for m in TASK_REF_RE.finditer(message):
        ref = m.group(0)
        scheme = ref.partition(":")[0]
        if scheme == "kanban":
            ref_matches, conflict = _kanban_ref_evidence(ref, registry, root)
            if conflict:
                return ActivationDecision(None, "abstain_conflict")
            matches.update(ref_matches)
        elif scheme == "github":
            # The ref names its repo: canonical membership evidence.
            for project in registry:
                if _ref_matches_project(ref, project):
                    matches.setdefault(project["project_id"], ref)
        # todoist ids carry no namespace — provider-level matching alone can
        # never prove which project a task belongs to, so it is not evidence.
    for project in registry:
        pid = project["project_id"]
        if re.search(_WORD_BOUNDARY.format(token=re.escape(pid)), message,
                     re.IGNORECASE):
            matches.setdefault(pid, pid)
    decision = _decide(matches, "explicit_ref")
    if decision is not None:
        return decision

    # Tier 2: longest-prefix workspace path match against canonical paths.
    if evidence.workspace_path:
        ws = str(Path(evidence.workspace_path).expanduser())
        best_len = 0
        best: dict[str, str] = {}
        for project in registry:
            for prefix in project.get("canonical_paths", []):
                p = str(Path(prefix).expanduser()) if prefix.startswith(("~", "/")) else prefix
                if ws == p or ws.startswith(p.rstrip("/") + "/"):
                    if len(p) > best_len:
                        best_len, best = len(p), {project["project_id"]: p}
                    elif len(p) == best_len:
                        best.setdefault(project["project_id"], p)
        decision = _decide(best, "workspace_path")
        if decision is not None:
            return decision

    # Tier 3: existing session binding, only against a fresh active record.
    if evidence.session_project_id:
        bound = [p for p in registry
                 if p["project_id"] == evidence.session_project_id]
        if not bound:
            return ActivationDecision(None, "abstain_stale")
        if not registry_record_fresh(bound[0], now):
            return ActivationDecision(None, "abstain_stale")
        return ActivationDecision(bound[0]["project_id"], "session_binding")

    # Tier 4: unique exact alias (whole word) in the first user message.
    alias_matches: dict[str, str] = {}
    for project in registry:
        for alias in project.get("aliases", []):
            if re.search(_WORD_BOUNDARY.format(token=re.escape(alias)),
                         message, re.IGNORECASE):
                alias_matches.setdefault(project["project_id"], alias)
    decision = _decide(alias_matches, "alias")
    if decision is not None:
        return decision

    return ActivationDecision(None, "abstain_no_evidence")


def resolve_project_reference(registry: list[dict[str, Any]], token: str
                              ) -> tuple[dict[str, Any] | None, str]:
    """Explicit project reference for retrieval: exact id, else unique
    canonical name or alias (case-insensitive). Returns (record, reason)
    where reason is 'ok' | 'unknown_project' | 'ambiguous_project'.

    Retrieval deliberately has no freshness gate — an archived project is
    still explicitly retrievable, with its status reported truthfully —
    but the matching primitives are the same ones activation uses.
    """
    for project in registry:
        if project["project_id"] == token:
            return project, "ok"
    needle = token.lower()
    hits = [p for p in registry
            if p["canonical_name"].lower() == needle
            or needle in [a.lower() for a in p.get("aliases", [])]]
    if len(hits) == 1:
        return hits[0], "ok"
    return None, ("ambiguous_project" if len(hits) > 1 else "unknown_project")


# ---------------------------------------------------------------------------
# Bounded thread collection with tracker provenance
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ThreadSnapshot:
    """One selected thread plus its tracker provenance — identical fields
    feed the wake packet lines and the lookup thread payloads."""

    row: Any                       # sqlite3.Row from the threads table
    tracker_state: str | None      # open | closed | stale | None (no ref)
    status_source: str             # tracker_live | collector_snapshot |
                                   # stored_continuity | no_task_ref
    age_days: float | None
    stale: bool
    gated: bool                    # last disposition was authority_gated


_SOURCE_LABEL = {"live": "tracker_live", "snapshot": "collector_snapshot",
                 "none": "stored_continuity"}


def _due_at(thread: Any) -> datetime | None:
    due_raw = thread["follow_up_after"] or thread["due_hint"]
    if not due_raw:
        return None
    try:
        return _as_utc(parse_iso_datetime(
            due_raw if "T" in due_raw else due_raw + "T00:00:00+00:00"))
    except (ValueError, TypeError):
        return None


# Sort fallback for an unparseable updated_at: older than everything real.
_DATETIME_FLOOR = datetime.min.replace(tzinfo=timezone.utc)


def _updated_at_utc(row: Any) -> datetime:
    """updated_at as an instant — contracts accept any valid ISO offset, so
    string comparison is not chronological across offsets."""
    try:
        return _as_utc(parse_iso_datetime(row["updated_at"]))
    except (ValueError, TypeError):
        return _DATETIME_FLOOR


def sort_threads_due_first(rows: list[Any], now: datetime) -> None:
    """In-place contract sort: due rows first (earliest due first), then
    everything else — undated AND future-dated alike — most recently
    updated first; thread_id breaks ties. A future follow-up is not due,
    so it never outranks a recently-updated undated row. All keys compare
    parsed UTC instants, never raw timestamp text (offsets vary)."""
    # Three stable passes, last pass primary.
    rows.sort(key=lambda r: r["thread_id"])
    rows.sort(key=_updated_at_utc, reverse=True)

    def _due_group(r: Any):
        due = _due_at(r)
        is_due = due is not None and due <= _as_utc(now)
        return (0, due) if is_due else (1, _DATETIME_FLOOR)

    rows.sort(key=_due_group)


def _bounded_candidates(store, *, owner: str | None, project_id: str | None,
                        due_only: bool, now: datetime,
                        candidate_limit: int) -> list[Any]:
    """SQL-bounded candidate window, ordered due-first — queried BEFORE any
    tracker refresh so an oversized backlog can never trigger an unbounded
    refresh sweep."""
    placeholders = ",".join("?" for _ in _SELECTABLE_STATES)
    sql = f"SELECT * FROM threads WHERE state IN ({placeholders})"
    params: list[Any] = list(_SELECTABLE_STATES)
    if owner is not None:
        sql += " AND owner = ?"
        params.append(owner)
    if project_id is not None:
        sql += " AND project_id = ?"
        params.append(project_id)
    if due_only:
        sql += " AND COALESCE(follow_up_after, due_hint) IS NOT NULL"
    # Approximate the exact python sort in SQL (due-as-of-now first by due
    # date, then recency); the exact key is re-applied to the bounded window
    # below. Timestamps ride julianday(), never raw text: contracts accept
    # any valid ISO offset, and lexical comparison would misclassify
    # due-ness and misorder recency across offsets BEFORE the LIMIT cuts
    # the window (julianday parses offsets/'Z'/date-only and yields NULL —
    # never-due, recency floor — for malformed values, matching _due_at).
    now_iso = _as_utc(now).isoformat()
    sql += (" ORDER BY CASE WHEN COALESCE(follow_up_after, due_hint)"
            " IS NOT NULL AND julianday(COALESCE(follow_up_after, due_hint))"
            " <= julianday(?) THEN 0 ELSE 1 END,"
            " CASE WHEN COALESCE(follow_up_after, due_hint)"
            " IS NOT NULL AND julianday(COALESCE(follow_up_after, due_hint))"
            " <= julianday(?)"
            " THEN julianday(COALESCE(follow_up_after, due_hint)) END,"
            " julianday(updated_at) DESC, thread_id LIMIT ?")
    params.extend([now_iso, now_iso, int(candidate_limit)])
    rows = list(store._conn.execute(sql, params))
    if due_only:
        rows = [r for r in rows
                if (due := _due_at(r)) is not None and due <= _as_utc(now)]
    sort_threads_due_first(rows, now)
    return rows


def collect_project_threads(store, *, now: datetime,
                            owner: str | None = None,
                            project_id: str | None = None,
                            due_only: bool = False,
                            limit: int,
                            kanban_root: Path | None = None,
                            todoist_export_path: Path | None = None,
                            todoist_confine_home: Path | None = None,
                            candidate_limit: int = THREAD_CANDIDATE_LIMIT
                            ) -> tuple[list[ThreadSnapshot], RefreshResult]:
    """The one thread-selection path wake and lookup share.

    Bounded candidates -> read-only tracker refresh of exactly those
    candidates' refs -> tracker-closed threads dropped (never written
    back) -> top *limit* with per-thread provenance.
    """
    rows = _bounded_candidates(store, owner=owner, project_id=project_id,
                               due_only=due_only, now=now,
                               candidate_limit=candidate_limit)
    refresh = refresh_refs(
        [r["external_task_ref"] for r in rows if r["external_task_ref"]],
        kanban_root=kanban_root,
        todoist_export_path=todoist_export_path,
        todoist_confine_home=todoist_confine_home, store=store, now=now)
    snapshots: list[ThreadSnapshot] = []
    for row in rows:
        ref = row["external_task_ref"]
        if ref and refresh.state_of(ref) == "closed":
            continue
        if ref:
            state = refresh.states.get(ref)
            tracker_state = state.state if state else "stale"
            source = _SOURCE_LABEL.get(state.source if state else "none",
                                       "stored_continuity")
            age_days = state.age_days if state else None
        else:
            tracker_state, source, age_days = None, "no_task_ref", None
        last = store.get_disposition(row["thread_id"],
                                     row["last_disposition_date"])
        snapshots.append(ThreadSnapshot(
            row=row,
            tracker_state=tracker_state,
            status_source=source,
            age_days=age_days,
            stale=bool(ref) and tracker_state == "stale",
            gated=bool(last is not None and last["action"] == "authority_gated"),
        ))
        if len(snapshots) >= limit:
            break
    return snapshots, refresh


# ---------------------------------------------------------------------------
# Project context (confined map/context doc + context skill)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ProjectContext:
    map_excerpt: str
    skill: SkillLoad


def _map_excerpt(projects_home: Path | None, project_id: str,
                 budget: int = MAP_EXCERPT_BUDGET) -> str:
    """Confined read of the project's map/context doc, bounded to *budget*.
    Any read problem (missing doc, symlink escape, bad bytes) yields ''."""
    if projects_home is None:
        return ""
    for doc in ("map", "context"):
        try:
            sections = read_project_doc_sections(projects_home, project_id, doc)
        except DreamCycleError:
            continue
        except (OSError, UnicodeDecodeError, ValueError):
            return ""
        parts: list[str] = []
        used = 0
        for heading, body in sections:
            chunk = (f"{sanitize_text(heading, 80)}: "
                     f"{sanitize_text(body, 200)}")
            if used + len(chunk) + 1 > budget:
                break
            parts.append(chunk)
            used += len(chunk) + 1
        return "\n".join(parts)[:budget]
    return ""


def load_project_context(project: dict[str, Any], *,
                         projects_home: Path | None,
                         skills_home: Path | None,
                         map_budget: int = MAP_EXCERPT_BUDGET,
                         skill_budget: int = SKILL_EXCERPT_BUDGET
                         ) -> ProjectContext:
    """Bounded project context — the same confined map excerpt and context
    skill load feed the wake packet and the project lookup payload."""
    return ProjectContext(
        map_excerpt=_map_excerpt(projects_home, project["project_id"],
                                 map_budget),
        skill=load_context_skill(skills_home, project.get("context_skill_id"),
                                 skill_budget),
    )
