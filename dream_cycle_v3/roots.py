"""Explicit, safe root resolution.

Phase 0-1 rule: no ambient defaults. Every collection root and every output
root is named by the caller and validated here. The v2 ancestor-walk heuristic
is preserved as an explicit, injectable helper for later live wiring, never as
a silent fallback inside collection.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Mapping

from .errors import RootResolutionError

ENV_HERMES_ROOT = "DREAM_CYCLE_V3_HERMES_ROOT"

_ROOT_KEY_MAX = 64


def _validate_root_key(key: str) -> None:
    if not key or len(key) > _ROOT_KEY_MAX:
        raise RootResolutionError(f"root key must be 1-{_ROOT_KEY_MAX} chars: {key!r}")
    if not all(c.isalnum() or c in "-_." for c in key):
        raise RootResolutionError(f"root key has invalid characters: {key!r}")
    if ":" in key:
        raise RootResolutionError(f"root key may not contain ':': {key!r}")


def resolve_dir(raw: str | os.PathLike[str], *, purpose: str) -> Path:
    """Resolve one directory root: expand, resolve symlinks, require existence."""
    p = Path(raw).expanduser()
    try:
        resolved = p.resolve(strict=True)
    except FileNotFoundError:
        raise RootResolutionError(f"{purpose} root does not exist: {p}") from None
    except OSError as exc:
        raise RootResolutionError(f"{purpose} root not resolvable: {p} ({exc})") from None
    if not resolved.is_dir():
        raise RootResolutionError(f"{purpose} root is not a directory: {resolved}")
    if resolved == Path(resolved.anchor):
        raise RootResolutionError(f"{purpose} root may not be the filesystem root")
    return resolved


def is_within(root: Path, path: Path) -> bool:
    """True when `path` (fully resolved) stays inside resolved `root`."""
    try:
        resolved = path.resolve(strict=True)
    except OSError:
        return False
    return resolved == root or resolved.is_relative_to(root)


@dataclass(frozen=True)
class CollectionRoots:
    """Named, validated read roots for one collection run."""

    profile: str
    roots: Mapping[str, Path] = field(default_factory=dict)

    @staticmethod
    def resolve(profile: str, raw_roots: Mapping[str, str | os.PathLike[str]]) -> "CollectionRoots":
        if not profile or not profile.strip():
            raise RootResolutionError("profile name is required")
        if not raw_roots:
            raise RootResolutionError("at least one collection root is required")
        resolved: dict[str, Path] = {}
        for key in sorted(raw_roots):
            _validate_root_key(key)
            resolved[key] = resolve_dir(raw_roots[key], purpose=f"collection[{key}]")
        overlapping = [
            (a, b)
            for a in resolved
            for b in resolved
            if a < b and (resolved[a].is_relative_to(resolved[b]) or resolved[b].is_relative_to(resolved[a]))
        ]
        if overlapping:
            raise RootResolutionError(f"collection roots overlap: {overlapping}")
        return CollectionRoots(profile=profile.strip(), roots=MappingProxyType(resolved))


def resolve_hermes_root(env: Mapping[str, str] | None = None,
                        script_path: Path | None = None,
                        home: Path | None = None) -> Path:
    """Deterministic port of the v2 heuristic, with injectable inputs.

    Order: explicit env var (must exist) -> nearest ancestor named `.hermes`
    of `script_path` -> `<home>/.hermes` (must exist). Unlike v2 this raises
    instead of returning a path that may not exist.
    """
    env = os.environ if env is None else env
    explicit = env.get(ENV_HERMES_ROOT)
    if explicit:
        return resolve_dir(explicit, purpose="hermes (env)")
    if script_path is not None:
        sp = Path(script_path).resolve()
        for candidate in (sp if sp.is_dir() else sp.parent, *sp.parents):
            if candidate.name == ".hermes":
                return resolve_dir(candidate, purpose="hermes (ancestor)")
    base = home if home is not None else Path(os.path.expanduser("~"))
    return resolve_dir(base / ".hermes", purpose="hermes (home)")


def prepare_output_root(raw: str | os.PathLike[str], *,
                        forbidden_within: Mapping[str, Path] | None = None) -> Path:
    """Create/validate a caller-selected output root.

    Refuses an output root nested inside any read root: collectors must never
    collect their own output, and outputs must never land in live source trees.
    """
    def validate(candidate: Path) -> None:
        if candidate == Path(candidate.anchor):
            raise RootResolutionError("output root may not be the filesystem root")
        for key, root in (forbidden_within or {}).items():
            if candidate == root or candidate.is_relative_to(root):
                raise RootResolutionError(
                    f"output root {candidate} is inside collection root "
                    f"'{key}' ({root})")

    p = Path(raw).expanduser()
    forbidden_initial_ids: dict[Path, tuple[int, int]] = {}
    for root in (forbidden_within or {}).values():
        try:
            root_stat = os.stat(root, follow_symlinks=False)
        except OSError as exc:
            raise RootResolutionError(
                f"forbidden collection root unavailable before output "
                f"creation: {root} ({exc})") from None
        forbidden_initial_ids[root] = (root_stat.st_dev, root_stat.st_ino)
    # Check both the normalized spelling and the eventual resolved target
    # before mkdir. The first catches a path lexically under a read root even
    # when a symlink points out; the second catches an outside alias whose
    # existing symlink ancestry points into a read root. resolve(strict=False)
    # resolves every existing ancestor without requiring the leaf to exist.
    lexical = Path(os.path.abspath(p))
    try:
        preflight_resolved = p.resolve(strict=False)
    except OSError as exc:
        raise RootResolutionError(
            f"output root not resolvable before creation: {p} ({exc})") from None
    validate(lexical)
    validate(preflight_resolved)

    if not forbidden_within:
        p.mkdir(parents=True, exist_ok=True)
        try:
            resolved = p.resolve(strict=True)
        except OSError as exc:
            raise RootResolutionError(
                f"output root not resolvable after creation: {p} ({exc})") \
                from None
        validate(resolved)
        return resolved

    # Path.mkdir(parents=True) re-resolves pathname components and can follow
    # an ancestor symlink swapped after preflight. Walk the canonical preflight
    # target from the filesystem root with held directory descriptors instead:
    # each lookup/open is relative to an already-open parent, and O_NOFOLLOW
    # atomically refuses newly introduced symlink components. This preserves
    # valid, pre-existing aliases without letting a later swap redirect mkdir.
    if (os.mkdir not in os.supports_dir_fd
            or os.open not in os.supports_dir_fd
            or not hasattr(os, "O_NOFOLLOW")):
        raise RootResolutionError(
            "safe output-root creation requires dir_fd and O_NOFOLLOW; "
            "refusing a racy mkdir with forbidden read roots")

    creation_target = preflight_resolved
    anchor = Path(creation_target.anchor)
    parts = creation_target.relative_to(anchor).parts
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | os.O_NOFOLLOW
    fds: list[int] = []
    created: list[tuple[int, str, tuple[int, int]]] = []
    forbidden_fds: list[int] = []

    def is_within_forbidden(candidate_fd: int,
                            forbidden_ids: set[tuple[int, int]]) -> bool:
        """Check descriptor ancestry by inode, independent of path renames."""
        probe_fd = os.dup(candidate_fd)
        try:
            while True:
                current = os.fstat(probe_fd)
                current_id = (current.st_dev, current.st_ino)
                if current_id in forbidden_ids:
                    return True
                parent_fd = os.open("..", flags, dir_fd=probe_fd)
                parent = os.fstat(parent_fd)
                parent_id = (parent.st_dev, parent.st_ino)
                os.close(probe_fd)
                probe_fd = parent_fd
                if parent_id == current_id:
                    return False
        finally:
            os.close(probe_fd)

    try:
        for root in forbidden_within.values():
            try:
                root_fd = os.open(str(root), flags)
            except OSError as exc:
                raise RootResolutionError(
                    f"forbidden collection root changed or became "
                    f"unavailable during output creation: {root} ({exc})") \
                    from None
            forbidden_fds.append(root_fd)
            opened = os.fstat(root_fd)
            if (opened.st_dev, opened.st_ino) != forbidden_initial_ids[root]:
                raise RootResolutionError(
                    f"forbidden collection root changed identity during "
                    f"output creation: {root}")
        forbidden_ids = set(forbidden_initial_ids.values())
        current_fd = os.open(str(anchor), flags)
        fds.append(current_fd)
        for part in parts:
            made = False
            try:
                child_fd = os.open(part, flags, dir_fd=current_fd)
            except FileNotFoundError:
                os.mkdir(part, mode=0o700, dir_fd=current_fd)
                made = True
                child_fd = os.open(part, flags, dir_fd=current_fd)
            except OSError as exc:
                raise RootResolutionError(
                    f"output root ancestry is not a stable real directory: "
                    f"{lexical} ({exc})") from None
            fds.append(child_fd)
            child_stat = os.fstat(child_fd)
            if made:
                created.append((current_fd, part,
                                (child_stat.st_dev, child_stat.st_ino)))
            if is_within_forbidden(child_fd, forbidden_ids):
                raise RootResolutionError(
                    f"output root descriptor ancestry enters a forbidden "
                    f"collection root: {lexical}")
            current_fd = child_fd

        try:
            resolved = lexical.resolve(strict=True)
        except OSError as exc:
            raise RootResolutionError(
                f"output root not resolvable after creation: {lexical} "
                f"({exc})") from None
        try:
            descriptor_stat = os.fstat(current_fd)
            path_stat = os.stat(resolved, follow_symlinks=False)
        except OSError as exc:
            raise RootResolutionError(
                f"output root identity unavailable after creation: "
                f"{lexical} ({exc})") from None
        if ((descriptor_stat.st_dev, descriptor_stat.st_ino)
                != (path_stat.st_dev, path_stat.st_ino)):
            raise RootResolutionError(
                f"output root changed identity during creation: {lexical}")
        validate(lexical)
        validate(resolved)
        return resolved
    except BaseException:
        # If post-creation validation detects a race, remove only directories
        # created here, relative to held parents (never by a redirected path).
        for parent_fd, name, created_id in reversed(created):
            try:
                current = os.stat(name, dir_fd=parent_fd,
                                  follow_symlinks=False)
                if (current.st_dev, current.st_ino) == created_id:
                    os.rmdir(name, dir_fd=parent_fd)
            except OSError:
                pass
        raise
    finally:
        for fd in reversed(fds):
            os.close(fd)
        for fd in reversed(forbidden_fds):
            os.close(fd)
