"""Deterministic routing: ref > path > alias; ambiguity and misses abstain."""
import copy

from dream_cycle_v3.routing import route_observation


def _registry(sample_projects):
    return copy.deepcopy(sample_projects)


def test_explicit_task_ref_wins_over_path_and_alias(sample_projects):
    decision = route_observation(
        text="TODO: verify board task kanban:sample-board:T-1003 for klas",
        source_id="profile:state/loose-threads.md",   # path would say hermes
        registry=_registry(sample_projects))
    assert decision.routed
    assert decision.project_id == "hermes-continuity"
    assert decision.method == "external_task_ref"
    assert decision.matched == "kanban:sample-board:T-1003"


def test_canonical_path_routes_when_no_ref(sample_projects):
    decision = route_observation(
        text="Decision: adopt run-manifest fingerprints",
        source_id="profile:state/loose-threads.md",
        registry=_registry(sample_projects))
    assert decision.project_id == "hermes-continuity"
    assert decision.method == "canonical_path"
    assert decision.matched == "profile:state/"


def test_alias_routes_when_no_ref_or_path(sample_projects):
    decision = route_observation(
        text="Follow-up needed on the klas duplicate-listing verification",
        source_id="profile:klas-notes.txt",
        registry=_registry(sample_projects))
    assert decision.project_id == "klas-sample"
    assert decision.method == "alias"
    assert decision.matched == "klas"


def test_alias_requires_word_boundary(sample_projects):
    decision = route_observation(
        text="the klasifier module is unrelated",
        source_id="profile:klas-notes.txt",
        registry=_registry(sample_projects))
    assert not decision.routed
    assert decision.method == "unresolved"


def test_unmatched_observation_abstains(sample_projects):
    decision = route_observation(
        text="TODO: schedule the quarterly export review",
        source_id="profile:klas-notes.txt",
        registry=_registry(sample_projects))
    assert not decision.routed
    assert decision.method == "unresolved"
    assert decision.project_id is None


def test_ambiguous_ref_abstains(sample_projects):
    registry = _registry(sample_projects)
    clone = copy.deepcopy(registry[0])
    clone["project_id"] = "hermes-clone"
    clone["canonical_paths"] = []
    registry.append(clone)  # two kanban projects share locator 'sample-board'
    decision = route_observation(
        text="check kanban:sample-board:T-1003 please",
        source_id="profile:other.md",
        registry=registry)
    assert not decision.routed
    assert decision.method == "ambiguous"
    assert any("hermes-clone" in d for d in decision.detail)


def test_canonical_path_matches_segment_boundaries_only(sample_projects):
    registry = _registry(sample_projects)
    registry[0]["canonical_paths"] = ["profile:state"]  # no trailing slash

    hit = route_observation(text="Decision: inside the real state dir",
                            source_id="profile:state/loose-threads.md",
                            registry=registry)
    assert hit.project_id == "hermes-continuity"
    assert hit.method == "canonical_path"

    exact = route_observation(text="Decision: the store itself",
                              source_id="profile:state",
                              registry=registry)
    assert exact.project_id == "hermes-continuity"

    # 'profile:stateful/...' shares the prefix but not the segment: abstain.
    miss = route_observation(text="Decision: lookalike directory",
                             source_id="profile:stateful/note.md",
                             registry=registry)
    assert not miss.routed
    assert miss.method == "unresolved"

    registry[0]["canonical_paths"] = ["profile:st"]
    partial = route_observation(text="Decision: partial segment",
                                source_id="profile:state/loose-threads.md",
                                registry=registry)
    assert not partial.routed


def test_ambiguous_path_abstains(sample_projects):
    registry = _registry(sample_projects)
    registry[1]["canonical_paths"] = ["profile:state/"]
    decision = route_observation(
        text="Decision: something in shared territory",
        source_id="profile:state/loose-threads.md",
        registry=registry)
    assert not decision.routed
    assert decision.method == "ambiguous"


def test_todoist_ref_is_provider_scoped(sample_projects):
    decision = route_observation(
        text="close todoist:8000000002 when confirmed",
        source_id="profile:other.md",
        registry=_registry(sample_projects))
    assert decision.project_id == "klas-sample"
    assert decision.method == "external_task_ref"


def test_github_ref_matches_repositories(sample_projects):
    registry = _registry(sample_projects)
    registry[1]["task_ssot"] = {"provider": "github", "locator": None,
                                "write_policy": "read_only"}
    decision = route_observation(
        text="see github:octocat/hello-world#7 for the fix",
        source_id="profile:other.md",
        registry=registry)
    assert decision.project_id == "klas-sample"
    assert decision.matched == "github:octocat/hello-world#7"


def test_wrong_scheme_does_not_match(sample_projects):
    registry = _registry(sample_projects)
    decision = route_observation(
        text="github:someone/elsewhere#1 is unrelated to any registry entry",
        source_id="profile:other.md",
        registry=registry)
    # No github project registered: ref tier misses entirely -> unresolved.
    assert not decision.routed
    assert decision.method == "unresolved"
