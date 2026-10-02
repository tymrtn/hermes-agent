"""Real conversation compression, requested by the cycle and reported honestly.

The cycle does not compress anything itself: it asks the gateway that owns the session, over that
gateway's control socket, and records what the answer PROVES. These tests pin the honesty contract
(`claim` is `applied` only for rows carrying before/after evidence), the identity hygiene of the
report section, and the fact that a replay may never drive it.

The gateway side of the same path — scheduler -> real compressor -> persisted continuation -> next
turn — is proven against real imports in `tests/gateway/test_scheduled_compression.py`.
"""
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from .conftest import write_tree  # noqa: F401 (package-relative test helper)

from dream_cycle_v3.conversation_compression import (CLAIM_APPLIED, CLAIM_DEFERRED, CLAIM_FAILED,
                                                     CLAIM_NOTHING_ELIGIBLE, CLAIM_UNAVAILABLE,
                                                     classify_conversation_compression,
                                                     control_home_candidates,
                                                     conversation_compression_status,
                                                     request_conversation_compression)
from dream_cycle_v3.dry_run import SAMPLE_DATA, _build_sample_kanban_db
from dream_cycle_v3.errors import DreamCycleError
from dream_cycle_v3.runtime import RuntimeConfig, run_cycle

WINDOW_START = datetime(2026, 7, 11, 0, 0, tzinfo=timezone.utc)
WINDOW_END = datetime(2026, 7, 12, 0, 0, tzinfo=timezone.utc)
DATE = "2026-07-11"
AS_OF = "2026-07-12T00:30:00+00:00"
MTIME = int(datetime(2026, 7, 11, 12, 0, tzinfo=timezone.utc).timestamp())
PROFILE = "nagatha-test"
SESSION_KEY = "agent:main:telegram:dm:6493121275"
SESSION_REF = "a1b2c3d4e5f6"

SECRET_CANARY = "sk-canary12345678901234567890"


def _applied_row(**over):
    row = {
        "session_key": SESSION_KEY, "session_ref": SESSION_REF,
        "platform": "telegram", "chat_type": "dm", "status": "applied",
        "before_tokens": 121_000, "after_tokens": 38_000,
        "before_messages": 420, "after_messages": 31, "rotated": False,
        "highlights": {"open_threads": ["Live activation awaits the owner"],
                       "learnings": ["Drive the existing /compress path"]},
    }
    row.update(over)
    return row


def _answer(*rows, **over):
    return {"ok": True, "profile": PROFILE, "cycle_id": DATE, "eligible": len(rows),
            "counts": {}, "sessions": list(rows), **over}


# -- honesty: applied is earned, never assumed --------------------------------

def test_applied_requires_before_after_evidence():
    section = classify_conversation_compression(_answer(_applied_row()), profile=PROFILE,
                                                cycle_id=DATE)

    assert section["claim"] == CLAIM_APPLIED
    assert section["counts"] == {"applied": 1}
    assert section["tokens"] == {"before": 121_000, "after": 38_000}
    assert section["messages"] == {"before": 420, "after": 31}
    assert section["highlights"]["open_threads"] == ["Live activation awaits the owner"]


def test_status_word_without_evidence_is_not_an_applied_claim():
    """A gateway (or an older one) that says "applied" but carries no counts has proven nothing."""
    row = _applied_row(before_tokens=0, before_messages=0, after_messages=0)

    section = classify_conversation_compression(_answer(row), profile=PROFILE, cycle_id=DATE)

    assert section["claim"] != CLAIM_APPLIED
    assert section["counts"]["applied_without_evidence"] == 1
    assert section["tokens"] == {"before": 0, "after": 0}
    assert section["highlights"] == {}


def test_unreachable_gateway_is_unavailable_not_applied():
    section = classify_conversation_compression(None, profile=PROFILE, cycle_id=DATE,
                                               unavailable_reason="no socket at /tmp/x")

    assert section["claim"] == CLAIM_UNAVAILABLE
    assert section["requested"] is True and section["gateway_answered"] is False
    assert "no socket" in section["reason"]
    assert section["sessions"] == [] and section["counts"] == {}
    assert "tokens" not in section  # nothing was measured because nothing was attempted


def test_deferred_busy_session_is_reported_as_deferred():
    row = {"session_ref": SESSION_REF, "platform": "telegram", "chat_type": "dm",
           "status": "deferred", "reason": "turn_running"}

    section = classify_conversation_compression(_answer(row), profile=PROFILE, cycle_id=DATE)

    assert section["claim"] == CLAIM_DEFERRED
    assert section["sessions"][0]["reason"] == "turn_running"
    assert section["tokens"] == {"before": 0, "after": 0}


def test_pending_answer_never_claims_applied():
    section = classify_conversation_compression(
        _answer(pending=True), profile=PROFILE, cycle_id=DATE)

    assert section["claim"] == CLAIM_DEFERRED and section["pending"] is True
    assert "outcome unknown" in section["reason"]


def test_refusal_is_failed_with_the_gateways_reason():
    section = classify_conversation_compression(
        {"ok": False, "error": "this gateway does not serve profile 'other'"},
        profile=PROFILE, cycle_id=DATE)

    assert section["claim"] == CLAIM_FAILED
    assert "does not serve" in section["reason"]


def test_no_eligible_conversation_says_so():
    section = classify_conversation_compression(_answer(), profile=PROFILE, cycle_id=DATE)

    assert section["claim"] == CLAIM_NOTHING_ELIGIBLE


# -- the section carries evidence, not identities or secrets ------------------

def test_section_never_carries_the_session_key():
    section = classify_conversation_compression(_answer(_applied_row()), profile=PROFILE,
                                                cycle_id=DATE)

    assert SESSION_KEY not in json.dumps(section)
    assert section["sessions"][0]["session_ref"] == SESSION_REF


def test_highlights_pass_through_the_shared_sanitizers():
    row = _applied_row(highlights={"learnings": [f"token is {SECRET_CANARY}"],
                                   "open_threads": ["mail tyler@example.com about it"]})

    section = classify_conversation_compression(_answer(row), profile=PROFILE, cycle_id=DATE)

    blob = json.dumps(section)
    assert SECRET_CANARY not in blob
    assert "tyler@example.com" not in blob


# -- client plumbing ---------------------------------------------------------

def test_candidate_homes_try_the_profile_home_before_the_multiplexer():
    homes = control_home_candidates(PROFILE, home=Path("/h/.hermes"))

    assert homes == [Path("/h/.hermes/profiles") / PROFILE, Path("/h/.hermes")]


def test_explicit_gateway_home_wins():
    assert control_home_candidates(PROFILE, gateway_home="/explicit") == [Path("/explicit")]


def test_request_falls_through_to_the_next_home_and_stops_at_the_first_answer():
    asked = []

    def _client(home, **kwargs):
        asked.append(Path(home))
        return _answer(_applied_row()) if len(asked) == 2 else None

    section = request_conversation_compression(profile=PROFILE, cycle_id=DATE,
                                               home=Path("/h/.hermes"), client=_client)

    assert len(asked) == 2 and section["claim"] == CLAIM_APPLIED


def test_socket_error_is_recorded_not_raised():
    def _client(home, **kwargs):
        raise OSError("connection refused")

    section = request_conversation_compression(profile=PROFILE, cycle_id=DATE,
                                               home=Path("/h/.hermes"), client=_client)

    assert section["claim"] == CLAIM_UNAVAILABLE
    assert "connection refused" in section["reason"]


def test_status_token_reports_the_claim_and_its_counts():
    section = classify_conversation_compression(_answer(_applied_row()), profile=PROFILE,
                                                cycle_id=DATE)

    assert conversation_compression_status(section) == "applied[applied=1]"
    assert conversation_compression_status({"enabled": False}) == "off"
    assert conversation_compression_status(None) == "off"


# -- the cycle wiring --------------------------------------------------------

@pytest.fixture
def home(tmp_path, monkeypatch):
    root = tmp_path / "hermes-home"
    (root / "profiles").mkdir(parents=True)
    profile_home = root / "profiles" / PROFILE
    profile_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(root))
    return profile_home


@pytest.fixture
def env(home):
    sources = home / "sources" / "profile"
    sources.mkdir(parents=True)
    write_tree(sources, {"state/wake-up.md": "## Wake\n- carry the thread forward\n"}, mtime=MTIME)
    registry = home / "registry.json"
    registry.write_text((SAMPLE_DATA / "projects.json").read_text(encoding="utf-8"),
                        encoding="utf-8")
    threads = home / "threads.json"
    threads.write_text((SAMPLE_DATA / "threads.json").read_text(encoding="utf-8"),
                       encoding="utf-8")
    kanban_db = _build_sample_kanban_db(home / "trackers" / "kanban" / "kanban.db")
    return {"home": home, "sources": sources, "v3_root": home / "dream-cycle-v3",
            "registry": registry, "threads": threads, "kanban_db": kanban_db}


def make_config(env, **over):
    base = dict(profile=PROFILE, owner="nagatha", read_roots={"profile": env["sources"]},
                v3_root=env["v3_root"], mode="shadow", window_start=WINDOW_START,
                window_end=WINDOW_END, disposition_date=DATE, as_of=AS_OF,
                registry_path=env["registry"], threads_path=env["threads"],
                kanban_db=env["kanban_db"], kanban_board="sample-board")
    base.update(over)
    return RuntimeConfig(**base)


def test_cycle_is_opt_in_and_labels_the_two_compressions_apart(env):
    """Default off, and the status line never lets the continuity record pass for the
    conversation's own compression."""
    result = run_cycle(make_config(env))

    assert result.report["conversation_compression"] == {"enabled": False}
    assert "conversation=off" in result.status_line
    assert "continuity=" in result.status_line
    assert "compression=" not in result.status_line


def test_cycle_records_the_unreachable_gateway_rather_than_failing(env):
    """No gateway is listening under this temp home: the cycle still succeeds and says so."""
    result = run_cycle(make_config(env, conversation_compression=True,
                                   gateway_home=str(env["home"])))

    section = result.report["conversation_compression"]
    assert result.ok is True
    assert section["requested"] is True and section["claim"] == CLAIM_UNAVAILABLE
    assert "conversation=unavailable" in result.status_line


def test_replay_may_not_drive_conversation_compression(env):
    with pytest.raises(DreamCycleError, match="historical_replay"):
        make_config(env, conversation_compression=True, historical_replay=True)
