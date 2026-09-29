"""`write_report` byte-exactness (codex phase-4 final review, Windows caveat).

Text-mode `Path.write_text()` translates a bare `\\n` to `\\r\\n` under
default newline handling on native Windows. If the persisted report is
written that way but hashed from an in-memory string, the returned digest
silently diverges from `sha256(path.read_bytes())` on that platform. The fix
must construct one UTF-8 byte buffer and write exactly those bytes in binary
mode so no newline translation is possible, and must reject writing through
a file/symlink planted at the target path ahead of time.
"""
import hashlib
from pathlib import Path

import pytest

from dream_cycle_v3.canonical import canonical_json
from dream_cycle_v3.errors import DreamCycleError
from dream_cycle_v3.report import write_report

REPORT = {"run_id": "run-report-bytes-0001", "kind": "dream-cycle-v3-phase4-cycle-report"}


def test_write_report_never_calls_text_mode_write(tmp_path, monkeypatch):
    """The persisted-bytes path must not go through `Path.write_text()`,
    which is what lets Windows newline translation corrupt the hashed
    buffer in the first place."""
    def _boom(self, *a, **k):
        raise AssertionError("write_report must not use text-mode write_text()")

    monkeypatch.setattr(Path, "write_text", _boom)
    path = write_report(REPORT, tmp_path / "reports")
    assert path.is_file()


def test_write_report_persists_exact_utf8_bytes_with_lf_only(tmp_path):
    path = write_report(REPORT, tmp_path / "reports")
    expected = (canonical_json(REPORT) + "\n").encode("utf-8")
    raw = path.read_bytes()
    assert raw == expected
    assert b"\r\n" not in raw


def test_write_report_digest_of_persisted_bytes_is_reproducible(tmp_path):
    path = write_report(REPORT, tmp_path / "reports")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    expected_digest = hashlib.sha256(
        (canonical_json(REPORT) + "\n").encode("utf-8")).hexdigest()
    assert digest == expected_digest


def test_write_report_refuses_to_follow_a_preexisting_symlink(tmp_path):
    """A symlink planted at the report path ahead of the write must never
    be followed: the write must fail closed with a typed error, and the
    symlink's target must never receive attacker-influenced bytes."""
    reports_dir = tmp_path / "reports"
    reports_dir.mkdir()
    evil_target = tmp_path / "outside-reports-dir.json"
    (reports_dir / f"{REPORT['run_id']}.json").symlink_to(evil_target)

    with pytest.raises(DreamCycleError):
        write_report(REPORT, reports_dir)

    assert not evil_target.exists(), \
        "the exclusive-create write must never follow the planted symlink"
