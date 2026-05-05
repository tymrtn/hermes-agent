"""Tests for the AFK open-thread store, tools, and cron pre-run script."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from gateway import open_threads


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------

def test_add_creates_thread_with_id_and_persists():
    t = open_threads.add(
        title="research X",
        description="dig into X",
        side_effects=["research"],
    )
    assert t.id and len(t.id) == 12
    assert t.title == "research X"
    assert t.status == "open"
    assert t.safety == "safe"
    assert t.is_eligible() is True

    threads = open_threads.list_threads()
    assert [x.id for x in threads] == [t.id]


def test_add_rejects_empty_title():
    with pytest.raises(ValueError):
        open_threads.add(title="   ")


def test_blocked_side_effect_forces_safety_blocked():
    """Recording a thread with a blocked side-effect tag must NOT allow auto-run."""
    t = open_threads.add(
        title="send the announcement",
        side_effects=["email", "publish"],
    )
    assert t.safety == "blocked"
    assert t.is_eligible() is False


def test_disallowed_side_effect_is_ineligible():
    """A side-effect not in the allowed set blocks cron pickup, even if 'safe'."""
    t = open_threads.add(
        title="draft a thing",
        safety="safe",
        side_effects=["draft"],  # explicitly excluded tonight
    )
    assert t.is_eligible() is False


def test_attempt_cap_makes_thread_ineligible():
    t = open_threads.add(
        title="research Y",
        side_effects=["research"],
    )
    for _ in range(t.max_attempts):
        open_threads.update(t.id, bump_attempt=True)
    refreshed = open_threads.list_threads()[0]
    assert refreshed.attempt_count == t.max_attempts
    assert refreshed.is_eligible() is False


def test_status_other_than_open_is_ineligible():
    t = open_threads.add(title="r", side_effects=["research"])
    open_threads.update(t.id, status="done", result_summary="found nothing")
    refreshed = open_threads.list_threads(status=None)[0]
    assert refreshed.status == "done"
    assert refreshed.is_eligible() is False


def test_pick_one_bumps_attempt_before_returning():
    """pick_one must mark the attempt before emitting so a crash doesn't loop."""
    t = open_threads.add(title="r", side_effects=["research"])
    chosen = open_threads.pick_one()
    assert chosen is not None
    assert chosen.id == t.id
    assert chosen.attempt_count == 1
    assert chosen.last_attempted_at is not None

    # On disk, the bump must be persisted.
    persisted = open_threads.list_threads(status=None)[0]
    assert persisted.attempt_count == 1


def test_pick_one_returns_none_when_no_eligible():
    open_threads.add(title="x", side_effects=["draft"])  # ineligible
    assert open_threads.pick_one() is None


def test_abandon_records_reason_in_summary():
    t = open_threads.add(title="r", side_effects=["research"])
    out = open_threads.abandon(t.id, reason="user said skip")
    assert out is not None
    assert out.status == "abandoned"
    assert "user said skip" in (out.result_summary or "")


def test_eligible_threads_filters_correctly():
    a = open_threads.add(title="a", side_effects=["research"])
    open_threads.add(title="b", side_effects=["draft"])  # excluded
    open_threads.add(title="c", side_effects=["email"])  # blocked
    eligible = open_threads.eligible_threads()
    assert [t.id for t in eligible] == [a.id]


def test_store_path_uses_hermes_home(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    assert open_threads.store_path() == tmp_path / "open_threads.json"


def test_corrupt_ledger_returns_empty(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "open_threads.json").write_text("{not json")
    assert open_threads.list_threads(status=None) == []


# ---------------------------------------------------------------------------
# Tool registration + handlers
# ---------------------------------------------------------------------------

def test_open_thread_tools_registered():
    # Importing the module triggers registration.
    import importlib
    importlib.import_module("tools.open_thread_tools")

    from tools.registry import registry as _registry
    for name in (
        "open_thread_add",
        "open_thread_list",
        "open_thread_update",
        "open_thread_abandon",
    ):
        entry = _registry.get_entry(name)
        assert entry is not None, f"{name} not registered"
        assert entry.toolset == "open_threads"


def test_tool_add_then_list_roundtrip():
    from tools import open_thread_tools as ott

    add_resp = json.loads(ott._handle_add({
        "title": "research the regex",
        "description": "look at the regex carefully",
        "side_effects": ["research"],
    }))
    assert add_resp["success"] is True
    assert add_resp["eligible_for_afk"] is True
    tid = add_resp["thread_id"]

    list_resp = json.loads(ott._handle_list({"status": "open"}))
    assert list_resp["count"] == 1
    assert list_resp["threads"][0]["id"] == tid


def test_tool_update_marks_done():
    from tools import open_thread_tools as ott

    add_resp = json.loads(ott._handle_add({
        "title": "summarize the doc",
        "side_effects": ["summarize"],
    }))
    tid = add_resp["thread_id"]

    update_resp = json.loads(ott._handle_update({
        "thread_id": tid,
        "status": "done",
        "result_summary": "Doc is 4 pages, key claim is X.",
    }))
    assert update_resp["success"] is True
    assert update_resp["thread"]["status"] == "done"
    assert update_resp["thread"]["result_summary"].startswith("Doc is 4 pages")


def test_tool_update_unknown_thread_returns_error():
    from tools import open_thread_tools as ott
    resp = json.loads(ott._handle_update({"thread_id": "ffffffffffff", "status": "done"}))
    assert "error" in resp


def test_tool_add_rejects_blocked_side_effect_quietly():
    """Blocked side-effects are recorded but NOT eligible for cron pickup."""
    from tools import open_thread_tools as ott
    resp = json.loads(ott._handle_add({
        "title": "publish the post",
        "side_effects": ["publish"],
    }))
    assert resp["success"] is True
    assert resp["eligible_for_afk"] is False
    assert resp["thread"]["safety"] == "blocked"


# ---------------------------------------------------------------------------
# Cron pre-run script
# ---------------------------------------------------------------------------

SCRIPT_PATH = (
    Path(__file__).resolve().parent.parent
    / "scripts" / "afk_open_threads_check.py"
)


def _run_script(args: list[str], hermes_home: Path) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["HERMES_HOME"] = str(hermes_home)
    return subprocess.run(
        [sys.executable, str(SCRIPT_PATH), *args],
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )


def test_script_silent_when_no_threads(tmp_path):
    proc = _run_script(
        ["--enabled", "--no-require-idle", "--idle-seconds", "0"],
        tmp_path,
    )
    assert proc.returncode == 0
    assert proc.stdout == ""


def test_script_disabled_by_default(tmp_path, monkeypatch):
    """Without --enabled or env opt-in, the script must be a silent no-op."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    open_threads.add(title="r", side_effects=["research"], hermes_home=tmp_path)
    proc = _run_script(["--no-require-idle", "--idle-seconds", "0"], tmp_path)
    assert proc.returncode == 0
    assert proc.stdout == ""
    # And no attempt bump — we never even looked at the ledger.
    persisted = open_threads.list_threads(status=None, hermes_home=tmp_path)
    assert persisted[0].attempt_count == 0


def test_script_enabled_via_env_var(tmp_path, monkeypatch):
    """HERMES_OPEN_THREADS_ENABLED=1 should be equivalent to --enabled."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    open_threads.add(title="r", side_effects=["research"], hermes_home=tmp_path)
    env = os.environ.copy()
    env["HERMES_HOME"] = str(tmp_path)
    env["HERMES_OPEN_THREADS_ENABLED"] = "1"
    proc = subprocess.run(
        [sys.executable, str(SCRIPT_PATH), "--no-require-idle", "--idle-seconds", "0"],
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )
    assert proc.returncode == 0
    assert "AFK Open-Thread Run" in proc.stdout


def test_script_emits_prompt_when_eligible_thread_and_idle(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    open_threads.add(title="research the regex", side_effects=["research"])
    proc = _run_script(
        ["--enabled", "--no-require-idle", "--idle-seconds", "0"],
        tmp_path,
    )
    assert proc.returncode == 0
    assert "AFK Open-Thread Run" in proc.stdout
    assert "open_thread_update" in proc.stdout
    assert "research the regex" in proc.stdout
    # Hard rules are present.
    assert "do exactly one thread" in proc.stdout.lower()
    assert "exactly [SILENT]" in proc.stdout
    assert "publish" in proc.stdout  # listed as a blocked action
    # Attempt counter must have been bumped on disk.
    persisted = open_threads.list_threads(status=None, hermes_home=tmp_path)
    assert persisted[0].attempt_count == 1


def test_script_silent_when_user_active(tmp_path, monkeypatch):
    """If sessions.json is fresh, the script must not fire even with eligible threads."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()
    sessions_file = sessions_dir / "sessions.json"
    sessions_file.write_text("{}")
    # Touch to "now" — user is active.
    os.utime(sessions_file, (time.time(), time.time()))

    open_threads.add(title="r", side_effects=["research"], hermes_home=tmp_path)
    proc = _run_script(["--enabled", "--idle-seconds", "3600"], tmp_path)
    assert proc.returncode == 0
    assert proc.stdout == ""
    persisted = open_threads.list_threads(status=None, hermes_home=tmp_path)
    # No attempt was bumped because we never picked.
    assert persisted[0].attempt_count == 0


def test_script_silent_when_only_blocked_threads(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    open_threads.add(title="send email", side_effects=["email"], hermes_home=tmp_path)
    proc = _run_script(
        ["--enabled", "--no-require-idle", "--idle-seconds", "0"],
        tmp_path,
    )
    assert proc.returncode == 0
    assert proc.stdout == ""


def test_script_max_attempts_caps_pickup(tmp_path, monkeypatch):
    """--max-attempts is a script-level soft cap below the per-thread cap."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    t = open_threads.add(title="r", side_effects=["research"], hermes_home=tmp_path)
    # Pre-bump attempt count to 1 so a --max-attempts=1 cap should skip.
    open_threads.update(t.id, bump_attempt=True, hermes_home=tmp_path)
    proc = _run_script(
        [
            "--enabled",
            "--no-require-idle",
            "--idle-seconds",
            "0",
            "--max-attempts",
            "1",
        ],
        tmp_path,
    )
    assert proc.returncode == 0
    assert proc.stdout == ""
    persisted = open_threads.list_threads(status=None, hermes_home=tmp_path)
    # No additional bump — cap blocked the pick.
    assert persisted[0].attempt_count == 1


def test_script_max_attempts_skips_at_cap_thread_and_picks_next(tmp_path, monkeypatch):
    """The script-level cap must filter before bumping, not after pick_one()."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    at_cap = open_threads.add(title="already tried", side_effects=["research"], hermes_home=tmp_path)
    open_threads.update(at_cap.id, bump_attempt=True, hermes_home=tmp_path)
    fresh = open_threads.add(title="fresh research", side_effects=["research"], hermes_home=tmp_path)

    proc = _run_script(
        [
            "--enabled",
            "--no-require-idle",
            "--idle-seconds",
            "0",
            "--max-attempts",
            "1",
        ],
        tmp_path,
    )

    assert proc.returncode == 0
    assert "fresh research" in proc.stdout
    persisted = {t.id: t for t in open_threads.list_threads(status=None, hermes_home=tmp_path)}
    assert persisted[at_cap.id].attempt_count == 1
    assert persisted[fresh.id].attempt_count == 1


def test_open_thread_tools_in_core_toolset():
    """Tool names are wired into the default Hermes core toolset."""
    from toolsets import _HERMES_CORE_TOOLS
    for name in (
        "open_thread_add",
        "open_thread_list",
        "open_thread_update",
        "open_thread_abandon",
    ):
        assert name in _HERMES_CORE_TOOLS, f"{name} missing from _HERMES_CORE_TOOLS"
