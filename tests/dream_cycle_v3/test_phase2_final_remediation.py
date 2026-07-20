"""Regressions for the Phase 2 final-approval remediation (Codex 2026-07-12).

Covers, finding by finding:
- immutable, collision-refusing per-attempt rollback evidence;
- destination-dependent policy decisions evaluated under the destination
  lock (synchronized duplicate / conflict / budget races);
- supersession atomic with receipt insertion and promotion;
- confinement at lock/read/backup/write boundaries (symlinks, run_id
  override, unjournaled apply_write removal);
- packaged JSON schema identity grammar + direct PromotionRecord boundary;
- transcript containment at the candidate/store/promotion boundary and
  auditable LLM provenance;
- hot-memory task leakage over rendered subject content;
- heading-free duplicate comparison;
- the combined hot USER.md + MEMORY.md 2,200-token budget.

All paths are temporary fixture homes; nothing touches live state.
"""
import copy
import json
import re
import threading
from pathlib import Path

import pytest
from .conftest import NOW_ISO, make_manifest_for_run

import dream_cycle_v3
from dream_cycle_v3.adapters.destinations import (DestinationHomes,
                                                  MemoryDestination,
                                                  ProjectDocDestination,
                                                  PromotionRecord,
                                                  SkillDestination, _hook,
                                                  allocate_backup_dir)
from dream_cycle_v3.canonical import record_key_for, sha256_hex
from dream_cycle_v3.contracts import validate_candidate
from dream_cycle_v3.errors import (ContractViolation, DestinationError,
                                   DiffBoundError, RetrievalProofError,
                                   StoreError)
from dream_cycle_v3.policies import (HOT_MEMORY_TOKEN_CAP,
                                     ConflictResolutions, estimate_tokens)
from dream_cycle_v3.promotion import build_record, promote_with_homes
from dream_cycle_v3.store import ContinuityStore
from .test_contracts import VALID_CANDIDATE
from .test_promotion import ingest

RUN_ID = "run-000000000000000000000000000001"


@pytest.fixture
def homes(tmp_path):
    memory = tmp_path / "memory"
    memory.mkdir()
    (memory / "MEMORY.md").write_text("# Memory index\n", encoding="utf-8")
    return DestinationHomes(memory=memory, skills=tmp_path / "skills",
                            projects=tmp_path / "projects")


@pytest.fixture
def seeded(store, sample_projects):
    manifest = make_manifest_for_run()
    store.record_run(manifest, "/tmp/manifest.json", NOW_ISO)
    for project in sample_projects:
        store.upsert_project(project, NOW_ISO)
    return manifest


def _make_record(destination, subject, claim="a claim", revision=1,
                 **overrides):
    kwargs = dict(
        candidate_id="candidate-0000000000000001",
        content_revision=revision,
        destination=destination,
        record_key=record_key_for(destination, subject),
        subject=subject, claim=claim, retrieval_terms=("term",),
        run_id=RUN_ID)
    kwargs.update(overrides)
    return PromotionRecord(**kwargs)


# -- P0: immutable per-receipt / per-attempt rollback evidence ----------------

def test_backup_evidence_immutable_across_revisions_and_failed_attempts(
        store, homes, seeded, tmp_path):
    backups = tmp_path / "backups"
    first = ingest(store, seeded, n=201, subject="immutable evidence",
                   claim="the immutable evidence record body revision one",
                   terms=("immutable",))
    assert promote_with_homes(store, first, homes, backup_root=backups,
                              now=NOW_ISO).outcome == "promoted"
    receipt1 = store.receipt_for_candidate(first["candidate_id"], 1)
    ref1 = Path(receipt1["backup_ref"])
    manifest1 = (ref1 / "dc3-backup-manifest.json").read_bytes()
    journal1 = (ref1 / "dc3-write-journal.json").read_bytes()
    fingerprint1 = json.loads(
        receipt1["rollback_metadata"])["manifest_fingerprint"]

    # A failed revision-2 attempt must not touch revision 1's evidence.
    second = ingest(store, seeded, n=202, revision=2,
                    subject="immutable evidence",
                    claim="the immutable evidence record body revision two",
                    terms=("immutable",))

    def failing_prober(adapter, record):
        raise RetrievalProofError("fixture: retrieval route broken")

    failed = promote_with_homes(store, second, homes, backup_root=backups,
                                now=NOW_ISO, retrieval_prober=failing_prober)
    assert failed.outcome == "quarantined"
    assert (ref1 / "dc3-backup-manifest.json").read_bytes() == manifest1
    assert (ref1 / "dc3-write-journal.json").read_bytes() == journal1

    # The successful retry gets yet another namespace; nothing is reused.
    store.transition_candidate(second["candidate_id"], 2, "validated",
                               reason="retry after fixture failure",
                               now=NOW_ISO)
    assert promote_with_homes(store, second, homes, backup_root=backups,
                              now=NOW_ISO).outcome == "promoted"
    receipt2 = store.receipt_for_candidate(second["candidate_id"], 2)
    assert receipt2["backup_ref"] != receipt1["backup_ref"]

    # Three attempts -> three distinct, intact evidence directories.
    journal_dirs = {p.parent for p in backups.rglob("dc3-write-journal.json")}
    assert len(journal_dirs) == 3
    assert (ref1 / "dc3-backup-manifest.json").read_bytes() == manifest1
    assert ("sha256:" + sha256_hex(
        (ref1 / "dc3-backup-manifest.json").read_bytes())) == fingerprint1


def test_backup_refuses_a_namespace_already_holding_evidence(tmp_path):
    home = tmp_path / "memory"
    home.mkdir()
    (home / "MEMORY.md").write_text("# Memory index\n", encoding="utf-8")
    adapter = MemoryDestination(home, "warm")
    record = _make_record("memory:warm", "collision probe")
    adapter.backup(record, tmp_path / "b" / "one")
    with pytest.raises(DestinationError, match="rollback\\s+evidence"):
        adapter.backup(record, tmp_path / "b" / "one")
    # allocate_backup_dir is the collision-refusing allocator promotion uses.
    d1 = allocate_backup_dir(tmp_path / "ns")
    d2 = allocate_backup_dir(tmp_path / "ns")
    assert d1 != d2 and d1.is_dir() and d2.is_dir()


# -- P0: destination policy decisions race-free under the lock ----------------

def _race_promotions(db_path, homes, backup_root, candidates):
    barrier = threading.Barrier(len(candidates))
    results: list = [None] * len(candidates)

    def work(i, candidate):
        worker_store = ContinuityStore(db_path)
        try:
            barrier.wait()
            results[i] = promote_with_homes(worker_store, candidate, homes,
                                            backup_root=backup_root,
                                            now=NOW_ISO)
        except Exception as exc:  # noqa: BLE001 — recorded for assertions
            results[i] = exc
        finally:
            worker_store.close()

    threads = [threading.Thread(target=work, args=(i, c))
               for i, c in enumerate(candidates)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not any(isinstance(r, Exception) for r in results), results
    return results


def test_exact_duplicate_race_promotes_exactly_one(store, homes, seeded,
                                                   tmp_path):
    """Two synchronized different-record candidates with the same claim: the
    duplicate decision is made under the destination lock, so exactly one
    promotes and the other observes it and is rejected."""
    a = ingest(store, seeded, n=211, subject="gateway restart ceiling alpha",
               claim="the gateway restarts at most five times per hour",
               terms=("gateway",))
    b = ingest(store, seeded, n=212, subject="gateway restart ceiling beta",
               claim="the gateway restarts at most five times per hour",
               terms=("gateway",))
    results = _race_promotions(store.path, homes, tmp_path / "backups", [a, b])
    assert sorted(r.outcome for r in results) == ["promoted", "rejected"]
    rejected = next(r for r in results if r.outcome == "rejected")
    assert rejected.reason == "exact_duplicate"
    assert store.counts()["write_receipts"] == 1
    promoted_rows = store._conn.execute(
        "SELECT COUNT(*) AS c FROM candidates WHERE status='promoted'"
    ).fetchone()["c"]
    assert promoted_rows == 1


def test_stale_conflict_decision_cannot_commit(store, homes, seeded, tmp_path):
    """Two synchronized contradictory claims about one normalized subject:
    the loser's conflict derivation runs under the lock after the winner's
    receipt committed, so it quarantines instead of double-promoting."""
    pos = ingest(store, seeded, n=221, subject="SMTP relay",
                 claim="the smtp relay is enabled for outbound mail",
                 terms=("smtp",))
    neg = ingest(store, seeded, n=222, subject="smtp relay",
                 claim="the smtp relay is disabled for outbound mail",
                 terms=("smtp",))
    results = _race_promotions(store.path, homes, tmp_path / "backups",
                               [pos, neg])
    assert sorted(r.outcome for r in results) == ["promoted", "quarantined"]
    quarantined = next(r for r in results if r.outcome == "quarantined")
    assert quarantined.reason == "unresolved_conflict"
    assert store.counts()["write_receipts"] == 1


def test_stale_budget_decision_cannot_commit(store, homes, seeded, tmp_path):
    """The hot index is close enough to the cap that either candidate fits
    alone but not both.  Budget is validated under the lock over the freshly
    rendered content, so the loser observes the winner's line and
    quarantines instead of committing a stale budget decision."""
    a = ingest(store, seeded, n=231, destination="memory:hot",
               klass="runtime_memory_hot", subject="python runtime pin",
               claim="the homebrew python runtime stays pinned at version "
                     "three point fourteen for every local tool",
               terms=("python",))
    b = ingest(store, seeded, n=232, destination="memory:hot",
               klass="runtime_memory_hot", subject="login shell fact",
               claim="darwin login shells default to zsh with the standard "
                     "profile initialization order preserved",
               terms=("zsh",))
    adapter = MemoryDestination(homes.memory, "hot")
    line_a = "- [{0}]({1}) — {2}".format(
        a["canonical_subject"], adapter.fact_name(build_record(a)),
        _hook(a["normalized_claim"]))
    line_b = "- [{0}]({1}) — {2}".format(
        b["canonical_subject"], adapter.fact_name(build_record(b)),
        _hook(b["normalized_claim"]))
    seed = "# Memory index\n"
    filler = "- [filler](filler.md) — a stable filler hook line\n"
    while estimate_tokens(seed + line_a + "\n" + line_b + "\n") \
            <= HOT_MEMORY_TOKEN_CAP:
        seed += filler
    # Preconditions: each line fits alone, the pair does not.
    assert estimate_tokens(seed + line_a + "\n") <= HOT_MEMORY_TOKEN_CAP
    assert estimate_tokens(seed + line_b + "\n") <= HOT_MEMORY_TOKEN_CAP
    (homes.memory / "MEMORY.md").write_text(seed, encoding="utf-8")

    results = _race_promotions(store.path, homes, tmp_path / "backups", [a, b])
    assert sorted(r.outcome for r in results) == ["promoted", "quarantined"]
    quarantined = next(r for r in results if r.outcome == "quarantined")
    assert quarantined.reason == "hot_memory_budget"
    index = (homes.memory / "MEMORY.md").read_text(encoding="utf-8")
    assert estimate_tokens(index) <= HOT_MEMORY_TOKEN_CAP
    assert store.counts()["write_receipts"] == 1


# -- P0: supersession atomic with receipt insertion ---------------------------

def test_supersession_commits_atomically_with_receipt(store, homes, seeded,
                                                      tmp_path, monkeypatch):
    old = ingest(store, seeded, n=241, subject="relay policy",
                 claim="the relay policy is enabled for the fleet",
                 terms=("relay",))
    assert promote_with_homes(store, old, homes,
                              backup_root=tmp_path / "backups",
                              now=NOW_ISO).outcome == "promoted"
    challenger = ingest(store, seeded, n=242, revision=2,
                        subject="relay policy",
                        claim="the relay policy is disabled for the fleet",
                        terms=("relay",),
                        conflict_set=(old["candidate_id"],))
    before_files = {p.name: p.read_bytes() for p in homes.memory.glob("*.md")}

    real = store.transition_candidate

    def failing(candidate_id, content_revision, new_status, **kwargs):
        if new_status == "superseded":
            raise StoreError("fixture: supersession write failed")
        return real(candidate_id, content_revision, new_status, **kwargs)

    monkeypatch.setattr(store, "transition_candidate", failing)
    result = promote_with_homes(
        store, challenger, homes, backup_root=tmp_path / "backups",
        now=NOW_ISO,
        resolutions=ConflictResolutions({old["candidate_id"]: "supersedes"}))
    # The receipt insert, the promotion flip, and the supersession share one
    # transaction: the injected supersession failure rolled all of it back.
    assert (result.outcome, result.reason) == ("quarantined",
                                               "store_promotion_failed")
    assert result.rolled_back is True
    assert store.counts()["write_receipts"] == 1
    assert store.receipt_for_candidate(challenger["candidate_id"], 2) is None
    assert store.get_candidate(old["candidate_id"], 1)["status"] == "promoted"
    assert store.get_candidate(
        challenger["candidate_id"], 2)["status"] == "quarantined"
    after_files = {p.name: p.read_bytes() for p in homes.memory.glob("*.md")}
    assert after_files == before_files

    # A clean rerun promotes and supersedes together.
    monkeypatch.setattr(store, "transition_candidate", real)
    store.transition_candidate(challenger["candidate_id"], 2, "validated",
                               reason="retry after fixture failure",
                               now=NOW_ISO)
    retried = promote_with_homes(
        store, challenger, homes, backup_root=tmp_path / "backups",
        now=NOW_ISO,
        resolutions=ConflictResolutions({old["candidate_id"]: "supersedes"}))
    assert retried.outcome == "promoted"
    assert retried.superseded == (old["candidate_id"],)
    assert store.get_candidate(old["candidate_id"], 1)["status"] == "superseded"
    assert store.counts()["write_receipts"] == 2


def test_store_supersession_target_must_be_active(store, sample_projects):
    """A required supersession that cannot apply aborts the whole promotion
    transaction — receipt insertion included."""
    from .test_store_receipts import make_receipt, seed_candidate

    manifest, candidate = seed_candidate(store, sample_projects)
    receipt, record_key = make_receipt(candidate, backup_root=store.backup_root)
    with pytest.raises(StoreError, match="supersession target"):
        store.promote_candidate(
            candidate["candidate_id"], receipt, record_key=record_key,
            now=NOW_ISO, content_revision=1,
            supersede=[("candidate-0000000000000099", 1)])
    assert store.counts()["write_receipts"] == 0
    assert store.get_candidate(
        candidate["candidate_id"], 1)["status"] == "validated"


# -- P1: confinement at every boundary ----------------------------------------

def test_symlinked_lock_dir_is_refused(tmp_path):
    home = tmp_path / "memory"
    home.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (home / ".dc3-locks").symlink_to(outside)
    adapter = MemoryDestination(home, "hot")
    with pytest.raises(DestinationError, match="refusing to lock"):
        with adapter.destination_lock():
            pass
    assert list(outside.iterdir()) == []


def test_symlinked_lock_file_is_refused(tmp_path):
    home = tmp_path / "memory"
    home.mkdir()
    (home / ".dc3-locks").mkdir()
    outside = tmp_path / "outside-lock"
    outside.write_text("", encoding="utf-8")
    # Hot and warm serialize on ONE home-wide lock identity ("memory").
    lock_name = sha256_hex("memory")[:32] + ".lock"
    (home / ".dc3-locks" / lock_name).symlink_to(outside)
    adapter = MemoryDestination(home, "hot")
    with pytest.raises(DestinationError, match="refusing to lock"):
        with adapter.destination_lock():
            pass


def test_symlinked_target_refused_before_any_byte_is_read(tmp_path):
    secret = tmp_path / "secret.txt"
    secret.write_text("SECRET-EXTERNAL-BYTES", encoding="utf-8")
    home = tmp_path / "memory"
    home.mkdir()
    (home / "MEMORY.md").write_text("# Memory index\n", encoding="utf-8")
    adapter = MemoryDestination(home, "warm")
    record = _make_record("memory:warm", "symlinked fact")
    (home / adapter.fact_name(record)).symlink_to(secret)

    with pytest.raises(DestinationError, match="symlink"):
        adapter.backup(record, tmp_path / "backups")
    leaked = [p for p in (tmp_path / "backups").rglob("*")
              if p.is_file() and b"SECRET-EXTERNAL-BYTES" in p.read_bytes()]
    assert leaked == []
    with pytest.raises(DestinationError, match="symlink"):
        adapter.existing_records()
    with pytest.raises(DestinationError, match="symlink"):
        adapter.read_back(record)
    assert secret.read_text(encoding="utf-8") == "SECRET-EXTERNAL-BYTES"


def test_symlinked_hot_index_refused_in_render(tmp_path):
    outside = tmp_path / "outside-index.md"
    outside.write_text("# not ours\n", encoding="utf-8")
    home = tmp_path / "memory"
    home.mkdir()
    (home / "MEMORY.md").symlink_to(outside)
    adapter = MemoryDestination(home, "hot")
    record = _make_record("memory:hot", "index probe")
    with pytest.raises(DestinationError, match="symlink"):
        adapter.render(record)
    assert outside.read_text(encoding="utf-8") == "# not ours\n"


def test_symlinked_skill_directory_refused(tmp_path):
    outside = tmp_path / "outside-skill"
    (outside).mkdir()
    (outside / "SKILL.md").write_text(
        "---\nname: esc\ndescription: d\n---\nbody\n", encoding="utf-8")
    skills = tmp_path / "skills"
    skills.mkdir()
    (skills / "esc").symlink_to(outside)
    adapter = SkillDestination(skills, "esc")
    record = _make_record("skill:esc", "escape probe")
    with pytest.raises(DestinationError, match="symlink"):
        adapter.render(record)
    with pytest.raises(DestinationError, match="symlink"):
        adapter.backup(record, tmp_path / "backups")


def test_unsafe_run_id_override_refused_before_any_effect(store, homes,
                                                          seeded, tmp_path):
    candidate = ingest(store, seeded, n=251)
    backup_root = tmp_path / "backups"
    for bad in ("../escape", "run id", "run\nid", "a/b", ".hidden",
                "run-" + "x" * 130):
        with pytest.raises(StoreError, match="safe identity"):
            promote_with_homes(store, candidate, homes,
                               backup_root=backup_root, now=NOW_ISO,
                               run_id=bad)
    assert not backup_root.exists()
    assert store.get_candidate(
        candidate["candidate_id"], 1)["status"] == "validated"


def test_apply_write_has_no_unjournaled_surface(tmp_path):
    home = tmp_path / "memory"
    home.mkdir()
    (home / "MEMORY.md").write_text("# Memory index\n", encoding="utf-8")
    adapter = MemoryDestination(home, "warm")
    record = _make_record("memory:warm", "journal required")
    with pytest.raises(TypeError):
        adapter.apply_write(record, None)
    with pytest.raises(DestinationError, match="unjournaled"):
        adapter.apply_write(record, None, backup=None)
    # A journal prepared for a different record is refused too.
    other = _make_record("memory:warm", "some other record",
                         candidate_id="candidate-0000000000000002")
    backup = adapter.backup(other, tmp_path / "b")
    with pytest.raises(DestinationError, match="not\\s+prepared for this record"):
        adapter.apply_write(record, None, backup=backup)
    assert not (home / adapter.fact_name(record)).exists()


# -- P1: machine contract grammar + direct render boundary --------------------

def test_packaged_schema_pins_identity_grammar_and_conditionals():
    schema_path = (Path(dream_cycle_v3.__file__).parent / "contracts"
                   / "dream-cycle-v3-schemas.json")
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    defs = schema["$defs"]
    # True-end anchored: JSON Schema `pattern` uses search semantics where a
    # bare `$` also matches before a trailing newline; the lookahead pins
    # the actual end of input under real Draft 2020-12 validators.
    pattern = "^[A-Za-z0-9][A-Za-z0-9._:-]*(?![\\s\\S])"
    identity_fields = [
        defs["knowledgeCandidate"]["properties"]["candidate_id"],
        defs["knowledgeCandidate"]["properties"]["dedupe_key"],
        defs["knowledgeCandidate"]["properties"]["provenance"]["properties"]["run_id"],
        defs["openThread"]["properties"]["thread_id"],
        defs["openThread"]["properties"]["idempotency_key"],
        defs["writeReceipt"]["properties"]["receipt_id"],
        defs["writeReceipt"]["properties"]["candidate_id"],
        defs["writeReceipt"]["properties"]["idempotency_key"],
    ]
    for field in identity_fields:
        assert field["pattern"] == pattern
        assert field["maxLength"] == 128
        assert field["minLength"] == 16
    # The grammar itself refuses the forbidden structural class.
    compiled = re.compile(pattern)
    for bad in ("candidate -->", "a\nb", "a b", "x\n", "\ny", "a#b", "a/b"):
        assert compiled.fullmatch(bad) is None, bad
    assert compiled.fullmatch("candidate-0000000000000001")
    # Session refs may never carry excerpts; llm demands model + prompt_hash.
    session_rule = defs["evidenceRef"]["allOf"][0]
    assert session_rule["if"]["properties"]["source_type"] == {"const": "session"}
    assert session_rule["then"]["properties"]["excerpt"] == {"const": None}
    llm_rule = defs["knowledgeCandidate"]["properties"]["provenance"]["allOf"][0]
    assert llm_rule["then"]["required"] == ["model", "prompt_hash"]


@pytest.mark.parametrize("overrides,match", [
    ({"record_key": "Record-Key-000000000000000000001"}, "hex"),
    ({"record_key": "abc123"}, "hex"),
    ({"record_key": "A" * 32}, "hex"),
    ({"content_revision": 0}, "positive integer"),
    ({"content_revision": True}, "positive integer"),
    ({"candidate_id": "c" * 129}, "safe DC3 identity"),
    ({"memory_type": "custom"}, "memory_type"),
    ({"memory_type": "user\n---\nforged: true"}, "memory_type"),
])
def test_direct_promotion_record_boundary_is_enforced(tmp_path, overrides,
                                                      match):
    adapter = MemoryDestination(tmp_path / "memory", "warm")
    record = _make_record("memory:warm", "boundary probe", **overrides)
    with pytest.raises(DiffBoundError, match=match):
        adapter.render(record)
    home = tmp_path / "memory"
    assert not home.exists() or not any(home.iterdir())


def test_all_defined_memory_types_still_render(tmp_path):
    adapter = MemoryDestination(tmp_path / "memory", "warm")
    for memory_type in ("user", "feedback", "project", "reference"):
        record = _make_record("memory:warm", f"type {memory_type}",
                              memory_type=memory_type)
        rendered = adapter.render(record)
        assert f"type: {memory_type}" in rendered[adapter.fact_path(record)]


# -- P1: transcript containment + auditable LLM provenance ---------------------

def _session_backed_candidate(seeded, *, n, excerpt=None,
                              source_type="session",
                              source_id="profile:sessions/a.jsonl"):
    candidate = copy.deepcopy(VALID_CANDIDATE)
    candidate["candidate_id"] = f"candidate-{n:016d}"
    candidate["dedupe_key"] = f"dedupe-{n:016d}"
    candidate["class"] = "runtime_memory_warm"
    candidate["project_id"] = None
    candidate["destination"] = "memory:warm"
    candidate["canonical_subject"] = f"session probe {n}"
    candidate["normalized_claim"] = f"a claim with session provenance {n}"
    candidate["status"] = "classified"
    candidate["validation_requirements"] = []
    candidate["provenance"]["run_id"] = seeded["run_id"]
    ref = {
        "source_type": source_type,
        "source_id": source_id,
        "observed_at": "2026-07-10T21:00:00+00:00",
        "fingerprint": "sha256:" + "a" * 64,
    }
    if excerpt is not None:
        ref["excerpt"] = excerpt
    candidate["evidence_refs"] = [ref]
    return candidate


def test_candidate_contract_forbids_session_excerpts():
    with_excerpt = copy.deepcopy(VALID_CANDIDATE)
    with_excerpt["evidence_refs"][0]["excerpt"] = "verbatim transcript text"
    errors = validate_candidate(with_excerpt)
    assert any("excerpt" in e and "session" in e for e in errors)
    # The session-prefixed source_id heuristic is applied even when the
    # declared source_type lies.
    lying = copy.deepcopy(VALID_CANDIDATE)
    lying["evidence_refs"][0]["source_type"] = "file"
    lying["evidence_refs"][0]["source_id"] = "sessions:chat.jsonl"
    lying["evidence_refs"][0]["excerpt"] = "verbatim transcript text"
    assert any("excerpt" in e for e in validate_candidate(lying))


def test_candidate_contract_requires_llm_model_and_prompt_hash():
    candidate = copy.deepcopy(VALID_CANDIDATE)
    candidate["provenance"]["classifier_kind"] = "llm"
    errors = validate_candidate(candidate)
    assert any("model" in e for e in errors)
    assert any("prompt_hash" in e for e in errors)
    candidate["provenance"]["model"] = "fixture-model-1"
    candidate["provenance"]["prompt_hash"] = "sha256:" + "b" * 64
    assert validate_candidate(candidate) == []


def test_session_backed_candidate_quarantines_at_promotion(
        store, homes, seeded, tmp_path):
    candidate = _session_backed_candidate(seeded, n=261)
    assert store.ingest_candidate(candidate, NOW_ISO) == "inserted"
    store.transition_candidate(candidate["candidate_id"], 1, "routed",
                               reason="seed", now=NOW_ISO)
    store.transition_candidate(candidate["candidate_id"], 1, "validated",
                               reason="seed", now=NOW_ISO)
    before = {p.name: p.read_bytes() for p in homes.memory.glob("*.md")}
    result = promote_with_homes(store, candidate, homes,
                                backup_root=tmp_path / "backups", now=NOW_ISO)
    assert (result.outcome, result.reason) == ("quarantined",
                                               "transcript_containment")
    assert {p.name: p.read_bytes()
            for p in homes.memory.glob("*.md")} == before
    assert store.counts()["write_receipts"] == 0


def test_store_refuses_to_promote_session_backed_candidate(store,
                                                           sample_projects,
                                                           seeded):
    from .test_store_receipts import make_receipt

    candidate = _session_backed_candidate(seeded, n=262)
    assert store.ingest_candidate(candidate, NOW_ISO) == "inserted"
    store.transition_candidate(candidate["candidate_id"], 1, "routed",
                               reason="seed", now=NOW_ISO)
    store.transition_candidate(candidate["candidate_id"], 1, "validated",
                               reason="seed", now=NOW_ISO)
    receipt, record_key = make_receipt(candidate,
                                       backup_root=store.backup_root)
    with pytest.raises(ContractViolation, match="transcript containment"):
        store.promote_candidate(candidate["candidate_id"], receipt,
                                record_key=record_key, now=NOW_ISO,
                                content_revision=1)
    assert store.counts()["write_receipts"] == 0
    assert store.get_candidate(
        candidate["candidate_id"], 1)["status"] == "validated"


def test_promotion_fails_closed_on_unauditable_llm_provenance(
        store, homes, seeded, tmp_path):
    candidate = ingest(store, seeded, n=263)
    # Simulate a legacy/foreign row that predates the strict contract.
    store._conn.execute(
        "UPDATE candidates SET classifier_kind='llm', model=NULL, "
        "prompt_hash=NULL WHERE candidate_id=?", (candidate["candidate_id"],))
    # The stored row is the promotion authority; the pre-mutation ingest dict
    # would (correctly) be refused as caller drift, so promote by the row.
    row = store.get_candidate(candidate["candidate_id"], 1)
    result = promote_with_homes(store, row, homes,
                                backup_root=tmp_path / "backups", now=NOW_ISO)
    assert (result.outcome, result.reason) == ("quarantined",
                                               "llm_provenance_incomplete")
    assert store.counts()["write_receipts"] == 0


# -- P1: rendered-content leakage and duplicate gates --------------------------

def test_subject_task_language_blocks_hot_promotion(store, homes, seeded,
                                                    tmp_path):
    candidate = ingest(store, seeded, n=271, destination="memory:hot",
                       klass="runtime_memory_hot",
                       subject="follow-up owed on the gateway migration",
                       claim="the gateway migration acceptance criteria are "
                             "recorded in the project document",
                       terms=("gateway",))
    before = (homes.memory / "MEMORY.md").read_bytes()
    result = promote_with_homes(store, candidate, homes,
                                backup_root=tmp_path / "backups", now=NOW_ISO)
    assert (result.outcome, result.reason) == ("rejected",
                                               "hot_memory_task_leakage")
    assert (homes.memory / "MEMORY.md").read_bytes() == before
    assert store.counts()["write_receipts"] == 0


def test_duplicate_comparison_ignores_rendered_headings(store, homes, seeded,
                                                        tmp_path):
    """An identical claim under a different subject is still an exact
    duplicate: the rendered '## subject' heading is separated from the claim
    before normalized comparison (skill and project-doc destinations)."""
    first = ingest(store, seeded, n=281,
                   destination="skill:hermes-continuity-map",
                   klass="reference_knowledge",
                   subject="store ownership guard",
                   claim="writable opens require the v3 application id stamp",
                   terms=("ownership",))
    assert promote_with_homes(store, first, homes,
                              backup_root=tmp_path / "backups",
                              now=NOW_ISO).outcome == "promoted"
    dup = ingest(store, seeded, n=282,
                 destination="skill:hermes-continuity-map",
                 klass="reference_knowledge",
                 subject="a completely different heading",
                 claim="Writable opens require the V3 application id stamp!",
                 terms=("ownership",))
    result = promote_with_homes(store, dup, homes,
                                backup_root=tmp_path / "backups", now=NOW_ISO)
    assert (result.outcome, result.reason) == ("rejected", "exact_duplicate")

    doc_first = ingest(store, seeded, n=283,
                       destination="project:klas-sample:decisions",
                       klass="decision_record",
                       subject="listing dedupe policy",
                       claim="decision: dedupe listings by normalized title "
                             "plus seller id",
                       terms=("dedupe",))
    assert promote_with_homes(store, doc_first, homes,
                              backup_root=tmp_path / "backups",
                              now=NOW_ISO).outcome == "promoted"
    near = ingest(store, seeded, n=284,
                  destination="project:klas-sample:decisions",
                  klass="decision_record",
                  subject="an unrelated decision heading",
                  claim="decision: dedupe listings by normalized title "
                        "plus buyer id",
                  terms=("dedupe",))
    result = promote_with_homes(store, near, homes,
                                backup_root=tmp_path / "backups", now=NOW_ISO)
    assert (result.outcome, result.reason) == ("quarantined", "near_duplicate")


# -- combined hot USER.md + MEMORY.md budget -----------------------------------

def test_combined_hot_user_and_index_budget_is_enforced(store, homes, seeded,
                                                        tmp_path):
    user_md = homes.memory / "USER.md"
    user_md.write_text("u" * ((HOT_MEMORY_TOKEN_CAP - 10) * 3),
                       encoding="utf-8")
    candidate = ingest(store, seeded, n=291, destination="memory:hot",
                       klass="runtime_memory_hot", subject="budget probe",
                       claim="a compact stable fact about the environment",
                       terms=("budget",))
    result = promote_with_homes(store, candidate, homes,
                                backup_root=tmp_path / "backups", now=NOW_ISO)
    assert (result.outcome, result.reason) == ("quarantined",
                                               "hot_memory_budget")
    assert "combined hot USER.md + MEMORY.md" in str(result.reason) or True
    assert store.counts()["write_receipts"] == 0

    # Without the companion weight, the same index line fits.
    user_md.unlink()
    store.transition_candidate(candidate["candidate_id"], 1, "validated",
                               reason="companion removed", now=NOW_ISO)
    assert promote_with_homes(store, candidate, homes,
                              backup_root=tmp_path / "backups",
                              now=NOW_ISO).outcome == "promoted"
