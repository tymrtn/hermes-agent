"""Phase 4 cron-contract: the tracked, INACTIVE Nagatha shadow cron artifact.

Validates the exact v3 cron-definition artifact and its shim:
- the definition is inactive (enabled=false, state=paused) and activates
  nothing by existing in the repo;
- it invokes the tracked no-agent wrapper in SHADOW mode only;
- it does not activate/restart/pause/remove legacy jobs or the gateway;
- it has no promotion or live destination path;
- the publication checklist preserves the legacy v2 job and requires seven
  elapsed shadow days before cutover.
"""
import json
import os
import re
import stat
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from cron.jobs import parse_schedule
from cron.lifecycle_guard import contains_gateway_lifecycle_command
from dream_cycle_v3.dry_run import SAMPLE_DATA

REPO = Path(__file__).resolve().parents[2]
ARTIFACT = REPO / "docs" / "dream-cycle-v3" / "nagatha-shadow-cron.job.json"
SHIM = REPO / "scripts" / "dream_cycle_v3_shadow_cron.sh"
WRAPPER = REPO / "scripts" / "dream_cycle_v3_run.sh"
CHECKLIST = (REPO / "docs" / "dream-cycle-v3" /
             "nagatha-shadow-publication-checklist.md")


@pytest.fixture(scope="module")
def job():
    return json.loads(ARTIFACT.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def shim_text():
    return SHIM.read_text(encoding="utf-8")


def shim_commands(text):
    """Non-comment, non-empty shim lines — what would actually execute."""
    return [line for line in text.splitlines()
            if line.strip() and not line.strip().startswith("#")]


def shim_var(text, name):
    """The exact double-quoted value a shim `export NAME="..."` assigns."""
    matches = re.findall(rf'^export {name}="(.*)"$', text, re.MULTILINE)
    assert len(matches) == 1, f"{name} must be exported exactly once"
    return matches[0]


def test_artifact_is_an_exact_inactive_job_definition(job):
    assert job["enabled"] is False
    assert job["state"] == "paused"
    assert job["paused_reason"], "must explain why it ships paused"
    assert job["no_agent"] is True
    assert job["script"] == "dream_cycle_v3_shadow_cron.sh"
    assert job["prompt"] == ""
    # `hermes -p nagatha cron create` selects the Nagatha cron STORE (via the
    # global -p selector) but there is no supported CLI flag to set a per-job
    # execution `profile` (cron_create in hermes_cli/cron.py never forwards
    # one to the cronjob tool). This no-agent script needs no profile-scoped
    # agent execution, so the tracked artifact must match what create
    # actually produces rather than claim a field the CLI cannot set.
    assert job["profile"] is None
    assert job["deliver"] == "local"
    assert job["repeat"] == {"times": None, "completed": 0}
    # the stored schedule must be exactly what the live scheduler would
    # parse from the display string — validated by the real parser, not a
    # shape regex
    parsed = parse_schedule(job["schedule_display"])
    assert parsed == job["schedule"]
    assert parsed["kind"] == "cron"
    # inactive: nothing is armed
    assert job["next_run_at"] is None
    assert job["last_run_at"] is None


def test_shim_is_tracked_executable_and_invokes_the_wrapper(shim_text):
    assert SHIM.is_file()
    assert SHIM.stat().st_mode & stat.S_IXUSR
    assert WRAPPER.is_file(), "the tracked no-agent wrapper must exist"
    assert "dream_cycle_v3_run.sh" in shim_text


def test_shim_is_shadow_mode_only(shim_text):
    assert "DC3_SHADOW_ROOT" in shim_text
    assert "DC3_V3_ROOT" not in shim_text
    assert "--v3-root" not in shim_text


def test_no_legacy_job_or_gateway_mutation(job, shim_text):
    for text in (shim_text, json.dumps(job)):
        assert not contains_gateway_lifecycle_command(text)
    forbidden = re.compile(
        r"\bcron\s+(pause|resume|remove|rm|delete|create)\b|"
        r"\bgateway\s+(restart|stop)\b", re.IGNORECASE)
    for line in shim_commands(shim_text):
        assert not forbidden.search(line), line


def test_no_promotion_or_live_destination_path(shim_text):
    for line in shim_commands(shim_text):
        lowered = line.lower()
        assert "promot" not in lowered, line
        assert "dry-run-phase2" not in lowered, line
        assert "memory:" not in lowered, line


def test_shim_reads_the_real_current_sources(shim_text):
    """Named roots for v2 state, v2 runs, and Nagatha sessions — and never
    the old nonexistent profile state path."""
    assert "$HOME/.hermes/profiles/nagatha/state\"" not in shim_text
    assert "$HOME/.hermes/profiles/nagatha/state " not in shim_text
    roots = dict(part.split("=", 1)
                 for part in shim_var(shim_text, "DC3_ROOTS").split())
    assert roots == {
        "v2-state": "$HOME/.hermes/dream-cycle/state/nagatha",
        "v2-runs": "$HOME/.hermes/dream-cycle/runs",
        "sessions": "$HOME/.hermes/profiles/nagatha/sessions",
    }


def test_shim_migrates_only_the_v2_artifact_roots(shim_text):
    migrate = set(shim_var(shim_text, "DC3_MIGRATE_V2_ROOTS").split())
    assert migrate == {"v2-state", "v2-runs"}


def test_date_window_computation_uses_placeholders_over_real_paths(shim_text):
    # real intended Nagatha paths, literal
    assert shim_var(shim_text, "DC3_SHADOW_ROOT") == \
        "$HOME/.hermes/dream-cycle/v3-shadow"
    # the only computed values are the current date/window
    assert "DC3_WINDOW_START" in shim_text and "DC3_WINDOW_END" in shim_text
    assert "timedelta" in shim_text or "date -u" in shim_text


def checklist_text():
    """Checklist with markdown hard-wrapping collapsed, for exact-phrase
    assertions."""
    return " ".join(CHECKLIST.read_text(encoding="utf-8").split())


def test_checklist_preserves_v2_and_requires_seven_shadow_days():
    text = checklist_text()
    assert "legacy v2" in text
    assert "MUST NOT be paused, removed, or modified" in text
    assert "seven distinct elapsed shadow days" in text
    assert "cutover-gate" in text


def test_checklist_identifies_the_live_legacy_and_productivity_jobs():
    """The observed live identity of the jobs being preserved — both live
    in the NAGATHA cron store; store ownership is distinct from each job's
    execution `profile` field — plus the requirement to re-verify at
    publication time."""
    text = checklist_text()
    assert "0ec6fab53a91" in text          # legacy v2 job id
    assert "30 2 * * *" in text            # legacy v2 schedule
    assert "3165dba05f75" in text          # productivity job id
    assert "execution `profile`" in text   # store vs execution distinction
    assert "re-verif" in text.lower()
    assert "productivity" in text.lower()
    # both jobs live in the Nagatha cron STORE — never claim the root/
    # default store holds them
    assert "$HOME/.hermes/profiles/nagatha/cron/jobs.json" in text
    assert "$HOME/.hermes/cron/jobs.json" not in text
    assert "legacy v2 dream-cycle job in the Nagatha profile" not in text


def test_checklist_uses_the_supported_profile_scoped_cron_workflow():
    """Publication goes through hermes cron (locking + atomic save), always
    via the explicit `-p nagatha` selector against the Nagatha cron store
    — never an unscoped cron command, raw jobs.json edit, or wrong store."""
    text = checklist_text()
    assert "hermes -p nagatha cron create" in text
    assert "hermes -p nagatha cron pause" in text
    assert "hermes -p nagatha cron list --all" in text
    assert "hermes --profile" not in text, \
        "cron commands should use the checklist's single -p convention"
    assert "hermes -p default cron list" not in text, \
        "legacy/productivity verification must check the Nagatha store"
    assert "Never edit jobs.json by hand" in text
    assert "Append the job object" not in text
    # create-then-immediately-pause is allowed only with explicit
    # future-schedule verification
    assert "next_run_at" in text and "future" in text


def test_checklist_documents_scheduler_artifacts_and_full_rollback():
    """Confinement and rollback must name every scheduler-owned artifact,
    not just the shadow root (codex phase-4 finding 13)."""
    text = checklist_text()
    assert "$HOME/.hermes/profiles/nagatha/cron/jobs.json" in text
    assert "$HOME/.hermes/profiles/nagatha/cron/output/" in text
    assert "$HOME/.hermes/profiles/nagatha/scripts/" in text
    assert "hermes -p nagatha cron remove" in text
    # rollback covers output logs and the installed shim, not only the job
    assert "cron/output/<job id>" in text or "cron/output/<job-id>" in text


def test_shim_runs_the_wrapper_in_shadow_mode(tmp_path):
    """Behavioral proof: under a sandboxed HOME the shim executes one real
    shadow cycle via the tracked wrapper and touches only the shadow root."""
    home = tmp_path
    hermes = home / ".hermes"
    (hermes).mkdir()
    (hermes / "hermes-agent").symlink_to(REPO)

    yesterday_noon = datetime.now(timezone.utc).replace(
        hour=12, minute=0, second=0, microsecond=0) - timedelta(days=1)
    mtime = int(yesterday_noon.timestamp())

    # the real current source layout: v2 state, v2 dated runs, sessions
    v2_state = hermes / "dream-cycle" / "state" / "nagatha"
    v2_runs = hermes / "dream-cycle" / "runs"
    sessions = hermes / "profiles" / "nagatha" / "sessions"
    run_dir = v2_runs / yesterday_noon.date().isoformat()
    for d in (v2_state, run_dir, sessions):
        d.mkdir(parents=True)
    files = {
        v2_state / "wake-up.md":
            "## Wake\n- carry the offload thread forward\n",
        run_dir / "summary.md": "- v2 run summary for the day\n",
        sessions / "20260712-session.jsonl":
            '{"role": "user", "content": "status update"}\n',
    }
    for path, content in files.items():
        path.write_text(content, encoding="utf-8")
        os.utime(path, (mtime, mtime))

    config = hermes / "dream-cycle" / "v3-config"
    config.mkdir(parents=True)
    for name in ("projects.json", "threads.json"):
        (config / name).write_text(
            (SAMPLE_DATA / name).read_text(encoding="utf-8"),
            encoding="utf-8")

    read_roots = (v2_state, v2_runs, sessions)

    def fingerprint():
        return sorted(
            (str(p), p.stat().st_mtime, p.read_bytes())
            for root in read_roots
            for p in root.rglob("*") if p.is_file())

    sources_before = fingerprint()

    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(home),
        "PYTHON": sys.executable,
    }
    proc = subprocess.run(["bash", str(SHIM)], env=env,
                          capture_output=True, text=True, timeout=300)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.count("\n") == 1
    assert proc.stdout.startswith("dream-cycle-v3 cycle ok mode=shadow ")

    shadow_root = hermes / "dream-cycle" / "v3-shadow"
    assert (shadow_root / "continuity.db").is_file()

    assert fingerprint() == sources_before, "read roots must stay untouched"
