"""Phase 3 wake broker: bounded lane-scoped packet, activation precedence,
task-authority conflicts, context skill, confinement, fail-closed."""
import sqlite3

import pytest

from dream_cycle_v3.broker import (ActivationEvidence,
                                   project_rows_to_registry,
                                   resolve_project_activation)
from dream_cycle_v3.contracts import parse_iso_datetime
from dream_cycle_v3.store import ContinuityStore
from dream_cycle_v3.wake import (MAP_EXCERPT_BUDGET, PACKET_BUDGET,
                                 THREAD_LIMIT, WakeInputs, WakePacket,
                                 build_wake_packet)

from .conftest import NOW_ISO, make_manifest_for_run

RUN_MANIFEST = make_manifest_for_run(profile="nagatha")
RUN_ID = RUN_MANIFEST["run_id"]
NOW = parse_iso_datetime(NOW_ISO)


def inputs(**overrides) -> WakeInputs:
    base = dict(profile="nagatha", owner="nagatha", now=NOW_ISO,
                first_message="", workspace_path=None,
                session_project_id=None)
    base.update(overrides)
    return WakeInputs(**base)


def evidence(**overrides) -> ActivationEvidence:
    base = dict(message="", workspace_path=None, session_project_id=None)
    base.update(overrides)
    return ActivationEvidence(**base)


def seed_board(root, board="sample-board"):
    """Real named-board layout: <root>/kanban/boards/<board>/kanban.db."""
    from dream_cycle_v3.dry_run import SAMPLE_DATA
    board_dir = root / "kanban" / "boards" / board
    board_dir.mkdir(parents=True)
    conn = sqlite3.connect(board_dir / "kanban.db")
    conn.executescript((SAMPLE_DATA / "kanban_seed.sql").read_text())
    conn.commit()
    conn.close()
    return root


@pytest.fixture
def seeded(tmp_path, sample_projects, sample_threads):
    """Owned store with the sample registry/threads, plus a live-shaped
    kanban root (real named-board layout) and a project-docs home."""
    store_path = tmp_path / "continuity.db"
    with ContinuityStore(store_path) as store:
        store.migrate(NOW_ISO)
        store.record_run(RUN_MANIFEST, "manifest.json", NOW_ISO)
        for project in sample_projects:
            store.upsert_project(project, NOW_ISO)
        for thread in sample_threads:
            store.open_thread(thread, NOW_ISO)

    seed_board(tmp_path)

    projects_home = tmp_path / "projects"
    (projects_home / "hermes-continuity").mkdir(parents=True)
    (projects_home / "hermes-continuity" / "map.md").write_text(
        "# hermes-continuity: map\n"
        "## Purpose\nContinuity architecture for Hermes profiles.\n"
        "## Canonical paths\nprofile:state and profile:sessions.\n",
        encoding="utf-8")
    return {"store": store_path, "kanban_root": tmp_path,
            "projects": projects_home}


def build(seeded, **input_overrides):
    return build_wake_packet(store_path=seeded["store"],
                             projects_home=seeded["projects"],
                             kanban_root=seeded["kanban_root"],
                             skills_home=seeded.get("skills"),
                             inputs=inputs(**input_overrides))


# -- fail closed ------------------------------------------------------------

def test_missing_store_yields_no_packet(tmp_path):
    packet = build_wake_packet(store_path=tmp_path / "absent.db",
                               projects_home=None, kanban_root=None,
                               inputs=inputs())
    assert packet is None


def test_foreign_sqlite_yields_neutral_packet(tmp_path):
    foreign = tmp_path / "foreign.db"
    conn = sqlite3.connect(foreign)
    conn.execute("CREATE TABLE tasks(id TEXT)")
    conn.commit()
    conn.close()
    packet = build_wake_packet(store_path=foreign, projects_home=None,
                               kanban_root=None, inputs=inputs())
    assert packet is not None and packet.degraded
    assert packet.project_id is None and packet.thread_ids == ()
    assert "unavailable" in packet.text
    assert len(packet.text) <= PACKET_BUDGET


def test_garbage_file_yields_neutral_packet(tmp_path):
    garbage = tmp_path / "garbage.db"
    garbage.write_bytes(b"not a sqlite database, definitely" * 10)
    packet = build_wake_packet(store_path=garbage, projects_home=None,
                               kanban_root=None, inputs=inputs())
    assert packet is not None and packet.degraded


def test_empty_owned_store_yields_calm_packet(tmp_path):
    store_path = tmp_path / "continuity.db"
    with ContinuityStore(store_path) as store:
        store.migrate(NOW_ISO)
    packet = build_wake_packet(store_path=store_path, projects_home=None,
                               kanban_root=None, inputs=inputs())
    assert packet is not None and not packet.degraded
    assert "No due owned threads." in packet.text
    assert packet.thread_ids == ()


# -- thread selection lanes / tracker refresh --------------------------------

def test_global_lane_surfaces_only_due_threads(seeded):
    # No project activates: the tiny global lane carries DUE threads only.
    packet = build(seeded)
    assert not packet.degraded
    assert len(packet.thread_ids) <= THREAD_LIMIT
    # 0003's follow-up elapsed => due; every other sample thread is undated
    # or due in the future and must not surface without a project.
    assert packet.thread_ids == ("sample-thread-0003-waiting-elapsed",)
    assert "task tracker" not in packet.text  # no outage this run
    assert len(packet.text) <= PACKET_BUDGET


def test_project_lane_scopes_threads_to_activated_project(seeded):
    # 'dream cycle' alias activates hermes-continuity: only that project's
    # threads are eligible, and the tracker-closed one (T-1001 done on the
    # live board) is dropped.
    packet = build(seeded, first_message="dream cycle status?")
    assert packet.project_id == "hermes-continuity"
    assert 0 < len(packet.thread_ids) <= THREAD_LIMIT
    assert "sample-thread-0001-kanban-done" not in packet.thread_ids
    # klas-sample threads never leak into another project's lane.
    assert "sample-thread-0006-todoist-done" not in packet.thread_ids
    assert "sample-thread-0007-authority-gated" not in packet.thread_ids
    # Due thread sorts first.
    assert packet.thread_ids[0] == "sample-thread-0003-waiting-elapsed"


def test_owner_filter_excludes_foreign_threads(seeded):
    packet = build(seeded, owner="someone-else")
    assert packet.thread_ids == ()
    assert "No due owned threads." in packet.text


def test_tracker_outage_marks_stale_never_closes(seeded, tmp_path):
    # Activate the project so kanban-ref threads are in the lane, but point
    # the kanban root somewhere unreadable: refs go stale, nothing closes.
    packet = build_wake_packet(store_path=seeded["store"],
                               projects_home=seeded["projects"],
                               kanban_root=tmp_path / "nonexistent",
                               inputs=inputs(
                                   first_message="dream cycle status?"))
    assert not packet.degraded
    assert packet.tracker_stale
    assert "tracker" in packet.text
    # Outage keeps kanban threads visible (T-1001's thread may surface again
    # because the evidence that closed it is unavailable) — nothing is
    # dropped as closed without tracker evidence.
    assert "status may be stale" in packet.text


def test_wake_never_writes_store_or_tracker(seeded):
    before_store = seeded["store"].read_bytes()
    board_db = (seeded["kanban_root"] / "kanban" / "boards" / "sample-board"
                / "kanban.db")
    before_board = board_db.read_bytes()
    build(seeded, first_message="dream cycle status?")
    assert seeded["store"].read_bytes() == before_store
    assert board_db.read_bytes() == before_board


# -- activation precedence ---------------------------------------------------

def registry_from(sample_projects):
    # Shape the JSON fixtures like stored rows.
    import json as _json
    rows = []
    for p in sample_projects:
        rows.append({
            "project_id": p["project_id"],
            "canonical_name": p["canonical_name"],
            "aliases": _json.dumps(p["aliases"]),
            "canonical_paths": _json.dumps(p.get("canonical_paths", [])),
            "repositories": _json.dumps(p.get("repositories", [])),
            "status": p["status"],
            "sensitivity_policy": p.get("sensitivity_policy", "normal"),
            "last_verified_at": p["last_verified_at"],
            "context_skill_id": p.get("context_skill_id"),
            "task_provider": p["task_ssot"]["provider"],
            "task_locator": p["task_ssot"]["locator"],
        })
    return project_rows_to_registry(rows)


@pytest.fixture
def kanban_root(tmp_path):
    """Live-shaped sample board under a real shared-root layout."""
    return seed_board(tmp_path / "kanban-tier1")


def resolve(sample_projects, kanban_root=None, registry=None, **ev):
    return resolve_project_activation(
        registry=registry if registry is not None
        else registry_from(sample_projects),
        evidence=evidence(**ev), now=NOW, kanban_root=kanban_root)


def test_tier1_explicit_task_ref(sample_projects, kanban_root):
    # The task itself (T-1003) carries project_id=hermes-continuity on the
    # board: canonical task->project evidence, not provider/board inference.
    decision = resolve(sample_projects, kanban_root,
                       message="please finish kanban:sample-board:T-1003")
    assert decision.project_id == "hermes-continuity"
    assert decision.method == "explicit_ref"


def test_tier1_kanban_ref_without_adapter_proof_never_activates(sample_projects):
    # No kanban root => the task adapter cannot prove membership; the ref is
    # not activation evidence (never provider/board inference alone) and the
    # message falls through to weaker tiers (here: no other evidence).
    decision = resolve(sample_projects, None,
                       message="please finish kanban:sample-board:T-1003")
    assert decision.project_id is None
    assert decision.method == "abstain_no_evidence"


def test_tier1_kanban_ref_missing_task_never_activates(sample_projects,
                                                       kanban_root):
    decision = resolve(sample_projects, kanban_root,
                       message="check kanban:sample-board:T-9999")
    assert decision.project_id is None
    assert decision.method == "abstain_no_evidence"


def test_tier1_todoist_ref_is_not_activation_evidence(sample_projects):
    # Todoist ids carry no namespace: provider-level matching alone can never
    # prove the task's project, so the ref cannot activate anything.
    projects = registry_from(sample_projects)
    projects[0] = dict(projects[0],
                       task_ssot={"provider": "todoist", "locator": None})
    decision = resolve(sample_projects, registry=projects,
                       message="finish todoist:8000000001 please")
    assert decision.project_id is None
    assert decision.method == "abstain_no_evidence"


def test_tier1_stale_registry_record_abstains(sample_projects, kanban_root):
    # An archived/expired record activated through an explicit ref is the
    # exact review finding: every tier must abstain on a stale record.
    projects = registry_from(sample_projects)
    projects[0] = dict(projects[0],
                       last_verified_at="2020-01-01T00:00:00+00:00")
    decision = resolve(sample_projects, kanban_root, registry=projects,
                       message="please finish kanban:sample-board:T-1003")
    assert decision.project_id is None
    assert decision.method == "abstain_stale"


def test_tier1_task_project_absent_from_registry_terminally_abstains(
        sample_projects, kanban_root):
    # The task's canonical project_id (hermes-continuity on the board) is
    # not in the registry at all: explicit authority contradicts the
    # registry, so the explicit tier abstains terminally — it must NOT fall
    # through to the workspace/alias tiers even when those would match.
    projects = [p for p in registry_from(sample_projects)
                if p["project_id"] != "hermes-continuity"]
    decision = resolve(sample_projects, kanban_root, registry=projects,
                       message="finish kanban:sample-board:T-1003 for klas",
                       workspace_path="klas:board/notes.md")
    assert decision.project_id is None
    assert decision.method == "abstain_conflict"


def test_tier1_task_ref_contradicting_explicit_project_id_abstains(
        sample_projects, kanban_root):
    # Task authority says hermes-continuity; the message explicitly names
    # klas-sample. Contradictory explicit evidence is terminal ambiguity.
    decision = resolve(sample_projects, kanban_root,
                       message="move kanban:sample-board:T-1003 under "
                               "klas-sample")
    assert decision.project_id is None
    assert decision.method == "abstain_ambiguous"


def test_tier2_stale_registry_record_abstains(sample_projects):
    projects = registry_from(sample_projects)
    projects[0] = dict(projects[0], status="archived")
    decision = resolve(sample_projects, registry=projects,
                       workspace_path="profile:state/notes.md")
    assert decision.project_id is None
    assert decision.method == "abstain_stale"


def test_tier4_stale_registry_record_abstains(sample_projects):
    projects = registry_from(sample_projects)
    projects[1] = dict(projects[1],
                       last_verified_at="2020-01-01T00:00:00+00:00")
    decision = resolve(sample_projects, registry=projects,
                       message="what's new with klas today?")
    assert decision.project_id is None
    assert decision.method == "abstain_stale"


def test_tier1_explicit_project_id(sample_projects):
    decision = resolve(sample_projects,
                       message="status of klas-sample please")
    assert decision.project_id == "klas-sample"
    assert decision.method == "explicit_ref"


def test_tier1_ambiguity_abstains_without_fallthrough(sample_projects):
    # Both project ids named explicitly: contradictory explicit evidence
    # abstains even though 'klas' alone would be a unique alias at tier 4.
    decision = resolve(sample_projects,
                       message="compare hermes-continuity and klas-sample "
                               "(klas)")
    assert decision.project_id is None
    assert decision.method == "abstain_ambiguous"


def test_tier2_workspace_longest_prefix(sample_projects):
    projects = registry_from(sample_projects)
    projects[1] = dict(projects[1],
                       canonical_paths=["profile:"])  # shorter prefix
    decision = resolve(sample_projects, registry=projects,
                       workspace_path="profile:state/notes.md")
    assert decision.project_id == "hermes-continuity"
    assert decision.method == "workspace_path"


def test_tier2_path_collision_abstains(sample_projects):
    projects = registry_from(sample_projects)
    projects[1] = dict(projects[1], canonical_paths=["profile:state/"])
    decision = resolve(sample_projects, registry=projects,
                       workspace_path="profile:state/notes.md")
    assert decision.project_id is None
    assert decision.method == "abstain_ambiguous"


def test_tier3_session_binding_fresh_record(sample_projects):
    decision = resolve(sample_projects,
                       session_project_id="hermes-continuity")
    assert decision.project_id == "hermes-continuity"
    assert decision.method == "session_binding"


def test_tier3_stale_registry_abstains(sample_projects):
    projects = registry_from(sample_projects)
    projects[0] = dict(projects[0], last_verified_at="2026-05-01T00:00:00+00:00")
    decision = resolve(sample_projects, registry=projects,
                       session_project_id="hermes-continuity")
    assert decision.project_id is None
    assert decision.method == "abstain_stale"


def test_tier3_unknown_binding_abstains(sample_projects):
    decision = resolve(sample_projects,
                       session_project_id="deleted-project")
    assert decision.project_id is None
    assert decision.method == "abstain_stale"


def test_tier3_inactive_project_abstains(sample_projects):
    projects = registry_from(sample_projects)
    projects[0] = dict(projects[0], status="dormant")
    decision = resolve(sample_projects, registry=projects,
                       session_project_id="hermes-continuity")
    assert decision.method == "abstain_stale"


def test_tier4_unique_alias(sample_projects):
    decision = resolve(sample_projects,
                       message="what's new with klas today?")
    assert decision.project_id == "klas-sample"
    assert decision.method == "alias"


def test_tier4_alias_collision_abstains(sample_projects):
    projects = registry_from(sample_projects)
    projects[1] = dict(projects[1], aliases=["klas", "continuity"])
    decision = resolve(sample_projects, registry=projects,
                       message="continuity check please")
    assert decision.project_id is None
    assert decision.method == "abstain_ambiguous"


def test_no_evidence_abstains(sample_projects):
    decision = resolve(sample_projects, message="good morning")
    assert decision.project_id is None
    assert decision.method == "abstain_no_evidence"


def test_substring_alias_does_not_match(sample_projects):
    # 'klasificados' contains 'klas' but is not an exact-word alias match.
    decision = resolve(sample_projects, message="klasificados rollout")
    assert decision.project_id is None
    assert decision.method == "abstain_no_evidence"


# -- kanban layout (shared root, default board, env immunity) ----------------

def test_default_board_resolves_at_shared_root(sample_projects, tmp_path):
    # The special 'default' board lives at <root>/kanban.db, NOT under
    # kanban/boards/default/. Canonical task->project evidence must resolve
    # through the real back-compat layout.
    from dream_cycle_v3.dry_run import SAMPLE_DATA
    root = tmp_path / "hermes-root"
    root.mkdir()
    conn = sqlite3.connect(root / "kanban.db")
    conn.executescript((SAMPLE_DATA / "kanban_seed.sql").read_text())
    conn.commit()
    conn.close()
    projects = registry_from(sample_projects)
    projects[0] = dict(projects[0],
                       task_ssot={"provider": "kanban", "locator": "default"})
    decision = resolve(sample_projects, root, registry=projects,
                       message="please finish kanban:default:T-1003")
    assert decision.project_id == "hermes-continuity"
    assert decision.method == "explicit_ref"


def test_ambient_kanban_db_env_never_redirects_explicit_ref(
        sample_projects, kanban_root, tmp_path, monkeypatch):
    # A worker's HERMES_KANBAN_DB pin (its own current board) must not
    # redirect a lookup for a different explicit board's ref: resolution is
    # pure layout under the explicit root.
    from dream_cycle_v3.dry_run import SAMPLE_DATA
    other = tmp_path / "other-board.db"
    conn = sqlite3.connect(other)
    conn.execute("CREATE TABLE tasks (id TEXT PRIMARY KEY, title TEXT, "
                 "status TEXT, assignee TEXT, completed_at INTEGER, "
                 "project_id TEXT)")
    conn.execute("INSERT INTO tasks VALUES ('T-1003', 'impostor', 'todo', "
                 "'x', NULL, 'klas-sample')")
    conn.commit()
    conn.close()
    monkeypatch.setenv("HERMES_KANBAN_DB", str(other))
    monkeypatch.setenv("HERMES_KANBAN_BOARD", "other-board")
    decision = resolve(sample_projects, kanban_root,
                       message="please finish kanban:sample-board:T-1003")
    # Still the real sample-board task's project, not the env-pinned DB's.
    assert decision.project_id == "hermes-continuity"


def test_malformed_board_slug_is_not_evidence(sample_projects, kanban_root):
    # A traversal-shaped board segment can never become a path.
    decision = resolve(sample_projects, kanban_root,
                       message="check kanban:..:T-1003")
    assert decision.project_id is None


# -- packet content / budget / privacy ---------------------------------------

def test_activated_project_includes_bounded_map_excerpt(seeded):
    packet = build(seeded, first_message="dream cycle status?")
    assert packet.project_id == "hermes-continuity"
    assert "Active project: Hermes continuity architecture" in packet.text
    assert "Purpose" in packet.text
    assert len(packet.text) <= PACKET_BUDGET


def test_abstention_loads_no_project_and_no_map(seeded):
    packet = build(seeded, first_message="good morning")
    assert packet.project_id is None
    assert "No project auto-activated" in packet.text
    assert "Purpose" not in packet.text
    assert "continuity_lookup" in packet.text  # handles offered instead


def test_symlinked_map_outside_home_is_refused(seeded, tmp_path):
    secret = tmp_path / "outside.md"
    secret.write_text("## Hidden\nshould never appear\n", encoding="utf-8")
    map_path = seeded["projects"] / "hermes-continuity" / "map.md"
    map_path.unlink()
    map_path.symlink_to(secret)
    packet = build(seeded, first_message="dream cycle status?")
    assert packet.project_id == "hermes-continuity"
    assert "should never appear" not in packet.text
    assert "Hidden" not in packet.text


def test_budget_holds_under_hostile_thread_titles(seeded, sample_threads):
    with ContinuityStore(seeded["store"]) as store:
        for i in range(6):
            thread = dict(sample_threads[7])
            thread["thread_id"] = f"bulky-thread-{i:02d}-0000-0000"
            thread["idempotency_key"] = f"bulky-idem-{i:02d}-0000-0000"
            thread["external_task_ref"] = None
            thread["link_disposition"] = "needs_link"
            thread["follow_up_after"] = "2026-07-01T00:00:00+00:00"  # due
            thread["title"] = "T" * 300
            thread["normalized_next_action"] = "N" * 300
            store.open_thread(thread, NOW_ISO)
    packet = build(seeded)
    assert len(packet.text) <= PACKET_BUDGET
    assert len(packet.thread_ids) <= THREAD_LIMIT


def test_secret_pattern_degrades_to_neutral(seeded, sample_threads):
    with ContinuityStore(seeded["store"]) as store:
        thread = dict(sample_threads[7])
        thread["thread_id"] = "leaky-thread-0000-0000"
        thread["idempotency_key"] = "leaky-idem-0000-00000"
        thread["external_task_ref"] = None
        thread["link_disposition"] = "needs_link"
        thread["follow_up_after"] = "2026-07-01T00:00:00+00:00"
        thread["state"] = "waiting"
        thread["blocked_by"] = "credentials"
        thread["title"] = "rotate key sk-AAAAAAAAAAAAAAAAAAAAAAAAAA now"
        store.open_thread(thread, NOW_ISO)
    packet = build(seeded)
    assert packet.degraded
    assert packet.project_id is None
    assert "sk-AAAA" not in packet.text
    assert "withheld" in packet.text


def test_sensitive_project_gets_no_map_excerpt(seeded, sample_projects):
    with ContinuityStore(seeded["store"]) as store:
        sensitive = dict(sample_projects[0])
        sensitive["sensitivity_policy"] = "legal"
        sensitive["registry_version"] = sensitive["registry_version"] + 1
        store.upsert_project(sensitive, NOW_ISO)
    packet = build(seeded, first_message="dream cycle status?")
    assert packet.project_id == "hermes-continuity"
    assert "withheld (sensitivity policy)" in packet.text
    assert "Purpose" not in packet.text  # map content suppressed
    assert "Await the tracker export" not in packet.text
    assert "Await the tracker export" not in build(seeded).text
    assert "Open thread details withheld (sensitivity policy)" in packet.text
    assert "No open project threads." not in packet.text


def test_packet_identity_is_deterministic(seeded):
    a = build(seeded, first_message="dream cycle status?")
    b = build(seeded, first_message="dream cycle status?")
    assert a.packet_id == b.packet_id
    assert a.content_hash == b.content_hash
    assert a.text == b.text
    assert a.content_hash.startswith("sha256:")


def test_map_excerpt_budget(seeded):
    big = "\n".join(f"## Section {i}\n" + "x" * 300 for i in range(20))
    (seeded["projects"] / "hermes-continuity" / "map.md").write_text(
        f"# hermes-continuity: map\n{big}\n", encoding="utf-8")
    packet = build(seeded, first_message="dream cycle status?")
    assert len(packet.text) <= PACKET_BUDGET
    # The excerpt itself can never exceed its own budget.
    from dream_cycle_v3.broker import _map_excerpt
    excerpt = _map_excerpt(seeded["projects"], "hermes-continuity")
    assert 0 < len(excerpt) <= MAP_EXCERPT_BUDGET


def test_no_backlog_dump_when_abstaining(seeded):
    packet = build(seeded)
    # Registry contents beyond the activated project never leak wholesale.
    assert "Marketplace sample project" not in packet.text
    assert "todoist_export" not in packet.text


# -- context skill in the packet ---------------------------------------------

def write_skill(skills_home, skill_id, body="## Runbook\nCheck the collector "
                                             "before every deploy."):
    leaf = skill_id.rsplit("/", 1)[-1]
    skill_dir = skills_home / skill_id
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {leaf}\ndescription: project context\n---\n{body}\n",
        encoding="utf-8")


def bind_skill(seeded, sample_projects, skill_id):
    with ContinuityStore(seeded["store"]) as store:
        project = dict(sample_projects[0])
        project["context_skill_id"] = skill_id
        project["registry_version"] = project["registry_version"] + 1
        store.upsert_project(project, NOW_ISO)


def test_context_skill_excerpt_included_for_activated_project(
        seeded, sample_projects, tmp_path):
    skills = tmp_path / "skills"
    write_skill(skills, "hermes-continuity-context")
    bind_skill(seeded, sample_projects, "hermes-continuity-context")
    seeded["skills"] = skills
    packet = build(seeded, first_message="dream cycle status?")
    assert "Project context (skill hermes-continuity-context):" in packet.text
    assert "Check the collector" in packet.text
    assert len(packet.text) <= PACKET_BUDGET


def test_categorized_context_skill_id_resolves(seeded, sample_projects,
                                               tmp_path):
    skills = tmp_path / "skills"
    write_skill(skills, "ops/hermes-continuity-context")
    bind_skill(seeded, sample_projects, "ops/hermes-continuity-context")
    seeded["skills"] = skills
    packet = build(seeded, first_message="dream cycle status?")
    assert ("Project context (skill ops/hermes-continuity-context):"
            in packet.text)


def test_missing_context_skill_warns_honestly(seeded, sample_projects,
                                              tmp_path):
    bind_skill(seeded, sample_projects, "vanished-skill")
    seeded["skills"] = tmp_path / "skills-empty"
    packet = build(seeded, first_message="dream cycle status?")
    assert not packet.degraded
    assert "vanished-skill" in packet.text and "not" in packet.text
    assert "Project context (skill" not in packet.text


def test_secret_in_context_skill_never_reaches_packet(seeded, sample_projects,
                                                      tmp_path):
    skills = tmp_path / "skills"
    write_skill(skills, "hermes-continuity-context",
                body="token sk-AAAAAAAAAAAAAAAAAAAAAAAAAA lives here")
    bind_skill(seeded, sample_projects, "hermes-continuity-context")
    seeded["skills"] = skills
    packet = build(seeded, first_message="dream cycle status?")
    assert "sk-AAAA" not in packet.text
    # The withheld-skill warning is honest, not silent.
    assert "withheld" in packet.text


def test_sensitive_project_gets_no_skill_excerpt(seeded, sample_projects,
                                                 tmp_path):
    skills = tmp_path / "skills"
    write_skill(skills, "hermes-continuity-context")
    with ContinuityStore(seeded["store"]) as store:
        project = dict(sample_projects[0])
        project["context_skill_id"] = "hermes-continuity-context"
        project["sensitivity_policy"] = "legal"
        project["registry_version"] = project["registry_version"] + 1
        store.upsert_project(project, NOW_ISO)
    seeded["skills"] = skills
    packet = build(seeded, first_message="dream cycle status?")
    assert "Project context (skill" not in packet.text
    assert "Check the collector" not in packet.text


# -- explicit-root confinement (profile-shaped home) --------------------------

def make_profile_home(tmp_path, seeded, name="alpha"):
    """Real profile layout: <root>/profiles/<name>/dream-cycle-v3/..."""
    import shutil
    home = tmp_path / "root" / "profiles" / name
    cont = home / "dream-cycle-v3"
    cont.mkdir(parents=True)
    shutil.copy(seeded["store"], cont / "continuity.db")
    shutil.copytree(seeded["projects"], cont / "projects")
    return home


def test_confined_build_reads_profile_layout(tmp_path, seeded):
    home = make_profile_home(tmp_path, seeded)
    packet = build_wake_packet(
        store_path=home / "dream-cycle-v3" / "continuity.db",
        projects_home=home / "dream-cycle-v3" / "projects",
        kanban_root=seeded["kanban_root"],
        inputs=inputs(first_message="dream cycle status?"),
        confine_root=home)
    assert packet.project_id == "hermes-continuity"
    assert "Purpose" in packet.text


def test_symlinked_projects_anchor_is_refused(tmp_path, seeded):
    # The projects ANCHOR directory (not just a file inside it) symlinks to
    # another tree: reads through it are refused, packet warns honestly.
    home = make_profile_home(tmp_path, seeded)
    real_projects = home / "dream-cycle-v3" / "projects"
    foreign = tmp_path / "foreign-projects"
    import shutil
    shutil.move(str(real_projects), str(foreign))
    real_projects.symlink_to(foreign)
    packet = build_wake_packet(
        store_path=home / "dream-cycle-v3" / "continuity.db",
        projects_home=real_projects,
        kanban_root=seeded["kanban_root"],
        inputs=inputs(first_message="dream cycle status?"),
        confine_root=home)
    assert "Purpose" not in packet.text
    assert "not confined" in packet.text


def test_symlinked_skills_anchor_is_refused(tmp_path, seeded,
                                            sample_projects):
    home = make_profile_home(tmp_path, seeded)
    foreign_skills = tmp_path / "foreign-skills"
    write_skill(foreign_skills, "hermes-continuity-context",
                body="## Hidden\nforeign skill body")
    (home / "skills").symlink_to(foreign_skills)
    with ContinuityStore(home / "dream-cycle-v3" / "continuity.db") as store:
        project = dict(sample_projects[0])
        project["context_skill_id"] = "hermes-continuity-context"
        project["registry_version"] = project["registry_version"] + 1
        store.upsert_project(project, NOW_ISO)
    packet = build_wake_packet(
        store_path=home / "dream-cycle-v3" / "continuity.db",
        projects_home=home / "dream-cycle-v3" / "projects",
        skills_home=home / "skills",
        kanban_root=seeded["kanban_root"],
        inputs=inputs(first_message="dream cycle status?"),
        confine_root=home)
    assert "foreign skill body" not in packet.text
    assert "not confined" in packet.text


# -- to_dict sanitization / caps ----------------------------------------------

def _packet(**overrides):
    base = dict(packet_id="wk_0001", content_hash="sha256:" + "0" * 64,
                profile="nagatha", built_at=NOW_ISO, project_id=None,
                project_method="abstain_no_evidence", text="hello",
                degraded=False)
    base.update(overrides)
    return WakePacket(**base)


def test_to_dict_withholds_secret_text():
    d = _packet(text="key sk-AAAAAAAAAAAAAAAAAAAAAAAAAA here").to_dict()
    assert "sk-AAAA" not in str(d)
    assert d["text"] == "[privacy_withheld]"


def test_to_dict_caps_oversized_fields():
    import json
    d = _packet(text="x" * 100_000,
                project_id="p" * 5_000,
                thread_ids=tuple(f"t{i}" for i in range(500))).to_dict()
    assert len(json.dumps(d)) <= PACKET_BUDGET + 900
    assert d["project_id"] == "[invalid]"      # identifier over bound
    assert len(d["thread_ids"]) <= THREAD_LIMIT


def test_to_dict_sanitizes_identifiers_and_profile():
    d = _packet(profile="ops name <ops@example.com>",
                project_id="two words").to_dict()
    assert "ops@example.com" not in str(d)
    assert d["project_id"] == "[invalid]"     # whitespace fails ident grammar
    assert d["schema_version"] == 1


def test_phone_shaped_task_ref_never_reaches_wake_text(tmp_path):
    """Post-verification finding 3: a phone-shaped identifier (here an
    external_task_ref, which rides sanitize_identifier, not the redacting
    text sanitizer) must never appear in the packet text; the final packet
    gate must catch PII, not just secret patterns."""
    from .test_tracker_refresh import PROJECT, make_thread
    store_path = tmp_path / "continuity.db"
    with ContinuityStore(store_path) as store:
        store.migrate(NOW_ISO)
        store.upsert_project(PROJECT, NOW_ISO)
        thread = make_thread("phone-thread-0001-000000",
                             "todoist:555-123-4567", "Call back")
        store.open_thread(thread, NOW_ISO)
    packet = build_wake_packet(store_path=store_path, projects_home=None,
                               kanban_root=None, inputs=inputs())
    assert "555-123-4567" not in packet.text
    assert "555-123-4567" not in str(packet.to_dict())


def test_to_dict_withholds_pii_bearing_text():
    """to_dict() re-sanitizes persisted/corrupt state: raw PII surviving in
    the text field means a layer was bypassed — withhold, never emit."""
    from dream_cycle_v3.sanitize import WITHHELD
    packet = WakePacket(
        packet_id="p" * 16, content_hash="sha256:" + "a" * 64,
        profile="nagatha", built_at=NOW_ISO, project_id=None,
        project_method="abstain_no_evidence",
        text="call me at 555-123-4567 about the thing",
        degraded=False)
    assert packet.to_dict()["text"] == WITHHELD
