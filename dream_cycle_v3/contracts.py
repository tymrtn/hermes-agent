"""Validators for the v3 machine contract (dream-cycle-v3-schemas.json).

Hand-rolled to stay standard-library-only. Each validator returns a list of
error strings (empty = valid) and mirrors the JSON Schema in
dream_cycle_v3/contracts/dream-cycle-v3-schemas.json: required keys, closed
key sets, enums, lengths, patterns, ranges, and the two openThread
conditionals (done -> closure_proof, linked -> external_task_ref).
"""
from __future__ import annotations

import re
from datetime import date, datetime
from pathlib import PurePath
from typing import Any, Callable, Mapping

from .canonical import SAFE_IDENTITY_RE
from .errors import ContractViolation

# Longest identity we accept.  Real ids are <=40 chars; a generous cap keeps a
# runaway id from bloating a managed marker region past its byte bound.
_MAX_IDENTITY_LEN = 128

ISO_DATETIME_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$")
ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
# ``\Z`` (not ``$``) anchors the true end: Python's ``$`` also matches just
# before a trailing newline, which would silently widen the grammar past the
# packaged JSON Schema's true-end pattern.
PROJECT_ID_RE = re.compile(r"[a-z0-9][a-z0-9_-]{1,63}\Z")
_UNSAFE_PROMOTION_TEXT = ("\r", "\n", "<!--", "-->", "\x00")


def _is_strict_int(value: Any) -> bool:
    """True for real integers only.  ``bool`` is a subclass of ``int`` in
    Python but a distinct type in JSON Schema, so parity demands excluding it
    everywhere the machine contract says ``integer``."""
    return isinstance(value, int) and not isinstance(value, bool)


def parse_iso_datetime(value: str) -> datetime:
    """Parse an ISO-8601 datetime, accepting the JSON-Schema-standard ``Z``
    suffix on every supported interpreter (datetime.fromisoformat only
    learned ``Z`` in Python 3.11). Single normalization point for the whole
    package — manifest validation and CLI parsing use this too.

    Raises ValueError for anything that is not a real calendar datetime.
    """
    if not isinstance(value, str):
        raise ValueError(f"not a string: {value!r}")
    if value.endswith(("Z", "z")):
        value = value[:-1] + "+00:00"
    return datetime.fromisoformat(value)


def is_iso_datetime(value: str) -> bool:
    """Shape and semantics: 2026-99-99T99:99:99Z matches no calendar."""
    if not isinstance(value, str) or not ISO_DATETIME_RE.match(value):
        return False
    try:
        parse_iso_datetime(value)
    except ValueError:
        return False
    return True


def is_iso_date(value: str) -> bool:
    if not isinstance(value, str) or not ISO_DATE_RE.match(value):
        return False
    try:
        date.fromisoformat(value)
    except ValueError:
        return False
    return True

SOURCE_TYPES = ("session", "task", "git", "cron", "log", "file",
                "user_confirmation", "live_probe")
CANDIDATE_CLASSES = ("runtime_memory_hot", "runtime_memory_warm", "project_context",
                     "task_thread", "reference_knowledge", "decision_record",
                     "ephemeral", "quarantine")
CANDIDATE_STATUSES = ("observed", "classified", "routed", "validated", "promoted",
                      "rejected", "superseded", "quarantined", "expired")
FRESHNESS_CLASSES = ("ephemeral", "days", "weeks", "months", "durable",
                     "live_verify_each_use")
SENSITIVITY_CLASSES = ("normal", "personal", "sensitive", "legal", "medical",
                       "financial", "credential_forbidden")
PROJECT_STATUSES = ("active", "dormant", "archived")
TASK_PROVIDERS = ("kanban", "github", "todoist", "project_tracker", "none")
WRITE_POLICIES = ("read_only", "preauthorized", "approval_required")
MEMORY_POLICIES = ("hot_allowed", "warm_only", "project_only", "no_memory")
SENSITIVITY_POLICIES = ("normal", "sensitive", "legal", "medical", "financial",
                        "credentials")
THREAD_STATES = ("observed", "triaged", "queued", "active", "blocked", "waiting",
                 "done", "dismissed", "stale")
TERMINAL_THREAD_STATES = ("done", "dismissed")
LINK_DISPOSITIONS = ("linked", "needs_link", "not_actionable", "ephemeral",
                     "quarantined")
CLOSURE_PROOF_KINDS = ("task_event", "commit", "pull_request", "decision",
                       "user_confirmation", "verified_evidence")
CLASSIFIER_KINDS = ("deterministic", "llm")


def _check_keys(obj: dict, required: tuple[str, ...], optional: tuple[str, ...],
                errors: list[str], where: str) -> bool:
    ok = True
    for k in required:
        if k not in obj:
            errors.append(f"{where}: missing required key '{k}'")
            ok = False
    allowed = set(required) | set(optional)
    for k in obj:
        if k not in allowed:
            errors.append(f"{where}: unknown key '{k}'")
            ok = False
    return ok


def _str(obj: dict, key: str, errors: list[str], where: str, *,
         min_len: int = 1, max_len: int | None = None, nullable: bool = False,
         enum: tuple[str, ...] | None = None, pattern: re.Pattern[str] | None = None,
         checker: Callable[[str], bool] | None = None) -> None:
    if key not in obj:
        return
    v = obj[key]
    if v is None:
        if not nullable:
            errors.append(f"{where}.{key}: may not be null")
        return
    if not isinstance(v, str):
        errors.append(f"{where}.{key}: must be a string")
        return
    if len(v) < min_len:
        errors.append(f"{where}.{key}: shorter than {min_len}")
    if max_len is not None and len(v) > max_len:
        errors.append(f"{where}.{key}: longer than {max_len}")
    if enum is not None and v not in enum:
        errors.append(f"{where}.{key}: '{v}' not in {enum}")
    if pattern is not None and not pattern.match(v):
        errors.append(f"{where}.{key}: does not match {pattern.pattern}")
    if checker is not None and not checker(v):
        errors.append(f"{where}.{key}: '{v}' is not a valid value")


def _string_array(obj: dict, key: str, errors: list[str], where: str) -> None:
    if key not in obj:
        return
    v = obj[key]
    if not isinstance(v, list) or not all(isinstance(s, str) and s for s in v):
        errors.append(f"{where}.{key}: must be an array of non-empty strings")
        return
    if len(set(v)) != len(v):
        errors.append(f"{where}.{key}: items must be unique")


def is_transcript_evidence(ref: Mapping[str, Any]) -> bool:
    """True when an evidence ref points at a session transcript.

    Mirrors the manifest heuristics: caller-controlled ``source_type`` alone
    is not trusted — a session-prefixed source ID or a session-flavored
    location component independently classifies the ref as transcript.
    """
    if ref.get("source_type") == "session":
        return True
    source_prefix = str(ref.get("source_id", "")).split(":", 1)[0].lower()
    if "session" in source_prefix:
        return True
    location_parts = {part.lower()
                      for part in PurePath(str(ref.get("location") or "")).parts}
    return any("session" in part for part in location_parts)


def validate_evidence_ref(obj: Any, where: str = "evidence_ref") -> list[str]:
    errors: list[str] = []
    if not isinstance(obj, dict):
        return [f"{where}: must be an object"]
    _check_keys(obj, ("source_type", "source_id", "observed_at", "fingerprint"),
                ("location", "excerpt"), errors, where)
    _str(obj, "source_type", errors, where, enum=SOURCE_TYPES)
    _str(obj, "source_id", errors, where)
    _str(obj, "location", errors, where, nullable=True, min_len=0)
    _str(obj, "observed_at", errors, where, checker=is_iso_datetime)
    _str(obj, "fingerprint", errors, where, min_len=16)
    _str(obj, "excerpt", errors, where, nullable=True, min_len=0, max_len=1000)
    # Same backstop as manifest validation: transcript-derived evidence is
    # metadata-only, so a session ref carrying an excerpt is structurally
    # invalid at the candidate/thread boundary too.
    if is_transcript_evidence(obj) and obj.get("excerpt") is not None:
        errors.append(f"{where}: session evidence must never carry an "
                      "excerpt (transcript containment)")
    return errors


def validate_project(obj: Any) -> list[str]:
    errors: list[str] = []
    where = "project"
    if not isinstance(obj, dict):
        return [f"{where}: must be an object"]
    required = ("schema_version", "project_id", "canonical_name", "aliases",
                "status", "owner", "task_ssot", "context_skill_id",
                "memory_policy", "sensitivity_policy", "registry_version",
                "last_verified_at")
    optional = ("scope_keywords", "canonical_paths", "repositories", "retrieval_terms")
    if not _check_keys(obj, required, optional, errors, where):
        return errors
    if not _is_strict_int(obj["schema_version"]) or obj["schema_version"] != 1:
        errors.append(f"{where}.schema_version: must be 1")
    _str(obj, "project_id", errors, where, pattern=PROJECT_ID_RE)
    _str(obj, "canonical_name", errors, where, max_len=160)
    for key in ("aliases", "scope_keywords", "canonical_paths", "repositories",
                "retrieval_terms"):
        _string_array(obj, key, errors, where)
    _str(obj, "status", errors, where, enum=PROJECT_STATUSES)
    _str(obj, "owner", errors, where)
    ssot = obj["task_ssot"]
    if not isinstance(ssot, dict):
        errors.append(f"{where}.task_ssot: must be an object")
    else:
        _check_keys(ssot, ("provider", "locator"), ("write_policy",), errors,
                    f"{where}.task_ssot")
        _str(ssot, "provider", errors, f"{where}.task_ssot", enum=TASK_PROVIDERS)
        _str(ssot, "locator", errors, f"{where}.task_ssot", nullable=True)
        _str(ssot, "write_policy", errors, f"{where}.task_ssot", enum=WRITE_POLICIES)
    _str(obj, "context_skill_id", errors, where, nullable=True)
    _str(obj, "memory_policy", errors, where, enum=MEMORY_POLICIES)
    _str(obj, "sensitivity_policy", errors, where, enum=SENSITIVITY_POLICIES)
    if not (_is_strict_int(obj["registry_version"]) and obj["registry_version"] >= 1):
        errors.append(f"{where}.registry_version: must be an integer >= 1")
    _str(obj, "last_verified_at", errors, where, checker=is_iso_datetime)
    return errors


def validate_candidate(obj: Any) -> list[str]:
    errors: list[str] = []
    where = "candidate"
    if not isinstance(obj, dict):
        return [f"{where}: must be an object"]
    required = ("schema_version", "candidate_id", "content_revision", "class",
                "destination", "normalized_claim", "canonical_subject",
                "evidence_refs", "confidence", "freshness_class",
                "sensitivity_class", "dedupe_key", "status",
                "validation_requirements", "conflict_set", "provenance")
    optional = ("project_id", "retrieval_terms", "semantic_cluster_id")
    if not _check_keys(obj, required, optional, errors, where):
        return errors
    if not _is_strict_int(obj["schema_version"]) or obj["schema_version"] != 1:
        errors.append(f"{where}.schema_version: must be 1")
    _str(obj, "candidate_id", errors, where, min_len=16,
         max_len=_MAX_IDENTITY_LEN, pattern=SAFE_IDENTITY_RE)
    if not (_is_strict_int(obj["content_revision"]) and obj["content_revision"] >= 1):
        errors.append(f"{where}.content_revision: must be an integer >= 1")
    _str(obj, "class", errors, where, enum=CANDIDATE_CLASSES)
    _str(obj, "project_id", errors, where, nullable=True)
    _str(obj, "destination", errors, where)
    _str(obj, "normalized_claim", errors, where, max_len=4000)
    _str(obj, "canonical_subject", errors, where, max_len=300)
    # Only adapter-backed candidate destinations render these fields as
    # Markdown.  Quarantine/ledger candidates remain evidence records and may
    # preserve a structural source excerpt without becoming write-capable.
    if str(obj.get("destination", "")).startswith(("memory:", "skill:", "project:")):
        for key in ("normalized_claim", "canonical_subject"):
            value = obj.get(key)
            if isinstance(value, str):
                if any(token in value for token in _UNSAFE_PROMOTION_TEXT):
                    errors.append(
                        f"{where}.{key}: contains a reserved DC3 or structural delimiter")
                if value.lstrip().startswith(("#", "---")):
                    errors.append(
                        f"{where}.{key}: starts a Markdown heading or frontmatter delimiter")
    _string_array(obj, "retrieval_terms", errors, where)
    refs = obj["evidence_refs"]
    if not (isinstance(refs, list) and len(refs) >= 1):
        errors.append(f"{where}.evidence_refs: must be a non-empty array")
    else:
        for i, ref in enumerate(refs):
            errors.extend(validate_evidence_ref(ref, f"{where}.evidence_refs[{i}]"))
    conf = obj["confidence"]
    if not (isinstance(conf, (int, float)) and not isinstance(conf, bool)
            and 0 <= conf <= 1):
        errors.append(f"{where}.confidence: must be a number in [0,1]")
    _str(obj, "freshness_class", errors, where, enum=FRESHNESS_CLASSES)
    _str(obj, "sensitivity_class", errors, where, enum=SENSITIVITY_CLASSES)
    _str(obj, "dedupe_key", errors, where, min_len=16,
         max_len=_MAX_IDENTITY_LEN, pattern=SAFE_IDENTITY_RE)
    _str(obj, "semantic_cluster_id", errors, where, nullable=True)
    _str(obj, "status", errors, where, enum=CANDIDATE_STATUSES)
    _string_array(obj, "validation_requirements", errors, where)
    _string_array(obj, "conflict_set", errors, where)
    prov = obj["provenance"]
    if not isinstance(prov, dict):
        errors.append(f"{where}.provenance: must be an object")
    else:
        pw = f"{where}.provenance"
        _check_keys(prov, ("run_id", "collector_version", "classifier_kind",
                           "classifier_version"), ("model", "prompt_hash"), errors, pw)
        _str(prov, "run_id", errors, pw, min_len=16,
             max_len=_MAX_IDENTITY_LEN, pattern=SAFE_IDENTITY_RE)
        _str(prov, "collector_version", errors, pw, min_len=0)
        _str(prov, "classifier_kind", errors, pw, enum=CLASSIFIER_KINDS)
        _str(prov, "classifier_version", errors, pw, min_len=0)
        _str(prov, "model", errors, pw, nullable=True)
        _str(prov, "prompt_hash", errors, pw, nullable=True)
        # An LLM-classified candidate without model + prompt_hash is not
        # auditable; the fields are nullable only for deterministic rules.
        if prov.get("classifier_kind") == "llm":
            for key in ("model", "prompt_hash"):
                value = prov.get(key)
                if not isinstance(value, str) or not value.strip():
                    errors.append(
                        f"{pw}.{key}: required for llm-classified candidates")
    return errors


def validate_closure_proof(obj: Any, where: str = "closure_proof") -> list[str]:
    errors: list[str] = []
    if not isinstance(obj, dict):
        return [f"{where}: must be an object"]
    _check_keys(obj, ("kind", "reference", "verified_at"), (), errors, where)
    _str(obj, "kind", errors, where, enum=CLOSURE_PROOF_KINDS)
    _str(obj, "reference", errors, where)
    _str(obj, "verified_at", errors, where, checker=is_iso_datetime)
    return errors


def validate_thread(obj: Any) -> list[str]:
    errors: list[str] = []
    where = "thread"
    if not isinstance(obj, dict):
        return [f"{where}: must be an object"]
    required = ("schema_version", "thread_id", "project_id", "title",
                "normalized_next_action", "owner", "state", "link_disposition",
                "opened_from", "evidence_refs", "last_disposition_date",
                "idempotency_key")
    optional = ("external_task_ref", "disposition_reason", "blocked_by", "due_hint",
                "follow_up_after", "closure_proof", "supersedes_thread_id")
    if not _check_keys(obj, required, optional, errors, where):
        return errors
    if not _is_strict_int(obj["schema_version"]) or obj["schema_version"] != 1:
        errors.append(f"{where}.schema_version: must be 1")
    _str(obj, "thread_id", errors, where, min_len=16,
         max_len=_MAX_IDENTITY_LEN, pattern=SAFE_IDENTITY_RE)
    _str(obj, "project_id", errors, where)
    _str(obj, "external_task_ref", errors, where, nullable=True)
    _str(obj, "link_disposition", errors, where, enum=LINK_DISPOSITIONS)
    _str(obj, "title", errors, where, max_len=300)
    _str(obj, "normalized_next_action", errors, where, max_len=1000)
    _str(obj, "owner", errors, where)
    _str(obj, "state", errors, where, enum=THREAD_STATES)
    _str(obj, "opened_from", errors, where)
    refs = obj["evidence_refs"]
    if not (isinstance(refs, list) and len(refs) >= 1):
        errors.append(f"{where}.evidence_refs: must be a non-empty array")
    else:
        for i, ref in enumerate(refs):
            errors.extend(validate_evidence_ref(ref, f"{where}.evidence_refs[{i}]"))
    _str(obj, "last_disposition_date", errors, where, checker=is_iso_date)
    _str(obj, "disposition_reason", errors, where, nullable=True)
    _str(obj, "blocked_by", errors, where, nullable=True)
    _str(obj, "due_hint", errors, where, nullable=True, checker=is_iso_datetime)
    _str(obj, "follow_up_after", errors, where, nullable=True, checker=is_iso_datetime)
    _str(obj, "supersedes_thread_id", errors, where, nullable=True)
    _str(obj, "idempotency_key", errors, where, min_len=16,
         max_len=_MAX_IDENTITY_LEN, pattern=SAFE_IDENTITY_RE)
    proof = obj.get("closure_proof")
    if proof is not None:
        errors.extend(validate_closure_proof(proof, f"{where}.closure_proof"))
    if obj.get("state") == "done" and proof is None:
        errors.append(f"{where}: state 'done' requires closure_proof")
    if obj.get("link_disposition") == "linked" and not obj.get("external_task_ref"):
        errors.append(f"{where}: link_disposition 'linked' requires external_task_ref")
    if obj.get("state") in ("blocked", "waiting"):
        if not obj.get("blocked_by"):
            errors.append(f"{where}: state '{obj.get('state')}' requires blocked_by")
        if not obj.get("follow_up_after"):
            errors.append(f"{where}: state '{obj.get('state')}' requires follow_up_after")
    return errors


def validate_receipt(obj: Any) -> list[str]:
    """writeReceipt validator — Phase 2 consumes it; kept here so the contract is complete."""
    errors: list[str] = []
    where = "receipt"
    if not isinstance(obj, dict):
        return [f"{where}: must be an object"]
    required = ("receipt_id", "candidate_id", "destination", "adapter",
                "target_revision_before", "target_revision_after", "backup_ref",
                "written_at", "read_back_verified", "retrieval_verified",
                "idempotency_key")
    # Retrieval and rollback metadata are kept optional in the wire contract
    # for Phase-2 schema compatibility.  ContinuityStore.promote_candidate
    # requires both concrete retrieval proof and structured rollback data at
    # the actual promotion boundary.
    optional = ("retrieval_proof", "rollback_command", "rollback_metadata")
    if not _check_keys(obj, required, optional, errors, where):
        return errors
    _str(obj, "receipt_id", errors, where, min_len=16,
         max_len=_MAX_IDENTITY_LEN, pattern=SAFE_IDENTITY_RE)
    _str(obj, "candidate_id", errors, where, min_len=16,
         max_len=_MAX_IDENTITY_LEN, pattern=SAFE_IDENTITY_RE)
    _str(obj, "destination", errors, where)
    _str(obj, "adapter", errors, where)
    _str(obj, "target_revision_before", errors, where, nullable=True)
    _str(obj, "target_revision_after", errors, where)
    _str(obj, "backup_ref", errors, where)
    _str(obj, "written_at", errors, where, checker=is_iso_datetime)
    for key in ("read_back_verified", "retrieval_verified"):
        if obj[key] is not True:
            errors.append(f"{where}.{key}: must be literal true")
    _str(obj, "idempotency_key", errors, where, min_len=16,
         max_len=_MAX_IDENTITY_LEN, pattern=SAFE_IDENTITY_RE)
    _str(obj, "retrieval_proof", errors, where)
    _str(obj, "rollback_command", errors, where, nullable=True)
    metadata = obj.get("rollback_metadata")
    if metadata is not None and not isinstance(metadata, dict):
        errors.append(f"{where}.rollback_metadata: must be an object")
    return errors


_VALIDATORS = {
    "project": validate_project,
    "candidate": validate_candidate,
    "thread": validate_thread,
    "receipt": validate_receipt,
    "evidence_ref": validate_evidence_ref,
}


def require_valid(kind: str, obj: Any) -> Any:
    errors = _VALIDATORS[kind](obj)
    if errors:
        raise ContractViolation(kind, errors)
    return obj
