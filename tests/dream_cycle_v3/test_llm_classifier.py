"""Typed LLM classifier: strict schema, thresholds, abstention, containment."""
import dataclasses
import json

import pytest

from dream_cycle_v3.canonical import sha256_hex
from dream_cycle_v3.classifier import (ClassificationInput, ClassifierPipeline,
                                       RuleBasedClassifier,
                                       classify_or_quarantine)
from dream_cycle_v3.llm_classifier import (PROMPT_TEMPLATE, PROPOSABLE_CLASSES,
                                           LLMClassifier)

CANARY = "CANARY-9f3a-transcript-secret-phrase"


def item(text="the deploy pipeline uses blue-green switching", n=1):
    return ClassificationInput(item_id=f"item-{n:04d}", text=text,
                               source_id="profile:notes/a.md")


def canned(reply):
    """Deterministic transport returning a fixed reply."""
    def transport(model, prompt):
        return reply
    return transport


def test_valid_reply_classifies_with_model_and_prompt_hash():
    clf = LLMClassifier(
        transport=canned('{"decision": "classify", '
                         '"class": "reference_knowledge", "confidence": 0.9}'),
        model="fixture-model-1")
    outcome = clf.classify(item())
    assert outcome.decision == "classified"
    assert outcome.classifier_kind == "llm"
    assert outcome.candidate_class == "reference_knowledge"
    assert outcome.confidence == 0.9
    assert outcome.model == "fixture-model-1"
    assert outcome.prompt_hash == "sha256:" + sha256_hex(PROMPT_TEMPLATE)
    assert outcome.reasons == ("llm_classified",)


def test_prompt_hash_is_template_identity_not_item_content():
    clf = LLMClassifier(transport=canned(
        '{"decision": "classify", "class": "decision_record", '
        '"confidence": 0.9}'), model="m")
    a = clf.classify(item("first observation", n=1))
    b = clf.classify(item("completely different observation", n=2))
    assert a.prompt_hash == b.prompt_hash


def test_below_threshold_abstains():
    clf = LLMClassifier(
        transport=canned('{"decision": "classify", '
                         '"class": "decision_record", "confidence": 0.5}'),
        model="m", min_confidence=0.75)
    outcome = clf.classify(item())
    assert outcome.decision == "abstain"
    assert outcome.reasons == ("below_min_confidence",)
    assert outcome.candidate_class is None


def test_model_abstention_is_respected():
    clf = LLMClassifier(
        transport=canned('{"decision": "abstain", "class": null, '
                         '"confidence": 0.2}'), model="m")
    assert clf.classify(item()).reasons == ("llm_abstained",)


@pytest.mark.parametrize("reply", [
    "not json at all",
    '"just a string"',
    '{"decision": "classify", "class": "reference_knowledge"}',   # missing key
    '{"decision": "classify", "class": "reference_knowledge", '
    '"confidence": 0.9, "note": "extra"}',                        # unknown key
    '{"decision": "classify", "class": "quarantine", "confidence": 0.9}',
    '{"decision": "classify", "class": "made_up_class", "confidence": 0.9}',
    '{"decision": "classify", "class": null, "confidence": 0.9}',
    '{"decision": "abstain", "class": "reference_knowledge", "confidence": 0.9}',
    '{"decision": "classify", "class": "reference_knowledge", '
    '"confidence": 1.5}',                                         # range
    '{"decision": "classify", "class": "reference_knowledge", '
    '"confidence": true}',                                        # bool
    '{"decision": "maybe", "class": "reference_knowledge", "confidence": 0.9}',
    '```json\n{"decision": "classify", "class": "reference_knowledge", '
    '"confidence": 0.9}\n```',                                    # fenced
])
def test_any_schema_violation_abstains(reply):
    clf = LLMClassifier(transport=canned(reply), model="m")
    outcome = clf.classify(item())
    assert outcome.decision == "abstain"
    assert outcome.reasons == ("schema_violation",)


def test_transport_error_abstains_without_leaking_details():
    def exploding(model, prompt):
        raise RuntimeError(f"connection refused while sending: {prompt}")

    clf = LLMClassifier(transport=exploding, model="m")
    outcome = clf.classify(item(CANARY))
    assert outcome.decision == "abstain"
    assert outcome.reasons == ("transport_error",)
    for value in dataclasses.asdict(outcome).values():
        assert CANARY not in str(value)


def test_no_transcript_content_in_any_outcome_field():
    """Containment: even a hostile model reply cannot smuggle item text into
    the outcome, because valid replies carry only enums/numbers and invalid
    ones abstain with fixed-vocabulary reasons."""
    hostile = canned(json.dumps({
        "decision": "classify",
        "class": f"echo {CANARY}",       # invalid class -> schema_violation
        "confidence": 0.99}))
    clf = LLMClassifier(transport=hostile, model="m")
    outcome = clf.classify(item(f"observation containing {CANARY}"))
    assert outcome.decision == "abstain"
    for value in dataclasses.asdict(outcome).values():
        assert CANARY not in str(value)

    # And a fully valid classification of canary-bearing text is also clean.
    valid = canned('{"decision": "classify", "class": "reference_knowledge", '
                   '"confidence": 0.9}')
    outcome = LLMClassifier(transport=valid, model="m").classify(
        item(f"gotcha: {CANARY} is the root cause"))
    for value in dataclasses.asdict(outcome).values():
        assert CANARY not in str(value)


def test_quarantine_class_is_never_proposable():
    assert "quarantine" not in PROPOSABLE_CLASSES


def test_pipeline_integration_rules_first_llm_residue():
    llm = LLMClassifier(
        transport=canned('{"decision": "classify", '
                         '"class": "project_context", "confidence": 0.85}'),
        model="fixture-model-1")
    pipeline = ClassifierPipeline(deterministic=RuleBasedClassifier(),
                                  semantic=llm)
    # A rules-covered item never reaches the LLM slot.
    rule_hit = pipeline.classify(item("Decision: adopt the v3 store"))
    assert rule_hit.classifier_kind == "deterministic"
    # Residue goes to the LLM and carries its provenance.
    residue = pipeline.classify(item("ambiguous prose the rules skip"))
    assert residue.classifier_kind == "llm"
    assert residue.model == "fixture-model-1"
    assert residue.prompt_hash is not None

    classified, quarantined = classify_or_quarantine(
        [item("Decision: adopt the v3 store", n=1),
         item("ambiguous prose the rules skip", n=2)], pipeline)
    assert len(classified) == 2 and not quarantined


def test_pipeline_abstention_still_quarantines():
    llm = LLMClassifier(transport=canned("garbage"), model="m")
    pipeline = ClassifierPipeline(semantic=llm)
    classified, quarantined = classify_or_quarantine(
        [item("ambiguous prose the rules skip")], pipeline)
    assert not classified and len(quarantined) == 1
    assert quarantined[0].reasons[-1] == "schema_violation"


def test_constructor_validation():
    with pytest.raises(ValueError, match="model is required"):
        LLMClassifier(transport=canned("{}"), model="")
    with pytest.raises(ValueError, match="min_confidence"):
        LLMClassifier(transport=canned("{}"), model="m", min_confidence=1.5)
    with pytest.raises(ValueError, match="prompt_template"):
        LLMClassifier(transport=canned("{}"), model="m",
                      prompt_template="no placeholder")
