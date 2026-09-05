"""Kernel-owned terminal reconciliation for dispatcher-spawned kanban workers.

A worker process that reaches normal Python exit while its assigned task is
still ``running`` must fail closed to a sticky block carrying a durable
handoff — never leave the task for the dispatcher to reap as a protocol
violation, and never infer completion.

All tests drive the real ``hermes_cli.kanban_db`` against a per-test board.
"""

from __future__ import annotations

import hermes_cli.kanban_db_connect as _reconciled_hermes_cli_kanban_db_connect
from pathlib import Path

import pytest

from agent import kanban_finalize
from hermes_cli import goals
from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    _reconciled_hermes_cli_kanban_db_connect.init_db()
    return home


def _claim_running(conn, *, title: str = "task") -> tuple[str, int]:
    """Create a task and claim it, returning ``(task_id, run_id)``."""
    tid = kb.create_task(conn, title=title, assignee="worker")
    host_prefix = kb._claimer_id().split(":", 1)[0]
    task = kb.claim_task(conn, tid, claimer=f"{host_prefix}:mock")
    assert task is not None and task.current_run_id is not None
    return tid, int(task.current_run_id)


def _as_worker(monkeypatch, task_id: str, run_id: int | None) -> None:
    monkeypatch.setenv("HERMES_KANBAN_TASK", task_id)
    if run_id is None:
        monkeypatch.delenv("HERMES_KANBAN_RUN_ID", raising=False)
    else:
        monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run_id))


def _last_summary(conn, task_id: str) -> str:
    runs = kb.list_runs(conn, task_id)
    assert runs, "expected at least one run row"
    return runs[-1].summary or ""


# ---------------------------------------------------------------------------
# Clean exit (rc=0) with no terminal kanban call
# ---------------------------------------------------------------------------

def test_clean_exit_blocks_current_run_with_final_response_evidence(
    kanban_home, monkeypatch
):
    """rc=0 + task still running → sticky block on THIS run, response kept."""
    conn = _reconciled_hermes_cli_kanban_db_connect.connect()
    try:
        tid, run_id = _claim_running(conn)
        _as_worker(monkeypatch, tid, run_id)

        outcome = kanban_finalize.reconcile_terminal_state(
            result={"final_response": "I will write the report next."},
            final_response="I will write the report next.",
        )

        assert outcome == "blocked"
        task = kb.get_task(conn, tid)
        assert task.status == "blocked"
        summary = _last_summary(conn, tid)
        assert "I will write the report next." in summary
        assert kanban_finalize.FAILURE_CLASS_CLEAN_EXIT in summary
        # Bound to the run the worker owned.
        assert kb.get_run(conn, run_id).outcome == "blocked"
    finally:
        conn.close()


def test_clean_exit_block_records_workspace_and_artifact_inventory(
    kanban_home, monkeypatch, tmp_path
):
    """Workspace path + bounded artifact names land in the handoff; no contents."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "report.md").write_text("SECRET BODY TEXT", encoding="utf-8")

    conn = _reconciled_hermes_cli_kanban_db_connect.connect()
    try:
        tid, run_id = _claim_running(conn)
        _as_worker(monkeypatch, tid, run_id)
        monkeypatch.setenv("HERMES_KANBAN_WORKSPACE", str(workspace))

        assert kanban_finalize.reconcile_terminal_state(final_response="ok") == "blocked"

        summary = _last_summary(conn, tid)
        assert str(workspace) in summary
        assert "report.md" in summary
        assert "SECRET BODY TEXT" not in summary
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Failed agent result (would exit 1)
# ---------------------------------------------------------------------------

def test_failed_result_blocks_with_sanitized_failure_evidence(
    kanban_home, monkeypatch
):
    """A failed result blocks first, and the durable reason carries no secrets."""
    conn = _reconciled_hermes_cli_kanban_db_connect.connect()
    try:
        tid, run_id = _claim_running(conn)
        _as_worker(monkeypatch, tid, run_id)

        outcome = kanban_finalize.reconcile_terminal_state(
            result={
                "failed": True,
                "failure_reason": "tool_error",
                "error": (
                    "provider rejected request "
                    "(Authorization: Bearer sk-ant-api03-AAAABBBBCCCCDDDDEEEEFFFF)"
                ),
            },
        )

        assert outcome == "blocked"
        assert kb.get_task(conn, tid).status == "blocked"
        summary = _last_summary(conn, tid)
        assert "provider rejected request" in summary
        assert "tool_error" in summary
        assert "sk-ant-api03-AAAABBBBCCCCDDDDEEEEFFFF" not in summary
    finally:
        conn.close()


def test_redactor_failure_omits_evidence_instead_of_leaking_it(
    kanban_home, monkeypatch
):
    """Shutdown-time redactor failure must fail closed, never preserve input."""
    from agent import redact

    conn = _reconciled_hermes_cli_kanban_db_connect.connect()
    try:
        tid, run_id = _claim_running(conn)
        _as_worker(monkeypatch, tid, run_id)

        def _broken_redactor(*_args, **_kwargs):
            raise RuntimeError("redactor unavailable")

        monkeypatch.setattr(redact, "redact_sensitive_text", _broken_redactor)
        secret = "Authorization: Bearer sk-ant-api03-DO-NOT-PERSIST"

        assert kanban_finalize.reconcile_terminal_state(
            result={"failed": True, "failure_reason": "tool_error", "error": secret},
        ) == "blocked"

        summary = _last_summary(conn, tid)
        assert "DO-NOT-PERSIST" not in summary
        assert "evidence omitted because redaction was unavailable" in summary
    finally:
        conn.close()


def test_rate_limited_result_is_left_to_the_dispatcher_sentinel(
    kanban_home, monkeypatch
):
    """Quota walls keep the EX_TEMPFAIL requeue path — never a sticky block."""
    conn = _reconciled_hermes_cli_kanban_db_connect.connect()
    try:
        tid, run_id = _claim_running(conn)
        _as_worker(monkeypatch, tid, run_id)

        outcome = kanban_finalize.reconcile_terminal_state(
            result={"failed": True, "failure_reason": "rate_limit", "error": "429"},
        )

        assert outcome == "rate_limited"
        assert kb.get_task(conn, tid).status == "running"
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Ownership: a stale process must never close a newer run
# ---------------------------------------------------------------------------

def test_stale_run_id_cannot_block_a_newer_run(kanban_home, monkeypatch):
    conn = _reconciled_hermes_cli_kanban_db_connect.connect()
    try:
        tid, stale_run_id = _claim_running(conn)
        # Task is reclaimed and re-dispatched: a NEW run owns it now.
        assert kb.block_task(conn, tid, reason="reclaimed") is True
        assert kb.unblock_task(conn, tid) is True
        host_prefix = kb._claimer_id().split(":", 1)[0]
        fresh = kb.claim_task(conn, tid, claimer=f"{host_prefix}:mock2")
        assert fresh is not None and fresh.current_run_id != stale_run_id

        # The stale worker finally exits and tries to reconcile.
        _as_worker(monkeypatch, tid, stale_run_id)
        outcome = kanban_finalize.reconcile_terminal_state(final_response="stale")

        assert outcome == "stale_run"
        task = kb.get_task(conn, tid)
        assert task.status == "running"
        assert task.current_run_id == fresh.current_run_id
    finally:
        conn.close()


def test_missing_run_id_without_pid_ownership_does_not_block(
    kanban_home, monkeypatch
):
    """No run id and no pid proof → refuse to transition someone else's task."""
    conn = _reconciled_hermes_cli_kanban_db_connect.connect()
    try:
        tid, _run_id = _claim_running(conn)
        _as_worker(monkeypatch, tid, None)

        outcome = kanban_finalize.reconcile_terminal_state(final_response="x")

        assert outcome == "not_owned"
        assert kb.get_task(conn, tid).status == "running"
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Idempotency / terminal no-ops
# ---------------------------------------------------------------------------

def test_reconciliation_is_a_noop_after_kanban_complete(kanban_home, monkeypatch):
    conn = _reconciled_hermes_cli_kanban_db_connect.connect()
    try:
        tid, run_id = _claim_running(conn)
        assert kb.complete_task(conn, tid, summary="done properly") is True
        _as_worker(monkeypatch, tid, run_id)

        outcome = kanban_finalize.reconcile_terminal_state(final_response="trailing text")

        assert outcome == "already_terminal"
        task = kb.get_task(conn, tid)
        assert task.status == "done"
        assert "trailing text" not in (_last_summary(conn, tid))
    finally:
        conn.close()


def test_reconciliation_is_a_noop_after_kanban_block(kanban_home, monkeypatch):
    conn = _reconciled_hermes_cli_kanban_db_connect.connect()
    try:
        tid, run_id = _claim_running(conn)
        assert kb.block_task(
            conn, tid, reason="worker asked for input", kind="needs_input",
        ) is True
        _as_worker(monkeypatch, tid, run_id)

        outcome = kanban_finalize.reconcile_terminal_state(final_response="trailing")

        assert outcome == "already_terminal"
        task = kb.get_task(conn, tid)
        assert task.status == "blocked"
        assert task.block_kind == "needs_input"
        assert _last_summary(conn, tid) == "worker asked for input"
    finally:
        conn.close()


def test_reconciliation_is_idempotent_when_called_twice(kanban_home, monkeypatch):
    conn = _reconciled_hermes_cli_kanban_db_connect.connect()
    try:
        tid, run_id = _claim_running(conn)
        _as_worker(monkeypatch, tid, run_id)

        assert kanban_finalize.reconcile_terminal_state(final_response="one") == "blocked"
        assert (
            kanban_finalize.reconcile_terminal_state(final_response="one")
            == "already_terminal"
        )

        blocked_events = [
            e for e in kb.list_events(conn, tid) if e.kind == "blocked"
        ]
        assert len(blocked_events) == 1
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Non-kanban CLI behaviour is untouched
# ---------------------------------------------------------------------------

def test_no_kanban_task_env_is_a_noop(kanban_home, monkeypatch):
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    assert kanban_finalize.reconcile_terminal_state(final_response="hello") is None


def test_delegated_child_context_never_reconciles(kanban_home, monkeypatch):
    from agent import delegation_context

    conn = _reconciled_hermes_cli_kanban_db_connect.connect()
    try:
        tid, run_id = _claim_running(conn)
        _as_worker(monkeypatch, tid, run_id)
        with delegation_context.delegated_child_context():
            outcome = kanban_finalize.reconcile_terminal_state(final_response="child")
        assert outcome is None
        assert kb.get_task(conn, tid).status == "running"
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Goal mode: budget exhaustion and judge failure evidence
# ---------------------------------------------------------------------------

def _patch_judge(monkeypatch, verdicts):
    """Scripted judge. Entries are ``verdict`` or ``(verdict, reason, transport)``."""
    seq = list(verdicts)

    def _fake_judge(goal, response, subgoals=None, background_processes=None, **_kw):
        item = seq.pop(0) if seq else "done"
        if isinstance(item, tuple):
            verdict, reason, transport = item
        else:
            verdict, reason, transport = item, f"scripted:{item}", False
        return verdict, reason, False, None, transport

    monkeypatch.setattr(goals, "judge_goal", _fake_judge)


def test_goal_budget_exhaustion_persists_response_and_judge_evidence(monkeypatch):
    """The budget blocker must carry turns used, verdict and the last response."""
    _patch_judge(monkeypatch, ["continue", "continue"])
    reasons: list[str] = []

    res = goals.run_kanban_goal_loop(
        task_id="t1",
        goal_text="ship the report",
        run_turn=lambda p: "partial draft only, section 3 missing",
        task_status_fn=lambda: "running",
        block_fn=reasons.append,
        max_turns=2,
        first_response="starting now",
        log=None,
    )

    assert res["outcome"] == "blocked_budget"
    assert res["turns_used"] == 2
    assert res["last_response"] == "partial draft only, section 3 missing"
    assert len(reasons) == 1
    reason = reasons[0]
    assert "2/2" in reason
    assert "scripted:continue" in reason
    assert "partial draft only, section 3 missing" in reason


def test_goal_judge_transport_failure_blocks_with_diagnostic(monkeypatch):
    """A broken judge blocks with a diagnostic instead of burning every turn."""
    failures = [("continue", "judge error: BadRequestError", True)] * 20
    _patch_judge(monkeypatch, failures)
    reasons: list[str] = []
    turns: list[str] = []

    res = goals.run_kanban_goal_loop(
        task_id="t2",
        goal_text="ship the report",
        run_turn=lambda p: turns.append(p) or "worked some more",
        task_status_fn=lambda: "running",
        block_fn=reasons.append,
        max_turns=50,
        first_response="first turn output",
        log=None,
    )

    assert res["outcome"] == "blocked_judge_failure"
    # Bailed well before the 50-turn budget.
    assert res["turns_used"] < 50
    assert len(reasons) == 1
    reason = reasons[0]
    assert "BadRequestError" in reason
    assert "worked some more" in reason
    assert res["last_response"] == "worked some more"


def test_goal_run_turn_failure_returns_evidence_for_the_finalizer(monkeypatch):
    """A crashed turn stops the loop but hands its evidence to the caller."""
    _patch_judge(monkeypatch, ["continue"])

    def _boom(prompt):
        raise RuntimeError("model transport died")

    res = goals.run_kanban_goal_loop(
        task_id="t3",
        goal_text="ship the report",
        run_turn=_boom,
        task_status_fn=lambda: "running",
        block_fn=lambda r: pytest.fail("loop must not block; the finalizer does"),
        max_turns=5,
        first_response="first turn output",
        log=None,
    )

    assert res["outcome"] == "stopped"
    assert res["last_response"] == "first turn output"
    assert "RuntimeError" in res["reason"]


def test_goal_loop_hook_returns_its_decision_and_blocks_with_evidence(
    kanban_home, monkeypatch
):
    """``_run_kanban_goal_loop_q`` must hand its decision back, and its block
    must land on the worker's own run with real evidence."""
    import types

    import cli as hermes_cli

    _patch_judge(monkeypatch, ["continue", "continue"])
    conn = _reconciled_hermes_cli_kanban_db_connect.connect()
    try:
        tid = kb.create_task(
            conn, title="ship the report", body="with all three sections",
            assignee="worker", goal_mode=True, goal_max_turns=2,
        )
        host_prefix = kb._claimer_id().split(":", 1)[0]
        task = kb.claim_task(conn, tid, claimer=f"{host_prefix}:mock")
        _as_worker(monkeypatch, tid, task.current_run_id)

        stub = types.SimpleNamespace(
            agent=types.SimpleNamespace(
                run_conversation=lambda **_kw: {"final_response": "section 3 pending"},
                session_id="s1",
            ),
            conversation_history=[],
            session_id="s1",
        )

        decision = hermes_cli._run_kanban_goal_loop_q(stub, "starting")

        assert decision is not None
        assert decision["outcome"] == "blocked_budget"
        assert decision["turns_used"] == 2
        assert kb.get_task(conn, tid).status == "blocked"
        summary = _last_summary(conn, tid)
        assert "2/2" in summary
        assert "section 3 pending" in summary
    finally:
        conn.close()


def test_cli_finalizer_blocks_a_clean_quiet_worker_exit(kanban_home, monkeypatch):
    """The CLI's exit hook reconciles a worker that never called a board tool."""
    import types

    import cli as hermes_cli

    conn = _reconciled_hermes_cli_kanban_db_connect.connect()
    try:
        tid, run_id = _claim_running(conn)
        _as_worker(monkeypatch, tid, run_id)
        stub = types.SimpleNamespace()

        hermes_cli._note_kanban_worker_outcome(
            stub,
            result={"final_response": "Let me write the report now."},
            response="Let me write the report now.",
        )
        hermes_cli._reconcile_kanban_terminal_state(stub)

        assert kb.get_task(conn, tid).status == "blocked"
        assert "Let me write the report now." in _last_summary(conn, tid)
    finally:
        conn.close()


def test_cli_finalizer_blocks_a_failed_result_before_exit_1(kanban_home, monkeypatch):
    import types

    import cli as hermes_cli

    conn = _reconciled_hermes_cli_kanban_db_connect.connect()
    try:
        tid, run_id = _claim_running(conn)
        _as_worker(monkeypatch, tid, run_id)
        stub = types.SimpleNamespace()

        hermes_cli._note_kanban_worker_outcome(
            stub,
            result={
                "failed": True,
                "failure_reason": "provider_error",
                "error": "upstream 500",
            },
        )
        hermes_cli._reconcile_kanban_terminal_state(stub)

        task = kb.get_task(conn, tid)
        assert task.status == "blocked"
        summary = _last_summary(conn, tid)
        assert "upstream 500" in summary
        assert "provider_error" in summary
    finally:
        conn.close()


def test_cli_finalizer_recovers_evidence_from_conversation_history(
    kanban_home, monkeypatch
):
    """Workers spawned without ``-Q`` exit through ``cli.chat`` with no result
    dict; the last assistant turn is still recoverable evidence."""
    import types

    import cli as hermes_cli

    conn = _reconciled_hermes_cli_kanban_db_connect.connect()
    try:
        tid, run_id = _claim_running(conn)
        _as_worker(monkeypatch, tid, run_id)
        stub = types.SimpleNamespace(
            conversation_history=[
                {"role": "user", "content": "work kanban task"},
                {"role": "assistant", "content": "Next I will run the tests."},
            ],
        )

        hermes_cli._reconcile_kanban_terminal_state(stub)

        assert kb.get_task(conn, tid).status == "blocked"
        assert "Next I will run the tests." in _last_summary(conn, tid)
    finally:
        conn.close()


def test_cli_finalizer_leaves_interrupts_to_dispatcher_crash_handling(
    kanban_home, monkeypatch
):
    """Ctrl-C / SIGINT is a crash, not a clean exit — the reaper owns it."""
    import types

    import cli as hermes_cli

    conn = _reconciled_hermes_cli_kanban_db_connect.connect()
    try:
        tid, run_id = _claim_running(conn)
        _as_worker(monkeypatch, tid, run_id)
        stub = types.SimpleNamespace()

        hermes_cli._note_kanban_worker_outcome(stub, skip="keyboard_interrupt")
        hermes_cli._reconcile_kanban_terminal_state(stub)

        assert kb.get_task(conn, tid).status == "running"
    finally:
        conn.close()


def test_cli_finalizer_is_a_noop_for_a_normal_cli_run(kanban_home, monkeypatch):
    """No kanban task in env → the exit hook must not touch any board."""
    import types

    import cli as hermes_cli

    conn = _reconciled_hermes_cli_kanban_db_connect.connect()
    try:
        tid, _run_id = _claim_running(conn)
        monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
        stub = types.SimpleNamespace()

        hermes_cli._note_kanban_worker_outcome(stub, response="ordinary answer")
        hermes_cli._reconcile_kanban_terminal_state(stub)

        assert kb.get_task(conn, tid).status == "running"
    finally:
        conn.close()


def test_goal_decision_evidence_reaches_the_durable_blocker(kanban_home, monkeypatch):
    """Goal evidence the loop returned is persisted by the terminal finalizer."""
    conn = _reconciled_hermes_cli_kanban_db_connect.connect()
    try:
        tid, run_id = _claim_running(conn)
        _as_worker(monkeypatch, tid, run_id)

        outcome = kanban_finalize.reconcile_terminal_state(
            goal_decision={
                "outcome": "stopped",
                "turns_used": 3,
                "reason": "run_turn error: RuntimeError",
                "last_response": "halfway through the migration",
                "verdict": "continue",
            },
        )

        assert outcome == "blocked"
        summary = _last_summary(conn, tid)
        assert "run_turn error: RuntimeError" in summary
        assert "halfway through the migration" in summary
        assert "3" in summary
    finally:
        conn.close()
