import pytest
"""Phase 3 wake-packet lifecycle on the ACP surface.

Packet construction is deferred from create_session to the first
model-bound user prompt (ensure_wake_for_prompt), so explicit task/project
evidence in the actual first message can activate a project. Forks inherit
their parent's binding at creation; restores reattach the original binding
verbatim and never rebuild.
"""
import json

from acp_adapter.session import SessionManager
from dream_cycle_v3.store import ContinuityStore
from hermes_constants import get_hermes_home
from hermes_state import SessionDB

NOW_ISO = "2026-07-11T08:00:00+00:00"


class FakeAgent:
    def __init__(self):
        self.ephemeral_system_prompt = None
        self.model = "test-model"


def seed_store():
    store_path = get_hermes_home() / "dream-cycle-v3" / "continuity.db"
    store_path.parent.mkdir(parents=True, exist_ok=True)
    with ContinuityStore(store_path) as store:
        store.migrate(NOW_ISO)
        store.upsert_project({
            "schema_version": 1, "project_id": "acp-proj",
            "canonical_name": "ACP project", "aliases": ["acpproj"],
            "canonical_paths": [], "repositories": [], "status": "active",
            "owner": "default",
            "task_ssot": {"provider": "none", "locator": None,
                          "write_policy": "read_only"},
            "context_skill_id": None, "memory_policy": "warm_only",
            "sensitivity_policy": "normal", "retrieval_terms": [],
            "registry_version": 1,
            "last_verified_at": "2026-07-10T00:00:00+00:00",
        }, NOW_ISO)


def make_manager(tmp_path) -> SessionManager:
    return SessionManager(agent_factory=FakeAgent,
                          db=SessionDB(db_path=tmp_path / "state.db"))


def test_create_session_defers_binding(tmp_path):
    from gateway.continuity_wake import wake_state_from_json
    seed_store()
    mgr = make_manager(tmp_path)
    state = mgr.create_session(cwd=".")
    assert state.wake_pending is True
    assert state.agent.ephemeral_system_prompt is None
    # No binding yet — only the durable pre-first-call pending sentinel, so
    # the deferral survives a restart (see the restart regression below).
    raw = mgr._get_db().get_session_wake_packet(state.session_id)
    assert wake_state_from_json(raw) == ("pending", None)


def test_first_prompt_binds_with_message_evidence(tmp_path):
    seed_store()
    mgr = make_manager(tmp_path)
    state = mgr.create_session(cwd=".")
    mgr.ensure_wake_for_prompt(state, "acpproj status please")
    text = state.agent.ephemeral_system_prompt
    assert text and "[Continuity wake packet" in text
    # The real first message activated the project — impossible with the
    # old create-time first_message="" bind.
    assert "Active project: ACP project" in text
    # Durable binding landed; the marker is consumed.
    raw = mgr._get_db().get_session_wake_packet(state.session_id)
    assert raw and json.loads(raw)["text"] in text
    assert state.wake_pending is False
    mgr.ensure_wake_for_prompt(state, "second message")
    assert state.agent.ephemeral_system_prompt == text  # no rebind/duplicate


def test_fork_inherits_parent_binding(tmp_path):
    seed_store()
    mgr = make_manager(tmp_path)
    parent = mgr.create_session(cwd=".")
    mgr.ensure_wake_for_prompt(parent, "acpproj status please")
    parent_binding = json.loads(
        mgr._get_db().get_session_wake_packet(parent.session_id))

    child = mgr.fork_session(parent.session_id, cwd=".")
    assert child.wake_pending is False
    child_binding = json.loads(
        mgr._get_db().get_session_wake_packet(child.session_id))
    assert child_binding["packet_id"] == parent_binding["packet_id"]
    assert child_binding["text"] == parent_binding["text"]
    assert parent_binding["text"] in child.agent.ephemeral_system_prompt
    # A fork's first prompt never rebinds over the inherited packet.
    before = child.agent.ephemeral_system_prompt
    mgr.ensure_wake_for_prompt(child, "acpproj again")
    assert child.agent.ephemeral_system_prompt == before


def test_restore_reattaches_original_binding(tmp_path):
    seed_store()
    mgr = make_manager(tmp_path)
    state = mgr.create_session(cwd=".")
    mgr.ensure_wake_for_prompt(state, "acpproj status please")
    original = state.agent.ephemeral_system_prompt
    session_id = state.session_id

    # Simulate process restart: drop the in-memory state, restore from DB.
    with mgr._lock:
        mgr._sessions.pop(session_id)
    restored = mgr.get_session(session_id)
    assert restored is not None
    assert restored.agent.ephemeral_system_prompt == original
    assert getattr(restored, "wake_pending", False) is False


def test_zero_message_session_restart_binds_on_first_prompt(tmp_path):
    """A restart between create_session and the first prompt must not
    strand the session: the persisted empty row carries the durable
    pre-first-call pending sentinel, restore rearms the deferred bind, and
    the first real prompt binds once with that message as evidence."""
    from gateway.continuity_wake import wake_state_from_json
    seed_store()
    mgr = make_manager(tmp_path)
    state = mgr.create_session(cwd=".")
    session_id = state.session_id
    raw = mgr._get_db().get_session_wake_packet(session_id)
    assert wake_state_from_json(raw) == ("pending", None)

    # Process restart: a brand-new manager over the same state.db.
    mgr2 = make_manager(tmp_path)
    restored = mgr2.get_session(session_id)
    assert restored is not None
    assert restored.wake_pending is True
    assert restored.agent.ephemeral_system_prompt is None

    mgr2.ensure_wake_for_prompt(restored, "acpproj status please")
    text = restored.agent.ephemeral_system_prompt
    assert text and "Active project: ACP project" in text
    binding = json.loads(mgr2._get_db().get_session_wake_packet(session_id))
    assert binding["text"] in text

    # Exactly once: another restart reattaches the binding, never rebinds.
    mgr3 = make_manager(tmp_path)
    again = mgr3.get_session(session_id)
    assert again.wake_pending is False
    assert again.agent.ephemeral_system_prompt == binding["text"]


def test_history_bearing_restored_session_never_binds(tmp_path):
    """A pre-Phase-3 transcript (real messages, no durable wake state) must
    never become bind-eligible through restart recovery."""
    seed_store()
    mgr = make_manager(tmp_path)
    db = mgr._get_db()
    db.create_session(session_id="legacy-acp-1", source="acp")
    db.append_message(session_id="legacy-acp-1", role="user",
                      content="old question")
    db.append_message(session_id="legacy-acp-1", role="assistant",
                      content="old answer")

    restored = mgr.get_session("legacy-acp-1")
    assert restored is not None
    assert len(restored.history) == 2
    assert restored.wake_pending is False
    assert restored.agent.ephemeral_system_prompt is None
    mgr.ensure_wake_for_prompt(restored, "acpproj status please")
    assert restored.agent.ephemeral_system_prompt is None
    assert db.get_session_wake_packet("legacy-acp-1") is None


def test_history_bearing_pending_record_settles_without_late_binding(tmp_path):
    """A failed first-turn wake read cannot rebind after restart history."""
    from gateway.continuity_wake import (mark_wake_pending_for_session_id,
                                         wake_state_from_json)
    seed_store()
    mgr = make_manager(tmp_path)
    db = mgr._get_db()
    db.create_session(session_id="late-pending-acp", source="acp")
    db.append_message(session_id="late-pending-acp", role="user", content="old")
    db.append_message(session_id="late-pending-acp", role="assistant", content="answer")
    mark_wake_pending_for_session_id(db, "late-pending-acp")
    restored = mgr.get_session("late-pending-acp")
    assert restored is not None
    assert restored.wake_pending is False
    assert restored.agent.ephemeral_system_prompt is None
    assert wake_state_from_json(db.get_session_wake_packet("late-pending-acp")) == (
        "none", None)


def test_fork_inherits_attempted_none_terminally(tmp_path):
    """A fork of an attempted-none parent durably carries the sentinel:
    no other surface can later attest the child new and bind it."""
    from gateway.continuity_wake import (ensure_wake_text_for_session_id,
                                         wake_state_from_json)
    mgr = make_manager(tmp_path)  # NO store: first prompt -> attempted-none
    parent = mgr.create_session(cwd=".")
    mgr.ensure_wake_for_prompt(parent, "hello")
    db = mgr._get_db()
    assert wake_state_from_json(
        db.get_session_wake_packet(parent.session_id)) == ("none", None)

    child = mgr.fork_session(parent.session_id, cwd=".")
    assert child.agent.ephemeral_system_prompt is None
    assert child.wake_pending is False
    assert wake_state_from_json(
        db.get_session_wake_packet(child.session_id)) == ("none", None)

    # A store appears and another surface claims the child is new — the
    # inherited terminal sentinel still wins.
    seed_store()
    assert ensure_wake_text_for_session_id(
        db, child.session_id, is_new_session=True,
        first_message="acpproj please", create_source="api") is None
    assert wake_state_from_json(
        db.get_session_wake_packet(child.session_id)) == ("none", None)


def test_fork_inherits_corrupt_verbatim(tmp_path):
    """A fork of a corrupt-wake parent copies the exact corrupt record:
    terminal (never injected, never rebuilt over) and never silently turned
    into absent — another surface's new-session attestation cannot rebind
    the child."""
    from gateway.continuity_wake import (ensure_wake_text_for_session_id,
                                         wake_state_from_json)
    seed_store()  # a rebuild WOULD produce a packet if corruption leaked
    mgr = make_manager(tmp_path)
    parent = mgr.create_session(cwd=".")
    db = mgr._get_db()
    corrupt_raw = json.dumps({
        "schema_version": 1, "packet_id": "pkt-x",
        "content_hash": "sha256:" + "0" * 64,
        "project_id": None, "text": "tampered"})
    db.set_session_wake_packet(parent.session_id, corrupt_raw)
    assert wake_state_from_json(corrupt_raw)[0] == "corrupt"

    child = mgr.fork_session(parent.session_id, cwd=".")
    assert child.agent.ephemeral_system_prompt is None
    assert child.wake_pending is False
    assert db.get_session_wake_packet(child.session_id) == corrupt_raw
    assert ensure_wake_text_for_session_id(
        db, child.session_id, is_new_session=True,
        first_message="acpproj please", create_source="api") is None
    assert db.get_session_wake_packet(child.session_id) == corrupt_raw


def test_fork_of_pending_parent_defers_like_parent(tmp_path):
    """Forking a zero-message parent (still pre-first-call) hands the child
    the same durable eligibility: the child binds at ITS first prompt, and
    the parent's own deferred bind stays untouched."""
    from gateway.continuity_wake import wake_state_from_json
    seed_store()
    mgr = make_manager(tmp_path)
    parent = mgr.create_session(cwd=".")  # no prompt yet -> durable pending
    child = mgr.fork_session(parent.session_id, cwd=".")
    db = mgr._get_db()
    assert child.wake_pending is True
    assert wake_state_from_json(
        db.get_session_wake_packet(child.session_id)) == ("pending", None)

    mgr.ensure_wake_for_prompt(child, "acpproj status please")
    assert "Active project: ACP project" in child.agent.ephemeral_system_prompt
    assert wake_state_from_json(
        db.get_session_wake_packet(parent.session_id)) == ("pending", None)


def test_no_store_first_prompt_marks_none_durably(tmp_path):
    """The deferred first-prompt attempt with no store persists the
    attempted-none sentinel: a store created later can never bind into
    this session."""
    from gateway.continuity_wake import wake_state_from_json
    mgr = make_manager(tmp_path)
    state = mgr.create_session(cwd=".")
    mgr.ensure_wake_for_prompt(state, "hello")
    assert state.agent.ephemeral_system_prompt is None
    raw = mgr._get_db().get_session_wake_packet(state.session_id)
    assert wake_state_from_json(raw) == ("none", None)

    seed_store()
    state.wake_pending = True  # even a wrongly re-armed marker cannot bind
    mgr.ensure_wake_for_prompt(state, "hello again")
    assert state.agent.ephemeral_system_prompt is None


# -- post-verification finding 8: failed attempts keep the deferral marker ------

def test_failed_attempt_never_retries_after_model_bound_turn(tmp_path, monkeypatch):
    """A failed wake read must not mutate prompt bytes on a later turn."""
    from gateway.continuity_wake import load_wake_state_for_session
    seed_store()
    mgr = make_manager(tmp_path)
    state = mgr.create_session(cwd=".")
    db = mgr._get_db()

    original = db.set_session_wake_packet

    def _boom(*args, **kwargs):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(db, "set_session_wake_packet", _boom)
    mgr.ensure_wake_for_prompt(state, "acpproj status please")
    assert state.agent.ephemeral_system_prompt is None
    assert state.wake_pending is False
    assert load_wake_state_for_session(
        db, state.session_id) == ("pending", None)

    monkeypatch.setattr(db, "set_session_wake_packet", original)
    mgr.ensure_wake_for_prompt(state, "acpproj status please")
    assert state.agent.ephemeral_system_prompt is None
    assert state.wake_pending is False


# -- re-review blocker 4: restore-time unavailable must rearm, never consume ----

def test_restore_unavailable_read_never_consumes_pending_with_empty_evidence(
        tmp_path, monkeypatch):
    """A transient wake-record read failure during restore must NOT fall
    through to a restore-time bind attempt: that attempt runs with
    first_message="" and, once storage recovers mid-attempt, consumes the
    durable pending sentinel with EMPTY evidence — the user's real first
    message can never activate a project. Restore must rearm instead."""
    from gateway.continuity_wake import (load_wake_state_for_session,
                                         wake_state_from_json)
    seed_store()
    mgr = make_manager(tmp_path)
    session_id = mgr.create_session(cwd=".").session_id
    raw = mgr._get_db().get_session_wake_packet(session_id)
    assert wake_state_from_json(raw) == ("pending", None)

    # Restart; the restore-time wake read fails exactly once, then storage
    # recovers (the mid-attempt window the finding describes).
    mgr2 = make_manager(tmp_path)
    db2 = mgr2._get_db()
    original = db2.get_session_wake_packet
    calls = {"n": 0}

    def _fail_once(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("disk read failed")
        return original(*args, **kwargs)

    monkeypatch.setattr(db2, "get_session_wake_packet", _fail_once)
    restored = mgr2.get_session(session_id)
    assert restored is not None
    # No restore-time consume: still pending, still armed, no packet built
    # from empty evidence.
    assert restored.wake_pending is True
    assert restored.agent.ephemeral_system_prompt is None
    assert load_wake_state_for_session(db2, session_id) == ("pending", None)

    # The REAL first prompt binds once, with that message as evidence.
    mgr2.ensure_wake_for_prompt(restored, "acpproj status please")
    text = restored.agent.ephemeral_system_prompt
    assert text and "Active project: ACP project" in text
    assert restored.wake_pending is False


def test_restore_unavailable_read_rearms_instead_of_stranding(tmp_path,
                                                              monkeypatch):
    """If the wake read stays down through the whole restore, the retryable
    outcome must survive as an armed marker: the first prompt after storage
    recovers still gets the session's one bind attempt. Discarding it
    strands durable pending until yet another restart."""
    from gateway.continuity_wake import wake_state_from_json
    seed_store()
    mgr = make_manager(tmp_path)
    session_id = mgr.create_session(cwd=".").session_id

    mgr2 = make_manager(tmp_path)
    db2 = mgr2._get_db()
    original = db2.get_session_wake_packet

    def _boom(*args, **kwargs):
        raise RuntimeError("disk read failed")

    monkeypatch.setattr(db2, "get_session_wake_packet", _boom)
    restored = mgr2.get_session(session_id)
    assert restored is not None
    assert restored.wake_pending is True

    monkeypatch.setattr(db2, "get_session_wake_packet", original)
    mgr2.ensure_wake_for_prompt(restored, "acpproj status please")
    text = restored.agent.ephemeral_system_prompt
    assert text and "Active project: ACP project" in text
    binding = json.loads(db2.get_session_wake_packet(session_id))
    assert binding["text"] in text
    assert restored.wake_pending is False


def test_restore_unavailable_history_bearing_session_never_binds(tmp_path,
                                                                 monkeypatch):
    """The rearm must not widen eligibility: a history-bearing transcript
    with NO durable wake record whose restore-time read failed transiently
    still settles as absent at the first prompt — the deferred retry
    attests nothing, so only a durable pending sentinel can bind it."""
    seed_store()  # a bind WOULD produce a packet if allowed
    mgr = make_manager(tmp_path)
    db = mgr._get_db()
    db.create_session(session_id="legacy-acp-2", source="acp")
    db.append_message(session_id="legacy-acp-2", role="user",
                      content="old question")
    db.append_message(session_id="legacy-acp-2", role="assistant",
                      content="old answer")

    original = db.get_session_wake_packet

    def _boom(*args, **kwargs):
        raise RuntimeError("disk read failed")

    monkeypatch.setattr(db, "get_session_wake_packet", _boom)
    restored = mgr.get_session("legacy-acp-2")
    assert restored is not None

    monkeypatch.setattr(db, "get_session_wake_packet", original)
    mgr.ensure_wake_for_prompt(restored, "acpproj status please")
    assert restored.agent.ephemeral_system_prompt is None
    assert db.get_session_wake_packet("legacy-acp-2") is None


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
