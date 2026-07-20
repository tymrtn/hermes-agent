from __future__ import annotations

import json
from pathlib import Path

import pytest

from dream_cycle_v3.context_health import (
    active_project_context_files,
    audit_context_health,
    write_context_health,
)
from dream_cycle_v3.errors import DreamCycleError


@pytest.fixture(autouse=True)
def _profile_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "profile-home"
    home.mkdir()
    monkeypatch.setattr(
        "hermes_cli.profiles.resolve_profile_env", lambda _profile: str(home)
    )


def test_active_sources_follow_prompt_builder_priority(tmp_path: Path) -> None:
    (tmp_path / "AGENTS.md").write_text("agent rules\n", encoding="utf-8")
    (tmp_path / "CLAUDE.md").write_text("claude rules\n", encoding="utf-8")
    (tmp_path / ".cursorrules").write_text("cursor rules\n", encoding="utf-8")

    assert active_project_context_files(tmp_path) == [
        (tmp_path / "AGENTS.md").resolve()
    ]


def test_context_health_uses_real_cap_and_detects_truncation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "AGENTS.md"
    source.write_text("x" * 200, encoding="utf-8")
    monkeypatch.setattr(
        "agent.prompt_builder._get_context_file_max_chars", lambda _length: 100
    )

    report = audit_context_health(
        tmp_path, profile="test-profile", context_length=1_000_000)

    assert report["pass"] is False
    assert report["remediation_required"] is True
    assert report["effective_cap_chars"] == 100
    assert report["active_source_count"] == 1
    assert report["active_sources"][0]["path"] == str(source.resolve())
    assert report["active_sources"][0]["chars"] == 200
    assert len(report["active_sources"][0]["sha256"]) == 64
    assert report["truncation_warnings"]


def test_context_health_passes_and_writes_fresh_canonical_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "AGENTS.md"
    source.write_text("complete instructions\n", encoding="utf-8")
    monkeypatch.setattr(
        "agent.prompt_builder._get_context_file_max_chars", lambda _length: 1_000
    )
    report = audit_context_health(
        tmp_path, profile="test-profile", context_length=300_000)
    out = tmp_path / "evidence" / "context-health.json"

    assert report["pass"] is True
    assert report["truncation_warnings"] == []
    assert write_context_health(report, out) == out
    assert json.loads(out.read_text(encoding="utf-8"))["pass"] is True
    with pytest.raises(DreamCycleError, match="refusing to overwrite"):
        write_context_health(report, out)
