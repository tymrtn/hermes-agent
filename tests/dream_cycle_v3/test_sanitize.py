"""Phase 3 privacy/bounds: recursive fail-closed sanitizer, PII redaction,
per-field validation, and the hard serialized-output cap.

Review finding 4 regression coverage: canaries for EVERY lookup output field
(task refs, provider/locator, context skill id, owner, project/thread/
candidate ids, status, disposition fields, destination, dates, headings) and
for corrupt/oversize (200k) owned-DB rows.
"""
import json
import sqlite3

import pytest

from dream_cycle_v3.lookup import (MAX_RESULT_CHARS, continuity_lookup)
from dream_cycle_v3.sanitize import (INVALID, WITHHELD, cap_serialized,
                                     has_raw_pii, redact_pii, sanitize_date,
                                     sanitize_identifier, sanitize_payload,
                                     sanitize_text)
from dream_cycle_v3.store import ContinuityStore

from .conftest import NOW_ISO, make_manifest_for_run

RUN_MANIFEST = make_manifest_for_run(profile="nagatha")

SECRET = "sk-AAAAAAAAAAAAAAAAAAAAAAAAAA"
EMAIL = "victim@example.com"
PHONE = "+34 612 345 678"


# -- unit: sanitizers ---------------------------------------------------------

def test_sanitize_text_redacts_email_and_phone():
    out = sanitize_text(f"call {PHONE} or write {EMAIL} today")
    assert EMAIL not in out and "612 345 678" not in out
    assert "[email_redacted]" in out and "[phone_redacted]" in out


def test_sanitize_text_withholds_secrets_before_clipping():
    # The secret sits past the clip limit: scanning happens on the FULL
    # string, so a clipped prefix can never leak a partial secret silently.
    text = "x" * 280 + " " + SECRET
    assert sanitize_text(text, 100) == WITHHELD


def test_sanitize_text_preserves_dates():
    assert sanitize_text("due 2026-07-13 at 12:30") == "due 2026-07-13 at 12:30"


def test_sanitize_identifier_rules():
    assert sanitize_identifier("kanban:hermes:T-1001") == "kanban:hermes:T-1001"
    assert sanitize_identifier("github:owner/repo#7") == "github:owner/repo#7"
    assert sanitize_identifier("has whitespace") == INVALID
    assert sanitize_identifier("x" * 500) == INVALID
    assert sanitize_identifier(SECRET) == WITHHELD


def test_sanitize_date_rules():
    assert sanitize_date("2026-07-13") == "2026-07-13"
    assert sanitize_date("2026-07-13T08:00:00+00:00") \
        == "2026-07-13T08:00:00+00:00"
    assert sanitize_date("not a date") == INVALID
    assert sanitize_date(EMAIL) == INVALID


def test_sanitize_payload_recursive_backstop():
    payload = {"a": [{"b": SECRET}, {"c": EMAIL}], "d": ("x", PHONE)}
    out = sanitize_payload(payload)
    blob = json.dumps(out)
    assert SECRET not in blob and EMAIL not in blob
    assert "612 345 678" not in blob


def test_cap_serialized_hard_limit():
    payload = {"schema_version": 1, "kind": "project",
               "items": [{"text": "y" * 5000} for _ in range(50)]}
    out = cap_serialized(payload, 4000)
    assert len(json.dumps(out, ensure_ascii=False)) <= 4000
    assert out.get("truncated") is True or out.get("kind") == "error"


def test_redact_pii_leaves_iso_dates_alone():
    assert redact_pii("2026-07-13T08:00:00+00:00") \
        == "2026-07-13T08:00:00+00:00"


# -- lookup field canaries over an adversarial owned store ---------------------

CANARY_FIELDS = {
    # column -> adversarial value planted directly in the owned store
    "title": f"contact {EMAIL} urgently",
    "normalized_next_action": f"phone {PHONE} about the rollout",
    "external_task_ref": SECRET,
    "owner": SECRET,
    "blocked_by": f"waiting on {EMAIL}",
}


@pytest.fixture
def adversarial(tmp_path, sample_projects, sample_threads):
    """Owned store whose rows carry secrets/PII in every field the review
    probed, planted via direct SQL (the audited APIs would reject them)."""
    store_path = tmp_path / "continuity.db"
    with ContinuityStore(store_path) as store:
        store.migrate(NOW_ISO)
        store.record_run(RUN_MANIFEST, "manifest.json", NOW_ISO)
        for project in sample_projects:
            store.upsert_project(project, NOW_ISO)
        for thread in sample_threads:
            store.open_thread(thread, NOW_ISO)

    conn = sqlite3.connect(store_path)
    # A genuinely corrupt DB would not honor CHECK constraints; simulate that.
    conn.execute("PRAGMA ignore_check_constraints = ON")
    conn.execute(
        "UPDATE threads SET title=?, normalized_next_action=?, "
        "external_task_ref=?, owner=?, blocked_by=?, state='zzz_corrupt' "
        "WHERE thread_id='sample-thread-0008-active-open'",
        (CANARY_FIELDS["title"], CANARY_FIELDS["normalized_next_action"],
         CANARY_FIELDS["external_task_ref"], CANARY_FIELDS["owner"],
         CANARY_FIELDS["blocked_by"]))
    # Secret-shaped provider/locator + skill id + bad date on a project.
    conn.execute(
        "UPDATE projects SET task_provider='kanban', task_locator=?, "
        "context_skill_id=?, last_verified_at='not-a-date', "
        "canonical_name=? WHERE project_id='hermes-continuity'",
        (SECRET, f"skill {EMAIL}", f"Continuity ({EMAIL})"))
    conn.commit()
    conn.close()

    projects_home = tmp_path / "projects"
    (projects_home / "hermes-continuity").mkdir(parents=True)
    (projects_home / "hermes-continuity" / "map.md").write_text(
        f"# map\n## Reach {EMAIL}\nBody mentions {SECRET} and {PHONE}.\n",
        encoding="utf-8")
    return {"store": store_path, "projects": projects_home}


def _assert_clean(payload):
    blob = json.dumps(payload, ensure_ascii=False)
    assert SECRET not in blob
    assert EMAIL not in blob
    assert "612 345 678" not in blob
    return blob


def test_thread_payload_all_fields_sanitized(adversarial):
    payload = continuity_lookup(store_path=adversarial["store"],
                                projects_home=adversarial["projects"],
                                thread_id="sample-thread-0008-active-open")
    _assert_clean(payload)
    assert payload["task_ref"] in (WITHHELD, INVALID)
    assert payload["owner"] in (WITHHELD, INVALID)
    assert payload["state"] == INVALID          # corrupt enum renders invalid


def test_project_payload_all_fields_sanitized(adversarial):
    payload = continuity_lookup(store_path=adversarial["store"],
                                projects_home=adversarial["projects"],
                                project="hermes-continuity")
    blob = _assert_clean(payload)
    # Secret-shaped locator withheld; corrupt date invalid; headings cleaned.
    assert payload["task_ssot"]["locator"] in (WITHHELD, INVALID)
    assert payload["last_verified_at"] in (INVALID, None)
    assert "[email_redacted]" in blob or WITHHELD in blob


def test_query_payload_sanitized(adversarial):
    payload = continuity_lookup(store_path=adversarial["store"],
                                projects_home=adversarial["projects"],
                                query="continuity")
    _assert_clean(payload)


def test_corrupt_200k_field_is_hard_capped(adversarial):
    conn = sqlite3.connect(adversarial["store"])
    conn.execute(
        "UPDATE projects SET task_locator=? WHERE project_id='hermes-continuity'",
        ("A" * 200_000,))
    conn.execute(
        "UPDATE threads SET title=? WHERE thread_id='sample-thread-0008-active-open'",
        ("B" * 200_000,))
    conn.commit()
    conn.close()
    for kwargs in ({"project": "hermes-continuity"},
                   {"thread_id": "sample-thread-0008-active-open"},
                   {"query": "continuity"}):
        payload = continuity_lookup(store_path=adversarial["store"],
                                    projects_home=adversarial["projects"],
                                    **kwargs)
        serialized = json.dumps(payload, ensure_ascii=False)
        assert len(serialized) <= MAX_RESULT_CHARS, kwargs
        assert "A" * 1000 not in serialized
        assert "B" * 1000 not in serialized


def test_wake_packet_never_emits_pii(adversarial):
    from dream_cycle_v3.wake import build_wake_packet, WakeInputs
    packet = build_wake_packet(
        store_path=adversarial["store"],
        projects_home=adversarial["projects"],
        kanban_root=None,
        inputs=WakeInputs(profile="nagatha", owner="nagatha", now=NOW_ISO))
    assert packet is not None
    assert SECRET not in packet.text
    assert EMAIL not in packet.text
    assert "612 345 678" not in packet.text
    assert len(packet.text) <= 1600


def test_sanitize_identifier_withholds_phone_shaped_values():
    """Post-verification finding 3: identifiers skip redact_pii (partial
    redaction would break the grammar), so a phone-shaped identifier must
    be withheld outright — the same treatment emails already get."""
    assert sanitize_identifier("555-123-4567") == WITHHELD
    assert sanitize_identifier("todoist:555-123-4567") == WITHHELD
    # Dates, hashes, and ordinary refs keep passing untouched.
    assert sanitize_identifier("2026-07-10") == "2026-07-10"
    assert sanitize_identifier("kanban:sample-board:T-1001") == \
        "kanban:sample-board:T-1001"
    assert sanitize_identifier("sha256:" + "a" * 64) == "sha256:" + "a" * 64


def test_phone_detector_catches_prefixed_and_country_code_forms():
    """Re-review blocker 2: the country-code alternative required a literal
    '+' and the national alternative could not start after '-', so
    identifiers like todoist:1-555-123-4567 carried a full phone number
    straight through sanitize_identifier AND the wake packet's final
    has_raw_pii gate."""
    for phone_shaped in ("todoist:1-555-123-4567", "1-555-123-4567",
                         "44-555-123-4567", "todoist:1.555.123.4567"):
        assert sanitize_identifier(phone_shaped) == WITHHELD, phone_shaped
        assert has_raw_pii(f"follow up on {phone_shaped}") is True, phone_shaped
    # Free-text redaction catches the same forms.
    assert redact_pii("call 1-555-123-4567 today") == \
        "call [phone_redacted] today"
    assert redact_pii("call 1 (555) 123-4567 today") == \
        "call [phone_redacted] today"


def test_phone_detector_keeps_dates_refs_and_hashes_clean():
    """The widened detector must not over-match structured identifiers:
    dates/datetimes (any offset), task refs, thread ids, idempotency keys,
    and hashes all pass unchanged."""
    for clean in ("2026-07-10", "2026-07-10T09:30:00+00:00",
                  "2026-07-10T09:30:00.123456+05:30",
                  "kanban:sample-board:T-1001", "github:owner/repo#7",
                  "thread-0200-000000", "idem-0104-00000000000",
                  "sha256:" + "a" * 64):
        assert sanitize_identifier(clean) == clean, clean
        assert redact_pii(clean) == clean, clean
        assert has_raw_pii(clean) is False, clean
