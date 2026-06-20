"""Read-only local iMessage / SMS search tool for macOS Messages chat.db."""

from __future__ import annotations

import json
import os
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from tools.registry import registry, tool_error
from utils import env_var_enabled

APPLE_EPOCH_OFFSET = 978307200
DEFAULT_DB = Path("/Users/tylermartin/Library/Messages/chat.db")


def _db_path() -> Path:
    return Path(os.getenv("IMESSAGE_CHAT_DB", str(DEFAULT_DB))).expanduser()


def _parse_date(value: Optional[str], *, end: bool = False) -> Optional[int]:
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        text += "T23:59:59" if end else "T00:00:00"
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"Invalid date {value!r}; use YYYY-MM-DD or ISO timestamp") from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    unix = int(dt.timestamp())
    return (unix - APPLE_EPOCH_OFFSET) * 1_000_000_000


def _apple_ns_to_iso(value: Any) -> Optional[str]:
    try:
        ns = int(value or 0)
    except (TypeError, ValueError):
        return None
    if ns <= 0:
        return None
    return datetime.fromtimestamp(ns / 1_000_000_000 + APPLE_EPOCH_OFFSET, tz=timezone.utc).isoformat()


def _open_db(path: Path) -> sqlite3.Connection:
    uri = f"file:{path}?mode=ro&immutable=1"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def check_imessage_search_requirements() -> bool:
    """Opt-in only; reading Messages requires macOS Full Disk Access."""
    return env_var_enabled("IMESSAGE_SEARCH_ENABLED")


def imessage_search(
    query: Optional[str] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    handle: Optional[str] = None,
    chat_identifier: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
    task_id: Optional[str] = None,
) -> str:
    """Search local macOS Messages chat.db read-only."""
    del task_id
    path = _db_path()
    if not path.exists():
        return tool_error(f"Messages database not found at {path}", success=False)
    try:
        start_ns = _parse_date(start_date, end=False)
        end_ns = _parse_date(end_date, end=True)
        limit = max(1, min(int(limit or 50), 500))
        offset = max(0, int(offset or 0))
    except Exception as exc:
        return tool_error(str(exc), success=False)

    where = ["m.text IS NOT NULL", "m.text != ''"]
    params: list[Any] = []
    if query:
        where.append("m.text LIKE ?")
        params.append(f"%{query}%")
    if start_ns is not None:
        where.append("m.date >= ?")
        params.append(start_ns)
    if end_ns is not None:
        where.append("m.date <= ?")
        params.append(end_ns)
    if handle:
        where.append("(h.id LIKE ? OR h.uncanonicalized_id LIKE ?)")
        like = f"%{handle}%"
        params.extend([like, like])
    if chat_identifier:
        where.append("c.chat_identifier LIKE ?")
        params.append(f"%{chat_identifier}%")

    sql = f"""
        SELECT
          m.ROWID AS message_id,
          m.guid AS message_guid,
          m.date AS apple_date,
          m.is_from_me,
          m.service,
          m.text,
          h.id AS handle_id,
          h.uncanonicalized_id AS handle_uncanonicalized_id,
          c.ROWID AS chat_id,
          c.guid AS chat_guid,
          c.chat_identifier,
          c.display_name
        FROM message m
        LEFT JOIN handle h ON h.ROWID = m.handle_id
        LEFT JOIN chat_message_join cmj ON cmj.message_id = m.ROWID
        LEFT JOIN chat c ON c.ROWID = cmj.chat_id
        WHERE {' AND '.join(where)}
        ORDER BY m.date ASC, m.ROWID ASC
        LIMIT ? OFFSET ?
    """
    params.extend([limit, offset])

    try:
        with _open_db(path) as conn:
            rows = conn.execute(sql, params).fetchall()
            total = conn.execute(
                f"""
                SELECT COUNT(*)
                FROM message m
                LEFT JOIN handle h ON h.ROWID = m.handle_id
                LEFT JOIN chat_message_join cmj ON cmj.message_id = m.ROWID
                LEFT JOIN chat c ON c.ROWID = cmj.chat_id
                WHERE {' AND '.join(where)}
                """,
                params[:-2],
            ).fetchone()[0]
    except PermissionError as exc:
        return tool_error(
            f"macOS denied access to {path}. Grant Full Disk Access to the running Hermes/Python/terminal process, then restart the gateway. ({exc})",
            success=False,
        )
    except sqlite3.OperationalError as exc:
        msg = str(exc)
        if "authorization denied" in msg.lower() or "unable to open" in msg.lower():
            return tool_error(
                f"Cannot open {path}: {msg}. This usually means macOS Full Disk Access is missing for Hermes/Python.",
                success=False,
            )
        return tool_error(f"SQLite error while reading Messages: {msg}", success=False)
    except Exception as exc:
        return tool_error(f"iMessage search failed: {exc}", success=False)

    results = []
    for r in rows:
        results.append({
            "message_id": r["message_id"],
            "message_guid": r["message_guid"],
            "date": _apple_ns_to_iso(r["apple_date"]),
            "is_from_me": bool(r["is_from_me"]),
            "service": r["service"],
            "text": r["text"],
            "handle": r["handle_id"] or r["handle_uncanonicalized_id"],
            "chat_id": r["chat_id"],
            "chat_guid": r["chat_guid"],
            "chat_identifier": r["chat_identifier"],
            "display_name": r["display_name"],
        })

    return json.dumps({
        "success": True,
        "db_path": str(path),
        "total_matches": total,
        "returned": len(results),
        "offset": offset,
        "limit": limit,
        "results": results,
    }, ensure_ascii=False, indent=2)


IMESSAGE_SEARCH_SCHEMA = {
    "name": "imessage_search",
    "description": "Search local macOS Messages/iMessage/SMS history from chat.db. Read-only. Requires IMESSAGE_SEARCH_ENABLED=true and macOS Full Disk Access.",
    "parameters": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Text substring to search for. Omit to list messages by date/handle."},
            "start_date": {"type": "string", "description": "Start date, YYYY-MM-DD or ISO timestamp."},
            "end_date": {"type": "string", "description": "End date, YYYY-MM-DD or ISO timestamp."},
            "handle": {"type": "string", "description": "Filter by phone/email/iMessage handle substring."},
            "chat_identifier": {"type": "string", "description": "Filter by Messages chat_identifier substring."},
            "limit": {"type": "integer", "default": 50, "description": "Max rows, 1-500."},
            "offset": {"type": "integer", "default": 0, "description": "Pagination offset."},
        },
    },
}


registry.register(
    name="imessage_search",
    toolset="imessage",
    schema=IMESSAGE_SEARCH_SCHEMA,
    handler=lambda args, **kw: imessage_search(
        query=args.get("query"),
        start_date=args.get("start_date"),
        end_date=args.get("end_date"),
        handle=args.get("handle"),
        chat_identifier=args.get("chat_identifier"),
        limit=args.get("limit", 50),
        offset=args.get("offset", 0),
        task_id=kw.get("task_id"),
    ),
    check_fn=check_imessage_search_requirements,
    requires_env=["IMESSAGE_SEARCH_ENABLED"],
    emoji="💬",
)
