"""The morning brief a Dream Cycle cron job delivers — rendered, not generated.

A presentation layer over two records the cycle already wrote: the cycle report and THIS attempt's
conversation-compression record. It adds no model call: every line is either a section of the
handoff the gateway's compressor wrote (``gateway/scheduled_compression.py``), the final assistant
reply that handoff retained verbatim, an open registered thread, or the compression outcome.

Daily threads use the agreed two-line layout: the priority dot (🔴 P0, 🟠 P1, 🟡 P2, 🔵 P3) and the
established ``P#-group/task`` code, then the state symbol (⛔ blocked, 🙋 needs help, 🔐 needs
authorization, ⏳ waiting externally, ▶️ next step in hand) and a next action of at most ten words.
Threads of one group sit together, groups ordered by their most urgent thread. Only a code the user
stated is shown as established (``agent/context_compressor_daily_threads.py`` verifies that before
the handoff is stored); proposed codes and uncoded threads are listed apart and undotted. Nothing is
inferred, classified or prioritized here. Where the records carry no priority, group or code (a
handoff without the section, the registry contract), the brief says so instead of formatting old
bullets as if they were prioritized threads.

Every recorded string passes the shared sanitizers and a presentation filter at this boundary, is
cut on a word boundary and leaves no Markdown open, so run counters, internal paths, run/session
identifiers and JSON stay in the machine report; the receipt's figures are the one deliberate number.
"""
from __future__ import annotations

import re
from datetime import date
from typing import Any, Mapping

from .sanitize import INVALID, WITHHELD, clip_words, has_raw_pii, sanitize_identifier, sanitize_text
from .secretguard import scan_content

# Machine output, not orientation: a JSON payload, or a status line of key=value counters.
_MACHINE_LINE = re.compile(r"^\s*[\[{]|\{\s*\"|(?:\b\w+=\d+\b.*){2}")
# Internal filesystem paths collapse to their last component; URLs are left alone.
_INTERNAL_PATH = re.compile(r"(?<![\w.:/~])~?/(?:[\w.@-]+/)+([\w.@-]+)/?")
# Run digests, session ids (20261004_063224_9e4976) and run= / session= handles.
_RUN_IDENTIFIER = re.compile(
    r"\b(?:run|run_id|session|session_id|cycle_id)=\S+|\b[0-9a-f]{16,}\b|\b\d{8}_\d{6}_[0-9a-f]{6}\b")
_WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
#: The pre-word-boundary gateway cut legacy handoff bullets at exactly this many characters.
_LEGACY_HARD_CUT = 160

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

_PRIORITY_DOTS = {"P0": "🔴", "P1": "🟠", "P2": "🟡", "P3": "🔵"}
_PRIORITY_RANK = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}
_STATE_SYMBOLS = {"blocked": "⛔", "needs_help": "🙋", "needs_authorization": "🔐",
                  "waiting_externally": "⏳"}
#: No dependency: the next step is in hand.
_IN_HAND = "▶️"
#: Registered threads record a state, not a dependency kind.
_REGISTERED_STATE_SYMBOLS = {"blocked": "⛔", "waiting": "⏳", "stale": "💤"}
_DESCRIPTION_MIN_WORDS, _DESCRIPTION_MAX_WORDS = 4, 10
_DESCRIPTION_SOURCE_CHARS = 1200
# Where a long description can stop and still say one whole thing.
_CLAUSE_END = re.compile(r"\s*(?:[;:,]|\s[—–-]\s)"
                         r"|\s+(?=(?:so that|because|which|while|unless|whereas|in order to)\s)",
                         re.IGNORECASE)
# Descriptions are plain text: a Markdown mark a cut could leave open is dropped before cutting.
_MD_LINK = re.compile(r"\[([^\]]+)\]\([^)\s]+\)")
_MD_EMPHASIS = re.compile(r"\*\*|__|`|(?<!\w)[*_](?=\S)|(?<=\S)[*_](?!\w)")
# Links belong in the task a thread points at, not in its ten words.
_URL = re.compile(r"https?://\S*")
_TOO_SHORT = " (no further detail recorded)"
# A description cut between clauses never ends on a word that needs the next one.
_DANGLING = frozenset("a an the and or but to of for with in on at by from before after then so if "
                      "until via into that which who when while because as than".split())


def _day(cycle_date: str) -> str:
    try:
        d = date.fromisoformat(cycle_date)
    except (TypeError, ValueError):
        return ""
    return f"{_WEEKDAYS[d.weekday()]} {d.day} {_MONTHS[d.month - 1]}"


def _balanced(text: str) -> str:
    """Drop an unpaired trailing ``**`` or backtick so a cut line never leaves Markdown open."""
    if text.count("**") % 2:
        head, _sep, tail = text.rpartition("**")
        text = head + tail
    if text.count("`") % 2:
        head, _sep, tail = text.rpartition("`")
        text = head + tail
    return text


def _presentable(value: object, limit: int = 300) -> str:
    """One recorded string as the brief may show it, or "" when it is withheld or machine output."""
    if _MACHINE_LINE.search(" ".join(str(value or "").split())):
        return ""
    text = sanitize_text(value, 4 * limit)
    if not text or text == WITHHELD:
        return ""
    text = _RUN_IDENTIFIER.sub("", _INTERNAL_PATH.sub(r"\1", text))
    text = _balanced(clip_words(" ".join(text.split()).rstrip(":").strip(), limit))
    return "" if _reveals(text) else text


def _reveals(text: str) -> bool:
    """True when edits made after the scan (a dropped backtick or Markdown mark, a collapsed path)
    rejoined a credential or an address the scan saw split: the text as shown is scanned again."""
    return bool(scan_content(text)) or has_raw_pii(text)


def _ident(value: object) -> str:
    text = sanitize_identifier(value, 80)
    return "" if text in (INVALID, WITHHELD) else text


def _items(highlights: Mapping[str, Any], key: str) -> list[str]:
    """Usable lines of one handoff section."""
    values = highlights.get(key)
    if not isinstance(values, list):
        return []
    return [line for line in (_presentable(v) for v in values) if line]


def _legacy_items(highlights: Mapping[str, Any], key: str) -> list[str]:
    """Bullets from a gateway that cut them at a fixed width, mid-word and without an ellipsis: a
    bullet of exactly that width is taken as cut and trimmed back to its last whole word."""
    values = highlights.get(key)
    if not isinstance(values, list):
        return []
    trimmed = [clip_words(v, _LEGACY_HARD_CUT - 1) if isinstance(v, str)
               and len(v) == _LEGACY_HARD_CUT else v for v in values]
    return [line for line in (_presentable(v) for v in trimmed) if line]


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
    if claim == "unchanged":
        return f"{head} skipped — nothing new said since the last compression"
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


def describe(value: object) -> str:
    """*value* as a thread's second line: four to ten whole words of plain text.

    The summarizer is asked for four to ten words; this enforces both bounds without a model call.
    A longer text keeps its longest run of leading clauses that fits, when that is four words or
    more. Otherwise it keeps the first ten words, drops a dangling connective, and says it was cut
    (…). A shorter one is not padded with invented work: it says that is all the record holds."""
    # Only the whole words of a bounded window can reach ten words, and all of it is scanned as
    # recorded: dropping Markdown marks first could shorten a credential below the scanner's
    # pattern, or pull one from past the scanned window into view.
    raw = clip_words(str(value if value is not None else ""), _DESCRIPTION_SOURCE_CHARS)
    if scan_content(raw):
        return ""
    # Markdown before the presentation filter: a line opening with a link is not machine output.
    plain = _MD_EMPHASIS.sub("", _MD_LINK.sub(r"\1", raw))
    text = " ".join(_URL.sub("", _presentable(plain, 300)).split())
    text = re.sub(r"\s*\([^)]*\)", "", text).strip(" .;:,—–-")
    if not text.strip("…"):
        return ""
    words = text.split()
    if len(words) > _DESCRIPTION_MAX_WORDS:
        ends = [m.start() for m in _CLAUSE_END.finditer(text) if _DESCRIPTION_MIN_WORDS
                <= len(text[:m.start()].split()) <= _DESCRIPTION_MAX_WORDS]
        if ends:
            text = text[:ends[-1]]
        else:
            words = words[:_DESCRIPTION_MAX_WORDS]
            while len(words) > 1 and words[-1].lower().strip(",;:") in _DANGLING:
                words.pop()
            text = " ".join(words).rstrip(",;:") + "…"
    if len(text.split()) < _DESCRIPTION_MIN_WORDS:
        text += _TOO_SHORT
    text = text[:1].upper() + text[1:]
    return "" if _reveals(text) else text


def _thread_block(head: str, thread: Mapping[str, Any]) -> list[str]:
    """The agreed two lines: *head*, then the state symbol and what happens next."""
    dependency = thread.get("dependency") if isinstance(thread.get("dependency"), Mapping) else {}
    if dependency.get("kind") in _STATE_SYMBOLS:
        symbol, text = _STATE_SYMBOLS[dependency["kind"]], describe(dependency.get("detail"))
    else:
        symbol, text = _IN_HAND, describe(thread.get("next"))
    return [head, f"{symbol} {text or 'No next step recorded'}", ""]


def _daily_thread_lines(daily: Mapping[str, Any]) -> list[str]:
    threads = [t for t in daily.get("threads") or [] if isinstance(t, Mapping)]
    established, proposed, uncoded = [], [], []
    for thread in threads:
        code = _ident(thread.get("code"))
        if thread.get("standing") == "uncoded":
            title = _presentable(thread.get("title"), 80)
            if title:
                uncoded += _thread_block(f"• {title}", thread)
        elif code and thread.get("standing") == "established" \
                and thread.get("priority") in _PRIORITY_DOTS:
            established.append((thread, code))
        elif code:
            proposed += _thread_block(f"• {code}", thread)

    lines: list[str] = []
    if not threads:
        lines += ["No open threads in the handoff.", ""]
    elif not established:
        lines += ["⚠️ No agreed P#-group/task codes yet, so nothing here is prioritized.", ""]
    groups: dict[str, list] = {}
    for thread, code in established:
        groups.setdefault(_ident(thread.get("group")).lower(), []).append((thread, code))
    for _group, members in sorted(groups.items(), key=lambda item: (
            min(_PRIORITY_RANK[t.get("priority")] for t, _ in item[1]), item[0])):
        for thread, code in sorted(members, key=lambda m: (_PRIORITY_RANK[m[0].get("priority")], m[1])):
            lines += _thread_block(f"{_PRIORITY_DOTS[thread.get('priority')]} {code}", thread)
    if proposed:
        lines += ["**Proposed** · codes and priorities not agreed yet", *proposed]
    if uncoded:
        lines += ["**No code yet**", *uncoded]
    unformatted = [line for line in (_presentable(v) for v in daily.get("unformatted") or []) if line]
    total = daily.get("unformatted_total") if isinstance(daily.get("unformatted_total"), int) else 0
    if total:
        lines += [f"⚠️ {total} handoff thread line{'s' if total != 1 else ''} "
                  f"{'were' if total != 1 else 'was'} not in the P#-group/task format:",
                  *(f"• {line}" for line in unformatted), ""]
    return lines


_NO_METADATA = ("⚠️ No priorities, groups or codes: this handoff has no daily-thread section, "
                "so its items are unprioritized.")


def _unthreaded_lines(unthreaded: object) -> list[str]:
    """Conversations whose handoff has no daily-thread section, beside others that do."""
    count = unthreaded.get("conversations") if isinstance(unthreaded, Mapping) else 0
    if not isinstance(count, int) or count < 1:
        return []
    return _section(f"⚠️ {count} conversation{'s' if count != 1 else ''} had no daily-thread "
                    f"section, so {'their' if count != 1 else 'its'} items are unprioritized:",
                    _legacy_items(unthreaded, "items")) or [
        f"⚠️ {count} conversation{'s' if count != 1 else ''} had no daily-thread section.", ""]


def _handoff_lines(highlights: Mapping[str, Any]) -> list[str]:
    daily = highlights.get("daily_threads")
    if isinstance(daily, Mapping):
        return _daily_thread_lines(daily) + _unthreaded_lines(highlights.get("unthreaded"))
    structured = any(highlights.get(k) for k in
                     ("active_task", "open_items", "corrections", "latest_reply"))
    if not structured:
        # A gateway that predates the brief sections: the bullets may come from any of several
        # headings and the summary was not consolidated, so say exactly that.
        legacy = (_section("**Handoff notes** · compacted history; may predate the final turns",
                           _legacy_items(highlights, "open_threads"))
                  + _section("**Learnings**", _legacy_items(highlights, "learnings")))
        return [_NO_METADATA, "", *legacy] if legacy else []
    lines: list[str] = [_NO_METADATA, ""]
    active = _items(highlights, "active_task")
    if active:
        lines += ["**Now**", active[0], ""]
    lines += _section("**Open items** · as the handoff records them",
                      _items(highlights, "open_items"))
    latest = _items(highlights, "latest_reply")
    if latest:
        lines += ["**Latest reply**", latest[0], ""]
    return lines


def _changes(report: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    threads = ((report.get("carry_forward") or {}).get("threads")) or []
    return {_ident(t.get("thread_id")): t for t in threads
            if isinstance(t, Mapping) and _ident(t.get("thread_id"))}


def _registered_block(thread_id: str, state: str, text: object) -> list[str]:
    symbol = _REGISTERED_STATE_SYMBOLS.get(state, _IN_HAND)
    return [f"• {thread_id}", f"{symbol} {describe(text) or 'No next step recorded'}", ""]


def _registered_lines(report: Mapping[str, Any]) -> list[str]:
    """Every open registered thread, changed tonight or not; completions announced once."""
    changes = _changes(report)
    lines: list[str] = []
    registered = report.get("registered_threads")
    if isinstance(registered, Mapping):
        projects: dict[str, list[str]] = {}
        for thread in registered.get("threads") or []:
            if not isinstance(thread, Mapping) or not _ident(thread.get("thread_id")):
                continue
            state = _ident(thread.get("state"))
            # A blocked or waiting thread's line says what it waits on, as the registry records it.
            text = (thread.get("blocked_by") if state in ("blocked", "waiting") else None) \
                or thread.get("next_action")
            project = _presentable(thread.get("project_name"), 80) or "Unassigned project"
            projects.setdefault(project, []).extend(
                _registered_block(_ident(thread.get("thread_id")), state, text))
        for project, entries in projects.items():
            lines += [f"**{project}**", *entries]
    else:
        # A report from before the projection existed: the carry-forward rows are all there is, and
        # they record a state change, not a next action.
        for thread_id, change in changes.items():
            state = _ident(change.get("state_after"))
            if change.get("action") != "close_done" and state:
                why = _THREAD_ACTIONS.get(change.get("action"))
                lines += _registered_block(thread_id, state, f"{state}, {why}" if why else state)
    lines += [f"✅ {thread_id} done ({_THREAD_ACTIONS['close_done']})"
              for thread_id, change in changes.items() if change.get("action") == "close_done"]
    if lines and lines[-1] != "":
        lines.append("")
    return ["**Registered threads** · no priority recorded", *lines] if lines else []


def render_daily_brief(report: Mapping[str, Any],
                       conversation: Mapping[str, Any] | None) -> str:
    """The phone-readable brief for one cycle. *conversation* is THIS attempt's record."""
    profile = _ident(report.get("profile"))
    profile = profile[:1].upper() + profile[1:]
    name = f"{profile} · " if profile and not _reveals(profile) else ""
    title = " · ".join(part for part in (f"🌙 **{name}Dream Cycle**", _day(report.get("cycle_date")))
                       if part)
    if report.get("mode") == "shadow":
        title += " · shadow"
    lines = [title, ""]
    highlights: Mapping[str, Any] = {}
    claim = conversation.get("claim") if isinstance(conversation, Mapping) else None
    if claim in ("applied", "unchanged", "already_applied"):
        highlights = conversation.get("highlights") or {}
    lines += _handoff_lines(highlights) or ["No fresh handoff this cycle.", ""]
    lines += _registered_lines(report)
    lines += _section("📝 **Corrections & fixes**", _items(highlights, "corrections"))
    lines.append(compression_receipt(conversation))
    return "\n".join(lines)
