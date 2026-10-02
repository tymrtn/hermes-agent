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
from datetime import datetime
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

    runner = object.__new__(GatewayRunner)
    runner.config = config
    runner.session_store = store
    runner._async_session_store = AsyncSessionStore(store)
    runner._session_db = _AsyncDbShim(store._db_for_session_id(""))
    runner._snapshot_running_agents = lambda: dict(running or {})
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
