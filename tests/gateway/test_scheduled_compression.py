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
from gateway.platforms.base import BasePlatformAdapter, SendResult
from gateway.platforms.event import MessageEvent, MessageType
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


def test_a_later_cycle_never_resummarizes_a_conversation_with_nothing_new(live):
    """A new date is not new conversation. A later cycle makes no model call while nothing new was
    said, even after the cycle's own brief came back through the real cron delivery mirror and its
    own rewrite replaced the history; a genuine new exchange reopens it."""
    from cron.scheduler_delivery import _maybe_mirror_cron_delivery

    config, store, _home = live
    entry = _seed(store, _source(), _history())
    runner = _runner(config, store)
    job = {"id": "dc3", "name": "dream-cycle-v3-live-nagatha"}

    with _real_compression(runner) as llm:
        first = _run_cycle(runner, cycle_id="2026-10-01")
        _maybe_mirror_cron_delivery(job, "telegram", "c1", "🌙 **Nagatha · Dream Cycle**\n\nbrief",
                                    user_id="u1", enabled=True)
        second = _run_cycle(runner, cycle_id="2026-10-02")
        live_id = store.lookup_by_session_key(entry.session_key).session_id
        assert any("[Cron delivery:" in str(m.get("content")) for m in store.load_transcript(live_id))
        store.append_to_transcript(live_id, {"role": "user", "content": "New ask: " + "draft " * 150})
        store.append_to_transcript(live_id, {"role": "assistant", "content": "Done: " + "text " * 150})
        # A retry of the cycle that already compressed: not compressed again, but what was said since
        # is new context, so it must not answer with the fingerprint its marker recorded.
        retry = _run_cycle(runner, cycle_id="2026-10-01")
        third = _run_cycle(runner, cycle_id="2026-10-03")

    assert first["counts"] == {"applied": 1}, first
    assert second["counts"] == {"unchanged": 1}, second
    unchanged = second["sessions"][0]
    assert unchanged["activity_fingerprint"] == first["sessions"][0]["activity_fingerprint"]
    assert unchanged["highlights"]["open_items"]  # the standing handoff still travels
    assert retry["counts"] == {"already_applied": 1}, retry
    assert retry["sessions"][0]["activity_fingerprint"] != unchanged["activity_fingerprint"]
    assert third["sessions"][0]["status"] == "applied", third
    assert third["sessions"][0]["activity_fingerprint"] != unchanged["activity_fingerprint"]
    assert llm.call_count == 2


def test_a_dry_run_is_a_read_only_poll_that_never_holds_the_conversation(live):
    """The nightly poll asks "anything new?" before deciding to run a cycle. Answering must not take
    the conversation from the user: a dry run reports new talk even mid-turn, reports a quiet
    conversation with the fingerprint the cycle gates on, and never calls the model."""
    config, store, _home = live
    entry = _seed(store, _source(), _history())
    runner = _runner(config, store)

    with _real_compression(runner) as llm:
        _run_cycle(runner, cycle_id="2026-10-01")
        quiet = _run_cycle(runner, cycle_id="2026-10-02", dry_run=True)["sessions"][0]
        live_id = store.lookup_by_session_key(entry.session_key).session_id
        store.append_to_transcript(live_id, {"role": "user", "content": "One more thing"})
        runner._session_state(entry.session_key).turn.agent = MagicMock()  # a turn is running
        busy = _run_cycle(runner, cycle_id="2026-10-02", dry_run=True)["sessions"][0]

    assert quiet["status"] == "unchanged" and len(quiet["activity_fingerprint"]) == 64
    assert busy["status"] == "eligible", busy
    assert llm.call_count == 1


def test_a_request_that_read_a_stale_marker_rechecks_once_it_holds_the_conversation(live):
    """Two requests for one conversation in one cycle: the second read the marker before the first
    stamped it. Holding the reservation, it must see that commit and not summarize again."""
    config, store, _home = live
    _seed(store, _source(), _history())
    runner = _runner(config, store)
    real = runner.async_session_store.get_session_metadata
    reads = []

    async def stale_first_read(*args, **kwargs):
        reads.append(args)
        return None if len(reads) == 1 else await real(*args, **kwargs)

    with _real_compression(runner) as llm:
        first = _run_cycle(runner)
        with patch.object(runner.async_session_store, "get_session_metadata", stale_first_read):
            second = _run_cycle(runner)

    assert first["counts"] == {"applied": 1}, first
    assert second["counts"] == {"already_applied": 1}, second
    assert llm.call_count == 1


def test_only_codes_the_user_stated_stay_established_in_the_persisted_handoff(live):
    """The scheduled summarizer writes ``## Daily Threads``; a P#-group/task code the user did not
    approve in a sentence of their own is rewritten as proposed before the handoff is persisted or
    highlighted. Writing the code is not approving it: a quoted example, a question or a refusal
    names a code without agreeing to it.

    The mirrored brief is not the user's words even where replay merges it into the user rows around
    it, and the user's reply typed straight after the brief still is."""
    config, store, _home = live
    history = _history()
    history[-2:-1] = [
        {"role": "user", "content": "Use exactly the agreed two-line format. Example only, not "
                                    "assigned metadata:\n\n🟠 P1-integrations/envelope-grok\n"
                                    "🔐 Approve authenticated remote access for Grok\n\n"
                                    "Should P3-ops/backup be tracked? Don't use P2-ops/disk.\n"
                                    "'Use P2-ops/logs.' was the old template's wording.\n"
                                    "Please don’t track P3-ops/fonts."},
        {"role": "user", "content": "Track this as P1-startups/expatriator. " + "x " * 200},
        {"role": "user", "content": "[Cron delivery: dream-cycle-v3-live-nagatha]\n"
                                    "🌙 **Nagatha · Dream Cycle**\n\n**Proposed** · not agreed yet\n"
                                    "• P0-career/jobs/invented\n▶️ Prepare the interview answers"},
        {"role": "user", "content": "Agreed: P2-health/exercise. " + "y " * 50},
    ]
    entry = _seed(store, _source(), history)
    runner = _runner(config, store)
    summary = FIXTURE_SUMMARY + """
## Daily Threads
- P1-startups/expatriator — Portal API live; next: fix the onboarding gap
  - waiting externally: Madrid portal support reply, check 7 Oct
- P0-career/jobs/invented — the summarizer made this up; next: nothing
- P2-health/exercise — no plan yet; next: ask for a weekly slot
- P1-integrations/envelope-grok — copied from the example; next: approve remote access
- P3-ops/backup — asked about; next: decide whether to track it
- P2-ops/disk — refused; next: measure the temp folders
- P2-ops/logs — single-quoted example; next: rotate the logs
- P3-ops/fonts — refused with a typographic apostrophe; next: prune the fonts
- unassigned: Disk cleanup — 244 GiB free; next: measure the temp folders
"""

    with _real_compression(runner) as llm:
        llm.return_value = summary
        row = _run_cycle(runner)["sessions"][0]

    assert "## Daily Threads" in llm.call_args.args[0]
    handoff = "\n".join(str(m.get("content")) for m in
                        store.load_transcript(store.lookup_by_session_key(entry.session_key).session_id)
                        if is_compaction_summary_message(m))
    assert "- P1-startups/expatriator — Portal API live" in handoff
    assert "- proposed P0-career/jobs/invented —" in handoff
    standing = {t.get("code") or t.get("title"): t["standing"]
                for t in row["highlights"]["daily_threads"]["threads"]}
    assert standing == {"P1-startups/expatriator": "established",
                        "P0-career/jobs/invented": "proposed",
                        "P2-health/exercise": "established",
                        "P1-integrations/envelope-grok": "proposed", "P3-ops/backup": "proposed",
                        "P2-ops/disk": "proposed", "P2-ops/logs": "proposed",
                        "P3-ops/fonts": "proposed", "Disk cleanup": "uncoded"}
    waiting = row["highlights"]["daily_threads"]["threads"][0]["dependency"]
    assert waiting == {"kind": "waiting_externally", "detail": "Madrid portal support reply, check 7 Oct"}


def test_a_handoff_the_scheduled_path_never_verified_establishes_no_code(live):
    """A ``## Daily Threads`` heading is not provenance. A handoff written by a manual /compress, a
    plugin engine or a cycle from before codes were verified can show codes as established; until the
    user approves one, the brief gets it as proposed, whether the conversation is unchanged or the
    next scheduled summary carries the section forward."""
    from agent.context_compressor import LEGACY_SUMMARY_PREFIX
    from gateway.scheduled_compression import activity_fingerprint

    config, store, _home = live
    carried = FIXTURE_SUMMARY + """
## Daily Threads
- P1-legacy/carried — written before verification; next: keep the old priority
"""
    # The handoff such a compression left behind, and the marker a cycle from before verification
    # stamped beside it: applied and fingerprinted, with no record of verified codes.
    entry = _seed(store, _source(),
                  [{"role": "user", "content": f"{LEGACY_SUMMARY_PREFIX} {carried}"}] + _history())
    store.set_session_metadata(entry.session_key, CYCLE_METADATA_KEY, {
        "cycle_id": "2026-09-30", "status": "applied", "activity_fingerprint": activity_fingerprint(
            store.load_transcript(entry.session_id, repair_alternation=False))})
    runner = _runner(config, store)

    with _real_compression(runner) as llm:
        llm.return_value = carried
        quiet = _run_cycle(runner, cycle_id="2026-10-01")["sessions"][0]
        live_id = store.lookup_by_session_key(entry.session_key).session_id
        for turn in _history(20):
            store.append_to_transcript(live_id, turn)
        recompressed = _run_cycle(runner, cycle_id="2026-10-02")["sessions"][0]

    assert quiet["status"] == "unchanged", quiet
    assert recompressed["status"] == "applied", recompressed
    for row in (quiet, recompressed):
        assert [t["standing"] for t in row["highlights"]["daily_threads"]["threads"]] == ["proposed"]


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


def test_brief_sections_each_come_from_one_heading_plus_the_retained_final_reply():
    summary = FIXTURE_SUMMARY.replace(
        "- Waiting on the maintainer's review of the control verb",
        "- **Active task:** Wire the brief into the nightly job\n- Branch: fix/brief",
    ).replace(
        "- Live activation still needs the owner's authority",
        "- Mail tool cannot start without a token:\n  - probe exited 1\n- Disk keeps filling",
    )
    messages = [{"role": "user", "content": "hello"},
                {"role": "assistant", "content": summary, "display_kind": "compaction_summary"},
                {"role": "user", "content": "enable it"},
                {"role": "assistant", "content": "Mail tool verified. Connector pending. Extra."},
                {"role": "assistant", "content": "", "tool_calls": [{"id": "t1"}]}]
    with patch("agent.context_compressor.is_compaction_summary_message",
               side_effect=lambda m: isinstance(m, dict) and m.get("display_kind") == "compaction_summary"):
        highlights = handoff_highlights(messages)

    assert highlights["active_task"] == ["Wire the brief into the nightly job"]
    # Top-level items only: the nested probe line is detail of the item above it.
    assert highlights["open_items"] == ["Mail tool cannot start without a token", "Disk keeps filling"]
    assert highlights["corrections"][0].startswith("Receipt claimed compression")
    # The newest assistant TEXT after the handoff (a tool-call stub has none), two sentences.
    assert highlights["latest_reply"] == ["Mail tool verified. Connector pending."]


def test_scheduled_summary_sees_the_retained_tail_and_manual_compress_does_not(live):
    """The tail keeps the final exchange verbatim and wins over the summary, so a nightly handoff
    whose state sections never saw it reports blockers that exchange resolved. The scheduled run's
    single summarizer call carries the tail as reference; a manual /compress is unchanged; the tail
    itself stays verbatim either way."""
    from agent.conversation_compression_manual import parse_compress_args

    config, store, _home = live
    history = _history()
    history[-1] = {"role": "assistant", "content": "RESOLVED-IN-FINAL-EXCHANGE: token installed."}
    nightly = _seed(store, _source(chat_id="nightly"), list(history))
    manual = _seed(store, _source(chat_id="manual"), list(history))
    runner = _runner(config, store)

    with _real_compression(runner) as llm:
        assert _run_cycle(runner, session_keys=[nightly.session_key])["counts"] == {"applied": 1}
        nightly_prompt = llm.call_args.args[0]
        asyncio.run(runner._run_manual_compression(manual.origin, manual, list(history),
                                                   parse_compress_args("")))
        manual_prompt = llm.call_args.args[0]

    reference = nightly_prompt.split("NEWER TURNS KEPT VERBATIM AFTER THIS SUMMARY", 1)
    assert len(reference) == 2 and "RESOLVED-IN-FINAL-EXCHANGE" in reference[1]
    assert "NEWER TURNS KEPT VERBATIM" not in manual_prompt
    assert "RESOLVED-IN-FINAL-EXCHANGE" not in manual_prompt
    continuation = store.load_transcript(store.lookup_by_session_key(nightly.session_key).session_id)
    assert continuation[-1]["content"] == "RESOLVED-IN-FINAL-EXCHANGE: token installed."


def test_an_echoed_tail_reference_never_reaches_the_persisted_handoff(live):
    """The reference rides on top of the bounded middle with its own small budget, keeping the END
    of the tail; a summarizer that echoes it back gets the echo cut before the handoff is persisted
    or kept as the previous summary the next update builds on."""
    import agent.context_compressor_summary as summary_mixin

    config, store, _home = live
    history = _history()
    history[-2] = {"role": "user", "content": "EARLY-TAIL " + "x" * 2_000}
    history[-1] = {"role": "assistant", "content": "RESOLVED-IN-FINAL-EXCHANGE: token installed."}
    entry = _seed(store, _source(), list(history))
    runner = _runner(config, store)
    seen = {}

    def _echoing_summarizer(prompt, *_a, **_k):
        seen["prompt"] = prompt
        echo = prompt[prompt.index("NEWER TURNS KEPT VERBATIM"):]
        return FIXTURE_SUMMARY + "\n\n" + echo

    with _real_compression(runner) as llm, patch.object(summary_mixin, "_RETAINED_TAIL_MAX_CHARS", 300):
        llm.side_effect = _echoing_summarizer
        assert _run_cycle(runner, session_keys=[entry.session_key])["counts"] == {"applied": 1}

    reference = seen["prompt"].split("<retained-tail-reference>", 1)[1].split("</retained-tail-reference>")[0]
    assert "RESOLVED-IN-FINAL-EXCHANGE" in reference and "EARLY-TAIL" not in reference
    assert len(reference) < 300 + 100
    continuation = store.load_transcript(store.lookup_by_session_key(entry.session_key).session_id)
    handoff = "\n".join(str(m.get("content")) for m in continuation[:-2])
    assert FIXTURE_SUMMARY.splitlines()[0] in handoff
    assert "retained-tail-reference" not in handoff and "NEWER TURNS KEPT VERBATIM" not in handoff
    assert "RESOLVED-IN-FINAL-EXCHANGE" not in handoff
    assert continuation[-1]["content"] == "RESOLVED-IN-FINAL-EXCHANGE: token installed."


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


# ---------------------------------------------------------------------------
# The reservation is released by its OWN generation, and inbound PARKS on it
# ---------------------------------------------------------------------------

class _NoTransportAdapter(BasePlatformAdapter):  # type: ignore[misc]
    """The shipped adapter dispatch (guard, background task, in-band drain) with no network."""

    async def connect(self, *, is_reconnect: bool = False):
        pass

    async def disconnect(self):
        pass

    async def get_chat_info(self, chat_id):
        return None

    async def send(self, *args, **kwargs):
        self.sent.append(args[1] if len(args) > 1 else kwargs.get("content", kwargs.get("text")))
        return SendResult(success=True, message_id="m1")


def _no_transport_adapter() -> "_NoTransportAdapter":
    """A real ``BasePlatformAdapter`` without its heavy ``__init__`` — the dispatch path under test
    (``_start_session_processing`` → ``_process_message_background`` → in-band drain) is the shipped
    one; only the wire is absent."""
    adapter = object.__new__(_NoTransportAdapter)
    adapter.config = PlatformConfig(enabled=True, token="***", typing_indicator=False)
    adapter.platform = Platform.TELEGRAM
    adapter.sent = []
    adapter._message_handler = None
    adapter._busy_session_handler = None
    adapter._active_sessions = {}
    adapter._pending_messages = {}
    adapter._session_tasks = {}
    adapter._background_tasks = set()
    adapter._post_delivery_callbacks = {}
    adapter._expected_cancelled_tasks = set()
    adapter._fatal_error_code = adapter._fatal_error_message = adapter._fatal_error_handler = None
    adapter._fatal_error_retryable = True
    adapter._running = True
    adapter._busy_text_mode = "queue"
    adapter._busy_text_debounce_seconds = 0.0
    adapter._busy_text_hard_cap_seconds = 0.5
    adapter._text_debounce = {}
    adapter._auto_tts_default = False
    adapter._auto_tts_enabled_chats = set()
    adapter._auto_tts_disabled_chats = set()
    adapter._typing_paused = set()
    adapter._streaming_tts_completed_turns = set()
    return adapter


def _wire_inbound(runner, adapter, dispatches: list, turns: list):
    """Point the adapter at the REAL ``_handle_message``. Two seams are fixtures: admission (auth /
    hooks / profile routing, upstream of the busy gate) and the agent turn itself."""
    async def _admit(event):
        dispatches.append(event.text)
        return event, event.source, False

    async def _turn(event, _source, _key, _generation):
        turns.append(event.text)
        return "answered"

    runner._hm_admit_event = _admit
    runner._handle_message_with_agent = _turn
    runner.adapters = {Platform.TELEGRAM: adapter}
    adapter._message_handler = runner._handle_message
    return adapter


async def _await_until(predicate, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.02)
    return False


async def _stop_session(runner, source: SessionSource, session_key: str) -> None:
    """The real /stop funnel both stop handlers use (``slash_commands._handle_stop_command`` →
    ``_interrupt_and_clear_session``) for a sentinel-held slot."""
    from gateway.run import _INTERRUPT_REASON_STOP

    await runner._interrupt_and_clear_session(
        session_key, source, interrupt_reason=_INTERRUPT_REASON_STOP,
        invalidation_reason="stop_command_pending")


def _profile_home(tmp_path, monkeypatch, *, extra_config: str = ""):
    """A profile home for PROFILE, optionally with extra compression config."""
    home = tmp_path / ".hermes" / "profiles" / PROFILE
    home.mkdir(parents=True)
    (home / "config.yaml").write_text(_TEST_CONFIG + extra_config)
    monkeypatch.setenv("HERMES_HOME", str(home))
    config = GatewayConfig(platforms={Platform.TELEGRAM: PlatformConfig(enabled=True, token="***")})
    return config, SessionStore(sessions_dir=home / "sessions", config=config)


@pytest.mark.parametrize("extra_config, rotates", [("", False), ("  in_place: false\n", True)])
def test_stop_midflight_keeps_the_mutation_lease_until_the_worker_stops(
        tmp_path, monkeypatch, extra_config, rotates):
    """``/stop`` lands while the compression is parked inside its summarizer.

    It may free the UI/admission slot — that is what /stop is for, and the successor turn claims it.
    What it must NOT do is hand the successor the canonical mutation lease: the summarization runs on
    an executor thread that no interrupt or cancellation stops, and it still archives (in place) or
    rotates the very transcript the successor would load. So the successor must be fenced out of the
    [read history → commit] region until the worker has really finished — and the compression's tail
    must still touch only its OWN slot and lease.
    """
    from gateway.turn_lease import TurnLeaseTimeoutError

    config, store = _profile_home(tmp_path, monkeypatch, extra_config=extra_config)
    entry = _seed(store, _source(), _history())
    runner = _runner(config, store)
    started, release = threading.Event(), threading.Event()

    async def _scenario():
        task = asyncio.create_task(run_scheduled_compression(
            runner, profile=PROFILE, cycle_id=CYCLE))
        assert await asyncio.to_thread(started.wait, 20), "compression never reached the summarizer"
        await _stop_session(runner, _source(), entry.session_key)
        assert runner._is_session_running(entry.session_key) is False, "/stop did not free the slot"

        # The successor turn, claimed exactly as ``_handle_message`` does once the slot came free.
        state = runner._session_state(entry.session_key)
        state.turn.agent = MagicMock()
        state.turn.started_ts = time.time()
        successor = runner._begin_session_run_generation(entry.session_key)
        # The real turn region starts HERE (``run_turn.py::_hmwa_acquire_turn_lease``, before the
        # transcript load). While the worker lives it must fail closed.
        fenced = False
        try:
            await runner._turn_leases.acquire(entry.session_id, owner_key=entry.session_key,
                                              generation=successor, timeout=0.3)
        except TurnLeaseTimeoutError:
            fenced = True
        release.set()
        result = await task
        # Worker done: the successor may now enter, on whichever id the conversation ended up on.
        live_id = store.lookup_by_session_key(entry.session_key).session_id
        state.turn.lease_tokens[successor] = await runner._turn_leases.acquire(
            live_id, owner_key=entry.session_key, generation=successor, timeout=2.0)
        return successor, fenced, result

    with _real_compression(runner) as llm:
        llm.side_effect = _barrier_summary(started, release)
        successor, fenced, result = asyncio.run(_scenario())

    assert fenced is True, (
        "the successor turn entered the history-read/commit region while the compression worker "
        "could still mutate the transcript")
    state = runner._peek_session_state(entry.session_key)
    assert runner._is_session_running(entry.session_key) is True, \
        "the compression's tail cleared the successor turn's slot"
    assert state.turn.agent is not None
    assert successor in state.turn.lease_tokens, "the successor's lease token was stolen"
    assert result["counts"] == {"applied": 1}, result
    assert result["sessions"][0]["rotated"] is rotates


def test_a_stale_lease_acquisition_does_not_compress(tmp_path, monkeypatch):
    """The lease acquisition AWAITS under contention, and /stop can invalidate the reservation in
    that window. Winning the lease afterwards must not start a compression on behalf of a
    reservation that no longer owns the conversation: it defers before reading any history."""
    config, store = _profile_home(tmp_path, monkeypatch)
    history = _history()
    entry = _seed(store, _source(), history)
    runner = _runner(config, store)

    async def _scenario():
        # A second routing key resolving this same session_id holds the lease (the switch_session
        # many-to-one case): the slot is free, so the reservation is claimed and then WAITS.
        alias = await runner._turn_leases.acquire(
            entry.session_id, owner_key="agent:main:alias", generation=1, timeout=1.0)
        task = asyncio.create_task(run_scheduled_compression(
            runner, profile=PROFILE, cycle_id=CYCLE))
        assert await _await_until(
            lambda: runner._turn_leases._leases[entry.session_id].pending_acquires > 0), \
            "the cycle never reached the contended lease"
        # /stop while it waits, then the holder lets go so the stale acquisition wins the lease.
        await _stop_session(runner, _source(), entry.session_key)
        runner._turn_leases.release(alias)
        return await task

    with _real_compression(runner) as llm:
        result = asyncio.run(_scenario())

    row = result["sessions"][0]
    assert row["status"] == "deferred" and row["reason"] == "reservation_invalidated", row
    llm.assert_not_called()
    assert store.lookup_by_session_key(entry.session_key).session_id == entry.session_id
    assert len(store.load_transcript(entry.session_id)) == len(history)
    assert store.get_session_metadata(entry.session_key, CYCLE_METADATA_KEY) is None
    # The lease it briefly won is given back, so the next real turn is not fenced out forever.
    assert runner._turn_leases._leases[entry.session_id].holder is None


def test_cancelling_the_cycle_holds_the_lease_until_the_worker_really_stops(tmp_path, monkeypatch):
    """Cancelling the awaiting coroutine does not stop the summarization — it runs on an executor
    thread. So the mutation lease must stay held past the cancellation and be given back only when the
    worker (and the writes it performs) has actually finished."""
    from gateway.turn_lease import TurnLeaseTimeoutError

    config, store = _profile_home(tmp_path, monkeypatch)
    entry = _seed(store, _source(), _history())
    runner = _runner(config, store)
    started, release = threading.Event(), threading.Event()

    async def _scenario():
        task = asyncio.create_task(run_scheduled_compression(
            runner, profile=PROFILE, cycle_id=CYCLE))
        assert await asyncio.to_thread(started.wait, 20), "compression never reached the summarizer"
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        fenced = False
        try:
            await runner._turn_leases.acquire(entry.session_id, owner_key="agent:main:alias",
                                              generation=99, timeout=0.3)
        except TurnLeaseTimeoutError:
            fenced = True
        release.set()
        freed = await _await_until(
            lambda: runner._turn_leases._leases[entry.session_id].holder is None, timeout=20.0)
        return fenced, freed

    with _real_compression(runner) as llm:
        llm.side_effect = _barrier_summary(started, release)
        fenced, freed = asyncio.run(_scenario())

    assert fenced is True, "the lease was released while the compression worker could still write"
    assert freed is True, "the lease was never given back after the worker finished"
    # The worker was never interrupted, so its rewrite still landed.
    continuation = store.load_transcript(
        store.lookup_by_session_key(entry.session_key).session_id)
    assert any(is_compaction_summary_message(m) for m in continuation)


@pytest.mark.parametrize("text, runs_turn", [("anything still open?", True),
                                              ("/not-a-gateway-command keep going", False)])
def test_inbound_parks_on_the_reservation_instead_of_redispatching(live, text, runs_turn):
    """One message arriving mid-reservation, through the REAL adapter dispatch. Queueing it was the
    defect: a reservation has no turn behind it, so the adapter's in-band drain re-dispatched the same
    event in a tight loop. It must park once and then be handled exactly once. A slash prompt the
    gateway does not register (a skill, a typo) is not a command the busy path dispatches, so it
    parks like text and only afterwards gets its ordinary handling (an unknown one is answered with
    the unknown-command notice); only commands the busy path dispatches itself skip the park."""
    config, store, _home = live
    entry = _seed(store, _source(), _history())
    runner = _runner(config, store)
    dispatches, turns = [], []
    adapter = _wire_inbound(runner, _no_transport_adapter(), dispatches, turns)
    started, release = threading.Event(), threading.Event()

    async def _scenario():
        task = asyncio.create_task(run_scheduled_compression(
            runner, profile=PROFILE, cycle_id=CYCLE))
        assert await asyncio.to_thread(started.wait, 20), "compression never reached the summarizer"
        event = MessageEvent(text=text, message_type=MessageType.TEXT, source=_source())
        assert adapter._start_session_processing(event, entry.session_key) is True
        await asyncio.sleep(0.25)  # long enough for a redispatch loop to show thousands of passes
        parked = {"dispatches": len(dispatches), "turns": list(turns),
                  "pending": dict(adapter._pending_messages)}
        release.set()
        result = await task
        assert await _await_until(lambda: bool(turns) or bool(adapter.sent)), \
            "the parked message was never handled"
        await asyncio.gather(*list(adapter._background_tasks), return_exceptions=True)
        return parked, result

    with _real_compression(runner) as llm:
        llm.side_effect = _barrier_summary(started, release)
        parked, result = asyncio.run(_scenario())

    assert parked["dispatches"] == 1, \
        f"the message was re-dispatched {parked['dispatches']} times during the reservation"
    assert parked["turns"] == [], "a turn ran against the transcript being compressed"
    assert parked["pending"] == {}, "the event was queued into a slot nothing would drain"
    # Released: the same inbound task handles the message once, nothing duplicated.
    assert dispatches == [text]
    if runs_turn:
        assert turns == [text]
    else:
        assert turns == [] and any("Unknown command" in str(sent) for sent in adapter.sent)
    assert result["counts"] == {"applied": 1}, result


def test_a_finishing_turn_that_still_holds_its_lease_is_not_superseded(live):
    """A turn frees its running slot when the agent returns (``_run_agent``'s guarded release) but
    keeps its turn lease until the outer handler has shaped, delivered and persisted the reply. A
    reservation claimed in that gap would bump the run generation, so the turn would discard its own
    reply as stale, and the turn's unconditional cleanup would then clear the reservation's slot. A
    held turn lease therefore means busy."""
    config, store, _home = live
    entry = _seed(store, _source(), _history())
    runner = _runner(config, store)

    async def _scenario():
        generation = runner._begin_session_run_generation(entry.session_key)
        token = await runner._turn_leases.acquire(
            entry.session_id, owner_key=entry.session_key, generation=generation, timeout=1.0)
        runner._session_state(entry.session_key).turn.lease_tokens[generation] = token
        assert runner._is_session_running(entry.session_key) is False
        return generation, await run_scheduled_compression(runner, profile=PROFILE, cycle_id=CYCLE)

    with _real_compression(runner) as llm:
        generation, result = asyncio.run(_scenario())
    assert not llm.called, "the compression summarized a conversation whose turn was still finishing"
    assert result["counts"] == {"deferred": 1}, result
    assert result["sessions"][0]["reason"] == "turn_running"
    assert runner._is_session_run_current(entry.session_key, generation), \
        "the reservation superseded the finishing turn's generation"


def test_stop_during_a_reservation_wakes_the_parked_inbound_at_once(live):
    """/stop is the escape hatch: it clears the slot, so the park must end on the release — not on
    its timeout — and the message must then run."""
    config, store, _home = live
    entry = _seed(store, _source(), _history())
    runner = _runner(config, store)
    dispatches, turns = [], []
    adapter = _wire_inbound(runner, _no_transport_adapter(), dispatches, turns)
    started, release = threading.Event(), threading.Event()

    async def _scenario():
        task = asyncio.create_task(run_scheduled_compression(
            runner, profile=PROFILE, cycle_id=CYCLE))
        assert await asyncio.to_thread(started.wait, 20), "compression never reached the summarizer"
        adapter._start_session_processing(
            MessageEvent(text="status?", message_type=MessageType.TEXT, source=_source()),
            entry.session_key)
        await asyncio.sleep(0.1)
        assert turns == []
        await _stop_session(runner, _source(), entry.session_key)
        woke = await _await_until(lambda: bool(turns), timeout=5.0)
        release.set()
        result = await task
        await asyncio.gather(*list(adapter._background_tasks), return_exceptions=True)
        return woke, result

    with _real_compression(runner) as llm:
        llm.side_effect = _barrier_summary(started, release)
        woke, result = asyncio.run(_scenario())

    assert woke is True, "the parked message waited past /stop clearing the reservation"
    assert turns == ["status?"] and dispatches == ["status?"]
    assert result["counts"] == {"applied": 1}, result


def test_cancelling_a_parked_inbound_does_not_wedge_the_compression(live):
    """The adapter's own cancellation (``cancel_session_processing``: shutdown, reset-like commands)
    must unwind a parked task without running its turn and without disturbing the compression."""
    config, store, _home = live
    entry = _seed(store, _source(), _history())
    runner = _runner(config, store)
    dispatches, turns = [], []
    adapter = _wire_inbound(runner, _no_transport_adapter(), dispatches, turns)
    started, release = threading.Event(), threading.Event()

    async def _scenario():
        task = asyncio.create_task(run_scheduled_compression(
            runner, profile=PROFILE, cycle_id=CYCLE))
        assert await asyncio.to_thread(started.wait, 20), "compression never reached the summarizer"
        adapter._start_session_processing(
            MessageEvent(text="never mind", message_type=MessageType.TEXT, source=_source()),
            entry.session_key)
        await asyncio.sleep(0.1)
        await asyncio.wait_for(
            adapter.cancel_session_processing(entry.session_key), timeout=5.0)
        release.set()
        return await task

    with _real_compression(runner) as llm:
        llm.side_effect = _barrier_summary(started, release)
        result = asyncio.run(_scenario())

    assert turns == [], "a cancelled park still ran its turn"
    assert dispatches == ["never mind"]
    assert result["counts"] == {"applied": 1}, result
    assert runner._is_session_running(entry.session_key) is False
