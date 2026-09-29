import os
from pathlib import Path

import pytest

from dream_cycle_v3.errors import RootResolutionError
from dream_cycle_v3.roots import (ENV_HERMES_ROOT, CollectionRoots,
                                  prepare_output_root, resolve_hermes_root)


def test_resolution_is_stable(tmp_path):
    (tmp_path / "profile").mkdir()
    a = CollectionRoots.resolve("nagatha", {"profile": tmp_path / "profile"})
    b = CollectionRoots.resolve("nagatha", {"profile": str(tmp_path / "profile")})
    assert a.roots["profile"] == b.roots["profile"]
    assert a.profile == "nagatha"


def test_missing_root_is_loud(tmp_path):
    with pytest.raises(RootResolutionError, match="does not exist"):
        CollectionRoots.resolve("p", {"profile": tmp_path / "nope"})


def test_file_root_is_rejected(tmp_path):
    f = tmp_path / "afile"
    f.write_text("x")
    with pytest.raises(RootResolutionError, match="not a directory"):
        CollectionRoots.resolve("p", {"profile": f})


def test_filesystem_root_is_rejected():
    with pytest.raises(RootResolutionError, match="filesystem root"):
        CollectionRoots.resolve("p", {"profile": "/"})


def test_overlapping_roots_are_rejected(tmp_path):
    (tmp_path / "a" / "b").mkdir(parents=True)
    with pytest.raises(RootResolutionError, match="overlap"):
        CollectionRoots.resolve("p", {"outer": tmp_path / "a",
                                      "inner": tmp_path / "a" / "b"})


def test_empty_inputs_are_rejected(tmp_path):
    with pytest.raises(RootResolutionError):
        CollectionRoots.resolve("", {"profile": tmp_path})
    with pytest.raises(RootResolutionError):
        CollectionRoots.resolve("p", {})
    with pytest.raises(RootResolutionError):
        CollectionRoots.resolve("p", {"bad key!": tmp_path})


def test_hermes_root_env_wins(tmp_path):
    hermes = tmp_path / "custom-hermes"
    hermes.mkdir()
    assert resolve_hermes_root(env={ENV_HERMES_ROOT: str(hermes)}) == hermes


def test_hermes_root_env_missing_path_is_loud(tmp_path):
    with pytest.raises(RootResolutionError):
        resolve_hermes_root(env={ENV_HERMES_ROOT: str(tmp_path / "gone")})


def test_hermes_root_ancestor_walk(tmp_path):
    hermes = tmp_path / ".hermes"
    script = hermes / "profiles" / "nagatha" / "scripts" / "x.py"
    script.parent.mkdir(parents=True)
    script.write_text("# stub")
    assert resolve_hermes_root(env={}, script_path=script) == hermes


def test_hermes_root_home_fallback(tmp_path):
    (tmp_path / ".hermes").mkdir()
    assert resolve_hermes_root(env={}, script_path=None, home=tmp_path) == \
        tmp_path / ".hermes"
    with pytest.raises(RootResolutionError):
        resolve_hermes_root(env={}, script_path=None, home=tmp_path / "empty")


def test_output_root_refuses_direct_collection_root_without_mutation(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    marker = src / "existing.txt"
    marker.write_text("unchanged", encoding="utf-8")

    with pytest.raises(RootResolutionError, match="inside collection root"):
        prepare_output_root(src, forbidden_within={"profile": src})

    assert list(src.iterdir()) == [marker]
    assert marker.read_text(encoding="utf-8") == "unchanged"


def test_output_root_refuses_nested_collection_path_without_mutation(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    forbidden = src / "missing" / "nested" / "out"

    with pytest.raises(RootResolutionError, match="inside collection root"):
        prepare_output_root(forbidden, forbidden_within={"profile": src})

    assert not (src / "missing").exists()


def test_output_root_refuses_lexical_containment_before_following_symlink(
        tmp_path):
    src = tmp_path / "src"
    safe = tmp_path / "safe"
    src.mkdir()
    safe.mkdir()
    egress = src / "egress"
    egress.symlink_to(safe, target_is_directory=True)
    requested = egress / "nested" / "out"

    with pytest.raises(RootResolutionError, match="inside collection root"):
        prepare_output_root(requested, forbidden_within={"profile": src})

    assert list(src.iterdir()) == [egress]
    assert not (safe / "nested").exists()


def test_output_root_valid_external_path_is_created(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    out = prepare_output_root(tmp_path / "out", forbidden_within={"profile": src})
    assert out.is_dir()


def test_output_root_valid_external_symlink_alias_is_created(tmp_path):
    src = tmp_path / "src"
    safe = tmp_path / "safe"
    src.mkdir()
    safe.mkdir()
    alias = tmp_path / "safe-alias"
    alias.symlink_to(safe, target_is_directory=True)

    out = prepare_output_root(alias / "nested" / "out",
                              forbidden_within={"profile": src})

    assert out == (safe / "nested" / "out").resolve()
    assert out.is_dir()


def test_output_root_refuses_resolved_containment_without_source_mutation(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    alias = tmp_path / "source-alias"
    alias.symlink_to(src, target_is_directory=True)
    forbidden = alias / "nested" / "out"

    with pytest.raises(RootResolutionError, match="inside collection root"):
        prepare_output_root(forbidden, forbidden_within={"profile": src})

    assert not (src / "nested").exists()


def test_output_root_refuses_symlink_mediated_existing_parent_without_mutation(
        tmp_path):
    src = tmp_path / "src"
    outside = tmp_path / "outside"
    src.mkdir()
    outside.mkdir()
    (outside / "parent-link").symlink_to(src, target_is_directory=True)
    requested = outside / "parent-link" / "missing" / "out"

    with pytest.raises(RootResolutionError, match="inside collection root"):
        prepare_output_root(requested, forbidden_within={"profile": src})

    assert not (src / "missing").exists()


def test_output_root_symlink_swap_race_is_zero_mutation(tmp_path, monkeypatch):
    """A safe alias swapped to a forbidden source after preflight must be
    rejected before mkdir can follow the changed symlink."""
    src = tmp_path / "src"
    safe = tmp_path / "safe"
    src.mkdir()
    safe.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(safe, target_is_directory=True)
    requested = alias / "nested" / "out"

    original_resolve = Path.resolve
    swapped = False

    def swap_after_preflight(self, *args, **kwargs):
        nonlocal swapped
        resolved = original_resolve(self, *args, **kwargs)
        if self == requested and not swapped:
            swapped = True
            alias.unlink()
            alias.symlink_to(src, target_is_directory=True)
        return resolved

    monkeypatch.setattr(Path, "resolve", swap_after_preflight)
    with pytest.raises(RootResolutionError):
        prepare_output_root(requested, forbidden_within={"profile": src})

    assert not (src / "nested").exists()
    assert not (safe / "nested").exists()


def test_output_root_post_resolve_swap_cannot_redirect_creation(
        tmp_path, monkeypatch):
    """A swap immediately after the post-create resolve cannot redirect the
    descriptor-relative mkdir into the forbidden tree."""
    src = tmp_path / "src"
    safe = tmp_path / "safe"
    parked = tmp_path / "safe-parked"
    src.mkdir()
    safe.mkdir()
    requested = safe / "nested" / "out"

    original_resolve = Path.resolve

    def swap_after_post_resolve(self, *args, **kwargs):
        resolved = original_resolve(self, *args, **kwargs)
        if self == requested and kwargs.get("strict") is True:
            safe.rename(parked)
            safe.symlink_to(src, target_is_directory=True)
        return resolved

    monkeypatch.setattr(Path, "resolve", swap_after_post_resolve)
    with pytest.raises(RootResolutionError):
        prepare_output_root(requested, forbidden_within={"profile": src})

    assert not (src / "nested").exists()
    assert not (parked / "nested").exists()


def test_output_root_rejects_forbidden_root_substitution_after_snapshot(
        tmp_path, monkeypatch):
    """Moving the snapshotted forbidden inode into output ancestry and
    replacing its original path with a decoy cannot redirect creation."""
    src = tmp_path / "src"
    safe = tmp_path / "safe"
    src.mkdir()
    safe.mkdir()
    requested = safe / "slot" / "out"
    moved_src = safe / "slot"
    original_resolve = Path.resolve
    swapped = False

    def substitute_after_snapshot(self, *args, **kwargs):
        nonlocal swapped
        resolved = original_resolve(self, *args, **kwargs)
        if self == requested and not swapped:
            swapped = True
            src.rename(moved_src)
            src.mkdir()
        return resolved

    monkeypatch.setattr(Path, "resolve", substitute_after_snapshot)
    with pytest.raises(RootResolutionError, match="changed identity"):
        prepare_output_root(requested, forbidden_within={"profile": src})

    assert moved_src.is_dir()
    assert not (moved_src / "out").exists()


def test_output_root_rejects_forbidden_inode_renamed_into_ancestry(
        tmp_path, monkeypatch):
    """Renaming the forbidden inode into an unopened output component cannot
    evade descriptor-ancestry validation or cause a child mkdir within it."""
    src = tmp_path / "src"
    safe = tmp_path / "safe"
    src.mkdir()
    safe.mkdir()
    requested = safe / "slot" / "out"
    moved_src = safe / "slot"

    original_open = os.open
    swapped = False

    def rename_before_component_open(path, flags, *args, **kwargs):
        nonlocal swapped
        if path == "slot" and not swapped:
            swapped = True
            src.rename(moved_src)
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", rename_before_component_open)
    monkeypatch.setattr(
        os, "supports_dir_fd", os.supports_dir_fd | {rename_before_component_open})
    with pytest.raises(RootResolutionError, match="descriptor ancestry"):
        prepare_output_root(requested, forbidden_within={"profile": src})

    assert moved_src.is_dir()
    assert not (moved_src / "out").exists()
