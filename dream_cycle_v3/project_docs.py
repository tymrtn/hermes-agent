"""Portable, read-only, confined project-document reader (Phase 3).

The Phase 3 wake/lookup read paths need exactly one thing from the Phase 2
destinations module: parsing '## ' sections out of a project document under
an explicit home. destinations.py is writer-heavy and imports POSIX-only
fcntl at module scope, so importing it just to read breaks Windows. This
module is standard library only, portable, and cannot write.

Confinement matches the destination adapters' read boundary: the target must
stay below the explicit home and no path component at or below the home —
including the home directory itself — may be a symlink, so a planted link
(e.g. a symlinked ``dream-cycle-v3/projects`` anchor) can never route reads
outside the declared home. Ancestors *above* the explicit home are trusted
as given: callers resolve the profile home once at the boundary and pass the
resolved path. On platforms without O_NOFOLLOW the lstat-based component
walk is the enforcement; O_NOFOLLOW is applied additionally where the OS
supports it.
"""
from __future__ import annotations

import os
import stat
from pathlib import Path

from .contracts import PROJECT_ID_RE
from .errors import DreamCycleError, ReadBackError

_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)


def assert_confined_read_target(home: Path, target: Path, *,
                                what: str = "read target") -> None:
    """Refuse a target outside *home* or reachable through a symlink,
    including a symlinked *home* anchor itself. relative_to() is lexical
    and keeps '..' parts, so dot components are refused explicitly — a
    below-home prefix must never walk back above the home."""
    if home.is_symlink():
        raise DreamCycleError(
            f"{what} home {home} is a symlink; refusing to read through it")
    try:
        rel = target.relative_to(home)
    except ValueError as exc:
        raise DreamCycleError(
            f"{what} {target} escapes explicit home {home}") from exc
    if any(part in ("..", ".") for part in rel.parts):
        raise DreamCycleError(
            f"{what} {target} traverses out of explicit home {home}; "
            "refusing")
    current = home
    for part in rel.parts:
        current = current / part
        if current.is_symlink():
            raise DreamCycleError(
                f"{what} {current} is a symlink; refusing to read bytes "
                "through it")
        if not current.exists():
            return


def confined_read_bytes(home: Path, target: Path) -> bytes:
    """Read a confined regular file without following a final symlink."""
    assert_confined_read_target(home, target)
    st = os.lstat(target)
    if not stat.S_ISREG(st.st_mode):
        raise DreamCycleError(
            f"read target {target} is not a regular file; refusing to read "
            "bytes through it")
    fd = os.open(str(target), os.O_RDONLY | _O_NOFOLLOW)
    with os.fdopen(fd, "rb") as fh:
        return fh.read()


def read_project_doc_sections(home: Path | str, project_id: str, doc: str
                              ) -> list[tuple[str, str]]:
    """Production-shaped doc reader: '## ' sections of the project document.

    project_id arrives from the continuity store, so it is validated at
    read time against the registry grammar (a valid store can never hold a
    path-shaped id); *doc* must be a single plain path component. Both are
    refused before any path is built.
    """
    if not isinstance(project_id, str) or not PROJECT_ID_RE.fullmatch(project_id):
        raise DreamCycleError(
            f"project id {project_id!r} violates the registry grammar; "
            "refusing to read a project document for it")
    if (not isinstance(doc, str) or not doc or doc in (".", "..")
            or "/" in doc or "\\" in doc or "\x00" in doc):
        raise DreamCycleError(
            f"project document name {doc!r} is not a plain file name; "
            "refusing")
    home = Path(home)
    path = home / project_id / f"{doc}.md"
    if not path.is_file():
        raise ReadBackError(f"{path}: no such project document")
    sections: list[tuple[str, str]] = []
    heading, buf = None, []
    for line in confined_read_bytes(home, path).decode("utf-8").splitlines():
        if line.startswith("## "):
            if heading is not None:
                sections.append((heading, "\n".join(buf)))
            heading, buf = line[3:].strip(), []
        elif heading is not None:
            buf.append(line)
    if heading is not None:
        sections.append((heading, "\n".join(buf)))
    return sections
