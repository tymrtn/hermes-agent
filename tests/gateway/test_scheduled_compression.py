"""Scheduled real compression of a live conversation (``compress-session`` control verb).

The chain under test is the real one: the control verb -> ``run_scheduled_compression`` ->
``GatewaySessionCommandsMixin._run_manual_compression`` -> ``compress_now`` -> a real
``AIAgent``/``ContextCompressor`` -> rotation into a new session in a real ``state.db`` -> the next
turn's ``load_transcript`` of the rotated id.

ONLY the model boundary is a fixture: ``ContextCompressor._call_summary_llm`` returns a canned
structured handoff. Everything else — prompt assembly, redaction, provenance validation, the
rotation, the store writes — is the shipped code. So these tests prove the PLUMBING end to end;
they do not and cannot prove summary quality, which needs real inference.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from agent.context_compressor import is_compaction_summary_message
from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.scheduled_compression import (CYCLE_METADATA_KEY, eligible_entries,
                                           handoff_highlights, key_namespace_for_profile,
                                           run_scheduled_compression, session_ref)
from gateway.session import AsyncSessionStore, SessionSource, SessionStore

CYCLE = "2026-10-01"
PROFILE = "nagatha"

# A structured handoff in the shape the shipped summary template asks for, so the real
# section/provenance handling runs over realistic input instead of a bare sentence.
FIXTURE_SUMMARY = """## Historical Task Snapshot
- Repair the nightly Dream Cycle so it really compresses

## Goal
Keep continuity across days without resetting the conversation.

## Constraints & Preferences
- Never reset the conversation; compress it
- Defer when a turn is running

## Completed Actions
1. READ daily_compression.py — bookkeeping only [tool: read_file]

## Active State
- Waiting on the maintainer's review of the control verb

## Blocked
- Live activation still needs the owner's authority

## Key Decisions
- Drive the existing /compress path from the owning gateway process
- No second summarizer, no reflection store

## Errors & Fixes
- Receipt claimed compression it had not done — now claims only applied rows

## Relevant Files
- gateway/scheduled_compression.py

## Critical Context
- Schedule stays 06:30 Europe/Madrid
"""


def _source(*, chat_id: str = "c1", profile: str | None = None) -> SessionSource:
    return SessionSource(platform=Platform.TELEGRAM, user_id="u1", chat_id=chat_id,
                         user_name="tester", chat_type="dm", profile=profile)


def _history(turns: int = 40) -> list[dict[str, str]]:
    """Long enough that the fixture handoff is a genuine reduction — the shipped compressor
    REFUSES a rewrite that would grow the transcript, so a toy history proves nothing."""
    out: list[dict[str, str]] = []
    for index in range(turns):
        out.append({"role": "user", "content": f"user turn {index}: " + "detail " * 120})
        out.append({"role": "assistant", "content": f"assistant turn {index}: " + "reply " * 120})
    return out


# `legacy` tail mode makes the verbatim tail a MESSAGE count instead of a slice of the context
# window, so the fixture transcript has a real summarizable middle. Without it the lean tail budget
# (2.5% of a 256K window) protects everything and the compressor correctly refuses a rewrite that
# would only add bytes.
_TEST_CONFIG = (
    "model:\n  context_length: 200000\n"
    "compression:\n  tail_mode: legacy\n  protect_first_n: 1\n  protect_last_n: 4\n")


def _home_for(tmp_path, monkeypatch, profile: str):
    """A profile home whose own label resolves to *profile* (``<root>/profiles/<name>``)."""
    home = tmp_path / ".hermes" / "profiles" / profile
    home.mkdir(parents=True)
    (home / "config.yaml").write_text(_TEST_CONFIG)
    monkeypatch.setenv("HERMES_HOME", str(home))
    return home


@pytest.fixture
def live(tmp_path, monkeypatch):
    """A real SessionStore + state.db under the nagatha profile home."""
    home = _home_for(tmp_path, monkeypatch, PROFILE)
    config = GatewayConfig(platforms={Platform.TELEGRAM: PlatformConfig(enabled=True, token="***")})
    store = SessionStore(sessions_dir=home / "sessions", config=config)
    return config, store, home


@pytest.fixture
def multiplexed(tmp_path, monkeypatch):
    """A multiplexer home serving several profiles out of one store."""
    home = tmp_path / ".hermes"
    home.mkdir(parents=True)
    (home / "config.yaml").write_text(_TEST_CONFIG)
    monkeypatch.setenv("HERMES_HOME", str(home))
    config = GatewayConfig(multiplex_profiles=True,
                           platforms={Platform.TELEGRAM: PlatformConfig(enabled=True, token="***")})
    store = SessionStore(sessions_dir=home / "sessions", config=config)
    return config, store, home


def _seed(store: SessionStore, source: SessionSource, history: list[dict]):
    entry = store.get_or_create_session(source)
    assert store.rewrite_transcript(entry.session_id, history)
    return entry


def _runner(config, store, *, running: dict | None = None):
    from gateway.run import GatewayRunner
    from gateway.turn_lease import SessionTurnLeaseRegistry

    runner = object.__new__(GatewayRunner)
    runner.config = config
    runner.session_store = store
    runner._async_session_store = AsyncSessionStore(store)
    runner._session_db = _AsyncDbShim(store._db_for_session_id(""))
    # The real per-session turn-slot map and lease registry: the scheduled compression reserves the
    # conversation through the same two mechanisms an inbound turn does, so stubbing them out would
    # test nothing about the serialization.
    runner._turn_leases = SessionTurnLeaseRegistry()
    for session_key, agent in (running or {}).items():
        state = runner._session_state(session_key)
        state.turn.agent = agent
        state.turn.started_ts = time.time()
    runner._session_has_compression_in_flight = _never_in_flight
    runner._evicted = []
    runner._evict_cached_agent = runner._evicted.append
    return runner


async def _never_in_flight(_session_key):
    return False


class _AsyncDbShim:
    """The runner holds an ASYNC SessionDB facade over the real sync db; mirror that shape."""

    def __init__(self, db):
        self._db = db

    async def get_session(self, session_id):
        return self._db.get_session(session_id)


@contextlib.contextmanager
def _real_compression(runner):
    """Run the REAL compression chain with only the summarizer LLM canned; yields that mock."""
    with contextlib.ExitStack() as stack:
        stack.enter_context(patch.dict(os.environ, {"OPENROUTER_API_KEY": "test-key"}))
        stack.enter_context(patch(
            "gateway.run._resolve_runtime_agent_kwargs",
            return_value={"api_key": "test-key", "base_url": "https://openrouter.ai/api/v1",
                          "skip_context_files": True}))
        stack.enter_context(patch("gateway.run._resolve_gateway_model", return_value="test/model"))
        stack.enter_context(patch.object(type(runner), "_sync_telegram_topic_binding",
                                        lambda *a, **k: None))
        yield stack.enter_context(patch(
            "agent.context_compressor.ContextCompressor._call_summary_llm",
            return_value=FIXTURE_SUMMARY))


def _run_cycle(runner, *, profile=PROFILE, cycle_id=CYCLE, **kwargs):
    return asyncio.run(run_scheduled_compression(runner, profile=profile, cycle_id=cycle_id,
                                                 **kwargs))


# ---------------------------------------------------------------------------
# Eligibility is a property of the session key, never of the process env
# ---------------------------------------------------------------------------

def test_multiplexed_eligibility_selects_only_the_cycles_own_profile(multiplexed):
    config, store, _home = multiplexed
    mine = _seed(store, _source(chat_id="mine", profile=PROFILE), _history(4))
    theirs = _seed(store, _source(chat_id="theirs", profile="other"), _history(4))
    runner = _runner(config, store)

    picked = eligible_entries(store.list_sessions(None),
                              namespace=key_namespace_for_profile(runner, PROFILE), limit=5)

    assert [e.session_key for e in picked] == [mine.session_key]
    assert theirs.session_key not in {e.session_key for e in picked}


def test_single_profile_gateway_refuses_another_profiles_cycle(live):
    """Its keys are the legacy ``agent:main``, so the namespace cannot discriminate: the request
    must name this gateway's own profile or nothing is touched."""
    config, store, _home = live
    entry = _seed(store, _source(), _history())
    runner = _runner(config, store)

    assert key_namespace_for_profile(runner, "someone-else") is None
    with patch("agent.context_compressor.ContextCompressor._call_summary_llm") as llm:
        result = _run_cycle(runner, profile="someone-else")

    assert result["ok"] is False and "does not serve" in result["error"]
    llm.assert_not_called()
    assert store.lookup_by_session_key(entry.session_key).session_id == entry.session_id


def test_entry_without_origin_is_not_eligible(live):
    _config, store, _home = live
    entry = _seed(store, _source(), _history(4))
    entry.origin = None

    assert eligible_entries([entry], namespace="agent:main") == []


# ---------------------------------------------------------------------------
# The real chain
# ---------------------------------------------------------------------------

def test_cycle_compresses_live_conversation_and_persists_the_continuation(live):
    """scheduler -> real compression -> persisted continuation -> next-turn consumption."""
    config, store, _home = live
    history = _history()
    entry = _seed(store, _source(), history)
    original_id = entry.session_id
    runner = _runner(config, store)

    with _real_compression(runner) as llm:
        result = _run_cycle(runner)

    assert result["counts"] == {"applied": 1}, result
    row = result["sessions"][0]
    assert row["status"] == "applied"
    assert row["before_messages"] == len(history) > row["after_messages"]
    assert row["before_tokens"] > 0 and row["after_tokens"] > 0
    # The digest handle is what leaves the gateway; the key itself is not in the report rows.
    assert row["session_ref"] == session_ref(entry.session_key)
    assert llm.call_count == 1  # exactly one summarization, not one per message

    # Next-turn consumption: loading the routing entry's transcript — what the next turn does —
    # now yields the compressed continuation carrying the handoff.
    live_entry = store.lookup_by_session_key(entry.session_key)
    assert live_entry.session_id == row["new_session_id"]
    continuation = store.load_transcript(live_entry.session_id)
    assert 0 < len(continuation) < len(history)
    assert any(is_compaction_summary_message(m) for m in continuation)
    # The live user ask survives the handoff: the last turn is still the user's own.
    assert continuation[-1]["role"] in {"user", "assistant"}
    assert original_id == live_entry.session_id  # default config compacts in place

    # The cached agent is evicted so the in-memory copy cannot diverge from the rewrite.
    assert entry.session_key in runner._evicted

    # Honest bullets, taken from the summary the compressor actually wrote.
    assert "Live activation still needs" in " ".join(row["highlights"]["open_threads"])
    assert any("existing /compress path" in b for b in row["highlights"]["learnings"])


def test_rotating_configuration_keeps_the_pre_compression_segment_readable(tmp_path, monkeypatch):
    """With ``compression.in_place: false`` the same cycle rotates onto a new session id and the
    original segment stays readable — no history is lost either way."""
    home = tmp_path / ".hermes" / "profiles" / PROFILE
    home.mkdir(parents=True)
    (home / "config.yaml").write_text(_TEST_CONFIG + "  in_place: false\n")
    monkeypatch.setenv("HERMES_HOME", str(home))
    config = GatewayConfig(platforms={Platform.TELEGRAM: PlatformConfig(enabled=True, token="***")})
    store = SessionStore(sessions_dir=home / "sessions", config=config)
    history = _history()
    entry = _seed(store, _source(), history)
    original_id = entry.session_id
    runner = _runner(config, store)

    with _real_compression(runner):
        result = _run_cycle(runner)

    row = result["sessions"][0]
    assert row["status"] == "applied" and row["rotated"] is True
    live_entry = store.lookup_by_session_key(entry.session_key)
    assert live_entry.session_id == row["new_session_id"] != original_id
    assert any(is_compaction_summary_message(m)
               for m in store.load_transcript(live_entry.session_id))


def test_rerun_in_the_same_cycle_does_not_compress_twice(live):
    config, store, _home = live
    entry = _seed(store, _source(), _history())
    runner = _runner(config, store)

    with _real_compression(runner) as llm:
        first = _run_cycle(runner)
        second_id = store.lookup_by_session_key(entry.session_key).session_id
        second = _run_cycle(runner)

    assert first["counts"] == {"applied": 1}
    assert second["counts"] == {"already_applied": 1}
    assert llm.call_count == 1
    # A later cycle is free to compress again.
    assert store.get_session_metadata(entry.session_key, CYCLE_METADATA_KEY)["cycle_id"] == CYCLE
    assert store.lookup_by_session_key(entry.session_key).session_id == second_id


def test_busy_conversation_defers_and_leaves_history_untouched(live):
    config, store, _home = live
    history = _history()
    entry = _seed(store, _source(), history)
    runner = _runner(config, store, running={entry.session_key: MagicMock()})

    with patch("agent.context_compressor.ContextCompressor._call_summary_llm") as llm:
        result = _run_cycle(runner)

    assert result["sessions"][0]["status"] == "deferred"
    assert result["sessions"][0]["reason"] == "turn_running"
    llm.assert_not_called()
    assert store.lookup_by_session_key(entry.session_key).session_id == entry.session_id
    assert len(store.load_transcript(entry.session_id)) == len(history)
    # Nothing is stamped, so the next attempt in the same cycle may still compress.
    assert store.get_session_metadata(entry.session_key, CYCLE_METADATA_KEY) is None


def test_in_flight_compression_defers(live):
    config, store, _home = live
    entry = _seed(store, _source(), _history())
    runner = _runner(config, store)

    async def _busy(_key):
        return True

    runner._session_has_compression_in_flight = _busy
    with patch("agent.context_compressor.ContextCompressor._call_summary_llm") as llm:
        result = _run_cycle(runner)

    assert result["sessions"][0]["reason"] == "compression_in_flight"
    llm.assert_not_called()
    assert len(store.load_transcript(entry.session_id)) == len(_history())


def test_short_conversation_defers_rather_than_burning_a_summary(live):
    config, store, _home = live
    _seed(store, _source(), _history(1))
    runner = _runner(config, store)

    with patch("agent.context_compressor.ContextCompressor._call_summary_llm") as llm:
        result = _run_cycle(runner)

    assert result["sessions"][0]["reason"] == "too_short"
    llm.assert_not_called()


def test_other_profiles_are_untouched_by_a_cycle(multiplexed):
    config, store, _home = multiplexed
    mine = _seed(store, _source(chat_id="mine", profile=PROFILE), _history())
    theirs = _seed(store, _source(chat_id="theirs", profile="other"), _history())
    runner = _runner(config, store)

    with _real_compression(runner):
        result = _run_cycle(runner, limit=5)

    assert [r["session_ref"] for r in result["sessions"]] == [session_ref(mine.session_key)]
    assert store.lookup_by_session_key(theirs.session_key).session_id == theirs.session_id
    assert store.get_session_metadata(theirs.session_key, CYCLE_METADATA_KEY) is None


# ---------------------------------------------------------------------------
# Highlights are a projection of the handoff, not of the transcript
# ---------------------------------------------------------------------------

def test_highlights_come_from_the_handoff_and_skip_empty_sections():
    messages = [{"role": "user", "content": "hello"},
                {"role": "assistant", "content": FIXTURE_SUMMARY.replace(
                    "- Live activation still needs the owner's authority", "None"),
                 "display_kind": "compaction_summary"}]
    with patch("agent.context_compressor.is_compaction_summary_message",
               side_effect=lambda m: isinstance(m, dict) and m.get("display_kind") == "compaction_summary"):
        highlights = handoff_highlights(messages)

    # ``## Blocked`` said None, so the next open-thread heading supplies the bullets.
    assert highlights["open_threads"] == ["Waiting on the maintainer's review of the control verb"]
    assert highlights["learnings"][0].startswith("Drive the existing /compress path")


def test_no_handoff_means_no_highlights():
    assert handoff_highlights([{"role": "user", "content": "plain"}]) == {}
    assert handoff_highlights(None) == {}


# ---------------------------------------------------------------------------
# The control-socket wire: the exact payload the nightly cycle sends
# ---------------------------------------------------------------------------

def test_control_verb_round_trips_the_cycle_over_the_real_wire(live):
    """One JSON line in, one JSON line out, through the shipped control-socket server."""
    import json

    from gateway.control_socket import CONTROL_PROTOCOL_VERSION, GatewayControlServer
    from gateway.scheduled_compression import compress_session_verb

    config, store, home = live
    entry = _seed(store, _source(), _history())
    runner = _runner(config, store)

    async def _serve():
        loop = asyncio.get_running_loop()
        server = GatewayControlServer(
            home, verb_handlers={"compress-session": compress_session_verb(runner, loop)})
        request = json.dumps({"verb": "compress-session", "id": 7,
                              "protocol": CONTROL_PROTOCOL_VERSION,
                              "params": {"profile": PROFILE, "cycle_id": CYCLE, "limit": 1}})
        return await loop.run_in_executor(
            None, server.handle_request_line, request.encode("utf-8"))

    with _real_compression(runner):
        raw = asyncio.run(_serve())

    response = json.loads(raw)
    assert response["ok"] is True and response["id"] == 7
    result = response["result"]
    assert result["profile"] == PROFILE and result["cycle_id"] == CYCLE
    assert result["counts"] == {"applied": 1}
    assert result["sessions"][0]["session_ref"] == session_ref(entry.session_key)


def test_control_verb_refuses_a_payload_without_profile_or_cycle(live):
    from gateway.scheduled_compression import compress_session_verb

    config, store, _home = live
    runner = _runner(config, store)

    async def _call(params):
        handler = compress_session_verb(runner, asyncio.get_running_loop())
        return await asyncio.get_running_loop().run_in_executor(None, handler, params)

    assert asyncio.run(_call({"profile": PROFILE}))["ok"] is False
    assert asyncio.run(_call({"cycle_id": CYCLE}))["ok"] is False


def test_origin_that_no_longer_reproduces_the_key_fails_closed(live):
    """The lock, the eviction and the topic re-bind all key off the SOURCE. An origin that derives
    a different key would compress this transcript while releasing another conversation's."""
    config, store, _home = live
    history = _history()
    entry = _seed(store, _source(), history)
    entry.origin = _source(chat_id="a-different-chat")
    runner = _runner(config, store)

    with patch("agent.context_compressor.ContextCompressor._call_summary_llm") as llm:
        result = asyncio.run(run_scheduled_compression(
            runner, profile=PROFILE, cycle_id=CYCLE, session_keys=[entry.session_key]))

    assert result["sessions"][0]["status"] == "failed"
    assert result["sessions"][0]["reason"] == "origin_key_mismatch"
    llm.assert_not_called()
    assert len(store.load_transcript(entry.session_id)) == len(history)


# ---------------------------------------------------------------------------
# Reservation: a message arriving mid-compression must not start a turn
# ---------------------------------------------------------------------------

def _barrier_summary(started: threading.Event, release: threading.Event):
    """Summarizer fixture that parks mid-compression so the test can inspect the live gateway."""
    def _call(*_args, **_kwargs):
        started.set()
        assert release.wait(20), "test never released the parked compression"
        return FIXTURE_SUMMARY
    return _call


async def _observe_midflight(runner, session_key: str, session_id: str,
                             started: threading.Event, release: threading.Event):
    """Run one cycle and, while the compressor is parked, probe what an inbound turn would see."""
    from gateway.run import _AGENT_PENDING_SENTINEL
    from gateway.scheduled_compression import _claim_turn_slot
    from gateway.turn_lease import TurnLeaseTimeoutError

    task = asyncio.create_task(run_scheduled_compression(runner, profile=PROFILE, cycle_id=CYCLE))
    assert await asyncio.to_thread(started.wait, 20), "compression never reached the summarizer"
    state = runner._peek_session_state(session_key)
    observed = {
        # The predicate the inbound admission gate itself uses (run_inbound: _is_session_running ->
        # _hm_handle_running_session_message), so a True here IS "the next message queues".
        "running": runner._is_session_running(session_key),
        # A pending sentinel (not a live agent) is what makes the busy path skip interrupt/steer and
        # merge the message for the next turn instead.
        "sentinel": (state.turn.agent if state else None) is _AGENT_PENDING_SENTINEL,
        "second_claim_refused": not _claim_turn_slot(runner, session_key),
    }
    try:
        await runner._turn_leases.acquire(session_id, owner_key="agent:main:alias",
                                         generation=99, timeout=0.05)
        observed["lease_blocked"] = False
    except TurnLeaseTimeoutError:
        observed["lease_blocked"] = True
    release.set()
    return observed, await task


def _assert_serialized(runner, session_key, observed, result):
    assert observed["running"] is True, "inbound admission would have started a turn mid-compression"
    assert observed["sentinel"] is True
    assert observed["second_claim_refused"] is True
    assert observed["lease_blocked"] is True, \
        "a second routing key on this session_id could have entered the turn region"
    assert result["counts"] == {"applied": 1}, result
    # The reservation is given back, so the next real message runs normally.
    assert runner._is_session_running(session_key) is False


def test_inbound_cannot_start_a_turn_while_in_place_compression_runs(live):
    config, store, _home = live
    entry = _seed(store, _source(), _history())
    runner = _runner(config, store)
    started, release = threading.Event(), threading.Event()

    with _real_compression(runner) as llm:
        llm.side_effect = _barrier_summary(started, release)
        observed, result = asyncio.run(_observe_midflight(
            runner, entry.session_key, entry.session_id, started, release))

    _assert_serialized(runner, entry.session_key, observed, result)


def test_inbound_cannot_start_a_turn_while_rotating_compression_runs(tmp_path, monkeypatch):
    """``compression.in_place: false`` CLOSES the parent session, so an unserialized concurrent turn
    flushing against it is the ``CompressionSessionClosedError`` the review reproduced."""
    home = tmp_path / ".hermes" / "profiles" / PROFILE
    home.mkdir(parents=True)
    (home / "config.yaml").write_text(_TEST_CONFIG + "  in_place: false\n")
    monkeypatch.setenv("HERMES_HOME", str(home))
    config = GatewayConfig(platforms={Platform.TELEGRAM: PlatformConfig(enabled=True, token="***")})
    store = SessionStore(sessions_dir=home / "sessions", config=config)
    entry = _seed(store, _source(), _history())
    runner = _runner(config, store)
    started, release = threading.Event(), threading.Event()

    with _real_compression(runner) as llm:
        llm.side_effect = _barrier_summary(started, release)
        observed, result = asyncio.run(_observe_midflight(
            runner, entry.session_key, entry.session_id, started, release))

    _assert_serialized(runner, entry.session_key, observed, result)
    assert result["sessions"][0]["rotated"] is True


# ---------------------------------------------------------------------------
# `applied` requires a committed rewrite, not a status word
# ---------------------------------------------------------------------------

def test_failed_persistence_is_not_applied_and_stays_retryable(live):
    """``compress_now`` answers ``compressed`` even when the compressor caught the persistence
    failure and restored the original transcript. That is not an applied compression."""
    config, store, _home = live
    history = _history()
    entry = _seed(store, _source(), history)
    runner = _runner(config, store)

    def _boom(*_args, **_kwargs):
        raise RuntimeError("archive_and_compact failed")

    with _real_compression(runner) as llm:
        with patch("hermes_state_messages.SessionMessagesMixin.archive_and_compact", _boom):
            result = _run_cycle(runner)
        row = result["sessions"][0]
        assert row["status"] == "failed", row
        assert row["reason"] == "rewrite_not_committed"
        # Nothing was rewritten: the whole transcript is still there under the same id.
        assert store.lookup_by_session_key(entry.session_key).session_id == entry.session_id
        assert len(store.load_transcript(entry.session_id)) == len(history)
        # The cycle is NOT stamped applied, so the same cycle may try again — and succeed.
        assert store.get_session_metadata(
            entry.session_key, CYCLE_METADATA_KEY)["status"] == "failed"
        retry = _run_cycle(runner)

    assert retry["counts"] == {"applied": 1}, retry
    assert llm.call_count == 2


# ---------------------------------------------------------------------------
# Standalone gateway + a hosted room that armed the process-wide secret guard
# ---------------------------------------------------------------------------

@contextlib.contextmanager
def _real_compression_with_scoped_credentials(runner, recorder: list):
    """Like ``_real_compression``, but the provider credential is read through the REAL
    ``get_secret`` at the moment the compression resolves its runtime kwargs.

    So the api_key that reaches the throwaway agent is whatever the bound profile scope resolves —
    and with no scope bound while the guard is armed, this read raises ``UnscopedSecretError``
    exactly as production would.
    """
    from agent.secret_scope import get_secret

    def _kwargs(*_args, **_kwargs):
        key = get_secret("OPENROUTER_API_KEY")
        recorder.append(key)
        return {"api_key": key, "base_url": "https://openrouter.ai/api/v1",
                "skip_context_files": True}

    with contextlib.ExitStack() as stack:
        stack.enter_context(patch("gateway.run._resolve_runtime_agent_kwargs", _kwargs))
        stack.enter_context(patch("gateway.run._resolve_gateway_model", return_value="test/model"))
        stack.enter_context(patch.object(type(runner), "_sync_telegram_topic_binding",
                                        lambda *a, **k: None))
        yield stack.enter_context(patch(
            "agent.context_compressor.ContextCompressor._call_summary_llm",
            return_value=FIXTURE_SUMMARY))


def _standalone_home(tmp_path, profile: str, *, api_key: str, extra_config: str = "") -> Path:
    home = tmp_path / ".hermes" / "profiles" / profile
    home.mkdir(parents=True)
    (home / "config.yaml").write_text(_TEST_CONFIG + extra_config)
    (home / ".env").write_text(f"OPENROUTER_API_KEY={api_key}\n", encoding="utf-8")
    return home


def test_standalone_gateway_compresses_after_a_hosted_room_armed_the_secret_guard(
        tmp_path, monkeypatch):
    """``multiplex_profiles`` is OFF, yet a native hosted room has flipped the process-wide guard, so
    every unscoped credential read fails closed. Scheduled compression must bind the launch
    profile's OWN scope (``run_turn.py::_standalone_launch_scope``) like every other standalone
    entry point — not skip binding because the config flag is off.

    Proven A → B → A across two launch homes: each cycle's credential must come from the home that
    launched it, so a frozen module-level or first-caller-wins value cannot pass.
    """
    from agent import secret_scope
    from agent.secret_scope import UnscopedSecretError, get_secret
    from tui_gateway import launch_profile_policy

    monkeypatch.setattr(secret_scope, "_MULTIPLEX_ACTIVE", False)
    monkeypatch.setattr(launch_profile_policy, "_snapshot", None)
    homes = {PROFILE: _standalone_home(tmp_path, PROFILE, api_key="key-for-nagatha"),
             "other": _standalone_home(tmp_path, "other", api_key="key-for-other")}

    def _cycle(profile: str, cycle_id: str = CYCLE) -> tuple[dict, list]:
        monkeypatch.setenv("HERMES_HOME", str(homes[profile]))
        config = GatewayConfig(multiplex_profiles=False, platforms={
            Platform.TELEGRAM: PlatformConfig(enabled=True, token="***")})
        store = SessionStore(sessions_dir=homes[profile] / "sessions", config=config)
        _seed(store, _source(chat_id=f"c-{profile}"), _history())
        runner = _runner(config, store)
        used: list[str] = []
        with _real_compression_with_scoped_credentials(runner, used):
            return _run_cycle(runner, profile=profile, cycle_id=cycle_id), used

    # The hosted room's real activation: unscoped reads fail closed from here on.
    monkeypatch.setenv("HERMES_HOME", str(homes[PROFILE]))
    launch_profile_policy.activate_multi_profile_hosting()
    assert secret_scope.is_multiplex_active()
    with pytest.raises(UnscopedSecretError):
        get_secret("OPENROUTER_API_KEY")

    first, first_keys = _cycle(PROFILE)
    second, second_keys = _cycle("other")
    # A fresh cycle id: the return to home A must really compress again, not read back the first
    # pass's idempotence marker.
    third, third_keys = _cycle(PROFILE, cycle_id="2026-10-02")

    for result in (first, second, third):
        assert result["counts"] == {"applied": 1}, result
    assert first_keys == third_keys == ["key-for-nagatha"]
    assert second_keys == ["key-for-other"]
    # The guard itself is never weakened by the operation.
    with pytest.raises(UnscopedSecretError):
        get_secret("OPENROUTER_API_KEY")
