"""Promotion orchestrator (design §6): validated candidate -> receipt.

Order of operations is the §6 promotion algorithm, and the candidate is
marked promoted only after every later step has passed.  Every step below
runs under one destination lock, held from policy evaluation through receipt
persistence, so destination-dependent decisions can never go stale between
check and write:

1-4. deterministic policies (transcript containment, provenance, duplicates,
     conflicts, leakage, budget) — a refusal transitions the candidate to
     rejected/quarantined, writes nothing;
5.   snapshot + backup of the destination targets into an immutable,
     collision-refusing per-attempt namespace;
6.   bounded, optimistic, atomic adapter write (a concurrent edit made after
     the backup raises and nothing is written);
7-8. production-compatible read-back, then the intended retrieval route —
     any failure restores the backup byte-identically and (by policy)
     quarantines the candidate;
9-10. write receipt persisted, candidate flipped to 'promoted', and every
     required supersession applied in ONE store transaction, emitting
     candidate_promoted. A database-level idempotency refusal (another
     candidate already wrote this record revision) also rolls the
     destination back.

Reruns are no-ops: an already-promoted candidate whose stored receipt
matches the recomputed idempotency key returns 'unchanged' without touching
the destination.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .adapters.destinations import (DestinationAdapter, DestinationHomes,
                                    PromotionRecord, adapter_for_destination,
                                    allocate_backup_dir)
from .canonical import (is_safe_identity, record_key_for, stable_id,
                        write_idempotency_key)
from .errors import (CandidateBindingError, CandidateStateError,
                     ConcurrentRevisionError, DestinationError,
                     IdempotencyError, PromotionPolicyError, ReadBackError,
                     RetrievalProofError, StoreError)
from .policies import (ConflictResolutions, check_budget, check_conflicts,
                       check_hot_memory_leakage, check_llm_provenance,
                       check_project_memory_policy,
                       check_sensitivity_and_authority,
                       check_transcript_containment,
                       check_validation_requirements,
                       derive_active_contradictions, find_duplicate)
from .store import ContinuityStore

# Longest run identity we will use as a backup namespace path component.
_MAX_RUN_ID_LEN = 128

RetrievalProber = Callable[[DestinationAdapter, PromotionRecord], str]

PROMOTABLE_STATUSES = ("routed", "validated")


@dataclass(frozen=True)
class PromotionResult:
    candidate_id: str
    content_revision: int
    destination: str
    outcome: str                 # promoted|unchanged|rejected|quarantined|revision_conflict
    reason: str | None = None
    receipt_id: str | None = None
    read_back_proof: str | None = None
    retrieval_proof: str | None = None
    rolled_back: bool = False
    superseded: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "content_revision": self.content_revision,
            "destination": self.destination,
            "outcome": self.outcome,
            "reason": self.reason,
            "receipt_id": self.receipt_id,
            "rolled_back": self.rolled_back,
            "superseded": list(self.superseded),
        }


def _field(candidate: Any, key: str) -> Any:
    if isinstance(candidate, sqlite3.Row):
        return candidate[key]
    return candidate[key]


def _json_field(candidate: Any, key: str) -> list:
    value = _field(candidate, key)
    if isinstance(value, str):
        return json.loads(value)
    return list(value or [])


def _run_id_of(candidate: Any) -> str:
    if isinstance(candidate, sqlite3.Row):
        return candidate["run_id"]
    return candidate["provenance"]["run_id"]


# Columns immutable for a given (candidate_id, content_revision).  A caller
# object that disagrees with the stored row on any of them is refused before
# anything is decided or written; lifecycle-mutable columns (status,
# semantic_cluster_id) are deliberately absent so idempotent reruns with the
# original ingest dict keep working.
_BOUND_SCALAR_COLUMNS = (
    "schema_version", "class", "project_id", "destination",
    "normalized_claim", "canonical_subject", "dedupe_key",
    "freshness_class", "sensitivity_class")
_BOUND_JSON_COLUMNS = ("retrieval_terms", "evidence_refs",
                       "validation_requirements", "conflict_set")
_BOUND_PROVENANCE_COLUMNS = ("run_id", "collector_version", "classifier_kind",
                             "classifier_version", "model", "prompt_hash")


def load_authoritative_candidate(store: ContinuityStore,
                                 candidate: Any) -> sqlite3.Row:
    """Load the stored candidate row and refuse any caller content drift.

    Promotion trusts the caller object for exactly one thing: naming the
    stored row via (candidate_id, content_revision).  Every byte that reaches
    a destination, policy, receipt, routing decision, provenance marker, or
    supersession is derived from the returned row.  As defense in depth, any
    promotion-relevant field the caller object also carries must equal the
    stored value byte-for-byte — a mismatch raises CandidateBindingError
    before any state is read for a decision, so a confused or malicious
    caller can never mark row A promoted while writing substituted content.
    """
    cid = _field(candidate, "candidate_id")
    rev = _field(candidate, "content_revision")
    if not isinstance(cid, str) or not cid:
        raise StoreError(f"candidate_id {cid!r} is not a non-empty string")
    if not isinstance(rev, int) or isinstance(rev, bool) or rev < 1:
        raise StoreError(
            f"candidate {cid} content_revision {rev!r} is not a positive "
            "integer")
    row = store.get_candidate(cid, rev)
    if row is None:
        raise StoreError(f"unknown candidate ({cid}, rev {rev})")

    drift: list[str] = []
    if isinstance(candidate, sqlite3.Row):
        present = set(candidate.keys())
        for key in (_BOUND_SCALAR_COLUMNS + _BOUND_PROVENANCE_COLUMNS
                    + ("confidence", "content_fingerprint")):
            if key in present and candidate[key] != row[key]:
                drift.append(key)
        for key in _BOUND_JSON_COLUMNS:
            if key in present and _json_field(candidate, key) != _json_field(row, key):
                drift.append(key)
    else:
        for key in _BOUND_SCALAR_COLUMNS:
            if key in candidate and candidate[key] != row[key]:
                drift.append(key)
        for key in _BOUND_JSON_COLUMNS:
            if key in candidate and _json_field(candidate, key) != _json_field(row, key):
                drift.append(key)
        if "confidence" in candidate and (
                float(candidate["confidence"]) != row["confidence"]):
            drift.append("confidence")
        provenance = candidate.get("provenance")
        if isinstance(provenance, dict):
            for key in _BOUND_PROVENANCE_COLUMNS:
                if key in provenance and provenance[key] != row[key]:
                    drift.append(f"provenance.{key}")
    if drift:
        raise CandidateBindingError(
            f"caller candidate ({cid}, rev {rev}) does not match its stored "
            f"row on: {', '.join(sorted(drift))}; the stored candidate row is "
            "authoritative and promotion refuses caller-substituted content")
    return row


def build_record(candidate: Any) -> PromotionRecord:
    destination = _field(candidate, "destination")
    subject = _field(candidate, "canonical_subject")
    return PromotionRecord(
        candidate_id=_field(candidate, "candidate_id"),
        content_revision=int(_field(candidate, "content_revision")),
        destination=destination,
        record_key=record_key_for(destination, subject),
        subject=subject,
        claim=_field(candidate, "normalized_claim"),
        retrieval_terms=tuple(_json_field(candidate, "retrieval_terms")),
        run_id=_run_id_of(candidate),
    )


def promote_candidate_via_adapter(
        store: ContinuityStore, candidate: Any, adapter: DestinationAdapter, *,
        backup_root: Path | str, now: str, run_id: str | None = None,
        resolutions: ConflictResolutions | None = None,
        retrieval_prober: RetrievalProber | None = None,
        quarantine_on_proof_failure: bool = True) -> PromotionResult:
    # The stored row is the single promotion authority: the caller object only
    # names it, and every destination byte, policy input, receipt field, and
    # supersession below derives from the row (any caller content drift raises
    # before this line returns).
    row = load_authoritative_candidate(store, candidate)
    record = build_record(row)
    if adapter.destination != record.destination:
        raise StoreError(
            f"adapter serves {adapter.destination!r}, candidate targets "
            f"{record.destination!r}")
    resolutions = resolutions or ConflictResolutions()
    run_id = run_id or record.run_id
    # The run id becomes a backup namespace path component; the safe-identity
    # grammar (no separators, no dots-only segments, no whitespace) makes
    # traversal impossible before any filesystem effect.
    if not is_safe_identity(run_id) or len(run_id) > _MAX_RUN_ID_LEN:
        raise StoreError(
            f"promotion run_id {run_id!r} is not a safe identity; refusing "
            "to use it as a backup namespace component")
    cid, rev = record.candidate_id, record.content_revision
    status = row["status"]

    if status == "promoted":
        existing = store.receipt_for_candidate(cid, rev)
        expected_key = write_idempotency_key(record.destination,
                                             record.record_key, rev)
        if existing is not None and existing["idempotency_key"] == expected_key:
            return PromotionResult(cid, rev, record.destination, "unchanged",
                                   reason="already_promoted",
                                   receipt_id=existing["receipt_id"])
        raise CandidateStateError(
            f"candidate {cid} rev {rev} is promoted but its receipt does not "
            "match this record identity")
    if status not in PROMOTABLE_STATUSES:
        raise CandidateStateError(
            f"candidate {cid} rev {rev} is '{status}'; promotion accepts "
            f"only {PROMOTABLE_STATUSES}")

    def _disposition(exc: PromotionPolicyError,
                     cluster_id: str | None = None) -> PromotionResult:
        target = "rejected" if exc.disposition == "reject" else "quarantined"
        store.transition_candidate(cid, rev, target,
                                   reason=f"{exc.reason}: {exc}", now=now,
                                   run_id=run_id,
                                   semantic_cluster_id=cluster_id)
        return PromotionResult(cid, rev, record.destination, target,
                               reason=exc.reason)

    # -- steps 1-10 run under ONE destination lock ---------------------------
    # Duplicate, conflict, leakage, and budget decisions all depend on
    # destination state, so they are evaluated under the same lock that stays
    # held through backup, write, and receipt persistence.  A concurrent
    # promotion therefore always observes the previous one's committed
    # receipt and destination bytes — there is no decide-then-write window.
    backup_root = Path(backup_root).resolve()

    def _receipt_exists(candidate_id: object, content_revision: object) -> bool:
        return (isinstance(candidate_id, str)
                and isinstance(content_revision, int)
                and store.receipt_for_candidate(candidate_id, content_revision)
                is not None)

    with adapter.destination_lock():
        adapter.recover_pending_writes(backup_root, _receipt_exists)

        # -- steps 1-4: deterministic policies, nothing written --------------
        match = None
        try:
            requirements = _json_field(row, "validation_requirements")
            evidence_refs = _json_field(row, "evidence_refs")
            check_transcript_containment(evidence_refs)
            check_llm_provenance(row["classifier_kind"], row["model"],
                                 row["prompt_hash"])
            project = None
            if row["project_id"] is not None:
                project = store.get_project(row["project_id"])
                if project is None:
                    raise PromotionPolicyError(
                        "project_not_registered",
                        f"candidate project {row['project_id']!r} is not in the registry",
                        disposition="quarantine")
            check_sensitivity_and_authority(
                sensitivity_class=row["sensitivity_class"],
                validation_requirements=requirements, evidence_refs=evidence_refs,
                project=project)
            check_project_memory_policy(record.destination, project)
            check_validation_requirements(requirements, evidence_refs, row["class"])
            check_hot_memory_leakage(record.destination, record.claim,
                                     row["class"], subject=record.subject)
            match = find_duplicate(record.claim, adapter.existing_records(),
                                   own_record_key=record.record_key)
            if match is not None:
                if match.kind == "exact":
                    raise PromotionPolicyError(
                        "exact_duplicate",
                        f"claim already present at {match.matched_location}",
                        disposition="reject")
                raise PromotionPolicyError(
                    "near_duplicate",
                    f"claim is {match.similarity:.2f} similar to "
                    f"{match.matched_location}; merge requires review",
                    disposition="quarantine")
            supplied_conflicts = _json_field(row, "conflict_set")
            # Contradictions derive over the adapter's full shared policy
            # scope: hot and warm memory share one home-wide fact set, so a
            # promoted claim in either tier can contradict this candidate.
            active_scope: list = []
            for scoped_destination in adapter.policy_scope_destinations():
                active_scope.extend(
                    store.active_promoted_candidates(scoped_destination))
            derived_conflicts = derive_active_contradictions(
                record.subject, record.claim, active_scope)
            conflict_set = sorted(set(supplied_conflicts) | set(derived_conflicts))
            statuses = {}
            for other_id in conflict_set:
                other = store.get_candidate(other_id)
                statuses[other_id] = other["status"] if other is not None else None
            # The relationship is durable evidence, not an ephemeral caller hint.
            # Detected contradictions are recorded even when this attempt then
            # quarantines for lack of an explicit resolution.
            store.record_conflict_relationships(
                cid, rev, conflict_set, relationships=resolutions.relationships,
                now=now, run_id=run_id)
            supersede = check_conflicts(conflict_set, statuses, resolutions)
            rendered = adapter.render(record)   # pure; nothing on disk yet
            check_budget(record.destination, rendered,
                         hot_companion=adapter.budget_context(record))
        except PromotionPolicyError as exc:
            cluster = None
            if exc.reason == "near_duplicate" and match is not None:
                cluster = match.matched_record_key
            return _disposition(exc, cluster)

        if status == "routed":
            store.transition_candidate(cid, rev, "validated",
                                       reason="promotion policies passed",
                                       now=now, run_id=run_id)

        # Required supersessions are resolved to concrete (id, revision)
        # pairs here, under the lock, and committed atomically with the
        # receipt inside store.promote_candidate.
        supersede_targets = []
        for other_id in supersede:
            other = store.get_candidate(other_id)
            if other is not None and other["status"] in ("promoted", "validated"):
                supersede_targets.append((other_id, other["content_revision"]))

        # -- steps 5-10: snapshot, backup, write, proofs, receipt -------------
        # A recovery journal makes a process death after a replace
        # recoverable; every caught post-backup error below restores the
        # pre-write bytes.  Each attempt gets an immutable, collision-refusing
        # backup namespace: no retry or later revision can overwrite an
        # earlier receipt's rollback evidence.
        expected_revision = adapter.snapshot_revision(record)
        # Every namespace component below the explicit backup root is
        # allocated with descriptor-pinned mkdir/openat O_NOFOLLOW, so a
        # planted symlink at any depth refuses before any byte is copied;
        # backup() re-walks the same confinement with the root it is given.
        backup = adapter.backup(
            record,
            allocate_backup_dir(backup_root, run_id, record.record_key,
                                f"rev-{rev}"),
            backup_root=backup_root)
        try:
            revision_after = adapter.apply_write(record, expected_revision,
                                                 backup=backup)
        except ConcurrentRevisionError as exc:
            # A rejected final CAS can follow earlier successful targets.
            # Reconcile the per-target journal first: restore only entries
            # whose inode/fingerprint still proves they are ours, preserve the
            # conflicting editor bytes, and then make the journal terminal.
            adapter.abort_pending_write(backup, conflict=True)
            return PromotionResult(cid, rev, record.destination,
                                   "revision_conflict", reason=str(exc))
        except BaseException as exc:
            adapter.abort_pending_write(backup)
            store.transition_candidate(
                cid, rev, "quarantined", reason=f"destination_write_failed: {exc}",
                now=now, run_id=run_id)
            return PromotionResult(cid, rev, record.destination, "quarantined",
                                   reason="destination_write_failed",
                                   rolled_back=True)

        try:
            backup.mark_state("written")
            read_back_proof = adapter.read_back(record)
            if retrieval_prober is not None:
                retrieval_proof = retrieval_prober(adapter, record)
            else:
                retrieval_proof = adapter.retrieval_proof(record)
            if not isinstance(retrieval_proof, str) or not retrieval_proof.strip():
                raise RetrievalProofError("retrieval proof was empty")
            backup.mark_state("verified")
        except BaseException as exc:
            adapter.abort_pending_write(backup)
            if isinstance(exc, ReadBackError):
                reason = "read_back_failed"
            elif isinstance(exc, RetrievalProofError):
                reason = "retrieval_proof_failed"
            else:
                reason = "post_write_verification_failed"
            if quarantine_on_proof_failure:
                store.transition_candidate(cid, rev, "quarantined",
                                           reason=f"{reason}: {exc}", now=now,
                                           run_id=run_id)
                outcome = "quarantined"
            else:
                outcome = "proof_failed"
            return PromotionResult(cid, rev, record.destination, outcome,
                                   reason=reason, rolled_back=True)

        receipt = {
            "receipt_id": stable_id("dream-cycle-v3-receipt", record.record_key,
                                    str(rev), cid),
            "candidate_id": cid,
            "destination": record.destination,
            "adapter": adapter.adapter_name,
            "target_revision_before": expected_revision,
            "target_revision_after": revision_after,
            "backup_ref": backup.ref,
            "written_at": now,
            "read_back_verified": True,
            "retrieval_verified": True,
            "retrieval_proof": retrieval_proof,
            "rollback_command": backup.rollback_command() or None,
            "rollback_metadata": backup.rollback_metadata(),
            "idempotency_key": write_idempotency_key(record.destination,
                                                     record.record_key, rev),
        }
        try:
            store.promote_candidate(
                cid, receipt, record_key=record.record_key, now=now,
                content_revision=rev, run_id=run_id, backup_root=backup_root,
                supersede=supersede_targets)
        except BaseException as exc:
            # Store receipt/transaction failures are no different from a
            # failed proof: a destination mutation without a receipt may not
            # survive the caught failure.
            adapter.abort_pending_write(backup)
            reason = ("db_idempotency_refused" if isinstance(exc, IdempotencyError)
                      else "store_promotion_failed")
            store.transition_candidate(cid, rev, "quarantined",
                                       reason=f"{reason}: {exc}", now=now,
                                       run_id=run_id)
            return PromotionResult(cid, rev, record.destination, "quarantined",
                                   reason=reason, rolled_back=True)

        # If marking this final journal state fails, recovery observes the
        # durable receipt and completes it; do not roll back a committed
        # receipt/destination pair.  The required supersessions committed in
        # the same store transaction as the receipt above.
        try:
            backup.mark_state("committed")
        except OSError:
            pass

        return PromotionResult(cid, rev, record.destination, "promoted",
                               receipt_id=receipt["receipt_id"],
                               read_back_proof=read_back_proof,
                               retrieval_proof=retrieval_proof,
                               superseded=tuple(
                                   oid for oid, _ in supersede_targets))


def promote_with_homes(store: ContinuityStore, candidate: Any,
                       homes: DestinationHomes, **kwargs) -> PromotionResult:
    # Adapter selection is routing: it must come from the stored row, never a
    # caller-substituted destination.  The loader also fails closed here on
    # any other caller content drift before an adapter (or lock) exists.
    row = load_authoritative_candidate(store, candidate)
    adapter = adapter_for_destination(row["destination"], homes)
    return promote_candidate_via_adapter(store, row, adapter, **kwargs)
