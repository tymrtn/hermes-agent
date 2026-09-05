"""Dispatcher board-scope resolution (merge regression, 20260804 review).

With no explicit ``dispatch_boards`` list, no ``kanban.default_board`` and no
``HERMES_KANBAN_BOARD`` env, the dispatcher must fall back to the profile's
home lane via ``kanban_db.get_profile_default_board()`` — never the literal
shared "default" board — so a secondary profile without kanban config
dispatches from its own board, matching ``kb.get_current_board()``.
"""

import hermes_cli.kanban_db_dispatch as _reconciled_hermes_cli_kanban_db_dispatch
from types import SimpleNamespace

from gateway.kanban_watchers import GatewayKanbanWatchersMixin
from hermes_cli import kanban_db


def _fake_kb(profile_default="alpha", boards=None):
    return SimpleNamespace(
        DEFAULT_BOARD="default",
        _normalize_board_slug=lambda s: (str(s).strip().lower() or None) if s else None,
        get_profile_default_board=lambda: profile_default,
        list_boards=lambda include_archived=False: [
            {"slug": slug} for slug in (boards or ["default", "alpha", "beta"])
        ],
    )


def test_fallback_uses_profile_default_board(monkeypatch):
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    slugs = GatewayKanbanWatchersMixin._kanban_scoped_board_slugs(
        {}, "dispatch_boards", _fake_kb(profile_default="alpha")
    )
    assert slugs == ["alpha"]


def test_explicit_default_board_beats_profile_inference(monkeypatch):
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    slugs = GatewayKanbanWatchersMixin._kanban_scoped_board_slugs(
        {"default_board": "custom"}, "dispatch_boards", _fake_kb()
    )
    assert slugs == ["custom"]


def test_env_board_beats_profile_inference(monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_BOARD", "envboard")
    slugs = GatewayKanbanWatchersMixin._kanban_scoped_board_slugs(
        {}, "dispatch_boards", _fake_kb()
    )
    assert slugs == ["envboard"]


def test_explicit_list_and_wildcard_unchanged(monkeypatch):
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    kb = _fake_kb(boards=["default", "alpha"])
    assert GatewayKanbanWatchersMixin._kanban_scoped_board_slugs(
        {"dispatch_boards": "alpha,beta"}, "dispatch_boards", kb
    ) == ["alpha", "beta"]
    assert GatewayKanbanWatchersMixin._kanban_scoped_board_slugs(
        {"dispatch_boards": "*"}, "dispatch_boards", kb
    ) == ["default", "alpha"]


def test_profile_default_failure_falls_back_to_default_board(monkeypatch):
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    kb = _fake_kb()

    def _boom():
        raise RuntimeError("no config")

    kb.get_profile_default_board = _boom
    slugs = GatewayKanbanWatchersMixin._kanban_scoped_board_slugs(
        {}, "dispatch_boards", kb
    )
    assert slugs == ["default"]


def test_admission_scope_stays_wider_than_the_dispatch_scope(monkeypatch):
    """The two scopes answer different questions and must not converge.

    ``dispatch_boards`` picks what this gateway pulls work from; the global
    cap counts what is running anywhere on the machine. Narrowing the count to
    the dispatch scope lets an explicit out-of-scope dispatch (CLI, or the
    dashboard's ``?board=`` nudge) run workers this gateway never sees, and the
    fleet settles at the cap plus that board's load.
    """
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    kb = _fake_kb(boards=["default", "alpha", "beta"])
    cfg = {"dispatch_boards": "alpha"}

    assert GatewayKanbanWatchersMixin._kanban_scoped_board_slugs(
        cfg, "dispatch_boards", kb
    ) == ["alpha"]
    assert _reconciled_hermes_cli_kanban_db_dispatch.resolve_admission_boards(cfg, _kb=kb) == [
        "default", "alpha", "beta",
    ]


def test_admission_scope_keeps_configured_boards_missing_from_disk(monkeypatch):
    """A board the operator named still counts even if discovery misses it."""
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    kb = _fake_kb(boards=["default"])

    assert _reconciled_hermes_cli_kanban_db_dispatch.resolve_admission_boards(
        {"dispatch_boards": "alpha,beta"}, _kb=kb
    ) == ["default", "alpha", "beta"]


def test_admission_scope_falls_back_to_dispatch_scope_when_discovery_fails(
    monkeypatch,
):
    """Unreadable board list degrades the count; it must not empty it."""
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    kb = _fake_kb()

    def _boom(include_archived=False):
        raise OSError("boards dir unreadable")

    kb.list_boards = _boom

    assert _reconciled_hermes_cli_kanban_db_dispatch.resolve_admission_boards({"dispatch_boards": "alpha"}, _kb=kb) == [
        "alpha"
    ]
