"""Phase 3 cross-profile isolation: multiplexed wake resolution, symlink
refusal, and profile-aware (but conversation-frozen) tool availability.

Review finding 1 regression coverage:
- parallel two-profile wake where one profile has no store
- both profiles with distinct stores resolve their OWN content
- a cross-profile symlink is refused (neutral degraded packet, never the
  other profile's data)
- registry check_fn cache and model_tools definition cache are keyed by the
  active profile home
"""
import concurrent.futures as cf
import os

import pytest

from dream_cycle_v3.store import ContinuityStore, assert_store_confined
from dream_cycle_v3.errors import StoreOwnershipError
from gateway.continuity_wake import build_wake_packet_for_session

NOW_ISO = "2026-07-11T08:00:00+00:00"


def make_profile(root, name, *, thread_title=None):
    home = root / "profiles" / name
    (home / "dream-cycle-v3").mkdir(parents=True)
    if thread_title is not None:
        store_path = home / "dream-cycle-v3" / "continuity.db"
        with ContinuityStore(store_path) as store:
            store.migrate(NOW_ISO)
            store.upsert_project({
                "schema_version": 1,
                "project_id": f"{name}-project",
                "canonical_name": f"{name} project",
                "aliases": [], "canonical_paths": [], "repositories": [],
                "status": "active", "owner": name,
                "task_ssot": {"provider": "none", "locator": None,
                              "write_policy": "read_only"},
                "context_skill_id": None, "memory_policy": "warm_only",
                "sensitivity_policy": "normal", "retrieval_terms": [],
                "registry_version": 1,
                "last_verified_at": "2026-07-10T00:00:00+00:00",
            }, NOW_ISO)
            store.open_thread({
                "schema_version": 1,
                "thread_id": f"{name}-thread-0001-000000",
                "project_id": f"{name}-project",
                "external_task_ref": None,
                "link_disposition": "needs_link",
                "title": thread_title,
                "normalized_next_action": "continue",
                "owner": name, "state": "active",
                "opened_from": "carry_forward",
                "evidence_refs": [{"source_type": "file",
                                   "source_id": "profile:state/x.md",
                                   "fingerprint": "fp-0000000000001",
                                   "observed_at": NOW_ISO}],
                "last_disposition_date": "2026-07-10",
                # Due in the past: without an activated project, only the
                # global due-thread lane can surface a thread.
                "follow_up_after": "2026-07-10T00:00:00+00:00",
                "idempotency_key": f"idem-{name}-000000000000001",
            }, NOW_ISO)
    return home


def test_two_profiles_resolve_their_own_content_in_parallel(tmp_path):
    alpha = make_profile(tmp_path, "alpha", thread_title="Alpha secret plan")
    beta = make_profile(tmp_path, "beta", thread_title="Beta rollout step")
    nostore = make_profile(tmp_path, "gamma")   # no store at all

    def build(home):
        return build_wake_packet_for_session("hello", profile_home=home)

    results = {}
    with cf.ThreadPoolExecutor(max_workers=6) as pool:
        futs = []
        for _ in range(5):
            for name, home in (("alpha", alpha), ("beta", beta),
                               ("gamma", nostore)):
                futs.append((name, pool.submit(build, home)))
        for name, fut in futs:
            results.setdefault(name, []).append(fut.result())

    for packet in results["alpha"]:
        assert packet is not None
        assert packet.profile == "alpha"
        assert "Alpha secret plan" in packet.text
        assert "Beta rollout step" not in packet.text
    for packet in results["beta"]:
        assert packet.profile == "beta"
        assert "Beta rollout step" in packet.text
        assert "Alpha secret plan" not in packet.text
    for packet in results["gamma"]:
        assert packet is None      # no store => session starts unchanged


def test_cross_profile_symlink_is_refused(tmp_path):
    alpha = make_profile(tmp_path, "alpha")
    beta = make_profile(tmp_path, "beta", thread_title="Beta private thread")
    # Alpha's continuity.db is a symlink to Beta's owned store.
    beta_db = beta / "dream-cycle-v3" / "continuity.db"
    alpha_db = alpha / "dream-cycle-v3" / "continuity.db"
    os.symlink(beta_db, alpha_db)

    with pytest.raises(StoreOwnershipError):
        assert_store_confined(alpha_db, alpha / "dream-cycle-v3")

    packet = build_wake_packet_for_session("hello", profile_home=alpha)
    assert packet is not None and packet.degraded
    assert "Beta private thread" not in packet.text
    assert packet.project_id is None


def test_symlinked_continuity_dir_is_refused(tmp_path):
    alpha = make_profile(tmp_path, "alpha")
    beta = make_profile(tmp_path, "beta", thread_title="Beta private thread")
    # The entire dream-cycle-v3 dir is a symlink into Beta.
    linked = tmp_path / "profiles" / "delta"
    linked.mkdir(parents=True)
    os.symlink(beta / "dream-cycle-v3", linked / "dream-cycle-v3")

    packet = build_wake_packet_for_session("hello", profile_home=linked)
    assert packet is not None and packet.degraded
    assert "Beta private thread" not in packet.text


def test_check_fn_gate_is_profile_aware(tmp_path, monkeypatch):
    """One multiplexed profile's verdict must not advertise or suppress the
    continuity tool for another, even within the cache TTL."""
    from tools.continuity_tool import check_continuity_requirements
    from tools.registry import _check_fn_cached, invalidate_check_fn_cache

    withstore = make_profile(tmp_path, "withstore", thread_title="t")
    without = make_profile(tmp_path, "without")

    invalidate_check_fn_cache()
    monkeypatch.setenv("HERMES_HOME", str(withstore))
    assert _check_fn_cached(check_continuity_requirements) is True
    monkeypatch.setenv("HERMES_HOME", str(without))
    assert _check_fn_cached(check_continuity_requirements) is False
    # And back — the first profile's cached verdict is still its own.
    monkeypatch.setenv("HERMES_HOME", str(withstore))
    assert _check_fn_cached(check_continuity_requirements) is True
    invalidate_check_fn_cache()


def test_check_fn_hides_tool_for_cross_profile_symlink(tmp_path, monkeypatch):
    from tools.continuity_tool import check_continuity_requirements

    beta = make_profile(tmp_path, "beta", thread_title="t")
    alpha = make_profile(tmp_path, "alpha")
    os.symlink(beta / "dream-cycle-v3" / "continuity.db",
               alpha / "dream-cycle-v3" / "continuity.db")

    monkeypatch.setenv("HERMES_HOME", str(alpha))
    assert check_continuity_requirements() is False
    monkeypatch.setenv("HERMES_HOME", str(beta))
    assert check_continuity_requirements() is True


def test_tool_definitions_cache_keyed_by_profile_home(tmp_path, monkeypatch):
    """model_tools memoization must not serve one profile's definition set
    to another (continuity_lookup present only where an owned store is)."""
    import model_tools
    from tools.registry import invalidate_check_fn_cache

    withstore = make_profile(tmp_path, "withstore", thread_title="t")
    without = make_profile(tmp_path, "without")

    invalidate_check_fn_cache()
    model_tools._clear_tool_defs_cache()

    def names(home):
        monkeypatch.setenv("HERMES_HOME", str(home))
        defs = model_tools.get_tool_definitions(
            enabled_toolsets=["continuity"], quiet_mode=True)
        return {d["function"]["name"] for d in defs}

    with_names = names(withstore)
    without_names = names(without)
    assert "continuity_lookup" in with_names
    assert "continuity_lookup" not in without_names
    # Cache hit for the first profile still serves its own set.
    assert "continuity_lookup" in names(withstore)
    model_tools._clear_tool_defs_cache()
    invalidate_check_fn_cache()


def test_same_profile_store_lifecycle_reevaluates_tool_defs(tmp_path,
                                                            monkeypatch):
    """Same-profile availability is not pinned by the outer definitions
    memo: creating (or removing) a continuity store changes the cache key's
    store fingerprint AND drops the continuity check_fn's TTL entry, so the
    next NEW agent's definition set reflects reality without waiting on
    unrelated invalidation."""
    import model_tools
    from tools.registry import invalidate_check_fn_cache

    home = make_profile(tmp_path, "lifecycle")   # no store yet
    monkeypatch.setenv("HERMES_HOME", str(home))
    invalidate_check_fn_cache()
    model_tools._clear_tool_defs_cache()

    def names():
        defs = model_tools.get_tool_definitions(
            enabled_toolsets=["continuity"], quiet_mode=True)
        return {d["function"]["name"] for d in defs}

    try:
        assert "continuity_lookup" not in names()
        # Store appears (dream cycle provisions it) — no manual cache pokes.
        store_path = home / "dream-cycle-v3" / "continuity.db"
        with ContinuityStore(store_path) as store:
            store.migrate(NOW_ISO)
        assert "continuity_lookup" in names()
        # Store removed (rollback path) — the tool disappears again.
        (home / "dream-cycle-v3" / "continuity.db").unlink()
        assert "continuity_lookup" not in names()
    finally:
        model_tools._clear_tool_defs_cache()
        invalidate_check_fn_cache()


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
