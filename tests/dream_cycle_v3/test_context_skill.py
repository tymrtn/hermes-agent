"""Phase 3 project-context skill loader: confined, validated, fail-closed."""
import pytest

from dream_cycle_v3.context_skill import (MAX_SKILL_FILE_BYTES,
                                          SKILL_EXCERPT_BUDGET,
                                          load_context_skill)


def write_skill(skills_home, skill_id, body="## Runbook\nCheck the collector.",
                name=None):
    leaf = name or skill_id.rsplit("/", 1)[-1]
    skill_dir = skills_home / skill_id
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {leaf}\ndescription: project context\n---\n{body}\n",
        encoding="utf-8")
    return skill_dir / "SKILL.md"


def test_flat_skill_loads_bounded_excerpt(tmp_path):
    write_skill(tmp_path, "deploy-runbook", body="## Steps\n" + "step " * 400)
    load = load_context_skill(tmp_path, "deploy-runbook")
    assert load.loaded and load.state == "ok"
    assert 0 < len(load.excerpt) <= SKILL_EXCERPT_BUDGET
    assert load.warning == ""


def test_categorized_skill_id_loads(tmp_path):
    write_skill(tmp_path, "ops/deploy-runbook")
    load = load_context_skill(tmp_path, "ops/deploy-runbook")
    assert load.loaded
    assert "Check the collector." in load.excerpt


def test_unconfigured_and_missing(tmp_path):
    assert load_context_skill(tmp_path, None).state == "unconfigured"
    assert load_context_skill(tmp_path, "").state == "unconfigured"
    assert load_context_skill(None, "some-skill").state == "missing"
    load = load_context_skill(tmp_path, "absent-skill")
    assert load.state == "missing" and not load.excerpt
    assert "absent-skill" in load.warning


@pytest.mark.parametrize("bad_id", [
    "../escape", "a/../b", "/abs/path", "cat/sub/deep", "has space",
    "trailing/", ".hidden", 42,
])
def test_invalid_ids_fail_closed(tmp_path, bad_id):
    load = load_context_skill(tmp_path, bad_id)
    assert load.state == "invalid_id"
    assert not load.excerpt


def test_frontmatter_name_must_match_leaf(tmp_path):
    write_skill(tmp_path, "real-skill", name="different-name")
    load = load_context_skill(tmp_path, "real-skill")
    assert load.state == "malformed" and not load.excerpt


def test_missing_frontmatter_is_malformed(tmp_path):
    d = tmp_path / "bare-skill"
    d.mkdir()
    (d / "SKILL.md").write_text("just text, no frontmatter\n",
                                encoding="utf-8")
    assert load_context_skill(tmp_path, "bare-skill").state == "malformed"


def test_oversized_skill_is_withheld(tmp_path):
    write_skill(tmp_path, "big-skill",
                body="x" * (MAX_SKILL_FILE_BYTES + 100))
    load = load_context_skill(tmp_path, "big-skill")
    assert load.state == "oversized" and not load.excerpt


def test_secret_body_is_withheld(tmp_path):
    write_skill(tmp_path, "leaky-skill",
                body="token sk-AAAAAAAAAAAAAAAAAAAAAAAAAA here")
    load = load_context_skill(tmp_path, "leaky-skill")
    assert load.state == "withheld"
    assert "sk-AAAA" not in load.excerpt + load.warning


def test_pii_is_redacted_not_leaked(tmp_path):
    write_skill(tmp_path, "pii-skill",
                body="Contact maria@example.com or +34 612 345 678.")
    load = load_context_skill(tmp_path, "pii-skill")
    assert load.loaded
    assert "maria@example.com" not in load.excerpt
    assert "612 345 678" not in load.excerpt


def test_symlinked_skill_dir_is_refused(tmp_path):
    outside = tmp_path / "outside"
    write_skill(outside, "stolen-skill", body="## Hidden\nnever emit this")
    skills = tmp_path / "skills"
    skills.mkdir()
    (skills / "stolen-skill").symlink_to(outside / "stolen-skill")
    load = load_context_skill(skills, "stolen-skill")
    assert load.state == "unreadable"
    assert "never emit this" not in load.excerpt + load.warning


def test_symlinked_skill_file_is_refused(tmp_path):
    outside = tmp_path / "outside.md"
    outside.write_text("---\nname: linked-skill\n---\n## Hidden\nsecret\n",
                       encoding="utf-8")
    skills = tmp_path / "skills"
    d = skills / "linked-skill"
    d.mkdir(parents=True)
    (d / "SKILL.md").symlink_to(outside)
    load = load_context_skill(skills, "linked-skill")
    assert load.state == "unreadable"
    assert "secret" not in load.excerpt


def test_symlinked_skills_home_is_refused(tmp_path):
    real = tmp_path / "real-skills"
    write_skill(real, "some-skill")
    link = tmp_path / "linked-skills"
    link.symlink_to(real)
    load = load_context_skill(link, "some-skill")
    assert load.state == "unreadable" and not load.excerpt


def test_warnings_never_carry_paths(tmp_path):
    for skill_id in ("absent-skill", "ops/absent-too"):
        load = load_context_skill(tmp_path, skill_id)
        assert str(tmp_path) not in load.warning
