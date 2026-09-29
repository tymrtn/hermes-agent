"""Promotion policies: token accounting, duplicates, leakage, budgets, conflicts."""
from pathlib import Path

import pytest

from dream_cycle_v3.adapters.destinations import ExistingRecord
from dream_cycle_v3.errors import PromotionPolicyError
from dream_cycle_v3.policies import (HOT_MEMORY_TOKEN_CAP,
                                     PROJECT_SKILL_TOKEN_CAP,
                                     ConflictResolutions, check_budget,
                                     check_conflicts, check_duplicates,
                                     check_hot_memory_leakage, estimate_tokens,
                                     find_duplicate, jaccard)


def rec(text, key=None, location="fixture"):
    return ExistingRecord(record_key=key, subject=None, text=text,
                          location=location)


# -- token accounting ---------------------------------------------------------

def test_token_estimate_is_documented_conservative_rule():
    # Documented rule: ceil(utf8_bytes / 3). Deterministic and over-counting.
    assert estimate_tokens("") == 0
    assert estimate_tokens("abc") == 1
    assert estimate_tokens("abcd") == 2
    text = "hello world " * 100  # 1200 ASCII bytes
    assert estimate_tokens(text) == 400
    # Multi-byte input counts bytes, not characters.
    assert estimate_tokens("é") == 1          # 2 utf-8 bytes
    assert estimate_tokens("日本語") == 3       # 9 utf-8 bytes
    # Determinism.
    assert estimate_tokens(text) == estimate_tokens(text)


def test_token_estimate_overcounts_typical_english():
    # ~4 bytes/token is the real-world average; our rule must give more.
    text = "the quick brown fox jumps over the lazy dog"
    realistic = len(text) / 4
    assert estimate_tokens(text) > realistic


# -- duplicates ----------------------------------------------------------------

def test_exact_duplicate_detected_despite_formatting():
    existing = [rec("Tyler prefers  feature-complete COMMITS.", key="k1")]
    match = find_duplicate("tyler prefers feature-complete commits",
                           existing, own_record_key="me")
    assert match.kind == "exact"
    with pytest.raises(PromotionPolicyError) as exc:
        check_duplicates("tyler prefers feature-complete commits",
                         existing, own_record_key="me")
    assert exc.value.reason == "exact_duplicate"
    assert exc.value.disposition == "reject"


def test_near_duplicate_quarantines_with_cluster():
    existing = [rec("the gateway error log is append-only and never rotated "
                    "so tail sees old floods", key="k1")]
    claim = ("gateway error log is append-only, never rotated; "
             "tail sees old floods")
    match = find_duplicate(claim, existing, own_record_key="me")
    assert match.kind == "near"
    assert match.matched_record_key == "k1"
    with pytest.raises(PromotionPolicyError) as exc:
        check_duplicates(claim, existing, own_record_key="me")
    assert exc.value.disposition == "quarantine"


def test_own_record_is_a_revision_not_a_duplicate():
    existing = [rec("tyler prefers feature-complete commits", key="mine")]
    assert find_duplicate("tyler prefers feature-complete commits",
                          existing, own_record_key="mine") is None


def test_unrelated_claims_pass():
    existing = [rec("the kanban root database is a decoy", key="k1")]
    assert find_duplicate("playwright browsers install via python3 -m "
                          "playwright install chromium",
                          existing, own_record_key="me") is None
    assert jaccard(frozenset({"a"}), frozenset({"b"})) == 0.0


# -- hot-memory task leakage ---------------------------------------------------

@pytest.mark.parametrize("claim", [
    "TODO: wire the retriever hook tomorrow",
    "blocked on Tyler approving the cron change",
    "waiting for the gh cli to be installed",
    "next action: ship the phase 2 adapters",
    "follow-up scheduled for the kanban migration",
    "kanban:hermes:T-1001 still needs review",
    "deploy is due by friday",
])
def test_hot_memory_rejects_task_language(claim):
    with pytest.raises(PromotionPolicyError) as exc:
        check_hot_memory_leakage("memory:hot", claim, "runtime_memory_hot")
    assert exc.value.reason == "hot_memory_task_leakage"
    assert exc.value.disposition == "reject"


def test_hot_memory_rejects_task_thread_class_outright():
    with pytest.raises(PromotionPolicyError):
        check_hot_memory_leakage("memory:hot",
                                 "a perfectly stable claim", "task_thread")


def test_stable_facts_pass_hot_and_everything_passes_warm():
    check_hot_memory_leakage("memory:hot",
                             "default python3 is 3.14 via homebrew",
                             "runtime_memory_hot")
    # Warm/other destinations are not subject to the hot gate.
    check_hot_memory_leakage("memory:warm", "TODO: anything", "task_thread")
    check_hot_memory_leakage("project:klas-sample:decisions",
                             "blocked on x", "decision_record")


# -- budgets --------------------------------------------------------------------

def test_hot_memory_budget_cap():
    small = {Path("/x/MEMORY.md"): "tiny index"}
    check_budget("memory:hot", small)
    over = {Path("/x/MEMORY.md"): "x" * (HOT_MEMORY_TOKEN_CAP * 3 + 3)}
    with pytest.raises(PromotionPolicyError) as exc:
        check_budget("memory:hot", over)
    assert exc.value.reason == "hot_memory_budget"
    assert exc.value.disposition == "quarantine"
    # The cap applies to the injected index, not fact files.
    check_budget("memory:hot",
                 {Path("/x/fact.md"): "x" * (HOT_MEMORY_TOKEN_CAP * 3 + 3)})


def test_project_skill_budget_cap():
    over = {Path("/s/demo/SKILL.md"): "y" * (PROJECT_SKILL_TOKEN_CAP * 3 + 3)}
    with pytest.raises(PromotionPolicyError) as exc:
        check_budget("skill:demo", over)
    assert exc.value.reason == "project_skill_budget"
    check_budget("skill:demo", {Path("/s/demo/SKILL.md"): "small"})
    # Other destinations have no token cap here (region bound applies instead).
    check_budget("project:klas-sample:decisions",
                 {Path("/p/klas-sample/decisions.md"): "z" * 20000})


# -- conflicts -------------------------------------------------------------------

def test_unresolved_conflict_with_active_claim_quarantines():
    with pytest.raises(PromotionPolicyError) as exc:
        check_conflicts(["cand-a"], {"cand-a": "promoted"},
                        ConflictResolutions())
    assert exc.value.reason == "unresolved_conflict"
    assert exc.value.disposition == "quarantine"


def test_conflict_with_inactive_candidate_is_informational():
    assert check_conflicts(["cand-a", "cand-b"],
                           {"cand-a": "rejected", "cand-b": None},
                           ConflictResolutions()) == []


def test_supersedes_and_scoped_exception_relationships():
    resolutions = ConflictResolutions({"cand-a": "supersedes",
                                       "cand-b": "scoped_exception"})
    supersede = check_conflicts(["cand-a", "cand-b"],
                                {"cand-a": "promoted", "cand-b": "promoted"},
                                resolutions)
    assert supersede == ["cand-a"]


def test_bad_relationship_is_refused_at_construction():
    with pytest.raises(ValueError, match="must be one of"):
        ConflictResolutions({"cand-a": "merge"})
