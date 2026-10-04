"""The delivered Dream Cycle brief (dream_cycle_v3/daily_brief.py).

Contracts: the brief is orientation built only from the records the cycle wrote — never run
counters, paths, JSON or a run id; the compression line claims no more than the record proves;
nothing invents a thread ID or a priority; a handoff from a gateway that predates the brief sections
is labelled as unreconciled.
"""
import re

import pytest

from dream_cycle_v3.daily_brief import compression_receipt, render_daily_brief

RUN_ID = "0123456789abcdef0123456789abcdef"


def _report(**overrides):
    report = {
        "profile": "nagatha", "mode": "live", "cycle_date": "2026-10-03", "run_id": RUN_ID,
        "sources": 4, "excluded": 107,
        "carry_forward": {"threads": [
            {"thread_id": "reg-thread-a", "action": "continue",
             "state_before": "active", "state_after": "active"}]},
    }
    report.update(overrides)
    return report


def _applied(highlights):
    return {"enabled": True, "claim": "applied", "counts": {"applied": 1},
            "tokens": {"before": 128409, "after": 38122},
            "messages": {"before": 229, "after": 86},
            "sessions": [{"session_ref": "abc123", "status": "applied"}],
            "highlights": highlights}


STRUCTURED = {
    "active_task": ["Connect the mail tool to the desktop assistant and verify it"],
    "open_items": ["Desktop assistant still has no connector registered",
                   "Disk usage keeps creeping back up"],
    "latest_reply": ["The mail tool is authenticated and verified. The remaining decision is "
                     "which host serves the connector."],
    "corrections": ["User correction: one skill name, not three"],
    "open_threads": ["stale legacy bullet that must not render when sections exist"],
}


def test_applied_brief_is_orientation_not_machine_output():
    brief = render_daily_brief(_report(), _applied(STRUCTURED))

    assert brief.startswith("🌙 **Nagatha · Dream Cycle** · Sat 3 Oct")
    assert "Connect the mail tool to the desktop assistant" in brief
    assert "• Desktop assistant still has no connector registered" in brief
    assert "The mail tool is authenticated and verified." in brief
    assert "📝 **Corrections & fixes**" in brief
    assert brief.rstrip().endswith(
        "🗜️ **Compression** applied · 128,409 → 38,122 tokens · 229 → 86 messages")
    assert "stale legacy bullet" not in brief
    for machine in (RUN_ID, "sources=", "excluded=", "dream-cycle-v3", "/Users/", "{", "abc123"):
        assert machine not in brief


@pytest.mark.parametrize("conversation, expected", [
    (None, "not requested by this job"),
    ({"enabled": False}, "not requested by this job"),
    ({"enabled": True, "claim": "unavailable", "reason": "no gateway answered"},
     "unavailable — no gateway confirmed an outcome"),
    ({"enabled": True, "claim": "deferred",
      "sessions": [{"status": "deferred", "reason": "turn_running"}]},
     "deferred — a turn was running; nothing was rewritten"),
    ({"enabled": True, "claim": "deferred", "pending": True},
     "still running when the cycle stopped waiting; outcome unknown"),
    ({"enabled": True, "claim": "failed",
      "sessions": [{"status": "failed", "reason": "rewrite_not_committed"}]},
     "failed — the rewrite did not commit; nothing reported as applied"),
    ({"enabled": True, "claim": "already_applied"}, "already applied earlier in this cycle"),
    ({"enabled": True, "claim": "nothing_eligible"}, "no recent conversation was eligible"),
])
def test_receipt_claims_only_what_the_record_proves(conversation, expected):
    receipt = compression_receipt(conversation)
    assert expected in receipt
    assert "applied ·" not in receipt
    assert "nothing was rewritten" not in receipt or conversation.get("claim") == "deferred"


def test_handoff_sections_render_only_for_an_applied_compression():
    """A deferred attempt's leftover highlights are not tonight's handoff."""
    deferred = {**_applied(STRUCTURED), "claim": "deferred",
                "sessions": [{"status": "deferred", "reason": "turn_running"}]}
    brief = render_daily_brief(_report(), deferred)

    assert "No fresh handoff this cycle." in brief
    assert "Desktop assistant" not in brief and "**Now**" not in brief


def test_brief_never_invents_thread_ids_or_priorities():
    brief = render_daily_brief(_report(), _applied(STRUCTURED))

    assert not re.search(r"\bP\d\b|P\d-", brief)
    assert not any(dot in brief for dot in "🔴🟠🟡🔵")


def test_legacy_handoff_is_labelled_unreconciled_and_drops_raw_json():
    legacy = _applied({
        "open_threads": ["Tool cannot start without its token:",
                         'Probe result: `{"exit_code": 1, "ok": false}`.'],
        "learnings": ["Reuse the existing compressor"]})
    brief = render_daily_brief(_report(), legacy)

    assert "**Handoff notes** · compacted history; may predate the final turns" in brief
    assert "• Tool cannot start without its token\n" in brief
    assert "exit_code" not in brief
    assert "• Reuse the existing compressor" in brief


def test_registered_thread_appears_only_when_its_state_changed():
    changed = _report(carry_forward={"threads": [
        {"thread_id": "reg-thread-a", "action": "continue",
         "state_before": "active", "state_after": "active"},
        {"thread_id": "reg-thread-b", "action": "stale_review",
         "state_before": "active", "state_after": "stale"}]})
    brief = render_daily_brief(changed, {"enabled": False})

    assert "`reg-thread-b` — active → stale (routed to review)" in brief
    assert "reg-thread-a" not in brief
    assert "reg-thread-a" not in render_daily_brief(_report(), {"enabled": False})


def test_every_recorded_string_is_sanitized_and_machine_detail_stays_internal():
    """Identifiers, handoff lines and reasons all cross the shared sanitizers at the render boundary;
    paths, run/session handles, counters and JSON never reach the delivered brief."""
    secret_id = "sk-" + "A1b2C3d4E5f6G7h8I9j0K1l2"
    report = _report(profile="owner@example.com", cycle_date="not-a-date", carry_forward={"threads": [
        {"thread_id": secret_id, "action": "stale_review", "state_before": "active",
         "state_after": "stale"},
        {"thread_id": "reg-thread-b", "action": "stale_review", "state_before": "active",
         "state_after": "stale"}]})
    brief = render_daily_brief(report, _applied({
        "active_task": ["Ship the fix described in /Users/someone/.hermes/profiles/x/notes/plan.md"],
        "open_items": ["Mail owner@example.com about the renewal",
                       "dream-cycle-v3 cycle ok run=abc sources=0 excluded=107",
                       "[1, 2, 3]",
                       "Session 20261004_063224_9e4976 run 0123456789abcdef0123 stalled"],
        "latest_reply": ["Docs are at https://example.com/a/b/c and the fix is verified."]}))

    assert brief.splitlines()[0] == "🌙 **Dream Cycle**"
    assert "Mail [email_redacted] about the renewal" in brief
    assert "Ship the fix described in plan.md" in brief
    assert "https://example.com/a/b/c" in brief
    assert "`reg-thread-b` — active → stale" in brief
    for leaked in ("owner@example.com", secret_id, "/Users/", "sources=", "excluded=", "[1, 2, 3]",
                   "20261004_063224_9e4976", "0123456789abcdef0123", "not-a-date", "[privacy_withheld]"):
        assert leaked not in brief
