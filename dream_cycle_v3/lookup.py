"""continuity_lookup core: bounded, typed, read-only retrieval (Phase 3).

A retrieval index, not a second knowledge corpus (design §8):
- project  -> registry metadata, bounded map excerpt, bounded project-context
  skill excerpt, linked open thread refs WITH tracker provenance
  (live/snapshot/stale + collector age — identical to the wake path via the
  shared broker), promoted durable decisions. Never raw tasks or the full
  backlog.
- thread_id -> current tracker status, owner, next action, bounded
  disposition history. Never transcript bodies.
- query    -> registry/ledger search with match confidence and source dates.

Project selection, thread collection/refresh, and context (map + skill)
loading all go through dream_cycle_v3.broker — the same canonical path the
wake packet uses, so a project resolves and reports identically on both
surfaces.

Privacy and bounds: every emitted string — including identifiers, task
refs, providers, locators, statuses, headings, and dates — passes the
fail-closed sanitizer (secretguard scan, email/phone redaction, per-field
caps); a final recursive sweep backstops any field missed, and the whole
serialized result is hard-capped at MAX_RESULT_CHARS. Typed error messages
never embed filesystem paths. Session-derived evidence stays metadata-only
— no excerpt fields are ever read from evidence refs.

Freshness is reported truthfully: thread status is refreshed read-only
against the task SSOT when tracker roots are provided; otherwise it is
labelled as stored continuity state with the collector snapshot age.

Errors are typed (LookupUnavailable / LookupBadRequest) so callers can fail
closed without parsing prose.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .broker import collect_project_threads, load_project_context, \
    load_registry, resolve_project_reference
from .errors import DreamCycleError
from .project_docs import assert_confined_read_target
from .sanitize import (cap_serialized, sanitize_date, sanitize_enum,
                       sanitize_identifier, sanitize_payload, sanitize_text)
from .store import (ContinuityStore, assert_store_confined,
                    inspect_store_identity)
from .tracker_refresh import refresh_refs

LOOKUP_SCHEMA_VERSION = 1
MAX_QUERY_RESULTS = 8
MAX_LINKED_THREADS = 10
MAX_DECISIONS = 5
MAX_DISPOSITIONS = 5
_FIELD_CHARS = 300
# Hard cap on the final serialized result. Count limits alone do not bound
# the output when a stored field is corrupt/oversized.
MAX_RESULT_CHARS = 8000

# Store CHECK-constraint enums (dream_cycle_v3/store.py schema): anything
# else in these columns is corruption and renders as "[invalid]".
_THREAD_STATES = frozenset({"observed", "triaged", "queued", "active",
                            "blocked", "waiting", "done", "dismissed",
                            "stale"})
_PROJECT_STATUSES = frozenset({"active", "dormant", "archived"})
_LINK_DISPOSITIONS = frozenset({"linked", "needs_link", "not_actionable",
                                "ephemeral", "quarantined"})


class LookupUnavailable(DreamCycleError):
    """No owned continuity store is readable."""


class LookupBadRequest(DreamCycleError):
    """Malformed arguments (wrong selector count, bad types)."""


def _clean(value: object, limit: int = _FIELD_CHARS) -> str:
    return sanitize_text(value, limit)


def _ident(value: object, limit: int = 120) -> str:
    return sanitize_identifier(value, limit)


def _json_list(raw: object) -> list:
    if not isinstance(raw, str):
        return []
    try:
        parsed = json.loads(raw)
    except ValueError:
        return []
    return parsed if isinstance(parsed, list) else []


def _open_store(store_path: Path | str,
                confine_root: Path | str | None = None) -> ContinuityStore:
    # Typed error messages are deliberately path-free: they are returned to
    # the model by the tool wrapper, and a raw store path is profile
    # topology the model has no business seeing. Details go to the caller's
    # logs via exception chaining.
    store_path = Path(store_path)
    try:
        if confine_root is not None:
            assert_store_confined(store_path, Path(confine_root))
        identity = inspect_store_identity(store_path)
    except DreamCycleError as exc:
        raise LookupUnavailable(
            "continuity store refused (not owned by this profile or not "
            "readable)") from exc
    if identity == "fresh":
        raise LookupUnavailable("no continuity store for this profile")
    try:
        return ContinuityStore(store_path, read_only=True)
    except DreamCycleError as exc:
        raise LookupUnavailable("continuity store unreadable") from exc


def _confined_or_none(confine_root: Path | str | None,
                      home: Path | str | None, what: str) -> Path | None:
    """An optional read home outside the profile root (or reached through a
    symlinked anchor) is dropped — content withheld, never read."""
    if home is None:
        return None
    home = Path(home)
    if confine_root is None:
        return home
    try:
        assert_confined_read_target(Path(confine_root), home, what=what)
    except DreamCycleError:
        return None
    return home


def _tracker_freshness(store: ContinuityStore, ref: str | None, *,
                       kanban_root: Path | None,
                       todoist_export_path: Path | None,
                       todoist_confine_home: Path | None = None
                       ) -> dict[str, Any]:
    """Truthful per-thread tracker status: refreshed when a read-only source
    is reachable, otherwise explicitly stored/stale with collector age."""
    if not ref:
        return {"tracker_state": None, "status_source": "no_task_ref"}
    now = datetime.now(timezone.utc)
    result = refresh_refs([ref], kanban_root=kanban_root,
                          todoist_export_path=todoist_export_path,
                          todoist_confine_home=todoist_confine_home,
                          store=store, now=now)
    state = result.states.get(ref)
    payload: dict[str, Any] = {
        "tracker_state": state.state if state else "stale",
        "status_source": {"live": "tracker_live",
                          "snapshot": "collector_snapshot",
                          "none": "stored_continuity"}.get(
                              state.source if state else "none",
                              "stored_continuity"),
    }
    if state and state.age_days is not None:
        payload["collector_age_days"] = round(state.age_days, 1)
    if result.warnings:
        payload["freshness_warning"] = _clean("; ".join(result.warnings), 200)
    return payload


def _project_payload(store: ContinuityStore, projects_home: Path | None,
                     skills_home: Path | None, project_id: str, *,
                     kanban_root: Path | None = None,
                     todoist_export_path: Path | None = None,
                     todoist_confine_home: Path | None = None
                     ) -> dict[str, Any]:
    registry = load_registry(store)
    record, reason = resolve_project_reference(registry, project_id)
    if record is None:
        return {"found": False, "project_id": _clean(project_id, 120),
                "reason": reason}
    pid = record["project_id"]
    row = store.get_project(pid)
    now = datetime.now(timezone.utc)
    snapshots, _refresh = collect_project_threads(
        store, now=now, project_id=pid, limit=MAX_LINKED_THREADS,
        kanban_root=kanban_root, todoist_export_path=todoist_export_path,
        todoist_confine_home=todoist_confine_home,
        candidate_limit=MAX_LINKED_THREADS * 2)
    threads = []
    for snap in snapshots:
        t = snap.row
        entry = {
            "thread_id": _ident(t["thread_id"]),
            "title": _clean(t["title"], 120),
            "state": sanitize_enum(t["state"], _THREAD_STATES),
            "task_ref": _ident(t["external_task_ref"]) or None,
            "as_of": sanitize_date(t["last_disposition_date"]) or None,
            # Same tracker provenance the wake path reports.
            "tracker_state": snap.tracker_state,
            "status_source": snap.status_source,
        }
        if snap.age_days is not None:
            entry["collector_age_days"] = round(snap.age_days, 1)
        threads.append(entry)
    decisions = [
        {"claim": _clean(c["normalized_claim"]),
         "subject": _clean(c["canonical_subject"], 120),
         "confidence": (float(c["confidence"])
                        if isinstance(c["confidence"], (int, float)) else 0.0),
         "as_of": sanitize_date(str(c["created_at"])[:10])}
        for c in store._conn.execute(
            "SELECT * FROM candidates WHERE project_id = ? AND "
            "class = 'decision_record' AND status = 'promoted' AND "
            "sensitivity_class = 'normal' "
            "ORDER BY created_at DESC LIMIT ?", (pid, MAX_DECISIONS))
    ]
    context = load_project_context(record, projects_home=projects_home,
                                   skills_home=skills_home)
    skill_payload: dict[str, Any] = {"state": _ident(context.skill.state, 40)}
    if context.skill.skill_id:
        skill_payload["skill_id"] = _ident(context.skill.skill_id)
    if context.skill.loaded:
        skill_payload["excerpt"] = context.skill.excerpt
    elif context.skill.warning:
        skill_payload["warning"] = _clean(context.skill.warning, 160)
    return {
        "found": True,
        "project_id": _ident(pid),
        "canonical_name": _clean(row["canonical_name"], 120),
        "status": sanitize_enum(row["status"], _PROJECT_STATUSES),
        "aliases": [_clean(a, 60) for a in _json_list(row["aliases"])][:8],
        "task_ssot": {"provider": _ident(row["task_provider"], 40) or None,
                      "locator": _ident(row["task_locator"], 120) or None},
        "context_skill_id": _ident(row["context_skill_id"]) or None,
        "context_skill": skill_payload,
        "last_verified_at": sanitize_date(row["last_verified_at"]) or None,
        "map_excerpt": context.map_excerpt,
        "open_threads": threads,
        "durable_decisions": decisions,
    }


def _thread_payload(store: ContinuityStore, thread_id: str, *,
                    kanban_root: Path | None = None,
                    todoist_export_path: Path | None = None,
                    todoist_confine_home: Path | None = None
                    ) -> dict[str, Any]:
    row = store.get_thread(thread_id)
    if row is None:
        return {"found": False, "thread_id": _clean(thread_id, 120),
                "reason": "unknown_thread"}
    history = [
        {"date": sanitize_date(d["disposition_date"]),
         "action": _ident(d["action"], 60),
         "reason": _clean(d["reason"], 160),
         "state_after": sanitize_enum(d["state_after"], _THREAD_STATES)}
        for d in store._conn.execute(
            "SELECT * FROM thread_dispositions WHERE thread_id = ? "
            "ORDER BY disposition_date DESC LIMIT ?",
            (thread_id, MAX_DISPOSITIONS))
    ]
    freshness = _tracker_freshness(
        store, row["external_task_ref"],
        kanban_root=kanban_root,
        todoist_export_path=todoist_export_path,
        todoist_confine_home=todoist_confine_home)
    return {
        "found": True,
        "thread_id": _ident(row["thread_id"]),
        "title": _clean(row["title"], 120),
        "state": sanitize_enum(row["state"], _THREAD_STATES),
        "owner": _ident(row["owner"], 60),
        "project_id": _ident(row["project_id"]) or None,
        "next_action": _clean(row["normalized_next_action"], 200),
        "task_ref": _ident(row["external_task_ref"]) or None,
        "link_disposition": sanitize_enum(row["link_disposition"],
                                          _LINK_DISPOSITIONS),
        "blocked_by": _clean(row["blocked_by"], 120) or None,
        "follow_up_after": sanitize_date(row["follow_up_after"]) or None,
        "as_of": sanitize_date(row["last_disposition_date"]) or None,
        "updated_at": sanitize_date(row["updated_at"]) or None,
        "dispositions": history,
        **freshness,
    }


def _query_payload(store: ContinuityStore, query: str) -> dict[str, Any]:
    needle = " ".join(query.lower().split())
    if not needle:
        raise LookupBadRequest("query must be a non-empty string")
    word = re.compile(rf"(?<![\w-]){re.escape(needle)}(?![\w-])")
    results: list[dict[str, Any]] = []

    for row in store._conn.execute("SELECT * FROM projects ORDER BY project_id"):
        name = row["canonical_name"].lower()
        aliases = [a.lower() for a in _json_list(row["aliases"])]
        terms = [t.lower() for t in _json_list(row["retrieval_terms"])]
        if needle == row["project_id"].lower() or needle in aliases:
            confidence = 0.9
        elif word.search(name):
            confidence = 0.7
        elif needle in terms:
            confidence = 0.6
        elif needle in name:
            confidence = 0.5
        else:
            continue
        results.append({"kind": "project", "id": _ident(row["project_id"]),
                        "label": _clean(row["canonical_name"], 120),
                        "confidence": confidence,
                        "as_of": sanitize_date(
                            str(row["last_verified_at"])[:10])})

    for row in store.select_nonterminal_threads():
        haystack = f"{row['title']} {row['normalized_next_action']}".lower()
        if needle in haystack:
            results.append({"kind": "thread", "id": _ident(row["thread_id"]),
                            "label": _clean(row["title"], 120),
                            "state": sanitize_enum(row["state"],
                                                   _THREAD_STATES),
                            "confidence": 0.6,
                            "as_of": sanitize_date(
                                row["last_disposition_date"]) or None})

    for row in store._conn.execute(
            "SELECT * FROM candidates WHERE status = 'promoted' AND "
            "sensitivity_class = 'normal' "
            "ORDER BY created_at DESC LIMIT 200"):
        haystack = " ".join([row["canonical_subject"], row["normalized_claim"],
                             row["retrieval_terms"]]).lower()
        if needle in haystack:
            try:
                confidence = min(0.8, float(row["confidence"]))
            except (TypeError, ValueError):
                confidence = 0.0
            results.append({"kind": "promoted_fact",
                            "id": _ident(row["candidate_id"]),
                            "label": _clean(row["normalized_claim"], 160),
                            "destination": _ident(row["destination"], 80),
                            "confidence": confidence,
                            "as_of": sanitize_date(
                                str(row["created_at"])[:10])})

    results.sort(key=lambda r: (-r["confidence"], r["kind"], r["id"]))
    return {"query": _clean(query, 120), "results": results[:MAX_QUERY_RESULTS],
            "truncated": len(results) > MAX_QUERY_RESULTS}


def continuity_lookup(*, store_path: Path | str,
                      projects_home: Path | str | None = None,
                      skills_home: Path | str | None = None,
                      project: str | None = None,
                      thread_id: str | None = None,
                      query: str | None = None,
                      kanban_root: Path | str | None = None,
                      todoist_export_path: Path | str | None = None,
                      confine_root: Path | str | None = None) -> dict[str, Any]:
    """Exactly one selector. Raises LookupBadRequest / LookupUnavailable;
    otherwise returns a bounded, typed, JSON-serializable dict whose
    serialized size never exceeds MAX_RESULT_CHARS."""
    selectors = [s for s in (project, thread_id, query) if s]
    if len(selectors) != 1:
        raise LookupBadRequest(
            "continuity_lookup requires exactly one of project, thread_id, "
            "query")
    for name, value in (("project", project), ("thread_id", thread_id),
                        ("query", query)):
        if value is not None and (not isinstance(value, str)
                                  or len(value) > 200):
            raise LookupBadRequest(f"{name} must be a string of <=200 chars")

    projects_home = _confined_or_none(confine_root, projects_home,
                                      "project docs home")
    skills_home = _confined_or_none(confine_root, skills_home, "skills home")
    kanban_root_path = Path(kanban_root) if kanban_root else None
    todoist_path = Path(todoist_export_path) if todoist_export_path else None
    todoist_home = Path(confine_root) if confine_root else None

    with _open_store(store_path, confine_root) as store:
        if project:
            body = _project_payload(store, projects_home, skills_home,
                                    project, kanban_root=kanban_root_path,
                                    todoist_export_path=todoist_path,
                                    todoist_confine_home=todoist_home)
            kind = "project"
        elif thread_id:
            body = _thread_payload(
                store, thread_id,
                kanban_root=kanban_root_path,
                todoist_export_path=todoist_path,
                todoist_confine_home=todoist_home)
            kind = "thread"
        else:
            body = _query_payload(store, query or "")
            kind = "query"
    payload = {"schema_version": LOOKUP_SCHEMA_VERSION, "kind": kind, **body}
    # Recursive backstop sweep (any field a future edit forgets to sanitize
    # explicitly still cannot emit a raw string), then the hard size cap.
    payload = sanitize_payload(payload, _FIELD_CHARS)
    payload["schema_version"] = LOOKUP_SCHEMA_VERSION
    payload["kind"] = kind
    return cap_serialized(payload, MAX_RESULT_CHARS)
