"""Typed LLM classifier for the semantic slot (design §4/§6, plan item 5).

Fills the same `Classifier` interface as the rule engine and gets no other
powers: its output is a structured proposal that downstream deterministic
policy may still route, validate, refuse, or quarantine.

Containment properties, enforced by construction and by tests:

- The transport is injected (`Callable[[model, prompt], str]`). The library
  ships no network client; tests and dry runs use canned deterministic
  transports, and a runtime integration must be wired explicitly.
- The item text may be read transiently to build the prompt, but NOTHING
  from it is ever copied into the outcome: every outcome field is either an
  enum from a fixed vocabulary, a number, or static provenance (model id,
  prompt hash, version). There is no free-text field, so transcript content
  cannot leak into candidates, events, or evidence via this classifier.
- The model's reply must be a single strict-schema JSON object (closed key
  set, typed values, enum class). Anything else — malformed JSON, unknown
  keys, out-of-range confidence, unknown class, free-form text — abstains,
  and abstention downstream is quarantine.
- `model` + `prompt_hash` provenance is mandatory on every classified
  outcome (ClassificationOutcome refuses llm-classified outcomes without
  them). `prompt_hash` is the SHA-256 of the prompt *template*, not the
  filled prompt: it identifies the classification instructions and stays
  constant across items, so it can never encode item content.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Callable

from .canonical import sha256_hex
from .classifier import ClassificationInput, ClassificationOutcome
from .contracts import CANDIDATE_CLASSES

LLM_CLASSIFIER_VERSION = "llm-typed-1.0.0"

# (model, prompt) -> raw model reply text. Injected; never constructed here.
Transport = Callable[[str, str], str]

# Classes the LLM may propose. 'quarantine' is not a proposal — an LLM that
# wants to quarantine must abstain, and abstention already quarantines.
PROPOSABLE_CLASSES = tuple(c for c in CANDIDATE_CLASSES if c != "quarantine")

PROMPT_TEMPLATE = """\
You classify one observation from an agent continuity pipeline.

Reply with EXACTLY one JSON object and nothing else, using this schema:
{"decision": "classify" | "abstain",
 "class": one of """ + json.dumps(list(PROPOSABLE_CLASSES)) + """ or null,
 "confidence": number between 0 and 1}

Rules:
- "class" must be null when decision is "abstain", non-null otherwise.
- Never add other keys, prose, or markdown fences.
- Abstain whenever unsure; abstention is safe and reviewed.

Observation:
{text}
"""

_REQUIRED_KEYS = frozenset({"decision", "class", "confidence"})


def prompt_hash_for(template: str) -> str:
    return "sha256:" + sha256_hex(template)


@dataclass(frozen=True)
class LLMClassifier:
    """Strict-schema LLM classifier; every failure mode abstains."""

    transport: Transport
    model: str
    min_confidence: float = 0.75
    version: str = LLM_CLASSIFIER_VERSION
    prompt_template: str = PROMPT_TEMPLATE

    def __post_init__(self) -> None:
        if not self.model:
            raise ValueError("model is required")
        if not 0.0 <= self.min_confidence <= 1.0:
            raise ValueError("min_confidence must be in [0,1]")
        if "{text}" not in self.prompt_template:
            raise ValueError("prompt_template must contain {text}")

    @property
    def prompt_hash(self) -> str:
        return prompt_hash_for(self.prompt_template)

    def build_prompt(self, item: ClassificationInput) -> str:
        return self.prompt_template.replace("{text}", item.text)

    def classify(self, item: ClassificationInput) -> ClassificationOutcome:
        try:
            raw = self.transport(self.model, self.build_prompt(item))
        except Exception:
            # Transport details may quote the prompt; never propagate them.
            return self._abstain(item, "transport_error")

        parsed = self._parse_strict(raw)
        if parsed is None:
            return self._abstain(item, "schema_violation")
        decision, klass, confidence = parsed
        if decision == "abstain":
            return self._abstain(item, "llm_abstained")
        if confidence < self.min_confidence:
            return self._abstain(item, "below_min_confidence")
        return ClassificationOutcome(
            item_id=item.item_id,
            decision="classified",
            classifier_kind="llm",
            classifier_version=self.version,
            candidate_class=klass,
            confidence=confidence,
            reasons=("llm_classified",),
            model=self.model,
            prompt_hash=self.prompt_hash,
        )

    @staticmethod
    def _parse_strict(raw: str) -> tuple[str, str | None, float] | None:
        """Closed-key, typed, enum-checked parse. None on ANY violation."""
        try:
            obj = json.loads(raw)
        except (ValueError, TypeError):
            return None
        if not isinstance(obj, dict) or set(obj) != _REQUIRED_KEYS:
            return None
        decision = obj["decision"]
        if decision not in ("classify", "abstain"):
            return None
        klass = obj["class"]
        confidence = obj["confidence"]
        if not isinstance(confidence, (int, float)) \
                or isinstance(confidence, bool) or not 0 <= confidence <= 1:
            return None
        if decision == "abstain":
            if klass is not None:
                return None
        else:
            if klass not in PROPOSABLE_CLASSES:
                return None
        return decision, klass, float(confidence)

    def _abstain(self, item: ClassificationInput,
                 reason: str) -> ClassificationOutcome:
        # `reason` is always one of our fixed vocabulary strings — never
        # model output, never item text.
        return ClassificationOutcome(
            item_id=item.item_id,
            decision="abstain",
            classifier_kind="llm",
            classifier_version=self.version,
            reasons=(reason,),
            model=self.model,
            prompt_hash=self.prompt_hash,
        )
