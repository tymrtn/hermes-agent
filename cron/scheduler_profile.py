"""Per-job profile execution, with bounded isolation of process environment changes.

The cron store stays with the job owner; config, secrets and delivery use the
selected execution profile. Ordinary jobs share the read side of the lock.
"""
from contextlib import contextmanager
import logging
import os
import threading
import time
from typing import Optional

logger = logging.getLogger("cron.scheduler")


class _ReadWriteLock:
    """Exclude profile environment writers while allowing ordinary cron runs in parallel."""

    def __init__(self) -> None:
        self._cond = threading.Condition(threading.Lock())
        self._readers = 0
        self._writer_active = False
        self._writers_waiting = 0

    def acquire_read(self, timeout: float | None = None) -> bool:
        """Acquire a read lock.

        Returns ``True`` if the lock was acquired, ``False`` on timeout.
        """
        deadline = (
            time.monotonic() + timeout if timeout is not None else None
        )
        with self._cond:
            while self._writer_active or self._writers_waiting > 0:
                if deadline is not None:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        self._cond.notify_all()
                        return False
                    self._cond.wait(timeout=remaining)
                else:
                    self._cond.wait()
            self._readers += 1
        return True

    def release_read(self) -> None:
        with self._cond:
            self._readers -= 1
            if self._readers == 0:
                self._cond.notify_all()

    def acquire_write(self, timeout: float | None = None) -> bool:
        """Acquire a write lock.

        Returns ``True`` if the lock was acquired, ``False`` on timeout.
        """
        deadline = (
            time.monotonic() + timeout if timeout is not None else None
        )
        with self._cond:
            self._writers_waiting += 1
            try:
                while self._writer_active or self._readers > 0:
                    if deadline is not None:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            self._cond.notify_all()
                            return False
                        self._cond.wait(timeout=remaining)
                    else:
                        self._cond.wait()
            finally:
                self._writers_waiting -= 1
            self._writer_active = True
        return True

    def release_write(self) -> None:
        with self._cond:
            self._writer_active = False
            self._cond.notify_all()


_profile_env_lock = _ReadWriteLock()


def _cwd_lock_timeout_seconds() -> float:
    from cron.scheduler import _cron_inactivity_seconds
    inactivity = _cron_inactivity_seconds()
    return max(inactivity if inactivity > 0 else 600.0, 120.0) + 60.0


@contextmanager
def _job_profile_context(job_id: str, profile: Optional[str]):
    """Temporarily run a cron job under a specific Hermes profile."""
    from pathlib import Path
    raw_profile = str(profile or '').strip()
    if not raw_profile:
        yield None
        return
    env_snapshot = os.environ.copy()
    from hermes_cli.profiles import normalize_profile_name, resolve_profile_env
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override
    normalized_profile = normalize_profile_name(raw_profile)
    try:
        profile_home = Path(resolve_profile_env(normalized_profile)).resolve()
    except (FileNotFoundError, ValueError) as exc:
        logger.warning("Job '%s': configured profile %r no longer valid (%s) — falling back to scheduler default", job_id, raw_profile, exc)
        yield None
        return
    override_token = None
    secret_token = None
    from agent.secret_scope import (
        build_profile_secret_scope, set_secret_scope, reset_secret_scope)
    try:
        override_token = set_hermes_home_override(profile_home)
        from hermes_cli.env_loader import load_hermes_dotenv, reset_secret_source_cache
        reset_secret_source_cache()
        load_hermes_dotenv(hermes_home=profile_home)
        secret_token = set_secret_scope(build_profile_secret_scope(profile_home))
        logger.info("Job '%s': using Hermes profile '%s' (%s)", job_id, normalized_profile, profile_home)
        yield normalized_profile
    finally:
        if secret_token is not None:
            reset_secret_scope(secret_token)
        if override_token is not None:
            reset_hermes_home_override(override_token)
        added = set(os.environ.keys()) - set(env_snapshot.keys())
        for key in added:
            os.environ.pop(key, None)
        for key, value in env_snapshot.items():
            if os.environ.get(key) != value:
                os.environ[key] = value


def run_one_job_profiled(job: dict, adapters: Optional[dict]=None, loop=None, verbose: bool=False, execution_id: Optional[str]=None, extra_prompt: Optional[str]=None, cancel_event=None) -> bool:
    """Run one job behind the profile/env isolation used by scheduler ticks.

    Manual tool runs and external scheduler providers must use the same lane as
    due-job dispatch. Otherwise a profile-pinned job inherits the caller's
    config, secrets, and live delivery adapter. The lock wait is bounded by the
    cron inactivity budget so a wedged profile job cannot block the fleet
    indefinitely before the job watchdog starts.
    """
    from cron.scheduler import run_one_job
    from cron.executions import create_execution
    from cron.executions import finish_execution
    from cron.executions import mark_execution_running
    from cron.jobs import mark_job_run
    has_profile_override = bool(str(job.get('profile') or '').strip())
    timeout = _cwd_lock_timeout_seconds()
    if has_profile_override:
        acquired = _profile_env_lock.acquire_write(timeout=timeout)
    else:
        acquired = _profile_env_lock.acquire_read(timeout=timeout)
    if not acquired:
        mode = 'write' if has_profile_override else 'read'
        error = f'Timed out waiting for the cron profile/env {mode} lock after {timeout:.0f}s — another profile-pinned cron job appears wedged'
        logger.error("Job '%s': %s", job['id'], error)
        ledger_execution_id = execution_id or job.get('execution_id')
        if not ledger_execution_id:
            ledger_execution_id = create_execution(job['id'], source='direct')['id']
        try:
            mark_execution_running(ledger_execution_id)
        except Exception as record_err:
            logger.error('Failed to mark execution %s running: %s', ledger_execution_id, record_err)
        try:
            mark_job_run(job['id'], False, error)
        except Exception as record_err:
            logger.error('Failed to record profile-lock timeout for job %s: %s', job['id'], record_err)
        try:
            finish_execution(ledger_execution_id, success=False, error=error)
        except Exception as record_err:
            logger.error('Failed to finish execution %s: %s', ledger_execution_id, record_err)
        return True
    try:
        from cron.jobs import get_cron_output_dir, use_cron_store
        store_home = get_cron_output_dir().parent.parent
        with use_cron_store(store_home), _job_profile_context(job['id'], job.get('profile')):
            delivery_adapters = None if has_profile_override else adapters
            delivery_loop = None if has_profile_override else loop
            cancel_kwargs = {"cancel_event": cancel_event} if cancel_event is not None else {}
            return run_one_job(job, adapters=delivery_adapters, loop=delivery_loop, verbose=verbose, execution_id=execution_id, extra_prompt=extra_prompt, **cancel_kwargs)
    finally:
        if has_profile_override:
            _profile_env_lock.release_write()
        else:
            _profile_env_lock.release_read()
