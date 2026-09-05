"""Every production dispatch entry point must admit under the same caps.

A concurrency cap is only a cap if *every* thing that can spawn a worker
resolves it. The gateway watcher was the only caller passing the configured
global / per-assignee caps and board scope, so a dashboard "dispatch" click,
``hermes kanban dispatch`` and the long-lived daemon each admitted workers
outside the machine-global admission lock — the fleet could sit at 3× the
configured ceiling with nothing in the logs to say why.

Also covers the board-scope half. ``kanban.dispatch_boards`` decides which
boards a dispatcher *pulls work from*; the global cap counts every board,
because an explicit out-of-scope dispatch spawns workers that in-scope ticks
would otherwise never see.
"""

from __future__ import annotations

import hermes_cli.kanban_db_connect as _reconciled_hermes_cli_kanban_db_connect
import hermes_cli.kanban_db_dispatch as _reconciled_hermes_cli_kanban_db_dispatch
import argparse
import json
import threading
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def fleet_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    (home / "profiles" / "alpha").mkdir(parents=True)
    for slug in ("board-a", "board-b", "board-c"):
        kb.create_board(slug=slug, name=slug)
    return home


@pytest.fixture
def captured_dispatch(monkeypatch):
    """Replace ``dispatch_once`` and record the kwargs each caller passes."""
    calls: list[dict] = []

    def fake_dispatch_once(_conn, **kwargs):
        calls.append(kwargs)
        return _reconciled_hermes_cli_kanban_db_dispatch.DispatchResult()

    monkeypatch.setattr(_reconciled_hermes_cli_kanban_db_dispatch, "dispatch_once", fake_dispatch_once)
    return calls


def _config(**kanban):
    return {"kanban": kanban}


ALL_BOARDS = {"default", "board-a", "board-b", "board-c"}


def _create_tasks(board: str, assignee: str, count: int, *, priority: int = 0):
    with _reconciled_hermes_cli_kanban_db_connect.connect_closing(board=board) as conn:
        return [
            kb.create_task(
                conn,
                title=f"{board}-{assignee}-{index}",
                assignee=assignee,
                priority=priority - index,
            )
            for index in range(count)
        ]


def _mark_running(board: str, task_ids: list[str]) -> None:
    with _reconciled_hermes_cli_kanban_db_connect.connect_closing(board=board) as conn:
        for task_id in task_ids:
            conn.execute(
                "UPDATE tasks SET status = 'running' WHERE id = ?", (task_id,)
            )
        conn.commit()


# ---------------------------------------------------------------------------
# Dashboard plugin API — POST /api/plugins/kanban/dispatch
# ---------------------------------------------------------------------------


def test_dashboard_dispatch_nudge_applies_configured_caps_and_scope(
    fleet_home, captured_dispatch, monkeypatch
):
    """The UI nudge skips the tick interval, not the concurrency ceiling."""
    from plugins.kanban.dashboard import plugin_api

    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: _config(
            max_in_progress=3,
            max_in_progress_per_profile=1,
            dispatch_boards="board-a,board-b",
        ),
    )

    plugin_api.dispatch(dry_run=True, max_n=8, board="board-a")

    (kwargs,) = captured_dispatch
    assert kwargs["max_in_progress"] == 3
    assert kwargs["max_in_progress_per_profile"] == 1
    assert set(kwargs["admission_boards"]) == ALL_BOARDS


def test_dashboard_dispatch_nudge_falls_back_to_conservative_defaults(
    fleet_home, captured_dispatch, monkeypatch
):
    """An install that never configured caps still dispatches under them."""
    from plugins.kanban.dashboard import plugin_api

    monkeypatch.setattr("hermes_cli.config.load_config", lambda: _config())

    plugin_api.dispatch(dry_run=True, max_n=8, board="board-a")

    (kwargs,) = captured_dispatch
    assert kwargs["max_in_progress"] == _reconciled_hermes_cli_kanban_db_dispatch.DEFAULT_MAX_IN_PROGRESS
    assert kwargs["max_in_progress_per_profile"] == (
        _reconciled_hermes_cli_kanban_db_dispatch.DEFAULT_MAX_IN_PROGRESS_PER_PROFILE
    )
    assert kwargs["admission_boards"]


def test_dashboard_nudge_on_an_out_of_scope_board_counts_every_board(
    fleet_home, captured_dispatch, monkeypatch
):
    """``?board=`` accepts a board outside the configured dispatch scope.

    The nudge dispatches it anyway, so the cap has to be counted over every
    board — otherwise this click and the gateway's in-scope tick each admit a
    full cap's worth of workers.
    """
    from plugins.kanban.dashboard import plugin_api

    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: _config(dispatch_boards="board-a"),
    )

    plugin_api.dispatch(dry_run=True, max_n=8, board="board-c")

    (kwargs,) = captured_dispatch
    assert "board-c" in kwargs["admission_boards"]
    assert set(kwargs["admission_boards"]) == ALL_BOARDS


# ---------------------------------------------------------------------------
# Long-lived daemon — kanban_db.run_daemon
# ---------------------------------------------------------------------------


def test_run_daemon_applies_configured_caps_and_scope(
    fleet_home, captured_dispatch, monkeypatch
):
    """``hermes kanban daemon`` ticks forever; uncapped it is the worst case."""
    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: _config(
            max_in_progress=5,
            max_in_progress_per_profile=2,
            dispatch_boards=["board-a", "board-c"],
        ),
    )
    stop_event = threading.Event()

    _reconciled_hermes_cli_kanban_db_dispatch.run_daemon(
        interval=0.01,
        stop_event=stop_event,
        on_tick=lambda _res: stop_event.set(),
    )

    assert captured_dispatch, "daemon ran no tick"
    kwargs = captured_dispatch[0]
    assert kwargs["max_in_progress"] == 5
    assert kwargs["max_in_progress_per_profile"] == 2
    assert set(kwargs["admission_boards"]) == ALL_BOARDS


def test_run_daemon_counts_boards_created_after_it_started(
    fleet_home, captured_dispatch, monkeypatch
):
    """A board created mid-run can hold workers, so it has to be counted."""
    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: _config(max_in_progress=5, dispatch_boards="board-a"),
    )
    stop_event = threading.Event()

    def _on_tick(_res):
        if len(captured_dispatch) == 1:
            kb.create_board(slug="board-late", name="board-late")
        else:
            stop_event.set()

    _reconciled_hermes_cli_kanban_db_dispatch.run_daemon(interval=0.01, stop_event=stop_event, on_tick=_on_tick)

    assert len(captured_dispatch) >= 2
    assert "board-late" not in captured_dispatch[0]["admission_boards"]
    assert "board-late" in captured_dispatch[1]["admission_boards"]


# ---------------------------------------------------------------------------
# CLI — hermes kanban dispatch
# ---------------------------------------------------------------------------


def _cli_dispatch(*, as_json: bool = False):
    from hermes_cli import kanban as kb_cli

    return kb_cli._cmd_dispatch(
        argparse.Namespace(
            dry_run=True, max=None, failure_limit=2, json=as_json
        )
    )


def test_cli_dispatch_counts_boards_outside_the_configured_scope(
    fleet_home, captured_dispatch, monkeypatch
):
    """The cap counts every board, even ones outside ``dispatch_boards``.

    board-c is outside the operator's dispatch scope but ``hermes kanban
    dispatch`` will happily tick it when pointed there, so its running workers
    have to be visible to the boards that *are* in scope.
    """
    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: _config(dispatch_boards="board-a,board-b"),
    )

    _cli_dispatch()

    (kwargs,) = captured_dispatch
    assert set(kwargs["admission_boards"]) == ALL_BOARDS


def test_cli_dispatch_without_configured_scope_still_counts_every_board(
    fleet_home, captured_dispatch, monkeypatch
):
    """Unset ``dispatch_boards`` narrows selection, never the count."""
    monkeypatch.setattr("hermes_cli.config.load_config", lambda: _config())

    _cli_dispatch()

    (kwargs,) = captured_dispatch
    assert set(kwargs["admission_boards"]) == ALL_BOARDS
    # Selection is still the profile's own lane — the two scopes differ.
    assert len(_reconciled_hermes_cli_kanban_db_dispatch.resolve_board_scope({})) == 1


def test_cli_dispatch_counts_a_configured_board_missing_from_disk(
    fleet_home, captured_dispatch, monkeypatch
):
    """A named-but-absent scope entry stays in the count, not silently dropped."""
    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: _config(dispatch_boards="board-a,board-ghost"),
    )

    _cli_dispatch()

    (kwargs,) = captured_dispatch
    assert set(kwargs["admission_boards"]) == ALL_BOARDS | {"board-ghost"}


# ---------------------------------------------------------------------------
# The leak itself: an out-of-scope dispatch must not hide its workers
# ---------------------------------------------------------------------------


def test_out_of_scope_dispatch_does_not_let_the_next_tick_exceed_the_cap(
    fleet_home, monkeypatch, capsys
):
    """End-to-end: workers on an out-of-scope board still consume the cap.

    board-c sits outside ``kanban.dispatch_boards``, which is exactly how an
    explicit ``hermes kanban dispatch`` on it gets its workers running. If the
    admission count only spanned the configured scope, the very next in-scope
    tick would read 0 running and admit a full ``max_in_progress`` on top of
    them — 4 workers under a cap of 2.
    """
    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: _config(max_in_progress=2, dispatch_boards="board-a"),
    )
    out_of_scope = _create_tasks("board-c", "alpha", 2, priority=100)
    _mark_running("board-c", out_of_scope)
    _create_tasks("board-a", "alpha", 2, priority=100)
    monkeypatch.setenv("HERMES_KANBAN_BOARD", "board-a")

    _cli_dispatch(as_json=True)

    payload = json.loads(capsys.readouterr().out)
    assert payload["spawned"] == [], (
        "board-c's 2 running workers already fill max_in_progress=2"
    )


# ---------------------------------------------------------------------------
# Uncounted boards fail closed and are reported, not swallowed
# ---------------------------------------------------------------------------


def _break_board_db(board: str) -> Path:
    """Leave ``board`` with a DB file SQLite cannot read."""
    path = kb.kanban_db_path(board=board)
    path.parent.mkdir(parents=True, exist_ok=True)
    header = b"SQLite format 3\x00" + b"\x10\x00\x02\x02\x00\x40\x20\x20"
    header += b"\x00\x00\x00\x0c\x00\x00\x23\x46\x00\x00\x00\x00"
    path.write_bytes(header.ljust(100, b"\x00") + b"garbage pages \x00\x01" * 64)
    kb._INITIALIZED_PATHS.discard(str(path.resolve()))
    return path


def test_cli_json_reports_boards_left_out_of_the_admission_count(
    fleet_home, monkeypatch, capsys
):
    """A degraded cap must be machine-readable, not just an ERROR log line."""
    monkeypatch.setattr(
        "hermes_cli.config.load_config", lambda: _config(max_in_progress=4)
    )
    monkeypatch.setenv("HERMES_KANBAN_BOARD", "board-a")
    _break_board_db("board-c")

    _cli_dispatch(as_json=True)

    payload = json.loads(capsys.readouterr().out)
    assert payload["spawned"] == []
    assert payload["skipped_uncounted_admission"] is True
    assert payload["uncounted_admission_boards"] == ["board-c"]


def test_cli_human_output_warns_about_boards_left_out_of_the_count(
    fleet_home, monkeypatch, capsys
):
    """An operator has to see why dispatch failed closed."""
    monkeypatch.setattr(
        "hermes_cli.config.load_config", lambda: _config(max_in_progress=4)
    )
    monkeypatch.setenv("HERMES_KANBAN_BOARD", "board-a")
    _break_board_db("board-c")

    _cli_dispatch()

    out = capsys.readouterr().out
    assert "WARNING" in out
    assert "dispatch skipped" in out
    assert "board-c" in out


# ---------------------------------------------------------------------------
# kanban.default_assignee is hand-edited YAML and arrives as any type
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("alpha", "alpha"),
        ("  alpha  ", "alpha"),
        ("", None),
        ("   ", None),
        (None, None),
        (42, None),
        (True, None),
        (["alpha"], None),
        ({"profile": "alpha"}, None),
    ],
)
def test_resolve_default_assignee_normalizes_whatever_was_configured(raw, expected):
    """Only a non-empty string is a profile name; the rest read as unset."""
    assert _reconciled_hermes_cli_kanban_db_dispatch.resolve_default_assignee({"default_assignee": raw}) == expected


def test_cli_dispatch_survives_a_non_string_default_assignee(
    fleet_home, captured_dispatch, monkeypatch
):
    """``default_assignee: 42`` is a config typo, not a reason to dispatch nothing.

    The value went straight into ``(cfg or "").strip()``, so an int, a list
    or a mapping raised AttributeError before ``dispatch_once`` was ever
    reached — one optional routing hint with the wrong type silently stopped
    every worker on the machine from being spawned.
    """
    monkeypatch.setattr(
        "hermes_cli.config.load_config", lambda: _config(default_assignee=42)
    )

    assert _cli_dispatch() == 0

    (kwargs,) = captured_dispatch
    assert kwargs["default_assignee"] is None


def test_cli_dispatch_still_passes_a_configured_default_assignee(
    fleet_home, captured_dispatch, monkeypatch
):
    """Guard against "normalize" turning into "always drop it"."""
    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: _config(default_assignee="  alpha  "),
    )

    _cli_dispatch()

    (kwargs,) = captured_dispatch
    assert kwargs["default_assignee"] == "alpha"


def test_cli_human_output_stays_quiet_when_every_board_counted(
    fleet_home, monkeypatch, capsys
):
    """No warning on the healthy path — otherwise it becomes background noise."""
    monkeypatch.setattr(
        "hermes_cli.config.load_config", lambda: _config(max_in_progress=4)
    )
    monkeypatch.setenv("HERMES_KANBAN_BOARD", "board-a")

    _cli_dispatch()

    assert "WARNING" not in capsys.readouterr().out
