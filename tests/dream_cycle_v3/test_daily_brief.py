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


DAILY_THREADS_HANDOFF = """## Active State
- **Active task:** Keep the portal and the job search moving

## Blocked
- Portal support has not replied

## Errors & Fixes
- User correction: the job search outranks the portal this week

## Daily Threads
- P1-startups/expatriator — Portal API live, onboarding still missing the residency step; next: fix the onboarding gap
  - waiting externally: Madrid portal support reply, check Tue 7 Oct
- P2-career/jobs/beta — applied last week; next: follow up with **the recruiter about the second round because the first call went well**
- P0-career/jobs/acme — final interview booked; next: prepare the system design answers
  - needs authorization: send the *reference list* to [Acme](https://acme.example/jobs)
- P3-career/network — met Ana at the meetup; next: call Ana
- proposed P2-health/exercise — no plan yet; next: ask for a weekly slot
- unassigned: Tax filing — forms gathered; next: [book the accountant call](https://cal.example/acct) this week https://cal.example/acct
- P3-ops/tracker — tracker moved; next: """ + "x" * 180 + """
- a line the summarizer wrote outside the grammar
"""


def _through_the_pipeline(summary_text):
    """Handoff text -> the gateway's highlight projection -> the control-socket row -> the client's
    classification: the exact path a nightly brief's thread lines travel."""
    from unittest.mock import patch

    from dream_cycle_v3.conversation_compression import classify_conversation_compression
    from gateway.scheduled_compression import handoff_highlights

    messages = [{"role": "user", "content": "hi"},
                {"role": "assistant", "content": summary_text, "display_kind": "compaction_summary"},
                {"role": "user", "content": "ok"}, {"role": "assistant", "content": "Done."}]
    with patch("agent.context_compressor.is_compaction_summary_message",
               side_effect=lambda m: isinstance(m, dict)
               and m.get("display_kind") == "compaction_summary"):
        highlights = handoff_highlights(messages)
    response = {"ok": True, "eligible": 1, "sessions": [{
        "status": "applied", "session_ref": "abc123", "before_tokens": 147037,
        "after_tokens": 42738, "before_messages": 190, "after_messages": 26,
        "highlights": highlights}]}
    return classify_conversation_compression(response, profile="nagatha", cycle_id="2026-10-04")


def test_daily_threads_travel_from_the_handoff_to_a_grouped_prioritized_brief():
    """The agreed two-line layout, end to end: dot + established code, then the state symbol and a
    next action of at most ten whole words; no state paragraph, no repeated title."""
    brief = render_daily_brief(_report(cycle_date="2026-10-04"),
                               _through_the_pipeline(DAILY_THREADS_HANDOFF))
    lines = brief.splitlines()

    # Plain text: emphasis and link markup are dropped, never left half open by a cut.
    assert "🔴 P0-career/jobs/acme\n🔐 Send the reference list to Acme\n" in brief
    # Under four words is not padded with invented work.
    assert "🔵 P3-career/network\n▶️ Call Ana (no further detail recorded)\n" in brief
    # Shortened where a clause ends, and the bold the cut would have left open is dropped.
    assert "🟡 P2-career/jobs/beta\n▶️ Follow up with the recruiter about the second round\n" in brief
    assert "🟠 P1-startups/expatriator\n⏳ Madrid portal support reply, check Tue 7 Oct\n" in brief
    # One group's threads stay together, the group placed by its most urgent thread.
    assert (brief.index("P0-career/jobs/acme") < brief.index("P2-career/jobs/beta")
            < brief.index("P3-career/network") < brief.index("P1-startups/expatriator"))
    assert "final interview booked" not in brief and "Portal API live" not in brief
    # Not agreed means not displayed as agreed: no colored dot, labelled, kept apart.
    assert ("**Proposed** · codes and priorities not agreed yet\n• P2-health/exercise\n"
            "▶️ Ask for a weekly slot\n") in brief
    # A line opening with a link keeps its words; a bare link is not a next action, and an
    # over-long first word is dropped whole, never cut.
    assert "**No code yet**\n• Tax filing\n▶️ Book the accountant call this week\n" in brief
    assert "🔵 P3-ops/tracker\n▶️ No next step recorded\n" in brief
    assert "⚠️ 1 handoff thread line was not in the P#-group/task format" in brief
    assert "📝 **Corrections & fixes**" in brief
    assert brief.rstrip().endswith("🗜️ **Compression** applied · 147,037 → 42,738 tokens · "
                                   "190 → 26 messages")

    symbols = ("⛔", "🙋", "🔐", "⏳", "▶️")
    handoff = lines[:lines.index("**Registered threads** · no priority recorded")]
    heads = [i for i, line in enumerate(handoff) if line.startswith(("🔴", "🟠", "🟡", "🔵")) or (
        line.startswith("• ") and lines[i + 1].startswith(symbols))]
    assert len(heads) == 7
    said = set(re.findall(r"[\w'/-]+", DAILY_THREADS_HANDOFF.lower()))
    for i in heads:
        state, _, description = lines[i + 1].partition(" ")
        assert state in symbols and 4 <= len(description.split()) <= 10, lines[i + 1]
        own = description.removesuffix(" (no further detail recorded)").replace(
            "No next step recorded", "")
        assert set(re.findall(r"[\w'/-]+", own.lower())) <= said, lines[i + 1]
        assert lines[i + 2] == "", "a thread is exactly two lines"
    for line in lines:
        assert line.count("`") % 2 == 0 and line.count("**") % 2 == 0, line


def test_registered_threads_show_even_when_unchanged_and_say_what_metadata_is_missing():
    """A legacy handoff (no daily-thread section) and a registry that carries no priority or group:
    the brief says so instead of formatting old bullets as if they were prioritized threads."""
    # What the old gateway shipped: cut at exactly 160 characters, mid-word, no ellipsis.
    clipped = ("Exact current size breakdown for the private temp folders and the user cache folder "
               "is still missing because the most recent bounded measurement returned no rea")
    assert len(clipped) == 160
    report = _report(registered_threads={"threads": [
        {"thread_id": "phase4-shadow-runtime-t_98231477",
         "project_name": "Hermes Dream Cycle continuity runtime", "state": "stale",
         "title": "Build and validate Dream Cycle v3 Phase 4 shadow runtime",
         "next_action": "complete the bounded shadow gates"},
        {"thread_id": "reg-thread-a", "project_name": "Hermes Dream Cycle continuity runtime",
         "state": "active", "title": "Unchanged but still open", "next_action": "keep going"}],
        "total": 2})
    brief = render_daily_brief(report, _applied({"open_threads": [clipped],
                                                 "learnings": ["Reuse the existing compressor"]}))

    assert "⚠️ No priorities, groups or codes" in brief
    assert not any(dot in brief for dot in "🔴🟠🟡🔵")
    assert "**Registered threads** · no priority recorded" in brief
    assert "• reg-thread-a\n▶️ Keep going (no further detail recorded)\n" in brief
    assert "• phase4-shadow-runtime-t_98231477\n💤 Complete the bounded shadow gates\n" in brief
    assert "measurement returned no…" in brief


def test_every_recorded_string_is_sanitized_and_machine_detail_stays_internal():
    """Identifiers, handoff lines and reasons all cross the shared sanitizers at the render boundary;
    paths, run/session handles, counters and JSON never reach the delivered brief."""
    secret_id = "sk-" + "A1b2C3d4E5f6G7h8I9j0K1l2"
    # Markdown clean-up would turn "__" into "" and shorten this key below the scanner's pattern.
    key = "AIza" + "SyA1b2C3d4E5f6G7h8I9j0K__l2M3n4O5p6"
    # One unpaired backtick splits this key for the scan; balancing the Markdown would rejoin it.
    split_key = "AIza" + "SyA1b2C3d4E5f6G7h8`I9j0K1l2M3n4O5p6Q"
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
        "latest_reply": ["Docs are at https://example.com/a/b/c and the fix is verified."],
        "corrections": [f"rotate {split_key} now"]}))

    assert brief.splitlines()[0] == "🌙 **Dream Cycle**"
    assert "Mail [email_redacted] about the renewal" in brief
    assert "Ship the fix described in plan.md" in brief
    assert "https://example.com/a/b/c" in brief
    assert "• reg-thread-b\n💤 Stale, routed to review\n" in brief
    for leaked in ("owner@example.com", secret_id, "/Users/", "sources=", "excluded=", "[1, 2, 3]",
                   "20261004_063224_9e4976", "0123456789abcdef0123", "not-a-date", "[privacy_withheld]"):
        assert leaked not in brief

    assert split_key.replace("`", "") not in brief

    # A thread's ten-word line drops Markdown marks; the secret scan sees the text before that,
    # including text a long link would push past the scanner's window.
    described = render_daily_brief(_report(registered_threads={"total": 3, "threads": [
        {"thread_id": "reg-thread-k", "project_name": "Keys", "state": "active",
         "next_action": f"rotate {key} today"},
        {"thread_id": "reg-thread-l", "project_name": "Keys", "state": "active",
         "next_action": f"[x](https://{'a' * 1800}) rotate {key} today"},
        {"thread_id": "reg-thread-m", "project_name": "Keys", "state": "active",
         "next_action": "aKIAIOSFODNN7EXAMPLE stays in the vault"}]}), None)
    assert "• reg-thread-k\n▶️ No next step recorded\n" in described
    assert key not in described and key.replace("__", "")[:30] not in described
    assert "AKIAIOSFODNN7EXAMPLE" not in described  # capitalizing the line must not complete one
    titled = render_daily_brief(_report(profile="aKIAIOSFODNN7EXAMPLE"), None)
    assert "AKIAIOSFODNN7EXAMPLE" not in titled and titled.startswith("🌙 **Dream Cycle**")

    # Through the gateway's transport too: a code's case and a key's last character survive to
    # the scan, so neither is the reason a credential gets through.
    sk = "sk-" + "Ab1" * 6 + "C-"  # exactly the scanner's minimum, the hyphen included
    leaky = render_daily_brief(_report(), _through_the_pipeline(
        "## Daily Threads\n"
        "- P1-ops/AKIAIOSFODNN7EXAMPLE — key in the code; next: rotate the access key today\n"
        f"- P3-ops/keys — rotating; next: {'w' * 135} {sk} and the rest of the action\n"))
    assert "akiaiosfodnn7example" not in leaky.lower()
    assert sk[:-1] not in leaky
