"""Phase 3 gateway wake seam: once-only binding, cache stability, reset
semantics, persistence round-trip, prompt-size boundaries vs hot memory."""
from datetime import datetime

import pytest

from dream_cycle_v3.store import ContinuityStore
from gateway.continuity_wake import (_active_profile_name,
                                     build_wake_packet_for_session,
                                     ensure_wake_packet)
from gateway.session import SessionEntry
from hermes_constants import get_hermes_home

NOW_ISO = "2026-07-11T08:00:00+00:00"

PROJECT = {
    "schema_version": 1,
    "project_id": "hermes-continuity",
    "canonical_name": "Hermes continuity architecture",
    "aliases": ["dream cycle"],
    "canonical_paths": [],
    "repositories": [],
    "status": "active",
    "owner": "default",
    "task_ssot": {"provider": "kanban", "locator": "sample-board",
                  "write_policy": "read_only"},
    "context_skill_id": None,
    "memory_policy": "warm_only",
    "sensitivity_policy": "normal",
    "retrieval_terms": ["continuity"],
    "registry_version": 1,
    "last_verified_at": "2026-07-10T00:00:00+00:00",
}


def make_thread(thread_id: str, title: str) -> dict:
    return {
        "schema_version": 1,
        "thread_id": thread_id,
        "project_id": "hermes-continuity",
        "external_task_ref": None,
        "link_disposition": "needs_link",
        "title": title,
        "normalized_next_action": "continue the work",
        "owner": "default",
        "state": "active",
        "opened_from": "carry_forward",
        "evidence_refs": [{
            "source_type": "file",
            "source_id": "profile:state/wake-up.md",
            "fingerprint": "fp-test-000000000001",
            "observed_at": "2026-07-10T21:00:00+00:00",
        }],
        "last_disposition_date": "2026-07-10",
        # Due in the past: without an activated project, only the
        # global due-thread lane can surface a thread.
        "follow_up_after": "2026-07-10T00:00:00+00:00",
        "idempotency_key": f"idem-{thread_id}",
    }


def seed_store(home_root, *, threads=()):
    store_path = home_root / "dream-cycle-v3" / "continuity.db"
    store_path.parent.mkdir(parents=True, exist_ok=True)
    with ContinuityStore(store_path) as store:
        store.migrate(NOW_ISO)
        store.upsert_project(PROJECT, NOW_ISO)
        for thread in threads:
            store.open_thread(thread, NOW_ISO)
    return store_path


def make_entry(session_id="sess-0001") -> SessionEntry:
    now = datetime(2026, 7, 11, 8, 0, 0)
    return SessionEntry(session_key="telegram_123", session_id=session_id,
                        created_at=now, updated_at=now)


# -- profile resolution -------------------------------------------------------

def test_profile_name_from_hermes_home(monkeypatch, tmp_path):
    profile = tmp_path / "profiles" / "alpha"
    profile.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(profile))
    assert _active_profile_name() == "alpha"

    plain = tmp_path / "plainhome"
    plain.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(plain))
    assert _active_profile_name() == "default"


def test_profile_name_does_not_filter_a_different_continuity_owner(tmp_path):
    home = tmp_path / "profiles" / "nagatha-test"
    project = dict(PROJECT, owner="nagatha")
    thread = dict(make_thread("wake-owner-mismatch-0001", "Owner survives"),
                  owner="nagatha")
    store_path = home / "dream-cycle-v3" / "continuity.db"
    store_path.parent.mkdir(parents=True)
    with ContinuityStore(store_path) as store:
        store.migrate(NOW_ISO)
        store.upsert_project(project, NOW_ISO)
        store.open_thread(thread, NOW_ISO)
        store.open_thread(
            dict(make_thread("wake-owner-foreign-0001", "Must not leak"),
                 owner="someone-else"),
            NOW_ISO)

    packet = build_wake_packet_for_session(
        "dream cycle status", profile_home=home)
    assert packet is not None
    assert packet.profile == "nagatha-test"
    assert packet.project_id == "hermes-continuity"
    assert "wake-owner-mismatch-0001" in packet.thread_ids
    assert "wake-owner-foreign-0001" not in packet.thread_ids


# -- new-session binding ------------------------------------------------------

def test_no_store_no_packet_no_binding():
    entry = make_entry()
    assert build_wake_packet_for_session("hello") is None
    assert ensure_wake_packet(entry, is_new_session=True,
                              first_message="hello") is False
    assert entry.wake_packet_id is None
    assert entry.wake_packet_text is None


def test_new_session_binds_packet_once():
    seed_store(get_hermes_home(),
               threads=[make_thread("wake-thread-0001-00000", "Fix the relay")])
    entry = make_entry()
    assert ensure_wake_packet(entry, is_new_session=True,
                              first_message="dream cycle status") is True
    assert entry.wake_packet_id
    assert entry.wake_packet_hash.startswith("sha256:")
    assert entry.wake_packet_project_id == "hermes-continuity"
    assert "Fix the relay" in entry.wake_packet_text
    assert len(entry.wake_packet_text) <= 1600


def test_existing_session_never_builds():
    seed_store(get_hermes_home())
    entry = make_entry()
    assert ensure_wake_packet(entry, is_new_session=False,
                              first_message="hello") is False
    assert entry.wake_packet_id is None


def test_bound_session_is_stable_across_store_mutation():
    """Prompt-cache invariant: an already-bound session never rebuilds, even
    when the continuity store changes underneath it."""
    home = get_hermes_home()
    seed_store(home, threads=[make_thread("wake-thread-0001-00000",
                                          "Fix the relay")])
    entry = make_entry()
    assert ensure_wake_packet(entry, is_new_session=True,
                              first_message="dream cycle status") is True
    first_id, first_text = entry.wake_packet_id, entry.wake_packet_text

    with ContinuityStore(home / "dream-cycle-v3" / "continuity.db") as store:
        store.open_thread(make_thread("wake-thread-0002-00000",
                                      "Brand new thread"), NOW_ISO)

    # Even a (buggy) repeated new-session signal must not rebuild.
    assert ensure_wake_packet(entry, is_new_session=True,
                              first_message="dream cycle status") is False
    assert entry.wake_packet_id == first_id
    assert entry.wake_packet_text == first_text
    assert "Brand new thread" not in entry.wake_packet_text


def test_manual_reset_gets_fresh_packet():
    """A /new-style reset mints a new SessionEntry; the fresh session sees
    the current store state."""
    home = get_hermes_home()
    seed_store(home, threads=[make_thread("wake-thread-0001-00000",
                                          "Fix the relay")])
    old = make_entry("sess-0001")
    ensure_wake_packet(old, is_new_session=True, first_message="hi")

    with ContinuityStore(home / "dream-cycle-v3" / "continuity.db") as store:
        store.open_thread(make_thread("wake-thread-0002-00000",
                                      "Post-reset thread"), NOW_ISO)

    fresh = make_entry("sess-0002")  # reset paths always mint a new entry
    assert ensure_wake_packet(fresh, is_new_session=True,
                              first_message="hi") is True
    assert fresh.wake_packet_id != old.wake_packet_id
    assert "Post-reset thread" in fresh.wake_packet_text
    # The old session keeps its original packet untouched.
    assert "Post-reset thread" not in old.wake_packet_text


# -- injection semantics (mirrors the gateway/run.py seam) --------------------

def turn_context(entry: SessionEntry, base_prompt: str) -> str:
    """The exact per-turn append gateway/run.py performs."""
    wake_text = getattr(entry, "wake_packet_text", None)
    if wake_text:
        return base_prompt + "\n\n" + wake_text
    return base_prompt


def test_packet_injected_once_per_prompt_and_stable_across_turns():
    seed_store(get_hermes_home(),
               threads=[make_thread("wake-thread-0001-00000", "Fix the relay")])
    entry = make_entry()
    ensure_wake_packet(entry, is_new_session=True, first_message="hello")

    turn1 = turn_context(entry, "## Current Session Context\nturn one")
    turn2 = turn_context(entry, "## Current Session Context\nturn one")
    assert turn1 == turn2  # byte-stable across turns
    assert turn1.count("[Continuity wake packet") == 1  # never duplicated


def test_binding_survives_persistence_round_trip():
    """Restart/compression persistence: the binding rides to_dict/from_dict."""
    seed_store(get_hermes_home(),
               threads=[make_thread("wake-thread-0001-00000", "Fix the relay")])
    entry = make_entry()
    ensure_wake_packet(entry, is_new_session=True, first_message="hello")

    revived = SessionEntry.from_dict(entry.to_dict())
    assert revived.wake_packet_id == entry.wake_packet_id
    assert revived.wake_packet_hash == entry.wake_packet_hash
    assert revived.wake_packet_project_id == entry.wake_packet_project_id
    assert revived.wake_packet_text == entry.wake_packet_text
    # And the revived entry still refuses to rebuild.
    assert ensure_wake_packet(revived, is_new_session=True,
                              first_message="hello") is False


def test_ambiguous_first_message_abstains():
    home = get_hermes_home()
    store_path = home / "dream-cycle-v3" / "continuity.db"
    store_path.parent.mkdir(parents=True, exist_ok=True)
    with ContinuityStore(store_path) as store:
        store.migrate(NOW_ISO)
        store.upsert_project(PROJECT, NOW_ISO)
        other = dict(PROJECT, project_id="klas-sample",
                     canonical_name="Marketplace sample", aliases=["klas"])
        store.upsert_project(other, NOW_ISO)
    entry = make_entry()
    ensure_wake_packet(entry, is_new_session=True,
                       first_message="compare hermes-continuity and klas-sample")
    assert entry.wake_packet_project_id is None
    assert "No project auto-activated" in entry.wake_packet_text


# -- prompt budget vs hot memory (requirement: wake never bypasses limits) ----

def test_combined_prompt_boundaries_with_hot_memory(monkeypatch, tmp_path):
    from tools.memory_tool import MemoryStore
    memory_dir = tmp_path / "memories"
    memory_dir.mkdir()
    (memory_dir / "MEMORY.md").write_text("M" * 10_000, encoding="utf-8")
    (memory_dir / "USER.md").write_text("U" * 10_000, encoding="utf-8")
    monkeypatch.setattr("tools.memory_tool.get_memory_dir", lambda: memory_dir)

    store = MemoryStore(memory_char_limit=2200, user_char_limit=1375,
                        warm_memory_enabled=False)
    store.load_from_disk()
    memory_block_before = store.format_for_system_prompt("memory")
    user_block_before = store.format_for_system_prompt("user")

    seed_store(get_hermes_home(),
               threads=[make_thread("wake-thread-0001-00000", "Fix the relay")])
    entry = make_entry()
    ensure_wake_packet(entry, is_new_session=True, first_message="hello")

    # Wake packet holds its own budget and never rides the memory store:
    assert len(entry.wake_packet_text) <= 1600
    assert store.format_for_system_prompt("memory") == memory_block_before
    assert store.format_for_system_prompt("user") == user_block_before
    assert "[Continuity wake packet" not in (memory_block_before or "")

    # Combined system-prompt volatile content grows by exactly the bounded
    # wake text plus separators — the wake path cannot smuggle extra bytes
    # into (or past) the hot-memory blocks; MemoryStore limit accounting
    # (the "N/2,200 chars" banner) is untouched.
    without_wake = "\n\n".join(filter(None, [memory_block_before,
                                             user_block_before]))
    combined = "\n\n".join(filter(None, [memory_block_before,
                                         user_block_before,
                                         entry.wake_packet_text]))
    assert len(combined) - len(without_wake) <= 1600 + len("\n\n")
    assert "100% — 10,000/2,200 chars" in memory_block_before  # unchanged accounting


@pytest.fixture(autouse=True)
def stable_wake_clock(monkeypatch):
    # Project freshness is meaningful; lifecycle fixtures must not age with wall time.
    from datetime import datetime as real_datetime
    import gateway.continuity_wake as wake

    class FixtureClock(real_datetime):
        @classmethod
        def now(cls, tz=None):
            fixed = real_datetime.fromisoformat(NOW_ISO)
            return fixed.astimezone(tz) if tz else fixed.replace(tzinfo=None)

    monkeypatch.setattr(wake, "datetime", FixtureClock)
