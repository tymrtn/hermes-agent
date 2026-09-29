"""Regressions for the Codex approved-final review blockers (2026-07-12,
codex-phase2-approved-final.md).

Covers, finding by finding:
- P0 cross-tier memory locking: memory:hot and memory:warm serialize on one
  home-wide lock identity; synchronized hot-vs-warm duplicate / conflict /
  budget races; shared-scope crash recovery; disjoint scopes elsewhere;
- P0 direct ContinuityStore.promote_candidate authority: explicit
  content_revision required, record identity derived from the stored row,
  supersession bound to durable reviewed conflict relationships;
- P1 backup-root descendant symlink confinement: descriptor-pinned
  allocation refuses planted symlinks at every namespace depth, and
  parent-swap races are refused at the backup/journal write boundaries;
- P1 contract parity: packaged JSON Schema identity patterns reject a
  trailing newline under a real Draft202012Validator, and the Python
  validators exclude bool where the machine contract says integer;
- P1 YAML rendering: untrusted frontmatter subjects are emitted as
  deterministic double-quoted scalars that round-trip through
  yaml.safe_load and the production-shaped loaders; read-back refuses
  loader-invalid frontmatter;
- nonblocking hardening: combined_revision is private, and a reused store
  carrying receiptless promoted rows is refused before promotion.

All homes, stores, and backup roots are throwaway tmp_path fixtures; nothing
touches live profile state.
"""
import copy
import json
import os
import threading
from pathlib import Path

import pytest
from .conftest import NOW_ISO, make_manifest_for_run

import dream_cycle_v3.adapters.destinations as destinations
from dream_cycle_v3.adapters.destinations import (DestinationHomes,
                                                  MemoryDestination,
                                                  ProjectDocDestination,
                                                  PromotionRecord,
                                                  SkillDestination, _hook,
                                                  allocate_backup_dir,
                                                  load_skill,
                                                  parse_frontmatter,
                                                  yaml_frontmatter_scalar)
from dream_cycle_v3.canonical import record_key_for
from dream_cycle_v3.contracts import (validate_candidate, validate_project,
                                      validate_thread)
from dream_cycle_v3.errors import (CandidateStateError, ContractViolation,
                                   DestinationError, ReadBackError,
                                   StoreError)
from dream_cycle_v3.policies import HOT_MEMORY_TOKEN_CAP, estimate_tokens
from dream_cycle_v3.promotion import build_record, promote_with_homes
from dream_cycle_v3.store import ContinuityStore
from .test_contracts import VALID_CANDIDATE
from .test_phase2_final_remediation import _race_promotions
from .test_promotion import ingest
from .test_store_receipts import make_receipt, seed_candidate

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


def _make_record(destination, subject, claim="a claim", revision=1):
    return PromotionRecord(
        candidate_id="candidate-0000000000000001",
        content_revision=revision, destination=destination,
        record_key=record_key_for(destination, subject), subject=subject,
        claim=claim, retrieval_terms=("term",), run_id=RUN_ID)


# -- P0: hot and warm memory serialize on one shared lock identity ------------

def test_hot_and_warm_serialize_on_one_lock(tmp_path):
    home = tmp_path / "memory"
    home.mkdir()
    hot = MemoryDestination(home, "hot")
    warm = MemoryDestination(home, "warm")
    assert hot.lock_scope == warm.lock_scope == "memory"
    assert hot.policy_scope_destinations() == ("memory:hot", "memory:warm")

    acquired, release, warm_locked = (threading.Event(), threading.Event(),
                                      threading.Event())

    def hold_hot():
        with hot.destination_lock():
            acquired.set()
            release.wait(10)

    def take_warm():
        with warm.destination_lock():
            warm_locked.set()

    holder = threading.Thread(target=hold_hot)
    holder.start()
    assert acquired.wait(10)
    taker = threading.Thread(target=take_warm)
    taker.start()
    # The warm lock is the SAME lock: it cannot be acquired while hot holds it.
    assert not warm_locked.wait(0.3)
    release.set()
    assert warm_locked.wait(10)
    holder.join()
    taker.join()


def test_other_adapters_keep_disjoint_policy_scopes(tmp_path):
    """Audit companion: skill and project-doc adapters own disjoint per-id
    files, so their lock/policy scope stays the exact destination."""
    alpha = SkillDestination(tmp_path / "skills", "alpha-skill")
    beta = SkillDestination(tmp_path / "skills", "beta-skill")
    assert alpha.lock_scope != beta.lock_scope
    assert alpha.policy_scope_destinations() == ("skill:alpha-skill",)
    doc_a = ProjectDocDestination(tmp_path / "projects", "proj", "decisions")
    doc_b = ProjectDocDestination(tmp_path / "projects", "proj", "context")
    assert doc_a.lock_scope != doc_b.lock_scope
    assert doc_a.policy_scope_destinations() == ("project:proj:decisions",)


def test_cross_tier_exact_duplicate_race_promotes_exactly_one(
        store, homes, seeded, tmp_path):
    """Synchronized hot vs warm candidates carrying the same claim: both scan
    the same home-wide fact set, so under the shared lock exactly one
    promotes and the other observes it and is rejected."""
    hot = ingest(store, seeded, n=311, destination="memory:hot",
                 klass="runtime_memory_hot",
                 subject="fleet dns resolver alpha",
                 claim="the fleet resolves dns through the local caching daemon",
                 terms=("dns",))
    warm = ingest(store, seeded, n=312, destination="memory:warm",
                  klass="runtime_memory_warm",
                  subject="fleet dns resolver beta",
                  claim="the fleet resolves dns through the local caching daemon",
                  terms=("dns",))
    results = _race_promotions(store.path, homes, tmp_path / "backups",
                               [hot, warm])
    assert sorted(r.outcome for r in results) == ["promoted", "rejected"]
    rejected = next(r for r in results if r.outcome == "rejected")
    assert rejected.reason == "exact_duplicate"
    assert store.counts()["write_receipts"] == 1
    promoted_rows = store._conn.execute(
        "SELECT COUNT(*) AS c FROM candidates WHERE status='promoted'"
    ).fetchone()["c"]
    assert promoted_rows == 1


def test_cross_tier_conflict_race_quarantines_the_loser(
        store, homes, seeded, tmp_path):
    """Synchronized contradictory hot vs warm claims about one normalized
    subject: conflict derivation covers the shared memory policy scope, so
    the loser observes the winner's committed receipt and quarantines."""
    pos = ingest(store, seeded, n=321, destination="memory:hot",
                 klass="runtime_memory_hot", subject="mdns advertising",
                 claim="the mdns advertising responder is enabled for the fleet",
                 terms=("mdns",))
    neg = ingest(store, seeded, n=322, destination="memory:warm",
                 klass="runtime_memory_warm", subject="MDNS Advertising",
                 claim="the mdns advertising responder is disabled for the fleet",
                 terms=("mdns",))
    results = _race_promotions(store.path, homes, tmp_path / "backups",
                               [pos, neg])
    assert sorted(r.outcome for r in results) == ["promoted", "quarantined"]
    quarantined = next(r for r in results if r.outcome == "quarantined")
    assert quarantined.reason == "unresolved_conflict"
    assert store.counts()["write_receipts"] == 1


def test_cross_tier_budget_race_stays_within_the_hot_cap(
        store, homes, seeded, tmp_path):
    """A hot candidate near the combined cap races a warm candidate.  Under
    the shared lock the hot budget decision is evaluated against committed
    home state only; both serialize cleanly and the injected index never
    exceeds the cap."""
    hot = ingest(store, seeded, n=331, destination="memory:hot",
                 klass="runtime_memory_hot", subject="python runtime pin",
                 claim="the homebrew python runtime stays pinned at version "
                       "three point fourteen for every local tool",
                 terms=("python",))
    warm = ingest(store, seeded, n=332, destination="memory:warm",
                  klass="runtime_memory_warm", subject="login shell fact",
                  claim="darwin login shells default to zsh with the standard "
                        "profile initialization order preserved",
                  terms=("zsh",))
    adapter = MemoryDestination(homes.memory, "hot")
    line = "- [{0}]({1}) — {2}".format(
        hot["canonical_subject"], adapter.fact_name(build_record(hot)),
        _hook(hot["normalized_claim"]))
    seed = "# Memory index\n"
    filler = "- [filler](filler.md) — a stable filler hook line\n"
    while estimate_tokens(seed + filler + line + "\n") <= HOT_MEMORY_TOKEN_CAP:
        seed += filler
    assert estimate_tokens(seed + line + "\n") <= HOT_MEMORY_TOKEN_CAP
    (homes.memory / "MEMORY.md").write_text(seed, encoding="utf-8")

    results = _race_promotions(store.path, homes, tmp_path / "backups",
                               [hot, warm])
    assert sorted(r.outcome for r in results) == ["promoted", "promoted"]
    index = (homes.memory / "MEMORY.md").read_text(encoding="utf-8")
    assert estimate_tokens(index) <= HOT_MEMORY_TOKEN_CAP
    assert store.counts()["write_receipts"] == 2


def test_crashed_warm_write_is_recovered_before_a_hot_promotion(
        store, homes, seeded, tmp_path):
    """Shared policy scope covers crash recovery too: an abandoned warm
    journal is reconciled by the next hot promotion before its uncommitted
    bytes can feed the hot duplicate/conflict scan."""
    backup_root = (tmp_path / "backups").resolve()
    warm = MemoryDestination(homes.memory, "warm")
    record = _make_record("memory:warm", "crashed warm fact",
                          claim="the crashed warm fact is enabled")
    backup = warm.backup(
        record,
        allocate_backup_dir(backup_root, RUN_ID, record.record_key, "rev-1"),
        backup_root=backup_root)
    warm.apply_write(record, warm.snapshot_revision(record), backup=backup)
    backup.mark_state("written")   # crash: no receipt was ever persisted
    assert warm.fact_path(record).exists()

    candidate = ingest(store, seeded, n=341, destination="memory:hot",
                       klass="runtime_memory_hot", subject="recovery probe",
                       claim="a stable fact promoted after crash recovery",
                       terms=("recovery",))
    result = promote_with_homes(store, candidate, homes,
                                backup_root=backup_root, now=NOW_ISO)
    assert result.outcome == "promoted"
    # The abandoned warm write was rolled back, not treated as destination truth.
    assert not warm.fact_path(record).exists()
    assert json.loads(backup.journal_path.read_text(
        encoding="utf-8"))["state"] == "rolled_back"


# -- P0: direct store promotion authority --------------------------------------

def test_store_promotion_requires_an_explicit_content_revision(
        store, sample_projects):
    _, candidate = seed_candidate(store, sample_projects)
    receipt, record_key = make_receipt(candidate, backup_root=store.backup_root)
    with pytest.raises(TypeError):
        store.promote_candidate(candidate["candidate_id"], receipt,
                                record_key=record_key, now=NOW_ISO)
    for bad in (None, True, 0, -1, "1"):
        with pytest.raises(StoreError, match="content_revision"):
            store.promote_candidate(candidate["candidate_id"], receipt,
                                    record_key=record_key, now=NOW_ISO,
                                    content_revision=bad)
    # Naming a revision that is not the stored row is refused, never
    # silently mapped to the latest row.
    with pytest.raises(StoreError, match="unknown candidate"):
        store.promote_candidate(candidate["candidate_id"], receipt,
                                record_key=record_key, now=NOW_ISO,
                                content_revision=2)
    assert store.counts()["write_receipts"] == 0
    assert store.get_candidate(
        candidate["candidate_id"], 1)["status"] == "validated"


def test_store_promotion_derives_record_key_from_the_stored_row(
        store, sample_projects):
    """record_key is optional and always derived from the stored destination
    + canonical subject; the persisted receipt carries the derivation."""
    _, candidate = seed_candidate(store, sample_projects)
    receipt, _ = make_receipt(candidate, backup_root=store.backup_root)
    assert store.promote_candidate(candidate["candidate_id"], receipt,
                                   now=NOW_ISO, content_revision=1) == "inserted"
    stored = store.receipt_for_candidate(candidate["candidate_id"], 1)
    assert stored["record_key"] == record_key_for(
        candidate["destination"], candidate["canonical_subject"])


def test_store_promotion_refuses_caller_record_key_drift(
        store, sample_projects):
    """An internally consistent receipt bound to a different record identity
    (forged record_key + matching idempotency key) is refused outright."""
    _, candidate = seed_candidate(store, sample_projects)
    forged_key = record_key_for(candidate["destination"],
                                "a completely different subject")
    receipt, _ = make_receipt(candidate, record_key=forged_key,
                              backup_root=store.backup_root)
    with pytest.raises(ContractViolation, match="authoritative"):
        store.promote_candidate(candidate["candidate_id"], receipt,
                                record_key=forged_key, now=NOW_ISO,
                                content_revision=1)
    # Dropping the explicit key does not help: the idempotency key inside the
    # receipt was computed from the forged identity and no longer matches the
    # derived one.
    with pytest.raises(ContractViolation, match="idempotency_key"):
        store.promote_candidate(candidate["candidate_id"], receipt,
                                now=NOW_ISO, content_revision=1)
    assert store.counts()["write_receipts"] == 0
    assert store.get_candidate(
        candidate["candidate_id"], 1)["status"] == "validated"


def test_store_promotion_never_supersedes_unrelated_rows(
        store, sample_projects):
    """The supersede list is a capability proven by durable reviewed
    'supersedes' conflict relationships, never a free caller input."""
    _, victim = seed_candidate(
        store, sample_projects,
        candidate_id="candidate-0000000000000021",
        dedupe_key="dedupe-0000000000000021", subject="victim subject")
    receipt_v, _ = make_receipt(victim, receipt_id="receipt-000000000000000021",
                                backup_root=store.backup_root)
    assert store.promote_candidate(victim["candidate_id"], receipt_v,
                                   now=NOW_ISO, content_revision=1) == "inserted"

    _, challenger = seed_candidate(
        store, sample_projects,
        candidate_id="candidate-0000000000000022",
        dedupe_key="dedupe-0000000000000022", subject="challenger subject")
    receipt_c, _ = make_receipt(challenger,
                                receipt_id="receipt-000000000000000022",
                                backup_root=store.backup_root)
    with pytest.raises(CandidateStateError,
                       match="durable reviewed 'supersedes'"):
        store.promote_candidate(challenger["candidate_id"], receipt_c,
                                now=NOW_ISO, content_revision=1,
                                supersede=[(victim["candidate_id"], 1)])
    # Atomicity: the refused supersession aborted the whole promotion.
    assert store.counts()["write_receipts"] == 1
    assert store.get_candidate(victim["candidate_id"], 1)["status"] == "promoted"
    assert store.get_candidate(
        challenger["candidate_id"], 1)["status"] == "validated"

    # With the durable reviewed relationship the same call succeeds and the
    # victim retires in the same transaction.
    store.record_conflict_relationships(
        challenger["candidate_id"], 1, [victim["candidate_id"]],
        relationships={victim["candidate_id"]: "supersedes"}, now=NOW_ISO)
    assert store.promote_candidate(
        challenger["candidate_id"], receipt_c, now=NOW_ISO,
        content_revision=1,
        supersede=[(victim["candidate_id"], 1)]) == "inserted"
    assert store.get_candidate(
        victim["candidate_id"], 1)["status"] == "superseded"
    assert store.counts()["write_receipts"] == 2


# -- P1: backup-root descendant symlink confinement -----------------------------

@pytest.mark.parametrize("depth", [0, 1, 2], ids=["run", "record-key", "rev"])
def test_backup_namespace_refuses_planted_symlink_at_every_depth(
        store, homes, seeded, tmp_path, depth):
    candidate = ingest(store, seeded, n=351 + depth,
                       subject=f"symlinked backup depth {depth}",
                       claim=f"backup namespace confinement probe {depth}",
                       terms=("confinement",))
    record = build_record(store.get_candidate(candidate["candidate_id"], 1))
    components = [candidate["provenance"]["run_id"], record.record_key,
                  "rev-1"]
    outside = tmp_path / "outside"
    outside.mkdir(exist_ok=True)
    backup_root = tmp_path / "backups"
    parent = backup_root
    for part in components[:depth]:
        parent = parent / part
    parent.mkdir(parents=True, exist_ok=True)
    (parent / components[depth]).symlink_to(outside)

    with pytest.raises(DestinationError, match="symlink|not a real directory"):
        promote_with_homes(store, candidate, homes, backup_root=backup_root,
                           now=NOW_ISO)
    assert list(outside.iterdir()) == []   # nothing routed through the link
    assert store.counts()["write_receipts"] == 0
    assert store.get_candidate(
        candidate["candidate_id"], 1)["status"] == "validated"


def test_symlinked_attempt_name_is_skipped_never_followed(
        store, homes, seeded, tmp_path):
    candidate = ingest(store, seeded, n=356, subject="attempt squat probe",
                       claim="the attempt squat probe fact is stable",
                       terms=("squat",))
    record = build_record(store.get_candidate(candidate["candidate_id"], 1))
    outside = tmp_path / "outside-attempt"
    outside.mkdir()
    backup_root = tmp_path / "backups"
    namespace = (backup_root / candidate["provenance"]["run_id"]
                 / record.record_key / "rev-1")
    namespace.mkdir(parents=True)
    (namespace / "attempt-00001").symlink_to(outside)

    result = promote_with_homes(store, candidate, homes,
                                backup_root=backup_root, now=NOW_ISO)
    assert result.outcome == "promoted"
    receipt = store.receipt_for_candidate(candidate["candidate_id"], 1)
    assert receipt["backup_ref"].endswith("attempt-00002")
    assert list(outside.iterdir()) == []
    assert (namespace / "attempt-00001").is_symlink()


def test_allocate_backup_dir_refuses_unsafe_components(tmp_path):
    for bad in ("../escape", "a/b", "", ".hidden", "run id"):
        with pytest.raises(DestinationError, match="safe identity"):
            allocate_backup_dir(tmp_path / "backups", bad)
    assert not (tmp_path / "escape").exists()
    assert not (tmp_path / "backups" / "a").exists()


def test_backup_refuses_namespace_swapped_after_allocation(tmp_path):
    """Cooperative parent-swap race on Darwin: a namespace component moved
    aside and replaced with a symlink between allocation and the first
    evidence write is refused, not followed."""
    home = tmp_path / "memory"
    home.mkdir()
    (home / "MEMORY.md").write_text("# Memory index\n", encoding="utf-8")
    adapter = MemoryDestination(home, "warm")
    record = _make_record("memory:warm", "parent swap probe")
    root = (tmp_path / "backups").resolve()
    allocated = allocate_backup_dir(root, "run-swap", record.record_key,
                                    "rev-1")
    stolen = tmp_path / "stolen"
    os.rename(root / "run-swap", stolen)
    (root / "run-swap").symlink_to(stolen)
    with pytest.raises(DestinationError, match="symlink"):
        adapter.backup(record, allocated, backup_root=root)
    assert not list(stolen.rglob("dc3-*"))   # no evidence written through it


def test_journal_writes_reassert_confinement_after_parent_swap(tmp_path):
    home = tmp_path / "memory"
    home.mkdir()
    (home / "MEMORY.md").write_text("# Memory index\n", encoding="utf-8")
    adapter = MemoryDestination(home, "warm")
    record = _make_record("memory:warm", "journal swap probe")
    root = (tmp_path / "backups").resolve()
    backup = adapter.backup(
        record, allocate_backup_dir(root, "run-jswap", record.record_key,
                                    "rev-1"),
        backup_root=root)
    stolen = tmp_path / "stolen-journal"
    os.rename(root / "run-jswap", stolen)
    (root / "run-jswap").symlink_to(stolen)
    before = {p: p.read_bytes() for p in stolen.rglob("*") if p.is_file()}
    with pytest.raises(DestinationError, match="symlink"):
        backup.mark_state("written")
    after = {p: p.read_bytes() for p in stolen.rglob("*") if p.is_file()}
    assert after == before   # the displaced journal was not mutated


# -- P1: machine-contract parity under a real JSON Schema validator ------------

def _packaged_schema():
    import dream_cycle_v3
    schema_path = (Path(dream_cycle_v3.__file__).parent / "contracts"
                   / "dream-cycle-v3-schemas.json")
    return json.loads(schema_path.read_text(encoding="utf-8"))


def test_schema_rejects_trailing_newline_under_draft202012():
    jsonschema = pytest.importorskip("jsonschema")
    validator = jsonschema.Draft202012Validator(_packaged_schema())
    assert validator.is_valid({"candidate": copy.deepcopy(VALID_CANDIDATE)})
    for field in ("candidate_id", "dedupe_key"):
        bad = copy.deepcopy(VALID_CANDIDATE)
        bad[field] = bad[field] + "\n"
        # Real pattern-search semantics, not Python fullmatch: the packaged
        # true-end grammar refuses the trailing newline...
        assert not validator.is_valid({"candidate": bad}), field
        # ...and the stdlib Python validator agrees (\\Z anchors).
        assert validate_candidate(bad), field
    bad_run = copy.deepcopy(VALID_CANDIDATE)
    bad_run["provenance"]["run_id"] += "\n"
    assert not validator.is_valid({"candidate": bad_run})
    assert validate_candidate(bad_run)


def test_content_revision_bool_is_refused_by_both_grammars():
    bad = copy.deepcopy(VALID_CANDIDATE)
    bad["content_revision"] = True
    errors = validate_candidate(bad)
    assert any("content_revision" in e for e in errors), errors
    jsonschema = pytest.importorskip("jsonschema")
    validator = jsonschema.Draft202012Validator(_packaged_schema())
    assert not validator.is_valid({"candidate": bad})


def test_python_integer_fields_exclude_bool(sample_projects, sample_threads):
    candidate = copy.deepcopy(VALID_CANDIDATE)
    candidate["schema_version"] = True
    assert any("schema_version" in e for e in validate_candidate(candidate))
    project = copy.deepcopy(sample_projects[0])
    project["registry_version"] = True
    assert any("registry_version" in e for e in validate_project(project))
    project = copy.deepcopy(sample_projects[0])
    project["schema_version"] = True
    assert any("schema_version" in e for e in validate_project(project))
    thread = copy.deepcopy(sample_threads[0])
    thread["schema_version"] = True
    assert any("schema_version" in e for e in validate_thread(thread))


def test_project_id_grammar_rejects_trailing_newline():
    project_bad = {"project_id": "valid-project\n"}
    from dream_cycle_v3.contracts import PROJECT_ID_RE
    assert PROJECT_ID_RE.match("valid-project")
    assert PROJECT_ID_RE.match("valid-project\n") is None
    jsonschema = pytest.importorskip("jsonschema")
    schema = _packaged_schema()
    pattern = (schema["$defs"]["projectRegistryEntry"]["properties"]
               ["project_id"]["pattern"])
    validator = jsonschema.Draft202012Validator(
        {"type": "string", "pattern": pattern})
    assert validator.is_valid("valid-project")
    assert not validator.is_valid(project_bad["project_id"])


# -- P1: YAML-safe rendering of untrusted frontmatter scalars ------------------

YAML_SUBJECTS = [
    "component: state",
    "value # not a comment",
    'say "hello" twice',
    "true",
    "null",
    "[list, like]",
    "{map: like}",
    "unicode ✓ sübject 日本語",
    " padded subject ",
    "- leading dash",
    "yes",
]


def test_emitter_round_trips_through_yaml_safe_load():
    yaml = pytest.importorskip("yaml")
    for value in YAML_SUBJECTS + ["back\\slash", 'mix "q" and \\ end ']:
        rendered = f"description: {yaml_frontmatter_scalar(value)}"
        assert yaml.safe_load(rendered)["description"] == value
        # The stdlib production-compatible parser decodes to the same string.
        fields, _ = parse_frontmatter(f"---\n{rendered}\n---\nbody\n", "t")
        assert fields["description"] == value


@pytest.mark.parametrize("subject", YAML_SUBJECTS)
def test_yaml_significant_subjects_promote_and_round_trip(
        store, homes, seeded, tmp_path, subject):
    yaml = pytest.importorskip("yaml")
    candidate = ingest(store, seeded, n=361, destination="memory:warm",
                       subject=subject,
                       claim="a stable quoted frontmatter fixture fact",
                       terms=("quoted",))
    result = promote_with_homes(store, candidate, homes,
                                backup_root=tmp_path / "backups", now=NOW_ISO)
    assert result.outcome == "promoted", (subject, result.reason)
    facts = [p for p in homes.memory.glob("*.md") if p.name != "MEMORY.md"]
    assert len(facts) == 1
    text = facts[0].read_text(encoding="utf-8")
    block = text[4:text.find("\n---\n", 4)]
    assert yaml.safe_load(block)["description"] == subject
    adapter = MemoryDestination(homes.memory, "warm")
    assert [r.subject for r in adapter.existing_records()] == [subject]


def test_skill_scaffold_quotes_untrusted_subject(store, homes, seeded,
                                                 tmp_path):
    yaml = pytest.importorskip("yaml")
    subject = "procedure: restart order"
    candidate = ingest(store, seeded, n=362,
                       destination="skill:fixture-quoted",
                       klass="reference_knowledge", subject=subject,
                       claim="restart the gateway before the dispatcher",
                       terms=("restart",))
    result = promote_with_homes(store, candidate, homes,
                                backup_root=tmp_path / "backups", now=NOW_ISO)
    assert result.outcome == "promoted"
    fields, body = load_skill(homes.skills, "fixture-quoted")
    assert fields["description"] == subject
    text = (homes.skills / "fixture-quoted" / "SKILL.md").read_text(
        encoding="utf-8")
    block = text[4:text.find("\n---\n", 4)]
    assert yaml.safe_load(block)["description"] == subject
    assert f"## {subject}" in body


def test_hot_promotion_with_colon_subject_end_to_end(store, homes, seeded,
                                                     tmp_path):
    subject = "environment: shell defaults"
    candidate = ingest(store, seeded, n=363, destination="memory:hot",
                       klass="runtime_memory_hot", subject=subject,
                       claim="darwin shells default to zsh everywhere",
                       terms=("zsh",))
    result = promote_with_homes(store, candidate, homes,
                                backup_root=tmp_path / "backups", now=NOW_ISO)
    assert result.outcome == "promoted"
    index = (homes.memory / "MEMORY.md").read_text(encoding="utf-8")
    assert subject in index


def test_read_back_refuses_loader_invalid_frontmatter(tmp_path):
    """A file whose frontmatter a standard production YAML loader cannot load
    (the exact pre-fix `component: state` shape) never earns read-back proof."""
    pytest.importorskip("yaml")
    home = tmp_path / "memory"
    home.mkdir()
    adapter = MemoryDestination(home, "warm")
    record = _make_record("memory:warm", "component: state",
                          claim="a claim body")
    adapter.fact_path(record).write_text(
        "---\nname: fixture\ndescription: component: state\n"
        "metadata:\n  type: project\n---\n\na claim body\n", encoding="utf-8")
    with pytest.raises(ReadBackError, match="standard YAML"):
        adapter.read_back(record)


def test_read_back_refuses_description_that_does_not_round_trip(tmp_path):
    home = tmp_path / "memory"
    home.mkdir()
    adapter = MemoryDestination(home, "warm")
    record = _make_record("memory:warm", "true", claim="a claim body")
    # Loader-VALID frontmatter whose plain `true` loads as a boolean, not the
    # subject string: proof is refused because the value does not round-trip.
    adapter.fact_path(record).write_text(
        "---\nname: fixture\ndescription: true\n---\n\na claim body\n",
        encoding="utf-8")
    with pytest.raises(ReadBackError, match="round-trip"):
        adapter.read_back(record)


# -- nonblocking hardening -------------------------------------------------------

def test_combined_revision_direct_path_is_private():
    assert not hasattr(destinations, "combined_revision")
    assert callable(destinations._combined_revision)


def test_reused_store_with_receiptless_promoted_row_is_refused(
        store, homes, seeded, tmp_path, sample_projects):
    healthy = ingest(store, seeded, n=371, subject="healthy fact",
                     claim="the healthy fact is promoted with a receipt",
                     terms=("healthy",))
    assert promote_with_homes(store, healthy, homes,
                              backup_root=tmp_path / "backups",
                              now=NOW_ISO).outcome == "promoted"
    store.audit_promotion_invariants()   # healthy store passes

    forged = ingest(store, seeded, n=372, subject="forged fact",
                    claim="the forged fact was never actually written",
                    terms=("forged",))
    store._conn.execute(
        "UPDATE candidates SET status='promoted' WHERE candidate_id=?",
        (forged["candidate_id"],))
    clean = ingest(store, seeded, n=373, subject="clean fact",
                   claim="the clean fact would otherwise promote",
                   terms=("clean",))
    receipt, _ = make_receipt(clean, receipt_id="receipt-000000000000000373",
                              backup_root=store.backup_root)
    with pytest.raises(StoreError, match="without a write receipt"):
        store.promote_candidate(clean["candidate_id"], receipt, now=NOW_ISO,
                                content_revision=1)
    assert store.counts()["write_receipts"] == 1
    # A fresh writable handle on the reused store is refused at open.
    with pytest.raises(StoreError, match="without a write receipt"):
        ContinuityStore(store.path)
