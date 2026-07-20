"""Read-only Todoist adapter.

Two sources, in priority order:
1. An exported JSON file (REST-v2 task array, or an object with an "items"
   list in sync-export shape) — no network at all.
2. The REST API via GET https://api.todoist.com/rest/v2/tasks with a caller-
   provided token. The HTTP layer is injectable and only ever issues GET.

No token and no export -> typed 'unavailable'. Malformed export -> typed
'error'. Nothing raises for environmental problems; nothing can write.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable

from .base import AdapterResult, TaskItem

ADAPTER_NAME = "todoist"
API_URL = "https://api.todoist.com/rest/v2/tasks"

HttpGet = Callable[[str, dict[str, str]], str]


def _default_http_get(url: str, headers: dict[str, str]) -> str:
    req = urllib.request.Request(url, headers=headers, method="GET")
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read().decode("utf-8")


def _items_from_payload(payload: Any, locator: str) -> list[TaskItem]:
    if isinstance(payload, dict) and isinstance(payload.get("items"), list):
        rows = payload["items"]
    elif isinstance(payload, list):
        rows = payload
    else:
        raise ValueError("expected a task array or an object with 'items'")
    items: list[TaskItem] = []
    for row in rows:
        if not isinstance(row, dict) or "id" not in row:
            raise ValueError("task entries must be objects with an 'id'")
        completed = bool(row.get("is_completed") or row.get("checked"))
        items.append(TaskItem(
            item_id=str(row["id"]),
            ref=f"todoist:{row['id']}",
            title=str(row.get("content", "")),
            state="closed" if completed else "open",
            status_raw="completed" if completed else "active",
            assignee=str(row["assignee_id"]) if row.get("assignee_id") else None,
            updated_at=row.get("updated_at") or row.get("completed_at"),
            url=row.get("url"),
        ))
    return items


def read_todoist_tasks(*, export_path: str | Path | None = None,
                       api_token: str | None = None,
                       http_get: HttpGet | None = None,
                       confine_home: str | Path | None = None) -> AdapterResult:
    """With *confine_home* (the Phase 3 per-profile read paths pass their
    profile root), the export rides the same confinement as stores, project
    docs, and skills: it must resolve below the home with no symlink
    crossing, and the bytes are read without following a final symlink.
    Without it (explicit operator-configured runtime paths) the plain read
    applies unchanged."""
    if export_path is not None:
        path = Path(export_path).expanduser()
        locator = str(path)
        if not path.is_file():
            return AdapterResult.unavailable(ADAPTER_NAME, locator,
                                             "export_not_found")
        try:
            if confine_home is not None:
                from ..errors import DreamCycleError
                from ..project_docs import confined_read_bytes
                try:
                    raw = confined_read_bytes(Path(confine_home), path
                                              ).decode("utf-8")
                except DreamCycleError:
                    return AdapterResult.unavailable(
                        ADAPTER_NAME, locator, "export_not_confined")
            else:
                raw = path.read_text(encoding="utf-8")
            payload = json.loads(raw)
            return AdapterResult.ok(ADAPTER_NAME, locator,
                                    _items_from_payload(payload, locator))
        except (OSError, ValueError) as exc:
            return AdapterResult.error(ADAPTER_NAME, locator,
                                       f"export_parse_failed:{type(exc).__name__}:{exc}")

    if api_token:
        locator = API_URL
        headers = {"Authorization": f"Bearer {api_token}"}
        try:
            body = (http_get or _default_http_get)(API_URL, headers)
            payload = json.loads(body)
            return AdapterResult.ok(ADAPTER_NAME, locator,
                                    _items_from_payload(payload, locator))
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            return AdapterResult.unavailable(ADAPTER_NAME, locator,
                                             f"api_unreachable:{type(exc).__name__}")
        except ValueError as exc:
            return AdapterResult.error(ADAPTER_NAME, locator,
                                       f"api_parse_failed:{type(exc).__name__}:{exc}")

    return AdapterResult.unavailable(ADAPTER_NAME, "<unconfigured>",
                                     "no_export_path_and_no_api_token")
