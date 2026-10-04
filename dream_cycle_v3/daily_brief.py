"""The morning brief a Dream Cycle cron job delivers — rendered, not generated.

A presentation layer over two records the cycle already wrote: the cycle report and THIS attempt's
conversation-compression record. It adds no model call: every line is either a section of the
handoff the gateway's compressor wrote tonight (``gateway/scheduled_compression.py``), the final
assistant reply that handoff retained verbatim, a registered thread's recorded state change, or the
compression outcome. Nothing is inferred, classified or prioritized here, so the brief never carries
thread IDs or priorities the records do not. Every recorded string passes the shared sanitizers and
a presentation filter at this boundary, so run counters, internal paths, run/session identifiers and
JSON stay in the machine report; the receipt's before/after figures are the one deliberate number.
"""
from __future__ import annotations

import re
from datetime import date
from typing import Any, Mapping

from .sanitize import INVALID, WITHHELD, sanitize_identifier, sanitize_text

# Machine output, not orientation: a JSON payload, or a status line of key=value counters.
_MACHINE_LINE = re.compile(r"^\s*[\[{]|\{\s*\"|(?:\b\w+=\d+\b.*){2}")
# Internal filesystem paths collapse to their last component; URLs are left alone.
_INTERNAL_PATH = re.compile(r"(?<![\w.:/~])~?/(?:[\w.@-]+/)+([\w.@-]+)/?")
# Run digests, session ids (20261004_063224_9e4976) and run= / session= handles.
_RUN_IDENTIFIER = re.compile(
    r"\b(?:run|run_id|session|session_id|cycle_id)=\S+|\b[0-9a-f]{16,}\b|\b\d{8}_\d{6}_[0-9a-f]{6}\b")
_WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")

# Why a conversation was left alone, in words. Unknown reasons pass through as recorded.
_REASONS = {
    "turn_running": "a turn was running",
    "compression_in_flight": "another compression was already running",
    "turn_lease_contended": "the conversation was in use",
    "reservation_invalidated": "/stop took the conversation",
    "too_short": "the conversation is too short to compress",
    "lock_skipped": "the compression lock was held",
    "nothing_to_do": "there was nothing to summarize",
    "rewrite_not_committed": "the rewrite did not commit",
}

_THREAD_ACTIONS = {
    "close_done": "its task reports completion",
    "stale_review": "routed to review",
    "continue": "follow-up window elapsed",
}


def _day(cycle_date: str) -> str:
    try:
        d = date.fromisoformat(cycle_date)
    except (TypeError, ValueError):
        return ""
    return f"{_WEEKDAYS[d.weekday()]} {d.day} {_MONTHS[d.month - 1]}"


def _presentable(value: object, limit: int = 300) -> str:
    """One recorded string as the brief may show it, or "" when it is withheld or machine output."""
    if _MACHINE_LINE.search(" ".join(str(value or "").split())):
        return ""
    text = sanitize_text(value, limit)
    if not text or text == WITHHELD:
        return ""
    text = _RUN_IDENTIFIER.sub("", _INTERNAL_PATH.sub(r"\1", text))
    return " ".join(text.split()).rstrip(":").strip()


def _ident(value: object) -> str:
    text = sanitize_identifier(value, 80)
    return "" if text in (INVALID, WITHHELD) else text


def _items(highlights: Mapping[str, Any], key: str) -> list[str]:
    """Usable lines of one handoff section."""
    values = highlights.get(key)
    if not isinstance(values, list):
        return []
    return [line for line in (_presentable(v) for v in values) if line]


def _section(title: str, lines: list[str]) -> list[str]:
    return [title, *(f"• {line}" for line in lines), ""] if lines else []


def _reason(conversation: Mapping[str, Any]) -> str:
    sessions = conversation.get("sessions") or []
    raw = next((s.get("reason") for s in sessions if isinstance(s, Mapping) and s.get("reason")),
               None) or conversation.get("reason") or ""
    return _REASONS.get(raw) or _presentable(raw, 120)


def compression_receipt(conversation: Mapping[str, Any] | None) -> str:
    """One line stating what happened to the conversation — never more than the record proves."""
    head = "🗜️ **Compression**"
    if not isinstance(conversation, Mapping) or not conversation.get("enabled"):
        return f"{head} not requested by this job"
    claim = conversation.get("claim")
    if claim == "applied":
        tokens = conversation.get("tokens") or {}
        messages = conversation.get("messages") or {}
        return (f"{head} applied · {tokens.get('before', 0):,} → {tokens.get('after', 0):,} tokens"
                f" · {messages.get('before', 0):,} → {messages.get('after', 0):,} messages")
    if claim == "already_applied":
        return f"{head} already applied earlier in this cycle; not repeated"
    if claim == "nothing_eligible":
        return f"{head} skipped — no recent conversation was eligible"
    if claim == "unavailable":
        # No answer is not proof of no rewrite: the response can be lost after the gateway acted.
        return f"{head} unavailable — no gateway confirmed an outcome"
    reason = _reason(conversation)
    if claim == "deferred":
        if conversation.get("pending"):
            return f"{head} still running when the cycle stopped waiting; outcome unknown"
        return f"{head} deferred — {reason or 'no reason recorded'}; nothing was rewritten"
    return f"{head} failed — {reason or 'no reason recorded'}; nothing reported as applied"


def _handoff_lines(highlights: Mapping[str, Any]) -> list[str]:
    structured = any(highlights.get(k) for k in
                     ("active_task", "open_items", "corrections", "latest_reply"))
    if not structured:
        # A gateway that predates the brief sections: the bullets may come from any of several
        # headings and the summary was not consolidated, so say exactly that.
        return (_section("**Handoff notes** · compacted history; may predate the final turns",
                         _items(highlights, "open_threads"))
                + _section("**Learnings**", _items(highlights, "learnings")))
    lines: list[str] = []
    active = _items(highlights, "active_task")
    if active:
        lines += ["**Now**", active[0], ""]
    lines += _section("**Open items** · as the handoff records them",
                      _items(highlights, "open_items"))
    latest = _items(highlights, "latest_reply")
    if latest:
        lines += ["**Latest reply**", latest[0], ""]
    lines += _section("📝 **Corrections & fixes**", _items(highlights, "corrections"))
    return lines


def _thread_lines(report: Mapping[str, Any]) -> list[str]:
    """Registered threads whose state CHANGED this cycle; an unchanged thread is not news."""
    threads = ((report.get("carry_forward") or {}).get("threads")) or []
    lines = []
    for thread in threads:
        if not isinstance(thread, Mapping):
            continue
        thread_id = _ident(thread.get("thread_id"))
        before, after = _ident(thread.get("state_before")), _ident(thread.get("state_after"))
        if not thread_id or before == after:
            continue
        why = _THREAD_ACTIONS.get(thread.get("action")) or _ident(thread.get("action"))
        lines.append(f"`{thread_id}` — {before or 'unrecorded'} → {after or 'unrecorded'}"
                     + (f" ({why})" if why else ""))
    return lines


def render_daily_brief(report: Mapping[str, Any],
                       conversation: Mapping[str, Any] | None) -> str:
    """The phone-readable brief for one cycle. *conversation* is THIS attempt's record."""
    profile = _ident(report.get("profile"))
    name = f"{profile[:1].upper() + profile[1:]} · " if profile else ""
    title = " · ".join(part for part in (f"🌙 **{name}Dream Cycle**", _day(report.get("cycle_date")))
                       if part)
    if report.get("mode") == "shadow":
        title += " · shadow"
    lines = [title, ""]
    highlights = {}
    if isinstance(conversation, Mapping) and conversation.get("claim") == "applied":
        highlights = conversation.get("highlights") or {}
    lines += _handoff_lines(highlights) or ["No fresh handoff this cycle.", ""]
    lines += _section("**Registered threads**", _thread_lines(report))
    lines.append(compression_receipt(conversation))
    return "\n".join(lines)
