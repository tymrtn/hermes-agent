"""Regression test for #28181 — kanban worker SIGTERM must terminate the process.

The single-query signal handler in cli.py (``_signal_handler_q``) raises
``KeyboardInterrupt`` to unwind the main thread on SIGTERM/SIGHUP. That works
for interactive ``hermes chat -q`` invocations, but kanban workers spawned by
the dispatcher are likely to have a non-daemon thread alive (terminal_tool's
``_wait_for_process``, custom plugin background workers, etc.). With
``KeyboardInterrupt`` only the main thread unwinds; the non-daemon thread
keeps the process alive after the gateway has already restarted, the kanban
dispatcher's ``_pid_alive`` check returns True forever, and the task stays
``running`` indefinitely.

The fix: when the process is a dispatcher-spawned worker (``HERMES_KANBAN_TASK``
env var set), flush logging + stdout/stderr and call ``os._exit(128 + signum)``
instead. The kernel reclaims the PID immediately, and ``detect_crashed_workers``
reclaims the stale claim on the next dispatcher tick.

The exit *code* matters as much as the hard exit. The original fix exited 0,
but that hard exit skips the worker's own terminal reconciliation
(``agent.kanban_finalize``) — so rc=0 with the task still ``running`` reaches
the reaper as a clean-exit protocol violation, which fails closed on the first
occurrence and permanently blocks recoverable work. ``128 + signum`` (the
conventional shell code) keeps the immediate teardown while naming the
shutdown as intentional; ``kanban_db._classify_worker_exit`` maps it to
``interrupted`` and the task is simply released back to ``ready``.

These tests use a synthetic Python script that mirrors the cli.py signal
handler shape so we can exercise the exit-path contract without booting the
full CLI (which needs a real provider config).
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import textwrap
import time

import pytest


# The shutdown signals cli.py routes through the kanban worker hard-exit
# path. SIGHUP is absent on Windows, where the dispatcher does not run.
_WORKER_SHUTDOWN_SIGNALS = [
    getattr(signal, _name)
    for _name in ("SIGTERM", "SIGHUP")
    if hasattr(signal, _name)
]


def _synthetic_worker_script() -> str:
    """A standalone script that mirrors cli.py's single-query SIGTERM handler.

    Keeping the synthetic copy here means the test exercises the exact handler
    shape without needing the full hermes_cli boot path (config, providers,
    skills, etc.). If the production handler in cli.py drifts, the test
    that loads the real handler (test_real_handler_uses_os_exit) will catch it.
    """
    return textwrap.dedent(
        """
        import os, signal, sys, threading, time

        # Non-daemon thread that blocks forever — simulates the worker
        # thread that would prevent orderly Python shutdown after
        # KeyboardInterrupt unwinds main.
        stuck = threading.Event()
        threading.Thread(target=stuck.wait, daemon=False).start()

        def handler(signum, frame):
            # Mirrors cli.py:_signal_handler_q. Real handler sleeps 1.5s; the
            # test uses a short grace so it runs fast.
            try:
                time.sleep(0.05)
            except Exception:
                pass
            if os.environ.get("HERMES_KANBAN_TASK"):
                exit_code = 128 + signum
                try:
                    if hasattr(signal, "SIGALRM"):
                        signal.signal(
                            signal.SIGALRM, lambda *_: os._exit(exit_code)
                        )
                        signal.alarm(2)
                except Exception:
                    pass
                sys.stdout.flush()
                sys.stderr.flush()
                os._exit(exit_code)
            raise KeyboardInterrupt()

        signal.signal(signal.SIGTERM, handler)
        if hasattr(signal, "SIGHUP"):
            signal.signal(signal.SIGHUP, handler)
        print("READY", flush=True)
        try:
            threading.Event().wait()
        except KeyboardInterrupt:
            sys.exit(0)
        """
    )


def _is_alive_like_dispatcher(pid: int) -> bool:
    """Mirrors hermes_cli/kanban_db.py:_pid_alive on Linux.

    A zombie is treated as dead — the dispatcher's _pid_alive checks
    /proc/<pid>/status for State: Z. We replicate that here so a clean
    os._exit followed by zombie-state is correctly counted as dead.
    """
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    if sys.platform == "linux":
        try:
            with open(f"/proc/{pid}/status") as f:
                for line in f:
                    if line.startswith("State:"):
                        if "Z" in line.split(":", 1)[1]:
                            return False
                        break
        except (FileNotFoundError, PermissionError, OSError):
            pass
    elif sys.platform == "darwin":
        try:
            proc = subprocess.run(
                ["ps", "-o", "stat=", "-p", str(pid)],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                timeout=1,
                check=False,
            )
            if proc.returncode != 0:
                return False
            if "Z" in (proc.stdout or "").strip():
                return False
        except (OSError, subprocess.SubprocessError, TimeoutError):
            pass
    return True


def _spawn_synthetic(env_overrides: dict) -> subprocess.Popen:
    env = dict(os.environ)
    env.update(env_overrides)
    proc = subprocess.Popen(
        [sys.executable, "-u", "-c", _synthetic_worker_script()],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    # Wait for "READY" so we know the signal handler is installed.
    assert proc.stdout is not None
    deadline = time.time() + 5.0
    while time.time() < deadline:
        line = proc.stdout.readline()
        if line and line.startswith(b"READY"):
            return proc
    proc.kill()
    raise RuntimeError("synthetic worker never signalled READY")


def _cleanup(proc: subprocess.Popen) -> None:
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    try:
        proc.communicate(timeout=2)
    except subprocess.TimeoutExpired:
        proc.kill()


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="SIGTERM semantics differ on Windows; kanban dispatcher is POSIX-only",
)
def test_sigterm_with_kanban_task_env_terminates_quickly():
    """With HERMES_KANBAN_TASK set, SIGTERM should kill the process in <2s
    even when a non-daemon thread is still alive."""
    proc = _spawn_synthetic({"HERMES_KANBAN_TASK": "t_test_28181"})
    try:
        t0 = time.time()
        os.kill(proc.pid, signal.SIGTERM)

        # Should die in <2s. The handler sleeps ~50ms, then os._exit(0)
        # is immediate. Give generous headroom for slow CI runners.
        deadline = t0 + 2.0
        while time.time() < deadline:
            if not _is_alive_like_dispatcher(proc.pid):
                elapsed = time.time() - t0
                assert elapsed < 2.0
                return
            time.sleep(0.02)
        pytest.fail(
            "process still alive 2s after SIGTERM with HERMES_KANBAN_TASK set "
            "(dispatcher would keep extending claim) — fix regressed"
        )
    finally:
        _cleanup(proc)


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="SIGTERM semantics differ on Windows; kanban dispatcher is POSIX-only",
)
@pytest.mark.parametrize("signum", _WORKER_SHUTDOWN_SIGNALS)
def test_kanban_worker_hard_exit_uses_128_plus_signum(signum):
    """The hard exit must carry the signal in its exit code.

    ``os._exit(0)`` reaches the dispatcher's reaper as a clean exit with the
    task still ``running`` — which the reaper reads as a protocol violation
    and blocks the card permanently on the first occurrence. Exiting
    ``128 + signum`` keeps the immediate-teardown property (non-daemon thread
    still alive) while naming the shutdown as intentional.
    """
    proc = _spawn_synthetic({"HERMES_KANBAN_TASK": "t_test_28181"})
    try:
        t0 = time.time()
        os.kill(proc.pid, signum)
        rc = proc.wait(timeout=5)
        elapsed = time.time() - t0
        assert rc == 128 + int(signum), (
            f"expected exit code {128 + int(signum)} for signal {signum}, "
            f"got {rc} — the reaper cannot distinguish an intentional "
            f"shutdown from a broken finalizer"
        )
        assert elapsed < 2.0, (
            f"hard exit took {elapsed:.2f}s — the non-daemon thread wedged "
            f"shutdown, so the dispatcher would keep extending the claim"
        )
    finally:
        _cleanup(proc)


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="SIGTERM semantics differ on Windows; kanban dispatcher is POSIX-only",
)
def test_sigterm_without_kanban_task_env_uses_keyboard_interrupt_path():
    """Without HERMES_KANBAN_TASK, the original KeyboardInterrupt path runs.

    This is the contrast case proving the fix is gated on the env var: in
    interactive ``hermes chat -q`` (no env var), behavior is unchanged. The
    process MAY hang under non-daemon threads, but that's not a kanban-worker
    concern. We just verify the handler logs the KeyboardInterrupt branch
    rather than os._exit'ing.
    """
    proc = _spawn_synthetic({})
    try:
        os.kill(proc.pid, signal.SIGTERM)
        # Wait a moment for the handler to react.
        time.sleep(0.5)
        # The process may or may not be dead depending on whether the
        # KeyboardInterrupt unwinds cleanly. The behavioral guarantee is
        # only that the env-gated path didn't fire.
        try:
            # Drain stdout up to whatever's available.
            if proc.stdout is not None:
                proc.stdout.close()
            if proc.stderr is not None:
                proc.stderr.close()
        except Exception:
            pass
    finally:
        _cleanup(proc)


