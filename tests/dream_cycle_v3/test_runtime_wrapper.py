"""Phase 4 no-agent wrapper script (scripts/dream_cycle_v3_run.sh).

Thin, tracked, deterministic: env vars/arguments in, one concise line on
success, nonzero with a concise safe message on failure. No LLM calls, no
credentials, no gateway restart.
"""
import os
import shutil
import stat
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

from dream_cycle_v3.dry_run import SAMPLE_DATA, _build_sample_kanban_db

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "dream_cycle_v3_run.sh"

MTIME = int(datetime(2026, 7, 11, 12, 0, tzinfo=timezone.utc).timestamp())


@pytest.fixture
def wrapper_env(tmp_path):
    sources = tmp_path / "sources" / "profile"
    (sources / "state").mkdir(parents=True)
    note = sources / "state" / "wake-up.md"
    note.write_text("## Wake\n- carry the offload thread forward\n",
                    encoding="utf-8")
    os.utime(note, (MTIME, MTIME))
    registry = tmp_path / "registry.json"
    registry.write_text(
        (SAMPLE_DATA / "projects.json").read_text(encoding="utf-8"),
        encoding="utf-8")
    threads = tmp_path / "threads.json"
    threads.write_text(
        (SAMPLE_DATA / "threads.json").read_text(encoding="utf-8"),
        encoding="utf-8")
    kanban_db = _build_sample_kanban_db(
        tmp_path / "trackers" / "kanban" / "kanban.db")
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": os.environ.get("HOME", str(tmp_path)),
        "PYTHON": sys.executable,
        "DC3_PROFILE": "nagatha-test",
        "DC3_OWNER": "nagatha",
        "DC3_ROOTS": f"profile={sources}",
        "DC3_SHADOW_ROOT": str(tmp_path / "v3-shadow"),
        "DC3_WINDOW_START": "2026-07-11T00:00:00+00:00",
        "DC3_WINDOW_END": "2026-07-12T00:00:00+00:00",
        "DC3_DATE": "2026-07-11",
        "DC3_AS_OF": "2026-07-12T00:30:00+00:00",
        "DC3_REGISTRY": str(registry),
        "DC3_THREADS": str(threads),
        "DC3_KANBAN_DB": str(kanban_db),
        "DC3_KANBAN_BOARD": "sample-board",
        "DC3_TODOIST_EXPORT": str(SAMPLE_DATA / "todoist_export.json"),
        "DC3_MIGRATE_V2_ROOTS": "profile",
    }
    return env


def run_wrapper(env):
    return subprocess.run(["bash", str(SCRIPT)], env=env, cwd=REPO,
                          capture_output=True, text=True, timeout=300)


def test_wrapper_is_tracked_and_executable():
    assert SCRIPT.is_file()
    assert SCRIPT.stat().st_mode & stat.S_IXUSR


def test_wrapper_success_prints_one_concise_line(wrapper_env):
    proc = run_wrapper(wrapper_env)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.count("\n") == 1
    assert proc.stdout.startswith("dream-cycle-v3 cycle ok mode=shadow ")


def test_wrapper_accepts_python_command_name_from_path(wrapper_env, tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "dc3-python").symlink_to(shutil.which("true"))
    env = dict(wrapper_env)
    env["PATH"] = f"{bin_dir}{os.pathsep}{env['PATH']}"
    env["PYTHON"] = "dc3-python"
    proc = run_wrapper(env)
    assert proc.returncode == 0, proc.stderr


def test_wrapper_missing_required_env_fails_concisely(wrapper_env):
    env = dict(wrapper_env)
    del env["DC3_PROFILE"]
    proc = run_wrapper(env)
    assert proc.returncode != 0
    assert proc.stdout == ""
    assert "DC3_PROFILE" in proc.stderr
    assert len(proc.stderr.splitlines()) <= 2


def test_wrapper_requires_an_output_root(wrapper_env):
    env = dict(wrapper_env)
    del env["DC3_SHADOW_ROOT"]
    proc = run_wrapper(env)
    assert proc.returncode != 0
    assert proc.stdout == ""
    assert "DC3_V3_ROOT" in proc.stderr or "DC3_SHADOW_ROOT" in proc.stderr


def test_wrapper_runtime_failure_is_nonzero_and_safe(wrapper_env, tmp_path):
    env = dict(wrapper_env)
    env["DC3_ROOTS"] = f"profile={tmp_path / 'does-not-exist'}"
    proc = run_wrapper(env)
    assert proc.returncode != 0
    assert proc.stdout == ""
    assert proc.stderr.strip(), "failure must carry a concise message"
