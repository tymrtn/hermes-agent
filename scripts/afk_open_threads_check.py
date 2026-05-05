#!/usr/bin/env python3
"""AFK open-thread cron check.

Wired into ``hermes cron`` as a pre-run script. On each tick:

1. Confirm the user has been idle for at least ``--idle-seconds``
   (default 1800s / 30 min) using the gateway sessions.json mtime as
   a best-effort "last user activity" timestamp.
2. Pick exactly one eligible thread (safe + allowed side-effects + not
   over its attempt cap) from ``$HERMES_HOME/open_threads.json``,
   bumping its attempt counter to prevent retry storms.
3. Print a self-contained prompt to stdout. The cron scheduler injects
   stdout into the agent's prompt as context. If we print nothing, the
   scheduler skips the AI call entirely (see
   ``cron.scheduler._build_job_prompt``).

The emitted prompt is fully self-contained — no chat-history dependency,
no relative date/time references, no allowed-action ambiguity. The
fresh cron agent reads it, executes one safe thread, and updates the
ledger via ``open_thread_update``.

Usage::

    python scripts/afk_open_threads_check.py --idle-seconds 1800

Exit codes::

    0  always (silent no-op or prompt emitted; cron handles both)
    2  argparse / configuration error
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

# Make the hermes-agent project importable when run from cron's CWD.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from gateway import open_threads  # noqa: E402
from hermes_constants import get_hermes_home  # noqa: E402


DEFAULT_IDLE_SECONDS = 1800  # 30 minutes

# Off by default. Flip via --enabled or HERMES_OPEN_THREADS_ENABLED=1.
# Cron defaults to "do nothing" so installing the job file can't surprise
# anyone — the human still has to opt in.
ENABLE_ENV_VAR = "HERMES_OPEN_THREADS_ENABLED"


def _env_truthy(value: "str | None") -> bool:
    if not value:
        return False
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _last_user_activity_age(hermes_home: Path) -> "float | None":
    """Return seconds since last gateway user activity, or None if unknown.

    Uses the mtime of ``$HERMES_HOME/sessions/sessions.json`` as a proxy
    — that file is rewritten on every gateway message turn. Falls back
    to ``$HERMES_HOME/gateway_state.json`` if sessions.json is missing.

    Returns ``None`` when neither file exists. The caller should treat
    "unknown" as "do not run" — better to be quiet than to fire while
    the user is actively typing.
    """
    candidates = [
        hermes_home / "sessions" / "sessions.json",
        hermes_home / "gateway_state.json",
    ]
    for path in candidates:
        try:
            if path.exists():
                return max(0.0, time.time() - path.stat().st_mtime)
        except OSError:
            continue
    return None


def _pick_one_under_extra_cap(extra_cap: "int | None") -> "open_threads.OpenThread | None":
    """Pick one eligible thread, respecting an optional script-level cap.

    ``open_threads.pick_one()`` intentionally knows only the per-thread
    ``max_attempts``. The cron script can impose a stricter cap; when it
    does, filter before bumping so an at-cap thread is never mutated just
    because another eligible thread exists later in the ledger.
    """
    if extra_cap is None:
        return open_threads.pick_one()

    eligible = [
        t for t in open_threads.eligible_threads()
        if t.attempt_count < extra_cap
    ]
    if not eligible:
        return None
    eligible.sort(key=lambda t: (t.last_attempted_at or "", t.created_at))
    chosen = eligible[0]
    return open_threads.update(chosen.id, bump_attempt=True)


def _build_prompt(thread: open_threads.OpenThread) -> str:
    """Compose the self-contained cron-agent prompt for one thread."""
    allowed = sorted(open_threads.ALLOWED_SIDE_EFFECTS)
    blocked = sorted(open_threads.BLOCKED_SIDE_EFFECTS)
    side_effects = ", ".join(thread.side_effects) or "(none)"
    return (
        "## AFK Open-Thread Run (cron)\n"
        "\n"
        "You are running unattended on a cron tick because the user has "
        "been idle. Treat this as a fresh session: no prior chat, no "
        "queued user message, no follow-up turn after this one.\n"
        "\n"
        "### Thread to work on\n"
        f"- id: {thread.id}\n"
        f"- title: {thread.title}\n"
        f"- description: {thread.description or '(none)'}\n"
        f"- declared side_effects: {side_effects}\n"
        f"- attempt: {thread.attempt_count} of {thread.max_attempts}\n"
        "\n"
        "### Hard rules (non-negotiable)\n"
        "1. Do exactly ONE thread — this one. Do not pick another, do "
        "not loop.\n"
        f"2. Allowed actions tonight: {allowed}. Anything else is "
        "out of scope.\n"
        f"3. NEVER perform: {blocked}. Do not send email, publish "
        "anything, push to git, deploy, restart services, touch "
        "credentials, post publicly, move money, or send messages on "
        "behalf of the user.\n"
        "4. No new chat replies to anyone. Output goes only to the "
        "thread's result_summary, not to a conversation.\n"
        "5. If the thread requires any blocked action to complete, "
        "STOP and call open_thread_update with status='blocked' and a "
        "result_summary explaining what's needed.\n"
        "6. When done, call open_thread_update with status='done' and "
        "a 1-3 sentence result_summary the user will read on their "
        "next return. After that tool call, your final response must be "
        "exactly [SILENT] so cron does not message the user while AFK.\n"
        "7. If you cannot make progress in a reasonable single turn, "
        "call open_thread_update with status='blocked' rather than "
        "leaving the thread half-finished.\n"
        "\n"
        "Begin."
    )


def main(argv: "list[str] | None" = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Pre-run script for the AFK open-threads cron job. Prints a "
            "self-contained prompt when an eligible thread is ready and "
            "the user has been idle. Prints nothing otherwise."
        ),
    )
    parser.add_argument(
        "--enabled",
        action="store_true",
        default=False,
        help=(
            "Enable the AFK runner. Off by default — set this flag (or "
            f"export {ENABLE_ENV_VAR}=1) to opt in. Without it the script "
            "is a silent no-op so dropping the cron file in place can't "
            "surprise anyone."
        ),
    )
    parser.add_argument(
        "--idle-seconds",
        type=int,
        default=DEFAULT_IDLE_SECONDS,
        help=(
            "Minimum seconds since last gateway activity before the "
            "AFK runner is allowed to fire. Default: 1800."
        ),
    )
    parser.add_argument(
        "--max-attempts",
        type=int,
        default=None,
        help=(
            "Optional script-level cap. When set, threads whose "
            "attempt_count is >= this value are skipped even if they "
            "haven't hit their per-thread max_attempts. Default: no "
            "extra cap (each thread's own max_attempts is the only "
            "limit)."
        ),
    )
    parser.add_argument(
        "--require-idle",
        action="store_true",
        default=True,
        help=(
            "Require an idle signal (sessions.json or gateway_state.json "
            "mtime). When neither file exists, exit silently. On by "
            "default — pass --no-require-idle to bypass."
        ),
    )
    parser.add_argument(
        "--no-require-idle",
        dest="require_idle",
        action="store_false",
        help="Allow firing even when no idle signal is available.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Inspect eligible threads without bumping attempt counters "
            "or printing a real prompt. Prints a JSON diagnostic to "
            "stderr; stdout stays empty so cron stays quiet."
        ),
    )
    args = parser.parse_args(argv)

    if not (args.enabled or _env_truthy(os.environ.get(ENABLE_ENV_VAR))):
        # Disabled by default. Silent no-op so cron stays quiet.
        return 0

    home = get_hermes_home()

    age = _last_user_activity_age(home)
    if args.require_idle and age is None:
        # Unknown state — don't fire. Quiet exit.
        return 0
    if age is not None and age < args.idle_seconds:
        return 0

    extra_cap = args.max_attempts

    if args.dry_run:
        eligible = open_threads.eligible_threads()
        if extra_cap is not None:
            eligible = [t for t in eligible if t.attempt_count < extra_cap]
        import json
        print(
            json.dumps({
                "idle_age_seconds": age,
                "eligible_count": len(eligible),
                "eligible_ids": [t.id for t in eligible],
            }),
            file=sys.stderr,
        )
        return 0

    thread = _pick_one_under_extra_cap(extra_cap)
    if thread is None:
        return 0

    sys.stdout.write(_build_prompt(thread))
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
