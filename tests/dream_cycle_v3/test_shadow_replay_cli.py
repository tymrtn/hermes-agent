"""Phase 4 `shadow-replay` CLI seam.

Accelerated historical evidence: seven contiguous one-day windows replayed
against real sources in one sitting, sharing the `historical-replay`
implementation. Help and output must say so explicitly — this is NOT seven
operational days — and the `historical-replay` seam stays available.
"""
import json
import os
from datetime import datetime
from pathlib import Path

import pytest

from dream_cycle_v3.cli import main
from dream_cycle_v3.dry_run import SAMPLE_DATA

START = "2026-07-05"
END = "2026-07-12"          # exclusive: seven one-day windows 07-05 .. 07-11
DAY_DATES = [f"2026-07-{d:02d}" for d in range(5, 12)]


def _mtime(day: str) -> int:
    dt = datetime.fromisoformat(day + "T12:00:00+00:00")
    return int(dt.timestamp())


@pytest.fixture
def env(tmp_path):
    sources = tmp_path / "sources" / "profile"
    sources.mkdir(parents=True)
    for day in DAY_DATES:
        path = sources / "notes" / f"{day}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"- observation recorded on {day}\n", encoding="utf-8")
        os.utime(path, (_mtime(day), _mtime(day)))
    registry = tmp_path / "registry.json"
    registry.write_text(
        (SAMPLE_DATA / "projects.json").read_text(encoding="utf-8"),
        encoding="utf-8")
    threads = tmp_path / "threads.json"
    threads.write_text(
        (SAMPLE_DATA / "threads.json").read_text(encoding="utf-8"),
        encoding="utf-8")
    return {"sources": sources, "v3_root": tmp_path / "v3-replay",
            "registry": registry, "threads": threads}


def replay_argv(env, command="shadow-replay", expect_project="hermes-continuity"):
    return [
        command,
        "--profile", "nagatha-test",
        "--owner", "nagatha",
        "--root", f"profile={env['sources']}",
        "--v3-root", str(env["v3_root"]),
        "--registry", str(env["registry"]),
        "--threads", str(env["threads"]),
        "--todoist-export", str(SAMPLE_DATA / "todoist_export.json"),
        "--migrate-v2-root", "profile",
        "--smoke-message", "status update on the dream cycle work",
        "--smoke-expect-project", expect_project,
        "--smoke-require-thread",
        "--start-date", START,
        "--end-date", END,
    ]


def test_shadow_replay_help_identifies_accelerated_historical_evidence(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["shadow-replay", "--help"])
    assert exc.value.code == 0
    out = " ".join(capsys.readouterr().out.split())
    assert "accelerated historical evidence" in out
    assert "NOT seven operational days" in out


def test_shadow_replay_emits_labeled_bounded_json(env, capsys):
    rc = main(replay_argv(env))
    captured = capsys.readouterr()
    assert rc == 0, captured.err

    lines = captured.out.splitlines()
    assert len(lines) == 1, "exactly one bounded status line"
    result = json.loads(lines[0])

    assert result["command"] == "shadow-replay"
    assert result["accelerated_historical_evidence"] is True
    assert result["historical_replay"] is True
    assert result["is_operational_evidence"] is False
    assert "NOT seven elapsed daily operational cycles" in result["label"]
    assert result["windows"] == 7
    assert result["ok"] is True
    assert result["invariants"]["all_reruns_zero_delta"] is True
    assert result["invariants"]["zero_live_destination_writes"] is True

    summary_path = Path(result["summary_path"])
    assert summary_path.is_file()
    on_disk = json.loads(summary_path.read_text(encoding="utf-8"))
    assert on_disk["historical_replay"] is True
    assert on_disk["is_operational_evidence"] is False


def test_shadow_replay_failure_exits_nonzero(env, capsys):
    rc = main(replay_argv(env, expect_project="klas-sample"))
    captured = capsys.readouterr()
    assert rc == 1
    result = json.loads(captured.out)
    assert result["ok"] is False
    assert result["is_operational_evidence"] is False


def test_shadow_replay_output_is_identical_on_exact_rerun(env, capsys):
    """codex phase-4 final review Low caveat: a full exact rerun's public
    shadow-replay CLI JSON must be identical to the first run's, including
    any receipts figure — not diverge because of a retry-local
    recomputation that bypasses the canonical stored summary."""
    rc1 = main(replay_argv(env))
    first = json.loads(capsys.readouterr().out.splitlines()[0])
    assert rc1 == 0

    rc2 = main(replay_argv(env))
    second = json.loads(capsys.readouterr().out.splitlines()[0])
    assert rc2 == 0

    assert second == first, \
        "an exact rerun must produce byte-for-byte identical CLI output"


def test_historical_replay_seam_is_preserved(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["historical-replay", "--help"])
    assert exc.value.code == 0
    out = " ".join(capsys.readouterr().out.split())
    assert "NOT seven operational days" in out
