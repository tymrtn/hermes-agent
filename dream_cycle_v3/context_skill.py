"""Confined, read-only project-context skill loader (Phase 3).

A registry record's ``context_skill_id`` names a local skill under the
calling profile's explicit skills home (Hermes layout:
``<skills_home>/<skill>/SKILL.md`` or the categorized
``<skills_home>/<category>/<skill>/SKILL.md`` — the contract places no
pattern on the id, so a single ``category/skill`` segment pair is accepted).

Loading is fail-closed in every direction:

- the id must match the bounded skill-id grammar (no traversal, no absolute
  paths, at most one category segment);
- the SKILL.md must resolve below the explicit skills home with no symlink
  crossing at or below it (the confined reader refuses a symlinked home);
- the file must be a regular file within MAX_SKILL_FILE_BYTES;
- the frontmatter must parse and its ``name:`` must match the skill id's
  leaf directory;
- the emitted excerpt passes the fail-closed sanitizer (secret scan,
  email/phone redaction, caps) — a privacy hit withholds the whole excerpt.

Any failure yields a typed non-``ok`` state with an honest, path-free
warning — never raw content, never an exception.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

from .errors import DreamCycleError
from .project_docs import confined_read_bytes
from .sanitize import WITHHELD, sanitize_text

_SEGMENT = r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}"
SKILL_ID_RE = re.compile(rf"^{_SEGMENT}(?:/{_SEGMENT})?$")
MAX_SKILL_FILE_BYTES = 64 * 1024
SKILL_EXCERPT_BUDGET = 400

_FRONTMATTER_NAME_RE = re.compile(r"^name:\s*(\S+)\s*$", re.MULTILINE)


@dataclass(frozen=True)
class SkillLoad:
    """Outcome of one context-skill load attempt.

    state: 'ok' | 'unconfigured' | 'invalid_id' | 'missing' | 'oversized' |
           'malformed' | 'unreadable' | 'withheld'
    """

    state: str
    skill_id: str = ""
    excerpt: str = ""
    warning: str = ""

    @property
    def loaded(self) -> bool:
        return self.state == "ok"


def _fail(state: str, skill_id: str, warning: str) -> SkillLoad:
    return SkillLoad(state=state, skill_id=skill_id, excerpt="",
                     warning=warning)


def load_context_skill(skills_home: Path | str | None,
                       skill_id: object,
                       budget: int = SKILL_EXCERPT_BUDGET) -> SkillLoad:
    """Load a bounded, sanitized excerpt of the named project-context skill.

    Never raises and never emits raw content on any failure path. The
    warnings deliberately carry the (grammar-validated) skill id only —
    no filesystem paths.
    """
    try:
        if skill_id is None or skill_id == "":
            return SkillLoad(state="unconfigured")
        if not isinstance(skill_id, str) or not SKILL_ID_RE.match(skill_id):
            return _fail("invalid_id", "",
                         "project context skill id invalid; content withheld")
        if skills_home is None:
            return _fail("missing", skill_id,
                         f"project context skill '{skill_id}' not available "
                         "(no skills home)")
        home = Path(skills_home)
        target = home / skill_id / "SKILL.md"
        if not target.is_file():
            return _fail("missing", skill_id,
                         f"project context skill '{skill_id}' not found")
        try:
            if os.lstat(target).st_size > MAX_SKILL_FILE_BYTES:
                return _fail("oversized", skill_id,
                             f"project context skill '{skill_id}' exceeds "
                             "size limit; content withheld")
            raw = confined_read_bytes(home, target)
        except DreamCycleError:
            return _fail("unreadable", skill_id,
                         f"project context skill '{skill_id}' refused "
                         "(confinement); content withheld")
        except (OSError, ValueError):
            return _fail("unreadable", skill_id,
                         f"project context skill '{skill_id}' unreadable; "
                         "content withheld")
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            return _fail("malformed", skill_id,
                         f"project context skill '{skill_id}' is not valid "
                         "UTF-8; content withheld")

        if not text.startswith("---"):
            return _fail("malformed", skill_id,
                         f"project context skill '{skill_id}' has no "
                         "frontmatter; content withheld")
        end = text.find("\n---", 3)
        if end < 0:
            return _fail("malformed", skill_id,
                         f"project context skill '{skill_id}' frontmatter "
                         "is unterminated; content withheld")
        frontmatter = text[3:end]
        body = text[end + 4:]
        name_match = _FRONTMATTER_NAME_RE.search(frontmatter)
        leaf = skill_id.rsplit("/", 1)[-1]
        if name_match is None or name_match.group(1) != leaf:
            return _fail("malformed", skill_id,
                         f"project context skill '{skill_id}' frontmatter "
                         "name does not match its id; content withheld")

        excerpt = sanitize_text(body, budget)
        if not excerpt:
            return _fail("malformed", skill_id,
                         f"project context skill '{skill_id}' has no body; "
                         "content withheld")
        if WITHHELD in excerpt:
            return _fail("withheld", skill_id,
                         f"project context skill '{skill_id}' content "
                         "withheld (privacy)")
        return SkillLoad(state="ok", skill_id=skill_id, excerpt=excerpt)
    except Exception:
        return _fail("unreadable", "", "project context skill load failed; "
                     "content withheld")
