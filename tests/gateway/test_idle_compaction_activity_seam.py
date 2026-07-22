"""Gateway-integrated regression for idle compaction and the activity seam.

Idle compaction (``compression.idle_compact_after_seconds``) fires from the
turn prologue (``agent.turn_context.build_turn_context``) by measuring the
wall-clock gap since the session's previous activity. In the gateway, a cached
agent is re-primed for each new external turn by
``GatewayRunner._init_cached_agent_for_turn``, which resets
``agent._last_activity_ts`` to *now* so the 30-minute inactivity watchdog does
not fire before the turn makes its first API call (#9051).

That reset used to happen on the SAME field the idle-gap check reads, so the
gap was always ~0 and idle compaction could never fire in the gateway. The fix
preserves the pre-reset timestamp on a dedicated anchor field
(``_idle_gap_anchor_ts``) that the prologue consumes, so:

- a cached agent resuming after a real idle gap still compacts, and
- a fresh agent (or a cached agent with a short idle) does not.

These tests drive the REAL gateway turn-init against a REAL ``AIAgent`` +
``SessionDB`` and the real prologue, so a regression in either half surfaces.
"""

from __future__ import annotations

import time
from pathlib import Path

from hermes_state import SessionDB

from gateway.run import GatewayRunner

from tests.agent.test_idle_compaction_lock_and_guards import (
    _history,
    _prep_idle_agent,
    _run_prologue,
)


def test_cached_agent_idle_gap_survives_turn_init_and_compacts(tmp_path: Path) -> None:
    """A cached agent idle long enough must still compact after turn-init reset.

    ``_init_cached_agent_for_turn`` resets ``_last_activity_ts`` for the
    watchdog; the idle-compaction gap must be measured from the preserved
    anchor, not the just-reset field, so the trigger still fires.
    """
    db = SessionDB(db_path=tmp_path / "state.db")
    sid = "IDLE_GATEWAY_CACHED"
    db.create_session(sid, source="telegram")
    agent = _prep_idle_agent(db, sid, idle_after=60, idle_gap=3600.0)

    # The gateway re-primes the cached agent for a fresh external turn (depth 0),
    # resetting the watchdog clock immediately before the prologue runs.
    GatewayRunner._init_cached_agent_for_turn(agent, 0)
    assert agent._last_activity_ts >= time.time() - 5  # reset happened

    _run_prologue(agent, _history())

    agent.context_compressor.compress.assert_called_once()


def test_cached_agent_short_idle_does_not_compact(tmp_path: Path) -> None:
    """A cached agent that was active seconds ago must not idle-compact."""
    db = SessionDB(db_path=tmp_path / "state.db")
    sid = "IDLE_GATEWAY_SHORT"
    db.create_session(sid, source="telegram")
    agent = _prep_idle_agent(db, sid, idle_after=60, idle_gap=5.0)

    GatewayRunner._init_cached_agent_for_turn(agent, 0)

    _run_prologue(agent, _history())

    agent.context_compressor.compress.assert_not_called()
    assert agent.session_id == sid


def test_fresh_agent_never_falsely_compacts(tmp_path: Path) -> None:
    """A freshly built agent (never turn-initialised) must not idle-compact.

    A fresh agent's ``_last_activity_ts`` is ~now and it carries no idle
    anchor, so the gap is ~0 even with a large context.
    """
    db = SessionDB(db_path=tmp_path / "state.db")
    sid = "IDLE_GATEWAY_FRESH"
    db.create_session(sid, source="telegram")
    # Fresh: activity is now, and we deliberately do NOT call
    # _init_cached_agent_for_turn (that only runs for cache reuse).
    agent = _prep_idle_agent(db, sid, idle_after=60, idle_gap=0.0)

    _run_prologue(agent, _history())

    agent.context_compressor.compress.assert_not_called()
    assert agent.session_id == sid


def test_interrupt_recursive_turn_stays_inert(tmp_path: Path) -> None:
    """Interrupt-recursive turns (depth > 0) must not idle-compact.

    ``_init_cached_agent_for_turn`` preserves ``_last_activity_ts`` for the
    watchdog at depth > 0; the idle anchor must be pinned to now so a nested
    turn does not compact on the parent turn's pre-resume idle gap.
    """
    db = SessionDB(db_path=tmp_path / "state.db")
    sid = "IDLE_GATEWAY_INTERRUPT"
    db.create_session(sid, source="telegram")
    agent = _prep_idle_agent(db, sid, idle_after=60, idle_gap=3600.0)

    # Interrupt recursion: depth 1. The watchdog clock is preserved, but a
    # nested turn is not a fresh resume and must not trigger idle compaction.
    GatewayRunner._init_cached_agent_for_turn(agent, 1)

    _run_prologue(agent, _history())

    agent.context_compressor.compress.assert_not_called()
