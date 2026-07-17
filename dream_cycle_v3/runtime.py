"""Phase 4 production runtime: the deterministic daily cycle, the seven-window
historical replay gate, and the cutover gate.

Scope (design §13-§14, Phase 4): collection -> store -> retrieval only. This
runtime performs NO destination promotion in shadow or live mode — memory,
skill, and project-doc writes remain behind the separately approved Phase 2
adapters and a later cutover step. Everything external is read-only.

Principles carried over from Phases 0-3:
- No ambient discovery. Every read root, the v3 output root, profile, owner,
  time window, disposition date, and `as_of` timestamp are caller-supplied
  and validated; nothing here resolves a live Hermes path on its own.
- Deterministic default path, no runtime AI. Identical pinned inputs produce
  the identical run ID, and an identical rerun adds zero candidates,
  threads, dispositions, events, snapshots, or receipts (the store enforces
  it; the replay gate proves it per window).
- Fail loud. Carry-forward invariant violations, lock contention, foreign
  stores, and confinement violations raise typed errors; a failed retrieval
  smoke marks the cycle unsuccessful (no success event, nonzero exit).

Operational-evidence honesty: a successful cycle records one idempotent
`runtime_cycle_completed` event whose `created_at` is the RUNTIME's own wall
clock — deliberately never the caller's pinned `as_of` — so the cutover gate
can count distinct elapsed operational dates in durable state. Historical
replay reports/events are explicitly marked and contribute zero operational
dates, regardless of when replay executes.
"""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from datetime import date as date_type
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping

from . import CONTRACT_SCHEMA_VERSION
from .adapters import read_github_issues, read_kanban_board, read_todoist_tasks
from .canonical import canonical_json, stable_id
from .carry_forward import CarryForwardPolicy, run_carry_forward
from .collect import CollectionBounds, collect_to_manifest
from .contracts import is_iso_date, is_iso_datetime, parse_iso_datetime
from .errors import DreamCycleError, RootResolutionError, RuntimeLockError
from .lookup import continuity_lookup
from .report import build_run_report, create_file_exclusive, write_report
from .roots import CollectionRoots, prepare_output_root
from .sanitize import sanitize_text
from .store import ContinuityStore, assert_store_confined
from .wake import WakeInputs, build_wake_packet

RUNTIME_SCHEMA_VERSION = 1
CYCLE_REPORT_KIND = "dream-cycle-v3-phase4-cycle-report"
REPLAY_SUMMARY_KIND = "dream-cycle-v3-historical-replay-summary"
CUTOVER_GATE_KIND = "dream-cycle-v3-cutover-gate"
CYCLE_EVENT_TYPE = "runtime_cycle_completed"
REPLAY_SUMMARY_EVENT_TYPE = "historical_replay_summary_completed"
REPLAY_WINDOW_COUNT = 7
REQUIRED_OPERATIONAL_DAYS = 7
MIN_RETRIEVAL_SUCCESS_RATE = 0.95
LOCK_FILENAME = "runtime.lock"
V2_MIGRATION_CLASSIFIER_VERSION = "v2-migration-1"
_V2_EXCERPT_CHARS = 700

REPLAY_LABEL = ("historical replay: seven contiguous one-day windows "
                "replayed against real sources in one sitting; "
                "NOT seven elapsed daily operational cycles")
# per-thread detail cap for cycle reports; totals are always exact
REPORT_THREADS_CAP = 100
# The negative retrieval probe: a resolver that activates a project on this
# no-evidence message is over-eager and must fail the smoke. The odd token
# keeps real registries from colliding with it accidentally.
SMOKE_ABSTENTION_PROBE = ("abstention probe zq9x7f3d1c with no recorded "
                          "project evidence in this message")


# ---------------------------------------------------------------------------
# Single-flight lock
# ---------------------------------------------------------------------------

class runtime_lock:
    """Exclusive single-flight lock at `<v3_root>/runtime.lock`.

    Symlink/cross-root tricks are refused: the lock path may not be a
    symlink (checked before AND after open via O_NOFOLLOW plus a
    dev/inode comparison, so a swap between check and open cannot win),
    and the caller must pass the already-resolved v3 root so the lock can
    never be redirected outside it. flock(2) auto-releases if the process
    dies, so a crashed cycle never wedges the next cron run.
    """

    def __init__(self, v3_root: Path):
        self.v3_root = Path(v3_root)
        self.path = self.v3_root / LOCK_FILENAME
        self._fd: int | None = None

    def __enter__(self) -> "runtime_lock":
        try:
            import fcntl
        except ImportError:  # pragma: no cover - POSIX-only cron runtime
            raise RuntimeLockError(
                "the phase 4 runtime requires POSIX flock; refusing to run "
                "without a single-flight lock") from None
        if self.path.is_symlink():
            raise RuntimeLockError(
                f"{self.path} is a symlink; refusing to lock through it")
        flags = os.O_CREAT | os.O_RDWR
        nofollow = getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(self.path, flags | nofollow, 0o600)
        except OSError as exc:
            raise RuntimeLockError(
                f"cannot open runtime lock {self.path}: {exc}") from None
        try:
            fstat = os.fstat(fd)
            lstat = os.lstat(self.path)
            if (fstat.st_dev, fstat.st_ino) != (lstat.st_dev, lstat.st_ino):
                raise RuntimeLockError(
                    f"{self.path} changed identity during locking; refusing")
            if self.path.parent.resolve() != self.v3_root:
                raise RuntimeLockError(
                    f"runtime lock parent {self.path.parent} does not resolve "
                    f"to the declared v3 root {self.v3_root}; refusing")
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                raise RuntimeLockError(
                    f"another dream-cycle-v3 runtime holds {self.path}; "
                    "single-flight refused") from None
        except BaseException:
            os.close(fd)
            raise
        self._fd = fd
        os.ftruncate(fd, 0)
        os.write(fd, f"pid={os.getpid()}\n".encode("utf-8"))
        return self

    def __exit__(self, *exc: object) -> None:
        if self._fd is not None:
            os.close(self._fd)  # closing the fd releases the flock
            self._fd = None


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class RuntimeConfig:
    """Explicit configuration for one daily cycle. No field is discovered."""

    profile: str
    owner: str
    read_roots: Mapping[str, str | os.PathLike]
    v3_root: str | os.PathLike
    window_start: datetime
    window_end: datetime
    disposition_date: str
    as_of: str
    mode: str = "shadow"
    registry_path: str | os.PathLike | None = None
    threads_path: str | os.PathLike | None = None
    kanban_db: str | os.PathLike | None = None
    kanban_board: str | None = None
    todoist_export: str | os.PathLike | None = None
    github_repo: str | None = None
    github_available: bool = False
    kanban_root: str | os.PathLike | None = None
    projects_home: str | os.PathLike | None = None
    migrate_v2_roots: tuple[str, ...] = ()
    bounds: CollectionBounds = field(default_factory=CollectionBounds)
    stale_after_days: int = 14
    smoke_message: str | None = None
    smoke_expected_project: str | None = None
    smoke_require_thread: bool = False
    historical_replay: bool = False

    def __post_init__(self) -> None:
        errors: list[str] = []
        if not self.profile or not self.profile.strip():
            errors.append("profile is required")
        if not self.owner or not self.owner.strip():
            errors.append("owner is required")
        if not self.read_roots:
            errors.append("at least one read root is required")
        for name in ("window_start", "window_end"):
            value = getattr(self, name)
            if not isinstance(value, datetime) or value.tzinfo is None \
                    or value.utcoffset() is None:
                errors.append(f"{name} must be a timezone-aware datetime")
        if not errors and self.window_start >= self.window_end:
            errors.append("window_start must precede window_end")
        if not is_iso_date(self.disposition_date):
            errors.append(f"disposition_date {self.disposition_date!r} is not "
                          "a valid calendar date")
        if not is_iso_datetime(self.as_of):
            errors.append(f"as_of {self.as_of!r} is not a valid ISO-8601 "
                          "datetime")
        if self.mode not in ("shadow", "live"):
            errors.append(f"mode must be 'shadow' or 'live', got {self.mode!r}")
        unknown = set(self.migrate_v2_roots) - set(self.read_roots)
        if unknown:
            errors.append(f"migrate_v2_roots name undeclared read roots: "
                          f"{sorted(unknown)}")
        if self.kanban_db is not None and not self.kanban_board:
            errors.append("kanban_db requires an explicit kanban_board key")
        if (self.smoke_expected_project is None) != (self.smoke_message is None):
            errors.append("retrieval smoke requires both smoke_message and "
                          "smoke_expected_project")
        if errors:
            raise DreamCycleError("RuntimeConfig: " + "; ".join(errors))

    @property
    def smoke_configured(self) -> bool:
        return self.smoke_expected_project is not None


@dataclass(frozen=True)
class CycleResult:
    report: dict[str, Any]
    report_path: Path
    status_line: str
    ok: bool


@dataclass(frozen=True)
class ReplayResult:
    summary: dict[str, Any]
    summary_path: Path
    status_line: str
    ok: bool


# ---------------------------------------------------------------------------
# Cycle internals
# ---------------------------------------------------------------------------

def _prepare_v3_root(config: RuntimeConfig, roots: CollectionRoots) -> Path:
    v3_root = prepare_output_root(config.v3_root,
                                  forbidden_within=roots.roots)
    for key, root in roots.roots.items():
        if root == v3_root or root.is_relative_to(v3_root):
            raise RootResolutionError(
                f"read root '{key}' ({root}) is inside the v3 output root "
                f"{v3_root}; the runtime must never collect its own output")
    return v3_root


def _real_subdir(v3_root: Path, name: str) -> Path:
    """A v3-root subdirectory that is provably not a symlink redirect."""
    path = v3_root / name
    if path.is_symlink():
        raise RootResolutionError(
            f"{path} is a symlink; refusing to write v3 output through it")
    path.mkdir(exist_ok=True)
    return path


def _tracker_results(config: RuntimeConfig) -> list:
    results = []
    if config.kanban_db is not None:
        results.append(read_kanban_board(config.kanban_db,
                                         board_key=config.kanban_board))
    if config.todoist_export is not None:
        results.append(read_todoist_tasks(export_path=config.todoist_export))
    if config.github_repo is not None:
        results.append(read_github_issues(
            config.github_repo, gh_available=config.github_available))
    return results


def _load_json_list(path: Path, what: str) -> list[dict[str, Any]]:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise DreamCycleError(f"{what} file unreadable: {exc}") from None
    if not isinstance(payload, list):
        raise DreamCycleError(f"{what} file must contain a JSON array")
    return payload


def _bump(counter: dict[str, int], key: str) -> None:
    counter[key] = counter.get(key, 0) + 1


def v2_migration_candidate(manifest: dict[str, Any],
                           source: dict[str, Any]) -> dict[str, Any]:
    """Quarantine-first candidate for one v2 artifact source.

    Identity is deterministic over the manifest source identity plus its
    full-content fingerprint; the destination is the review quarantine and
    the status is 'quarantined' — never a live destination, never promoted
    here. Session sources stay metadata-only (transcript containment);
    other excerpts pass the fail-closed sanitizer (secret scan + PII
    redaction) before being bounded into the evidence reference.
    """
    source_id = source["source_id"]
    fingerprint = source["fingerprint"]
    candidate_id = stable_id("dream-cycle-v3-v2-candidate", source_id,
                             fingerprint)
    claim = (f"v2 artifact {source_id} ({fingerprint[:23]}) quarantined "
             "for operator review (phase 4 v2 migration)")
    subject = f"v2-artifact {source_id}"[:300]
    evidence: dict[str, Any] = {
        "source_type": source["source_type"],
        "source_id": source_id,
        "location": source["location"],
        "observed_at": source["mtime_utc"],
        "fingerprint": fingerprint,
    }
    if source["source_type"] != "session" and source["excerpt"]:
        evidence["excerpt"] = sanitize_text(source["excerpt"],
                                            _V2_EXCERPT_CHARS)
    return {
        "schema_version": CONTRACT_SCHEMA_VERSION,
        "candidate_id": candidate_id,
        "content_revision": 1,
        "class": "quarantine",
        "project_id": None,
        "destination": "quarantine:review",
        "normalized_claim": claim,
        "canonical_subject": subject,
        "retrieval_terms": [],
        "evidence_refs": [evidence],
        "confidence": 0.0,
        "freshness_class": "ephemeral",
        "sensitivity_class": "normal",
        "dedupe_key": stable_id("dream-cycle-v3-dedupe", "quarantine:review",
                                "", subject, claim,
                                str(CONTRACT_SCHEMA_VERSION)),
        "semantic_cluster_id": None,
        "status": "quarantined",
        "validation_requirements": ["human_review", "v2_migration"],
        "conflict_set": [],
        "provenance": {
            "run_id": manifest["run_id"],
            "collector_version": manifest["collector_version"],
            "classifier_kind": "deterministic",
            "classifier_version": V2_MIGRATION_CLASSIFIER_VERSION,
            "model": None,
            "prompt_hash": None,
        },
    }


def _retrieval_smoke(config: RuntimeConfig, db_path: Path) -> dict[str, Any]:
    """Exercise the real Phase 3 wake and lookup brokers against the store.

    Three probes: positive activation of the expected project, an explicit
    lookup, and a no-evidence ABSTENTION probe — a resolver that activates
    anything on the abstention message is over-eager and fails the smoke,
    so a regression that always activates the expected project can never
    score a pass.
    """
    expected = config.smoke_expected_project

    def wake(message: str):
        return build_wake_packet(
            store_path=db_path,
            projects_home=config.projects_home,
            kanban_root=config.kanban_root,
            todoist_export_path=config.todoist_export,
            inputs=WakeInputs(profile=config.profile, owner=config.owner,
                              now=config.as_of, first_message=message))

    packet = wake(config.smoke_message or "")
    wake_ok = (packet is not None and not packet.degraded
               and packet.project_id == expected)
    wake_threads = len(packet.thread_ids) if packet is not None else 0

    lookup_ok, lookup_threads = False, 0
    try:
        lookup = continuity_lookup(
            store_path=db_path, projects_home=config.projects_home,
            project=expected, kanban_root=config.kanban_root,
            todoist_export_path=config.todoist_export)
        lookup_ok = bool(lookup.get("found")) \
            and lookup.get("project_id") == expected
        lookup_threads = len(lookup.get("open_threads") or [])
    except DreamCycleError:
        lookup_ok = False

    probe = wake(SMOKE_ABSTENTION_PROBE)
    abstain_ok = (
        probe is not None
        and probe.project_id is None
        and not probe.degraded
        and probe.project_method == "abstain_no_evidence"
    )

    ok = wake_ok and lookup_ok and abstain_ok
    if config.smoke_require_thread:
        ok = ok and (wake_threads >= 1 or lookup_threads >= 1)
    return {
        "configured": True,
        "ok": ok,
        "expected_project": expected,
        "wake": {
            "ok": wake_ok,
            "project_id": packet.project_id if packet is not None else None,
            "method": packet.project_method if packet is not None else "none",
            "threads": wake_threads,
        },
        "lookup": {"ok": lookup_ok, "open_threads": lookup_threads},
        "abstention": {
            "ok": abstain_ok,
            "activated": probe.project_id if probe is not None else None,
            "method": probe.project_method if probe is not None else "none",
        },
    }


def _record_success_event(store: ContinuityStore, *, run_id: str,
                          payload: dict[str, Any]) -> None:
    # `created_at` is the runtime's own wall clock, NEVER the caller's
    # pinned as_of: this event is the durable evidence of an elapsed
    # operational date, and a replayed window must not be able to fabricate
    # one. The payload is deterministic, so the event key is stable and an
    # identical rerun inserts nothing (INSERT OR IGNORE).
    executed_at = datetime.now(timezone.utc).isoformat()
    with store.transaction() as conn:
        store._emit_event(conn, entity_type="run", entity_id=run_id,
                          event_type=CYCLE_EVENT_TYPE, payload=payload,
                          run_id=run_id, now=executed_at)


def _hot_memory_task_leakage(store: ContinuityStore) -> dict[str, int]:
    task_hot = store._conn.execute(
        "SELECT COUNT(*) AS c FROM candidates WHERE destination LIKE "
        "'memory:hot%' AND class = 'task_thread'").fetchone()["c"]
    promoted = store._conn.execute(
        "SELECT COUNT(*) AS c FROM candidates "
        "WHERE status = 'promoted'").fetchone()["c"]
    return {"task_candidates_in_hot_memory": task_hot,
            "promoted_candidates": promoted}


def _status_line(report: dict[str, Any], report_path: Path, ok: bool) -> str:
    if not ok:
        return (f"dream-cycle-v3 cycle FAIL reason=retrieval_smoke "
                f"mode={report['mode']} profile={report['profile']} "
                f"date={report['cycle_date']} run={report['run_id']} "
                f"report={report_path}")
    carry = report["carry_forward"]
    smoke = report["retrieval_smoke"]
    smoke_state = "pass" if smoke["configured"] else "off"
    dispositioned = carry["dispositioned"] + carry["already_dispositioned"]
    return (f"dream-cycle-v3 cycle ok mode={report['mode']} "
            f"profile={report['profile']} date={report['cycle_date']} "
            f"run={report['run_id']} sources={report['sources']} "
            f"excluded={report['excluded']} "
            f"candidates_new={report['v2_migration']['candidates_new']} "
            f"threads_new={report['threads_seed']['inserted']} "
            f"dispositions={dispositioned}/{carry['selected']} "
            f"receipts={report['invariants']['write_receipts']} "
            f"smoke={smoke_state} report={report_path}")


def _trusted_report_hash(conn, *, run_id: str, profile: str, mode: str,
                         historical_replay: bool) -> str | None:
    """The report_sha256 already attested for *run_id*, if any (codex
    phase-4 fourth review finding 1).

    This is prior DURABLE evidence — a `runtime_cycle_completed` event
    joined to a recorded run for the same profile — never a value derived
    from the report file currently on disk. It is the trust anchor
    `_publish_cycle_report_once` checks an existing file against: only a
    file whose bytes reproduce THIS hash may stand as "the same report";
    anything else fails closed rather than being re-attested.
    """
    rows = conn.execute(
        "SELECT e.payload AS payload FROM events e "
        "JOIN runs r ON e.run_id = r.run_id "
        "WHERE e.event_type = ? AND e.run_id = ? AND r.profile = ?",
        (CYCLE_EVENT_TYPE, run_id, profile)).fetchall()
    trusted_hashes: set[str] = set()
    mismatched_evidence = False
    for row in rows:
        try:
            payload = json.loads(row["payload"])
        except (TypeError, ValueError):
            mismatched_evidence = True
            continue
        if not isinstance(payload, dict):
            mismatched_evidence = True
            continue
        if (payload.get("mode") != mode
                or payload.get("profile") != profile
                or payload.get("historical_replay") is not historical_replay):
            mismatched_evidence = True
            continue
        sha = payload.get("report_sha256")
        if isinstance(sha, str) and sha:
            trusted_hashes.add(sha)
        else:
            mismatched_evidence = True
    if mismatched_evidence:
        raise DreamCycleError(
            f"completion evidence for run {run_id!r} has a missing or "
            "conflicting evidence-origin marker; refusing cross-role reuse")
    if len(trusted_hashes) > 1:
        raise DreamCycleError(
            f"completion evidence for run {run_id!r} has conflicting report "
            "hashes; refusing reuse")
    return next(iter(trusted_hashes), None)


def _publish_cycle_report_once(store: ContinuityStore, report: dict[str, Any],
                               reports_dir: Path, *, ok: bool
                               ) -> tuple[Path, str]:
    """Publish the first successful report for a deterministic run once,
    returning (path, sha256-hex digest) of the exact bytes THIS call just
    verified or wrote.

    Retry-local deltas are useful in the one-line status but must not rewrite
    the durable report for the same run id. The runtime lock makes this
    check/write single-flight.

    Trust comes from prior DURABLE evidence, never from shape-matching an
    existing file (codex phase-4 fourth review finding 1): an existing file
    is accepted only when its current bytes reproduce a report_sha256
    already attested by a `runtime_cycle_completed` event for this run. A
    file present without that prior trusted evidence — a planted file, or a
    crash between the write and the event record — fails closed whenever
    THIS call would go on to hash-and-trust it (`ok` is true: the caller is
    about to record it as new completion evidence); recovery there is
    deleting/replaying the isolated v3 root, never trusting unverified
    bytes. An unsuccessful cycle (`ok` is false) never records completion
    evidence regardless, so re-running an already-failed, never-attested
    report creates no re-attestation risk and is returned as-is — this is
    what lets the historical replay's immediate identical rerun complete
    for a smoke-failed window without aborting. Once ANY trust anchor
    exists for a run, its bytes are enforced against that hash regardless
    of the current call's outcome. Trusted evidence whose report is missing
    from disk fails closed instead of quietly republishing. This makes it
    impossible for a retry to compute a new "valid" hash over edited bytes.

    The returned digest is computed from the SAME bytes this call just
    wrote (hashed in memory, no disk read) or verified (hashed once, from
    the single read already performed here) — never from a later,
    independent re-read of `path`. A caller that records this digest as
    completion evidence therefore stays bound to what was actually
    verified/published here even if `path` is replaced afterward (codex
    phase-4 fifth review finding 1: the cycle-report attestation TOCTOU).
    """
    path = reports_dir / f"{report['run_id']}.json"
    trusted = _trusted_report_hash(
        store._conn, run_id=report["run_id"], profile=report["profile"],
        mode=report["mode"],
        historical_replay=report["historical_replay"])
    if not path.exists():
        if trusted is not None:
            raise DreamCycleError(
                f"trusted completion evidence exists for run "
                f"{report['run_id']!r} but its report is missing at {path}; "
                "refusing to republish over unverified state")
        written_path = write_report(report, reports_dir)
        digest = hashlib.sha256(
            (canonical_json(report) + "\n").encode("utf-8")).hexdigest()
        return written_path, digest
    if path.is_symlink() or not path.is_file():
        raise DreamCycleError(
            f"existing cycle report is not a regular owned file: {path}")
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise DreamCycleError(
            f"existing cycle report is unreadable; refusing overwrite: {exc}") \
            from None
    digest = hashlib.sha256(raw).hexdigest()
    try:
        existing_report = json.loads(raw)
    except ValueError:
        raise DreamCycleError(
            f"existing cycle report at {path} is not valid JSON; refusing "
            "reuse") from None
    expected_replay = report.get("historical_replay")
    if (not isinstance(existing_report, dict)
            or existing_report.get("historical_replay") is not expected_replay):
        raise DreamCycleError(
            f"existing cycle report at {path} has a missing or mismatched "
            "historical_replay evidence-origin marker; refusing cross-role "
            "reuse")
    if trusted is not None:
        if digest != trusted:
            raise DreamCycleError(
                f"existing cycle report at {path} does not match its "
                "previously trusted hash; refusing to re-attest edited "
                "bytes")
        return path, digest
    if ok:
        raise DreamCycleError(
            f"cycle report already exists at {path} without prior trusted "
            "completion evidence for this run; refusing to re-attest "
            "unverified bytes as a new successful completion — "
            "delete/replay the isolated v3 root to recover")
    return path, digest


# ---------------------------------------------------------------------------
# One daily cycle
# ---------------------------------------------------------------------------

def run_cycle(config: RuntimeConfig) -> CycleResult:
    """Execute one deterministic Phase 4 cycle under the single-flight lock.

    Collection, store recording, registry/thread upserts, tracker
    snapshots, quarantine-first v2 migration, carry-forward, retrieval
    smoke, machine report. No destination promotion in any mode.
    """
    roots = CollectionRoots.resolve(config.profile, dict(config.read_roots))
    v3_root = _prepare_v3_root(config, roots)

    with runtime_lock(v3_root):
        _real_subdir(v3_root, "manifests")
        reports_dir = _real_subdir(v3_root, "reports")

        manifest, manifest_path = collect_to_manifest(
            roots, v3_root, window_start=config.window_start,
            window_end=config.window_end, bounds=config.bounds,
            generated_at=parse_iso_datetime(config.as_of))
        run_id = manifest["run_id"]
        now = config.as_of

        db_path = v3_root / "continuity.db"
        assert_store_confined(db_path, v3_root)
        with ContinuityStore(db_path) as store:
            store.migrate(now)
            store.record_run(manifest, str(manifest_path), now)

            registry_outcomes: dict[str, int] = {}
            if config.registry_path is not None:
                for entry in _load_json_list(Path(config.registry_path),
                                             "project registry"):
                    _bump(registry_outcomes,
                          store.upsert_project(entry, now, run_id=run_id))

            tracker_rows = []
            for result in _tracker_results(config):
                store.record_adapter_snapshot(
                    run_id=run_id, adapter=result.adapter,
                    source_locator=result.source_locator,
                    status=result.status, detail=result.detail,
                    items=result.items_payload(), now=now)
                tracker_rows.append({
                    "adapter": result.adapter,
                    "source_locator": sanitize_text(result.source_locator),
                    "status": result.status,
                    "items": len(result.items),
                })

            thread_outcomes: dict[str, int] = {"inserted": 0, "exists": 0}
            if config.threads_path is not None:
                for thread in _load_json_list(Path(config.threads_path),
                                              "thread seed"):
                    _bump(thread_outcomes,
                          store.open_thread(thread, now, run_id=run_id))

            migration_outcomes: dict[str, int] = {}
            for source in manifest["sources"]:
                if source["root"] not in config.migrate_v2_roots:
                    continue
                candidate = v2_migration_candidate(manifest, source)
                _bump(migration_outcomes,
                      store.ingest_candidate(candidate, now))

            carry = run_carry_forward(
                store, run_id=run_id,
                disposition_date=config.disposition_date, now=now,
                policy=CarryForwardPolicy(
                    stale_after_days=config.stale_after_days))

            if config.smoke_configured:
                smoke = _retrieval_smoke(config, db_path)
            else:
                smoke = {"configured": False, "ok": None}
            ok = smoke["ok"] is not False

            leakage = _hot_memory_task_leakage(store)
            quarantined_total = store._conn.execute(
                "SELECT COUNT(*) AS c FROM candidates "
                "WHERE status = 'quarantined'").fetchone()["c"]
            receipts = store.counts()["write_receipts"]

            # One carry-forward section, bounded: exact totals always, but
            # per-thread rows are capped and never duplicated into the
            # embedded run_report (report size stays O(cap), not O(seed)).
            carry_dict = carry.to_dict()
            thread_rows = carry_dict.pop("threads")
            carry_bounded = dict(
                carry_dict,
                threads=thread_rows[:REPORT_THREADS_CAP],
                threads_total=len(thread_rows),
                threads_truncated=max(0,
                                      len(thread_rows) - REPORT_THREADS_CAP))
            run_report = build_run_report(store, run_id, generated_at=now,
                                          carry_forward=carry_dict)

            report = {
                "schema_version": RUNTIME_SCHEMA_VERSION,
                "kind": CYCLE_REPORT_KIND,
                "mode": config.mode,
                "historical_replay": config.historical_replay,
                "profile": config.profile,
                "cycle_date": config.disposition_date,
                "run_id": run_id,
                "window": {"start": manifest["window"]["start"],
                           "end": manifest["window"]["end"]},
                "as_of": now,
                "manifest_path": str(manifest_path),
                "manifest_fingerprint": manifest["manifest_fingerprint"],
                "sources": len(manifest["sources"]),
                "excluded": len(manifest["excluded"]),
                "registry": {"path": str(config.registry_path)
                             if config.registry_path else None,
                             "outcomes": dict(sorted(
                                 registry_outcomes.items()))},
                "threads_seed": {"path": str(config.threads_path)
                                 if config.threads_path else None,
                                 **{k: thread_outcomes.get(k, 0)
                                    for k in ("inserted", "exists")}},
                "trackers": tracker_rows,
                "v2_migration": {
                    "roots": sorted(config.migrate_v2_roots),
                    "outcomes": dict(sorted(migration_outcomes.items())),
                    "candidates_new": migration_outcomes.get("inserted", 0),
                    "quarantined_total": quarantined_total,
                },
                "carry_forward": carry_bounded,
                "retrieval_smoke": smoke,
                "run_report": run_report,
                "invariants": {
                    "carry_forward_invariant_ok": carry.invariant_ok,
                    "write_receipts": receipts,
                    "live_destination_writes": 0,  # no promotion path here
                    **leakage,
                },
                "generated_at": now,
            }
            # Publication ordering (operational-evidence honesty): the
            # report must be durably published BEFORE the success event is
            # recorded, so a publication failure can never count as an
            # elapsed operational day.
            report_path, report_digest = _publish_cycle_report_once(
                store, report, reports_dir, ok=ok)

            if ok:
                # `report_sha256` is the tamper-evident link the cutover gate
                # relies on (codex phase-4 finding 1): it is the digest
                # `_publish_cycle_report_once` returned from the bytes it
                # itself just verified/wrote, never a later independent
                # re-read of `report_path` (codex phase-4 fifth review
                # finding 1) — so neither an edited report file, an edited
                # caller-supplied dict, nor a replacement that lands after
                # publication can reproduce it without also rewriting this
                # event.
                report_sha256 = report_digest
                _record_success_event(store, run_id=run_id, payload={
                    "cycle_date": config.disposition_date,
                    "mode": config.mode,
                    "historical_replay": config.historical_replay,
                    "profile": config.profile,
                    "manifest_fingerprint": manifest["manifest_fingerprint"],
                    "report_sha256": report_sha256,
                })

    return CycleResult(report=report, report_path=report_path,
                       status_line=_status_line(report, report_path, ok),
                       ok=ok)


# ---------------------------------------------------------------------------
# Seven-window historical replay
# ---------------------------------------------------------------------------

def _trusted_replay_summary_hash(conn, *, profile: str, start_date: str,
                                 end_date: str) -> str | None:
    """The summary_sha256 already attested for this profile/start/end, if
    any (codex phase-4 fourth review finding 1) — the trust anchor
    `_publish_replay_summary_once` checks an existing summary file against.
    """
    rows = conn.execute(
        "SELECT payload FROM events WHERE event_type = ?",
        (REPLAY_SUMMARY_EVENT_TYPE,)).fetchall()
    for row in rows:
        try:
            payload = json.loads(row["payload"])
        except (TypeError, ValueError):
            continue
        if not isinstance(payload, dict):
            continue
        if (payload.get("profile") == profile
                and payload.get("start_date") == start_date
                and payload.get("end_date") == end_date):
            sha = payload.get("summary_sha256")
            if isinstance(sha, str) and sha:
                return sha
    return None


def _publish_replay_summary_once(summary: dict[str, Any], reports_dir: Path,
                                 *, trusted_hash: str | None
                                 ) -> tuple[Path, str, dict[str, Any]]:
    """Publish the canonical historical replay summary once, mirroring
    `_publish_cycle_report_once`'s trust model: an existing summary file is
    accepted only when its bytes reproduce a previously attested
    summary_sha256; anything else (a planted file, or a crash between write
    and event) fails closed rather than being re-attested.

    Returns (path, sha256-hex digest, canonical summary object) — all three
    derived from the SAME bytes this call just wrote (hashed/parsed from
    the in-memory `body`, no disk read) or verified (hashed/parsed once,
    from the single read already performed here), never from a later
    independent re-read (codex phase-4 fifth review finding 3: the
    replay-summary attestation TOCTOU).

    On a retry against an already-trusted summary, the canonical object
    returned is the one PARSED FROM THE VERIFIED ON-DISK BYTES, never the
    caller's freshly recomputed `summary` argument — retry-local values
    (e.g. carry-forward counts that now see threads as already-dispositioned
    instead of newly dispositioned) can genuinely differ from the first
    run's, and a caller that trusted the fresh recomputation while pointing
    at the untouched old artifact would return a result disconnected from
    what was actually published (codex phase-4 fifth review finding 4).
    """
    path = reports_dir / (f"historical-replay-{summary['start_date']}_"
                          f"{summary['end_date']}.json")
    if not path.exists():
        if trusted_hash is not None:
            raise DreamCycleError(
                f"trusted replay summary evidence exists for "
                f"{summary['profile']!r} {summary['start_date']}.."
                f"{summary['end_date']} but its summary is missing at "
                f"{path}; refusing to republish over unverified state")
        data = (canonical_json(summary) + "\n").encode("utf-8")
        create_file_exclusive(path, data)
        digest = hashlib.sha256(data).hexdigest()
        return path, digest, summary
    if path.is_symlink() or not path.is_file():
        raise DreamCycleError(
            f"existing replay summary is not a regular owned file: {path}")
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise DreamCycleError(
            f"existing replay summary is unreadable; refusing overwrite: "
            f"{exc}") from None
    if trusted_hash is None:
        raise DreamCycleError(
            f"replay summary already exists at {path} without prior "
            "trusted completion evidence; refusing to re-attest unverified "
            "bytes — delete/replay the isolated v3 root to recover")
    digest = hashlib.sha256(raw).hexdigest()
    if digest != trusted_hash:
        raise DreamCycleError(
            f"existing replay summary at {path} does not match its "
            "previously trusted hash; refusing to re-attest edited bytes")
    canonical_summary = json.loads(raw)
    return path, digest, canonical_summary


def _read_roots_integrity_fingerprint(roots: CollectionRoots) -> str:
    """Streaming metadata fingerprint of the read roots (integrity check).

    Detects writes into the read roots via (path, size, mtime_ns) of every
    regular file, hashed incrementally: bounded memory, no file CONTENT is
    ever opened (secret-excluded files are never read just to hash them),
    and symlinks are skipped entirely per the collector's symlink policy —
    a symlink target outside the roots is not ours to fingerprint. This is
    an integrity report only; the zero-live-writes verdict derives from
    mode/receipt/promotion evidence, not from these hashes.
    """
    hasher = hashlib.sha256()
    for key in sorted(roots.roots):
        root = roots.roots[key]
        for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
            dirnames.sort()
            for name in sorted(filenames):
                path = Path(dirpath) / name
                if path.is_symlink():
                    continue
                try:
                    st = path.stat()
                    entry = [key, str(path.relative_to(root)),
                             st.st_size, st.st_mtime_ns]
                except OSError as exc:
                    entry = [key, str(path.relative_to(root)),
                             f"stat_failed:{type(exc).__name__}"]
                hasher.update(canonical_json(entry).encode("utf-8"))
                hasher.update(b"\n")
    return "sha256:" + hasher.hexdigest()


def run_historical_replay(*, start_date: str, end_date: str,
                          **cycle_kwargs: Any) -> ReplayResult:
    """Seven contiguous one-day windows against real configured read roots.

    Each window runs one full cycle, then an immediate identical rerun whose
    row delta must be zero. This is a HISTORICAL REPLAY: it proves the
    pipeline over real sources but does not constitute the design's seven
    elapsed daily operational cycles (all success events land on today's
    wall-clock date).
    """
    # Evidence origin is runtime-owned, never a caller preference. Bind it
    # into each authenticated report/event so replay can never be mistaken
    # for an elapsed operational cycle, even when executed on a real day.
    cycle_kwargs = dict(cycle_kwargs)
    cycle_kwargs["historical_replay"] = True

    for name in ("window_start", "window_end", "disposition_date", "as_of",
                 "mode"):
        if name in cycle_kwargs:
            raise DreamCycleError(
                f"run_historical_replay derives {name} per window; do not "
                "pass it")
    if not (is_iso_date(start_date) and is_iso_date(end_date)):
        raise DreamCycleError("start_date and end_date must be calendar dates")
    start = date_type.fromisoformat(start_date)
    end = date_type.fromisoformat(end_date)
    if (end - start).days != REPLAY_WINDOW_COUNT:
        raise DreamCycleError(
            f"historical replay requires exactly {REPLAY_WINDOW_COUNT} "
            f"contiguous one-day windows; {start_date}..{end_date} spans "
            f"{(end - start).days}")

    probe = RuntimeConfig(mode="shadow",
                          window_start=datetime.combine(
                              start, datetime.min.time(), timezone.utc),
                          window_end=datetime.combine(
                              start + timedelta(days=1), datetime.min.time(),
                              timezone.utc),
                          disposition_date=start_date,
                          as_of=datetime.combine(
                              start + timedelta(days=1), datetime.min.time(),
                              timezone.utc).isoformat(),
                          **cycle_kwargs)
    roots = CollectionRoots.resolve(probe.profile, dict(probe.read_roots))
    roots_before = _read_roots_integrity_fingerprint(roots)

    windows: list[dict[str, Any]] = []
    smoke_passes = 0
    modes: set[str] = set()
    last_as_of = probe.as_of
    db_path: Path | None = None
    for i in range(REPLAY_WINDOW_COUNT):
        day = start + timedelta(days=i)
        window_start = datetime.combine(day, datetime.min.time(), timezone.utc)
        window_end = window_start + timedelta(days=1)
        as_of = window_end.isoformat()
        last_as_of = as_of
        cfg = RuntimeConfig(mode="shadow", window_start=window_start,
                            window_end=window_end,
                            disposition_date=day.isoformat(), as_of=as_of,
                            **cycle_kwargs)
        first = run_cycle(cfg)
        modes.add(first.report["mode"])
        db_path = Path(first.report["manifest_path"]).parent.parent / \
            "continuity.db"
        with ContinuityStore(db_path, read_only=True) as store:
            counts_before = store.counts()
            dump_before = store.dump_canonical()
        second = run_cycle(cfg)
        with ContinuityStore(db_path, read_only=True) as store:
            counts_after = store.counts()
            dump_after = store.dump_canonical()
            receipts = counts_after["write_receipts"]
        delta = {t: counts_after[t] - counts_before[t] for t in counts_before
                 if counts_after[t] != counts_before[t]}
        rerun_zero_delta = (not delta) and dump_before == dump_after
        if first.report["run_id"] != second.report["run_id"]:
            raise DreamCycleError("identical replay window produced a "
                                  "different run_id on rerun")

        smoke = first.report["retrieval_smoke"]
        if smoke["configured"]:
            smoke_state = "pass" if smoke["ok"] else "fail"
            smoke_passes += 1 if smoke["ok"] else 0
        else:
            smoke_state = "not_configured"

        windows.append({
            "date": day.isoformat(),
            "run_id": first.report["run_id"],
            "sources": first.report["sources"],
            "excluded": first.report["excluded"],
            "selected": first.report["carry_forward"]["selected"],
            "dispositioned": first.report["carry_forward"]["dispositioned"],
            "already_dispositioned":
                first.report["carry_forward"]["already_dispositioned"],
            "invariant_ok": first.report["carry_forward"]["invariant_ok"],
            "rerun_zero_delta": rerun_zero_delta,
            "rerun_row_delta": delta,
            "receipts": receipts,
            "retrieval_smoke": smoke_state,
        })

    roots_after = _read_roots_integrity_fingerprint(roots)
    read_roots_unchanged = roots_before == roots_after

    assert db_path is not None
    with ContinuityStore(db_path, read_only=True) as store:
        leakage = _hot_memory_task_leakage(store)
        total_receipts = store.counts()["write_receipts"]

    smoke_configured = probe.smoke_configured
    retrieval_rate = (smoke_passes / REPLAY_WINDOW_COUNT
                      if smoke_configured else None)
    invariants = {
        "seven_contiguous_windows": len(windows) == REPLAY_WINDOW_COUNT,
        "distinct_run_ids":
            len({w["run_id"] for w in windows}) == REPLAY_WINDOW_COUNT,
        "all_disposition_invariants_ok":
            all(w["invariant_ok"] for w in windows),
        "all_reruns_zero_delta": all(w["rerun_zero_delta"] for w in windows),
        "zero_write_receipts": total_receipts == 0,
        # integrity of the read roots, reported on its own (metadata-only)
        "read_roots_unchanged": read_roots_unchanged,
        # zero live writes derives from DESTINATION evidence: every window
        # ran in shadow mode, no write receipt exists, and no candidate was
        # ever promoted — never inferred from source hashes
        "all_windows_shadow_mode": modes == {"shadow"},
        "zero_live_destination_writes":
            modes == {"shadow"} and total_receipts == 0
            and leakage["promoted_candidates"] == 0,
        "zero_task_fields_in_hot_memory":
            leakage["task_candidates_in_hot_memory"] == 0
            and leakage["promoted_candidates"] == 0,
        "retrieval_success_rate": retrieval_rate,
    }
    ok = all(v is True for k, v in invariants.items()
             if k != "retrieval_success_rate") \
        and (retrieval_rate is None or retrieval_rate == 1.0)

    summary = {
        "schema_version": RUNTIME_SCHEMA_VERSION,
        "kind": REPLAY_SUMMARY_KIND,
        "historical_replay": True,
        "is_operational_evidence": False,
        "label": REPLAY_LABEL,
        "profile": probe.profile,
        "start_date": start_date,
        "end_date": end_date,
        "windows": windows,
        "invariants": invariants,
        "read_roots_fingerprint": roots_after,
        "generated_at": last_as_of,
    }
    v3_root = db_path.parent
    reports_dir = _real_subdir(v3_root, "reports")
    with ContinuityStore(db_path) as store:
        trusted_hash = _trusted_replay_summary_hash(
            store._conn, profile=probe.profile, start_date=start_date,
            end_date=end_date)
        summary_path, summary_digest, canonical_summary = \
            _publish_replay_summary_once(
                summary, reports_dir, trusted_hash=trusted_hash)
        if trusted_hash is None:
            # First publication: attest the digest `_publish_replay_summary_
            # once` returned for the bytes it just durably wrote, then
            # record the dedicated summary event bound to profile/start/end
            # (codex phase-4 fourth review finding 1) — mirrors the cycle
            # report's publish-then-attest ordering so a publication
            # failure can never count as attested, and never re-reads
            # `summary_path` to compute the digest (codex phase-4 fifth
            # review finding 3: the replay-summary attestation TOCTOU).
            executed_at = datetime.now(timezone.utc).isoformat()
            with store.transaction() as conn:
                store._emit_event(
                    conn, entity_type="run",
                    entity_id=f"replay-summary:{probe.profile}:"
                              f"{start_date}:{end_date}",
                    event_type=REPLAY_SUMMARY_EVENT_TYPE,
                    payload={"profile": probe.profile,
                             "start_date": start_date, "end_date": end_date,
                             "summary_sha256": summary_digest},
                    run_id=None, now=executed_at)

    # Everything downstream derives from the CANONICAL summary — the exact
    # bytes actually durably published/verified above — never from the
    # freshly recomputed local `summary`/`windows`/`ok`: a retry's in-memory
    # recomputation can genuinely diverge (e.g. carry-forward counts differ
    # once the first run's dispositions are already durable) from what was
    # actually attested, and `ReplayResult` must never point callers at an
    # artifact/object split (codex phase-4 fifth review finding 4).
    summary = canonical_summary
    windows = summary["windows"]
    # The receipts figure shown in the status line must derive from this
    # same canonical evidence, never from the pre-publish `total_receipts`
    # local variable above: that value is a fresh `store.counts()` read
    # taken on THIS call, and a retry's local recomputation can diverge from
    # what was actually attested even though the store is logically
    # unchanged (codex phase-4 final review Low caveat — finding 4's retry
    # protection covered `summary`/`ok`/`windows` but missed this field).
    total_receipts = windows[-1]["receipts"] if windows else total_receipts
    inv = summary["invariants"]
    ok = all(v is True for k, v in inv.items()
             if k != "retrieval_success_rate") \
        and (inv.get("retrieval_success_rate") is None
             or inv.get("retrieval_success_rate") == 1.0)
    zero_delta_count = sum(1 for w in windows if w["rerun_zero_delta"])
    smoke_passes = sum(1 for w in windows if w.get("retrieval_smoke") == "pass")
    smoke_configured = any(
        w.get("retrieval_smoke") != "not_configured" for w in windows)
    retrieval_note = (f"{smoke_passes}/{REPLAY_WINDOW_COUNT}"
                      if smoke_configured else "off")
    verdict = "ok" if ok else "FAIL"
    status_line = (f"dream-cycle-v3 historical-replay {verdict} "
                   f"windows={len(windows)} profile={summary['profile']} "
                   f"start={summary['start_date']} end={summary['end_date']} "
                   f"rerun_zero_delta={zero_delta_count}/{REPLAY_WINDOW_COUNT} "
                   f"receipts={total_receipts} retrieval={retrieval_note} "
                   f"summary={summary_path}")
    return ReplayResult(summary=summary, summary_path=summary_path,
                        status_line=status_line, ok=ok)


# ---------------------------------------------------------------------------
# Cutover gate
# ---------------------------------------------------------------------------

def _distinct_operational_dates(store_path: Path, *, profile: str | None
                                ) -> int:
    """Distinct wall-clock dates evidenced by real successful SHADOW cycles.

    `runtime_cycle_completed` events are stamped with the runtime's own
    clock at execution time, and historical-replay reports/events are
    explicitly excluded. Only events that (a) link to
    a run actually recorded in this store (`run_id` joins `runs`; synthetic
    `run_id=None` rows never count), (b) carry a payload declaring
    `mode == "shadow"` for the SAME profile, and (c) carry a report_sha256
    that authenticates the canonical store-owned report for that run — the
    actual bytes on disk, re-hashed here, must reproduce it — are counted
    (codex phase-4 fourth review finding 1). A hashless or fabricated-hash
    payload-only event, even one linked to a genuinely recorded run, proves
    nothing and is never counted.

    Two further bindings close the "one run/report evidences seven days"
    gap (codex phase-4 fifth review finding 2), where a probe replayed the
    SAME already-attested run_id/report_sha256 under several fabricated
    events (distinct entity_ids, distinct backdated wall clocks) and had
    each one counted as its own elapsed day:

    - EVERY event field that claims something about the run is checked
      against the canonical report's OWN fields and the joined `runs` row:
      the payload's `cycle_date` and `manifest_fingerprint` must match the
      canonical report's, and the canonical report's own `run_id`,
      `profile`, `mode`, and `window` must match the joined run. A payload
      that varies any of these away from what the run's one immutable,
      hash-verified report actually says is rejected outright.
    - Even a payload that reproduces every bound field exactly can only
      count ONCE per run_id: at most one distinct date is credited per
      genuinely-evidenced run, no matter how many completion events (real
      or fabricated) reference it.
    """
    if not profile:
        return 0
    store_path = Path(store_path)
    reports_dir = store_path.parent / "reports"
    with ContinuityStore(store_path, read_only=True) as store:
        rows = store._conn.execute(
            "SELECT e.created_at AS created_at, e.payload AS payload, "
            "e.run_id AS run_id, r.profile AS run_profile, "
            "r.manifest_fingerprint AS run_manifest_fingerprint, "
            "r.window_start AS run_window_start, "
            "r.window_end AS run_window_end "
            "FROM events e JOIN runs r ON e.run_id = r.run_id "
            "WHERE e.event_type = ?", (CYCLE_EVENT_TYPE,)).fetchall()
    dates: set[str] = set()
    counted_runs: set[str] = set()
    for row in rows:
        try:
            payload = json.loads(row["payload"])
        except (TypeError, ValueError):
            continue
        if not isinstance(payload, dict):
            continue
        run_id = row["run_id"]
        if row["run_profile"] != profile \
                or payload.get("mode") != "shadow" \
                or payload.get("historical_replay") is not False \
                or payload.get("profile") != profile \
                or not isinstance(run_id, str) or not run_id:
            continue
        if run_id in counted_runs:
            continue
        claimed_hash = payload.get("report_sha256")
        if not isinstance(claimed_hash, str) or not claimed_hash:
            continue
        report_path = reports_dir / f"{run_id}.json"
        try:
            if report_path.is_symlink() or not report_path.is_file():
                continue
            raw = report_path.read_bytes()
        except OSError:
            continue
        actual_hash = hashlib.sha256(raw).hexdigest()
        if actual_hash != claimed_hash:
            continue
        try:
            report = json.loads(raw)
        except ValueError:
            continue
        if not isinstance(report, dict):
            continue
        if (report.get("kind") != CYCLE_REPORT_KIND
                or report.get("mode") != "shadow"
                or report.get("historical_replay") is not False
                or report.get("profile") != profile
                or report.get("run_id") != run_id
                or report.get("cycle_date") != payload.get("cycle_date")
                or report.get("manifest_fingerprint")
                != payload.get("manifest_fingerprint")
                or report.get("manifest_fingerprint")
                != row["run_manifest_fingerprint"]
                or not isinstance(report.get("window"), dict)
                or report["window"].get("start") != row["run_window_start"]
                or report["window"].get("end") != row["run_window_end"]):
            continue
        counted_runs.add(run_id)
        dates.add(str(row["created_at"])[:10])
    return len(dates)


def _canonical_report_for_run(conn, reports_dir: Path, *, run_id: str,
                              profile: str, historical_replay: bool
                              ) -> dict[str, Any] | None:
    """The durable canonical report for *run_id*, hash-verified against its
    own success event (codex phase-4 finding 1).

    A caller-supplied report dict, or a report file edited directly on
    disk, is exactly the trust gap this closes: shape-valid JSON can claim
    any invariant/smoke outcome. This instead reads the report bytes
    actually on disk and requires their sha256 to match `report_sha256` in
    a `runtime_cycle_completed` event for the SAME run_id, joined to a run
    recorded for the SAME profile — a value nothing but `run_cycle` itself
    (see `_record_success_event`) ever writes. Neither an edited dict nor
    an edited file can reproduce that hash, so both fail closed (return
    None) rather than being trusted.
    """
    if not isinstance(run_id, str) or not run_id or not profile:
        return None
    path = reports_dir / f"{run_id}.json"
    try:
        if path.is_symlink() or not path.is_file():
            return None
        raw = path.read_bytes()
    except OSError:
        return None
    digest = hashlib.sha256(raw).hexdigest()
    rows = conn.execute(
        "SELECT e.payload AS payload FROM events e "
        "JOIN runs r ON e.run_id = r.run_id "
        "WHERE e.event_type = ? AND e.run_id = ? AND r.profile = ?",
        (CYCLE_EVENT_TYPE, run_id, profile)).fetchall()
    for row in rows:
        try:
            payload = json.loads(row["payload"])
        except (TypeError, ValueError):
            continue
        if not isinstance(payload, dict):
            continue
        if (payload.get("mode") != "shadow"
                or payload.get("profile") != profile
                or payload.get("historical_replay") is not historical_replay):
            continue
        if payload.get("report_sha256") != digest:
            continue
        try:
            report = json.loads(raw)
        except ValueError:
            return None
        if (not isinstance(report, dict)
                or report.get("historical_replay") is not historical_replay):
            return None
        return report
    return None


def _verify_replay_windows_against_store(replay_store_path: Path | str, *,
                                         profile: str | None,
                                         windows: Any) -> tuple[bool, str]:
    """Every replay window's run/report/event verified against the replay
    store, never trusted from the summary JSON alone (codex phase-4 finding
    1a: fabricated shape-valid replay rows).
    """
    if not profile:
        return False, "no replay profile to verify windows against"
    if not isinstance(windows, list) or not windows:
        return False, "no replay window rows to verify"
    reports_dir = Path(replay_store_path).parent / "reports"
    try:
        with ContinuityStore(Path(replay_store_path),
                             read_only=True) as store:
            for row in windows:
                if not isinstance(row, dict):
                    return False, "a replay window row is not an object"
                run_id = row.get("run_id")
                run_row = None
                if isinstance(run_id, str) and run_id:
                    run_row = store._conn.execute(
                        "SELECT profile FROM runs WHERE run_id = ?",
                        (run_id,)).fetchone()
                if run_row is None or run_row["profile"] != profile:
                    return False, (
                        f"replay window run_id {run_id!r} is not recorded "
                        f"in the replay store for profile {profile!r}")
                canonical = _canonical_report_for_run(
                    store._conn, reports_dir, run_id=run_id, profile=profile,
                    historical_replay=True)
                if canonical is None:
                    return False, (
                        f"replay window run_id {run_id!r} has no "
                        "hash-verified canonical report/event in the "
                        "replay store")
                inv = canonical.get("invariants") or {}
                smoke = canonical.get("retrieval_smoke") or {}
                claim = row.get("retrieval_smoke")
                smoke_matches = (
                    (claim == "pass" and smoke.get("configured") is True
                     and smoke.get("ok") is True)
                    or (claim == "fail" and smoke.get("configured") is True
                        and smoke.get("ok") is False)
                    or (claim == "not_configured"
                        and smoke.get("configured") is not True))
                if not (canonical.get("kind") == CYCLE_REPORT_KIND
                        and canonical.get("mode") == "shadow"
                        and canonical.get("cycle_date") == row.get("date")
                        and canonical.get("sources") == row.get("sources")
                        and canonical.get("excluded") == row.get("excluded")
                        and inv.get("carry_forward_invariant_ok")
                        == row.get("invariant_ok")
                        and inv.get("write_receipts") == row.get("receipts")
                        and smoke_matches):
                    return False, (
                        f"replay window run_id {run_id!r} canonical report "
                        "does not match its claimed window row")
    except DreamCycleError as exc:
        return False, f"replay store unreadable or invalid: {exc}"
    return True, ("every replay window run/report/event verified against "
                  "the replay store")


def _replay_store_current_invariants(replay_store_path: Path | str
                                     ) -> tuple[bool, str]:
    """Independent (not JSON-trusted) receipt/leakage check of the replay
    store's CURRENT state, mirroring the shadow store's own check."""
    try:
        with ContinuityStore(Path(replay_store_path),
                             read_only=True) as store:
            receipts = store.counts()["write_receipts"]
            leakage = _hot_memory_task_leakage(store)
    except DreamCycleError as exc:
        return False, f"replay store unreadable or invalid: {exc}"
    ok = (receipts == 0 and leakage["task_candidates_in_hot_memory"] == 0
          and leakage["promoted_candidates"] == 0)
    return ok, ("replay store currently shows zero write receipts, zero "
               "promoted candidates, zero task fields in hot memory" if ok
               else f"replay store violates invariants: receipts="
               f"{receipts}, {leakage}")


def _canonical_replay_summary(replay_store_path: Path | str, *,
                              profile: str | None, start_date: object,
                              end_date: object) -> dict[str, Any] | None:
    """The durable canonical replay summary, hash-verified against its own
    dedicated attestation event (codex phase-4 fourth review finding 1).

    A caller-supplied `replay_summary` dict, or a summary file edited
    directly on disk, is exactly the trust gap this closes: shape-valid
    JSON can claim any `rerun_zero_delta`/`rerun_row_delta`/
    `read_roots_unchanged`/aggregate-invariant outcome. This instead reads
    the summary bytes actually on disk (derived from the replay store's own
    `reports` directory plus profile/start/end — never a caller-supplied
    path) and requires their sha256 to match a `summary_sha256` recorded in
    a dedicated event for the SAME profile/start/end. Neither an edited
    dict nor an edited file can reproduce that hash, so both fail closed
    (return None) rather than being trusted.
    """
    if (not profile or not isinstance(start_date, str)
            or not isinstance(end_date, str)):
        return None
    reports_dir = Path(replay_store_path).parent / "reports"
    path = reports_dir / f"historical-replay-{start_date}_{end_date}.json"
    try:
        if path.is_symlink() or not path.is_file():
            return None
        raw = path.read_bytes()
    except OSError:
        return None
    digest = hashlib.sha256(raw).hexdigest()
    try:
        with ContinuityStore(Path(replay_store_path),
                             read_only=True) as store:
            trusted = _trusted_replay_summary_hash(
                store._conn, profile=profile, start_date=start_date,
                end_date=end_date)
    except DreamCycleError:
        return None
    if trusted is None or trusted != digest:
        return None
    try:
        parsed = json.loads(raw)
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None


def evaluate_cutover_gate(*, store_path: Path | str,
                          replay_summary: dict[str, Any],
                          replay_store_path: Path | str,
                          shadow_report: dict[str, Any],
                          accept_replay_as_operational: bool = False,
                          required_operational_days: int =
                          REQUIRED_OPERATIONAL_DAYS,
                          min_retrieval_rate: float =
                          MIN_RETRIEVAL_SUCCESS_RATE) -> dict[str, Any]:
    """Pass/fail for the Phase 4 hard invariants.

    Cutover is always refused until at least seven distinct successful
    operational dates are evidenced in durable state. Historical replay
    never satisfies elapsed operational days. The legacy keyword arguments
    remain source-compatible, but replay equivalence is ignored and a caller
    cannot lower the immutable seven-day minimum.

    `replay_store_path` is the confined continuity store the seven-window
    replay actually ran against (see `run_historical_replay`): every window
    row is joined to a real recorded run and hash-verified canonical report
    there, never trusted from `replay_summary` JSON alone. Likewise, the
    current shadow report's invariants/smoke are read from a hash-verified
    canonical report in `store_path`, not from the `shadow_report` dict —
    an edited file or dict can neither fabricate a run nor fake a passing
    outcome (codex phase-4 finding 1).
    """
    del accept_replay_as_operational
    required_operational_days = max(REQUIRED_OPERATIONAL_DAYS,
                                    required_operational_days)
    checks: dict[str, dict[str, Any]] = {}

    def check(name: str, ok: bool, detail: str) -> None:
        checks[name] = {"ok": bool(ok), "detail": detail}

    windows = (replay_summary.get("windows")
               if isinstance(replay_summary, dict) else None)
    window_rows_ok = False
    # Every row must be a dict BEFORE any `.get()` runs against it — a
    # malformed (non-dict) row must yield a structured failing verdict,
    # never raise (codex phase-4 fourth review finding 2).
    if (isinstance(windows, list) and len(windows) == REPLAY_WINDOW_COUNT
            and all(isinstance(row, dict) for row in windows)):
        try:
            start = date_type.fromisoformat(
                str(replay_summary.get("start_date")))
            end = date_type.fromisoformat(str(replay_summary.get("end_date")))
            expected_dates = [
                (start + timedelta(days=i)).isoformat()
                for i in range(REPLAY_WINDOW_COUNT)
            ]
            window_rows_ok = (
                end == start + timedelta(days=REPLAY_WINDOW_COUNT)
                and [row.get("date") for row in windows] == expected_dates
                and len({row.get("run_id") for row in windows})
                == REPLAY_WINDOW_COUNT
                and all(
                    isinstance(row.get("run_id"), str)
                    and bool(row.get("run_id"))
                    and isinstance(row.get("sources"), int)
                    and row.get("sources", -1) >= 0
                    and isinstance(row.get("excluded"), int)
                    and row.get("excluded", -1) >= 0
                    and row.get("invariant_ok") is True
                    and row.get("rerun_zero_delta") is True
                    and row.get("rerun_row_delta") == {}
                    and row.get("receipts") == 0
                    and row.get("retrieval_smoke") == "pass"
                    for row in windows)
            )
        except (TypeError, ValueError):
            window_rows_ok = False
    summary_ok = (isinstance(replay_summary, dict)
                  and replay_summary.get("kind") == REPLAY_SUMMARY_KIND
                  and replay_summary.get("historical_replay") is True
                  and isinstance(replay_summary.get("profile"), str)
                  and bool(replay_summary.get("profile"))
                  and window_rows_ok)
    check("replay_summary_shape", summary_ok,
          "seven-window historical replay summary present and well-formed"
          if summary_ok else "replay summary missing, wrong kind, or not "
          "seven windows")

    # rerun_zero_delta / rerun_row_delta / read_roots_unchanged / aggregate
    # invariants and rate are otherwise editable assertion-only JSON (codex
    # phase-4 fourth review finding 1): trust them only once the WHOLE
    # caller-supplied summary reproduces a hash-verified canonical summary
    # published under the replay store.
    canonical_summary = _canonical_replay_summary(
        replay_store_path,
        profile=(replay_summary.get("profile")
                if isinstance(replay_summary, dict) else None),
        start_date=(replay_summary.get("start_date")
                   if isinstance(replay_summary, dict) else None),
        end_date=(replay_summary.get("end_date")
                 if isinstance(replay_summary, dict) else None))
    authentic = (isinstance(replay_summary, dict)
                and canonical_summary is not None
                and canonical_summary == replay_summary)
    check("replay_summary_authenticity", authentic,
          "replay summary bytes hash-verified against a recorded event in "
          "the replay store and match the caller-supplied summary exactly"
          if authentic else "replay summary is unauthenticated, missing "
          "its recorded hash event, or does not match the canonical bytes "
          "on disk in the replay store")

    inv = (replay_summary.get("invariants")
           if isinstance(replay_summary, dict) else None) or {}
    replay_inv_ok = summary_ok and authentic and all(
        inv.get(key) is True for key in (
            "seven_contiguous_windows", "distinct_run_ids",
            "all_disposition_invariants_ok", "all_reruns_zero_delta",
            "zero_write_receipts", "read_roots_unchanged",
            "zero_live_destination_writes",
            "zero_task_fields_in_hot_memory"))
    check("replay_invariants", replay_inv_ok,
          "disposition/zero-delta/zero-receipt/read-only/hot-memory "
          "invariants all hold" if replay_inv_ok
          else "one or more replay hard invariants failed")

    rate = inv.get("retrieval_success_rate")
    rate_ok = (authentic and isinstance(rate, (int, float))
              and rate >= min_retrieval_rate)
    check("retrieval_success_rate", rate_ok,
          f"retrieval smoke success rate {rate!r} vs required "
          f">= {min_retrieval_rate}")

    replay_summary_profile = (replay_summary.get("profile")
                              if isinstance(replay_summary, dict) else None)
    linked_ok, linked_detail = _verify_replay_windows_against_store(
        replay_store_path, profile=replay_summary_profile, windows=windows)
    check("replay_windows_linked_to_store", linked_ok, linked_detail)

    replay_current_ok, replay_current_detail = \
        _replay_store_current_invariants(replay_store_path)
    check("replay_store_current_invariants", replay_current_ok,
          replay_current_detail)

    profile = (shadow_report.get("profile")
               if isinstance(shadow_report, dict) else None)
    replay_profile = (replay_summary.get("profile")
                      if isinstance(replay_summary, dict) else None)
    profile_ok = bool(profile) and replay_profile == profile
    check("profile_agreement", profile_ok,
          f"replay summary and shadow report agree on profile {profile!r}"
          if profile_ok else
          f"replay profile {replay_profile!r} does not match shadow report "
          f"profile {profile!r}")

    shadow_run_id = (shadow_report.get("run_id")
                     if isinstance(shadow_report, dict) else None)

    # The report's own claims are necessary but not sufficient: the gate
    # also measures the CURRENT store directly and requires the report's
    # run to actually exist there for the same profile, so stale or
    # hand-edited JSON cannot pass.
    with ContinuityStore(Path(store_path), read_only=True) as store:
        current_receipts = store.counts()["write_receipts"]
        current_leakage = _hot_memory_task_leakage(store)
        run_row = None
        if isinstance(shadow_run_id, str) and shadow_run_id:
            run_row = store._conn.execute(
                "SELECT profile, manifest_fingerprint, window_start, "
                "window_end FROM runs WHERE run_id = ?",
                (shadow_run_id,)).fetchone()
        canonical_shadow_report = None
        if isinstance(shadow_run_id, str) and shadow_run_id and profile:
            canonical_shadow_report = _canonical_report_for_run(
                store._conn, Path(store_path).parent / "reports",
                run_id=shadow_run_id, profile=profile,
                historical_replay=False)

    check("shadow_report_authenticity", canonical_shadow_report is not None,
          "shadow cycle report bytes verified against the sha256 recorded "
          "in its own runtime_cycle_completed event" if canonical_shadow_report
          is not None else "no hash-verified canonical report/event links "
          "this run_id to a durable publication for this profile — an "
          "edited report file or dict cannot substitute for it")

    # Invariant/smoke outcomes come ONLY from the hash-verified canonical
    # report, never from the caller-supplied shadow_report dict (codex
    # phase-4 finding 1): editing those fields in the dict has no effect.
    canonical_inv = ((canonical_shadow_report or {}).get("invariants") or {})
    canonical_smoke = ((canonical_shadow_report or {}).get("retrieval_smoke")
                      or {})
    shadow_ok = (isinstance(shadow_report, dict)
                 and shadow_report.get("kind") == CYCLE_REPORT_KIND
                 and shadow_report.get("mode") == "shadow"
                 and run_row is not None
                 and run_row["profile"] == profile
                 and run_row["manifest_fingerprint"]
                 == shadow_report.get("manifest_fingerprint")
                 and isinstance(shadow_report.get("window"), dict)
                 and run_row["window_start"]
                 == shadow_report["window"].get("start")
                 and run_row["window_end"]
                 == shadow_report["window"].get("end")
                 and canonical_shadow_report is not None
                 and canonical_inv.get("carry_forward_invariant_ok") is True
                 and canonical_inv.get("write_receipts") == 0
                 and canonical_inv.get("task_candidates_in_hot_memory") == 0
                 and canonical_inv.get("promoted_candidates") == 0
                 and canonical_smoke.get("configured") is True
                 and canonical_smoke.get("ok") is True)
    check("shadow_cycle_report", shadow_ok,
          "current shadow cycle report is well-formed, linked to a run "
          "recorded in this store, with all invariants and a passing "
          "retrieval smoke, verified from the hash-authenticated canonical "
          "report" if shadow_ok
          else "shadow cycle report missing, wrong shape, not linked to a "
          "recorded run for this profile, or failing an invariant/smoke in "
          "the hash-authenticated canonical report")

    current_ok = (current_receipts == 0
                  and current_leakage["task_candidates_in_hot_memory"] == 0
                  and current_leakage["promoted_candidates"] == 0)
    check("store_current_invariants", current_ok,
          "current store shows zero write receipts, zero promoted "
          "candidates, zero task fields in hot memory" if current_ok
          else f"current store violates invariants: receipts="
          f"{current_receipts}, {current_leakage}")

    evidenced = _distinct_operational_dates(Path(store_path),
                                            profile=profile)
    days_ok = evidenced >= required_operational_days
    operational = {
        "ok": days_ok,
        "detail": (f"{evidenced} distinct successful operational date(s) "
                   f"evidenced in durable state; {required_operational_days} "
                   "required"),
    }
    checks["operational_days"] = operational

    statement = (
        "Historical replay does not by itself satisfy the design's seven "
        f"elapsed daily operational cycles; {evidenced} distinct successful "
        "operational date(s) are evidenced in durable state.")

    return {
        "schema_version": RUNTIME_SCHEMA_VERSION,
        "kind": CUTOVER_GATE_KIND,
        "pass": all(c["ok"] for c in checks.values()),
        "checks": checks,
        "operational_days_evidenced": evidenced,
        "required_operational_days": required_operational_days,
        "replay_equivalence_override": False,
        "statement": statement,
    }
