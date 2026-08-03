"""Real-path integration tests for dormant-turn persistence + assembly seam.

These exercise the actual ``state.db`` (via a temp ``HERMES_HOME`` supplied by
conftest), the synchronous ``SessionStore.record_principal_activity`` helper,
its ``AsyncSessionStore`` facade, the ``compute_dormant_turn_note``
orchestrator, and the real ``agent/turn_context`` api_content assembly seam —
proving restart durability, that ineligible/future-spoofed turns never touch the
anchor, and that the note rides the api_content sidecar with clean canonical
content and exact replay. (AC3, AC6, AC7, AC10, plus repair gaps 3 & 7.)
"""

import asyncio
from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from agent.turn_context import (
    append_notes_to_multimodal_content,
    compose_user_api_content,
    consume_gateway_turn_context_notes,
    substitute_api_content,
)
from gateway.config import GatewayConfig, Platform, SessionResetPolicy
from gateway.session import AsyncSessionStore, SessionSource, SessionStore
from gateway.dormant_turn_context import (
    build_dormant_turn_note,
    compute_dormant_turn_note,
    principal_hash,
    resolve_config,
)


MADRID = ZoneInfo("Europe/Madrid")
NOW = datetime(2026, 4, 28, 13, 40, 0, tzinfo=MADRID).timestamp()


def _store(tmp_path) -> SessionStore:
    config = GatewayConfig(default_reset_policy=SessionResetPolicy(mode="none"))
    return SessionStore(sessions_dir=tmp_path, config=config)


def _enabled_cfg(verified=("u1",)):
    return resolve_config(
        {
            "gateway": {
                "dormant_turn_context": {
                    "enabled": True,
                    "verified_user_ids": {"telegram": list(verified)},
                }
            }
        }
    )


def test_record_principal_activity_atomic_read_and_replace(tmp_path):
    store = _store(tmp_path)
    key = "principal-a"
    assert store.record_principal_activity(key, 1000.0) is None
    assert store.record_principal_activity(key, 5000.0) == 1000.0
    # Out-of-order (older) event returns the current anchor but must NOT
    # regress the stored max — spoof/skew resistance.
    assert store.record_principal_activity(key, 4000.0) == 5000.0
    assert store.record_principal_activity(key, 6000.0) == 5000.0


def test_record_principal_activity_restart_durability(tmp_path):
    store = _store(tmp_path)
    key = "principal-b"
    store.record_principal_activity(key, 1234.0)
    store2 = _store(tmp_path)
    assert store2.record_principal_activity(key, 9999.0) == 1234.0


def test_record_principal_activity_no_db_is_safe(tmp_path):
    store = _store(tmp_path)
    store._db = None
    assert store.record_principal_activity("principal-c", 1.0) is None


def test_compute_note_first_then_strong_end_to_end(tmp_path):
    store = _store(tmp_path)
    async_store = AsyncSessionStore(store)
    cfg = _enabled_cfg()
    src = SessionSource(
        platform=Platform.TELEGRAM, chat_id="c", chat_type="dm", user_id="u1"
    )

    async def run():
        first = await compute_dormant_turn_note(
            async_store, source=src, is_internal=False, profile=None,
            event_ts=NOW - 3 * 86400, now_epoch=NOW - 3 * 86400, tz=MADRID, config=cfg,
        )
        second = await compute_dormant_turn_note(
            async_store, source=src, is_internal=False, profile=None,
            event_ts=NOW, now_epoch=NOW, tz=MADRID, config=cfg,
        )
        return first, second

    first, second = asyncio.run(run())
    assert first is None
    assert second is not None
    assert "about 3 days" in second
    assert "Tuesday" in second


def test_compute_ineligible_group_never_records_anchor(tmp_path):
    store = _store(tmp_path)
    async_store = AsyncSessionStore(store)
    cfg = _enabled_cfg(verified=("grp-only-sender",))
    group = SessionSource(
        platform=Platform.TELEGRAM, chat_id="c", chat_type="group",
        user_id="grp-only-sender",
    )

    async def run():
        return await compute_dormant_turn_note(
            async_store, source=group, is_internal=False, profile=None,
            event_ts=NOW, now_epoch=NOW, tz=MADRID, config=cfg,
        )

    assert asyncio.run(run()) is None
    key = principal_hash("default", "telegram", "grp-only-sender")
    assert store.record_principal_activity(key, NOW + 1) is None


def test_malformed_enabled_writes_no_anchor(tmp_path):
    """A malformed ``enabled`` (e.g. YAML ``"false"``) never injects nor records.

    ``resolve_config`` fails such a config closed to ``None``; ``compute_dormant_turn_note``
    must then short-circuit before any store access, so the principal anchor stays
    absent even after an otherwise-eligible verified DM turn.
    """
    store = _store(tmp_path)
    async_store = AsyncSessionStore(store)
    # Unique sender: the process-shared state.db is namespaced by principal, so a
    # dedicated id keeps this test independent of the others in this module.
    sender = "malformed-enabled-user"
    src = SessionSource(
        platform=Platform.TELEGRAM, chat_id="c", chat_type="dm", user_id=sender
    )

    for bad in ("false", "true", 1, 0, None, "False"):
        cfg = resolve_config(
            {
                "gateway": {
                    "dormant_turn_context": {
                        "enabled": bad,
                        "verified_user_ids": {"telegram": [sender]},
                    }
                }
            }
        )
        assert cfg is None, bad

        async def run():
            return await compute_dormant_turn_note(
                async_store, source=src, is_internal=False, profile=None,
                event_ts=NOW, now_epoch=NOW, tz=MADRID, config=cfg,
            )

        assert asyncio.run(run()) is None

    # No anchor was ever written: a fresh record on this principal returns None.
    key = principal_hash("default", "telegram", sender)
    assert store.record_principal_activity(key, NOW + 1) is None


def test_compute_future_spoofed_event_does_not_create_or_reset_anchor(tmp_path):
    """gap 3: a far-future event neither injects nor mutates the anchor.

    A legit anchor set 3 days ago must survive a future-spoofed event unchanged,
    so the next legit turn still measures the real 3-day gap rather than a
    fabricated one — and a spoof on a fresh principal creates no anchor at all.
    """
    store = _store(tmp_path)
    async_store = AsyncSessionStore(store)
    cfg = _enabled_cfg(verified=("spoof-user",))
    src = SessionSource(
        platform=Platform.TELEGRAM, chat_id="c", chat_type="dm", user_id="spoof-user"
    )

    async def run():
        # Legit first turn 3 days ago → records anchor, injects nothing.
        first = await compute_dormant_turn_note(
            async_store, source=src, is_internal=False, profile=None,
            event_ts=NOW - 3 * 86400, now_epoch=NOW - 3 * 86400, tz=MADRID, config=cfg,
        )
        # Future-spoofed event (10 days ahead of processing now) → rejected.
        spoof = await compute_dormant_turn_note(
            async_store, source=src, is_internal=False, profile=None,
            event_ts=NOW + 10 * 86400, now_epoch=NOW, tz=MADRID, config=cfg,
        )
        # Real turn now: the anchor must still be the 3-day-old value.
        third = await compute_dormant_turn_note(
            async_store, source=src, is_internal=False, profile=None,
            event_ts=NOW, now_epoch=NOW, tz=MADRID, config=cfg,
        )
        return first, spoof, third

    first, spoof, third = asyncio.run(run())
    assert first is None
    assert spoof is None  # future event injected nothing
    assert third is not None
    assert "about 3 days" in third  # gap measured from the real prior, not spoof


def test_compute_future_spoof_creates_no_anchor_on_fresh_principal(tmp_path):
    store = _store(tmp_path)
    async_store = AsyncSessionStore(store)
    cfg = _enabled_cfg(verified=("fresh-spoof",))
    src = SessionSource(
        platform=Platform.TELEGRAM, chat_id="c", chat_type="dm", user_id="fresh-spoof"
    )

    async def run():
        return await compute_dormant_turn_note(
            async_store, source=src, is_internal=False, profile=None,
            event_ts=NOW + 10 * 86400, now_epoch=NOW, tz=MADRID, config=cfg,
        )

    assert asyncio.run(run()) is None
    # No anchor was written, so a probe still reports no prior activity.
    key = principal_hash("default", "telegram", "fresh-spoof")
    assert store.record_principal_activity(key, NOW) is None


# ---------------------------------------------------------------------------
# gap 7: a real computed dormant note through the api_content assembly seam
# ---------------------------------------------------------------------------


def _real_note():
    cfg = resolve_config(
        {"gateway": {"dormant_turn_context": {"enabled": True}}}
    )
    note = build_dormant_turn_note(NOW, NOW - 3 * 86400, MADRID, cfg)
    assert note  # a genuine strong-layer note
    return note


def test_dormant_note_string_content_clean_and_exactly_replayed():
    note = _real_note()
    # The gateway stages the note on the agent; the prologue consumes it (one
    # shot) and folds it into the user-message api_content composition.
    agent = SimpleNamespace(_gateway_turn_context_notes=note)
    consumed = consume_gateway_turn_context_notes(agent)
    assert consumed == note
    assert consume_gateway_turn_context_notes(agent) == ""  # one-shot

    clean = "hello there"
    api_content = compose_user_api_content(clean, "", consumed)
    assert api_content == f"{clean}\n\n{note}"

    # Persisted row: clean canonical content + exact api_content sidecar.
    stored = {"role": "user", "content": clean, "api_content": api_content}
    replay = dict(stored)
    popped = substitute_api_content(replay)
    assert popped == api_content
    assert replay["content"] == api_content  # replayed byte-for-byte
    assert stored["content"] == clean  # canonical stored content stays clean


def test_dormant_note_multimodal_appends_exactly_one_text_part():
    note = _real_note()
    content = [
        {"type": "text", "text": "hi"},
        {"type": "image", "source": {"type": "base64", "data": "AAAA"}},
    ]
    before = len(content)
    appended = append_notes_to_multimodal_content(content, note)
    assert appended is True
    assert len(content) == before + 1
    assert content[-1] == {"type": "text", "text": note}
    # Pre-existing parts are untouched (canonical multimodal content preserved).
    assert content[0] == {"type": "text", "text": "hi"}
    assert content[1]["type"] == "image"
