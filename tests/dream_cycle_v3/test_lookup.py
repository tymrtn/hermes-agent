"""Phase 3 continuity_lookup core: bounded typed retrieval, fail closed."""
import json
import sqlite3

import pytest

from dream_cycle_v3.lookup import (MAX_QUERY_RESULTS, LookupBadRequest,
                                   LookupUnavailable, continuity_lookup)
from dream_cycle_v3.store import ContinuityStore

from .conftest import NOW_ISO, make_manifest_for_run

RUN_MANIFEST = make_manifest_for_run(profile="nagatha")


@pytest.fixture
def seeded(tmp_path, sample_projects, sample_threads):
    store_path = tmp_path / "continuity.db"
    with ContinuityStore(store_path) as store:
        store.migrate(NOW_ISO)
        store.record_run(RUN_MANIFEST, "manifest.json", NOW_ISO)
        for project in sample_projects:
            store.upsert_project(project, NOW_ISO)
        for thread in sample_threads:
            store.open_thread(thread, NOW_ISO)
        store.record_disposition(
            thread_id="sample-thread-0008-active-open",
            disposition_date="2026-07-11", run_id=RUN_MANIFEST["run_id"],
            action="continue", reason="still in progress",
            state_after="active", now=NOW_ISO)

    projects_home = tmp_path / "projects"
    (projects_home / "hermes-continuity").mkdir(parents=True)
    (projects_home / "hermes-continuity" / "map.md").write_text(
        "# hermes-continuity: map\n## Purpose\nContinuity architecture.\n",
        encoding="utf-8")
    return {"store": store_path, "projects": projects_home}


@pytest.fixture
def seeded_with_decision(seeded):
    """Read-path fixture shortcut: a promoted decision_record row inserted
    directly (the audited promotion path needs full receipt machinery that is
    exercised in test_promotion.py; lookup only ever reads). Tests using this
    fixture must not reopen the store writable — the Phase 2 receiptless-
    promotion audit would (correctly) refuse it."""
    store_path = seeded["store"]
    conn = sqlite3.connect(store_path)
    conn.execute(
        "INSERT INTO candidates(candidate_id, content_revision, schema_version,"
        " class, project_id, destination, normalized_claim, canonical_subject,"
        " retrieval_terms, evidence_refs, confidence, freshness_class,"
        " sensitivity_class, dedupe_key, status, validation_requirements,"
        " conflict_set, run_id, collector_version, classifier_kind,"
        " classifier_version, content_fingerprint, created_at) VALUES "
        "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("decision-0001-sample-0001", 1, 1, "decision_record",
         "hermes-continuity", "project:hermes-continuity:decisions",
         "The wake packet is bounded to 1600 chars",
         "wake packet budget", '["wake","budget"]', "[]", 0.9, "durable",
         "normal", "dedupe-decision-0001", "promoted", "[]", "[]",
         RUN_MANIFEST["run_id"], "3.0.1", "deterministic", "v1",
         "sha256:decision", NOW_ISO))
    conn.commit()
    conn.close()
    return seeded


def lookup(seeded, **kwargs):
    return continuity_lookup(store_path=seeded["store"],
                             projects_home=seeded["projects"], **kwargs)


# -- selectors / errors -------------------------------------------------------

def test_requires_exactly_one_selector(seeded):
    with pytest.raises(LookupBadRequest):
        lookup(seeded)
    with pytest.raises(LookupBadRequest):
        lookup(seeded, project="hermes-continuity", query="x")
    with pytest.raises(LookupBadRequest):
        lookup(seeded, query="q" * 500)


def test_missing_store_is_typed_unavailable(tmp_path):
    with pytest.raises(LookupUnavailable):
        continuity_lookup(store_path=tmp_path / "absent.db",
                          query="anything")


def test_foreign_store_is_typed_unavailable(tmp_path):
    foreign = tmp_path / "foreign.db"
    conn = sqlite3.connect(foreign)
    conn.execute("CREATE TABLE tasks(id TEXT)")
    conn.commit()
    conn.close()
    with pytest.raises(LookupUnavailable):
        continuity_lookup(store_path=foreign, query="anything")


# -- project ------------------------------------------------------------------

def test_project_payload_shape(seeded_with_decision):
    payload = lookup(seeded_with_decision, project="hermes-continuity")
    assert payload["kind"] == "project" and payload["found"]
    assert payload["canonical_name"] == "Hermes continuity architecture"
    assert payload["task_ssot"] == {"provider": "kanban",
                                    "locator": "sample-board"}
    assert "Purpose" in payload["map_excerpt"]
    assert 0 < len(payload["open_threads"]) <= 10
    # Broker-shaped entries: identity fields plus tracker provenance
    # (identical semantics to the wake path).
    assert all({"thread_id", "title", "state", "task_ref", "as_of",
                "tracker_state", "status_source"} <= set(t)
               for t in payload["open_threads"])
    assert "context_skill" in payload
    decisions = payload["durable_decisions"]
    assert decisions and decisions[0]["claim"].startswith("The wake packet")
    # Typed JSON: fully serializable, no raw rows.
    json.dumps(payload)


def test_project_lookup_by_unique_alias(seeded):
    payload = lookup(seeded, project="klas")
    assert payload["found"] and payload["project_id"] == "klas-sample"


def test_project_lookup_unknown_and_ambiguous(seeded, tmp_path,
                                              sample_projects):
    assert lookup(seeded, project="nope")["found"] is False
    with ContinuityStore(seeded["store"]) as store:
        clone = dict(sample_projects[1])
        clone["project_id"] = "klas-clone"
        clone["aliases"] = ["klas"]
        store.upsert_project(clone, NOW_ISO)
    payload = lookup(seeded, project="klas")
    assert payload["found"] is False
    assert payload["reason"] == "ambiguous_project"


def test_project_never_dumps_raw_tasks(seeded):
    payload = lookup(seeded, project="hermes-continuity")
    text = json.dumps(payload)
    assert "evidence_refs" not in text
    assert "idempotency_key" not in text


# -- thread ---------------------------------------------------------------------

def test_thread_payload_shape(seeded):
    payload = lookup(seeded, thread_id="sample-thread-0008-active-open")
    assert payload["kind"] == "thread" and payload["found"]
    assert payload["owner"] == "nagatha"
    assert payload["state"] == "active"
    assert payload["task_ref"] == "kanban:sample-board:T-1003"
    assert payload["next_action"]
    assert payload["dispositions"][0]["action"] == "continue"
    assert "evidence" not in json.dumps(payload)


def test_thread_unknown(seeded):
    payload = lookup(seeded, thread_id="th_does_not_exist")
    assert payload["found"] is False and payload["reason"] == "unknown_thread"


# -- query ----------------------------------------------------------------------

def test_query_matches_alias_with_confidence(seeded):
    payload = lookup(seeded, query="klas")
    kinds = {r["kind"]: r for r in payload["results"]}
    assert kinds["project"]["id"] == "klas-sample"
    assert kinds["project"]["confidence"] == 0.9
    assert all("as_of" in r for r in payload["results"])


def test_query_matches_threads_and_facts(seeded_with_decision):
    payload = lookup(seeded_with_decision, query="wake packet")
    kinds = [r["kind"] for r in payload["results"]]
    assert "promoted_fact" in kinds


def test_query_bounded(seeded, sample_threads):
    with ContinuityStore(seeded["store"]) as store:
        for i in range(12):
            thread = dict(sample_threads[7])
            thread["thread_id"] = f"needle-thread-{i:02d}-000000"
            thread["idempotency_key"] = f"needle-idem-{i:02d}-000000"
            thread["external_task_ref"] = None
            thread["link_disposition"] = "needs_link"
            thread["title"] = f"needle common phrase {i}"
            store.open_thread(thread, NOW_ISO)
    payload = lookup(seeded, query="needle")
    assert len(payload["results"]) <= MAX_QUERY_RESULTS
    assert payload["truncated"] is True


def test_secretish_content_is_withheld(seeded, sample_threads):
    with ContinuityStore(seeded["store"]) as store:
        thread = dict(sample_threads[7])
        thread["thread_id"] = "secretish-thread-00000"
        thread["idempotency_key"] = "secretish-idem-000000"
        thread["external_task_ref"] = None
        thread["link_disposition"] = "needs_link"
        thread["title"] = "rotate sk-BBBBBBBBBBBBBBBBBBBBBBBB quickly"
        store.open_thread(thread, NOW_ISO)
    payload = lookup(seeded, thread_id="secretish-thread-00000")
    assert payload["title"] == "[privacy_withheld]"
    assert "sk-BBBB" not in json.dumps(payload)


def test_non_normal_sensitivity_facts_never_emitted(seeded_with_decision,
                                                    tmp_path):
    import sqlite3 as _sqlite3
    conn = _sqlite3.connect(seeded_with_decision["store"])
    conn.execute(
        "INSERT INTO candidates(candidate_id, content_revision, schema_version,"
        " class, project_id, destination, normalized_claim, canonical_subject,"
        " retrieval_terms, evidence_refs, confidence, freshness_class,"
        " sensitivity_class, dedupe_key, status, validation_requirements,"
        " conflict_set, run_id, collector_version, classifier_kind,"
        " classifier_version, content_fingerprint, created_at) VALUES "
        "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("decision-0002-medical-001", 1, 1, "decision_record",
         "hermes-continuity", "project:hermes-continuity:decisions",
         "wake packet medical detail that must stay private",
         "wake private detail", '["wake"]', "[]", 0.9, "durable",
         "medical", "dedupe-decision-0002", "promoted", "[]", "[]",
         RUN_MANIFEST["run_id"], "3.0.1", "deterministic", "v1",
         "sha256:decision2", NOW_ISO))
    conn.commit()
    conn.close()
    project = lookup(seeded_with_decision, project="hermes-continuity")
    query = lookup(seeded_with_decision, query="wake packet")
    dump = json.dumps(project) + json.dumps(query)
    assert "medical detail" not in dump


def test_project_open_threads_report_live_tracker_provenance(seeded,
                                                              tmp_path):
    # With a real named-board kanban root, the project's threads carry the
    # SAME live/closed verdicts wake applies: the board-closed thread is
    # dropped, the open one is tracker_live/open.
    import sqlite3 as _sqlite3
    from dream_cycle_v3.dry_run import SAMPLE_DATA
    root = tmp_path / "hermes-root"
    board_dir = root / "kanban" / "boards" / "sample-board"
    board_dir.mkdir(parents=True)
    conn = _sqlite3.connect(board_dir / "kanban.db")
    conn.executescript((SAMPLE_DATA / "kanban_seed.sql").read_text())
    conn.commit()
    conn.close()
    payload = lookup(seeded, project="hermes-continuity", kanban_root=root)
    by_id = {t["thread_id"]: t for t in payload["open_threads"]}
    assert "sample-thread-0001-kanban-done" not in by_id  # closed on board
    live = by_id["sample-thread-0008-active-open"]
    assert live["tracker_state"] == "open"
    assert live["status_source"] == "tracker_live"
    no_ref = by_id["sample-thread-0003-waiting-elapsed"]
    assert no_ref["tracker_state"] is None
    assert no_ref["status_source"] == "no_task_ref"


def test_project_open_threads_stale_without_tracker_root(seeded):
    payload = lookup(seeded, project="hermes-continuity")
    by_id = {t["thread_id"]: t for t in payload["open_threads"]}
    stale = by_id["sample-thread-0001-kanban-done"]  # can't prove closed
    assert stale["tracker_state"] == "stale"
    assert stale["status_source"] == "stored_continuity"


def test_project_context_skill_payload(seeded, sample_projects, tmp_path):
    skills = tmp_path / "skills"
    skill_dir = skills / "hermes-continuity-context"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: hermes-continuity-context\ndescription: ctx\n---\n"
        "## Runbook\nCheck the collector.\n", encoding="utf-8")
    with ContinuityStore(seeded["store"]) as store:
        project = dict(sample_projects[0])
        project["context_skill_id"] = "hermes-continuity-context"
        project["registry_version"] = project["registry_version"] + 1
        store.upsert_project(project, NOW_ISO)
    payload = lookup(seeded, project="hermes-continuity", skills_home=skills)
    skill = payload["context_skill"]
    assert skill["state"] == "ok"
    assert skill["skill_id"] == "hermes-continuity-context"
    assert "Check the collector." in skill["excerpt"]

    missing = lookup(seeded, project="hermes-continuity",
                     skills_home=tmp_path / "no-skills")
    assert missing["context_skill"]["state"] == "missing"
    assert "warning" in missing["context_skill"]


def test_typed_errors_never_embed_store_paths(tmp_path):
    # A raw store path is profile topology; typed error prose stays path-free.
    with pytest.raises(LookupUnavailable) as exc:
        continuity_lookup(store_path=tmp_path / "absent.db", query="x")
    assert str(tmp_path) not in str(exc.value)

    foreign = tmp_path / "foreign.db"
    conn = sqlite3.connect(foreign)
    conn.execute("CREATE TABLE tasks(id TEXT)")
    conn.commit()
    conn.close()
    with pytest.raises(LookupUnavailable) as exc:
        continuity_lookup(store_path=foreign, query="x")
    assert str(tmp_path) not in str(exc.value)


def test_confined_projects_home_symlink_is_withheld(tmp_path, seeded):
    # Profile-shaped layout with the projects ANCHOR symlinked elsewhere:
    # lookup drops it (no map excerpt), never reads through the link.
    import shutil
    home = tmp_path / "root" / "profiles" / "alpha"
    cont = home / "dream-cycle-v3"
    cont.mkdir(parents=True)
    shutil.copy(seeded["store"], cont / "continuity.db")
    foreign = tmp_path / "foreign-projects"
    shutil.copytree(seeded["projects"], foreign)
    (cont / "projects").symlink_to(foreign)
    payload = continuity_lookup(store_path=cont / "continuity.db",
                                projects_home=cont / "projects",
                                project="hermes-continuity",
                                confine_root=home)
    assert payload["found"] is True
    assert payload["map_excerpt"] == ""


def test_lookup_never_writes(seeded):
    before = seeded["store"].read_bytes()
    lookup(seeded, project="hermes-continuity")
    lookup(seeded, query="klas")
    assert seeded["store"].read_bytes() == before
