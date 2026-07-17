"""Phase 3 durable wake-packet lifetime over a REAL SessionStore + state.db.

Covers every row of the review's session-transition matrix:
- direct first prompt binds once (explicit first_model_call_pending state)
- pre-model slash traffic (updated_at bump) does not misclassify the first
  model call as continuing
- /new when no entry exists (force_new) still binds
- /branch inherits the parent's binding via the durable parent chain
- A -> /new B -> /resume A restores A's original packet
- in-handler switch to an old transcript never binds current continuity
- crash recovery (routing entry lost) restores from state.db
- restart inside the compression parent->child publication window recovers
  the binding through the parent chain
- corrupted persisted metadata (types/size/hash) fails closed everywhere
- entries persisted by older builds never rebuild
"""
import hashlib
import json
from datetime import datetime

import pytest

import hermes_state
from gateway.continuity_wake import (ensure_wake_text_for_session_id,
                                     load_wake_binding_for_session,
                                     validate_wake_binding,
                                     validated_wake_text,
                                     wake_binding_to_json)
from gateway.session import GatewayConfig, SessionEntry, SessionSource, SessionStore
from gateway.session import Platform


def make_store(tmp_path, monkeypatch) -> SessionStore:
    monkeypatch.setattr(hermes_state, "DEFAULT_DB_PATH", tmp_path / "state.db")
    return SessionStore(sessions_dir=tmp_path / "sessions",
                        config=GatewayConfig())


def source(chat_id="chat-1") -> SessionSource:
    return SessionSource(platform=Platform.TELEGRAM, user_id="u1",
                         chat_id=chat_id, chat_type="dm")


def binding_for(text: str) -> dict:
    return {
        "packet_id": "pkt-" + hashlib.sha256(text.encode()).hexdigest()[:12],
        "content_hash": "sha256:" + hashlib.sha256(text.encode()).hexdigest(),
        "project_id": "proj-1",
        "text": text,
    }


def bind(entry: SessionEntry, text: str) -> None:
    b = binding_for(text)
    entry.wake_packet_id = b["packet_id"]
    entry.wake_packet_hash = b["content_hash"]
    entry.wake_packet_project_id = b["project_id"]
    entry.wake_packet_text = b["text"]


# -- explicit first-model-call state ------------------------------------------

def test_direct_first_prompt_is_pending_then_consumed(tmp_path, monkeypatch):
    store = make_store(tmp_path, monkeypatch)
    entry = store.get_or_create_session(source())
    assert entry.first_model_call_pending is True
    # The gateway consumes the flag exactly when it dispatches the model call.
    entry.first_model_call_pending = False
    store._save()
    again = store.get_or_create_session(source())
    assert again.first_model_call_pending is False


def test_pre_model_slash_traffic_keeps_first_call_pending(tmp_path, monkeypatch):
    """Matrix row: pre-model slash/session lookup, then first prompt.

    Repeated get_or_create_session calls bump updated_at (the old, broken
    timestamp heuristic) but MUST NOT consume first-model-call eligibility.
    """
    store = make_store(tmp_path, monkeypatch)
    entry = store.get_or_create_session(source())
    for _ in range(3):  # slash-command handlers re-resolve the session
        entry = store.get_or_create_session(source())
    assert entry.updated_at >= entry.created_at
    assert entry.first_model_call_pending is True


def test_force_new_entry_is_pending(tmp_path, monkeypatch):
    """Matrix row: /new when no entry exists (force_new path)."""
    store = make_store(tmp_path, monkeypatch)
    entry = store.get_or_create_session(source(), force_new=True)
    assert entry.first_model_call_pending is True


def test_reset_session_mints_pending_entry(tmp_path, monkeypatch):
    store = make_store(tmp_path, monkeypatch)
    entry = store.get_or_create_session(source())
    entry.first_model_call_pending = False
    fresh = store.reset_session(entry.session_key)
    assert fresh.is_fresh_reset is True
    assert fresh.first_model_call_pending is True
    assert fresh.wake_packet_id is None


def test_old_persisted_entries_never_pending(tmp_path, monkeypatch):
    """Entries serialized by older builds lack the flag: from_dict => False,
    so an old session can never rebuild a packet."""
    now = datetime.now()
    entry = SessionEntry(session_key="k", session_id="sess_old",
                         created_at=now, updated_at=now)
    data = entry.to_dict()
    del data["first_model_call_pending"]
    revived = SessionEntry.from_dict(data)
    assert revived.first_model_call_pending is False


# -- durable persistence keyed by session_id ----------------------------------

def test_resume_restores_original_packet(tmp_path, monkeypatch):
    """Matrix row: Session A -> /new B -> /resume A."""
    store = make_store(tmp_path, monkeypatch)
    src = source()
    a = store.get_or_create_session(src)
    a_id = a.session_id
    bind(a, "packet for session A")
    store.persist_wake_packet(a)
    store._save()

    b = store.reset_session(a.session_key)      # /new -> session B
    assert b.session_id != a_id
    assert b.wake_packet_text is None

    resumed = store.switch_session(a.session_key, a_id)   # /resume A
    assert resumed.session_id == a_id
    assert resumed.wake_packet_text == "packet for session A"
    assert resumed.wake_packet_id == binding_for("packet for session A")["packet_id"]
    assert resumed.first_model_call_pending is False


def test_switch_to_old_transcript_never_binds_fresh(tmp_path, monkeypatch):
    """Matrix row: in-handler switch to an old transcript. The switched
    entry is not first-model-call pending, so the wake block never rebuilds
    current continuity into old history."""
    store = make_store(tmp_path, monkeypatch)
    src = source()
    a = store.get_or_create_session(src)
    store._db.create_session(session_id="19990101_000000_old", source="telegram")
    switched = store.switch_session(a.session_key, "19990101_000000_old")
    assert switched.first_model_call_pending is False
    assert switched.wake_packet_id is None
    assert validated_wake_text(switched) is None


def test_branch_inherits_parent_binding_via_chain(tmp_path, monkeypatch):
    """Matrix row: /branch. The branched child row carries
    parent_session_id, so the durable chain walk inherits the parent's
    packet instead of leaving the branch bare or rebuilding."""
    store = make_store(tmp_path, monkeypatch)
    src = source()
    parent = store.get_or_create_session(src)
    bind(parent, "parent packet")
    store.persist_wake_packet(parent)

    child_id = "20260713_000000_branch1"
    store._db.create_session(session_id=child_id, source="telegram",
                             parent_session_id=parent.session_id)
    branched = store.switch_session(parent.session_key, child_id)
    assert branched.wake_packet_text == "parent packet"
    assert branched.first_model_call_pending is False


def test_crash_recovery_restores_binding(tmp_path, monkeypatch):
    """Matrix row: routing entry lost (crash); recovery from state.db."""
    store = make_store(tmp_path, monkeypatch)
    src = source()
    entry = store.get_or_create_session(src)
    sid, key = entry.session_id, entry.session_key
    bind(entry, "recovered packet")
    store.persist_wake_packet(entry)
    store._db.append_message(session_id=sid, role="user", content="hi")

    # Simulate a crash that lost the routing index.
    with store._lock:
        store._entries.pop(key)
    recovered = store.get_or_create_session(src)
    assert recovered.session_id == sid
    assert recovered.wake_packet_text == "recovered packet"
    assert recovered.first_model_call_pending is False


def test_compression_publication_window_restart(tmp_path, monkeypatch):
    """Matrix row: restart during the compression parent->child publication
    window. The child row exists (parent ended, parent_session_id set) but
    the crash happened before the gateway re-keyed the binding — the chain
    walk still finds the parent's packet."""
    store = make_store(tmp_path, monkeypatch)
    src = source()
    entry = store.get_or_create_session(src)
    parent_id = entry.session_id
    bind(entry, "pre-compression packet")
    store.persist_wake_packet(entry)

    child_id = "20260713_010101_child1"
    store._db.create_session(session_id=child_id, source="telegram",
                             parent_session_id=parent_id)
    store._db.end_session(parent_id, "compression")

    binding = load_wake_binding_for_session(store._db, child_id)
    assert binding is not None
    assert binding["text"] == "pre-compression packet"

    # And the publication path re-keys it under the child explicitly.
    entry.session_id = child_id
    store.persist_wake_packet(entry)
    assert store._db.get_session_wake_packet(child_id) is not None


# -- fail-closed validation ----------------------------------------------------

def test_validate_wake_binding_rejects_corruption():
    good = binding_for("hello")
    assert validate_wake_binding(good["packet_id"], good["content_hash"],
                                 good["project_id"], good["text"])
    # Non-string text (the review's TypeError repro).
    assert not validate_wake_binding(good["packet_id"], good["content_hash"],
                                     None, {"not": "a string"})
    # Oversize text (the review's 100k bypass repro).
    big = "x" * 100_000
    assert not validate_wake_binding(
        "pkt", "sha256:" + hashlib.sha256(big.encode()).hexdigest(), None, big)
    # Hash mismatch (stored content hash must be verified).
    assert not validate_wake_binding(good["packet_id"], good["content_hash"],
                                     None, "tampered text")
    # Malformed hash / empty id.
    assert not validate_wake_binding(good["packet_id"], "md5:abc", None, "x")
    assert not validate_wake_binding("", good["content_hash"], None, "hello")


def test_from_dict_drops_corrupt_binding(tmp_path):
    now = datetime.now()
    entry = SessionEntry(session_key="k", session_id="sess_x",
                         created_at=now, updated_at=now)
    bind(entry, "valid text")
    data = entry.to_dict()
    data["wake_packet_text"] = "tampered"
    revived = SessionEntry.from_dict(data)
    assert revived.wake_packet_id is None
    assert revived.wake_packet_text is None

    data2 = entry.to_dict()
    data2["wake_packet_text"] = 12345           # wrong type
    revived2 = SessionEntry.from_dict(data2)
    assert revived2.wake_packet_text is None


def test_validated_wake_text_fails_closed():
    now = datetime.now()
    entry = SessionEntry(session_key="k", session_id="sess_y",
                         created_at=now, updated_at=now)
    bind(entry, "stable text")
    assert validated_wake_text(entry) == "stable text"
    entry.wake_packet_text = "mutated"
    assert validated_wake_text(entry) is None
    assert entry.wake_packet_id is None          # dropped, not injected


def test_durable_json_round_trip_and_corruption(tmp_path, monkeypatch):
    store = make_store(tmp_path, monkeypatch)
    entry = store.get_or_create_session(source())
    bind(entry, "round trip")
    raw = wake_binding_to_json(entry)
    assert raw is not None
    store._db.set_session_wake_packet(entry.session_id, raw)
    assert load_wake_binding_for_session(store._db, entry.session_id)["text"] \
        == "round trip"

    # Corrupt stored JSON fails closed (no restore, no raise).
    store._db.set_session_wake_packet(entry.session_id, "{not json")
    assert load_wake_binding_for_session(store._db, entry.session_id) is None
    tampered = json.loads(raw)
    tampered["text"] = "evil"
    store._db.set_session_wake_packet(entry.session_id, json.dumps(tampered))
    assert load_wake_binding_for_session(store._db, entry.session_id) is None


# -- shared surface hook (API server / TUI / ACP) ------------------------------

def test_surface_hook_restores_and_never_rebuilds_old(tmp_path, monkeypatch):
    store = make_store(tmp_path, monkeypatch)
    db = store._db
    db.create_session(session_id="surf-1", source="api")
    raw = json.dumps({"schema_version": 1, **binding_for("surface packet")})
    db.set_session_wake_packet("surf-1", raw)

    # Existing binding wins regardless of the new-session attestation.
    assert ensure_wake_text_for_session_id(
        db, "surf-1", is_new_session=True) == "surface packet"
    # An old session with no binding never builds one.
    db.create_session(session_id="surf-2", source="api")
    assert ensure_wake_text_for_session_id(
        db, "surf-2", is_new_session=False) is None


def test_surface_hook_creates_row_when_missing(tmp_path, monkeypatch):
    """Lazily-created surfaces (API/TUI) bind before their session row
    exists; the binding must not silently vanish."""
    store = make_store(tmp_path, monkeypatch)
    db = store._db
    raw = json.dumps({"schema_version": 1, **binding_for("early packet")})
    db.set_session_wake_packet("lazy-1", raw, create_source="api")
    assert db.get_session_wake_packet("lazy-1") == raw
    row = db.get_session("lazy-1")
    assert row["source"] == "api"


# -- durable attempted-none sentinel (first-call marker) ------------------------

def test_surface_hook_marks_attempted_none_durably(tmp_path, monkeypatch):
    """A first call that finds no packet (no store here) persists the
    explicit attempted-none sentinel — the durable first-call marker."""
    from gateway.continuity_wake import (load_wake_state_for_session,
                                         wake_state_from_json)
    store = make_store(tmp_path, monkeypatch)
    db = store._db
    db.create_session(session_id="none-1", source="api")
    assert ensure_wake_text_for_session_id(
        db, "none-1", is_new_session=True, first_message="hi") is None
    state, binding = wake_state_from_json(db.get_session_wake_packet("none-1"))
    assert state == "none" and binding is None
    assert load_wake_state_for_session(db, "none-1") == ("none", None)


def test_attempted_none_session_never_binds_later(tmp_path, monkeypatch):
    """After the sentinel, later calls can never opportunistically bind —
    even when the caller (wrongly) re-attests is_new_session and a store
    has appeared since."""
    from dream_cycle_v3.store import ContinuityStore
    from hermes_constants import get_hermes_home
    store = make_store(tmp_path, monkeypatch)
    db = store._db
    db.create_session(session_id="none-2", source="api")
    assert ensure_wake_text_for_session_id(
        db, "none-2", is_new_session=True, first_message="hi") is None

    # A store appears after the session's one first-call attempt.
    store_path = get_hermes_home() / "dream-cycle-v3" / "continuity.db"
    store_path.parent.mkdir(parents=True, exist_ok=True)
    with ContinuityStore(store_path) as cstore:
        cstore.migrate("2026-07-11T08:00:00+00:00")

    assert ensure_wake_text_for_session_id(
        db, "none-2", is_new_session=True, first_message="hi again") is None
    # A genuinely NEW session on the same surface does bind.
    db.create_session(session_id="fresh-1", source="api")
    text = ensure_wake_text_for_session_id(
        db, "fresh-1", is_new_session=True, first_message="hi")
    assert text is not None and "[Continuity wake packet" in text


def test_sentinel_terminates_parent_chain_walk(tmp_path, monkeypatch):
    """A child of an attempted-none session inherits 'none' — the walk must
    stop at the sentinel, not skip past it to a bound grandparent."""
    from gateway.continuity_wake import load_wake_state_for_session
    store = make_store(tmp_path, monkeypatch)
    db = store._db
    db.create_session(session_id="grand-1", source="api")
    db.set_session_wake_packet(
        "grand-1", json.dumps({"schema_version": 1,
                               **binding_for("grandparent packet")}))
    db.create_session(session_id="parent-1", source="api",
                      parent_session_id="grand-1")
    db.set_session_wake_packet(
        "parent-1", json.dumps({"schema_version": 1, "state": "none"}))
    db.create_session(session_id="child-1", source="api",
                      parent_session_id="parent-1")
    assert load_wake_state_for_session(db, "child-1") == ("none", None)
    assert ensure_wake_text_for_session_id(
        db, "child-1", is_new_session=False) is None
    # Without the sentinel in between, the chain still inherits normally.
    db.create_session(session_id="child-2", source="api",
                      parent_session_id="grand-1")
    assert ensure_wake_text_for_session_id(
        db, "child-2", is_new_session=False) == "grandparent packet"


# -- corrupt-present durable state is terminal (never rebuilt over) -------------

def _seed_continuity_store():
    from dream_cycle_v3.store import ContinuityStore
    from hermes_constants import get_hermes_home
    store_path = get_hermes_home() / "dream-cycle-v3" / "continuity.db"
    store_path.parent.mkdir(parents=True, exist_ok=True)
    with ContinuityStore(store_path) as cstore:
        cstore.migrate("2026-07-11T08:00:00+00:00")


def test_corrupt_durable_state_never_rebuilds(tmp_path, monkeypatch):
    """Present-but-corrupt wake state is distinct from truly absent: even a
    (wrong) is_new_session=True attestation — the API's empty-history
    heuristic — must not rebuild current continuity into that session, and
    the corrupt record is left in place, not overwritten."""
    from gateway.continuity_wake import (load_wake_state_for_session,
                                         wake_state_from_json)
    _seed_continuity_store()   # a rebuild WOULD produce a packet if allowed
    store = make_store(tmp_path, monkeypatch)
    db = store._db

    good = json.dumps({"schema_version": 1, **binding_for("ok packet")})
    tampered_dict = json.loads(good)
    tampered_dict["text"] = "evil"
    corrupt_values = [
        "{not json",                                   # unparseable
        json.dumps({"schema_version": 99}),            # wrong schema
        json.dumps(tampered_dict),                     # hash mismatch
        "x" * 70_000,                                  # oversized
    ]
    for i, raw in enumerate(corrupt_values):
        sid = f"corrupt-{i}"
        db.create_session(session_id=sid, source="api")
        db.set_session_wake_packet(sid, raw)
        assert wake_state_from_json(raw) == ("corrupt", None)
        assert load_wake_state_for_session(db, sid) == ("corrupt", None)
        assert ensure_wake_text_for_session_id(
            db, sid, is_new_session=True, first_message="hi") is None
        # Fail closed is read-only: the corrupt record is preserved.
        assert db.get_session_wake_packet(sid) == raw

    # Truly absent (NULL) on a genuinely new session still binds normally.
    db.create_session(session_id="absent-ok", source="api")
    text = ensure_wake_text_for_session_id(
        db, "absent-ok", is_new_session=True, first_message="hi")
    assert text is not None and "[Continuity wake packet" in text


def test_corrupt_parent_state_blocks_child_inheritance(tmp_path, monkeypatch):
    from gateway.continuity_wake import load_wake_state_for_session
    store = make_store(tmp_path, monkeypatch)
    db = store._db
    db.create_session(session_id="cg-1", source="api")
    db.set_session_wake_packet(
        "cg-1", json.dumps({"schema_version": 1,
                            **binding_for("grand packet")}))
    db.create_session(session_id="cp-1", source="api",
                      parent_session_id="cg-1")
    db.set_session_wake_packet("cp-1", "{corrupt")
    db.create_session(session_id="cc-1", source="api",
                      parent_session_id="cp-1")
    # The walk stops at the corrupt parent — no skip to the bound ancestor.
    assert load_wake_state_for_session(db, "cc-1") == ("corrupt", None)
    assert ensure_wake_text_for_session_id(
        db, "cc-1", is_new_session=False) is None


# -- gateway first-call durability (entry path over the shared lifecycle) -------

def test_gateway_attempted_none_is_durable_and_survives_replay(tmp_path,
                                                               monkeypatch):
    """A gateway first-call attempt that produces no packet persists the
    attempted-none sentinel by durable session_id —
    first_model_call_pending=False in sessions.json alone is not the
    marker. A crash/replay whose stale routing store re-attests
    pending=True must not rebuild, even after a store appears."""
    from gateway.continuity_wake import (ensure_wake_packet,
                                         load_wake_state_for_session)
    store = make_store(tmp_path, monkeypatch)
    entry = store.get_or_create_session(source())
    assert ensure_wake_packet(entry, is_new_session=True, first_message="hi",
                              session_db=store._db) is False
    assert entry.wake_packet_id is None
    assert load_wake_state_for_session(store._db, entry.session_id) \
        == ("none", None)

    _seed_continuity_store()   # store appears AFTER the one attempt
    now = datetime.now()
    replay = SessionEntry(session_key=entry.session_key,
                          session_id=entry.session_id,
                          created_at=now, updated_at=now,
                          first_model_call_pending=True)
    assert ensure_wake_packet(replay, is_new_session=True, first_message="hi",
                              session_db=store._db) is False
    assert replay.wake_packet_id is None
    assert load_wake_state_for_session(store._db, entry.session_id) \
        == ("none", None)


def test_gateway_none_sentinel_blocks_cross_surface_rebind(tmp_path,
                                                           monkeypatch):
    """Cross-surface: an API-style hook on the same durable session_id sees
    the gateway's sentinel and never rebuilds, and a compression child of
    that session inherits the sentinel through the parent chain."""
    from gateway.continuity_wake import (ensure_wake_packet,
                                         load_wake_state_for_session)
    store = make_store(tmp_path, monkeypatch)
    entry = store.get_or_create_session(source())
    assert ensure_wake_packet(entry, is_new_session=True, first_message="hi",
                              session_db=store._db) is False
    _seed_continuity_store()
    assert ensure_wake_text_for_session_id(
        store._db, entry.session_id, is_new_session=True,
        first_message="hi again") is None

    child_id = "20260713_020202_child2"
    store._db.create_session(session_id=child_id, source="telegram",
                             parent_session_id=entry.session_id)
    assert load_wake_state_for_session(store._db, child_id) == ("none", None)


def test_gateway_bound_replay_restores_never_rebuilds(tmp_path, monkeypatch):
    """A crash/replay of a session whose packet WAS bound restores the
    durable binding onto the fresh entry verbatim — no rebuild, even with
    a (wrong) pending=True attestation."""
    import gateway.continuity_wake as cw
    _seed_continuity_store()
    store = make_store(tmp_path, monkeypatch)
    entry = store.get_or_create_session(source())
    assert cw.ensure_wake_packet(entry, is_new_session=True,
                                 first_message="hi",
                                 session_db=store._db) is True
    original = entry.wake_packet_text
    assert original and "[Continuity wake packet" in original
    state, binding = cw.load_wake_state_for_session(store._db,
                                                    entry.session_id)
    assert state == "bound" and binding["text"] == original

    monkeypatch.setattr(cw, "build_wake_packet_for_session",
                        lambda *a, **k: pytest.fail("rebuilt a wake packet"))
    now = datetime.now()
    replay = SessionEntry(session_key=entry.session_key,
                          session_id=entry.session_id,
                          created_at=now, updated_at=now,
                          first_model_call_pending=True)
    assert cw.ensure_wake_packet(replay, is_new_session=True,
                                 first_message="different first message",
                                 session_db=store._db) is True
    assert replay.wake_packet_text == original


def test_gateway_corrupt_durable_state_blocks_entry_bind(tmp_path,
                                                         monkeypatch):
    """Present-but-corrupt durable state is terminal on the gateway entry
    path too: no bind, no rebuild, record preserved."""
    from gateway.continuity_wake import ensure_wake_packet
    _seed_continuity_store()
    store = make_store(tmp_path, monkeypatch)
    entry = store.get_or_create_session(source())
    store._db.set_session_wake_packet(entry.session_id, "{corrupt")
    assert ensure_wake_packet(entry, is_new_session=True, first_message="hi",
                              session_db=store._db) is False
    assert entry.wake_packet_id is None
    assert store._db.get_session_wake_packet(entry.session_id) == "{corrupt"


def test_gateway_without_session_db_keeps_legacy_gate(tmp_path, monkeypatch):
    """Legacy/lightweight store doubles pass session_db=None: the
    pre-Phase-3 in-memory gate still binds (nothing durable required)."""
    from gateway.continuity_wake import ensure_wake_packet
    _seed_continuity_store()
    now = datetime.now()
    entry = SessionEntry(session_key="legacy", session_id="sess_legacy",
                         created_at=now, updated_at=now,
                         first_model_call_pending=True)
    assert ensure_wake_packet(entry, is_new_session=True,
                              first_message="hi") is True
    assert entry.wake_packet_text
    assert "[Continuity wake packet" in entry.wake_packet_text


# -- concurrent first calls (compare-and-set) -----------------------------------

def test_concurrent_first_calls_bind_exactly_one_packet(tmp_path, monkeypatch):
    """Realistic API shape: several worker threads race the same session's
    first call. The only-if-absent CAS lets exactly one build win; every
    caller returns the persisted winner's bytes."""
    import concurrent.futures as cf
    from gateway.continuity_wake import load_wake_state_for_session
    _seed_continuity_store()
    store = make_store(tmp_path, monkeypatch)
    db = store._db
    db.create_session(session_id="race-1", source="api")

    def first_call(i):
        return ensure_wake_text_for_session_id(
            db, "race-1", is_new_session=True,
            first_message=f"hello {i}", create_source="api")

    with cf.ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(first_call, range(8)))

    assert all(r is not None for r in results)
    assert len(set(results)) == 1        # one set of bytes, everywhere
    state, binding = load_wake_state_for_session(db, "race-1")
    assert state == "bound"
    assert results[0] == binding["text"]  # everyone saw the persisted winner


def test_cas_write_is_only_if_absent(tmp_path, monkeypatch):
    store = make_store(tmp_path, monkeypatch)
    db = store._db
    db.create_session(session_id="cas-1", source="api")
    first = json.dumps({"schema_version": 1, **binding_for("first value")})
    second = json.dumps({"schema_version": 1, **binding_for("second value")})
    assert db.set_session_wake_packet("cas-1", first,
                                      only_if_absent=True) is True
    assert db.set_session_wake_packet("cas-1", second,
                                      only_if_absent=True) is False
    assert db.get_session_wake_packet("cas-1") == first
    # Lazy-row CAS creates the bare row exactly once.
    assert db.set_session_wake_packet("cas-2", first, create_source="api",
                                      only_if_absent=True) is True
    assert db.set_session_wake_packet("cas-2", second, create_source="api",
                                      only_if_absent=True) is False
    assert db.get_session_wake_packet("cas-2") == first


# -- durable pre-first-call pending sentinel (restart eligibility) ---------------

def test_pending_sentinel_binds_on_first_prompt_despite_not_new(tmp_path,
                                                                monkeypatch):
    """A row durably marked pending (persisted before any prompt) binds at
    the next real first message even when the surface, after a restart, can
    only attest is_new_session=False — and exactly once."""
    from gateway.continuity_wake import (load_wake_state_for_session,
                                         mark_wake_pending_for_session_id,
                                         wake_state_from_json)
    _seed_continuity_store()
    store = make_store(tmp_path, monkeypatch)
    db = store._db
    db.create_session(session_id="pend-1", source="acp")
    assert mark_wake_pending_for_session_id(db, "pend-1") is True
    assert wake_state_from_json(
        db.get_session_wake_packet("pend-1")) == ("pending", None)

    text = ensure_wake_text_for_session_id(
        db, "pend-1", is_new_session=False, first_message="first real prompt")
    assert text is not None and "[Continuity wake packet" in text
    state, binding = load_wake_state_for_session(db, "pend-1")
    assert state == "bound" and binding["text"] == text
    # The one attempt is consumed: replay returns the stored bytes.
    assert ensure_wake_text_for_session_id(
        db, "pend-1", is_new_session=True, first_message="other") == text


def test_pending_attempt_without_store_becomes_terminal_none(tmp_path,
                                                             monkeypatch):
    """The pending session's one attempt with no store persists the
    attempted-none sentinel — pending never grants a second attempt."""
    from gateway.continuity_wake import (load_wake_state_for_session,
                                         mark_wake_pending_for_session_id)
    store = make_store(tmp_path, monkeypatch)
    db = store._db
    db.create_session(session_id="pend-2", source="acp")
    mark_wake_pending_for_session_id(db, "pend-2")
    assert ensure_wake_text_for_session_id(
        db, "pend-2", is_new_session=False, first_message="hi") is None
    assert load_wake_state_for_session(db, "pend-2") == ("none", None)
    _seed_continuity_store()
    assert ensure_wake_text_for_session_id(
        db, "pend-2", is_new_session=True, first_message="hi again") is None


def test_mark_pending_never_overwrites_existing_state(tmp_path, monkeypatch):
    """The pending marker is CAS'd only-if-absent: bound/none/corrupt
    records (and a missing row without create_source) are untouched — a
    history-bearing session can never be re-marked eligible."""
    from gateway.continuity_wake import mark_wake_pending_for_session_id
    store = make_store(tmp_path, monkeypatch)
    db = store._db
    bound = json.dumps({"schema_version": 1, **binding_for("kept packet")})
    for sid, raw in (
            ("mark-bound", bound),
            ("mark-none", json.dumps({"schema_version": 1, "state": "none"})),
            ("mark-corrupt", "{not json")):
        db.create_session(session_id=sid, source="acp")
        db.set_session_wake_packet(sid, raw)
        assert mark_wake_pending_for_session_id(db, sid) is False
        assert db.get_session_wake_packet(sid) == raw
    # Without create_source no row is ever created (no-empty-row surfaces).
    assert mark_wake_pending_for_session_id(db, "mark-missing") is False
    assert db.get_session("mark-missing") is None


# -- post-verification finding 7: parser/CAS agreement on pending records --------

def test_noncanonical_pending_record_is_replaced_by_first_outcome(
        tmp_path, monkeypatch):
    """A semantically-pending record whose raw bytes differ from the
    canonical sentinel (key order, whitespace, extra fields from an older
    build) must still be consumed by the session's one first-call attempt:
    what the parser classifies as pending, the CAS must treat as
    replaceable — otherwise the session retries a fresh build on every
    prompt and no outcome ever persists."""
    from gateway.continuity_wake import (load_wake_state_for_session,
                                         wake_state_from_json)
    _seed_continuity_store()
    store = make_store(tmp_path, monkeypatch)
    db = store._db
    noncanonical = [
        '{"state": "pending", "schema_version": 1}',
        '{ "schema_version" : 1 , "state" : "pending" }',
        json.dumps({"schema_version": 1, "state": "pending",
                    "marker": "old-build"}),
    ]
    for i, raw in enumerate(noncanonical):
        sid = f"noncanon-{i}"
        db.create_session(session_id=sid, source="api")
        db.set_session_wake_packet(sid, raw)
        assert wake_state_from_json(raw)[0] == "pending"
        text = ensure_wake_text_for_session_id(
            db, sid, is_new_session=False, first_message="hi")
        assert text is not None and "[Continuity wake packet" in text
        state, _ = load_wake_state_for_session(db, sid)
        assert state == "bound"


def test_noncanonical_pending_without_store_becomes_terminal_none(
        tmp_path, monkeypatch):
    store = make_store(tmp_path, monkeypatch)
    db = store._db
    db.create_session(session_id="noncanon-none", source="api")
    db.set_session_wake_packet("noncanon-none",
                               '{"state": "pending", "schema_version": 1}')
    assert ensure_wake_text_for_session_id(
        db, "noncanon-none", is_new_session=False, first_message="hi") is None
    from gateway.continuity_wake import load_wake_state_for_session
    assert load_wake_state_for_session(db, "noncanon-none") == ("none", None)


# -- post-verification finding 8: failed attempts stay pending -------------------

def test_failed_cas_write_keeps_pending_and_reports_unconcluded(
        tmp_path, monkeypatch):
    """A CAS/storage failure mid-attempt must leave the durable pending
    sentinel in place AND report a non-concluded state, so surfaces keep
    their in-memory deferral markers. Consuming the marker while durable
    state stays pending lets a restart rearm a history-bearing
    transcript."""
    import sqlite3 as _sqlite3
    from gateway import continuity_wake
    _seed_continuity_store()
    store = make_store(tmp_path, monkeypatch)
    db = store._db
    db.create_session(session_id="fail-1", source="api")
    assert continuity_wake.mark_wake_pending_for_session_id(db, "fail-1")

    original = db.set_session_wake_packet

    def _boom(*args, **kwargs):
        raise _sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(db, "set_session_wake_packet", _boom)
    state, binding = continuity_wake.ensure_wake_state_for_session_id(
        db, "fail-1", is_new_session=False, first_message="hi")
    assert binding is None
    assert not continuity_wake.wake_attempt_concluded(state)
    monkeypatch.setattr(db, "set_session_wake_packet", original)

    # Durable state is still the pending sentinel: the one attempt was not
    # consumed, and the next prompt binds normally.
    assert continuity_wake.load_wake_state_for_session(
        db, "fail-1") == ("pending", None)
    state, binding = continuity_wake.ensure_wake_state_for_session_id(
        db, "fail-1", is_new_session=False, first_message="hi")
    assert state == "bound" and binding is not None
    assert continuity_wake.wake_attempt_concluded(state)


def test_wake_attempt_concluded_classification():
    from gateway.continuity_wake import wake_attempt_concluded
    for terminal in ("bound", "none", "corrupt", "absent", "exhausted"):
        assert wake_attempt_concluded(terminal)
    for retryable in ("pending", "unavailable"):
        assert not wake_attempt_concluded(retryable)


# -- post-verification finding 9: deep chains terminate safely / materialize ----

def test_deep_parent_chain_is_terminal_not_absent(tmp_path, monkeypatch):
    """Walk exhaustion beyond the parent-chain limit must fail closed —
    no packet, no rebuild — never read as absent (which would let a
    surface bind fresh continuity into a deep descendant transcript)."""
    from gateway.continuity_wake import load_wake_state_for_session
    _seed_continuity_store()   # a rebuild WOULD produce a packet if allowed
    store = make_store(tmp_path, monkeypatch)
    db = store._db
    db.create_session(session_id="root-0", source="api")
    db.set_session_wake_packet(
        "root-0", json.dumps({"schema_version": 1,
                              **binding_for("root packet")}))
    prev = "root-0"
    for i in range(1, 13):
        sid = f"hop-{i}"
        db.create_session(session_id=sid, source="api",
                          parent_session_id=prev)
        prev = sid
    state, binding = load_wake_state_for_session(db, prev)
    assert state != "absent" and binding is None
    assert ensure_wake_text_for_session_id(
        db, prev, is_new_session=True, first_message="hi") is None
    # Fail closed is read-only: nothing was persisted onto the child.
    assert db.get_session_wake_packet(prev) is None


def test_materialize_wake_record_keeps_deep_rotations_inherited(
        tmp_path, monkeypatch):
    """Rotation surfaces copy the parent chain's terminating record
    verbatim onto each child, so inheritance never depends on walking an
    ever-growing chain."""
    from gateway.continuity_wake import (load_wake_state_for_session,
                                         materialize_wake_record_for_child)
    store = make_store(tmp_path, monkeypatch)
    db = store._db
    raw = json.dumps({"schema_version": 1, **binding_for("rotated packet")})
    db.create_session(session_id="rot-0", source="api")
    db.set_session_wake_packet("rot-0", raw)
    prev = "rot-0"
    for i in range(1, 15):
        sid = f"rot-{i}"
        db.create_session(session_id=sid, source="api",
                          parent_session_id=prev)
        assert materialize_wake_record_for_child(db, sid, prev) is True
        assert db.get_session_wake_packet(sid) == raw
        prev = sid
    state, binding = load_wake_state_for_session(db, prev)
    assert state == "bound" and binding["text"] == "rotated packet"


def test_materialize_copies_terminal_records_verbatim_never_overwrites(
        tmp_path, monkeypatch):
    from gateway.continuity_wake import (load_wake_state_for_session,
                                         materialize_wake_record_for_child)
    store = make_store(tmp_path, monkeypatch)
    db = store._db
    cases = [
        ("corrupt", "{not json"),
        ("none", json.dumps({"schema_version": 1, "state": "none"})),
        ("pending", json.dumps({"schema_version": 1, "state": "pending"})),
    ]
    for state_name, raw in cases:
        parent, child = f"mat-{state_name}-p", f"mat-{state_name}-c"
        db.create_session(session_id=parent, source="api")
        db.set_session_wake_packet(parent, raw)
        db.create_session(session_id=child, source="api",
                          parent_session_id=parent)
        assert materialize_wake_record_for_child(db, child, parent) is True
        assert db.get_session_wake_packet(child) == raw
        assert load_wake_state_for_session(db, child)[0] == state_name

    # An absent parent materializes nothing.
    db.create_session(session_id="mat-absent-p", source="api")
    db.create_session(session_id="mat-absent-c", source="api",
                      parent_session_id="mat-absent-p")
    assert materialize_wake_record_for_child(
        db, "mat-absent-c", "mat-absent-p") is False
    assert db.get_session_wake_packet("mat-absent-c") is None

    # An existing child record is never overwritten.
    bound_raw = json.dumps({"schema_version": 1,
                            **binding_for("child's own")})
    db.create_session(session_id="mat-keep-p", source="api")
    db.set_session_wake_packet(
        "mat-keep-p", json.dumps({"schema_version": 1, "state": "none"}))
    db.create_session(session_id="mat-keep-c", source="api",
                      parent_session_id="mat-keep-p")
    db.set_session_wake_packet("mat-keep-c", bound_raw)
    assert materialize_wake_record_for_child(
        db, "mat-keep-c", "mat-keep-p") is False
    assert db.get_session_wake_packet("mat-keep-c") == bound_raw


# -- re-review blocker 5: messaging /branch materializes the parent record ------

@pytest.mark.asyncio
async def test_messaging_branch_chain_materializes_wake_record(tmp_path,
                                                               monkeypatch):
    """The messaging /branch handler creates a parent-linked child session;
    each branch must copy the parent chain's terminating wake record
    verbatim onto the child, so a 12-hop branch chain still resolves its
    binding instead of reaching terminal 'exhausted' past the ten-hop walk
    limit."""
    from types import SimpleNamespace
    from gateway.continuity_wake import load_wake_state_for_session
    from gateway.session import AsyncSessionStore
    from gateway.slash_commands import GatewaySlashCommandsMixin
    from hermes_state import AsyncSessionDB

    store = make_store(tmp_path, monkeypatch)
    db = store._db
    src = source()
    entry = store.get_or_create_session(src)
    db.create_session(session_id=entry.session_id, source="telegram")
    db.append_message(session_id=entry.session_id, role="user",
                      content="first question")
    db.append_message(session_id=entry.session_id, role="assistant",
                      content="first answer")
    raw = json.dumps({"schema_version": 1,
                      **binding_for("messaging branch packet")})
    db.set_session_wake_packet(entry.session_id, raw)

    gw = SimpleNamespace(
        _session_db=AsyncSessionDB(db),
        session_store=store,
        # tyler/live migrated the branch handler to the async session-store
        # facade; the mock must expose it the way gateway.run.py does.
        async_session_store=AsyncSessionStore(store),
        config={},
        _session_key_for_source=lambda s: entry.session_key,
        _clear_session_boundary_security_state=lambda key: None,
        _evict_cached_agent=lambda key: None,
    )
    event = SimpleNamespace(source=src, get_command_args=lambda: "")

    for i in range(12):
        parent_id = store.get_or_create_session(src).session_id
        out = await GatewaySlashCommandsMixin._handle_branch_command(
            gw, event)
        assert isinstance(out, str) and out, i
        child_id = store.get_or_create_session(src).session_id
        assert child_id != parent_id, i
        assert db.get_session(child_id)["parent_session_id"] == parent_id, i
        assert db.get_session_wake_packet(child_id) == raw, i

    leaf = store.get_or_create_session(src).session_id
    state, binding = load_wake_state_for_session(db, leaf)
    assert state == "bound" and binding["text"] == "messaging branch packet"
