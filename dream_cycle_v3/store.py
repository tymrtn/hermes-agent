"""Continuity store: SQLite migrations plus transactional, idempotent operations.

Ownership boundary: a writable open is allowed only on (a) a missing/empty
path, which becomes a new v3 store stamped with our SQLite application_id, or
(b) an existing database carrying that application_id. Anything else — a
Kanban/task database, an arbitrary SQLite file, a non-SQLite file — is
refused via file-header inspection *before* any connection exists, so not
even a WAL-mode switch can touch a foreign database.

Idempotency is enforced by the database, never by caller discipline:
- primary keys / UNIQUE on every natural identity (run_id, dedupe_key,
  idempotency_key, (thread_id, disposition_date), event_key, snapshot identity);
- CHECK constraints mirror the machine contract (done needs closure proof,
  blocked/waiting need blocker + follow-up);
- events and thread_dispositions are append-only via BEFORE UPDATE/DELETE
  triggers that RAISE(ABORT);
- every mutating method performs its read-decide-write inside one
  BEGIN IMMEDIATE transaction (concurrent callers serialize and receive the
  same typed outcomes: 'inserted' / 'unchanged' / typed conflict error);
- nested use joins the enclosing transaction, so a whole carry-forward run
  commits or rolls back as one unit.

Wall-clock time never originates here: callers pass `now` explicitly,
including for migrations.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Iterable, Mapping

from . import STORE_SCHEMA_TARGET
from .canonical import (canonical_json, fingerprint_obj, record_key_for,
                        sha256_hex, stable_id, write_idempotency_key)
from .contracts import (TERMINAL_THREAD_STATES, is_iso_date, is_iso_datetime,
                        is_transcript_evidence, require_valid,
                        validate_closure_proof)
from .errors import (CandidateStateError, ContractViolation,
                     DispositionConflictError, IdempotencyError, StoreError,
                     StoreOwnershipError)
from .manifest import require_valid_manifest
from .sanitize import sanitize_text

# "DC3S" — stamps continuity.db as an owned Dream Cycle v3 store.
V3_APPLICATION_ID = 0x44433353
_SQLITE_MAGIC = b"SQLite format 3\x00"

DISPOSITION_ACTIONS = ("close_done", "close_dismissed", "blocked", "waiting",
                       "continue", "defer", "stale_review", "needs_link",
                       "authority_gated")

_MIGRATIONS: list[tuple[int, str, str]] = [
    (1, "phase 1 continuity schema", """
CREATE TABLE runs(
    run_id TEXT PRIMARY KEY,
    profile TEXT NOT NULL,
    window_start TEXT NOT NULL,
    window_end TEXT NOT NULL,
    collector_version TEXT NOT NULL,
    manifest_fingerprint TEXT NOT NULL,
    manifest_path TEXT NOT NULL,
    generated_at TEXT NOT NULL,
    recorded_at TEXT NOT NULL
);

CREATE TABLE projects(
    project_id TEXT PRIMARY KEY,
    schema_version INTEGER NOT NULL DEFAULT 1,
    canonical_name TEXT NOT NULL,
    aliases TEXT NOT NULL DEFAULT '[]',
    scope_keywords TEXT NOT NULL DEFAULT '[]',
    canonical_paths TEXT NOT NULL DEFAULT '[]',
    repositories TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL CHECK (status IN ('active','dormant','archived')),
    owner TEXT NOT NULL,
    task_provider TEXT NOT NULL
        CHECK (task_provider IN ('kanban','github','todoist','project_tracker','none')),
    task_locator TEXT,
    task_write_policy TEXT NOT NULL DEFAULT 'read_only'
        CHECK (task_write_policy IN ('read_only','preauthorized','approval_required')),
    context_skill_id TEXT,
    memory_policy TEXT NOT NULL
        CHECK (memory_policy IN ('hot_allowed','warm_only','project_only','no_memory')),
    sensitivity_policy TEXT NOT NULL
        CHECK (sensitivity_policy IN ('normal','sensitive','legal','medical','financial','credentials')),
    retrieval_terms TEXT NOT NULL DEFAULT '[]',
    registry_version INTEGER NOT NULL CHECK (registry_version >= 1),
    last_verified_at TEXT NOT NULL,
    content_fingerprint TEXT NOT NULL
);

CREATE TABLE candidates(
    candidate_id TEXT NOT NULL,
    content_revision INTEGER NOT NULL CHECK (content_revision >= 1),
    schema_version INTEGER NOT NULL DEFAULT 1,
    class TEXT NOT NULL CHECK (class IN (
        'runtime_memory_hot','runtime_memory_warm','project_context','task_thread',
        'reference_knowledge','decision_record','ephemeral','quarantine')),
    project_id TEXT REFERENCES projects(project_id),
    destination TEXT NOT NULL,
    normalized_claim TEXT NOT NULL,
    canonical_subject TEXT NOT NULL,
    retrieval_terms TEXT NOT NULL DEFAULT '[]',
    evidence_refs TEXT NOT NULL,
    confidence REAL NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
    freshness_class TEXT NOT NULL CHECK (freshness_class IN (
        'ephemeral','days','weeks','months','durable','live_verify_each_use')),
    sensitivity_class TEXT NOT NULL CHECK (sensitivity_class IN (
        'normal','personal','sensitive','legal','medical','financial','credential_forbidden')),
    dedupe_key TEXT NOT NULL,
    semantic_cluster_id TEXT,
    status TEXT NOT NULL CHECK (status IN (
        'observed','classified','routed','validated','promoted','rejected',
        'superseded','quarantined','expired')),
    validation_requirements TEXT NOT NULL DEFAULT '[]',
    conflict_set TEXT NOT NULL DEFAULT '[]',
    run_id TEXT NOT NULL REFERENCES runs(run_id),
    collector_version TEXT NOT NULL,
    classifier_kind TEXT NOT NULL CHECK (classifier_kind IN ('deterministic','llm')),
    classifier_version TEXT NOT NULL,
    model TEXT,
    prompt_hash TEXT,
    content_fingerprint TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (candidate_id, content_revision)
);
CREATE UNIQUE INDEX ux_candidates_dedupe_key ON candidates(dedupe_key);

CREATE TABLE threads(
    thread_id TEXT PRIMARY KEY,
    schema_version INTEGER NOT NULL DEFAULT 1,
    project_id TEXT NOT NULL REFERENCES projects(project_id),
    external_task_ref TEXT,
    link_disposition TEXT NOT NULL CHECK (link_disposition IN (
        'linked','needs_link','not_actionable','ephemeral','quarantined')),
    title TEXT NOT NULL,
    normalized_next_action TEXT NOT NULL,
    owner TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN (
        'observed','triaged','queued','active','blocked','waiting','done','dismissed','stale')),
    opened_from TEXT NOT NULL,
    evidence_refs TEXT NOT NULL,
    last_disposition_date TEXT NOT NULL,
    disposition_reason TEXT,
    blocked_by TEXT,
    due_hint TEXT,
    follow_up_after TEXT,
    closure_proof TEXT,
    supersedes_thread_id TEXT,
    idempotency_key TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK (state <> 'done' OR closure_proof IS NOT NULL),
    CHECK (link_disposition <> 'linked' OR external_task_ref IS NOT NULL),
    CHECK (state NOT IN ('blocked','waiting')
           OR (blocked_by IS NOT NULL AND follow_up_after IS NOT NULL))
);

CREATE TABLE thread_dispositions(
    disposition_id TEXT PRIMARY KEY,
    thread_id TEXT NOT NULL REFERENCES threads(thread_id),
    disposition_date TEXT NOT NULL,
    run_id TEXT NOT NULL REFERENCES runs(run_id),
    action TEXT NOT NULL CHECK (action IN (
        'close_done','close_dismissed','blocked','waiting','continue','defer',
        'stale_review','needs_link','authority_gated')),
    reason TEXT NOT NULL,
    blocker TEXT,
    follow_up_after TEXT,
    closure_proof TEXT,
    state_before TEXT NOT NULL,
    state_after TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (thread_id, disposition_date),
    CHECK (action <> 'close_done' OR closure_proof IS NOT NULL),
    CHECK (action NOT IN ('blocked','waiting')
           OR (blocker IS NOT NULL AND follow_up_after IS NOT NULL))
);
CREATE TRIGGER thread_dispositions_no_update BEFORE UPDATE ON thread_dispositions
BEGIN SELECT RAISE(ABORT, 'thread_dispositions is append-only'); END;
CREATE TRIGGER thread_dispositions_no_delete BEFORE DELETE ON thread_dispositions
BEGIN SELECT RAISE(ABORT, 'thread_dispositions is append-only'); END;

CREATE TABLE events(
    event_seq INTEGER PRIMARY KEY AUTOINCREMENT,
    event_key TEXT NOT NULL UNIQUE,
    entity_type TEXT NOT NULL CHECK (entity_type IN (
        'run','project','candidate','thread','disposition','adapter')),
    entity_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    payload TEXT NOT NULL DEFAULT '{}',
    run_id TEXT REFERENCES runs(run_id),
    created_at TEXT NOT NULL
);
CREATE TRIGGER events_no_update BEFORE UPDATE ON events
BEGIN SELECT RAISE(ABORT, 'events is append-only'); END;
CREATE TRIGGER events_no_delete BEFORE DELETE ON events
BEGIN SELECT RAISE(ABORT, 'events is append-only'); END;

CREATE TABLE adapter_snapshots(
    snapshot_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(run_id),
    adapter TEXT NOT NULL,
    source_locator TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('ok','unavailable','error')),
    detail TEXT,
    items TEXT NOT NULL DEFAULT '[]',
    fingerprint TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (run_id, adapter, source_locator)
);
"""),
    (2, "phase 2 write receipts (promotion)", """
CREATE TABLE write_receipts(
    receipt_id TEXT PRIMARY KEY,
    candidate_id TEXT NOT NULL,
    content_revision INTEGER NOT NULL CHECK (content_revision >= 1),
    destination TEXT NOT NULL,
    adapter TEXT NOT NULL,
    record_key TEXT NOT NULL,
    target_revision_before TEXT,
    target_revision_after TEXT NOT NULL,
    backup_ref TEXT NOT NULL,
    written_at TEXT NOT NULL,
    read_back_verified INTEGER NOT NULL CHECK (read_back_verified = 1),
    retrieval_verified INTEGER NOT NULL CHECK (retrieval_verified = 1),
    retrieval_proof TEXT,
    rollback_command TEXT,
    idempotency_key TEXT NOT NULL UNIQUE,
    run_id TEXT REFERENCES runs(run_id),
    content_fingerprint TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (candidate_id, content_revision)
        REFERENCES candidates(candidate_id, content_revision)
);
CREATE UNIQUE INDEX ux_write_receipts_candidate
    ON write_receipts(candidate_id, content_revision);
CREATE UNIQUE INDEX ux_write_receipts_record_revision
    ON write_receipts(record_key, content_revision);
CREATE TRIGGER write_receipts_no_update BEFORE UPDATE ON write_receipts
BEGIN SELECT RAISE(ABORT, 'write_receipts is append-only'); END;
CREATE TRIGGER write_receipts_no_delete BEFORE DELETE ON write_receipts
BEGIN SELECT RAISE(ABORT, 'write_receipts is append-only'); END;
"""),
    (3, "phase 2 promotion recovery metadata and conflicts", """
ALTER TABLE write_receipts ADD COLUMN rollback_metadata TEXT;

CREATE TABLE candidate_conflicts(
    candidate_id TEXT NOT NULL,
    content_revision INTEGER NOT NULL,
    conflicting_candidate_id TEXT NOT NULL,
    conflicting_content_revision INTEGER NOT NULL,
    relationship TEXT NOT NULL CHECK (relationship IN (
        'unresolved','supersedes','scoped_exception')),
    detected_at TEXT NOT NULL,
    reviewed_at TEXT,
    PRIMARY KEY (candidate_id, content_revision,
                 conflicting_candidate_id, conflicting_content_revision),
    FOREIGN KEY (candidate_id, content_revision)
        REFERENCES candidates(candidate_id, content_revision),
    FOREIGN KEY (conflicting_candidate_id, conflicting_content_revision)
        REFERENCES candidates(candidate_id, content_revision)
);
CREATE TRIGGER candidate_conflicts_no_delete BEFORE DELETE ON candidate_conflicts
BEGIN SELECT RAISE(ABORT, 'candidate_conflicts is append-only'); END;
"""),
    # The Phase 3 read paths build filesystem paths from stored project ids,
    # so the schema mirrors contracts.PROJECT_ID_RE ([a-z0-9][a-z0-9_-]{1,63})
    # for writes that bypass the contract layer. SQLite cannot ALTER TABLE
    # ADD CHECK, hence BEFORE INSERT/UPDATE guard triggers. The explicit
    # IS NULL arm is load-bearing: projects is a rowid table, so its TEXT
    # PRIMARY KEY accepts NULL, and under three-valued logic a bare
    # WHEN NOT (...) is NULL (not true) for a NULL id — the trigger would
    # never fire.
    (4, "projects.project_id registry-grammar guard", """
CREATE TRIGGER projects_project_id_grammar_insert BEFORE INSERT ON projects
WHEN NEW.project_id IS NULL
     OR NOT (NEW.project_id GLOB '[a-z0-9]*'
             AND NEW.project_id NOT GLOB '*[^a-z0-9_-]*'
             AND length(NEW.project_id) BETWEEN 2 AND 64)
BEGIN SELECT RAISE(ABORT, 'projects.project_id violates the registry grammar'); END;

CREATE TRIGGER projects_project_id_grammar_update
BEFORE UPDATE OF project_id ON projects
WHEN NEW.project_id IS NULL
     OR NOT (NEW.project_id GLOB '[a-z0-9]*'
             AND NEW.project_id NOT GLOB '*[^a-z0-9_-]*'
             AND length(NEW.project_id) BETWEEN 2 AND 64)
BEGIN SELECT RAISE(ABORT, 'projects.project_id violates the registry grammar'); END;
"""),
]

# States a candidate may legitimately be born in.  Collection/classification
# produce 'observed' or 'classified'; the classifier may also park a candidate
# directly in 'quarantined' (quarantine-class evidence records).  Everything
# later in the lifecycle — routed, validated, promoted, rejected, superseded,
# expired — is reachable only through the audited transition/promotion APIs,
# so ingest refuses to import those states wholesale (a 'promoted' row without
# a receipt would otherwise appear by construction).
INGESTABLE_STATUSES = ("observed", "classified", "quarantined")

# Candidate lifecycle (design §6). 'promoted' is reachable ONLY through
# promote_candidate, which requires a verified receipt in the same
# transaction; transition_candidate refuses it.
CANDIDATE_TRANSITIONS: dict[str, frozenset[str]] = {
    "observed": frozenset({"classified", "quarantined", "rejected", "expired"}),
    "classified": frozenset({"routed", "quarantined", "rejected", "expired"}),
    "routed": frozenset({"validated", "quarantined", "rejected", "expired"}),
    "validated": frozenset({"quarantined", "rejected", "superseded", "expired"}),
    "promoted": frozenset({"superseded"}),
    "quarantined": frozenset({"classified", "routed", "validated", "rejected",
                              "expired"}),
    "rejected": frozenset(),
    "superseded": frozenset(),
    "expired": frozenset(),
}


def _check_now(now: str) -> str:
    if not is_iso_datetime(now):
        raise StoreError(f"now must be a valid ISO-8601 datetime, got {now!r}")
    return now


def inspect_store_identity(path: Path) -> str:
    """Classify a path before any SQLite connection is made.

    Returns 'fresh' (missing or empty file), or 'owned' (SQLite header carries
    the v3 application_id). Everything else raises StoreOwnershipError. This
    is a pure file read: foreign databases are never opened, so nothing —
    including journal-mode changes — can mutate them.
    """
    if path.exists() and not path.is_file():
        raise StoreOwnershipError(f"{path} exists and is not a regular file")
    try:
        size = path.stat().st_size
    except FileNotFoundError:
        return "fresh"
    if size == 0:
        return "fresh"
    try:
        with open(path, "rb") as fh:
            header = fh.read(100)
    except OSError as exc:
        raise StoreOwnershipError(f"cannot read {path}: {exc}") from None
    if len(header) < 100 or not header.startswith(_SQLITE_MAGIC):
        raise StoreOwnershipError(
            f"{path} is not a SQLite database; refusing to touch it")
    app_id = int.from_bytes(header[68:72], "big")
    if app_id != V3_APPLICATION_ID:
        raise StoreOwnershipError(
            f"{path} is a SQLite database with application_id "
            f"0x{app_id:08X}, not an owned v3 continuity store "
            f"(expected 0x{V3_APPLICATION_ID:08X}); refusing to open it "
            "writable — it may be a task/Kanban database")
    return "owned"


def assert_store_confined(path: Path, confine_root: Path) -> None:
    """Reject a continuity DB path outside its profile-owned root (Phase 3).

    The Phase 3 read paths bind a store to one profile's continuity home. A
    path that is not lexically below the root, crosses a symlink anywhere at
    or below the root, or resolves to a file outside the resolved root is
    another profile's (or an attacker-planted) database and is refused before
    any byte is read.
    """
    path = Path(path)
    root = Path(confine_root)
    try:
        rel = path.relative_to(root)
    except ValueError:
        raise StoreOwnershipError(
            f"{path} is outside the profile continuity root {root}") from None
    # relative_to is purely lexical: '<root>/../x' yields rel parts
    # ('..', 'x') and a nonexistent target would fall out of the walk below
    # before anything resolves. Dot components are therefore refused
    # outright — a confined path must name its target plainly.
    if any(part in ("..", ".") for part in rel.parts):
        raise StoreOwnershipError(
            f"{path} contains '.'/'..' components; refusing to resolve a "
            "confined store path through them")
    try:
        real_root = root.resolve()
    except OSError as exc:
        raise StoreOwnershipError(
            f"cannot resolve continuity root {root}: {exc}") from None
    current = root
    for part in rel.parts:
        current = current / part
        if current.is_symlink():
            raise StoreOwnershipError(
                f"{current} is a symlink; refusing to resolve the continuity "
                "store through it")
        if not current.exists():
            return
    try:
        real = path.resolve()
    except OSError as exc:
        raise StoreOwnershipError(f"cannot resolve {path}: {exc}") from None
    if not real.is_relative_to(real_root):
        raise StoreOwnershipError(
            f"{path} resolves to {real}, outside the profile continuity "
            f"root {real_root}")


def _safe_manifest_relative_path(value: object) -> bool:
    if not isinstance(value, str) or not value or value.startswith("/"):
        return False
    parts = Path(value).parts
    return bool(parts) and all(part not in ("", ".", "..") for part in parts)


def _valid_backup_home_identity(value: object) -> bool:
    """Validate the opaque canonical home binding carried by a backup.

    The store never resolves this path or writes to it; it only requires that
    the receipt and manifest bind to the same concrete explicit home.  Adapter
    recovery performs the live identity comparison before it can restore.
    """
    if not isinstance(value, dict) or set(value) != {
            "canonical_path", "device", "inode"}:
        return False
    path = value["canonical_path"]
    return (isinstance(path, str) and Path(path).is_absolute()
            and isinstance(value["device"], int)
            and not isinstance(value["device"], bool)
            and isinstance(value["inode"], int)
            and not isinstance(value["inode"], bool))


def _require_promotion_recovery_evidence(receipt: dict[str, Any],
                                         backup_root: Path | str | None) -> None:
    """Validate receipt proof and its constrained backup manifest.

    The store never follows target paths from receipt data.  It only reads a
    manifest directory below the caller's explicit shadow backup root, and the
    manifest contains destination-relative target identifiers rather than live
    paths.  This keeps a direct-store caller from smuggling arbitrary files
    into the promotion boundary.
    """
    proof = receipt.get("retrieval_proof")
    if not isinstance(proof, str) or not proof.strip():
        raise ContractViolation(
            "receipt", ["receipt.retrieval_proof must be non-empty at promotion"])
    metadata = receipt.get("rollback_metadata")
    if not isinstance(metadata, dict) or not metadata:
        raise ContractViolation(
            "receipt", ["receipt.rollback_metadata must be a non-empty object at promotion"])
    if backup_root is None:
        raise ContractViolation(
            "receipt", ["promotion requires an explicit allowed backup_root"])
    root = Path(backup_root).resolve()
    ref_value = receipt.get("backup_ref")
    ref = Path(ref_value) if isinstance(ref_value, str) else None
    if ref is None or not ref.is_absolute():
        raise ContractViolation(
            "receipt", ["receipt.backup_ref must be an absolute path under backup_root"])
    try:
        backup_dir = ref.resolve(strict=True)
    except OSError as exc:
        raise ContractViolation(
            "receipt", [f"receipt.backup_ref does not exist: {exc}"]) from None
    if not backup_dir.is_dir() or not backup_dir.is_relative_to(root):
        raise ContractViolation(
            "receipt", ["receipt.backup_ref is outside the allowed backup_root"])
    if (metadata.get("version") != 2
            or metadata.get("kind") != "dc3_filesystem_backup"
            or metadata.get("manifest") != "dc3-backup-manifest.json"
            or not _valid_backup_home_identity(metadata.get("home_identity"))
            or not isinstance(metadata.get("entries"), list)):
        raise ContractViolation("receipt", ["receipt.rollback_metadata has invalid shape"])
    manifest_path = backup_dir / "dc3-backup-manifest.json"
    if manifest_path.is_symlink():
        raise ContractViolation(
            "receipt", ["receipt backup manifest is a symlink; refusing to "
                        "read through it"])
    try:
        manifest_bytes = manifest_path.read_bytes()
        manifest = json.loads(manifest_bytes)
    except (OSError, ValueError) as exc:
        raise ContractViolation(
            "receipt", [f"receipt backup manifest is unreadable: {exc}"]) from None
    expected_fp = "sha256:" + sha256_hex(manifest_bytes)
    if metadata.get("manifest_fingerprint") != expected_fp:
        raise ContractViolation(
            "receipt", ["rollback metadata does not fingerprint its backup manifest"])
    if (not isinstance(manifest, dict)
            or manifest.get("version") != 2
            or manifest.get("kind") != "dc3_filesystem_backup"
            or manifest.get("destination") != receipt["destination"]
            or manifest.get("home_identity") != metadata["home_identity"]
            or manifest.get("entries") != metadata["entries"]
            or not manifest["entries"]):
        raise ContractViolation(
            "receipt", ["backup manifest does not match receipt rollback metadata"])
    for entry in manifest["entries"]:
        if not isinstance(entry, dict) or set(entry) != {
                "target", "existed", "backup_file", "fingerprint"}:
            raise ContractViolation("receipt", ["backup manifest entry has invalid shape"])
        if not _safe_manifest_relative_path(entry["target"]):
            raise ContractViolation(
                "receipt", ["backup manifest target must be a safe relative path"])
        existed = entry["existed"]
        name = entry["backup_file"]
        fingerprint = entry["fingerprint"]
        if not isinstance(existed, bool):
            raise ContractViolation("receipt", ["backup manifest existed must be boolean"])
        if existed:
            if (not isinstance(name, str) or Path(name).name != name
                    or not isinstance(fingerprint, str)
                    or not fingerprint.startswith("sha256:")):
                raise ContractViolation(
                    "receipt", ["backup manifest existing entry is not concrete"])
            saved = (backup_dir / name).resolve()
            if not saved.is_file() or not saved.is_relative_to(backup_dir):
                raise ContractViolation(
                    "receipt", ["backup manifest backup file is missing or escapes manifest"])
            if "sha256:" + sha256_hex(saved.read_bytes()) != fingerprint:
                raise ContractViolation(
                    "receipt", ["backup manifest fingerprint does not match backup file"])
        elif name is not None or fingerprint is not None:
            raise ContractViolation(
                "receipt", ["absent target must not name a backup file"])


class ContinuityStore:
    def __init__(self, path: Path | str, *, read_only: bool = False,
                 backup_root: Path | str | None = None):
        self.path = Path(path)
        self.read_only = read_only
        # The explicit promotion API passes its shadow backup root.  Direct
        # store callers without one are constrained to a deterministic sibling
        # of the owned continuity DB, never to a caller-supplied live path.
        self.backup_root = (Path(backup_root) if backup_root is not None
                            else self.path.parent / "backups").resolve()
        identity = inspect_store_identity(self.path)
        if read_only:
            if identity == "fresh":
                raise StoreError(f"no continuity store at {self.path}")
            self._conn = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True,
                                         isolation_level=None, timeout=10)
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA query_only = ON")
            return
        self._conn = sqlite3.connect(self.path, isolation_level=None, timeout=10)
        self._conn.row_factory = sqlite3.Row
        if identity == "fresh":
            self._conn.execute(f"PRAGMA application_id = {V3_APPLICATION_ID}")
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.execute("PRAGMA journal_mode = WAL")
        if identity == "owned":
            # A fresh store cannot create a receiptless promoted row (ingest,
            # transitions, and promotion all refuse), but a REUSED shadow
            # store may predate those guards or have been edited directly.
            # Audit before this writable handle can promote anything.
            self.audit_promotion_invariants()

    # -- lifecycle ---------------------------------------------------------
    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "ContinuityStore":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def transaction(self):
        """BEGIN IMMEDIATE transaction; nested use joins the enclosing one.

        BEGIN IMMEDIATE takes the write lock up front, making every
        read-decide-write block atomic against concurrent writers. Lock
        contention surfaces as a typed StoreError instead of a raw
        sqlite3.OperationalError.
        """
        conn = self._conn

        class _Tx:
            def __init__(self) -> None:
                self.joined = False

            def __enter__(self) -> sqlite3.Connection:
                if conn.in_transaction:
                    self.joined = True
                    return conn
                try:
                    conn.execute("BEGIN IMMEDIATE")
                except sqlite3.OperationalError as exc:
                    raise StoreError(f"store busy: {exc}") from None
                return conn

            def __exit__(self, exc_type, exc, tb) -> None:
                if self.joined:
                    return
                if exc_type is None:
                    conn.execute("COMMIT")
                else:
                    conn.execute("ROLLBACK")

        return _Tx()

    _tx = transaction

    # -- migrations --------------------------------------------------------
    def migrate(self, now: str) -> list[int]:
        _check_now(now)
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS schema_migrations(
                version INTEGER PRIMARY KEY,
                description TEXT NOT NULL,
                applied_at TEXT NOT NULL
            )""")
        applied = {r["version"] for r in
                   self._conn.execute("SELECT version FROM schema_migrations")}
        newly: list[int] = []
        for version, description, ddl in _MIGRATIONS:
            if version in applied:
                continue
            # executescript would implicitly commit a wrapper transaction, so
            # the script opens its own; the migration row joins it before COMMIT.
            try:
                self._conn.executescript("BEGIN IMMEDIATE;\n" + ddl)
                self._conn.execute(
                    "INSERT INTO schema_migrations(version, description, applied_at) "
                    "VALUES (?, ?, ?)",
                    (version, description, now))
                self._conn.execute("COMMIT")
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            newly.append(version)
        current = self.schema_version()
        if current != STORE_SCHEMA_TARGET:
            raise StoreError(
                f"schema version {current} != target {STORE_SCHEMA_TARGET}")
        return newly

    def schema_version(self) -> int:
        row = self._conn.execute(
            "SELECT MAX(version) AS v FROM schema_migrations").fetchone()
        return int(row["v"] or 0)

    # -- events ------------------------------------------------------------
    def _emit_event(self, conn: sqlite3.Connection, *, entity_type: str,
                    entity_id: str, event_type: str, payload: dict[str, Any],
                    run_id: str | None, now: str) -> None:
        payload_json = canonical_json(payload)
        event_key = stable_id("dream-cycle-v3-event", entity_type, entity_id,
                              event_type, payload_json, run_id or "")
        conn.execute(
            "INSERT OR IGNORE INTO events(event_key, entity_type, entity_id, "
            "event_type, payload, run_id, created_at) VALUES (?,?,?,?,?,?,?)",
            (event_key, entity_type, entity_id, event_type, payload_json,
             run_id, now))

    # -- runs ----------------------------------------------------------------
    def record_run(self, manifest: dict[str, Any], manifest_path: str,
                   now: str) -> str:
        # Full structural validation — run ID, fingerprints, self-consistency —
        # before anything is persisted. A forged manifest never reaches disk.
        require_valid_manifest(manifest)
        _check_now(now)
        with self._tx() as conn:
            existing = conn.execute(
                "SELECT manifest_fingerprint FROM runs WHERE run_id = ?",
                (manifest["run_id"],)).fetchone()
            if existing is not None:
                if existing["manifest_fingerprint"] != manifest["manifest_fingerprint"]:
                    raise IdempotencyError(
                        f"run {manifest['run_id']} already recorded with a "
                        "different manifest fingerprint")
                return "unchanged"
            conn.execute(
                "INSERT INTO runs(run_id, profile, window_start, window_end, "
                "collector_version, manifest_fingerprint, manifest_path, "
                "generated_at, recorded_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (manifest["run_id"], manifest["profile"],
                 manifest["window"]["start"], manifest["window"]["end"],
                 manifest["collector_version"], manifest["manifest_fingerprint"],
                 manifest_path, manifest["generated_at"], now))
            self._emit_event(conn, entity_type="run", entity_id=manifest["run_id"],
                             event_type="run_recorded",
                             payload={"manifest_fingerprint":
                                      manifest["manifest_fingerprint"]},
                             run_id=manifest["run_id"], now=now)
        return "inserted"

    # -- projects ------------------------------------------------------------
    @staticmethod
    def _project_fingerprint(entry: dict[str, Any]) -> str:
        return fingerprint_obj(entry)

    def upsert_project(self, entry: dict[str, Any], now: str,
                       run_id: str | None = None) -> str:
        require_valid("project", entry)
        _check_now(now)
        fp = self._project_fingerprint(entry)
        ssot = entry["task_ssot"]
        params = (
            entry["project_id"], entry["schema_version"], entry["canonical_name"],
            canonical_json(entry["aliases"]),
            canonical_json(entry.get("scope_keywords", [])),
            canonical_json(entry.get("canonical_paths", [])),
            canonical_json(entry.get("repositories", [])),
            entry["status"], entry["owner"],
            ssot["provider"], ssot["locator"],
            ssot.get("write_policy", "read_only"),
            entry["context_skill_id"], entry["memory_policy"],
            entry["sensitivity_policy"],
            canonical_json(entry.get("retrieval_terms", [])),
            entry["registry_version"], entry["last_verified_at"], fp,
        )
        with self._tx() as conn:
            row = conn.execute(
                "SELECT registry_version, content_fingerprint FROM projects "
                "WHERE project_id = ?", (entry["project_id"],)).fetchone()
            if row is not None:
                if row["content_fingerprint"] == fp:
                    return "unchanged"
                if entry["registry_version"] <= row["registry_version"]:
                    raise IdempotencyError(
                        f"project {entry['project_id']} registry_version "
                        f"{entry['registry_version']} does not supersede stored "
                        f"version {row['registry_version']} with different content")
            if row is None:
                conn.execute(
                    "INSERT INTO projects(project_id, schema_version, canonical_name, "
                    "aliases, scope_keywords, canonical_paths, repositories, status, "
                    "owner, task_provider, task_locator, task_write_policy, "
                    "context_skill_id, memory_policy, sensitivity_policy, "
                    "retrieval_terms, registry_version, last_verified_at, "
                    "content_fingerprint) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    params)
                action = "inserted"
            else:
                conn.execute(
                    "UPDATE projects SET schema_version=?, canonical_name=?, aliases=?, "
                    "scope_keywords=?, canonical_paths=?, repositories=?, status=?, "
                    "owner=?, task_provider=?, task_locator=?, task_write_policy=?, "
                    "context_skill_id=?, memory_policy=?, sensitivity_policy=?, "
                    "retrieval_terms=?, registry_version=?, last_verified_at=?, "
                    "content_fingerprint=? WHERE project_id=?",
                    params[1:] + (entry["project_id"],))
                action = "updated"
            self._emit_event(conn, entity_type="project",
                             entity_id=entry["project_id"],
                             event_type=f"project_{action}",
                             payload={"registry_version": entry["registry_version"],
                                      "content_fingerprint": fp},
                             run_id=run_id, now=now)
        return action

    def get_project(self, project_id: str) -> sqlite3.Row | None:
        return self._conn.execute(
            "SELECT * FROM projects WHERE project_id = ?", (project_id,)).fetchone()

    # -- candidates ------------------------------------------------------------
    def ingest_candidate(self, candidate: dict[str, Any], now: str,
                         routing: dict[str, Any] | None = None) -> str:
        require_valid("candidate", candidate)
        _check_now(now)
        if candidate["status"] not in INGESTABLE_STATUSES:
            # Fail closed before the transaction opens: no row, receipt, or
            # event may exist for an illegally imported lifecycle state.
            raise CandidateStateError(
                f"candidate {candidate['candidate_id']} arrives with status "
                f"{candidate['status']!r}; ingest accepts only "
                f"{INGESTABLE_STATUSES} — later states are reachable solely "
                "through transition_candidate/promote_candidate")
        fp = fingerprint_obj(candidate)
        prov = candidate["provenance"]
        with self._tx() as conn:
            existing = conn.execute(
                "SELECT candidate_id, content_revision, content_fingerprint "
                "FROM candidates WHERE dedupe_key = ?",
                (candidate["dedupe_key"],)).fetchone()
            if existing is not None:
                if (existing["candidate_id"] == candidate["candidate_id"]
                        and existing["content_revision"] == candidate["content_revision"]
                        and existing["content_fingerprint"] == fp):
                    return "unchanged"
                self._emit_event(
                    conn, entity_type="candidate",
                    entity_id=candidate["candidate_id"],
                    event_type="candidate_duplicate_rejected",
                    payload={"dedupe_key": candidate["dedupe_key"],
                             "kept_candidate_id": existing["candidate_id"],
                             "kept_content_revision": existing["content_revision"]},
                    run_id=prov["run_id"], now=now)
                return "duplicate_rejected"
            conn.execute(
                "INSERT INTO candidates(candidate_id, content_revision, schema_version, "
                "class, project_id, destination, normalized_claim, canonical_subject, "
                "retrieval_terms, evidence_refs, confidence, freshness_class, "
                "sensitivity_class, dedupe_key, semantic_cluster_id, status, "
                "validation_requirements, conflict_set, run_id, collector_version, "
                "classifier_kind, classifier_version, model, prompt_hash, "
                "content_fingerprint, created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (candidate["candidate_id"], candidate["content_revision"],
                 candidate["schema_version"], candidate["class"],
                 candidate.get("project_id"), candidate["destination"],
                 candidate["normalized_claim"], candidate["canonical_subject"],
                 canonical_json(candidate.get("retrieval_terms", [])),
                 canonical_json(candidate["evidence_refs"]),
                 float(candidate["confidence"]), candidate["freshness_class"],
                 candidate["sensitivity_class"], candidate["dedupe_key"],
                 candidate.get("semantic_cluster_id"), candidate["status"],
                 canonical_json(candidate["validation_requirements"]),
                 canonical_json(candidate["conflict_set"]),
                 prov["run_id"], prov["collector_version"],
                 prov["classifier_kind"], prov["classifier_version"],
                 prov.get("model"), prov.get("prompt_hash"), fp, now))
            payload = {"dedupe_key": candidate["dedupe_key"],
                       "content_revision": candidate["content_revision"],
                       "class": candidate["class"],
                       "content_fingerprint": fp}
            if routing is not None:
                payload["routing"] = routing
            self._emit_event(
                conn, entity_type="candidate", entity_id=candidate["candidate_id"],
                event_type=f"candidate_{candidate['status']}",
                payload=payload,
                run_id=prov["run_id"], now=now)
        return "inserted"

    def get_candidate(self, candidate_id: str,
                      content_revision: int | None = None) -> sqlite3.Row | None:
        if content_revision is None:
            return self._conn.execute(
                "SELECT * FROM candidates WHERE candidate_id = ? "
                "ORDER BY content_revision DESC LIMIT 1", (candidate_id,)).fetchone()
        return self._conn.execute(
            "SELECT * FROM candidates WHERE candidate_id = ? AND "
            "content_revision = ?", (candidate_id, content_revision)).fetchone()

    def active_promoted_candidates(self, destination: str) -> list[sqlite3.Row]:
        """Current promoted claims at a destination for conflict derivation."""
        return list(self._conn.execute(
            "SELECT * FROM candidates WHERE destination = ? AND status = 'promoted' "
            "ORDER BY candidate_id, content_revision", (destination,)))

    def record_conflict_relationships(
            self, candidate_id: str, content_revision: int,
            conflicting_candidate_ids: Iterable[str], *,
            relationships: Mapping[str, str], now: str,
            run_id: str | None = None) -> None:
        """Persist detected conflicts and any explicit reviewed relationship."""
        _check_now(now)
        with self._tx() as conn:
            for other_id in sorted(set(conflicting_candidate_ids) - {candidate_id}):
                other = self.get_candidate(other_id)
                if other is None:
                    continue
                relationship = relationships.get(other_id, "unresolved")
                if relationship not in ("unresolved", "supersedes", "scoped_exception"):
                    raise ContractViolation(
                        "conflict_relationship",
                        [f"unsupported relationship {relationship!r}"])
                existing = conn.execute(
                    "SELECT relationship FROM candidate_conflicts WHERE "
                    "candidate_id = ? AND content_revision = ? AND "
                    "conflicting_candidate_id = ? AND conflicting_content_revision = ?",
                    (candidate_id, content_revision, other_id,
                     other["content_revision"])).fetchone()
                if existing is None:
                    conn.execute(
                        "INSERT INTO candidate_conflicts(candidate_id, content_revision, "
                        "conflicting_candidate_id, conflicting_content_revision, "
                        "relationship, detected_at, reviewed_at) VALUES (?,?,?,?,?,?,?)",
                        (candidate_id, content_revision, other_id,
                         other["content_revision"], relationship, now,
                         now if relationship != "unresolved" else None))
                elif (existing["relationship"] == "unresolved"
                      and relationship != "unresolved"):
                    conn.execute(
                        "UPDATE candidate_conflicts SET relationship = ?, reviewed_at = ? "
                        "WHERE candidate_id = ? AND content_revision = ? AND "
                        "conflicting_candidate_id = ? AND conflicting_content_revision = ?",
                        (relationship, now, candidate_id, content_revision,
                         other_id, other["content_revision"]))
                self._emit_event(
                    conn, entity_type="candidate", entity_id=candidate_id,
                    event_type="candidate_conflict_recorded",
                    payload={"content_revision": content_revision,
                             "conflicting_candidate_id": other_id,
                             "conflicting_content_revision": other["content_revision"],
                             "relationship": relationship},
                    run_id=run_id, now=now)

    def transition_candidate(self, candidate_id: str, content_revision: int,
                             new_status: str, *, reason: str, now: str,
                             run_id: str | None = None,
                             semantic_cluster_id: str | None = None) -> str:
        """Audited lifecycle transition. 'promoted' is refused here by design:
        only promote_candidate — which demands a verified receipt in the same
        transaction — may reach it."""
        _check_now(now)
        if new_status == "promoted":
            raise CandidateStateError(
                "candidates reach 'promoted' only via promote_candidate "
                "with a verified write receipt")
        if not reason or not reason.strip():
            raise ContractViolation("candidate_transition",
                                    ["reason is required"])
        with self._tx() as conn:
            row = self.get_candidate(candidate_id, content_revision)
            if row is None:
                raise StoreError(
                    f"unknown candidate ({candidate_id}, rev {content_revision})")
            current = row["status"]
            if current == new_status and (
                    semantic_cluster_id is None
                    or semantic_cluster_id == row["semantic_cluster_id"]):
                return "unchanged"
            if new_status not in CANDIDATE_TRANSITIONS.get(current, frozenset()):
                raise CandidateStateError(
                    f"candidate {candidate_id} rev {content_revision}: "
                    f"illegal transition {current!r} -> {new_status!r}")
            conn.execute(
                "UPDATE candidates SET status = ?, semantic_cluster_id = "
                "COALESCE(?, semantic_cluster_id) WHERE candidate_id = ? AND "
                "content_revision = ? AND status = ?",
                (new_status, semantic_cluster_id, candidate_id,
                 content_revision, current))
            self._emit_event(
                conn, entity_type="candidate", entity_id=candidate_id,
                event_type="candidate_transition",
                payload={"content_revision": content_revision, "from": current,
                         "to": new_status, "reason": reason,
                         "semantic_cluster_id": semantic_cluster_id},
                run_id=run_id, now=now)
        return "transitioned"

    # -- promotion (Phase 2) -------------------------------------------------
    _RECEIPTLESS_PROMOTED_SQL = (
        "SELECT COUNT(*) AS c FROM candidates c LEFT JOIN write_receipts r "
        "ON c.candidate_id = r.candidate_id "
        "AND c.content_revision = r.content_revision "
        "WHERE c.status = 'promoted' AND r.receipt_id IS NULL")

    def _table_exists(self, name: str) -> bool:
        return self._conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (name,)).fetchone() is not None

    def audit_promotion_invariants(self) -> None:
        """Refuse to trust promoted rows that carry no write receipt.

        Runs on every writable open of an existing owned store and again at
        the start of every direct promotion.  A promoted candidate without a
        receipt cannot exist in a fresh v3 store (ingest refuses lifecycle
        import, transition_candidate refuses 'promoted', promote_candidate
        requires the verified receipt in the same transaction), so its
        presence proves a reused/tampered shadow store whose promoted state
        must be audited and repaired before any further promotion.
        """
        if not self._table_exists("candidates"):
            return
        if self._table_exists("write_receipts"):
            count = self._conn.execute(
                self._RECEIPTLESS_PROMOTED_SQL).fetchone()["c"]
        else:
            count = self._conn.execute(
                "SELECT COUNT(*) AS c FROM candidates "
                "WHERE status = 'promoted'").fetchone()["c"]
        if count:
            raise StoreError(
                f"store {self.path} holds {count} promoted candidate row(s) "
                "without a write receipt; a fresh v3 store cannot produce "
                "this state, so the reused store must be audited/repaired "
                "before promotion is allowed")

    def get_receipt(self, receipt_id: str) -> sqlite3.Row | None:
        return self._conn.execute(
            "SELECT * FROM write_receipts WHERE receipt_id = ?",
            (receipt_id,)).fetchone()

    def receipt_for_candidate(self, candidate_id: str, content_revision: int
                              ) -> sqlite3.Row | None:
        return self._conn.execute(
            "SELECT * FROM write_receipts WHERE candidate_id = ? AND "
            "content_revision = ?", (candidate_id, content_revision)).fetchone()

    def promote_candidate(self, candidate_id: str, receipt: dict[str, Any], *,
                          now: str, content_revision: int,
                          record_key: str | None = None,
                          run_id: str | None = None,
                          backup_root: Path | str | None = None,
                          supersede: Iterable[tuple[str, int]] = ()) -> str:
        """Atomically record a verified write receipt and mark the candidate
        promoted, emitting candidate_promoted — all in one transaction.

        The stored row is the record-identity authority at this boundary too:
        ``content_revision`` is required and must name the exact stored row
        (no latest-row fallback), and the record identity is derived here
        from the stored destination + canonical subject via
        ``record_key_for``.  A caller-supplied ``record_key`` is only
        cross-checked against that derivation and refused on any drift, so a
        direct caller can never bind row A's promoted state to a different
        record identity, however internally consistent its receipt is.

        The receipt must validate against the machine contract (which already
        requires read_back_verified/retrieval_verified to be literal true),
        belong to this candidate/destination, and carry the §11 idempotency
        key recomputed from (destination, derived record identity,
        content_revision) — a forged or stale key is refused before anything
        is written. Only a 'validated' candidate may promote; a failed proof
        therefore leaves the candidate unpromoted because no valid receipt
        can exist for it.

        ``supersede`` lists (candidate_id, content_revision) pairs whose
        supersession this promotion requires.  Each pair must already carry a
        durable reviewed 'supersedes' relationship in candidate_conflicts
        (recorded by the orchestrator's conflict step) — supersession is a
        capability generated from stored conflicts, never a free caller
        input, so a direct caller cannot retire unrelated rows.  They
        transition inside the same transaction as the receipt insert and
        status flip, so a crash or failure can never leave the new claim
        promoted while a contradictory old claim also remains promoted.
        """
        require_valid("receipt", receipt)
        _check_now(now)
        if (not isinstance(content_revision, int)
                or isinstance(content_revision, bool) or content_revision < 1):
            raise StoreError(
                "promotion requires an explicit positive integer "
                f"content_revision naming the stored row, got "
                f"{content_revision!r}")
        if receipt["candidate_id"] != candidate_id:
            raise ContractViolation(
                "receipt", [f"receipt.candidate_id {receipt['candidate_id']!r} "
                            f"does not match candidate {candidate_id!r}"])
        receipt_fp = fingerprint_obj(receipt)
        with self._tx() as conn:
            self.audit_promotion_invariants()
            candidate = self.get_candidate(candidate_id, content_revision)
            if candidate is None:
                raise StoreError(f"unknown candidate {candidate_id} "
                                 f"rev {content_revision}")
            revision = candidate["content_revision"]
            if candidate["destination"] != receipt["destination"]:
                raise ContractViolation(
                    "receipt",
                    [f"receipt.destination {receipt['destination']!r} does not "
                     f"match candidate destination {candidate['destination']!r}"])
            derived_key = record_key_for(candidate["destination"],
                                         candidate["canonical_subject"])
            if record_key is not None and record_key != derived_key:
                raise ContractViolation(
                    "receipt",
                    [f"caller record_key {record_key!r} does not equal the "
                     "record identity derived from the stored row's "
                     "destination and canonical subject; the stored row is "
                     "authoritative and promotion refuses caller identity "
                     "drift"])
            record_key = derived_key
            expected_key = write_idempotency_key(
                receipt["destination"], record_key, revision)
            if receipt["idempotency_key"] != expected_key:
                raise ContractViolation(
                    "receipt",
                    ["idempotency_key does not equal hash(destination, "
                     "record identity, content revision) per design §11"])
            _require_promotion_recovery_evidence(
                receipt, backup_root or self.backup_root)
            # Fail closed on unsafe provenance at the store boundary too: a
            # direct caller must not promote session-backed (transcript)
            # candidates or unauditable LLM output, regardless of what the
            # orchestrator checked.
            for ref in json.loads(candidate["evidence_refs"]):
                if isinstance(ref, dict) and is_transcript_evidence(ref):
                    raise ContractViolation(
                        "candidate",
                        ["session-derived evidence is metadata-only; a "
                         "session-backed candidate is never promoted "
                         "(transcript containment)"])
            if candidate["classifier_kind"] == "llm" and (
                    not candidate["model"] or not candidate["prompt_hash"]):
                raise ContractViolation(
                    "candidate",
                    ["llm-classified candidates require model and "
                     "prompt_hash provenance before promotion"])
            if candidate["status"] == "promoted":
                existing = self.receipt_for_candidate(candidate_id, revision)
                if existing is not None \
                        and existing["content_fingerprint"] == receipt_fp:
                    return "unchanged"
                raise IdempotencyError(
                    f"candidate {candidate_id} rev {revision} is already "
                    "promoted with a different receipt")
            if candidate["status"] != "validated":
                raise CandidateStateError(
                    f"candidate {candidate_id} rev {revision} is "
                    f"'{candidate['status']}'; only 'validated' candidates "
                    "may promote")
            try:
                conn.execute(
                    "INSERT INTO write_receipts(receipt_id, candidate_id, "
                    "content_revision, destination, adapter, record_key, "
                    "target_revision_before, target_revision_after, backup_ref, "
                    "written_at, read_back_verified, retrieval_verified, "
                    "retrieval_proof, rollback_command, rollback_metadata, "
                    "idempotency_key, run_id, content_fingerprint, created_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (receipt["receipt_id"], candidate_id, revision,
                     receipt["destination"], receipt["adapter"], record_key,
                     receipt["target_revision_before"],
                     receipt["target_revision_after"], receipt["backup_ref"],
                     receipt["written_at"], 1, 1,
                     receipt.get("retrieval_proof"),
                     receipt.get("rollback_command"),
                     canonical_json(receipt["rollback_metadata"]),
                     receipt["idempotency_key"], run_id, receipt_fp, now))
            except sqlite3.IntegrityError as exc:
                raise IdempotencyError(
                    f"receipt for record {record_key} rev {revision} at "
                    f"{receipt['destination']} conflicts with an existing "
                    f"receipt: {exc}") from None
            cur = conn.execute(
                "UPDATE candidates SET status = 'promoted' WHERE "
                "candidate_id = ? AND content_revision = ? AND "
                "status = 'validated'",
                (candidate_id, revision))
            if cur.rowcount != 1:
                raise StoreError(
                    f"candidate {candidate_id} rev {revision} changed state "
                    "concurrently during promotion")
            self._emit_event(
                conn, entity_type="candidate", entity_id=candidate_id,
                event_type="candidate_promoted",
                payload={"content_revision": revision,
                         "receipt_id": receipt["receipt_id"],
                         "record_key": record_key,
                         "destination": receipt["destination"],
                         "adapter": receipt["adapter"],
                         "idempotency_key": receipt["idempotency_key"],
                         "target_revision_after":
                             receipt["target_revision_after"],
                         "backup_ref": receipt["backup_ref"]},
                run_id=run_id, now=now)
            # Required supersessions join this transaction: either the
            # receipt, the promotion, and every supersession commit together,
            # or none of them do.
            for other_id, other_revision in supersede:
                if other_id == candidate_id:
                    continue
                other = self.get_candidate(other_id, other_revision)
                if other is None:
                    raise StoreError(
                        f"required supersession target ({other_id}, rev "
                        f"{other_revision}) does not exist")
                # Supersession authority is the durable reviewed conflict
                # relationship, never the caller's list by itself: without a
                # stored 'supersedes' row binding this promotion to that
                # exact target row, retiring it is refused.
                relationship = conn.execute(
                    "SELECT relationship FROM candidate_conflicts WHERE "
                    "candidate_id = ? AND content_revision = ? AND "
                    "conflicting_candidate_id = ? AND "
                    "conflicting_content_revision = ?",
                    (candidate_id, revision, other_id,
                     other_revision)).fetchone()
                if relationship is None or (
                        relationship["relationship"] != "supersedes"):
                    raise CandidateStateError(
                        f"required supersession target ({other_id}, rev "
                        f"{other_revision}) has no durable reviewed "
                        f"'supersedes' conflict relationship with "
                        f"({candidate_id}, rev {revision}); promotion never "
                        "supersedes unrelated rows")
                if other["status"] == "superseded":
                    continue
                if other["status"] not in ("promoted", "validated"):
                    raise CandidateStateError(
                        f"required supersession target {other_id} rev "
                        f"{other_revision} is '{other['status']}'; promotion "
                        "and its supersessions commit as one unit")
                if self.transition_candidate(
                        other_id, other_revision, "superseded",
                        reason=f"superseded_by:{candidate_id}", now=now,
                        run_id=run_id) != "transitioned":
                    raise StoreError(
                        f"could not supersede ({other_id}, rev "
                        f"{other_revision}) within the promotion transaction")
        return "inserted"

    # -- threads ------------------------------------------------------------
    def open_thread(self, thread: dict[str, Any], now: str,
                    run_id: str | None = None) -> str:
        require_valid("thread", thread)
        _check_now(now)
        with self._tx() as conn:
            existing = conn.execute(
                "SELECT thread_id FROM threads WHERE thread_id = ? OR idempotency_key = ?",
                (thread["thread_id"], thread["idempotency_key"])).fetchone()
            if existing is not None:
                # Re-observation never rewinds store state; transitions own changes.
                return "exists"
            conn.execute(
                "INSERT INTO threads(thread_id, schema_version, project_id, "
                "external_task_ref, link_disposition, title, normalized_next_action, "
                "owner, state, opened_from, evidence_refs, last_disposition_date, "
                "disposition_reason, blocked_by, due_hint, follow_up_after, "
                "closure_proof, supersedes_thread_id, idempotency_key, created_at, "
                "updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (thread["thread_id"], thread["schema_version"], thread["project_id"],
                 thread.get("external_task_ref"), thread["link_disposition"],
                 thread["title"], thread["normalized_next_action"], thread["owner"],
                 thread["state"], thread["opened_from"],
                 canonical_json(thread["evidence_refs"]),
                 thread["last_disposition_date"], thread.get("disposition_reason"),
                 thread.get("blocked_by"), thread.get("due_hint"),
                 thread.get("follow_up_after"),
                 canonical_json(thread["closure_proof"])
                 if thread.get("closure_proof") else None,
                 thread.get("supersedes_thread_id"), thread["idempotency_key"],
                 now, now))
            self._emit_event(conn, entity_type="thread",
                             entity_id=thread["thread_id"],
                             event_type="thread_opened",
                             payload={"state": thread["state"],
                                      "idempotency_key": thread["idempotency_key"]},
                             run_id=run_id, now=now)
        return "inserted"

    def get_thread(self, thread_id: str) -> sqlite3.Row | None:
        return self._conn.execute(
            "SELECT * FROM threads WHERE thread_id = ?", (thread_id,)).fetchone()

    def select_nonterminal_threads(self, project_ids: Iterable[str] | None = None
                                   ) -> list[sqlite3.Row]:
        placeholders = ",".join("?" for _ in TERMINAL_THREAD_STATES)
        sql = (f"SELECT * FROM threads WHERE state NOT IN ({placeholders})")
        params: list[Any] = list(TERMINAL_THREAD_STATES)
        if project_ids is not None:
            ids = sorted(set(project_ids))
            sql += " AND project_id IN (" + ",".join("?" for _ in ids) + ")"
            params.extend(ids)
        sql += " ORDER BY thread_id"
        return list(self._conn.execute(sql, params))

    # -- dispositions ------------------------------------------------------
    def get_disposition(self, thread_id: str, disposition_date: str
                        ) -> sqlite3.Row | None:
        return self._conn.execute(
            "SELECT * FROM thread_dispositions WHERE thread_id = ? AND "
            "disposition_date = ?", (thread_id, disposition_date)).fetchone()

    def record_disposition(self, *, thread_id: str, disposition_date: str,
                           run_id: str, action: str, reason: str,
                           state_after: str, now: str,
                           blocker: str | None = None,
                           follow_up_after: str | None = None,
                           closure_proof: dict[str, Any] | None = None) -> str:
        _check_now(now)
        if not is_iso_date(disposition_date):
            raise ContractViolation(
                "disposition",
                [f"disposition_date '{disposition_date}' is not a valid calendar date"])
        if action not in DISPOSITION_ACTIONS:
            raise ContractViolation("disposition", [f"unknown action '{action}'"])
        if not reason or not reason.strip():
            raise ContractViolation("disposition", ["reason is required"])
        if action == "close_done":
            if closure_proof is None:
                raise ContractViolation(
                    "disposition", ["close_done requires closure_proof"])
            errors = validate_closure_proof(closure_proof)
            if errors:
                raise ContractViolation("disposition", errors)
        if action in ("blocked", "waiting") and (not blocker or not follow_up_after):
            raise ContractViolation(
                "disposition",
                [f"action '{action}' requires blocker and follow_up_after"])
        if follow_up_after is not None and not is_iso_datetime(follow_up_after):
            raise ContractViolation(
                "disposition",
                [f"follow_up_after '{follow_up_after}' is not a valid "
                 "ISO-8601 datetime"])

        content = {
            "action": action, "reason": reason, "blocker": blocker,
            "follow_up_after": follow_up_after,
            "closure_proof": closure_proof, "state_after": state_after,
        }
        disposition_id = stable_id("dream-cycle-v3-disposition", thread_id,
                                   disposition_date)
        proof_json = canonical_json(closure_proof) if closure_proof else None
        with self._tx() as conn:
            thread = self.get_thread(thread_id)
            if thread is None:
                raise StoreError(f"unknown thread {thread_id}")
            state_before = thread["state"]

            existing = self.get_disposition(thread_id, disposition_date)
            if existing is not None:
                existing_content = {
                    "action": existing["action"], "reason": existing["reason"],
                    "blocker": existing["blocker"],
                    "follow_up_after": existing["follow_up_after"],
                    "closure_proof": json.loads(existing["closure_proof"])
                    if existing["closure_proof"] else None,
                    "state_after": existing["state_after"],
                }
                if existing_content == content:
                    return "unchanged"
                raise DispositionConflictError(
                    f"thread {thread_id} already has a different disposition for "
                    f"{disposition_date}")

            conn.execute(
                "INSERT INTO thread_dispositions(disposition_id, thread_id, "
                "disposition_date, run_id, action, reason, blocker, follow_up_after, "
                "closure_proof, state_before, state_after, created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (disposition_id, thread_id, disposition_date, run_id, action,
                 reason, blocker, follow_up_after, proof_json, state_before,
                 state_after, now))
            cur = conn.execute(
                "UPDATE threads SET state=?, last_disposition_date=?, "
                "disposition_reason=?, blocked_by=COALESCE(?, blocked_by), "
                "follow_up_after=COALESCE(?, follow_up_after), "
                "closure_proof=COALESCE(?, closure_proof), updated_at=? "
                "WHERE thread_id=? AND state=?",
                (state_after, disposition_date, reason, blocker, follow_up_after,
                 proof_json, now, thread_id, state_before))
            if cur.rowcount != 1:
                raise StoreError(
                    f"thread {thread_id} state changed concurrently "
                    f"(expected '{state_before}')")
            self._emit_event(conn, entity_type="disposition",
                             entity_id=disposition_id,
                             event_type="disposition_recorded",
                             payload={"thread_id": thread_id,
                                      "date": disposition_date, **content},
                             run_id=run_id, now=now)
            if state_after != state_before:
                self._emit_event(conn, entity_type="thread", entity_id=thread_id,
                                 event_type="thread_transition",
                                 payload={"from": state_before, "to": state_after,
                                          "date": disposition_date},
                                 run_id=run_id, now=now)
        return "inserted"

    # -- adapter snapshots ---------------------------------------------------
    def record_adapter_snapshot(self, *, run_id: str, adapter: str,
                                source_locator: str, status: str,
                                detail: str | None, items: list[dict[str, Any]],
                                now: str) -> str:
        _check_now(now)
        if status not in ("ok", "unavailable", "error"):
            raise StoreError(f"bad adapter status {status!r}")
        safe_locator = sanitize_text(source_locator)
        safe_detail = sanitize_text(detail) if detail is not None else None
        safe_items = [
            {str(key): (sanitize_text(value) if isinstance(value, str)
                        else value)
             for key, value in item.items()}
            for item in items
        ]
        items_json = canonical_json(safe_items)
        fp = fingerprint_obj({"status": status, "detail": safe_detail,
                              "items": safe_items})
        snapshot_id = stable_id("dream-cycle-v3-snapshot", run_id, adapter,
                                safe_locator)
        with self._tx() as conn:
            existing = conn.execute(
                "SELECT fingerprint FROM adapter_snapshots WHERE run_id=? AND "
                "adapter=? AND source_locator=?",
                (run_id, adapter, safe_locator)).fetchone()
            if existing is not None:
                if existing["fingerprint"] != fp:
                    raise IdempotencyError(
                        f"adapter snapshot ({run_id}, {adapter}, {safe_locator}) "
                        "already recorded with different content")
                return "unchanged"
            conn.execute(
                "INSERT INTO adapter_snapshots(snapshot_id, run_id, adapter, "
                "source_locator, status, detail, items, fingerprint, created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (snapshot_id, run_id, adapter, safe_locator, status, safe_detail,
                 items_json, fp, now))
            self._emit_event(conn, entity_type="adapter", entity_id=snapshot_id,
                             event_type=f"adapter_snapshot_{status}",
                             payload={"adapter": adapter,
                                      "source_locator": safe_locator,
                                      "fingerprint": fp},
                             run_id=run_id, now=now)
        return "inserted"

    def adapter_snapshots_for_run(self, run_id: str) -> list[sqlite3.Row]:
        return list(self._conn.execute(
            "SELECT * FROM adapter_snapshots WHERE run_id = ? "
            "ORDER BY adapter, source_locator", (run_id,)))

    def latest_adapter_snapshot(self, adapter: str) -> sqlite3.Row | None:
        """Most recent collector snapshot for *adapter* joined with its run.

        Read-only Phase 3 helper: the wake/lookup tracker refresh consults
        this when no live read-only source is configured, so thread status
        can be reported with an honest collector age instead of silently
        trusting stored state.
        """
        return self._conn.execute(
            "SELECT s.*, r.window_end AS run_window_end, "
            "r.recorded_at AS run_recorded_at "
            "FROM adapter_snapshots s JOIN runs r ON r.run_id = s.run_id "
            "WHERE s.adapter = ? "
            "ORDER BY r.recorded_at DESC, s.created_at DESC LIMIT 1",
            (adapter,)).fetchone()

    # -- idempotency proofs --------------------------------------------------
    TABLES = ("schema_migrations", "runs", "projects", "candidates", "threads",
              "thread_dispositions", "events", "adapter_snapshots",
              "write_receipts")

    def counts(self) -> dict[str, int]:
        return {t: self._conn.execute(f"SELECT COUNT(*) AS c FROM {t}")
                .fetchone()["c"] for t in self.TABLES}

    def dump_canonical(self) -> str:
        """Deterministic dump of every row for whole-store equality checks."""
        out: dict[str, list[dict[str, Any]]] = {}
        for table in self.TABLES:
            rows = [dict(r) for r in self._conn.execute(f"SELECT * FROM {table}")]
            key_cols = [c for c in ("run_id", "project_id", "candidate_id",
                                    "thread_id", "disposition_id", "event_key",
                                    "snapshot_id", "receipt_id", "version")
                        if rows and c in rows[0]]
            rows.sort(key=lambda r: canonical_json(
                {k: r.get(k) for k in (key_cols or sorted(r))}))
            out[table] = rows
        return canonical_json(out)
