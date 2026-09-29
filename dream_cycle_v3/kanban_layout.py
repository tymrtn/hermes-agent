"""Shared explicit-root Kanban path layout (Phase 3 read paths).

Mirrors the canonical hermes_cli.kanban_db on-disk contract, given an
explicit shared Hermes root:

- named board -> ``<root>/kanban/boards/<board>/kanban.db``
- the special ``default`` board -> ``<root>/kanban.db`` (back-compat with
  pre-boards installs; there is deliberately no ``boards/default/`` DB)

Nothing here consults ambient state. ``HERMES_KANBAN_DB`` /
``HERMES_KANBAN_BOARD`` / the current-board pointer are conveniences for a
worker's *current* board and must never redirect a lookup for a different
explicit ``kanban:<board>:<task>`` ref, so they are intentionally ignored.
"""
from __future__ import annotations

import re
from pathlib import Path

DEFAULT_BOARD = "default"
# Matches hermes_cli.kanban_db._BOARD_SLUG_RE semantics: bounded slug that
# cannot traverse (no '/', no '..', no leading separator).
_BOARD_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


def kanban_db_path(kanban_root: Path | str, board: str) -> Path | None:
    """The kanban.db path for *board* under the explicit shared root.

    Returns None for a malformed board key (never a traversal path).
    """
    if not isinstance(board, str) or not _BOARD_RE.match(board):
        return None
    root = Path(kanban_root)
    if board == DEFAULT_BOARD:
        return root / "kanban.db"
    return root / "kanban" / "boards" / board / "kanban.db"
