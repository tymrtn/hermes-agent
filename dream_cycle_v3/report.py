"""Machine-readable run reports (design §3: reports/<run_id>.json).

The report is compact JSON derived entirely from the continuity store plus
the in-memory carry-forward result; any human summary is a presentation layer
on top of this, never a second source of truth.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from .canonical import canonical_json
from .errors import DreamCycleError
from .store import ContinuityStore

REPORT_SCHEMA_VERSION = 1


def _group(store: ContinuityStore, sql: str, params: tuple = ()) -> dict[str, int]:
    return {str(r[0]): r[1] for r in store._conn.execute(sql, params)}


def build_run_report(store: ContinuityStore, run_id: str, *,
                     generated_at: str,
                     carry_forward: dict[str, Any] | None = None,
                     idempotency: dict[str, Any] | None = None,
                     routing: dict[str, int] | None = None) -> dict[str, Any]:
    run = store._conn.execute(
        "SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
    if run is None:
        raise ValueError(f"unknown run_id {run_id}")

    disposition_date = (carry_forward or {}).get("disposition_date")
    dispositions_by_action: dict[str, int] = {}
    if disposition_date:
        dispositions_by_action = _group(
            store, "SELECT action, COUNT(*) FROM thread_dispositions "
                   "WHERE disposition_date = ? GROUP BY action ORDER BY action",
            (disposition_date,))

    done_without_proof = store._conn.execute(
        "SELECT COUNT(*) AS c FROM threads WHERE state='done' AND closure_proof IS NULL"
    ).fetchone()["c"]

    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "kind": "dream-cycle-v3-run-report",
        "run_id": run_id,
        "profile": run["profile"],
        "window": {"start": run["window_start"], "end": run["window_end"]},
        "collector_version": run["collector_version"],
        "manifest_fingerprint": run["manifest_fingerprint"],
        "generated_at": generated_at,
        "store_counts": store.counts(),
        "candidates": {
            "by_status": _group(store,
                                "SELECT status, COUNT(*) FROM candidates "
                                "GROUP BY status ORDER BY status"),
            "by_class": _group(store,
                               "SELECT class, COUNT(*) FROM candidates "
                               "GROUP BY class ORDER BY class"),
        },
        "threads": {
            "by_state": _group(store, "SELECT state, COUNT(*) FROM threads "
                                      "GROUP BY state ORDER BY state"),
            "needs_link": store._conn.execute(
                "SELECT COUNT(*) AS c FROM threads WHERE link_disposition = "
                "'needs_link'").fetchone()["c"],
        },
        "dispositions": {
            "date": disposition_date,
            "by_action": dispositions_by_action,
        },
        "adapters": [
            {"adapter": r["adapter"], "source_locator": r["source_locator"],
             "status": r["status"], "detail": r["detail"],
             "items": len(json.loads(r["items"]))}
            for r in store.adapter_snapshots_for_run(run_id)
        ],
        "carry_forward": carry_forward,
        "idempotency": idempotency,
        "routing": routing,
        "invariants": {
            "every_selected_thread_dispositioned":
                bool((carry_forward or {}).get("invariant_ok")),
            "done_threads_without_closure_proof": done_without_proof,
        },
    }


def create_file_exclusive(path: Path, data: bytes) -> None:
    """Create *path* fresh and write *data* verbatim in binary mode.

    Uses O_CREAT|O_EXCL (plus O_NOFOLLOW where available) so a file or
    symlink already sitting at *path* — planted between a caller's
    existence check and this call — can never be followed or silently
    overwritten; the write fails closed with a typed error instead. Binary
    mode means the bytes on disk are exactly *data*, with no text-mode
    newline translation (the codex phase-4 final review Windows caveat:
    default text-mode writes turn a trailing `\\n` into `\\r\\n` on native
    Windows, desyncing any digest computed from the in-memory buffer).
    """
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags, 0o644)
    except FileExistsError as exc:
        raise DreamCycleError(
            f"{path} already exists; refusing to overwrite") from exc
    except OSError as exc:
        raise DreamCycleError(f"cannot create {path}: {exc}") from exc
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
    except OSError as exc:
        raise DreamCycleError(f"cannot write {path}: {exc}") from exc


def write_report(report: dict[str, Any], reports_dir: Path) -> Path:
    reports_dir.mkdir(parents=True, exist_ok=True)
    path = reports_dir / f"{report['run_id']}.json"
    create_file_exclusive(path, (canonical_json(report) + "\n").encode("utf-8"))
    return path
