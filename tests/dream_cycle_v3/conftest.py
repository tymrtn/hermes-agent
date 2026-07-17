import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from dream_cycle_v3.dry_run import SAMPLE_DATA  # noqa: E402

WINDOW_START = datetime(2026, 7, 9, 0, 0, 0, tzinfo=timezone.utc)
WINDOW_END = datetime(2026, 7, 12, 0, 0, 0, tzinfo=timezone.utc)
AS_OF = datetime(2026, 7, 11, 8, 0, 0, tzinfo=timezone.utc)
NOW_ISO = "2026-07-11T08:00:00+00:00"
TODAY = "2026-07-11"
SOURCE_MTIME = 1783684800  # 2026-07-10T12:00:00Z


def write_tree(root: Path, files: dict[str, str], mtime: int = SOURCE_MTIME) -> Path:
    """Materialize a source tree with pinned mtimes (deepest paths first)."""
    for rel, content in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    for path in sorted(root.rglob("*"), reverse=True):
        os.utime(path, (mtime, mtime))
    os.utime(root, (mtime, mtime))
    return root


@pytest.fixture
def sample_projects() -> list[dict]:
    return json.loads((SAMPLE_DATA / "projects.json").read_text(encoding="utf-8"))


@pytest.fixture
def sample_threads() -> list[dict]:
    return json.loads((SAMPLE_DATA / "threads.json").read_text(encoding="utf-8"))


@pytest.fixture
def store(tmp_path):
    from dream_cycle_v3.store import ContinuityStore

    s = ContinuityStore(tmp_path / "continuity.db")
    s.migrate(NOW_ISO)
    yield s
    s.close()


def make_manifest_for_run(profile: str = "test-profile", max_depth: int = 8) -> dict:
    """Minimal valid manifest for store tests that need a recorded run."""
    from dream_cycle_v3 import COLLECTOR_VERSION
    from dream_cycle_v3.manifest import assemble_manifest

    return assemble_manifest(
        profile=profile,
        window_start="2026-07-09T00:00:00+00:00",
        window_end="2026-07-12T00:00:00+00:00",
        collector_version=COLLECTOR_VERSION,
        bounds={"max_files_per_root": 64, "max_bytes_per_file": 65536,
                "max_total_bytes": 4194304, "max_depth": max_depth,
                "excerpt_chars": 700, "allowed_suffixes": [".md"]},
        sources=[],
        excluded=[],
        roots={"profile": "/tmp/example"},
        generated_at=NOW_ISO,
    )
