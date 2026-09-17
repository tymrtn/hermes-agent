"""Gateway-native AFK follow-up helpers.

The gateway owns the idle/scheduling decision. The model owns deciding which
safe, useful work to do once a virtual turn is injected.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable, Optional, Set


DEFAULT_AFK_THRESHOLDS_MINUTES: tuple[int, ...] = (5, 15, 60)


@dataclass(frozen=True)
class AfkFollowupConfig:
    enabled: bool = False
    thresholds_minutes: tuple[int, ...] = DEFAULT_AFK_THRESHOLDS_MINUTES
    interval_seconds: float = 60.0


@dataclass(frozen=True)
class AfkFollowupDecision:
    session_key: str
    threshold_minutes: int
    idle_seconds: float
    idle_label: str
    due_minutes: int


def _as_aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def normalize_thresholds(values: Iterable[int] | None) -> tuple[int, ...]:
    if values is None:
        return DEFAULT_AFK_THRESHOLDS_MINUTES
    thresholds: set[int] = set()
    for value in values:
        try:
            threshold = int(value)
        except (TypeError, ValueError):
            continue
        if threshold > 0:
            thresholds.add(threshold)
    if not thresholds:
        return DEFAULT_AFK_THRESHOLDS_MINUTES
    return tuple(sorted(thresholds))


def parse_afk_followup_config(raw) -> AfkFollowupConfig:
    """Parse ``gateway.afk_followup`` config with safe defaults.

    AFK automation is opt-in. Malformed values are tolerated so a bad config
    cannot crash gateway startup.
    """
    if not isinstance(raw, dict):
        return AfkFollowupConfig(enabled=False)
    enabled = bool(raw.get("enabled", False))
    thresholds = normalize_thresholds(raw.get("thresholds_minutes"))
    try:
        interval = float(raw.get("interval_seconds", 60.0))
    except (TypeError, ValueError):
        interval = 60.0
    if interval <= 0:
        interval = 60.0
    return AfkFollowupConfig(
        enabled=enabled,
        thresholds_minutes=thresholds,
        interval_seconds=interval,
    )


def format_idle_label(minutes: int) -> str:
    if minutes >= 60 and minutes % 60 == 0:
        hours = minutes // 60
        return f"{hours}h"
    return f"{minutes}m"


def next_afk_followup(
    entry,
    *,
    now: datetime,
    thresholds_minutes: Iterable[int] = DEFAULT_AFK_THRESHOLDS_MINUTES,
    fired_thresholds: Set[int] | None = None,
    running_session_keys: Set[str] | None = None,
    queued_session_keys: Set[str] | None = None,
) -> Optional[AfkFollowupDecision]:
    """Return the next AFK threshold to inject for a session, if any."""
    session_key = getattr(entry, "session_key", "") or ""
    if not session_key:
        return None
    if getattr(entry, "suspended", False):
        return None
    if getattr(entry, "origin", None) is None:
        return None
    if session_key in (running_session_keys or set()):
        return None
    if session_key in (queued_session_keys or set()):
        return None

    updated_at = getattr(entry, "updated_at", None)
    if not isinstance(updated_at, datetime):
        return None

    now_utc = _as_aware_utc(now)
    updated_utc = _as_aware_utc(updated_at)
    idle_seconds = max(0.0, (now_utc - updated_utc).total_seconds())
    idle_minutes = idle_seconds / 60.0
    fired = fired_thresholds if fired_thresholds is not None else set()
    thresholds = normalize_thresholds(thresholds_minutes)
    cycle_minutes = max(thresholds)
    due_candidates: list[tuple[int, int]] = []
    for threshold in thresholds:
        if idle_minutes < threshold:
            continue
        cycle_index = int((idle_minutes - threshold) // cycle_minutes)
        due_candidates.append((threshold + (cycle_index * cycle_minutes), threshold))

    if fired_thresholds is not None:
        # Keep only the current idle-cycle due marks. This lets thresholds recur
        # in later cycles without replaying every missed historical cycle, and
        # also resets stale marks after real user activity moves updated_at
        # forward and the computed idle age drops.
        fired.intersection_update(due for due, _threshold in due_candidates)

    for due_minutes, threshold in sorted(due_candidates):
        if due_minutes in fired:
            continue
        return AfkFollowupDecision(
            session_key=session_key,
            threshold_minutes=threshold,
            idle_seconds=idle_seconds,
            idle_label=format_idle_label(threshold),
            due_minutes=due_minutes,
        )
    return None


def build_afk_followup_prompt(
    idle_label: str,
    *,
    recent_instruction: str | None = None,
    cycle_index: int = 1,
) -> str:
    instruction_line = ""
    if recent_instruction:
        instruction_line = (
            f"\nRecent user instruction about autonomous follow-ups: {recent_instruction}.\n"
            "If there has been little recent conversation or the user asked you not to work while they are away, ignore this prompt and stay quiet.\n"
        )

    ordinals = {1: "first", 2: "second", 3: "third"}
    ordinal = ordinals.get(cycle_index, f"#{cycle_index}")

    if cycle_index <= 1:
        body = (
            "Identify the top 3 loose ends or safe follow-up tasks from the recent conversation "
            "or your durable task board, in priority order. Tasks include: github issues, pending PRs (must result in approval/merge or a rejection and new dev task), dev task, content written, emails triaged, email replies drafted (leave in drafts not sent), or design a work loop and print it for user to the conversation. Internal memos, notes, or docs are NOT valid tasks for AFK work. Now, work on the first one now and complete it."
        )
    else:
        body = (
            "You may have identified loose ends in an earlier AFK cycle. Work on the "
            f"{ordinal} unresolved safe task now. If that task is already resolved, pick the next unresolved one."
        )

    return (
        "[Automated AFK follow-up]\n\n"
        f"The user has been AFK for {idle_label}.\n"
        f"{instruction_line}\n"
        f"{body}\n\n"
        "Safety bounds: do not take destructive, financially risky, credential-changing, "
        "public-publishing, email-sending, or irreversible actions unless the user already explicitly "
        "approved that exact action. Deployment is different: do not treat a withheld deploy/restart "
        "as a success or something to be proud of. If deployment or restart is the natural endpoint, "
        "do every safe prerequisite first, then stop at the approval boundary and ask plainly: "
        "`It's ready to deploy. Here is the evidence and a link/command to test it yourself. "
        "Can we deploy, or do you have modifications?`\n\n"
        "When finished, reply to the user with a concise 1-3 sentence summary. The tone is responsive, "
        "not self-congratulatory: report readiness, evidence, and the exact approval needed. "
        "Use this prefix when applicable: `AFK task completed: <what changed>.` "
        "If you hit a real blocker, report it concisely."
    )
