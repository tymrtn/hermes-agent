"""continuity_lookup tool: registration, gating, typed JSON, profile isolation."""
import json

import pytest

from dream_cycle_v3.store import ContinuityStore
from hermes_constants import get_hermes_home
from tools.continuity_tool import (check_continuity_requirements,
                                   continuity_lookup_tool,
                                   continuity_projects_home,
                                   continuity_skills_home,
                                   continuity_store_path,
                                   hermes_root_for_home,
                                   kanban_root)
from tools.registry import invalidate_check_fn_cache, registry

NOW_ISO = "2026-07-11T08:00:00+00:00"


def seed_store(home_root):
    """Create an owned continuity store with one project under a HERMES_HOME."""
    store_path = home_root / "dream-cycle-v3" / "continuity.db"
    store_path.parent.mkdir(parents=True, exist_ok=True)
    with ContinuityStore(store_path) as store:
        store.migrate(NOW_ISO)
        store.upsert_project({
            "schema_version": 1,
            "project_id": "hermes-continuity",
            "canonical_name": "Hermes continuity architecture",
            "aliases": ["dream cycle"],
            "canonical_paths": [],
            "repositories": [],
            "status": "active",
            "owner": "nagatha",
            "task_ssot": {"provider": "kanban", "locator": "sample-board",
                          "write_policy": "read_only"},
            "context_skill_id": None,
            "memory_policy": "warm_only",
            "sensitivity_policy": "normal",
            "retrieval_terms": ["continuity"],
            "registry_version": 1,
            "last_verified_at": NOW_ISO,
        }, NOW_ISO)
    return store_path


@pytest.fixture(autouse=True)
def _fresh_check_cache():
    invalidate_check_fn_cache()
    yield
    invalidate_check_fn_cache()


def test_paths_follow_hermes_home(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "profile-a"))
    (tmp_path / "profile-a").mkdir()
    assert continuity_store_path() == (
        tmp_path / "profile-a" / "dream-cycle-v3" / "continuity.db")
    assert continuity_projects_home() == (
        tmp_path / "profile-a" / "dream-cycle-v3" / "projects")
    assert continuity_skills_home() == tmp_path / "profile-a" / "skills"
    # Non-profile home IS the shared Hermes root (custom/Docker layout).
    assert kanban_root() == tmp_path / "profile-a"
    assert "~/.hermes" not in str(continuity_store_path())


def test_kanban_root_derives_shared_root_from_profile_home(monkeypatch,
                                                           tmp_path):
    # Profile mode: <root>/profiles/<name> -> shared <root>. Kanban is
    # deliberately shared across profiles; deriving it from the profile
    # home would silently fork the board per profile.
    root = tmp_path / "hermes-root"
    home = root / "profiles" / "nagatha"
    home.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    assert hermes_root_for_home() == root
    assert kanban_root() == root
    assert kanban_root(home) == root
    # Explicit-home form ignores ambient state entirely.
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path / "elsewhere"))
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "pinned.db"))
    assert kanban_root(home) == root


def test_tool_errors_are_typed_and_path_free(monkeypatch, tmp_path):
    # No store: typed 'unavailable' whose message carries no filesystem path.
    home = tmp_path / "profile-err"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    payload = json.loads(continuity_lookup_tool(query="anything"))
    assert payload["error_type"] == "unavailable"
    assert str(home) not in payload["error"]
    assert str(tmp_path) not in payload["error"]
    # Bad request: typed, bounded, path-free.
    seed_store(home)
    payload = json.loads(continuity_lookup_tool())
    assert payload["error_type"] == "bad_request"
    assert str(home) not in payload["error"]
    assert len(payload["error"]) <= 300


def test_registered_with_registry():
    entry = registry.get_entry("continuity_lookup")
    assert entry is not None
    assert entry.toolset == "continuity"
    assert entry.schema["name"] == "continuity_lookup"
    params = entry.schema["parameters"]["properties"]
    assert set(params) == {"project", "thread_id", "query"}


def test_in_core_toolset():
    from toolsets import _HERMES_CORE_TOOLS
    assert "continuity_lookup" in _HERMES_CORE_TOOLS


def test_check_fn_hidden_without_store():
    # conftest points HERMES_HOME at an empty tempdir: no store, no tool.
    assert check_continuity_requirements() is False
    assert registry.get_definitions(["continuity_lookup"], quiet=True) == []


def test_check_fn_visible_with_owned_store():
    seed_store(get_hermes_home())
    assert check_continuity_requirements() is True
    defs = registry.get_definitions(["continuity_lookup"], quiet=True)
    assert len(defs) == 1
    assert defs[0]["function"]["name"] == "continuity_lookup"


def test_check_fn_hidden_for_foreign_db():
    import sqlite3
    path = continuity_store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE tasks(id TEXT)")
    conn.commit()
    conn.close()
    assert check_continuity_requirements() is False


def test_handler_unavailable_is_typed_error():
    result = json.loads(continuity_lookup_tool(query="anything"))
    assert result["error_type"] == "unavailable"
    assert "error" in result


def test_handler_bad_request_is_typed_error():
    seed_store(get_hermes_home())
    result = json.loads(continuity_lookup_tool())
    assert result["error_type"] == "bad_request"
    result = json.loads(continuity_lookup_tool(project="a", query="b"))
    assert result["error_type"] == "bad_request"


def test_handler_project_lookup_round_trip():
    seed_store(get_hermes_home())
    result = json.loads(continuity_lookup_tool(project="hermes-continuity"))
    assert result["kind"] == "project" and result["found"]
    assert result["canonical_name"] == "Hermes continuity architecture"


def test_dispatch_through_registry():
    seed_store(get_hermes_home())
    raw = registry.dispatch("continuity_lookup", {"query": "dream cycle"})
    result = json.loads(raw)
    assert result["kind"] == "query"
    assert result["results"][0]["id"] == "hermes-continuity"


def test_profile_isolation(monkeypatch, tmp_path):
    root = tmp_path / "root"
    profile_a = root / "profiles" / "alpha"
    profile_b = root / "profiles" / "beta"
    profile_a.mkdir(parents=True)
    profile_b.mkdir(parents=True)
    seed_store(profile_a)

    monkeypatch.setenv("HERMES_HOME", str(profile_a))
    assert check_continuity_requirements() is True
    assert json.loads(continuity_lookup_tool(project="hermes-continuity"))["found"]

    monkeypatch.setenv("HERMES_HOME", str(profile_b))
    assert check_continuity_requirements() is False
    result = json.loads(continuity_lookup_tool(project="hermes-continuity"))
    assert result["error_type"] == "unavailable"
