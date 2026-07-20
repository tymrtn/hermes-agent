"""Read-only task-SSOT refresh shared by the Phase 3 wake and lookup paths.

Per provider, in order of preference:

- kanban: live read of the board's DB under the explicit shared Hermes root
  (canonical layout: named boards at `<root>/kanban/boards/<board>/kanban.db`,
  the special `default` board at `<root>/kanban.db`; ambient
  HERMES_KANBAN_DB/BOARD overrides are deliberately ignored — see
  kanban_layout.py).
- todoist: live read of a configured export JSON; otherwise the latest
  collector snapshot recorded in the continuity store.
- github: the latest collector snapshot (no network or subprocess calls are
  ever made at session start).

When current status cannot be established — provider unconfigured, source
unreadable, ref missing from the tracker, or the collector snapshot older
than SNAPSHOT_FRESH_DAYS — the ref is explicitly 'stale' with a
collector-health/age warning. A stale or missing tracker never closes a
thread and nothing here can write.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .adapters.kanban import read_kanban_task_states
from .adapters.todoist import read_todoist_tasks
from .contracts import parse_iso_datetime
from .kanban_layout import kanban_db_path

SNAPSHOT_FRESH_DAYS = 2
_SNAPSHOT_PROVIDERS = ("todoist", "github")


def _as_utc(dt: datetime) -> datetime:
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


@dataclass(frozen=True)
class RefState:
    state: str                 # 'open' | 'closed' | 'stale'
    source: str                # 'live' | 'snapshot' | 'none'
    age_days: float | None = None


@dataclass
class RefreshResult:
    states: dict[str, RefState] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    outage: bool = False

    def state_of(self, ref: str) -> str:
        rs = self.states.get(ref)
        return rs.state if rs else "stale"


def _snapshot_map(store, adapter: str, now: datetime
                  ) -> tuple[dict[str, str] | None, float | None]:
    """(ref -> open|closed from the latest ok snapshot, age in days)."""
    try:
        row = store.latest_adapter_snapshot(adapter)
    except Exception:
        return None, None
    if row is None or row["status"] != "ok":
        return None, None
    try:
        items = json.loads(row["items"])
        mapping = {i["ref"]: i["state"] for i in items
                   if isinstance(i, dict) and i.get("ref")
                   and i.get("state") in ("open", "closed")}
    except (ValueError, TypeError, KeyError):
        return None, None
    age_days = None
    try:
        end = _as_utc(parse_iso_datetime(row["run_window_end"]))
        age_days = (_as_utc(now) - end).total_seconds() / 86400.0
    except (ValueError, TypeError, KeyError):
        pass
    return mapping, age_days


def refresh_refs(refs: list[str], *, kanban_root: Path | None,
                 todoist_export_path: Path | None, store, now: datetime,
                 todoist_confine_home: Path | None = None) -> RefreshResult:
    result = RefreshResult()
    kanban_boards: dict[str, dict[str, str]] = {}
    kanban_status: dict[str, str] = {}
    todoist_live: dict[str, str] | None = None
    todoist_live_failed = False
    snapshots: dict[str, tuple[dict[str, str] | None, float | None]] = {}

    # Targeted refresh: collect the referenced item ids per board up front so
    # each board is queried for exactly those ids — a referenced task's
    # freshness never depends on how many other tasks the board holds.
    kanban_items: dict[str, list[str]] = {}
    for ref in refs:
        parts = (ref or "").split(":", 2)
        if (len(parts) == 3 and parts[0] == "kanban"
                and parts[1] and parts[2]):
            kanban_items.setdefault(parts[1], []).append(parts[2])

    def _snapshot(adapter: str):
        if adapter not in snapshots:
            snapshots[adapter] = _snapshot_map(store, adapter, now)
        return snapshots[adapter]

    def _warn(text: str) -> None:
        if text not in result.warnings:
            result.warnings.append(text)

    for ref in refs:
        scheme, _, rest = (ref or "").partition(":")

        if scheme == "kanban":
            parts = (ref or "").split(":", 2)
            if len(parts) != 3 or not parts[1] or not parts[2]:
                result.states[ref] = RefState("stale", "none")
                continue
            board = parts[1]
            if board not in kanban_boards:
                db_path = (kanban_db_path(kanban_root, board)
                           if kanban_root is not None else None)
                if db_path is None:
                    kanban_status[board] = "unavailable"
                    kanban_boards[board] = {}
                else:
                    status, states = read_kanban_task_states(
                        db_path, kanban_items.get(board, []),
                        board_key=board)
                    kanban_status[board] = status
                    kanban_boards[board] = states if status == "ok" else {}
            if kanban_status[board] != "ok":
                result.states[ref] = RefState("stale", "none")
                result.outage = True
                _warn("kanban tracker unavailable; thread status may be stale")
                continue
            state = kanban_boards[board].get(ref)
            # Ref absent from the tracker: evidence is inconclusive, so the
            # thread stays visible but stale — never assumed closed.
            result.states[ref] = (RefState(state, "live") if state
                                  else RefState("stale", "live"))
            continue

        if scheme == "todoist" and todoist_export_path is not None:
            if todoist_live is None and not todoist_live_failed:
                read = read_todoist_tasks(export_path=todoist_export_path,
                                          confine_home=todoist_confine_home)
                if read.status == "ok":
                    todoist_live = {i.ref: i.state for i in read.items}
                else:
                    todoist_live_failed = True
            if todoist_live is not None:
                state = todoist_live.get(ref)
                result.states[ref] = (RefState(state, "live") if state
                                      else RefState("stale", "live"))
                continue
            result.states[ref] = RefState("stale", "none")
            result.outage = True
            _warn("todoist tracker source unreadable; thread status may "
                  "be stale")
            continue

        if scheme in _SNAPSHOT_PROVIDERS:
            mapping, age_days = _snapshot(scheme)
            if mapping is None:
                result.states[ref] = RefState("stale", "none")
                result.outage = True
                _warn(f"{scheme} tracker not refreshable (no live source or "
                      "collector snapshot); thread status may be stale")
                continue
            if age_days is None or age_days > SNAPSHOT_FRESH_DAYS:
                # An old snapshot can neither prove open nor closed.
                result.states[ref] = RefState("stale", "snapshot", age_days)
                shown = ("unknown" if age_days is None
                         else f"{age_days:.0f}d")
                _warn(f"{scheme} tracker status from collector snapshot aged "
                      f"{shown}; status may be stale")
                continue
            state = mapping.get(ref)
            result.states[ref] = (RefState(state, "snapshot", age_days)
                                  if state else RefState("stale", "snapshot",
                                                         age_days))
            continue

        # Unknown or empty scheme: nothing to refresh against.
        result.states[ref] = RefState("stale", "none")

    return result
