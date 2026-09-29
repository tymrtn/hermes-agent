"""Typed classifier boundary (design §4/§6: LLM output is a proposal only).

Two implementations of one interface:

- RuleBasedClassifier — the deterministic default. Explicit rule table,
  confidence per rule, abstains below the floor or without a rule match.
- UnavailableSemanticClassifier — the Phase 0-1 stand-in for the runtime LLM.
  It always abstains, which proves the pipeline (and every test/dry run)
  needs no model. A future LLM classifier must fill the same outcome type,
  including model + prompt_hash provenance, and gets no other powers.

Whatever abstains is quarantined by `classify_or_quarantine`; nothing guesses.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Literal, Protocol

DETERMINISTIC_CLASSIFIER_VERSION = "rules-1.0.0"

Decision = Literal["classified", "abstain"]


@dataclass(frozen=True)
class ClassificationInput:
    item_id: str
    text: str
    source_id: str
    project_hint: str | None = None


@dataclass(frozen=True)
class ClassificationOutcome:
    item_id: str
    decision: Decision
    classifier_kind: Literal["deterministic", "llm"]
    classifier_version: str
    candidate_class: str | None = None
    confidence: float = 0.0
    reasons: tuple[str, ...] = ()
    model: str | None = None
    prompt_hash: str | None = None

    def __post_init__(self) -> None:
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError("confidence must be in [0,1]")
        if self.decision == "classified" and not self.candidate_class:
            raise ValueError("classified outcome requires candidate_class")
        if self.decision == "abstain" and self.candidate_class is not None:
            raise ValueError("abstain outcome may not carry a class")
        if self.classifier_kind == "llm" and self.decision == "classified" \
                and (self.model is None or self.prompt_hash is None):
            raise ValueError("llm classification requires model and prompt_hash")


class Classifier(Protocol):
    def classify(self, item: ClassificationInput) -> ClassificationOutcome: ...


@dataclass(frozen=True)
class Rule:
    name: str
    pattern: re.Pattern[str]
    candidate_class: str
    confidence: float


DEFAULT_RULES: tuple[Rule, ...] = (
    Rule("explicit_decision", re.compile(r"(?im)^\s*(?:-\s*)?decision\s*:"),
         "decision_record", 0.95),
    Rule("open_loop_marker", re.compile(
        r"(?i)\b(?:TODO|open thread|loose thread|follow[ -]?up needed|blocked on|waiting (?:for|on)|next action)\b"),
         "task_thread", 0.9),
    Rule("stable_preference", re.compile(
        r"(?im)^\s*(?:-\s*)?(?:tyler|user)\s+(?:prefers|always|never)\b"),
         "runtime_memory_warm", 0.85),
    Rule("procedure_lesson", re.compile(
        r"(?i)\b(?:root cause|fix(?:ed)? by|workaround|gotcha|lesson)\b\s*:"),
         "reference_knowledge", 0.85),
)


@dataclass(frozen=True)
class RuleBasedClassifier:
    rules: tuple[Rule, ...] = DEFAULT_RULES
    min_confidence: float = 0.8
    version: str = DETERMINISTIC_CLASSIFIER_VERSION

    def classify(self, item: ClassificationInput) -> ClassificationOutcome:
        for rule in self.rules:
            if rule.pattern.search(item.text):
                if rule.confidence < self.min_confidence:
                    return self._abstain(item, (f"rule:{rule.name}",
                                                "below_min_confidence"))
                return ClassificationOutcome(
                    item_id=item.item_id,
                    decision="classified",
                    classifier_kind="deterministic",
                    classifier_version=self.version,
                    candidate_class=rule.candidate_class,
                    confidence=rule.confidence,
                    reasons=(f"rule:{rule.name}",),
                )
        return self._abstain(item, ("no_rule_matched",))

    def _abstain(self, item: ClassificationInput,
                 reasons: tuple[str, ...]) -> ClassificationOutcome:
        return ClassificationOutcome(
            item_id=item.item_id,
            decision="abstain",
            classifier_kind="deterministic",
            classifier_version=self.version,
            reasons=reasons,
        )


@dataclass(frozen=True)
class UnavailableSemanticClassifier:
    """Runtime-LLM slot, permanently abstaining in Phase 0-1.

    No model is ever consulted here, so the outcome's provenance is
    truthfully deterministic (a pure always-abstain rule); the version string
    still names the vacant llm slot.  An actual LLM classifier reports
    classifier_kind 'llm' and must carry model + prompt_hash even on abstain.
    """

    version: str = "llm-unavailable"
    reason: str = "semantic_classifier_not_enabled_phase1"

    def classify(self, item: ClassificationInput) -> ClassificationOutcome:
        return ClassificationOutcome(
            item_id=item.item_id,
            decision="abstain",
            classifier_kind="deterministic",
            classifier_version=self.version,
            reasons=(self.reason,),
        )


@dataclass(frozen=True)
class ClassifierPipeline:
    """Deterministic first; semantic only for the residue; abstain -> quarantine."""

    deterministic: Classifier = field(default_factory=RuleBasedClassifier)
    semantic: Classifier = field(default_factory=UnavailableSemanticClassifier)

    def classify(self, item: ClassificationInput) -> ClassificationOutcome:
        outcome = self.deterministic.classify(item)
        if outcome.decision == "classified":
            return outcome
        semantic = self.semantic.classify(item)
        if semantic.decision == "classified":
            return semantic
        return ClassificationOutcome(
            item_id=item.item_id,
            decision="abstain",
            classifier_kind=semantic.classifier_kind,
            classifier_version=semantic.classifier_version,
            reasons=tuple(dict.fromkeys(outcome.reasons + semantic.reasons)),
        )


def classify_or_quarantine(items: list[ClassificationInput],
                           pipeline: ClassifierPipeline | None = None,
                           ) -> tuple[list[ClassificationOutcome], list[ClassificationOutcome]]:
    """Split items into (classified, quarantined) — no third bucket exists."""
    pipeline = pipeline or ClassifierPipeline()
    classified: list[ClassificationOutcome] = []
    quarantined: list[ClassificationOutcome] = []
    for item in items:
        outcome = pipeline.classify(item)
        (classified if outcome.decision == "classified" else quarantined).append(outcome)
    return classified, quarantined
