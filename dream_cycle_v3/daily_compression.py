"""Daily continuity compression: one bounded, durable record per profile/date.

Hermes does not reset daily. This module adds the missing layer between the
continuity store and the next wake: a compacted, deterministic record of what
the day's already-collected evidence says is still live — active projects,
unresolved decision/task threads, the day's key state changes, and the source
references that back them.

What it is NOT: a second knowledge corpus, and never a transcript digest.
Every field is derived from rows the collector/carry-forward already wrote
(projects, threads, thread_dispositions, promoted decision candidates,
adapter snapshots, the run's manifest). Session-derived evidence stays
metadata-only — no excerpt is ever read from an evidence ref, so a transcript
body cannot reach the record by any path.

Invariants:
- Deterministic. The record is a pure function of the store rows plus the
  caller's pinned run_id/date/as_of. No wall clock, no ambient discovery, no
  runtime AI (see `DailySummarizer` below — the interface exists, disabled).
- Idempotent and non-destructive. Identity is content-addressed, the table is
  append-only by trigger, and nothing here writes to, rewrites, or deletes any
  source row, manifest, session, skill, or project doc.
- Explicit abstention. A day with no active projects, no unresolved threads
  and no state changes records outcome `no_new_evidence` rather than nothing,
  so "we looked and there was nothing" is durable evidence too.
- Bounded and fail-closed. Per-section caps, the shared sanitizers on every
  emitted string, and a hard cap on the serialized record.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from . import CONTRACT_SCHEMA_VERSION
from .canonical import fingerprint_obj
from .contracts import is_iso_date, is_iso_datetime
from .errors import DreamCycleError
from .sanitize import (cap_serialized, sanitize_date, sanitize_enum,
                       sanitize_identifier, sanitize_payload, sanitize_text)
from .store import (ContinuityStore, assert_store_confined,
                    inspect_store_identity)

DAILY_COMPRESSION_SCHEMA_VERSION = 1
DAILY_COMPRESSION_KIND = "dream-cycle-v3-daily-compression"
# Bump when the derivation changes in a way that should produce a NEW
# revision for the same day rather than silently reusing the old record.
COMPRESSOR_VERSION = "1.0.0-deterministic"

OUTCOME_RECORDED = "recorded"
OUTCOME_NO_NEW_EVIDENCE = "no_new_evidence"

# Section caps. Totals are always exact; only the per-item detail rows are
# capped, so a compacted day never grows with the backlog.
MAX_PROJECTS = 12
MAX_THREADS = 20
MAX_STATE_CHANGES = 20
MAX_DECISIONS = 8
MAX_SOURCE_REFS = 20
TITLE_CHARS = 120
ACTION_CHARS = 200
# Hard cap on the serialized record (same failure direction as the lookup
# path: shrink deterministically, mark truncated, never emit oversized).
MAX_RECORD_CHARS = 12_000

# Bounded one-liner for the wake packet.
WAKE_LINE_BUDGET = 400
WAKE_LINE_PROJECTS = 3

_THREAD_STATES = frozenset({"observed", "triaged", "queued", "active",
                            "blocked", "waiting", "done", "dismissed",
                            "stale"})
_PROJECT_STATUSES = frozenset({"active", "dormant", "archived"})
_ACTIONS = frozenset({"close_done", "close_dismissed", "blocked", "waiting",
                      "continue", "defer", "stale_review", "needs_link",
                      "authority_gated"})
# Unresolved work: everything the carry-forward policy has not put to bed.
_TERMINAL_STATES = ("done", "dismissed")

__all__ = [
    "DAILY_COMPRESSION_SCHEMA_VERSION", "DAILY_COMPRESSION_KIND",
    "COMPRESSOR_VERSION", "OUTCOME_RECORDED", "OUTCOME_NO_NEW_EVIDENCE",
    "LLM_SUMMARY_ENABLED", "DailySummarizer", "DailyCompressionResult",
    "build_daily_compression", "compress_day", "read_daily_compression",
    "wake_compression_line",
]


# ---------------------------------------------------------------------------
# Optional future LLM summarization interface — defined, and disabled.
# ---------------------------------------------------------------------------

class DailySummarizer(Protocol):
    """The one seam an LLM summarizer would ever be allowed to occupy.

    It receives the ALREADY-compacted, already-sanitized deterministic
    sections (never raw sources, never transcripts) and returns one bounded
    prose line. Its output would land in a separate `llm_summary` field
    carrying its own provenance (`classifier_kind: "llm"`, model,
    prompt_hash) — the deterministic sections stay authoritative and are
    never replaced by it.

    Deliberately inert: `LLM_SUMMARY_ENABLED` is False, no runtime config
    plumbs a summarizer, and passing one raises rather than silently doing
    nothing. Enabling it is a separate, explicitly approved change — it
    would make the daily record nondeterministic and add a paid call to the
    cron path, so it must not happen by default.
    """

    def summarize(self, sections: dict[str, Any]) -> str:  # pragma: no cover
        ...


LLM_SUMMARY_ENABLED = False


@dataclass(frozen=True)
class DailyCompressionResult:
    """What one compression pass did. `stored` is "inserted" or "exists" —
    retry-local state, deliberately kept out of the record itself so an
    identical rerun produces identical durable bytes."""

    record: dict[str, Any]
    compression_id: str
    revision: int
    outcome: str
    stored: str


# ---------------------------------------------------------------------------
# Derivation
# ---------------------------------------------------------------------------

def _json_list(raw: object) -> list:
    if not isinstance(raw, str):
        return []
    try:
        parsed = json.loads(raw)
    except ValueError:
        return []
    return parsed if isinstance(parsed, list) else []


def _source_refs_from_thread(row: Any) -> list[dict[str, str]]:
    """Source pointers for one thread — identity and location only.

    `excerpt` is never read: transcript containment holds by construction,
    not by trusting the collector to have suppressed it upstream.
    """
    refs = []
    for ref in _json_list(row["evidence_refs"]):
        if not isinstance(ref, dict):
            continue
        refs.append({
            "source_type": sanitize_identifier(ref.get("source_type"), 40),
            "source_id": sanitize_identifier(ref.get("source_id"), 160),
            "location": sanitize_text(ref.get("location") or "", 160),
            "observed_at": sanitize_date(ref.get("observed_at")),
            "fingerprint": sanitize_identifier(ref.get("fingerprint"), 80),
        })
    return refs


def _active_projects(store: ContinuityStore) -> list[dict[str, Any]]:
    rows = store._conn.execute(
        "SELECT * FROM projects WHERE status = 'active' ORDER BY project_id")
    return [{
        "project_id": sanitize_identifier(r["project_id"]),
        "canonical_name": sanitize_text(r["canonical_name"], TITLE_CHARS),
        "status": sanitize_enum(r["status"], _PROJECT_STATUSES),
        "task_provider": sanitize_identifier(r["task_provider"], 40) or None,
        "task_locator": sanitize_identifier(r["task_locator"], 160) or None,
        "context_skill_id": sanitize_identifier(r["context_skill_id"]) or None,
        "last_verified_at": sanitize_date(r["last_verified_at"]),
    } for r in rows]


def _unresolved_threads(store: ContinuityStore) -> list[dict[str, Any]]:
    placeholders = ",".join("?" for _ in _TERMINAL_STATES)
    rows = store._conn.execute(
        f"SELECT * FROM threads WHERE state NOT IN ({placeholders}) "
        "ORDER BY thread_id", _TERMINAL_STATES)
    return [{
        "thread_id": sanitize_identifier(r["thread_id"]),
        "project_id": sanitize_identifier(r["project_id"]),
        "title": sanitize_text(r["title"], TITLE_CHARS),
        "state": sanitize_enum(r["state"], _THREAD_STATES),
        "next_action": sanitize_text(r["normalized_next_action"],
                                     ACTION_CHARS),
        "task_ref": sanitize_identifier(r["external_task_ref"]) or None,
        "owner": sanitize_identifier(r["owner"], 60),
        "blocked_by": sanitize_text(r["blocked_by"] or "", 160) or None,
        "follow_up_after": sanitize_date(r["follow_up_after"]) or None,
        "as_of": sanitize_date(r["last_disposition_date"]),
        "source_refs": _source_refs_from_thread(r),
    } for r in rows]


def _state_changes(store: ContinuityStore, compression_date: str
                   ) -> list[dict[str, Any]]:
    rows = store._conn.execute(
        "SELECT * FROM thread_dispositions WHERE disposition_date = ? "
        "ORDER BY thread_id", (compression_date,))
    return [{
        "thread_id": sanitize_identifier(r["thread_id"]),
        "action": sanitize_enum(r["action"], _ACTIONS),
        "state_before": sanitize_enum(r["state_before"], _THREAD_STATES),
        "state_after": sanitize_enum(r["state_after"], _THREAD_STATES),
        "reason": sanitize_text(r["reason"], ACTION_CHARS),
    } for r in rows]


def _durable_decisions(store: ContinuityStore) -> list[dict[str, Any]]:
    """Promoted decision records — the durable half of "unresolved decision
    threads": what was actually decided, so the next session does not
    relitigate it. Sensitive-class records stay lookup-only."""
    rows = store._conn.execute(
        "SELECT * FROM candidates WHERE class = 'decision_record' AND "
        "status = 'promoted' AND sensitivity_class = 'normal' "
        "ORDER BY created_at DESC, candidate_id LIMIT ?", (MAX_DECISIONS,))
    return [{
        "candidate_id": sanitize_identifier(r["candidate_id"]),
        "project_id": sanitize_identifier(r["project_id"]) or None,
        "subject": sanitize_text(r["canonical_subject"], TITLE_CHARS),
        "claim": sanitize_text(r["normalized_claim"], ACTION_CHARS),
        "as_of": sanitize_date(str(r["created_at"])[:10]),
    } for r in rows]


def _run_source_refs(store: ContinuityStore, run_id: str) -> list[dict[str, Any]]:
    """The run's own provenance chain: manifest + every adapter snapshot it
    recorded. These are pointers back to preserved raw evidence, never
    copies of it."""
    refs: list[dict[str, Any]] = []
    run = store._conn.execute("SELECT * FROM runs WHERE run_id = ?",
                              (run_id,)).fetchone()
    if run is not None:
        refs.append({
            "kind": "manifest",
            "ref": sanitize_text(run["manifest_path"], 240),
            "fingerprint": sanitize_identifier(run["manifest_fingerprint"], 80),
            "as_of": sanitize_date(run["generated_at"]),
        })
    for row in store.adapter_snapshots_for_run(run_id):
        refs.append({
            "kind": "adapter_snapshot",
            "ref": sanitize_text(
                f"{row['adapter']}:{row['source_locator']}", 240),
            "fingerprint": sanitize_identifier(row["fingerprint"], 80),
            "status": sanitize_enum(row["status"],
                                    frozenset({"ok", "unavailable", "error"})),
            "as_of": sanitize_date(row["created_at"]),
        })
    return refs


def build_daily_compression(store: ContinuityStore, *, profile: str,
                            compression_date: str, run_id: str, as_of: str,
                            summarizer: DailySummarizer | None = None
                            ) -> dict[str, Any]:
    """Derive the deterministic daily record. Pure read; writes nothing.

    Every input is explicit — nothing here reads a wall clock, resolves a
    live profile path, or consults anything outside *store*.
    """
    if summarizer is not None and not LLM_SUMMARY_ENABLED:
        raise DreamCycleError(
            "daily compression LLM summarization is disabled; the "
            "DailySummarizer seam exists for a future, separately approved "
            "change and must not be wired into the deterministic cycle")
    if not isinstance(profile, str) or not profile.strip():
        raise DreamCycleError("daily compression requires a profile")
    if not is_iso_date(compression_date):
        raise DreamCycleError(
            f"compression_date {compression_date!r} is not a valid calendar date")
    if not is_iso_datetime(as_of):
        raise DreamCycleError(
            f"as_of {as_of!r} is not a valid ISO-8601 datetime")
    if not isinstance(run_id, str) or not run_id.strip():
        raise DreamCycleError("daily compression requires a source run_id")

    projects = _active_projects(store)
    threads = _unresolved_threads(store)
    changes = _state_changes(store, compression_date)
    decisions = _durable_decisions(store)
    source_refs = _run_source_refs(store, run_id)

    # Thread-level source pointers ride the thread rows; the record-level
    # list stays the run's own provenance chain, bounded.
    outcome = (OUTCOME_RECORDED
               if (projects or threads or changes or decisions)
               else OUTCOME_NO_NEW_EVIDENCE)

    run = store._conn.execute("SELECT * FROM runs WHERE run_id = ?",
                              (run_id,)).fetchone()
    record: dict[str, Any] = {
        "schema_version": DAILY_COMPRESSION_SCHEMA_VERSION,
        "kind": DAILY_COMPRESSION_KIND,
        "profile": sanitize_text(profile, 40),
        "compression_date": compression_date,
        "outcome": outcome,
        "abstained": outcome == OUTCOME_NO_NEW_EVIDENCE,
        "provenance": {
            "contract_schema_version": CONTRACT_SCHEMA_VERSION,
            "compressor_version": COMPRESSOR_VERSION,
            "classifier_kind": "deterministic",
            "llm_summary": None,
            "run_id": sanitize_identifier(run_id),
            "collector_version": sanitize_text(
                run["collector_version"] if run else "", 60),
            "manifest_fingerprint": sanitize_identifier(
                run["manifest_fingerprint"] if run else "", 80),
            "window": {
                "start": sanitize_date(run["window_start"]) if run else None,
                "end": sanitize_date(run["window_end"]) if run else None,
            },
            "generated_at": as_of,
        },
        "counts": {
            "active_projects": len(projects),
            "unresolved_threads": len(threads),
            "state_changes": len(changes),
            "durable_decisions": len(decisions),
            "source_refs": len(source_refs),
        },
        "active_projects": projects[:MAX_PROJECTS],
        "unresolved_threads": threads[:MAX_THREADS],
        "state_changes": changes[:MAX_STATE_CHANGES],
        "durable_decisions": decisions[:MAX_DECISIONS],
        "source_refs": source_refs[:MAX_SOURCE_REFS],
    }
    if outcome == OUTCOME_NO_NEW_EVIDENCE:
        record["abstention_reason"] = (
            "no active projects, unresolved threads, durable decisions, or "
            "state changes in the collected evidence for this date")
    # Backstop sweep (a section added later without an explicit sanitizer
    # still cannot emit a raw string), then the hard size cap.
    record = sanitize_payload(record, ACTION_CHARS)
    record["schema_version"] = DAILY_COMPRESSION_SCHEMA_VERSION
    record["kind"] = DAILY_COMPRESSION_KIND
    record["compression_date"] = compression_date
    record["outcome"] = outcome
    return cap_serialized(record, MAX_RECORD_CHARS)


def compress_day(store: ContinuityStore, *, profile: str,
                 compression_date: str, run_id: str, as_of: str,
                 now: str) -> DailyCompressionResult:
    """Build and durably append one daily compression record.

    Idempotent: an identical rerun recomputes the identical record, matches
    the existing content-addressed row, and writes nothing.
    """
    record = build_daily_compression(store, profile=profile,
                                     compression_date=compression_date,
                                     run_id=run_id, as_of=as_of)
    compression_id, stored, revision = store.record_daily_compression(
        profile=profile, compression_date=compression_date, run_id=run_id,
        outcome=record["outcome"], payload=record, generated_at=as_of,
        now=now)
    return DailyCompressionResult(record=record, compression_id=compression_id,
                                  revision=revision, outcome=record["outcome"],
                                  stored=stored)


# ---------------------------------------------------------------------------
# Retrieval
# ---------------------------------------------------------------------------

def _row_to_payload(row: Any) -> dict[str, Any] | None:
    if row is None:
        return None
    try:
        payload = json.loads(row["payload"])
    except (TypeError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    payload = dict(payload)
    payload["compression_id"] = sanitize_identifier(row["compression_id"])
    payload["revision"] = int(row["revision"])
    payload["content_fingerprint"] = sanitize_identifier(
        row["content_fingerprint"], 80)
    return cap_serialized(payload, MAX_RECORD_CHARS)


def current_daily_compression(store: ContinuityStore, *, profile: str,
                              compression_date: str | None = None,
                              on_or_before: str | None = None
                              ) -> dict[str, Any] | None:
    """The current record for an exact date, or the most recent one at or
    before *on_or_before*. Returns None when there is nothing recorded."""
    # A store predating migration 5 has no table to read; absence of a
    # compression is a normal, non-degrading outcome everywhere.
    if store.schema_version() < 5:
        return None
    if compression_date is not None:
        row = store.get_daily_compression(profile, compression_date)
    else:
        row = store.latest_daily_compression(profile,
                                             on_or_before=on_or_before)
    return _row_to_payload(row)


def read_daily_compression(store_path: Path | str, *, profile: str,
                           compression_date: str | None = None,
                           on_or_before: str | None = None,
                           confine_root: Path | str | None = None
                           ) -> dict[str, Any] | None:
    """Read-only retrieval against an owned continuity store.

    Same confinement guards as the Phase 3 read paths: a store that is not
    an owned v3 database, or that does not resolve below *confine_root*, is
    refused rather than read. Returns None when nothing is recorded.
    """
    store_path = Path(store_path)
    if confine_root is not None:
        assert_store_confined(store_path, Path(confine_root))
    if inspect_store_identity(store_path) == "fresh":
        return None
    with ContinuityStore(store_path, read_only=True) as store:
        return current_daily_compression(store, profile=profile,
                                         compression_date=compression_date,
                                         on_or_before=on_or_before)


def wake_compression_line(record: dict[str, Any] | None,
                          budget: int = WAKE_LINE_BUDGET) -> str:
    """One bounded line summarizing the record for the wake packet, or "".

    An abstained (`no_new_evidence`) record renders as "" — the wake packet
    says nothing rather than spending its budget on an empty day.
    """
    if not isinstance(record, dict):
        return ""
    if record.get("outcome") != OUTCOME_RECORDED:
        return ""
    counts = record.get("counts")
    counts = counts if isinstance(counts, dict) else {}
    date = sanitize_date(record.get("compression_date"))
    revision = record.get("revision")
    revision = int(revision) if isinstance(revision, int) else 1
    projects = record.get("active_projects")
    projects = projects if isinstance(projects, list) else []
    names = [sanitize_identifier(p.get("project_id"), 64)
             for p in projects[:WAKE_LINE_PROJECTS] if isinstance(p, dict)]
    names = [n for n in names if n]
    more = max(0, len(projects) - len(names))
    project_bit = (", ".join(names) + (f" +{more} more" if more else "")
                   if names else "none")

    def _count(key: str) -> int:
        value = counts.get(key)
        return value if isinstance(value, int) and not isinstance(value, bool) \
            else 0

    line = (f"Daily continuity compression {date} (rev {revision}): "
            f"active projects {project_bit}; "
            f"{_count('unresolved_threads')} unresolved threads; "
            f"{_count('state_changes')} state changes; "
            f"{_count('durable_decisions')} durable decisions.")
    return sanitize_text(line, budget)
