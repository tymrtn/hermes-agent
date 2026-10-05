"""The no-new-context gate: a Dream Cycle repeats only when there is new conversation or context.

Driven through the real CLI (`python -m dream_cycle_v3 run`), the real runtime, the real client
classification and the real cron no-agent delivery code. Only the gateway's control-socket answer is
scripted; the gateway side of the same gate (no model call for an unchanged conversation, the
cycle's own mirrored brief ignored) is proven in `tests/gateway/test_scheduled_compression.py`.
"""
import json
from datetime import date, datetime, timedelta, timezone

import pytest

from .conftest import write_tree

from dream_cycle_v3 import cli
from dream_cycle_v3.dry_run import SAMPLE_DATA

PROFILE = "nagatha-test"
SESSION_REF = "a1b2c3d4e5f6"
F1, F2 = "1" * 64, "2" * 64
D1 = "2026-07-11"
SOURCE_MTIME = int(datetime(2026, 7, 11, 12, 0, tzinfo=timezone.utc).timestamp())
HANDOFF = {"daily_threads": {"threads": [
    {"standing": "established", "code": "P1-startups/expatriator", "priority": "P1",
     "key": "startups/expatriator", "group": "startups", "state": "Portal API live",
     "next": "fix the onboarding gap"}]}}


@pytest.fixture
def env(tmp_path, monkeypatch):
    root = tmp_path / "hermes-home"
    home = root / "profiles" / PROFILE
    home.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(root))
    sources = write_tree(home / "sources",
                         {"state/wake-up.md": "## Wake\n- carry the thread forward\n"},
                         mtime=SOURCE_MTIME)
    for name in ("projects.json", "threads.json"):
        (home / name).write_text((SAMPLE_DATA / name).read_text(encoding="utf-8"),
                                 encoding="utf-8")
    return {"home": home, "sources": sources}


def _row(status, fingerprint, **over):
    row = {"session_ref": SESSION_REF, "platform": "telegram", "chat_type": "dm",
           "status": status, "activity_fingerprint": fingerprint, "highlights": HANDOFF}
    if status == "applied":
        row.update(before_tokens=120_000, after_tokens=40_000, before_messages=200,
                   after_messages=30)
    row.update(over)
    return row


@pytest.fixture
def gateway(monkeypatch):
    """The gateway's control-socket answers, one per cycle, in order; None is a silent gateway."""
    import gateway.control_socket as control_socket

    answers, calls = [], []

    def compress_gateway_session(home, *, profile, cycle_id, **_kwargs):
        calls.append(cycle_id)
        answer = answers.pop(0)
        if answer is None:
            return None
        return {"ok": True, "profile": profile, "cycle_id": cycle_id, "eligible": 1,
                "counts": {}, "sessions": [answer]}

    monkeypatch.setattr(control_socket, "compress_gateway_session", compress_gateway_session)
    return answers, calls


def _run(env, day, capsys, output="brief"):
    """One nightly cron run of the CLI for *day*; returns (exit code, stdout)."""
    start = date.fromisoformat(D1) + timedelta(days=day)
    end = start + timedelta(days=1)
    code = cli.main([
        "run", "--output", output, "--profile", PROFILE, "--owner", "nagatha",
        "--root", f"profile={env['sources']}", "--v3-root", str(env["home"] / "dream-cycle-v3"),
        "--window-start", f"{start}T00:00:00+00:00", "--window-end", f"{end}T00:00:00+00:00",
        "--date", str(start), "--registry", str(env["home"] / "projects.json"),
        "--threads", str(env["home"] / "threads.json"),
        "--conversation-compression", "--gateway-home", str(env["home"])])
    return code, capsys.readouterr().out


def _delivered(stdout, monkeypatch):
    """What the real cron no-agent runner would deliver for this script stdout (None = silence)."""
    from cron import scheduler

    monkeypatch.setattr(scheduler, "_run_job_script_with_claim_heartbeat",
                        lambda *_a, **_k: (True, stdout))
    ok, _doc, final, _err = scheduler._run_no_agent_job(
        {"id": "dc3", "name": "dream-cycle-v3-live-nagatha", "script": "dc3.sh", "no_agent": True},
        "dc3", "dream-cycle-v3-live-nagatha", None)
    assert ok
    return None if scheduler._is_cron_silence_response(final) else final


def test_unchanged_context_on_a_later_date_is_silent_and_new_conversation_delivers(
        env, gateway, capsys, monkeypatch):
    answers, calls = gateway
    answers += [_row("applied", F1), _row("unchanged", F1), _row("applied", F2),
                _row("unchanged", F2), _row("unchanged", F2)]

    code, first = _run(env, 0, capsys)
    assert code == 0 and "🟠 P1-startups/expatriator\n▶️ Fix the onboarding gap" in first
    assert _delivered(first, monkeypatch) == first

    # The next night: a new date and a new cycle id, but no new conversation and no changed
    # registry, threads or trackers. The cron contract's own silence signal, not an empty guess.
    code, second = _run(env, 1, capsys)
    assert code == 0
    assert second.strip() == '{"wakeAgent": false}'
    assert _delivered(second, monkeypatch) is None

    code, third = _run(env, 2, capsys)
    assert code == 0 and "🟠 P1-startups/expatriator\n▶️ Fix the onboarding gap" in third
    assert _delivered(third, monkeypatch) == third
    assert len(calls) == 3  # the gateway was asked every night; IT decides whether to summarize

    # A status line is a different delivery: the brief going out does not stand in for it, and
    # once it went out it does not repeat either.
    code, status = _run(env, 3, capsys, output="status")
    assert code == 0 and status.startswith("dream-cycle-v3 cycle ok ")
    code, again = _run(env, 4, capsys, output="status")
    assert code == 0 and again.strip() == '{"wakeAgent": false}'


def test_a_failed_or_unconfirmed_run_stays_retryable_and_is_never_silenced(
        env, gateway, capsys, monkeypatch):
    answers, _calls = gateway
    answers += [_row("applied", F1), _row("already_applied", F1), _row("unchanged", F1), None,
                _row("unchanged", F1)]
    real_render = cli.render_daily_brief

    def broken_render(*_a, **_k):
        raise RuntimeError("renderer crashed before anything was delivered")

    monkeypatch.setattr(cli, "render_daily_brief", broken_render)
    with pytest.raises(RuntimeError):
        _run(env, 0, capsys)
    capsys.readouterr()
    monkeypatch.setattr(cli, "render_daily_brief", real_render)

    # The retry finds the gateway's compression already committed for this cycle; nothing was
    # delivered, so it delivers rather than treating the committed rewrite as done.
    code, retry = _run(env, 0, capsys)
    assert code == 0 and "P1-startups/expatriator" in retry

    code, quiet = _run(env, 1, capsys)
    assert code == 0 and quiet.strip() == '{"wakeAgent": false}'

    # No gateway answered: unknown is not "unchanged", so the failure is reported, not silenced...
    code, failed = _run(env, 2, capsys)
    assert code == 0 and "unavailable" in failed
    assert _delivered(failed, monkeypatch) == failed

    # ...and it set no checkpoint: the next unchanged night is judged against the last delivery.
    code, after = _run(env, 3, capsys)
    assert code == 0 and after.strip() == '{"wakeAgent": false}'
    json.loads(after)
