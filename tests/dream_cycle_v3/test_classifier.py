import pytest

from dream_cycle_v3.classifier import (ClassificationInput, ClassificationOutcome,
                                       ClassifierPipeline, RuleBasedClassifier,
                                       UnavailableSemanticClassifier,
                                       classify_or_quarantine)


def _item(text, item_id="item-1"):
    return ClassificationInput(item_id=item_id, text=text, source_id="profile:x.md")


@pytest.mark.parametrize("text,expected", [
    ("Decision: keep task state in the external tracker", "decision_record"),
    ("TODO: wire the retriever hook", "task_thread"),
    ("blocked on Tyler approval for the deploy", "task_thread"),
    ("Tyler prefers compact machine reports", "runtime_memory_warm"),
    ("Root cause: HOME sandboxing forked the state directory", "reference_knowledge"),
])
def test_rules_classify_deterministically(text, expected):
    outcome = RuleBasedClassifier().classify(_item(text))
    assert outcome.decision == "classified"
    assert outcome.candidate_class == expected
    assert outcome.classifier_kind == "deterministic"
    assert outcome.confidence >= 0.8
    assert outcome.model is None


def test_unmatched_text_abstains():
    outcome = RuleBasedClassifier().classify(_item("just some prose"))
    assert outcome.decision == "abstain"
    assert outcome.candidate_class is None
    assert "no_rule_matched" in outcome.reasons


def test_below_confidence_floor_abstains():
    strict = RuleBasedClassifier(min_confidence=0.99)
    outcome = strict.classify(_item("TODO: anything"))
    assert outcome.decision == "abstain"
    assert "below_min_confidence" in outcome.reasons


def test_semantic_slot_always_abstains_in_phase1():
    outcome = UnavailableSemanticClassifier().classify(_item("mystery"))
    assert outcome.decision == "abstain"
    # No model runs in the vacant slot, so provenance is truthfully
    # deterministic; the version string still names the llm slot.
    assert outcome.classifier_kind == "deterministic"
    assert outcome.classifier_version == "llm-unavailable"


def test_pipeline_quarantines_the_residue():
    items = [_item("Decision: adopt fingerprints", "i1"),
             _item("unclassifiable musing", "i2")]
    classified, quarantined = classify_or_quarantine(items, ClassifierPipeline())
    assert [o.item_id for o in classified] == ["i1"]
    assert [o.item_id for o in quarantined] == ["i2"]
    assert all(o.decision == "abstain" for o in quarantined)


def test_outcome_type_invariants():
    with pytest.raises(ValueError, match="requires candidate_class"):
        ClassificationOutcome(item_id="x", decision="classified",
                              classifier_kind="deterministic",
                              classifier_version="v")
    with pytest.raises(ValueError, match="may not carry a class"):
        ClassificationOutcome(item_id="x", decision="abstain",
                              classifier_kind="deterministic",
                              classifier_version="v", candidate_class="ephemeral")
    with pytest.raises(ValueError, match="model and prompt_hash"):
        ClassificationOutcome(item_id="x", decision="classified",
                              classifier_kind="llm", classifier_version="v",
                              candidate_class="ephemeral", confidence=0.9)
    with pytest.raises(ValueError, match="confidence"):
        ClassificationOutcome(item_id="x", decision="abstain",
                              classifier_kind="deterministic",
                              classifier_version="v", confidence=2.0)
