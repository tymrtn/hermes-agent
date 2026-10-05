"""The no-new-context gate, driven nightly by the real cron scheduler.

Each night is ``cron.scheduler.run_one_job`` on a ``no_agent`` job whose script runs the real CLI
(``dream_cycle_v3.cli.main``) in a subprocess, with the real executions ledger and the real Telegram
delivery path. Only the platform send and the gateway's control-socket answer are scripted; the
gateway side (a dry run is a read-only poll, the mirrored brief is not conversation) is proven in
``tests/gateway/test_scheduled_compression.py``.
"""
import importlib
import json
import os
import sqlite3
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from dream_cycle_v3.dry_run import SAMPLE_DATA

PROFILE = "nagatha-test"
D0 = date(2026, 7, 11)
BRIEF_LINES = "🟠 P1-startups/expatriator\n▶️ Fix the onboarding gap"

# The scheduled script: the real CLI behind a scripted gateway. The gateway compresses only when the
# conversation moved since its last compression, and a dry run never compresses (as the real one).
# ``fail`` stands in for a run that breaks: "after_output" exits non-zero once the brief is written
# (a timeout or crash after the flush), "in_cycle" raises after the store pass ingested tonight's
# sources but before anything was written.
FIXTURE_SCRIPT = """\
import json, sys
sys.path.insert(0, {worktree!r})
from pathlib import Path

import gateway.control_socket as control_socket
from dream_cycle_v3 import cli

STATE = Path({state!r})
HANDOFF = {{"daily_threads": {{"threads": [
    {{"standing": "established", "code": "P1-startups/expatriator", "priority": "P1",
      "key": "startups/expatriator", "group": "startups", "state": "Portal API live",
      "next": "fix the onboarding gap"}}]}}}}


def compress_gateway_session(home, *, profile, cycle_id, dry_run=False, **_kwargs):
    state = json.loads(STATE.read_text())
    state["polls" if dry_run else "requests"] += 1
    row = {{"session_ref": "a1b2c3d4e5f6", "platform": "telegram", "chat_type": "dm"}}
    if state["marker"] == state["conversation"]:
        row.update(status="unchanged", activity_fingerprint=state["conversation"],
                   highlights=HANDOFF)
    elif dry_run:
        row.update(status="eligible")
    else:
        row.update(status="applied", activity_fingerprint=state["conversation"],
                   highlights=HANDOFF, before_tokens=120000, after_tokens=40000,
                   before_messages=200, after_messages=30)
        state["marker"] = state["conversation"]
        state["compressions"] += 1
    STATE.write_text(json.dumps(state))
    return {{"ok": True, "profile": profile, "cycle_id": cycle_id, "eligible": 1, "counts": {{}},
            "sessions": [row]}}


def broken_render(*_args, **_kwargs):
    raise RuntimeError("the cycle broke after ingesting")


control_socket.compress_gateway_session = compress_gateway_session
night = json.loads(STATE.read_text())
if night["fail"] == "in_cycle":
    cli.render_daily_brief = broken_render
code = cli.main(night["argv"])
sys.exit(3 if night["fail"] == "after_output" else code)
"""


def _mtime(day):
    return int(datetime.combine(D0 + timedelta(days=day), datetime.min.time(),
                                tzinfo=timezone.utc).timestamp()) + 12 * 3600


def _put(path, text, day):
    path.parent.mkdir(parents=True, exist_ok=True)
    if text is not None:
        path.write_text(text, encoding="utf-8")
    os.utime(path, (_mtime(day), _mtime(day)))


class Nightly:
    """One profile's cron job, its Telegram target and its scripted gateway."""

    def __init__(self, root, monkeypatch):
        from gateway.config import Platform

        self.root = root
        self.home = root / "profiles" / PROFILE
        self.state_path = root / "fixture-state.json"
        self.v2_state = self.home / "v2-state"
        self.sessions = self.home / "sessions"
        for name in ("projects.json", "threads.json"):
            (self.home / name).write_text((SAMPLE_DATA / name).read_text(encoding="utf-8"),
                                          encoding="utf-8")
        _put(self.v2_state / "wake-up.md", "## Wake\n- carry the thread forward\n", 0)
        _put(self.sessions / "chat.md", "user: the portal API is live\n", 0)
        self.state = {"conversation": "1" * 64, "marker": None, "polls": 0, "requests": 0,
                      "compressions": 0, "argv": [], "fail": None}
        (root / "scripts" / "dc3_fixture.py").write_text(FIXTURE_SCRIPT.format(
            worktree=str(Path(__file__).resolve().parents[2]), state=str(self.state_path)))

        telegram = MagicMock()
        telegram.enabled = True
        config = MagicMock()
        config.platforms = {Platform.TELEGRAM: telegram}
        monkeypatch.setattr("gateway.config.load_gateway_config", lambda *_a, **_k: config)
        self.sent, self.send_results = [], []

        async def send(*args, **kwargs):
            self.sent.append(kwargs.get("content") or args[-1])
            return self.send_results.pop(0)

        monkeypatch.setattr("tools.send_message_tool._send_to_platform", send)

        from cron.jobs import create_job

        self.job = create_job(prompt=None, schedule="every 1d", script="dc3_fixture.py",
                              no_agent=True, deliver="telegram:4242",
                              name="dream-cycle-v3-live-nagatha")

    def run(self, day, *, send=None, fail=None):
        """Night *day*: returns the ledger row for this run. *send* is the platform's answer, if
        anything is sent; *fail* breaks the run (see ``FIXTURE_SCRIPT``)."""
        from cron import executions, scheduler
        from cron.jobs import get_job

        start = D0 + timedelta(days=day)
        self.state["argv"] = [
            "run", "--output", "brief", "--profile", PROFILE, "--owner", "nagatha",
            "--root", f"v2-state={self.v2_state}", "--root", f"sessions={self.sessions}",
            "--migrate-v2-root", "v2-state",
            "--v3-root", str(self.home / "dream-cycle-v3"),
            "--window-start", f"{start}T00:00:00+00:00",
            "--window-end", f"{start + timedelta(days=1)}T00:00:00+00:00",
            "--date", str(start), "--registry", str(self.home / "projects.json"),
            "--threads", str(self.home / "threads.json"),
            "--conversation-compression", "--gateway-home", str(self.home)]
        self.state["fail"] = fail
        self.state_path.write_text(json.dumps(self.state))
        self.send_results[:] = [send] if send is not None else []
        assert scheduler.run_one_job(get_job(self.job["id"]))
        self.state = json.loads(self.state_path.read_text())
        return executions.latest_execution(self.job["id"])

    def cycles(self):
        """Substantive cycles run (one store run each); a poll records none."""
        with sqlite3.connect(self.home / "dream-cycle-v3" / "continuity.db") as conn:
            return conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0]


@pytest.fixture
def nightly(tmp_path, monkeypatch):
    root = tmp_path / "hermes-home"
    (root / "scripts").mkdir(parents=True)
    (root / "cron").mkdir()
    (root / "profiles" / PROFILE).mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(root))
    import hermes_constants
    importlib.reload(hermes_constants)
    import cron.jobs
    importlib.reload(cron.jobs)
    import cron.scheduler
    importlib.reload(cron.scheduler)
    return Nightly(root, monkeypatch)


def test_a_brief_cron_failed_to_deliver_is_resent_unchanged_then_silence_follows(nightly):
    # The brief was written, then the run broke: cron delivered its failure notice, not the brief.
    broke = nightly.run(0, send={"success": True}, fail="after_output")
    assert broke["status"] == "failed" and broke["delivery_outcome"] == "delivered"
    assert BRIEF_LINES not in nightly.sent[0]
    assert nightly.state["compressions"] == 1

    # Nothing new the next night: the kept brief goes out again from the poll alone, with no second
    # compression and no second cycle; this time the send itself fails.
    failed = nightly.run(1, send={"error": "telegram unreachable"})
    assert failed["delivery_outcome"] == "failed"
    assert len(nightly.sent) == 2 and BRIEF_LINES in nightly.sent[1]

    delivered = nightly.run(2, send={"success": True})
    assert delivered["delivery_outcome"] == "delivered"
    assert nightly.sent[2] == nightly.sent[1]
    assert nightly.state["compressions"] == 1 and nightly.cycles() == 1

    # Delivered and still nothing new: silence.
    quiet = nightly.run(3)
    assert quiet["status"] == "completed" and len(nightly.sent) == 3
    assert nightly.state["compressions"] == 1 and nightly.cycles() == 1


def test_a_changed_consumed_source_runs_a_cycle_and_the_cycles_own_echo_does_not(nightly):
    nightly.run(0, send={"success": True})
    assert len(nightly.sent) == 1 and nightly.cycles() == 1

    # The brief was mirrored into the conversation's transcript and the migrated source was
    # re-stamped with the same bytes: bookkeeping, not context. A poll answers; no cycle runs.
    _put(nightly.sessions / "chat.md",
         "user: the portal API is live\n[Cron delivery: dream-cycle-v3-live-nagatha] brief\n", 1)
    _put(nightly.v2_state / "wake-up.md", None, 1)
    nightly.run(1)
    assert len(nightly.sent) == 1 and nightly.cycles() == 1
    assert nightly.state["requests"] == 1 and nightly.state["polls"] >= 1

    # No new conversation, but a source the cycle ingests changed: the cycle runs and says why.
    _put(nightly.v2_state / "wake-up.md", "## Wake\n- book the accountant call\n", 2)
    nightly.run(2, send={"success": True})
    assert nightly.cycles() == 2 and len(nightly.sent) == 2
    assert "📥 1 new source file quarantined for review" in nightly.sent[1]
    assert BRIEF_LINES in nightly.sent[1]
    assert nightly.state["compressions"] == 1

    # A cycle that ingests a changed source and then breaks has delivered nothing for it: the next
    # night, with nothing newer, runs again rather than reading the ingested source as old news.
    _put(nightly.v2_state / "wake-up.md", "## Wake\n- file the quarterly return\n", 3)
    nightly.run(3, send={"success": True}, fail="in_cycle")
    assert nightly.cycles() == 3 and BRIEF_LINES not in nightly.sent[2]
    nightly.run(4, send={"success": True})
    assert nightly.cycles() == 4 and BRIEF_LINES in nightly.sent[3]

    nightly.run(5)
    assert len(nightly.sent) == 4 and nightly.cycles() == 4
    assert nightly.state["compressions"] == 1
