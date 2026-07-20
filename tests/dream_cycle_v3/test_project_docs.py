"""Confined project-doc reader: lexical traversal can never escape the
explicit home, and DB-sourced project ids are validated at read time
(post-verification finding 1). pathlib's relative_to() keeps '..' parts,
so confinement must reject them explicitly — a hostile registry row like
project_id='../outside' would otherwise read bytes above the home."""
import pytest

from dream_cycle_v3.broker import load_project_context
from dream_cycle_v3.errors import DreamCycleError
from dream_cycle_v3.project_docs import (assert_confined_read_target,
                                         confined_read_bytes,
                                         read_project_doc_sections)


@pytest.fixture
def home(tmp_path):
    home = tmp_path / "projects"
    (home / "good-project").mkdir(parents=True)
    (home / "good-project" / "map.md").write_text(
        "## Purpose\nfine\n", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "map.md").write_text("## Secret\nNOT-YOURS-DOC\n",
                                    encoding="utf-8")
    (tmp_path / "loot.md").write_text("## Loot\nNOT-YOURS-LOOT\n",
                                      encoding="utf-8")
    return home


def test_good_project_still_reads(home):
    sections = read_project_doc_sections(home, "good-project", "map")
    assert sections == [("Purpose", "fine")]


@pytest.mark.parametrize("evil", ["../outside", "a/../../outside",
                                  "good-project/../../outside"])
def test_traversal_project_id_is_refused(home, evil):
    with pytest.raises(DreamCycleError):
        read_project_doc_sections(home, evil, "map")


def test_traversal_doc_name_is_refused(home):
    with pytest.raises(DreamCycleError):
        read_project_doc_sections(home, "good-project", "../../loot")


def test_contract_grammar_enforced_at_read_time(home):
    # Even without separators, an id outside the registry grammar (which a
    # valid store can never contain) is refused before any path is built.
    with pytest.raises(DreamCycleError):
        read_project_doc_sections(home, "UPPER CASE", "map")


def test_confinement_walk_rejects_dotdot_components(home):
    target = home / ".." / "loot.md"
    with pytest.raises(DreamCycleError):
        assert_confined_read_target(home, target)
    with pytest.raises(DreamCycleError):
        confined_read_bytes(home, target)


def test_map_excerpt_fails_closed_on_traversal_id(home):
    project = {"project_id": "../outside", "context_skill_id": None}
    context = load_project_context(project, projects_home=home,
                                   skills_home=None)
    assert context.map_excerpt == ""
    assert "NOT-YOURS-DOC" not in context.map_excerpt
