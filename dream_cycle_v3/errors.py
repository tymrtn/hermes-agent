"""Typed failure modes. Every error here is loud and actionable by design."""


class DreamCycleError(Exception):
    """Base for all Dream Cycle v3 errors."""


class RootResolutionError(DreamCycleError):
    """A collection or output root could not be resolved safely."""


class CollectionBoundError(DreamCycleError):
    """A collection bound was violated in a way that cannot be degraded."""


class ManifestConflictError(DreamCycleError):
    """An immutable manifest already exists with different content."""


class ManifestValidationError(DreamCycleError):
    """A manifest failed structural validation."""

    def __init__(self, errors: list[str]):
        self.errors = list(errors)
        super().__init__("; ".join(self.errors))


class ContractViolation(DreamCycleError):
    """An object failed validation against the v3 machine contract."""

    def __init__(self, kind: str, errors: list[str]):
        self.kind = kind
        self.errors = list(errors)
        super().__init__(f"{kind}: " + "; ".join(self.errors))


class IdempotencyError(DreamCycleError):
    """The same identity was presented with different content."""


class DispositionConflictError(DreamCycleError):
    """A second, different disposition was attempted for the same thread/date."""


class CarryForwardInvariantError(DreamCycleError):
    """A selected nonterminal thread ended the run without exactly one disposition."""


class RuntimeLockError(DreamCycleError):
    """The single-flight runtime lock could not be acquired safely."""


class StoreError(DreamCycleError):
    """Continuity store failure (migration, constraint, transaction)."""


class StoreOwnershipError(StoreError):
    """A writable open was attempted on a database that is not an owned v3 store."""


class CandidateStateError(StoreError):
    """A candidate transition or promotion violated the lifecycle state machine."""


class CandidateBindingError(StoreError):
    """A caller-supplied candidate object disagrees with its stored row.

    The stored (candidate_id, content_revision) row is the only promotion
    authority; a caller object whose content fields drift from that row is
    refused before any policy decision, destination byte, receipt, or status
    change can be influenced by the substituted values."""


class PromotionPolicyError(DreamCycleError):
    """A promotion policy (duplicate, conflict, leakage, budget) refused a write.

    `disposition` says what the pipeline must do with the candidate:
    'reject' (terminal) or 'quarantine' (held for review).
    """

    def __init__(self, reason: str, message: str, *, disposition: str):
        if disposition not in ("reject", "quarantine"):
            raise ValueError(f"bad policy disposition {disposition!r}")
        self.reason = reason
        self.disposition = disposition
        super().__init__(f"{reason}: {message}")


class DestinationError(DreamCycleError):
    """Base for destination-adapter write failures."""


class ConcurrentRevisionError(DestinationError):
    """The destination changed after the backup/revision snapshot was taken."""


class DiffBoundError(DestinationError):
    """A rendered write would change the destination outside its bounded region."""


class ReadBackError(DestinationError):
    """The production-compatible reader could not verify the written record."""


class RetrievalProofError(DestinationError):
    """The intended retrieval route failed to return the promoted record."""
