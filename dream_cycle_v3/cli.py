"""dream-cycle-v3 CLI: collect, init-db, validate-manifest, carry-forward,
report, dry-run, and the Phase 4 runtime (run, historical-replay,
shadow-replay, seed-store, cutover-gate).

Every command requires explicit roots/paths — there are no ambient defaults
pointing at live Hermes state. Output is JSON on stdout (the `run` and
`historical-replay` commands emit one exact status line instead, for
no-agent cron scripts); failures exit non-zero with the typed error on
stderr.
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from . import COLLECTOR_VERSION
from .canonical import canonical_json
from .carry_forward import CarryForwardPolicy, run_carry_forward
from .collect import CollectionBounds, collect_to_manifest
from .context_health import audit_context_health, write_context_health
from .contracts import parse_iso_datetime, require_valid
from .dry_run import execute_dry_run
from .dry_run_phase2 import execute_phase2_dry_run
from .errors import DreamCycleError, RootResolutionError
from .manifest import load_manifest, validate_manifest
from .report import build_run_report, write_report
from .roots import CollectionRoots, prepare_output_root
from .runtime import (RuntimeConfig, _bump, _load_json_list,
                      evaluate_cutover_gate, run_cycle,
                      run_historical_replay)
from .store import ContinuityStore, assert_store_confined


def _parse_dt(value: str) -> datetime:
    try:
        dt = parse_iso_datetime(value)  # accepts 'Z' on all interpreters
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"{value!r}: {exc}") from None
    if dt.tzinfo is None:
        raise argparse.ArgumentTypeError(f"{value!r} must include a timezone")
    return dt


def _parse_root(value: str) -> tuple[str, str]:
    key, sep, path = value.partition("=")
    if not sep or not key or not path:
        raise argparse.ArgumentTypeError(
            f"root must be KEY=PATH, got {value!r}")
    return key, path


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _emit(obj: dict) -> None:
    print(canonical_json(obj))


def cmd_context_health(args: argparse.Namespace) -> int:
    report = audit_context_health(
        args.cwd,
        profile=args.profile,
        context_length=args.context_length,
    )
    if args.out:
        write_context_health(report, args.out)
    _emit(report)
    return 0 if report["pass"] else 1


def _writable_db_path(db: str, v3_root: str) -> Path:
    """Constrain writable store paths to the caller-declared v3 output root.

    Containment is the first gate; the store's application_id ownership check
    is the second — even a task database placed inside the root is refused.
    """
    root = prepare_output_root(v3_root)
    raw = Path(db).expanduser()
    resolved = (raw if raw.is_absolute() else Path.cwd() / raw).resolve()
    if not (resolved == root or resolved.is_relative_to(root)):
        raise RootResolutionError(
            f"--db {resolved} is outside the declared --v3-root {root}; "
            "writable stores must live inside the v3 output root")
    resolved.parent.mkdir(parents=True, exist_ok=True)
    return resolved


def cmd_collect(args: argparse.Namespace) -> int:
    roots = CollectionRoots.resolve(args.profile, dict(args.root))
    out_dir = prepare_output_root(args.out, forbidden_within=roots.roots)
    bounds = CollectionBounds(
        max_files_per_root=args.max_files_per_root,
        max_bytes_per_file=args.max_bytes_per_file,
        max_total_bytes=args.max_total_bytes,
        max_depth=args.max_depth,
        excerpt_chars=args.excerpt_chars,
    )
    manifest, path = collect_to_manifest(
        roots, out_dir, window_start=args.window_start,
        window_end=args.window_end, bounds=bounds, generated_at=args.as_of)
    _emit({
        "run_id": manifest["run_id"],
        "manifest_path": str(path),
        "manifest_fingerprint": manifest["manifest_fingerprint"],
        "collector_version": COLLECTOR_VERSION,
        "sources": len(manifest["sources"]),
        "excluded": len(manifest["excluded"]),
    })
    return 0


def cmd_init_db(args: argparse.Namespace) -> int:
    db_path = _writable_db_path(args.db, args.v3_root)
    with ContinuityStore(db_path) as store:
        applied = store.migrate(args.as_of.isoformat() if args.as_of else _now_iso())
        _emit({"db": str(db_path), "applied_migrations": applied,
               "schema_version": store.schema_version()})
    return 0


def cmd_validate_manifest(args: argparse.Namespace) -> int:
    try:
        raw = json.loads(Path(args.manifest).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        _emit({"valid": False, "errors": [f"unreadable: {exc}"]})
        return 1
    errors = validate_manifest(raw)
    _emit({"valid": not errors, "errors": errors})
    return 0 if not errors else 1


def cmd_carry_forward(args: argparse.Namespace) -> int:
    db_path = _writable_db_path(args.db, args.v3_root)
    with ContinuityStore(db_path) as store:
        report = run_carry_forward(
            store, run_id=args.run_id, disposition_date=args.date,
            now=args.as_of.isoformat() if args.as_of else _now_iso(),
            policy=CarryForwardPolicy(stale_after_days=args.stale_after_days),
            project_ids=args.project or None)
        _emit(report.to_dict())
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    with ContinuityStore(args.db, read_only=True) as store:
        report = build_run_report(store, args.run_id, generated_at=_now_iso())
        if args.out:
            path = write_report(report, Path(args.out))
            report["report_path"] = str(path)
        _emit(report)
    return 0


def cmd_dry_run(args: argparse.Namespace) -> int:
    workdir = args.workdir or tempfile.mkdtemp(prefix="dream-cycle-v3-dryrun-")
    report = execute_dry_run(workdir, date=args.date, as_of=args.as_of)
    _emit({
        "workdir": report["workdir"],
        "run_id": report["run_id"],
        "manifest_path": report["manifest_path"],
        "continuity_db": report["continuity_db"],
        "report_path": report["report_path"],
        "store_counts": report["store_counts"],
        "dispositions": report["dispositions"],
        "adapters": report["adapters"],
        "idempotency": report["idempotency"],
        "invariants": report["invariants"],
    })
    return 0


def cmd_dry_run_phase2(args: argparse.Namespace) -> int:
    workdir = args.workdir or tempfile.mkdtemp(prefix="dream-cycle-v3-phase2-")
    report = execute_phase2_dry_run(workdir, as_of=args.as_of)
    _emit(report)
    ok = (report["invariants"]["every_scenario_matched_expected"]
          and report["invariants"]["promoted_without_full_receipt"] == 0
          and report["idempotency"]["rerun_store_identical"]
          and report["idempotency"]["rerun_destinations_identical"]
          and not report["idempotency"]["rerun_row_delta"])
    return 0 if ok else 1


def _cycle_source_kwargs(args: argparse.Namespace) -> dict:
    """Shared runtime source/config arguments for `run` and
    `historical-replay`."""
    return dict(
        profile=args.profile,
        owner=args.owner,
        read_roots=dict(args.root),
        registry_path=args.registry,
        threads_path=args.threads,
        kanban_db=args.kanban_db,
        kanban_board=args.kanban_board,
        todoist_export=args.todoist_export,
        github_repo=args.github_repo,
        github_available=args.github_available,
        kanban_root=args.kanban_root,
        projects_home=args.projects_home,
        migrate_v2_roots=tuple(args.migrate_v2_root or ()),
        stale_after_days=args.stale_after_days,
        smoke_message=args.smoke_message,
        smoke_expected_project=args.smoke_expect_project,
        smoke_require_thread=args.smoke_require_thread,
    )


def cmd_run(args: argparse.Namespace) -> int:
    # Unpinned as_of derives deterministically from the logical window so a
    # cron retry of the identical window/config produces byte-identical
    # manifests instead of a ManifestConflictError (the success event still
    # uses the runtime's own wall clock for operational-day honesty).
    config = RuntimeConfig(
        v3_root=args.shadow if args.shadow else args.v3_root,
        mode="shadow" if args.shadow else "live",
        window_start=args.window_start,
        window_end=args.window_end,
        disposition_date=args.date,
        as_of=(args.as_of.isoformat() if args.as_of
               else args.window_end.isoformat()),
        **_cycle_source_kwargs(args),
    )
    result = run_cycle(config)
    if result.ok:
        print(result.status_line)
        return 0
    print(result.status_line, file=sys.stderr)
    return 1


def cmd_historical_replay(args: argparse.Namespace) -> int:
    result = run_historical_replay(
        start_date=args.start_date, end_date=args.end_date,
        v3_root=args.v3_root, **_cycle_source_kwargs(args))
    if result.ok:
        print(result.status_line)
        return 0
    print(result.status_line, file=sys.stderr)
    return 1


def cmd_shadow_replay(args: argparse.Namespace) -> int:
    """Accelerated historical evidence via the historical-replay engine.

    Same implementation as `historical-replay`; the output is one bounded
    JSON line that labels the result honestly — a replay compressed into
    one sitting, explicitly NOT seven operational days.
    """
    result = run_historical_replay(
        start_date=args.start_date, end_date=args.end_date,
        v3_root=args.v3_root, **_cycle_source_kwargs(args))
    summary = result.summary
    _emit({
        "command": "shadow-replay",
        "kind": summary["kind"],
        "accelerated_historical_evidence": True,
        "historical_replay": True,
        "is_operational_evidence": False,
        "label": summary["label"],
        "profile": summary["profile"],
        "start_date": summary["start_date"],
        "end_date": summary["end_date"],
        "windows": len(summary["windows"]),
        "invariants": summary["invariants"],
        "summary_path": str(result.summary_path),
        "ok": result.ok,
    })
    return 0 if result.ok else 1


_THREAD_SEMANTIC_KEYS = (
    "thread_id", "schema_version", "project_id", "external_task_ref",
    "link_disposition", "title", "normalized_next_action", "owner",
    "state", "opened_from", "last_disposition_date",
    "disposition_reason", "blocked_by", "due_hint", "follow_up_after",
    "supersedes_thread_id", "idempotency_key",
)


def _assert_thread_not_drifted(store: ContinuityStore,
                               thread: dict) -> None:
    """CAS gate: an existing thread must match the seed's semantic content.

    `open_thread` alone answers 'exists' for a matching id/idempotency key
    without comparing content, which would silently ignore an edited seed
    row. Seeding is initialization: any semantic difference is drift the
    operator must resolve, so it fails the whole batch.
    """
    row = store._conn.execute(
        "SELECT * FROM threads WHERE thread_id = ? OR idempotency_key = ?",
        (thread["thread_id"], thread["idempotency_key"])).fetchone()
    if row is None:
        return
    stored = {k: row[k] for k in _THREAD_SEMANTIC_KEYS}
    incoming = {k: thread.get(k) for k in _THREAD_SEMANTIC_KEYS}
    stored["evidence_refs"] = row["evidence_refs"]
    incoming["evidence_refs"] = canonical_json(thread["evidence_refs"])
    stored["closure_proof"] = row["closure_proof"]
    incoming["closure_proof"] = (
        canonical_json(thread["closure_proof"])
        if thread.get("closure_proof") else None)
    if stored != incoming:
        raise DreamCycleError(
            f"seed-store drift: thread {thread['thread_id']} already exists "
            "with different content; refusing to overwrite (review the seed "
            "file against the store)")


def cmd_seed_store(args: argparse.Namespace) -> int:
    """Seed a confined v3 continuity store from operator-reviewed JSON.

    Uses only the existing validated store primitives (migrate,
    upsert_project, open_thread). The entire batch is contract-validated
    before any write and applied in one transaction: a byte-identical rerun
    is a no-op, while an invalid row or semantic drift against existing
    projects/threads fails the whole seed with nothing written. The store
    must be the canonical `continuity.db` directly inside the explicit
    --v3-root; foreign, alternate, dot-component, and symlinked
    stores are refused before any byte is written. No promotion, tracker,
    or live-state path exists here.
    """
    if args.registry is None and args.threads is None:
        raise DreamCycleError(
            "seed-store requires at least one operator-reviewed input: "
            "--registry and/or --threads")
    root = prepare_output_root(args.v3_root)
    if args.db is None:
        db_path = root / "continuity.db"
    else:
        raw = Path(args.db).expanduser()
        db_path = raw if raw.is_absolute() else Path.cwd() / raw
        if db_path != root / "continuity.db":
            raise DreamCycleError(
                "seed-store --db must be exactly <v3-root>/continuity.db; "
                "alternate/nested store paths are not writable")
    assert_store_confined(db_path, root)

    now = args.as_of.isoformat()
    # whole-batch contract validation BEFORE any write
    registry_entries = (_load_json_list(Path(args.registry),
                                        "project registry")
                        if args.registry is not None else [])
    thread_entries = (_load_json_list(Path(args.threads), "thread seed")
                      if args.threads is not None else [])
    for entry in registry_entries:
        require_valid("project", entry)
    for thread in thread_entries:
        require_valid("thread", thread)

    registry_outcomes: dict[str, int] = {}
    thread_outcomes: dict[str, int] = {"inserted": 0, "exists": 0}
    with ContinuityStore(db_path) as store:
        store.migrate(now)
        with store.transaction():  # atomic batch: any failure rolls back all
            for entry in registry_entries:
                outcome = store.upsert_project(entry, now)
                if outcome == "updated":
                    raise DreamCycleError(
                        f"seed-store drift: project {entry['project_id']} "
                        "already exists with different content; refusing to "
                        "overwrite (review the registry against the store)")
                _bump(registry_outcomes, outcome)
            for thread in thread_entries:
                _assert_thread_not_drifted(store, thread)
                _bump(thread_outcomes, store.open_thread(thread, now))
        schema_version = store.schema_version()
    _emit({
        "command": "seed-store",
        "db": str(db_path),
        "v3_root": str(root),
        "as_of": now,
        "schema_version": schema_version,
        "registry": {"path": str(args.registry) if args.registry else None,
                     "outcomes": dict(sorted(registry_outcomes.items()))},
        "threads": {"path": str(args.threads) if args.threads else None,
                    "inserted": thread_outcomes.get("inserted", 0),
                    "exists": thread_outcomes.get("exists", 0)},
    })
    return 0


def _replay_store_path_from_summary(replay_summary_path: str) -> Path:
    """Derive the replay continuity store from the replay-summary artifact.

    `run_historical_replay` always writes its summary to
    `<v3_root>/reports/historical-replay-<start>_<end>.json` and the store
    to `<v3_root>/continuity.db`, so the store is a fixed sibling of the
    summary's grandparent directory — never a caller-supplied `--db`-style
    path, which would let a fabricated replay point at an unrelated store.
    """
    resolved = Path(replay_summary_path).expanduser().resolve()
    store = resolved.parent.parent / "continuity.db"
    if store.is_symlink():
        raise DreamCycleError(
            f"replay store {store} (derived from --replay-summary) is a "
            "symlink; refusing to verify replay evidence through it")
    return store


def cmd_cutover_gate(args: argparse.Namespace) -> int:
    def load(path: str, what: str) -> dict:
        try:
            payload = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise DreamCycleError(f"{what} unreadable: {exc}") from None
        if not isinstance(payload, dict):
            raise DreamCycleError(f"{what} must contain a JSON object")
        return payload

    verdict = evaluate_cutover_gate(
        store_path=Path(args.db),
        replay_summary=load(args.replay_summary, "replay summary"),
        replay_store_path=_replay_store_path_from_summary(
            args.replay_summary),
        shadow_report=load(args.shadow_report, "shadow cycle report"),
        context_cwd=Path(args.context_cwd),
        context_length=args.context_length)
    _emit(verdict)
    return 0 if verdict["pass"] else 1


def _add_cycle_source_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--profile", required=True)
    p.add_argument("--owner", required=True)
    p.add_argument("--root", action="append", required=True, type=_parse_root,
                   metavar="KEY=PATH")
    p.add_argument("--registry", help="operator-reviewed project registry JSON")
    p.add_argument("--threads", help="operator-reviewed thread seed JSON")
    p.add_argument("--kanban-db", help="read-only kanban board db to snapshot")
    p.add_argument("--kanban-board", help="board key for --kanban-db refs")
    p.add_argument("--todoist-export", help="read-only todoist export JSON")
    p.add_argument("--github-repo", help="owner/repo for read-only gh snapshot")
    p.add_argument("--github-available", action="store_true",
                   help="allow the gh CLI (default: recorded unavailable)")
    p.add_argument("--kanban-root",
                   help="shared Hermes root for wake/lookup kanban refresh")
    p.add_argument("--projects-home",
                   help="confined project map docs home for wake/lookup")
    p.add_argument("--migrate-v2-root", action="append", metavar="ROOT_KEY",
                   help="read-root key whose sources are quarantine-migrated")
    p.add_argument("--stale-after-days", type=int, default=14)
    p.add_argument("--smoke-message",
                   help="first message / task reference for the retrieval "
                        "smoke gate")
    p.add_argument("--smoke-expect-project",
                   help="project id the retrieval smoke must activate")
    p.add_argument("--smoke-require-thread", action="store_true",
                   help="smoke also requires at least one relevant thread")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="dream-cycle-v3")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("collect", help="deterministic bounded collection")
    p.add_argument("--profile", required=True)
    p.add_argument("--root", action="append", required=True, type=_parse_root,
                   metavar="KEY=PATH")
    p.add_argument("--out", required=True)
    p.add_argument("--window-start", required=True, type=_parse_dt)
    p.add_argument("--window-end", required=True, type=_parse_dt)
    p.add_argument("--as-of", type=_parse_dt, default=None,
                   help="pin generated_at for reproducible manifests")
    p.add_argument("--max-files-per-root", type=int, default=64)
    p.add_argument("--max-bytes-per-file", type=int, default=65536)
    p.add_argument("--max-total-bytes", type=int, default=4_194_304)
    p.add_argument("--max-depth", type=int, default=8)
    p.add_argument("--excerpt-chars", type=int, default=700)
    p.set_defaults(func=cmd_collect)

    p = sub.add_parser("init-db", help="create/upgrade a continuity database")
    p.add_argument("--db", required=True)
    p.add_argument("--v3-root", required=True,
                   help="declared v3 output root; --db must live inside it")
    p.add_argument("--as-of", type=_parse_dt, default=None)
    p.set_defaults(func=cmd_init_db)

    p = sub.add_parser("validate-manifest", help="validate a manifest file")
    p.add_argument("--manifest", required=True)
    p.set_defaults(func=cmd_validate_manifest)

    p = sub.add_parser("carry-forward", help="daily thread dispositions")
    p.add_argument("--db", required=True)
    p.add_argument("--v3-root", required=True,
                   help="declared v3 output root; --db must live inside it")
    p.add_argument("--run-id", required=True)
    p.add_argument("--date", required=True)
    p.add_argument("--as-of", type=_parse_dt, default=None)
    p.add_argument("--stale-after-days", type=int, default=14)
    p.add_argument("--project", action="append")
    p.set_defaults(func=cmd_carry_forward)

    p = sub.add_parser("report", help="machine-readable run report")
    p.add_argument("--db", required=True)
    p.add_argument("--run-id", required=True)
    p.add_argument("--out")
    p.set_defaults(func=cmd_report)

    p = sub.add_parser("dry-run", help="full sample pipeline in a workdir")
    p.add_argument("--workdir", default=None,
                   help="defaults to a fresh temp directory")
    p.add_argument("--date", default="2026-07-11")
    p.add_argument("--as-of", default="2026-07-11T08:00:00+00:00")
    p.set_defaults(func=cmd_dry_run)

    p = sub.add_parser("dry-run-phase2",
                       help="promotion adapters end-to-end in a workdir")
    p.add_argument("--workdir", default=None,
                   help="defaults to a fresh temp directory")
    p.add_argument("--as-of", default="2026-07-12T08:00:00+00:00")
    p.set_defaults(func=cmd_dry_run_phase2)

    p = sub.add_parser("run", help="phase 4 daily cycle "
                                   "(collection/store/retrieval; no promotion)")
    _add_cycle_source_args(p)
    out = p.add_mutually_exclusive_group(required=True)
    out.add_argument("--v3-root", help="declared live v3 output root")
    out.add_argument("--shadow", metavar="SHADOW_ROOT",
                     help="isolate ALL output under this shadow root")
    p.add_argument("--window-start", required=True, type=_parse_dt)
    p.add_argument("--window-end", required=True, type=_parse_dt)
    p.add_argument("--date", required=True,
                   help="disposition date (ISO calendar date)")
    p.add_argument("--as-of", type=_parse_dt, default=None,
                   help="pin `now` explicitly; defaults to the window end "
                        "so unpinned retries stay byte-identical")
    p.set_defaults(func=cmd_run)

    replay_description = (
        "accelerated historical evidence: seven contiguous one-day windows "
        "replayed against real sources in one sitting. This is replay "
        "evidence, NOT seven operational days, and not a substitute for the "
        "fresh current-source shadow run "
        "required by the same-day controlled cutover gate.")

    p = sub.add_parser("historical-replay",
                       help="seven contiguous one-day windows over real "
                            "sources (a replay, NOT seven operational days)",
                       description=replay_description)
    _add_cycle_source_args(p)
    p.add_argument("--v3-root", required=True,
                   help="isolated replay output root")
    p.add_argument("--start-date", required=True)
    p.add_argument("--end-date", required=True,
                   help="exclusive; must be start-date + 7 days")
    p.set_defaults(func=cmd_historical_replay)

    p = sub.add_parser("shadow-replay",
                       help="accelerated historical evidence (NOT seven "
                            "operational days); labeled JSON result",
                       description=replay_description + " Shares the "
                       "historical-replay implementation; emits one bounded "
                       "labeled JSON line instead of a status line.")
    _add_cycle_source_args(p)
    p.add_argument("--v3-root", required=True,
                   help="isolated replay output root")
    p.add_argument("--start-date", required=True)
    p.add_argument("--end-date", required=True,
                   help="exclusive; must be start-date + 7 days")
    p.set_defaults(func=cmd_shadow_replay)

    p = sub.add_parser(
        "seed-store",
        help="seed a confined v3 continuity store from operator-reviewed "
             "registry/thread JSON (idempotent; no promotion)",
        description="seed a confined v3 continuity store from "
                    "operator-reviewed registry/thread JSON via the "
                    "existing validated store primitives. Idempotent; "
                    "refuses foreign, out-of-root, and symlinked stores; "
                    "no destination promotion, tracker mutation, or live "
                    "state writes.")
    p.add_argument("--v3-root", required=True,
                   help="declared v3 output root; the store must live "
                        "inside it")
    p.add_argument("--db",
                   help="store path confined to --v3-root "
                        "(default: <v3-root>/continuity.db)")
    p.add_argument("--as-of", required=True, type=_parse_dt,
                   help="explicit ISO-8601 timestamp recorded on every "
                        "seeded row/event")
    p.add_argument("--registry", help="operator-reviewed project registry "
                                      "JSON")
    p.add_argument("--threads", help="operator-reviewed thread seed JSON")
    p.set_defaults(func=cmd_seed_store)

    p = sub.add_parser(
        "context-health",
        help="audit active project context files against the real prompt cap",
        description="Enumerate only the project context sources Hermes would "
                    "load, record source hashes/sizes and the effective cap, "
                    "then build the real prompt and fail on any truncation.")
    p.add_argument("--cwd", required=True,
                   help="explicit project working directory")
    p.add_argument("--profile", required=True,
                   help="target Hermes profile whose config supplies the cap")
    p.add_argument("--context-length", type=int, required=True,
                   help="effective model context window in tokens")
    p.add_argument("--out",
                   help="write canonical JSON evidence to a new file")
    p.set_defaults(func=cmd_context_health)

    p = sub.add_parser("cutover-gate",
                       help="pass/fail hard invariants for cutover; refuses "
                            "replay-only operational evidence")
    p.add_argument("--db", required=True, help="shadow continuity.db")
    p.add_argument("--replay-summary", required=True)
    p.add_argument("--shadow-report", required=True)
    p.add_argument("--context-cwd", required=True,
                   help="project directory whose active context file must "
                        "render without truncation")
    p.add_argument("--context-length", type=int, required=True,
                   help="effective model context window in tokens")
    p.set_defaults(func=cmd_cutover_gate)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except DreamCycleError as exc:
        print(f"error[{type(exc).__name__}]: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
