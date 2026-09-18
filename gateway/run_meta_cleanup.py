"""Held progress-bubble cleanup for GatewayRunner (``display.platforms.<p>.cleanup_progress_*``).

``cleanup_progress`` on its own deletes a turn's own progress bubbles the moment its final response
lands (see ``_run_agent_schedule_bubble_cleanup``). Setting ``cleanup_progress_idle_seconds`` or
``cleanup_progress_max_exchanges`` switches to *holding* them instead: ids accumulate in a
session-scoped registry and are deleted together once the session goes quiet, or after N
cleanup-tracked turns. Only bot-owned ids that the cleanup path explicitly tracked are ever
deleted — user messages and final assistant answers are never touched.

A held entry is guarded by a monotonic ``token``: every re-arm supersedes the timer that preceded
it, so a delete scheduled by turn N can never fire against ids turn N+1 is still accumulating.
Bound onto ``GatewayRunner`` via the MRO; ``gateway.run`` internals are imported lazily inside
method bodies (import cycle).
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


async def _delete_held_bubbles(entry: Dict[str, Any]) -> None:
    """Best-effort delete of every tracked bubble in *entry*; deletion is never worth raising for."""
    adapter, chat_id = entry.get("adapter"), entry.get("chat_id")
    if adapter is None or not chat_id:
        return
    for mid in list(entry.get("ids") or []):
        with suppress(Exception):
            await adapter.delete_message(chat_id, str(mid))


def _hold_delay(entry: Dict[str, Any], idle_seconds: float, max_exchanges: int) -> Optional[float]:
    """Seconds to wait before deleting *entry*, or None to keep holding without a timer.

    An exchange limit that has been reached deletes now; one that has not, with no idle window to
    fall back on, has nothing to schedule yet and waits for the next tracked turn.
    """
    if max_exchanges and int(entry.get("exchanges") or 0) >= max_exchanges:
        return 0.0
    if max_exchanges and idle_seconds <= 0:
        return None
    return idle_seconds


class GatewayMetaCleanupMixin:
    """Session-scoped registry of gateway meta bubbles awaiting deletion."""

    @staticmethod
    def _cleanup_retention_window(disp: Any) -> tuple[float, int]:
        """``(idle_seconds, max_exchanges)`` for this turn's platform; ``(0.0, 0)`` = no hold."""
        resolve = disp.resolve_display_setting
        return (
            float(resolve(disp.user_config, disp.platform_key, "cleanup_progress_idle_seconds", 0) or 0),
            int(resolve(disp.user_config, disp.platform_key, "cleanup_progress_max_exchanges", 0) or 0),
        )

    def _meta_cleanup_registry(self, *, create: bool = False) -> Optional[Dict[str, Dict[str, Any]]]:
        """The pending-entry map, lazily created. Partially constructed runners in tests never have
        it, so every reader tolerates its absence."""
        pending = getattr(self, "_meta_cleanup_pending", None)
        if isinstance(pending, dict):
            return pending
        if not create:
            return None
        pending = {}
        self._meta_cleanup_pending = pending
        return pending

    def _arm_meta_cleanup(
        self, session_key: str, entry: Dict[str, Any], *,
        loop: asyncio.AbstractEventLoop, delay: float,
    ) -> None:
        """(Re)arm *entry*'s delete timer, superseding whatever was armed before it."""
        from gateway.run import safe_schedule_threadsafe
        pending = self._meta_cleanup_registry(create=True)
        old_future = entry.get("future")
        if old_future is not None:
            with suppress(Exception):
                old_future.cancel()
        entry["token"] = token = int(entry.get("token") or 0) + 1
        entry["paused"] = False

        async def _delete_when_current() -> None:
            try:
                if delay > 0:
                    await asyncio.sleep(delay)
                current = pending.get(session_key)
                # A newer arm (or a reset flush) owns these ids now; that owner will delete them.
                if not current or current.get("token") != token:
                    return
                pending.pop(session_key, None)
                await _delete_held_bubbles(current)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.debug("Meta message cleanup failed for %s: %s", session_key, exc)

        entry["future"] = safe_schedule_threadsafe(
            _delete_when_current(), loop, logger=logger,
            log_message="Meta message cleanup scheduling error",
        )

    def _schedule_meta_message_cleanup(
        self, *, session_key: Optional[str], chat_id: str, adapter: Any, message_ids: list,
        loop: asyncio.AbstractEventLoop, idle_seconds: float = 0.0, max_exchanges: int = 0,
        count_exchange: bool = True,
    ) -> None:
        """Hold *message_ids* for this session and (re)arm their deletion.

        ``count_exchange=False`` for bubbles that arrive outside a turn boundary (a post-delivery
        background-review notice), so they restart the quiet window without inflating the turn count.
        """
        from gateway.platforms.base import BasePlatformAdapter
        if not session_key or not message_ids or adapter is None:
            return
        if getattr(type(adapter), "delete_message", None) in (None, BasePlatformAdapter.delete_message):
            return

        pending = self._meta_cleanup_registry(create=True)
        entry = pending.setdefault(
            session_key,
            {"chat_id": chat_id, "adapter": adapter, "ids": [], "exchanges": 0, "future": None, "token": 0},
        )
        # A session can move chats (Telegram topic rebind); the newest delivery owns the ids.
        entry["chat_id"], entry["adapter"] = chat_id, adapter
        ids = entry.setdefault("ids", [])
        seen = {str(mid) for mid in ids}
        for mid in message_ids:
            smid = str(mid)
            if smid and smid not in seen:
                ids.append(smid)
                seen.add(smid)
        if count_exchange:
            entry["exchanges"] = int(entry.get("exchanges") or 0) + 1

        delay = _hold_delay(entry, max(0.0, float(idle_seconds or 0.0)), max(0, int(max_exchanges or 0)))
        if delay is None:
            # Exchange-count-only mode: keep holding, with no timer to schedule until the limit.
            future = entry.get("future")
            if future is not None:
                with suppress(Exception):
                    future.cancel()
            entry["future"], entry["paused"] = None, False
            return
        self._arm_meta_cleanup(session_key, entry, loop=loop, delay=delay)

    def _pause_meta_cleanup_for_session(self, session_key: Optional[str]) -> None:
        """Cancel a held session's idle timer for the duration of a fresh turn, so bubbles are never
        deleted out from under a user who is actively interacting."""
        pending = self._meta_cleanup_registry() if session_key else None
        entry = pending.get(session_key) if pending else None
        if not entry:
            return
        future = entry.get("future")
        if future is not None:
            with suppress(Exception):
                future.cancel()
        entry["future"], entry["paused"] = None, True

    def _has_paused_meta_cleanup(self, session_key: Optional[str]) -> bool:
        """True when a session holds ids with no live timer — the state ``_pause_…`` leaves behind."""
        pending = self._meta_cleanup_registry() if session_key else None
        entry = pending.get(session_key) if pending else None
        return bool(entry and entry.get("ids") and entry.get("future") is None)

    def _resume_paused_meta_cleanup_for_session(
        self, session_key: Optional[str], *, loop: asyncio.AbstractEventLoop,
        idle_seconds: float = 0.0, max_exchanges: int = 0,
    ) -> None:
        """Re-arm a paused session after a turn that contributed no bubbles of its own (a failed run,
        or one with progress off). Without this the pause at turn start strands the held ids."""
        if not self._has_paused_meta_cleanup(session_key):
            return
        entry = self._meta_cleanup_registry()[session_key]
        delay = _hold_delay(entry, max(0.0, float(idle_seconds or 0.0)), max(0, int(max_exchanges or 0)))
        if delay is None:
            entry["paused"] = False
            return
        self._arm_meta_cleanup(session_key, entry, loop=loop, delay=delay)

    async def _flush_meta_cleanup_for_session(
        self, session_key: Optional[str], *, platform_key: str = "",
    ) -> None:
        """Delete a session's held bubbles now, at the /new or /reset boundary, instead of leaving
        them to expire minutes later above a fresh conversation. The hold would retire them on its
        own schedule regardless — the session key outlives the reset — so this is about when, not
        whether. ``cleanup_progress_on_reset: false`` opts out; a caller with no *platform_key*
        cannot resolve that per-platform setting and always flushes."""
        pending = self._meta_cleanup_registry() if session_key else None
        if not pending or session_key not in pending:
            return
        if platform_key:
            from gateway.display_config import resolve_display_setting
            from gateway.run import _load_gateway_config
            try:
                user_config = _load_gateway_config()
            except Exception:
                user_config = {}
            if not bool(resolve_display_setting(user_config, platform_key, "cleanup_progress_on_reset", True)):
                return
        entry = pending.pop(session_key, None)
        if not entry:
            return
        future = entry.get("future")
        if future is not None:
            with suppress(Exception):
                future.cancel()
        await _delete_held_bubbles(entry)
