"""Open-thread tools — agents add/list/update/abandon AFK-safe threads.

These tools form the agent-facing surface for the open-thread ledger
defined in :mod:`gateway.open_threads`. They are intentionally
unconditional (registered in toolset ``open_threads`` with no env gating)
so any agent in any context can record a follow-up. The cron-driven
runner reads the same ledger via ``scripts/afk_open_threads_check.py``.

Tonight's MVP only allows cron to *execute* threads whose ``safety`` is
``safe`` and whose ``side_effects`` are a subset of
:data:`gateway.open_threads.ALLOWED_SIDE_EFFECTS`. Anything else is still
recordable — it just won't be auto-picked.
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any, Optional

from gateway import open_threads
from tools.registry import registry, tool_error, tool_result

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _resolve_session_key(explicit: Optional[str]) -> Optional[str]:
    """Prefer explicit arg; fall back to gateway session, then env."""
    if explicit:
        return str(explicit)
    try:
        from gateway.session_context import get_session_env
        env_key = get_session_env("HERMES_SESSION_KEY")
        if env_key:
            return env_key
    except Exception:
        pass
    return os.environ.get("HERMES_SESSION_KEY") or None


def _thread_to_dict(t: open_threads.OpenThread) -> dict:
    return t.to_dict()


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------

def _handle_add(args: dict, **_kw) -> str:
    title = args.get("title") or ""
    if not str(title).strip():
        return tool_error("title is required")
    description = args.get("description") or ""
    safety = args.get("safety") or "safe"
    side_effects = args.get("side_effects") or []
    if isinstance(side_effects, str):
        side_effects = [side_effects]
    if not isinstance(side_effects, list):
        return tool_error(
            f"side_effects must be a list of strings, got {type(side_effects).__name__}"
        )
    try:
        thread = open_threads.add(
            title=str(title),
            description=str(description),
            safety=str(safety),
            side_effects=[str(s) for s in side_effects],
            session_key=_resolve_session_key(args.get("session_key")),
            created_by=os.environ.get("HERMES_PROFILE") or "agent",
        )
    except ValueError as e:
        return tool_error(str(e))
    except Exception as e:
        logger.exception("open_thread_add failed")
        return tool_error(f"open_thread_add: {e}")
    return tool_result(
        success=True,
        thread_id=thread.id,
        thread=_thread_to_dict(thread),
        eligible_for_afk=thread.is_eligible(),
    )


def _handle_list(args: dict, **_kw) -> str:
    status = args.get("status", "open")
    if status in ("", None, "all"):
        status = None
    try:
        threads = open_threads.list_threads(status=status)
    except Exception as e:
        logger.exception("open_thread_list failed")
        return tool_error(f"open_thread_list: {e}")
    return tool_result(
        success=True,
        count=len(threads),
        threads=[_thread_to_dict(t) for t in threads],
    )


def _handle_update(args: dict, **_kw) -> str:
    thread_id = args.get("thread_id")
    if not thread_id:
        return tool_error("thread_id is required")
    status = args.get("status")
    result_summary = args.get("result_summary")
    safety = args.get("safety")
    side_effects = args.get("side_effects")
    if side_effects is not None:
        if isinstance(side_effects, str):
            side_effects = [side_effects]
        if not isinstance(side_effects, list):
            return tool_error(
                f"side_effects must be a list of strings, got {type(side_effects).__name__}"
            )
        side_effects = [str(s) for s in side_effects]
    try:
        thread = open_threads.update(
            str(thread_id),
            status=str(status) if status else None,
            result_summary=str(result_summary) if result_summary is not None else None,
            safety=str(safety) if safety else None,
            side_effects=side_effects,
        )
    except ValueError as e:
        return tool_error(str(e))
    except Exception as e:
        logger.exception("open_thread_update failed")
        return tool_error(f"open_thread_update: {e}")
    if thread is None:
        return tool_error(f"thread {thread_id} not found")
    return tool_result(success=True, thread=_thread_to_dict(thread))


def _handle_abandon(args: dict, **_kw) -> str:
    thread_id = args.get("thread_id")
    if not thread_id:
        return tool_error("thread_id is required")
    reason = args.get("reason")
    try:
        thread = open_threads.abandon(
            str(thread_id),
            reason=str(reason) if reason else None,
        )
    except Exception as e:
        logger.exception("open_thread_abandon failed")
        return tool_error(f"open_thread_abandon: {e}")
    if thread is None:
        return tool_error(f"thread {thread_id} not found")
    return tool_result(success=True, thread=_thread_to_dict(thread))


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

_ALLOWED_SE = sorted(open_threads.ALLOWED_SIDE_EFFECTS)
_BLOCKED_SE = sorted(open_threads.BLOCKED_SIDE_EFFECTS)

OPEN_THREAD_ADD_SCHEMA = {
    "name": "open_thread_add",
    "description": (
        "Record an AFK-safe open thread (a follow-up the agent can pick "
        "up while the user is away). Examples: research a question, "
        "summarize a long doc, leave an internal note, search the "
        f"codebase for a pattern. Allowed side_effects: {_ALLOWED_SE}. "
        f"Blocked side_effects (never auto-run): {_BLOCKED_SE}. Threads "
        "with safety='safe' AND side_effects within the allowed set are "
        "eligible for the cron-driven AFK runner; everything else is "
        "still stored but won't be picked up automatically."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "title": {
                "type": "string",
                "description": "Short title (required).",
            },
            "description": {
                "type": "string",
                "description": (
                    "Full description: what to do, why, and what 'done' "
                    "looks like. The cron agent reads this fresh, so be "
                    "self-contained — no 'as we discussed' references."
                ),
            },
            "safety": {
                "type": "string",
                "enum": ["safe", "needs_review", "blocked"],
                "description": (
                    "'safe' = cron may auto-run. 'needs_review' = recorded "
                    "for the user to review later. 'blocked' = never "
                    "auto-run."
                ),
            },
            "side_effects": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Tags describing what executing this thread does. "
                    f"Allowed for auto-run: {_ALLOWED_SE}. Blocked: "
                    f"{_BLOCKED_SE}. Threads with any blocked tag are "
                    "force-downgraded to safety='blocked'."
                ),
            },
        },
        "required": ["title"],
    },
}

OPEN_THREAD_LIST_SCHEMA = {
    "name": "open_thread_list",
    "description": (
        "List open threads in the current profile's ledger. Use "
        "status='open' (default) to see what's pending; 'all' to see "
        "everything including done/blocked/abandoned."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "status": {
                "type": "string",
                "description": (
                    "Filter by status. One of open/in_progress/done/"
                    "blocked/abandoned, or 'all' for no filter. Defaults "
                    "to 'open'."
                ),
            },
        },
        "required": [],
    },
}

OPEN_THREAD_UPDATE_SCHEMA = {
    "name": "open_thread_update",
    "description": (
        "Update an open thread — typically called by the AFK cron agent "
        "after it finishes work, to mark the thread done/blocked and "
        "leave a result_summary the human sees on the next return."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "thread_id": {
                "type": "string",
                "description": "Thread id (from open_thread_add or _list).",
            },
            "status": {
                "type": "string",
                "enum": ["open", "in_progress", "done", "blocked", "abandoned"],
                "description": "New status.",
            },
            "result_summary": {
                "type": "string",
                "description": (
                    "1-3 sentence handoff describing what was found / "
                    "done. Surfaced to the user when they next return."
                ),
            },
            "safety": {
                "type": "string",
                "enum": ["safe", "needs_review", "blocked"],
                "description": "Adjust the safety tag.",
            },
            "side_effects": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Replace the side_effects list.",
            },
        },
        "required": ["thread_id"],
    },
}

OPEN_THREAD_ABANDON_SCHEMA = {
    "name": "open_thread_abandon",
    "description": (
        "Abandon an open thread — short-circuit for 'not worth doing' or "
        "'no longer relevant'. Stores the reason as the result_summary."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "thread_id": {
                "type": "string",
                "description": "Thread id (from open_thread_add or _list).",
            },
            "reason": {
                "type": "string",
                "description": "Why this thread is being abandoned.",
            },
        },
        "required": ["thread_id"],
    },
}


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

registry.register(
    name="open_thread_add",
    toolset="open_threads",
    schema=OPEN_THREAD_ADD_SCHEMA,
    handler=_handle_add,
    emoji="📌",
)

registry.register(
    name="open_thread_list",
    toolset="open_threads",
    schema=OPEN_THREAD_LIST_SCHEMA,
    handler=_handle_list,
    emoji="📋",
)

registry.register(
    name="open_thread_update",
    toolset="open_threads",
    schema=OPEN_THREAD_UPDATE_SCHEMA,
    handler=_handle_update,
    emoji="✏️",
)

registry.register(
    name="open_thread_abandon",
    toolset="open_threads",
    schema=OPEN_THREAD_ABANDON_SCHEMA,
    handler=_handle_abandon,
    emoji="🗑",
)
