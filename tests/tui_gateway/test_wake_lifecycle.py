"""Phase 3 wake-packet lifecycle on the TUI surface.

The TUI prewarms agents before any prompt, so packet construction is
deferred to the first model-bound user prompt (_attach_wake_for_prompt):
- the real first message is the activation evidence (a prewarm-time bind
  with first_message="" could never activate a project);
- no durable state.db row is created for a session the user never speaks
  to (the documented no-empty-session behavior);
- resumed sessions reattach their original binding verbatim;
- /personality and /prompt rewrite the base ephemeral prompt WITHOUT
  dropping the wake binding (_compose_ephemeral_prompt).
"""
import json

import pytest

import hermes_state
from dream_cycle_v3.store import ContinuityStore
from hermes_constants import get_hermes_home
from hermes_state import SessionDB

NOW_ISO = "2026-07-11T08:00:00+00:00"


class FakeAgent:
    def __init__(self, db):
        self.ephemeral_system_prompt = None
        self._session_db = db


def seed_store(home=None):
    """Owned continuity store with one alias-activatable project under
    *home* (default: the hermetic, per-test HERMES_HOME)."""
    store_path = (home or get_hermes_home()) / "dream-cycle-v3" / "continuity.db"
    store_path.parent.mkdir(parents=True, exist_ok=True)
    with ContinuityStore(store_path) as store:
        store.migrate(NOW_ISO)
        store.upsert_project({
            "schema_version": 1, "project_id": "tui-proj",
            "canonical_name": "TUI project", "aliases": ["tuiproj"],
            "canonical_paths": [], "repositories": [], "status": "active",
            "owner": "default",
            "task_ssot": {"provider": "none", "locator": None,
                          "write_policy": "read_only"},
            "context_skill_id": None, "memory_policy": "warm_only",
            "sensitivity_policy": "normal", "retrieval_terms": [],
            "registry_version": 1,
            "last_verified_at": "2026-07-10T00:00:00+00:00",
        }, NOW_ISO)


def make_db(tmp_path, monkeypatch) -> SessionDB:
    monkeypatch.setattr(hermes_state, "DEFAULT_DB_PATH",
                        tmp_path / "state.db")
    return SessionDB(db_path=tmp_path / "state.db")


def make_session(**overrides) -> dict:
    """Session dict exactly as _start_agent_build leaves it: the deferred
    wake marker set, no binding performed."""
    base = {"session_key": "tui-sess-0001", "source": "tui", "cwd": None,
            "wake_pending": True, "wake_is_new_session": True}
    base.update(overrides)
    return base


def test_first_prompt_binds_with_message_evidence(tmp_path, monkeypatch):
    from tui_gateway.continuity import _attach_wake_for_prompt
    seed_store()
    db = make_db(tmp_path, monkeypatch)
    session = make_session()
    agent = FakeAgent(db)

    # Prewarm/build phase created NO durable row and NO binding.
    assert db.get_session("tui-sess-0001") is None

    _attach_wake_for_prompt(session, agent, "tuiproj status please")
    assert agent.ephemeral_system_prompt is not None
    assert "[Continuity wake packet" in agent.ephemeral_system_prompt
    # The real first message activated the project — impossible with the
    # old prewarm-time first_message="" bind.
    assert "Active project: TUI project" in agent.ephemeral_system_prompt
    assert agent._wake_packet_text in agent.ephemeral_system_prompt
    # Durable binding landed under the durable session id.
    raw = db.get_session_wake_packet("tui-sess-0001")
    assert raw and json.loads(raw)["text"] == agent._wake_packet_text

    # Marker consumed: a second prompt never rebinds or duplicates.
    before = agent.ephemeral_system_prompt
    _attach_wake_for_prompt(session, agent, "second message")
    assert agent.ephemeral_system_prompt == before
    assert "wake_pending" not in session


def test_no_binding_without_pending_marker(tmp_path, monkeypatch):
    from tui_gateway.continuity import _attach_wake_for_prompt
    seed_store()
    db = make_db(tmp_path, monkeypatch)
    session = make_session()
    session.pop("wake_pending")
    agent = FakeAgent(db)
    _attach_wake_for_prompt(session, agent, "tuiproj status please")
    assert agent.ephemeral_system_prompt is None
    assert db.get_session_wake_packet("tui-sess-0001") is None


def test_resumed_session_reattaches_original_binding(tmp_path, monkeypatch):
    from tui_gateway.continuity import _attach_wake_for_prompt
    seed_store()  # a store exists, but the resumed session must NOT rebuild
    db = make_db(tmp_path, monkeypatch)
    import hashlib
    original = "original resumed packet"
    db.create_session(session_id="resumed-0001", source="tui")
    db.set_session_wake_packet("resumed-0001", json.dumps({
        "schema_version": 1, "packet_id": "pkt-original",
        "content_hash": "sha256:"
        + hashlib.sha256(original.encode()).hexdigest(),
        "project_id": None, "text": original}))
    session = make_session(resume_session_id="resumed-0001",
                           wake_is_new_session=False)
    agent = FakeAgent(db)
    _attach_wake_for_prompt(session, agent, "tuiproj status please")
    assert agent.ephemeral_system_prompt == original


def test_titled_zero_message_row_binds_after_restart(tmp_path, monkeypatch):
    """/title persists a zero-message row before any prompt
    (_ensure_session_db_row), and a TUI restart then resumes that row —
    classified not-new. The durable pre-first-call pending sentinel written
    with the row keeps it bind-eligible: the first real prompt after the
    restart binds exactly once, with that message as evidence."""
    from gateway.continuity_wake import wake_state_from_json
    from tui_gateway.continuity import _attach_wake_for_prompt
    from tui_gateway.server import _ensure_session_db_row
    seed_store(tmp_path)
    db = make_db(tmp_path, monkeypatch)
    draft = make_session(profile_home=str(tmp_path))
    _ensure_session_db_row(draft)  # the session.title handler's row create
    db.set_session_title("tui-sess-0001", "planning draft")
    assert wake_state_from_json(
        db.get_session_wake_packet("tui-sess-0001")) == ("pending", None)

    # Restart: a fresh session dict resumes the row exactly as
    # _start_agent_build leaves it — any resumed row is not-new.
    resumed = make_session(profile_home=str(tmp_path),
                           resume_session_id="tui-sess-0001",
                           wake_is_new_session=False)
    agent = FakeAgent(db)
    _attach_wake_for_prompt(resumed, agent, "tuiproj status please")
    text = agent.ephemeral_system_prompt
    assert text and "Active project: TUI project" in text
    raw = db.get_session_wake_packet("tui-sess-0001")
    assert json.loads(raw)["text"] == agent._wake_packet_text

    # Exactly once: a second restart/resume reattaches, never rebinds.
    again = make_session(profile_home=str(tmp_path),
                         resume_session_id="tui-sess-0001",
                         wake_is_new_session=False)
    agent2 = FakeAgent(db)
    _attach_wake_for_prompt(again, agent2, "different message")
    assert agent2.ephemeral_system_prompt == agent._wake_packet_text


def test_history_bearing_resumed_row_never_becomes_eligible(tmp_path,
                                                            monkeypatch):
    """A row with real history and no durable wake state stays packetless
    across restart/resume: neither the resumed prompt path nor the row
    re-persist on prompt.submit may mark it pending or bind it."""
    from tui_gateway.continuity import _attach_wake_for_prompt
    from tui_gateway.server import _ensure_session_db_row
    seed_store(tmp_path)  # a rebuild WOULD produce a packet if allowed
    db = make_db(tmp_path, monkeypatch)
    db.create_session(session_id="legacy-0001", source="tui")
    db.append_message(session_id="legacy-0001", role="user",
                      content="old question")
    db.append_message(session_id="legacy-0001", role="assistant",
                      content="old answer")

    resumed = make_session(session_key="legacy-0001",
                           profile_home=str(tmp_path),
                           resume_session_id="legacy-0001",
                           wake_is_new_session=False)
    _ensure_session_db_row(resumed)  # prompt.submit re-persist is a no-op
    agent = FakeAgent(db)
    _attach_wake_for_prompt(resumed, agent, "tuiproj status please")
    assert agent.ephemeral_system_prompt is None
    assert db.get_session_wake_packet("legacy-0001") is None


def create_session_rpc(monkeypatch, db, params=None) -> dict:
    """Create a session through the REAL session.create RPC handler (agent
    build and cap enforcement stubbed — neither touches wake state) and
    return the live session dict exactly as the handler left it."""
    import tui_gateway.server as server
    monkeypatch.setattr(server, "_schedule_agent_build", lambda *a, **k: None)
    monkeypatch.setattr(server, "_schedule_session_cap_enforcement",
                        lambda: None)
    monkeypatch.setattr(server, "_get_db", lambda: db)
    resp = server.handle_request({"id": "wt-1", "method": "session.create",
                                  "params": {"cols": 80, **(params or {})}})
    sid = resp["result"]["session_id"]
    return server._sessions.pop(sid)


SEEDED_STRING_MESSAGES = [
    {"role": "user", "content": "earlier question"},
    {"role": "assistant", "content": "earlier answer"},
]

SEEDED_MULTIMODAL_MESSAGES = [
    {"role": "user", "content": [
        {"type": "text", "text": "earlier multimodal question"},
        {"type": "image_url", "image_url": {"url": "https://x/img.png"}}]},
    {"role": "user", "content": [
        {"type": "image_url", "image_url": {"url": "https://x/img2.png"}}]},
]


def test_seeded_string_history_session_is_never_rearmed(tmp_path, monkeypatch):
    """session.create with seeded string history: the verdict is not-new at
    the source of truth, the deferred build never re-widens it, the row
    persist writes NO pending sentinel, and the first prompt never binds."""
    from tui_gateway.continuity import _arm_deferred_wake, _attach_wake_for_prompt
    from tui_gateway.server import _ensure_session_db_row
    seed_store()  # a bind WOULD produce a packet if allowed
    db = make_db(tmp_path, monkeypatch)
    session = create_session_rpc(monkeypatch, db,
                                 {"messages": SEEDED_STRING_MESSAGES})
    key = session["session_key"]
    assert session["wake_is_new_session"] is False

    _arm_deferred_wake(session)  # the deferred agent build's marker step
    assert session["wake_is_new_session"] is False

    _ensure_session_db_row(session)  # /title-style pre-prompt row create
    assert db.get_session(key) is not None
    assert db.get_session_wake_packet(key) is None  # no pending sentinel

    agent = FakeAgent(db)
    _attach_wake_for_prompt(session, agent, "tuiproj status please")
    assert agent.ephemeral_system_prompt is None
    assert db.get_session_wake_packet(key) is None


def test_seeded_multimodal_history_session_is_never_rearmed(tmp_path,
                                                            monkeypatch):
    """Multimodal seed entries (list-of-parts content, including image-only)
    coerce to no displayable text — the verdict must key off the PRESENCE of
    the entries, not the truthiness of their coerced text."""
    from tui_gateway.continuity import _arm_deferred_wake, _attach_wake_for_prompt
    from tui_gateway.server import _ensure_session_db_row
    seed_store()
    db = make_db(tmp_path, monkeypatch)
    session = create_session_rpc(monkeypatch, db,
                                 {"messages": SEEDED_MULTIMODAL_MESSAGES})
    key = session["session_key"]
    # The display coercion keeps no string transcript for these shapes —
    # exactly the case a text-truthiness check would misread as empty.
    assert session["history"] == []
    assert session["wake_is_new_session"] is False

    _arm_deferred_wake(session)
    assert session["wake_is_new_session"] is False

    _ensure_session_db_row(session)
    assert db.get_session(key) is not None
    assert db.get_session_wake_packet(key) is None

    agent = FakeAgent(db)
    _attach_wake_for_prompt(session, agent, "tuiproj status please")
    assert agent.ephemeral_system_prompt is None
    assert db.get_session_wake_packet(key) is None


@pytest.mark.parametrize("params", [{"messages": []}, {"messages": None}, {}],
                         ids=["empty-list", "null", "absent"])
def test_empty_history_session_create_stays_eligible(tmp_path, monkeypatch,
                                                     params):
    """A genuinely empty, parentless session.create draft — empty list, null,
    or absent messages — is still wake-new: the row persist writes the pending
    sentinel and the first prompt binds with message evidence."""
    from gateway.continuity_wake import wake_state_from_json
    from tui_gateway.continuity import _arm_deferred_wake, _attach_wake_for_prompt
    from tui_gateway.server import _ensure_session_db_row
    seed_store()
    db = make_db(tmp_path, monkeypatch)
    session = create_session_rpc(monkeypatch, db, params)
    key = session["session_key"]
    assert session["wake_is_new_session"] is True

    _arm_deferred_wake(session)
    assert session["wake_is_new_session"] is True

    _ensure_session_db_row(session)
    assert wake_state_from_json(
        db.get_session_wake_packet(key)) == ("pending", None)

    agent = FakeAgent(db)
    _attach_wake_for_prompt(session, agent, "tuiproj status please")
    assert agent.ephemeral_system_prompt is not None
    assert "Active project: TUI project" in agent.ephemeral_system_prompt
    assert json.loads(db.get_session_wake_packet(key))["text"] \
        == agent._wake_packet_text


def test_zero_message_titled_rpc_session_binds_after_restart(tmp_path,
                                                             monkeypatch):
    """The /title-then-restart flow stays eligible end-to-end from the real
    session.create: zero-message draft → titled row (pending sentinel) →
    restart-resume classified not-new → first real prompt binds once."""
    from gateway.continuity_wake import wake_state_from_json
    from tui_gateway.continuity import _arm_deferred_wake, _attach_wake_for_prompt
    from tui_gateway.server import _ensure_session_db_row
    seed_store()
    db = make_db(tmp_path, monkeypatch)
    draft = create_session_rpc(monkeypatch, db)
    key = draft["session_key"]
    _arm_deferred_wake(draft)
    _ensure_session_db_row(draft)
    db.set_session_title(key, "planning draft")
    assert wake_state_from_json(
        db.get_session_wake_packet(key)) == ("pending", None)

    resumed = make_session(session_key=key, resume_session_id=key,
                           wake_is_new_session=False)
    _arm_deferred_wake(resumed)
    agent = FakeAgent(db)
    _attach_wake_for_prompt(resumed, agent, "tuiproj status please")
    text = agent.ephemeral_system_prompt
    assert text and "Active project: TUI project" in text
    assert json.loads(db.get_session_wake_packet(key))["text"] \
        == agent._wake_packet_text


def test_seeded_session_restart_remains_ineligible(tmp_path, monkeypatch):
    """A seeded session whose row was persisted pre-prompt (no sentinel)
    stays packetless after a restart-resume: nothing on the resumed path
    may mark it pending or bind it."""
    from tui_gateway.continuity import _arm_deferred_wake, _attach_wake_for_prompt
    from tui_gateway.server import _ensure_session_db_row
    seed_store()
    db = make_db(tmp_path, monkeypatch)
    session = create_session_rpc(monkeypatch, db,
                                 {"messages": SEEDED_STRING_MESSAGES})
    key = session["session_key"]
    _arm_deferred_wake(session)
    _ensure_session_db_row(session)
    assert db.get_session_wake_packet(key) is None

    # Restart: resume the row exactly as _start_agent_build leaves it.
    resumed = make_session(session_key=key, resume_session_id=key,
                           wake_is_new_session=False)
    _arm_deferred_wake(resumed)
    _ensure_session_db_row(resumed)  # prompt.submit re-persist is a no-op
    agent = FakeAgent(db)
    _attach_wake_for_prompt(resumed, agent, "tuiproj status please")
    assert agent.ephemeral_system_prompt is None
    assert db.get_session_wake_packet(key) is None


EMPTY_CONTENT_SEED_CASES = {
    "empty-string": [{"role": "user", "content": ""}],
    "whitespace": [{"role": "assistant", "content": "  \n\t "}],
    "empty-list-content": [{"role": "user", "content": []}],
    "null-content": [{"role": "system", "content": None}],
    "missing-content": [{"role": "user"}],
    "image-only": [{"role": "user", "content": [
        {"type": "image_url", "image_url": {"url": "https://x/img.png"}}]}],
    "malformed-dict": [{"kind": "note", "body": "entry without a role"}],
    "non-dict-entry": ["bare string entry"],
}


@pytest.mark.parametrize("messages", list(EMPTY_CONTENT_SEED_CASES.values()),
                         ids=list(EMPTY_CONTENT_SEED_CASES))
def test_seed_presence_is_row_based(messages):
    """The seededness contract is row PRESENCE: any entry in a seeded
    messages list — a supported role row regardless of content, or an
    unsupported/malformed entry (fail closed) — is history."""
    from tui_gateway.continuity import _seed_history_present
    assert _seed_history_present(messages) is True


def test_seed_presence_only_absent_or_empty_is_eligible():
    from tui_gateway.continuity import _seed_history_present
    assert _seed_history_present(None) is False
    assert _seed_history_present([]) is False
    # A non-list messages param is malformed, never a zero-message draft.
    assert _seed_history_present("not-a-list") is True


@pytest.mark.parametrize("messages", list(EMPTY_CONTENT_SEED_CASES.values()),
                         ids=list(EMPTY_CONTENT_SEED_CASES))
def test_empty_content_seed_rows_never_rearm(tmp_path, monkeypatch, messages):
    """Seeded rows with empty/whitespace/null/empty-container/image-only
    content — and malformed entries, which fail closed — make the session
    history-bearing: verdict not-new at create, no pending sentinel on row
    persist, no bind at the first prompt, and still ineligible after a
    restart-resume."""
    from tui_gateway.continuity import _arm_deferred_wake, _attach_wake_for_prompt
    from tui_gateway.server import _ensure_session_db_row
    seed_store()  # a bind WOULD produce a packet if allowed
    db = make_db(tmp_path, monkeypatch)
    session = create_session_rpc(monkeypatch, db, {"messages": messages})
    key = session["session_key"]
    assert session["wake_is_new_session"] is False

    _arm_deferred_wake(session)  # the deferred agent build never re-widens
    assert session["wake_is_new_session"] is False

    _ensure_session_db_row(session)  # /title-style pre-prompt row create
    assert db.get_session(key) is not None
    assert db.get_session_wake_packet(key) is None  # no pending sentinel

    agent = FakeAgent(db)
    _attach_wake_for_prompt(session, agent, "tuiproj status please")
    assert agent.ephemeral_system_prompt is None
    assert db.get_session_wake_packet(key) is None

    # Restart: resume the row exactly as _start_agent_build leaves it —
    # nothing on the resumed path may mark it pending or bind it.
    resumed = make_session(session_key=key, resume_session_id=key,
                           wake_is_new_session=False)
    _arm_deferred_wake(resumed)
    _ensure_session_db_row(resumed)  # prompt.submit re-persist is a no-op
    agent2 = FakeAgent(db)
    _attach_wake_for_prompt(resumed, agent2, "tuiproj status please")
    assert agent2.ephemeral_system_prompt is None
    assert db.get_session_wake_packet(key) is None


def test_personality_and_prompt_preserve_wake(tmp_path, monkeypatch):
    from tui_gateway.continuity import _compose_ephemeral_prompt
    db = make_db(tmp_path, monkeypatch)
    agent = FakeAgent(db)
    agent._wake_packet_text = "[Continuity wake packet — test]"
    agent.ephemeral_system_prompt = agent._wake_packet_text

    # /personality-style full rewrite keeps the wake binding.
    _compose_ephemeral_prompt(agent, "You are a pirate.")
    assert agent.ephemeral_system_prompt == (
        "You are a pirate.\n\n[Continuity wake packet — test]")
    # /prompt reset-to-config with an empty prompt keeps the wake alone.
    _compose_ephemeral_prompt(agent, None)
    assert agent.ephemeral_system_prompt == "[Continuity wake packet — test]"
    # And an agent with no wake behaves exactly as before.
    plain = FakeAgent(db)
    _compose_ephemeral_prompt(plain, "base only")
    assert plain.ephemeral_system_prompt == "base only"
    _compose_ephemeral_prompt(plain, None)
    assert plain.ephemeral_system_prompt is None


# -- post-verification finding 5: pre-model validation must precede wake --------

def test_blocked_context_reference_prompt_never_consumes_wake(tmp_path,
                                                              monkeypatch):
    """A prompt refused by context-reference validation never reaches the
    model — it must not consume the session's one wake attempt nor persist
    a durable outcome; the next (valid) prompt still binds."""
    import sys
    import threading as _threading
    import types
    import tui_gateway.server as server
    seed_store()
    db = make_db(tmp_path, monkeypatch)

    class _Agent:
        model = "test/model"
        base_url = ""
        api_key = ""

        def __init__(self):
            self.ephemeral_system_prompt = None
            self._session_db = db

        def run_conversation(self, prompt, conversation_history=None,
                             stream_callback=None, **kwargs):
            raise AssertionError("blocked prompt must never reach the model")

    class _ImmediateThread:
        def __init__(self, target=None, daemon=None):
            self._target = target

        def start(self):
            self._target()

    fake_ctx = types.ModuleType("agent.context_references")
    fake_ctx.preprocess_context_references = (
        lambda message, **kwargs: types.SimpleNamespace(
            blocked=True, message="", warnings=["Context injection refused."],
            references=[], injected_tokens=0))
    fake_meta = types.ModuleType("agent.model_metadata")
    fake_meta.get_model_context_length = lambda *args, **kwargs: 100000

    session = make_session()
    session.update({"agent": _Agent(), "history": [],
                    "history_lock": _threading.Lock(), "history_version": 0,
                    "running": False, "attached_images": [], "cols": 80,
                    "slash_worker": None, "show_reasoning": False,
                    "tool_progress_mode": "all"})
    server._sessions["wake-blocked-sid"] = session
    try:
        monkeypatch.setattr(server.threading, "Thread", _ImmediateThread)
        monkeypatch.setattr(server, "_emit", lambda *a, **k: None)
        monkeypatch.setattr(server, "make_stream_renderer", lambda cols: None)
        monkeypatch.setattr(server, "_wire_callbacks", lambda sid: None)
        monkeypatch.setattr(server, "_sync_agent_model_with_config",
                            lambda sid, s: None)
        monkeypatch.setitem(sys.modules, "agent.context_references", fake_ctx)
        monkeypatch.setitem(sys.modules, "agent.model_metadata", fake_meta)

        server.handle_request({"id": "1", "method": "prompt.submit",
                               "params": {"session_id": "wake-blocked-sid",
                                          "text": "@blocked ref"}})
    finally:
        server._sessions.pop("wake-blocked-sid", None)

    agent = session["agent"]
    assert agent.ephemeral_system_prompt is None
    assert session.get("wake_pending") is True
    # No settled outcome persisted: the row persist may write the
    # pre-first-call pending sentinel (still eligible), never a binding or
    # attempted-none built from the rejected prompt.
    from gateway.continuity_wake import wake_state_from_json
    state, _ = wake_state_from_json(
        db.get_session_wake_packet("tui-sess-0001"))
    assert state in ("absent", "pending")


# -- post-verification finding 8: failed attempts keep the deferral marker ------

def test_failed_attempt_never_retries_after_model_bound_turn(tmp_path, monkeypatch):
    """A failed wake read must not mutate prompt bytes on a later turn."""
    from tui_gateway.continuity import _attach_wake_for_prompt
    from tui_gateway.server import _ensure_session_db_row
    seed_store()
    db = make_db(tmp_path, monkeypatch)
    session = make_session()
    _ensure_session_db_row(session)   # durable pending sentinel written
    agent = FakeAgent(db)

    original = db.set_session_wake_packet

    def _boom(*args, **kwargs):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(db, "set_session_wake_packet", _boom)
    _attach_wake_for_prompt(session, agent, "tuiproj status please")
    assert agent.ephemeral_system_prompt is None
    assert "wake_pending" not in session

    monkeypatch.setattr(db, "set_session_wake_packet", original)
    _attach_wake_for_prompt(session, agent, "tuiproj status please")
    assert agent.ephemeral_system_prompt is None
    assert "wake_pending" not in session


def test_history_bearing_pending_record_settles_without_late_binding(tmp_path,
                                                                     monkeypatch):
    """A restart after an unavailable first turn must not bind into history."""
    from gateway.continuity_wake import wake_state_from_json
    from tui_gateway.continuity import _attach_wake_for_prompt
    seed_store()
    db = make_db(tmp_path, monkeypatch)
    session = make_session(history=[{"role": "user", "content": "old"}],
                           wake_is_new_session=False)
    db.create_session(session_id=session["session_key"], source="tui")
    from gateway.continuity_wake import mark_wake_pending_for_session_id
    mark_wake_pending_for_session_id(db, session["session_key"])
    agent = FakeAgent(db)
    _attach_wake_for_prompt(session, agent, "tuiproj status please")
    assert agent.ephemeral_system_prompt is None
    assert "wake_pending" not in session
    assert wake_state_from_json(db.get_session_wake_packet(session["session_key"])) == (
        "none", None)


# -- post-verification finding 9: branch materializes the parent record ---------

def test_branch_materializes_parent_wake_record_on_child(tmp_path,
                                                         monkeypatch):
    """session.branch creates a child row with parent_session_id; the
    parent's durable wake record must be copied onto the child verbatim so
    inheritance never depends on an ever-growing parent-chain walk."""
    import hashlib
    import threading as _threading
    import types
    import tui_gateway.server as server
    db = make_db(tmp_path, monkeypatch)
    parent_key = "branch-parent-0001"
    db.create_session(session_id=parent_key, source="tui")
    raw = json.dumps({
        "schema_version": 1, "packet_id": "pkt-branch",
        "content_hash": "sha256:"
        + hashlib.sha256(b"branch packet").hexdigest(),
        "project_id": None, "text": "branch packet"})
    db.set_session_wake_packet(parent_key, raw)

    session = {"session_key": parent_key, "source": "tui", "cwd": None,
               "history": [{"role": "user", "content": "hi"}],
               "history_lock": _threading.Lock(), "cols": 80}
    server._sessions["branch-parent-sid"] = session
    try:
        monkeypatch.setattr(server, "_get_db", lambda: db)
        monkeypatch.setattr(server, "_claim_active_session_slot",
                            lambda *a, **k: (None, None))
        monkeypatch.setattr(server, "_new_session_key",
                            lambda: "branch-child-0001")
        monkeypatch.setattr(server, "_make_agent",
                            lambda *a, **k: types.SimpleNamespace())
        monkeypatch.setattr(server, "_init_session", lambda *a, **k: None)
        resp = server.handle_request({"id": "b1", "method": "session.branch",
                                      "params": {"session_id":
                                                 "branch-parent-sid"}})
        assert "result" in resp
    finally:
        server._sessions.pop("branch-parent-sid", None)

    assert db.get_session("branch-child-0001") is not None
    assert db.get_session_wake_packet("branch-child-0001") == raw


# -- re-review blocker 5: session.create(parent) materializes like branch -------

def _bound_record(text: str) -> str:
    import hashlib
    return json.dumps({
        "schema_version": 1, "packet_id": "pkt-" + text.replace(" ", "-"),
        "content_hash": "sha256:" + hashlib.sha256(text.encode()).hexdigest(),
        "project_id": None, "text": text})


def test_session_create_with_parent_materializes_wake_record(tmp_path,
                                                             monkeypatch):
    """The desktop branch path is session.create with parent_session_id
    (not the session.branch RPC): its row persist must copy the parent's
    durable wake record verbatim onto the child, exactly like the explicit
    branch and compression rotation do."""
    from tui_gateway.server import _ensure_session_db_row
    db = make_db(tmp_path, monkeypatch)
    db.create_session(session_id="desk-parent-0001", source="tui")
    raw = _bound_record("desktop branch packet")
    db.set_session_wake_packet("desk-parent-0001", raw)

    session = create_session_rpc(
        monkeypatch, db, {"parent_session_id": "desk-parent-0001",
                          "messages": SEEDED_STRING_MESSAGES})
    key = session["session_key"]
    # A branch child is never wake-new (existing verdict), so no pending
    # sentinel may be written — the parent's record is what it carries.
    assert session["wake_is_new_session"] is False

    _ensure_session_db_row(session)  # first prompt.submit persists the row
    row = db.get_session(key)
    assert row is not None and row["parent_session_id"] == "desk-parent-0001"
    assert db.get_session_wake_packet(key) == raw


def test_deep_session_create_branch_chain_keeps_binding(tmp_path,
                                                        monkeypatch):
    """Repeated desktop branches must not out-run the parent-chain walk
    limit: with per-child materialization the leaf of a 12-hop chain still
    resolves its inherited binding (the walk alone fails terminal
    'exhausted' past ten hops)."""
    import tui_gateway.server as server
    from gateway.continuity_wake import load_wake_state_for_session
    from tui_gateway.server import _ensure_session_db_row
    db = make_db(tmp_path, monkeypatch)
    monkeypatch.setattr(server, "_claim_active_session_slot",
                        lambda *a, **k: (None, None))
    db.create_session(session_id="deep-root", source="tui")
    raw = _bound_record("deep chain packet")
    db.set_session_wake_packet("deep-root", raw)

    prev = "deep-root"
    for _ in range(12):
        session = create_session_rpc(
            monkeypatch, db, {"parent_session_id": prev,
                              "messages": SEEDED_STRING_MESSAGES})
        _ensure_session_db_row(session)
        prev = session["session_key"]

    assert db.get_session_wake_packet(prev) == raw
    state, binding = load_wake_state_for_session(db, prev)
    assert state == "bound" and binding["text"] == "deep chain packet"


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
