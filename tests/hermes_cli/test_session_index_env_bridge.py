"""Standalone (hermes_cli.main) session-index config → env bridge.

``sessions.cjk_fts`` and ``sessions.search_slow_ms`` are config.yaml knobs
carried to ``hermes_state`` (SessionDB) through the internal env vars
``HERMES_CJK_FTS`` / ``HERMES_SEARCH_SLOW_MS``. The gateway and the legacy
``cli.py`` chat surface bridge them, but the primary ``hermes`` CLI entrypoint
(``hermes_cli/main.py``) drives standalone ``hermes sessions ...`` commands
that construct a SessionDB directly — without the bridge those commands
ignored the config knobs entirely.

The bridge is a shared helper applied in main.py's early config
initialization (before any SessionDB is built). It is config-authoritative
when the knob is present and leaves an explicit env carrier untouched when the
config omits it — matching the gateway/cli.py semantics.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import yaml

from hermes_cli.config import apply_session_index_env_bridge

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_cjk_fts_config_authoritative_over_env(monkeypatch):
    monkeypatch.setenv("HERMES_CJK_FTS", "1")
    apply_session_index_env_bridge({"sessions": {"cjk_fts": False}})
    assert os.environ["HERMES_CJK_FTS"] == "False"


def test_search_slow_ms_bridged_from_config(monkeypatch):
    monkeypatch.delenv("HERMES_SEARCH_SLOW_MS", raising=False)
    apply_session_index_env_bridge({"sessions": {"search_slow_ms": 250}})
    assert os.environ["HERMES_SEARCH_SLOW_MS"] == "250"


def test_env_survives_when_config_omits_knobs(monkeypatch):
    monkeypatch.setenv("HERMES_CJK_FTS", "0")
    monkeypatch.setenv("HERMES_SEARCH_SLOW_MS", "700")
    apply_session_index_env_bridge({"sessions": {"auto_prune": False}})
    assert os.environ["HERMES_CJK_FTS"] == "0"
    assert os.environ["HERMES_SEARCH_SLOW_MS"] == "700"


def test_non_dict_sessions_section_is_noop(monkeypatch):
    monkeypatch.setenv("HERMES_CJK_FTS", "1")
    apply_session_index_env_bridge({"sessions": "broken"})
    apply_session_index_env_bridge({})
    apply_session_index_env_bridge(None)
    assert os.environ["HERMES_CJK_FTS"] == "1"


def test_bridged_value_drives_sessiondb_cjk_semantics(monkeypatch):
    """The bridged 'False' carrier must disable the cjk index in hermes_state,
    proving the standalone SessionDB-init path honours the config knob."""
    from hermes_state import _cjk_fts_config_enabled

    monkeypatch.delenv("HERMES_CJK_FTS", raising=False)
    apply_session_index_env_bridge({"sessions": {"cjk_fts": False}})
    assert _cjk_fts_config_enabled() is False


def test_standalone_entrypoint_applies_bridge_at_import(tmp_path):
    """Importing hermes_cli.main with a config.yaml knob must set the carrier.

    Runs in a fresh interpreter so the module-level early bridge actually
    executes against the temp HERMES_HOME (config-authoritative import path).
    """
    home = tmp_path / ".hermes"
    home.mkdir()
    (home / "config.yaml").write_text(
        yaml.safe_dump({"sessions": {"cjk_fts": False, "search_slow_ms": 250}}),
        encoding="utf-8",
    )
    env = {
        "HERMES_HOME": str(home),
        "PATH": os.environ.get("PATH", ""),
        "PYTHONPATH": str(REPO_ROOT),
        "HOME": str(tmp_path),
    }
    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            "import os, hermes_cli.main as m; "
            "print(os.environ.get('HERMES_CJK_FTS'));"
            "print(os.environ.get('HERMES_SEARCH_SLOW_MS'))",
        ],
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    lines = [ln for ln in proc.stdout.splitlines() if ln.strip()]
    assert lines[-2:] == ["False", "250"], proc.stdout
