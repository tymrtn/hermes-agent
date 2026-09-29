"""Deterministic promotion policies (design §6 steps 1-4, §9 budgets, §11-12).

Every check either passes silently or raises PromotionPolicyError carrying a
machine-readable reason and the required disposition ('reject' is terminal,
'quarantine' holds the candidate for review). Nothing here writes; the
orchestrator applies dispositions through the store.

Token accounting
----------------
`estimate_tokens` is deliberately conservative and fully deterministic:
``ceil(utf8_byte_length / 3)``. Real BPE tokenizers average roughly 4 bytes
per token on English prose, so dividing by 3 over-counts by ~33%; content
that passes a budget under this rule cannot exceed it under a real
tokenizer. The trade-off (some budget headroom is never used) is accepted so
the runtime stays standard-library-only and byte-reproducible. Budgets per
design §9: hot memory hard cap 2,200 tokens; project skill cap 2,500 tokens.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

from .contracts import is_transcript_evidence
from .errors import PromotionPolicyError
from .routing import TASK_REF_RE

HOT_MEMORY_TOKEN_CAP = 2200
PROJECT_SKILL_TOKEN_CAP = 2500
NEAR_DUPLICATE_JACCARD = 0.8

CONFLICT_RELATIONSHIPS = ("supersedes", "scoped_exception")
_REVIEW_SENSITIVITIES = frozenset(
    ("personal", "sensitive", "legal", "medical", "financial"))
_PROJECT_SENSITIVITY_ALLOWLIST = {
    "normal": frozenset(("normal",)),
    "sensitive": frozenset(("normal", "personal", "sensitive")),
    "legal": frozenset(("normal", "legal")),
    "medical": frozenset(("normal", "medical")),
    "financial": frozenset(("normal", "financial")),
    "credentials": frozenset(("normal",)),
}
_POLARITY_TRUE = frozenset(("enabled", "active", "allowed", "on", "true",
                            "available", "present"))
_POLARITY_FALSE = frozenset(("disabled", "inactive", "disallowed", "off",
                             "false", "unavailable", "absent"))

# Task/open-loop language that must never reach hot memory (§2 rule 3, §15
# "zero task-state items in hot memory"). Over-inclusion is the safe
# direction: a false positive quarantines for review; a false negative leaks
# task state into every future session prompt.
_TASK_LEAKAGE_RE = re.compile(
    r"(?i)\b(?:TODO|open thread|loose thread|open loop|follow[ -]?ups?|"
    r"blocked (?:on|by)|waiting (?:for|on)|next action|next step|"
    r"in progress|due (?:by|on|date)|deadline|revisit|carry[ -]?forward|"
    r"as of (?:today|yesterday|this week))\b")


def estimate_tokens(text: str) -> int:
    """Conservative deterministic token estimate: ceil(utf8_bytes / 3)."""
    data = text.encode("utf-8")
    return -(-len(data) // 3)


def _normalized_words(text: str) -> frozenset[str]:
    return frozenset(re.findall(r"[a-z0-9]+", text.lower()))


def normalized_claim_text(text: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", text.lower()))


def jaccard(a: frozenset[str], b: frozenset[str]) -> float:
    if not a and not b:
        return 1.0
    union = a | b
    if not union:
        return 0.0
    return len(a & b) / len(union)


@dataclass(frozen=True)
class DuplicateMatch:
    kind: str            # 'exact' | 'near'
    similarity: float
    matched_record_key: str | None
    matched_location: str


def find_duplicate(claim: str, existing: Iterable, *,
                   own_record_key: str,
                   threshold: float = NEAR_DUPLICATE_JACCARD
                   ) -> DuplicateMatch | None:
    """Exact/near duplicate detection against destination records.

    The candidate's own record (same record_key) is a *revision*, never a
    duplicate. Exact = identical normalized text; near = word-set Jaccard at
    or above the threshold. Deterministic by construction: normalization and
    similarity involve no randomness or model calls.
    """
    norm_claim = normalized_claim_text(claim)
    words = _normalized_words(claim)
    best: DuplicateMatch | None = None
    for record in existing:
        if record.record_key == own_record_key:
            continue
        norm_existing = normalized_claim_text(record.text)
        if not norm_existing:
            continue
        if norm_existing == norm_claim:
            return DuplicateMatch(kind="exact", similarity=1.0,
                                  matched_record_key=record.record_key,
                                  matched_location=record.location)
        sim = jaccard(words, _normalized_words(record.text))
        if sim >= threshold and (best is None or sim > best.similarity):
            best = DuplicateMatch(kind="near", similarity=sim,
                                  matched_record_key=record.record_key,
                                  matched_location=record.location)
    return best


def check_duplicates(claim: str, existing: Iterable, *,
                     own_record_key: str) -> None:
    match = find_duplicate(claim, existing, own_record_key=own_record_key)
    if match is None:
        return
    if match.kind == "exact":
        raise PromotionPolicyError(
            "exact_duplicate",
            f"claim already present at {match.matched_location}",
            disposition="reject")
    raise PromotionPolicyError(
        "near_duplicate",
        f"claim is {match.similarity:.2f} similar to "
        f"{match.matched_location}; semantic merge requires review "
        f"(cluster {match.matched_record_key})",
        disposition="quarantine")


def check_hot_memory_leakage(destination: str, claim: str,
                             candidate_class: str, *,
                             subject: str = "") -> None:
    """No task/open-loop state may be promoted to hot memory.

    Both the claim and the canonical subject are scanned: the subject is
    rendered into the globally injected MEMORY.md index line and the fact
    frontmatter, so task language there leaks exactly like claim text.
    """
    if destination != "memory:hot":
        return
    if candidate_class == "task_thread":
        raise PromotionPolicyError(
            "hot_memory_task_leakage",
            "task_thread candidates are ledger material, never hot memory",
            disposition="reject")
    rendered_text = f"{subject}\n{claim}" if subject else claim
    if _TASK_LEAKAGE_RE.search(rendered_text) or TASK_REF_RE.search(rendered_text):
        raise PromotionPolicyError(
            "hot_memory_task_leakage",
            "subject or claim carries task/open-loop language; hot memory "
            "holds stable cross-project context only",
            disposition="reject")


def check_transcript_containment(evidence_refs: Iterable[Mapping[str, Any]]
                                 ) -> None:
    """Session-derived evidence is metadata-only and never promotes (§2).

    Transcripts may be read transiently during collection, but a candidate
    whose evidence is session-backed must stay quarantined rather than carry
    possibly transcript-derived text into a destination.
    """
    for ref in evidence_refs:
        if isinstance(ref, Mapping) and is_transcript_evidence(ref):
            raise PromotionPolicyError(
                "transcript_containment",
                "session-derived evidence is metadata-only; a session-backed "
                "candidate is never promoted to a destination",
                disposition="quarantine")


def check_llm_provenance(classifier_kind: Any, model: Any,
                         prompt_hash: Any) -> None:
    """LLM-classified candidates must carry auditable model provenance."""
    if classifier_kind != "llm":
        return
    for name, value in (("model", model), ("prompt_hash", prompt_hash)):
        if not isinstance(value, str) or not value.strip():
            raise PromotionPolicyError(
                "llm_provenance_incomplete",
                f"llm-classified candidate lacks auditable provenance "
                f"({name}); promotion fails closed",
                disposition="quarantine")


def _evidence_source_types(evidence_refs: Iterable[Mapping[str, Any]]) -> set[str]:
    return {str(ref.get("source_type")) for ref in evidence_refs
            if isinstance(ref, Mapping)}


def check_validation_requirements(requirements: Iterable[str],
                                  evidence_refs: Iterable[Mapping[str, Any]],
                                  candidate_class: str) -> None:
    """Prove every declared validation requirement from typed evidence.

    A classifier cannot assert that a requirement is satisfied merely by
    placing its label in the candidate.  Unknown labels are held for review,
    so adding a new validation mode is an intentional code/schema change.
    """
    sources = _evidence_source_types(evidence_refs)
    for requirement in sorted(set(requirements)):
        if requirement == "explicit_review":
            satisfied = "user_confirmation" in sources
        elif requirement == "live_verification":
            satisfied = "live_probe" in sources
        elif requirement == "task_ssot_link":
            satisfied = "task" in sources
        elif requirement == "decision_evidence":
            satisfied = bool(sources & {"user_confirmation", "file", "git"})
        elif requirement == "procedure_fixture":
            satisfied = "log" in sources and bool(sources & {"file", "git"})
        elif requirement == "class_validation":
            # The requirement names the classifier's class-specific proof
            # gate.  Runtime claims need a live probe; decisions need durable
            # decision evidence; procedures need a reproduced fixture.
            if candidate_class in ("runtime_memory_hot", "runtime_memory_warm"):
                satisfied = "live_probe" in sources
            elif candidate_class == "decision_record":
                satisfied = bool(sources & {"user_confirmation", "file", "git"})
            elif candidate_class == "reference_knowledge":
                satisfied = "log" in sources and bool(sources & {"file", "git"})
            else:
                satisfied = bool(sources)
        else:
            raise PromotionPolicyError(
                "unknown_validation_requirement",
                f"{candidate_class} declares unsupported validation requirement "
                f"{requirement!r}", disposition="quarantine")
        if not satisfied:
            raise PromotionPolicyError(
                "validation_requirement_unmet",
                f"{candidate_class} requires {requirement!r} but its typed "
                "evidence does not prove it", disposition="quarantine")


def check_sensitivity_and_authority(*, sensitivity_class: str,
                                    validation_requirements: Iterable[str],
                                    evidence_refs: Iterable[Mapping[str, Any]],
                                    project: Mapping[str, Any] | None) -> None:
    """Apply non-bypassable sensitivity and explicit-review gates."""
    if sensitivity_class == "credential_forbidden":
        raise PromotionPolicyError(
            "credential_forbidden",
            "credential material is never eligible for promotion",
            disposition="reject")
    sources = _evidence_source_types(evidence_refs)
    requirements = set(validation_requirements)
    if sensitivity_class in _REVIEW_SENSITIVITIES and "user_confirmation" not in sources:
        raise PromotionPolicyError(
            "sensitivity_review_required",
            f"{sensitivity_class} promotion requires explicit review evidence",
            disposition="quarantine")
    if "explicit_review" in requirements and "user_confirmation" not in sources:
        raise PromotionPolicyError(
            "authority_review_required",
            "candidate requires explicit review evidence before promotion",
            disposition="quarantine")
    if project is not None:
        allowed = _PROJECT_SENSITIVITY_ALLOWLIST[project["sensitivity_policy"]]
        if sensitivity_class not in allowed:
            raise PromotionPolicyError(
                "project_sensitivity_policy",
                f"project policy {project['sensitivity_policy']!r} does not "
                f"allow {sensitivity_class!r} promotion",
                disposition="quarantine")


def check_project_memory_policy(destination: str,
                                project: Mapping[str, Any] | None) -> None:
    """Honor the registry's project memory scope before any write."""
    if project is None:
        return
    if project["status"] != "active":
        raise PromotionPolicyError(
            "project_not_active",
            f"project {project['project_id']!r} is {project['status']!r}",
            disposition="quarantine")
    policy = project["memory_policy"]
    if policy == "hot_allowed":
        return
    if policy == "warm_only" and destination == "memory:hot":
        raise PromotionPolicyError(
            "project_memory_policy",
            "warm_only project policy blocks hot-memory promotion",
            disposition="quarantine")
    if policy in ("project_only", "no_memory") and destination.startswith("memory:"):
        raise PromotionPolicyError(
            "project_memory_policy",
            f"{policy} project policy blocks memory promotion",
            disposition="quarantine")


def _value(item: Any, key: str) -> Any:
    if isinstance(item, Mapping):
        return item.get(key)
    return item[key]


def _polarity(text: str) -> bool | None:
    words = set(re.findall(r"[a-z0-9]+", text.lower()))
    positive = bool(words & _POLARITY_TRUE)
    negative = bool(words & _POLARITY_FALSE)
    if positive == negative:
        return None
    return positive


def derive_active_contradictions(subject: str, claim: str,
                                 active_candidates: Iterable[Any]) -> list[str]:
    """Find deterministic same-subject polarity contradictions.

    This is intentionally narrow and explainable.  It supplements, never
    replaces, classifier-supplied conflict sets; other semantic conflicts
    continue to quarantine via the reviewed relationship workflow.
    """
    new_subject = normalized_claim_text(subject)
    new_polarity = _polarity(claim)
    if not new_subject or new_polarity is None:
        return []
    matches = []
    for existing in active_candidates:
        if normalized_claim_text(str(_value(existing, "canonical_subject"))) != new_subject:
            continue
        if _polarity(str(_value(existing, "normalized_claim"))) is not None \
                and _polarity(str(_value(existing, "normalized_claim"))) != new_polarity:
            matches.append(str(_value(existing, "candidate_id")))
    return sorted(set(matches))


def check_budget(destination: str, rendered: Mapping, *,
                 hot_companion: str = "") -> None:
    """§9 size budgets on the *resulting* destination files.

    ``hot_companion`` is the destination text that shares the hot budget but
    is not a write target — the injected USER.md.  The §9 cap is the combined
    hot USER.md + MEMORY.md size, not the index alone.
    """
    companion_tokens = estimate_tokens(hot_companion) if hot_companion else 0
    for path, content in rendered.items():
        tokens = estimate_tokens(content)
        if destination == "memory:hot" and path.name == "MEMORY.md" \
                and tokens + companion_tokens > HOT_MEMORY_TOKEN_CAP:
            raise PromotionPolicyError(
                "hot_memory_budget",
                f"combined hot USER.md + MEMORY.md would be "
                f"{tokens + companion_tokens} tokens "
                f"(cap {HOT_MEMORY_TOKEN_CAP}); demote or trim first",
                disposition="quarantine")
        if destination.startswith("skill:") and path.name == "SKILL.md" \
                and tokens > PROJECT_SKILL_TOKEN_CAP:
            raise PromotionPolicyError(
                "project_skill_budget",
                f"skill file would be {tokens} tokens "
                f"(cap {PROJECT_SKILL_TOKEN_CAP}); compact the skill first",
                disposition="quarantine")


@dataclass(frozen=True)
class ConflictResolutions:
    """Explicit, reviewed relationships for conflicting candidates (§11).

    Maps conflicting candidate_id -> 'supersedes' | 'scoped_exception'.
    Anything conflicting without an explicit relationship quarantines; the
    pipeline never merges contradictions on similarity alone.
    """
    relationships: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for cid, rel in self.relationships.items():
            if rel not in CONFLICT_RELATIONSHIPS:
                raise ValueError(
                    f"conflict relationship for {cid} must be one of "
                    f"{CONFLICT_RELATIONSHIPS}, got {rel!r}")


def check_conflicts(conflict_set: Iterable[str],
                    conflict_statuses: Mapping[str, str | None],
                    resolutions: ConflictResolutions) -> list[str]:
    """Returns the candidate ids this promotion supersedes.

    A conflict against an *active* (promoted/validated) claim blocks
    promotion unless an explicit relationship exists. 'scoped_exception'
    lets both stand; 'supersedes' promotes and retires the old claim.
    """
    supersede: list[str] = []
    for cid in sorted(set(conflict_set)):
        status = conflict_statuses.get(cid)
        relationship = resolutions.relationships.get(cid)
        if relationship == "supersedes":
            supersede.append(cid)
            continue
        if relationship == "scoped_exception":
            continue
        if status in ("promoted", "validated"):
            raise PromotionPolicyError(
                "unresolved_conflict",
                f"conflicts with active candidate {cid} and no explicit "
                "supersedes/scoped_exception relationship was provided",
                disposition="quarantine")
        # Conflict with a non-active candidate is informational only.
    return supersede
