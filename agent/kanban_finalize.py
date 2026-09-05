"""Terminal reconciliation for dispatcher-spawned kanban workers.

``agent.kanban_stop`` nudges a worker that tries to end its turn without a
board tool. Nudges have a budget, and once it runs out the conversation
returns plain text and the process exits ``rc=0`` with the task still
``running`` — which the dispatcher can only reap as a ``protocol_violation``,
long after the worker is gone and its evidence is lost.

This module is the kernel-owned backstop for that window. Every
dispatcher-owned worker that reaches normal Python exit while its task is
still ``running`` fails closed here: one sticky ``transient`` block carrying a
bounded, redacted handoff (failure class, model error, final response
excerpt, goal verdict, workspace, changed-artifact names).

Rules this module exists to enforce:

* Completion is NEVER inferred. Only an explicit ``kanban_complete`` marks a
  task done; anything else that reaches exit is a block.
* Every transition is bound to this worker's run id, so a stale process can
  never close a newer run.
* Idempotent: a task already ``done`` / ``blocked`` / ``todo`` / ``triage`` is
  left exactly as the process that transitioned it left it.
* Never raises. A finalizer that fails logs and leaves the dispatcher's crash
  handling as the backstop.
"""

from __future__ import annotations

import logging
import os
import subprocess
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)


# Block kind for every reconciled exit. ``transient`` says "this may clear on
# a retry" without routing to a human-input queue, and still participates in
# the unblock-loop breaker so a forever-failing card escalates to triage.
RECONCILE_BLOCK_KIND = "transient"

FAILURE_CLASS_CLEAN_EXIT = "clean_exit_without_terminal_call"
FAILURE_CLASS_RESULT_FAILED = "agent_result_failed"
FAILURE_CLASS_GOAL_BUDGET = "goal_turn_budget_exhausted"
FAILURE_CLASS_GOAL_JUDGE = "goal_judge_failure"
FAILURE_CLASS_GOAL_STOPPED = "goal_loop_stopped"

# Evidence bounds. The handoff is read by the next worker's context builder
# and by humans on the board, so it stays short and never carries file bodies.
_MAX_RESPONSE_EXCERPT = 600
_MAX_ERROR_EXCERPT = 400
_MAX_JUDGE_EXCERPT = 300
_MAX_ARTIFACTS = 20
_GIT_STATUS_TIMEOUT_SECONDS = 5.0

# Provider quota walls are not task failures: the CLI exits with the
# EX_TEMPFAIL sentinel and the dispatcher requeues the task without counting a
# failure. Blocking here would convert that neutral requeue into a sticky
# block, so these results are deliberately left alone.
_RATE_LIMIT_FAILURE_REASONS = frozenset({"rate_limit", "billing"})


def _truncate(text: str, limit: int) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit] + "… [truncated]"


def _worker_task_id() -> Optional[str]:
    """This process's own kanban task id, or None when it doesn't own one."""
    task_id = (os.environ.get("HERMES_KANBAN_TASK") or "").strip()
    if not task_id:
        return None
    try:
        from agent.delegation_context import is_dispatcher_owned_worker_context

        if not is_dispatcher_owned_worker_context():
            return None
    except Exception:
        logger.debug("kanban finalize: ownership probe failed", exc_info=True)
    return task_id


def _owned_run_id(task: Any) -> Optional[int]:
    """Resolve the run id this process is allowed to transition.

    ``HERMES_KANBAN_RUN_ID`` is the dispatcher's binding. When it is absent
    (worker launched outside the spawn path) the stamped ``worker_pid`` is the
    only other proof of ownership; without either, we refuse to write.
    """
    raw = os.environ.get("HERMES_KANBAN_RUN_ID")
    if raw:
        try:
            return int(raw)
        except (TypeError, ValueError):
            logger.debug("kanban finalize: unparseable HERMES_KANBAN_RUN_ID %r", raw)
    worker_pid = getattr(task, "worker_pid", None)
    if worker_pid is not None and int(worker_pid) == os.getpid():
        return getattr(task, "current_run_id", None)
    return None


def _redact(text: str) -> str:
    """Strip secrets from durable board text. Never raises."""
    try:
        from agent.redact import redact_sensitive_text

        return redact_sensitive_text(text, force=True)
    except Exception:
        logger.debug("kanban finalize: redaction unavailable", exc_info=True)
        # Provider errors and model output can contain credentials. If the
        # shared redactor is unavailable during shutdown, omit the evidence
        # rather than durably writing unredacted text to the board.
        return "[REDACTED: worker evidence omitted because redaction was unavailable]"


def _artifact_candidates(workspace: str) -> list[str]:
    """Bounded inventory of artifact NAMES in the workspace. Never reads bodies.

    Prefers ``git status --porcelain`` (changed + untracked) when the
    workspace is a repo; otherwise lists top-level entries. Both are names
    only — the handoff must never embed file contents.
    """
    root = Path(workspace)
    try:
        if not root.is_dir():
            return []
        if (root / ".git").exists():
            # Default ``--untracked-files=normal`` collapses untracked dirs to
            # ``dir/`` — cheaper than walking a workspace full of build output,
            # and a better inventory at this bound anyway.
            proc = subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=str(root),
                capture_output=True,
                text=True,
                timeout=_GIT_STATUS_TIMEOUT_SECONDS,
            )
            if proc.returncode != 0:
                return []
            names = [line[3:].strip() for line in proc.stdout.splitlines()]
        else:
            names = sorted(p.name for p in root.iterdir())
    except (OSError, ValueError, subprocess.SubprocessError):
        logger.debug("kanban finalize: artifact scan failed", exc_info=True)
        return []
    return [n for n in names if n]


def _format_artifacts(names: list[str]) -> str:
    shown = names[:_MAX_ARTIFACTS]
    line = ", ".join(shown)
    if len(names) > len(shown):
        line += f", +{len(names) - len(shown)} more"
    return line


def _failure_class(
    result: Optional[dict], goal_decision: Optional[dict]
) -> str:
    if goal_decision:
        outcome = goal_decision.get("outcome")
        if outcome == "blocked_judge_failure":
            return FAILURE_CLASS_GOAL_JUDGE
        if outcome == "blocked_budget":
            return FAILURE_CLASS_GOAL_BUDGET
        return FAILURE_CLASS_GOAL_STOPPED
    if result and result.get("failed"):
        return FAILURE_CLASS_RESULT_FAILED
    return FAILURE_CLASS_CLEAN_EXIT


def build_handoff_reason(
    *,
    result: Optional[dict] = None,
    final_response: str = "",
    goal_decision: Optional[dict] = None,
) -> str:
    """Assemble the durable handoff.

    Redaction happens once, at the write boundary in
    :func:`block_current_worker_task`, so every path onto the board — this one
    and the goal loop's own block reasons — goes through the same filter.
    """
    failure_class = _failure_class(result, goal_decision)
    lines = [
        f"Worker exited without a terminal kanban call ({failure_class}). "
        "The task was still running, so Hermes blocked it rather than "
        "inferring completion."
    ]

    if result:
        error = str(result.get("error") or "")
        reason = str(result.get("failure_reason") or "")
        if reason:
            lines.append(f"Failure reason: {reason}")
        if error:
            lines.append(f"Model error: {_truncate(error, _MAX_ERROR_EXCERPT)}")

    if goal_decision:
        turns = goal_decision.get("turns_used")
        if turns is not None:
            lines.append(f"Goal turns used: {turns}")
        verdict = str(goal_decision.get("verdict") or "")
        goal_reason = str(goal_decision.get("reason") or "")
        if verdict or goal_reason:
            lines.append(
                f"Last judge verdict: {verdict or 'n/a'} — "
                f"{_truncate(goal_reason, _MAX_JUDGE_EXCERPT)}"
            )

    excerpt = final_response or ""
    if not excerpt and goal_decision:
        excerpt = str(goal_decision.get("last_response") or "")
    if not excerpt and result:
        excerpt = str(result.get("final_response") or "")
    if excerpt:
        lines.append(
            f"Final response: {_truncate(excerpt, _MAX_RESPONSE_EXCERPT)}"
        )

    workspace = (os.environ.get("HERMES_KANBAN_WORKSPACE") or "").strip()
    if workspace:
        lines.append(f"Workspace: {workspace}")
        artifacts = _artifact_candidates(workspace)
        if artifacts:
            lines.append(f"Artifact candidates: {_format_artifacts(artifacts)}")

    return "\n".join(lines)


def block_current_worker_task(
    reason: str, *, kind: str = RECONCILE_BLOCK_KIND
) -> Optional[str]:
    """Block this worker's own task, bound to the run it owns.

    The single durable-write boundary for worker-side terminal transitions —
    used by the goal loop and by :func:`reconcile_terminal_state`. Returns an
    outcome label for logging, or None when this process owns no task.
    """
    task_id = _worker_task_id()
    if not task_id:
        return None

    conn = None
    try:
        from hermes_cli import kanban_db as kb

        conn = _reconciled_hermes_cli_kanban_db_connect.connect()
        task = kb.get_task(conn, task_id)
        if task is None:
            logger.warning("kanban finalize: task %s not found on this board", task_id)
            return "unknown_task"
        if task.status != "running":
            logger.info(
                "kanban finalize: task %s already terminal (status=%s)",
                task_id, task.status,
            )
            return "already_terminal"

        run_id = _owned_run_id(task)
        if run_id is None:
            logger.warning(
                "kanban finalize: refusing to transition %s — no run id binding "
                "(HERMES_KANBAN_RUN_ID unset and worker_pid mismatch)",
                task_id,
            )
            return "not_owned"

        blocked = kb.block_task(
            conn, task_id,
            reason=_redact(reason),
            kind=kind,
            expected_run_id=run_id,
        )
        if not blocked:
            logger.warning(
                "kanban finalize: task %s moved on (run %s is no longer current); "
                "leaving it to its current owner",
                task_id, run_id,
            )
            return "stale_run"
        logger.info("kanban finalize: blocked task %s on run %s", task_id, run_id)
        return "blocked"
    except Exception:
        logger.warning(
            "kanban finalize: reconciliation of %s failed", task_id, exc_info=True,
        )
        return "error"
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def reconcile_terminal_state(
    *,
    result: Optional[dict] = None,
    final_response: str = "",
    goal_decision: Optional[dict] = None,
) -> Optional[str]:
    """Fail closed if this worker is exiting with its task still running.

    Returns None outside dispatcher-owned worker context (so normal CLI runs
    are untouched), otherwise one of ``blocked``, ``already_terminal``,
    ``stale_run``, ``not_owned``, ``rate_limited``, ``unknown_task``,
    ``error``.
    """
    if not _worker_task_id():
        return None

    if (
        result
        and result.get("failed")
        and result.get("failure_reason") in _RATE_LIMIT_FAILURE_REASONS
    ):
        return "rate_limited"

    try:
        reason = build_handoff_reason(
            result=result,
            final_response=final_response,
            goal_decision=goal_decision,
        )
    except Exception:
        logger.warning("kanban finalize: handoff assembly failed", exc_info=True)
        reason = (
            "Worker exited without a terminal kanban call; evidence collection "
            "failed (see the worker log)."
        )
    return block_current_worker_task(reason)


__all__ = [
    "FAILURE_CLASS_CLEAN_EXIT",
    "FAILURE_CLASS_GOAL_BUDGET",
    "FAILURE_CLASS_GOAL_JUDGE",
    "FAILURE_CLASS_GOAL_STOPPED",
    "FAILURE_CLASS_RESULT_FAILED",
    "RECONCILE_BLOCK_KIND",
    "block_current_worker_task",
    "build_handoff_reason",
    "reconcile_terminal_state",
]

import hermes_cli.kanban_db_connect as _reconciled_hermes_cli_kanban_db_connect
