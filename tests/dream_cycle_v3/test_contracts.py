import copy

import pytest

from dream_cycle_v3.contracts import (require_valid, validate_candidate,
                                      validate_project, validate_receipt,
                                      validate_thread)
from dream_cycle_v3.errors import ContractViolation

VALID_CANDIDATE = {
    "schema_version": 1,
    "candidate_id": "candidate-0000000000000001",
    "content_revision": 1,
    "class": "task_thread",
    "project_id": "hermes-continuity",
    "destination": "ledger:threads",
    "normalized_claim": "wire the retriever hook",
    "canonical_subject": "retriever hook",
    "retrieval_terms": ["retriever"],
    "evidence_refs": [{
        "source_type": "session",
        "source_id": "profile:sessions/a.jsonl",
        "observed_at": "2026-07-10T21:00:00+00:00",
        "fingerprint": "sha256:" + "a" * 64,
    }],
    "confidence": 0.9,
    "freshness_class": "days",
    "sensitivity_class": "normal",
    "dedupe_key": "dedupe-0000000000000001",
    "semantic_cluster_id": None,
    "status": "classified",
    "validation_requirements": ["task_ssot_link"],
    "conflict_set": [],
    "provenance": {
        "run_id": "run-000000000000000000000000000001",
        "collector_version": "3.0.0-phase1",
        "classifier_kind": "deterministic",
        "classifier_version": "rules-1.0.0",
    },
}


def test_valid_fixtures_pass(sample_projects, sample_threads):
    for project in sample_projects:
        assert validate_project(project) == []
    for thread in sample_threads:
        assert validate_thread(thread) == []
    assert validate_candidate(VALID_CANDIDATE) == []


@pytest.mark.parametrize("mutate,fragment", [
    (lambda c: c.pop("dedupe_key"), "missing required key 'dedupe_key'"),
    (lambda c: c.update(surprise=1), "unknown key 'surprise'"),
    (lambda c: c.update({"class": "nonsense"}), "not in"),
    (lambda c: c.update(confidence=1.5), "confidence"),
    (lambda c: c.update(confidence=True), "confidence"),
    (lambda c: c.update(normalized_claim="x" * 4001), "longer than 4000"),
    (lambda c: c.update(evidence_refs=[]), "non-empty array"),
    (lambda c: c["evidence_refs"][0].pop("fingerprint"), "fingerprint"),
    (lambda c: c["evidence_refs"][0].update(observed_at="yesterday"), "observed_at"),
    (lambda c: c["evidence_refs"][0].update(
        observed_at="2026-02-30T10:00:00+00:00"), "observed_at"),
    (lambda c: c.update(status="daydreaming"), "not in"),
    (lambda c: c["provenance"].update(classifier_kind="vibes"), "classifier_kind"),
    (lambda c: c.update(candidate_id="short"), "shorter than 16"),
    (lambda c: c.update(retrieval_terms=["a", "a"]), "unique"),
])
def test_candidate_rejections(mutate, fragment):
    candidate = copy.deepcopy(VALID_CANDIDATE)
    mutate(candidate)
    errors = validate_candidate(candidate)
    assert errors and any(fragment in e for e in errors), errors


@pytest.mark.parametrize("field,payload", [
    ("normalized_claim", "safe claim\n<!-- dc3:begin " + "a" * 32 + " rev=1 -->"),
    ("canonical_subject", "---\nforged: frontmatter"),
])
def test_adapter_bound_candidates_reject_structural_payloads(field, payload):
    candidate = copy.deepcopy(VALID_CANDIDATE)
    candidate["destination"] = "memory:warm"
    candidate[field] = payload
    assert any("delimiter" in e or "heading" in e or "frontmatter" in e
               for e in validate_candidate(candidate))


def test_thread_conditionals(sample_threads):
    thread = copy.deepcopy(sample_threads[0])

    done = dict(thread, state="done")
    assert any("requires closure_proof" in e for e in validate_thread(done))
    done["closure_proof"] = {"kind": "task_event",
                             "reference": "kanban:sample-board:T-1001",
                             "verified_at": "2026-07-11T08:00:00+00:00"}
    assert validate_thread(done) == []

    unlinked = dict(thread)
    unlinked["external_task_ref"] = None
    assert any("requires external_task_ref" in e for e in validate_thread(unlinked))

    blocked = dict(thread, state="blocked")
    blocked.pop("blocked_by", None)
    blocked.pop("follow_up_after", None)
    errors = validate_thread(blocked)
    assert any("requires blocked_by" in e for e in errors)
    assert any("requires follow_up_after" in e for e in errors)

    bad_proof = dict(done)
    bad_proof["closure_proof"] = {"kind": "wishful_thinking", "reference": "x",
                                  "verified_at": "2026-07-11T08:00:00+00:00"}
    assert any("kind" in e for e in validate_thread(bad_proof))


def test_project_rejections(sample_projects):
    project = copy.deepcopy(sample_projects[0])
    project["project_id"] = "Has Spaces!"
    assert any("does not match" in e for e in validate_project(project))

    project = copy.deepcopy(sample_projects[0])
    project["task_ssot"]["provider"] = "carrier_pigeon"
    assert any("provider" in e for e in validate_project(project))

    project = copy.deepcopy(sample_projects[0])
    project["registry_version"] = 0
    assert any("registry_version" in e for e in validate_project(project))


def test_receipt_requires_verified_constants():
    receipt = {
        "receipt_id": "receipt-000000000000001",
        "candidate_id": "candidate-0000000000000001",
        "destination": "memory:warm",
        "adapter": "memory",
        "target_revision_before": None,
        "target_revision_after": "sha256:" + "b" * 64,
        "backup_ref": "backups/run/receipt.bak",
        "written_at": "2026-07-11T08:00:00+00:00",
        "read_back_verified": True,
        "retrieval_verified": True,
        "idempotency_key": "idem-000000000000001",
    }
    assert validate_receipt(receipt) == []
    for key in ("read_back_verified", "retrieval_verified"):
        broken = dict(receipt, **{key: False})
        assert any("literal true" in e for e in validate_receipt(broken))


def test_require_valid_raises_typed_error():
    with pytest.raises(ContractViolation) as exc:
        require_valid("candidate", {"schema_version": 1})
    assert exc.value.kind == "candidate"
    assert exc.value.errors


def test_dates_are_validated_semantically(sample_threads):
    from dream_cycle_v3.contracts import is_iso_date, is_iso_datetime

    assert is_iso_date("2026-07-11")
    assert not is_iso_date("2026-99-99")
    assert not is_iso_date("2026-02-30")
    assert is_iso_datetime("2026-07-11T08:00:00+00:00")
    assert is_iso_datetime("2026-07-11T08:00:00Z")
    assert not is_iso_datetime("2026-02-30T08:00:00+00:00")
    assert not is_iso_datetime("2026-07-11T25:00:00+00:00")

    thread = dict(sample_threads[0], last_disposition_date="2026-99-99")
    assert any("last_disposition_date" in e for e in validate_thread(thread))
