"""Per-job ``script_timeout_seconds`` override for cron scripts.

Added 2026-06-12 (Dream Cycle): the nightly fable5 productivity audit runs
a ~20-minute Claude Code session from a no-agent cron script, but the
global script timeout default (120s) killed the launcher while the claude
child survived as an orphan — cron delivered FAIL, the completed report
was never delivered.  Long-running jobs can now declare their own budget
without raising the global default for every job.
"""

import sys
import textwrap
from pathlib import Path

import pytest

# Ensure project root is importable
sys.path.insert(0, str(Path(__file__).parent.parent.parent))


@pytest.fixture
def cron_env(tmp_path, monkeypatch):
    """Isolated cron environment with temp HERMES_HOME."""
    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    (hermes_home / "scripts").mkdir()
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    return hermes_home


class TestGetScriptTimeoutOverride:
    """Precedence: per-job override > module patch > env > config > default."""

    def test_valid_override_wins(self, monkeypatch):
        from cron import scheduler

        monkeypatch.setenv("HERMES_CRON_SCRIPT_TIMEOUT", "300")
        assert scheduler._get_script_timeout(2400) == 2400

    def test_string_override_accepted(self):
        from cron import scheduler

        assert scheduler._get_script_timeout("1800") == 1800

    def test_invalid_override_falls_through(self, monkeypatch):
        from cron import scheduler

        monkeypatch.delenv("HERMES_CRON_SCRIPT_TIMEOUT", raising=False)
        default = scheduler._get_script_timeout()
        assert scheduler._get_script_timeout("not-a-number") == default

    def test_nonpositive_override_falls_through(self, monkeypatch):
        from cron import scheduler

        monkeypatch.delenv("HERMES_CRON_SCRIPT_TIMEOUT", raising=False)
        default = scheduler._get_script_timeout()
        assert scheduler._get_script_timeout(0) == default
        assert scheduler._get_script_timeout(-5) == default

    def test_none_override_preserves_existing_chain(self, monkeypatch):
        from cron import scheduler

        monkeypatch.setenv("HERMES_CRON_SCRIPT_TIMEOUT", "777")
        assert scheduler._get_script_timeout(None) == 777


class TestRunJobScriptTimeoutOverride:
    """The override must reach the actual subprocess timeout."""

    @pytest.mark.live_system_guard_bypass
    def test_short_override_times_out_long_script(self, cron_env):
        """Real signal delivery required: subprocess.run kills the child on
        timeout, and the conftest live-system guard would otherwise block
        that kill and mask the TimeoutExpired path under test."""
        from cron.scheduler import _run_job_script

        script = cron_env / "scripts" / "sleepy.py"
        script.write_text(
            textwrap.dedent(
                """
                import time
                time.sleep(10)
                print("done")
                """
            )
        )

        success, output = _run_job_script(str(script), timeout_seconds=1)
        assert success is False
        assert "timed out after 1s" in output

    def test_override_allows_completion_within_budget(self, cron_env):
        from cron.scheduler import _run_job_script

        script = cron_env / "scripts" / "quick.py"
        script.write_text('print("quick ok")\n')

        success, output = _run_job_script(str(script), timeout_seconds=30)
        assert success is True
        assert output == "quick ok"
