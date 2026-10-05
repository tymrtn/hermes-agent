"""``## Daily Threads``: the handoff section a nightly brief reads as structured thread state.

Only the scheduled (reconciled) summarization asks for it (``DAILY_THREADS_INSTRUCTIONS`` rides the
retained-tail block), in the daily-status format: one line per open thread,

    - P1-group/task — current state; next: next action
    - proposed P2-group/task — ...         a code the user has not agreed
    - unassigned: short title — ...        no code at all

with at most one indented dependency line (blocked / needs help / needs authorization / waiting
externally). Next actions and dependency lines are asked for in 4-10 words; the brief enforces the
ten-word ceiling (``dream_cycle_v3/daily_brief.py::describe``). Priority is shown in the code but is not the thread's identity: ``key`` is
``group/task``, so a re-prioritized thread stays the same thread.

"Established" is never the summarizer's say-so. ``verify_codes`` rewrites, as ``proposed``, every
code line whose exact code the user never wrote and no earlier verified handoff carried, before the
summary is stored. The persisted handoff, which the next update builds on, therefore only carries
codes the user stated. Deterministic; no model call.
"""

from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List, Optional, Tuple

DAILY_THREADS_HEADING = "Daily Threads"

DAILY_THREADS_INSTRUCTIONS = """

Also write a "## Daily Threads" section as of the END of these turns: one line per thread the user is actively tracking, in exactly this form:
- P1-group/task — <concrete current state>; next: <next action>
Use a P#-group/task code only when the user wrote that exact code, or it appears unchanged in the previous summary's Daily Threads. Never assign, raise or lower a priority yourself. For a code or priority the user has not agreed, start the line with "proposed " (write "P?" when no priority was stated). For a thread without a code, write "- unassigned: <short title> — <state>; next: <next action>". Under a thread, add at most one indented dependency line, only when the turns show it:
  - blocked: <exact obstacle and recovery action>
  - needs help: <what only the user can provide>
  - needs authorization: <what the approval permits>
  - waiting externally: <who or what, and the next check date if known>
Write every "next:" action and dependency line as one plain imperative of 4 to 10 words; keep reasons and consequences in the state, never there. Leave out finished or resolved threads. Write "None" when nothing is open."""

DEPENDENCY_KINDS = {"blocked": "blocked", "needs help": "needs_help",
                    "needs authorization": "needs_authorization",
                    "waiting externally": "waiting_externally"}

_DASH = r"\s+(?:—|–|--?)\s+"
_THREAD_RE = re.compile(
    r"^[-*•]\s+(?:(?P<proposed>proposed)\s+)?(?P<priority>P[0-3?])-"
    r"(?P<key>[a-z0-9][\w.-]*(?:/[\w.-]+)+)" + _DASH + r"(?P<rest>\S.*)$", re.IGNORECASE)
_UNCODED_RE = re.compile(r"^[-*•]\s+unassigned:\s*(?P<title>.+?)" + _DASH + r"(?P<rest>\S.*)$",
                         re.IGNORECASE)
_DEPENDENCY_RE = re.compile(
    r"^\s+[-*•]\s+(?P<kind>" + "|".join(DEPENDENCY_KINDS) + r"):\s*(?P<detail>\S.*)$", re.IGNORECASE)
_REST_RE = re.compile(r"^(?P<state>.*?)(?:;\s*next:\s*(?P<next>.+))?$", re.IGNORECASE)
_SECTION_RE = re.compile(r"^##\s+" + DAILY_THREADS_HEADING + r"\s*$(?P<body>.*?)(?=^##\s|\Z)",
                         re.IGNORECASE | re.MULTILINE | re.DOTALL)
_PLACEHOLDERS = {"none", "none.", "n/a", "(none)"}
# ``cron/scheduler_delivery.py::_cron_mirror_message`` opens every mirrored delivery with this.
_CRON_DELIVERY_LINE = re.compile(r"^\[Cron delivery:", re.MULTILINE)


def section_body(summary: Optional[str]) -> Optional[str]:
    """The ``## Daily Threads`` body of *summary*, or None when the section is absent."""
    match = _SECTION_RE.search(summary or "")
    return match.group("body") if match else None


def _state_and_next(rest: str) -> Tuple[str, str]:
    match = _REST_RE.match(rest.strip())
    return match.group("state").strip(), (match.group("next") or "").strip()


def parse_daily_threads(body: str) -> Tuple[List[Dict[str, Any]], List[str]]:
    """``(threads, unformatted)`` from one section body. A top-level line outside the grammar is
    returned as unformatted rather than guessed into a thread."""
    threads: List[Dict[str, Any]] = []
    unformatted: List[str] = []
    for raw in (body or "").splitlines():
        if not raw.strip() or raw.strip().lower() in _PLACEHOLDERS:
            continue
        if raw[:1].isspace():
            dependency = _DEPENDENCY_RE.match(raw)
            if dependency and threads and "dependency" not in threads[-1]:
                threads[-1]["dependency"] = {
                    "kind": DEPENDENCY_KINDS[dependency.group("kind").lower()],
                    "detail": dependency.group("detail").strip()}
            continue
        coded, uncoded = _THREAD_RE.match(raw.strip()), _UNCODED_RE.match(raw.strip())
        if coded:
            # The key as written: case-folding it here would hide a credential from the secret scan
            # the brief runs later. Codes still match case-insensitively (``verify_codes``).
            priority, key = coded.group("priority").upper(), coded.group("key")
            state, next_action = _state_and_next(coded.group("rest"))
            proposed = bool(coded.group("proposed")) or priority == "P?"
            threads.append({"standing": "proposed" if proposed else "established",
                            "code": f"{priority}-{key}", "priority": priority, "key": key,
                            "group": key.split("/", 1)[0], "state": state, "next": next_action})
        elif uncoded:
            state, next_action = _state_and_next(uncoded.group("rest"))
            threads.append({"standing": "uncoded", "title": uncoded.group("title").strip(),
                            "state": state, "next": next_action})
        else:
            unformatted.append(raw.strip().lstrip("-*• ").strip())
    return threads, unformatted


def _user_words(messages: Iterable[Any]) -> str:
    """Everything the user wrote in *messages*: user rows that are not compaction handoffs, runtime
    scaffolding or a mirrored cron delivery (the brief itself is not the user agreeing to it)."""
    from agent.context_compressor import (ContextCompressor, _content_text_for_contains,
                                          _synthetic_user_row)

    words = []
    for message in messages or ():
        if not isinstance(message, dict) or message.get("role") != "user":
            continue
        text = _content_text_for_contains(message.get("content"))
        if _synthetic_user_row(text) or ContextCompressor._is_synthetic_compression_user_turn(message):
            continue
        # Replay repair merges a mirrored delivery into the user row before it. Only the part before
        # the delivery is known to be the user's; the rest of that merged row is not counted.
        words.append(_CRON_DELIVERY_LINE.split(text, maxsplit=1)[0])
    return "\n".join(words)


def _stated(code: str, text: str) -> bool:
    return re.search(r"(?<![\w/-])" + re.escape(code) + r"(?![\w/-])", text, re.IGNORECASE) is not None


def verify_codes(summary: Optional[str], messages: Iterable[Any],
                 previous_summary: Optional[str]) -> Optional[str]:
    """*summary* with every unverified established code line rewritten as ``proposed``.

    A code is established when the user wrote it (any non-synthetic user row in *messages*) or the
    previous handoff, verified the same way when it was stored, carried it as established. A no-op
    when *summary* has no ``## Daily Threads`` section."""
    body = section_body(summary)
    if body is None:
        return summary
    carried = {thread["code"].lower() for thread in parse_daily_threads(
        section_body(previous_summary) or "")[0] if thread["standing"] == "established"}
    user_words = _user_words(messages)

    def _verified(line: str) -> str:
        coded = _THREAD_RE.match(line.strip())
        if coded is None or coded.group("proposed"):
            return line
        code = f"{coded.group('priority')}-{coded.group('key')}"
        if not code.startswith("P?") and (code.lower() in carried or _stated(code, user_words)):
            return line
        indent = line[:len(line) - len(line.lstrip())]
        bullet, item = line.lstrip()[0], line.lstrip()[1:].lstrip()
        return f"{indent}{bullet} proposed {item}"

    verified_body = "\n".join(_verified(line) if not line[:1].isspace() else line
                              for line in body.split("\n"))
    start = _SECTION_RE.search(summary).start("body")
    return summary[:start] + verified_body + summary[start + len(body):]
