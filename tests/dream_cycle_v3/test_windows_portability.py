"""Phase 3 portability: read modules must import without POSIX-only fcntl.

Review finding 7 regression coverage: a simulated Windows interpreter (fcntl
import blocked, os.O_NOFOLLOW absent) must import wake/lookup and run the
confined project-doc reader; the guarded tool handler must degrade to a
typed error instead of a traceback when the package cannot import.
"""
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]

_BLOCK_FCNTL = """
import sys

class _BlockFcntl:
    def find_module(self, name, path=None):
        return self if name == "fcntl" else None
    def load_module(self, name):
        raise ImportError("No module named 'fcntl' (simulated Windows)")

sys.meta_path.insert(0, _BlockFcntl())
sys.modules.pop("fcntl", None)

import os
if hasattr(os, "O_NOFOLLOW"):
    del os.O_NOFOLLOW          # Windows has no O_NOFOLLOW either
"""


def run_sim_windows(body: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-c", _BLOCK_FCNTL + body],
        capture_output=True, text=True, cwd=REPO, timeout=120)


def test_wake_and_lookup_import_without_fcntl():
    proc = run_sim_windows("""
import dream_cycle_v3.wake
import dream_cycle_v3.lookup
import dream_cycle_v3.project_docs
import dream_cycle_v3.tracker_refresh
import dream_cycle_v3.sanitize
print("READ-MODULES-OK")
""")
    assert proc.returncode == 0, proc.stderr
    assert "READ-MODULES-OK" in proc.stdout


def test_wake_builds_packet_without_fcntl(tmp_path):
    """Full wake construction — including the confined project-doc read —
    on the simulated-Windows interpreter."""
    proc = run_sim_windows(f"""
from pathlib import Path
from dream_cycle_v3.store import ContinuityStore
from dream_cycle_v3.wake import WakeInputs, build_wake_packet

root = Path({str(tmp_path)!r})
store_path = root / "continuity.db"
with ContinuityStore(store_path) as store:
    store.migrate("2026-07-11T08:00:00+00:00")
    store.upsert_project({{
        "schema_version": 1, "project_id": "proj-win",
        "canonical_name": "Windows project", "aliases": ["winproj"],
        "canonical_paths": [], "repositories": [], "status": "active",
        "owner": "nagatha",
        "task_ssot": {{"provider": "none", "locator": None,
                       "write_policy": "read_only"}},
        "context_skill_id": None, "memory_policy": "warm_only",
        "sensitivity_policy": "normal", "retrieval_terms": [],
        "registry_version": 1,
        "last_verified_at": "2026-07-10T00:00:00+00:00",
    }}, "2026-07-11T08:00:00+00:00")

projects = root / "projects"
(projects / "proj-win").mkdir(parents=True)
(projects / "proj-win" / "map.md").write_text(
    "# map\\n## Purpose\\nWindows portability check.\\n", encoding="utf-8")

packet = build_wake_packet(
    store_path=store_path, projects_home=projects, kanban_root=None,
    inputs=WakeInputs(profile="nagatha", owner="nagatha",
                      now="2026-07-11T08:00:00+00:00",
                      first_message="winproj status"))
assert packet is not None and not packet.degraded, packet
assert "Purpose" in packet.text
print("WAKE-OK")
""")
    assert proc.returncode == 0, proc.stderr
    assert "WAKE-OK" in proc.stdout


def test_lookup_runs_without_fcntl(tmp_path):
    proc = run_sim_windows(f"""
from pathlib import Path
from dream_cycle_v3.store import ContinuityStore
from dream_cycle_v3.lookup import continuity_lookup

root = Path({str(tmp_path)!r})
store_path = root / "continuity.db"
with ContinuityStore(store_path) as store:
    store.migrate("2026-07-11T08:00:00+00:00")

payload = continuity_lookup(store_path=store_path, query="anything")
assert payload["kind"] == "query"
print("LOOKUP-OK")
""")
    assert proc.returncode == 0, proc.stderr
    assert "LOOKUP-OK" in proc.stdout


def test_tool_handler_degrades_typed_when_package_unimportable(monkeypatch):
    """check_fn may still advertise the tool in a race; the handler's guarded
    import must return a typed error, never a traceback."""
    import builtins
    from tools.continuity_tool import continuity_lookup_tool

    real_import = builtins.__import__

    def blocking_import(name, *args, **kwargs):
        if name.startswith("dream_cycle_v3"):
            raise ImportError("simulated: package unavailable on this platform")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", blocking_import)
    result = continuity_lookup_tool(query="anything")
    assert "unavailable" in result
    assert "Traceback" not in result
