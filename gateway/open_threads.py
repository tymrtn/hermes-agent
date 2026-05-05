"""AFK open-thread ledger for the gateway.

A small per-profile JSON-backed store of "safe open threads" the agent
can pick up while the user is AFK. Storage lives at
``$HERMES_HOME/open_threads.json`` so each profile has its own ledger.

Safety semantics (MVP, cron-driven):

- ``safety`` must be ``"safe"`` for cron to pick a thread up.
- ``side_effects`` must be a subset of :data:`ALLOWED_SIDE_EFFECTS`.
- ``side_effects`` must be disjoint from :data:`BLOCKED_SIDE_EFFECTS`.
- ``status`` must be ``"open"``.
- ``attempt_count`` must be ``< max_attempts`` (default 3).

The store is intentionally tiny — no database, no schema migrations.
All writes go through :func:`utils.atomic_json_write` so a crash mid-
write leaves the previous version intact.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from hermes_constants import get_hermes_home
from hermes_time import now as _hermes_now
from utils import atomic_json_write

logger = logging.getLogger(__name__)

# Tonight's MVP whitelist. ``draft`` is intentionally excluded — the user
# wants no automated drafting tonight even though it would be "soft".
ALLOWED_SIDE_EFFECTS: frozenset[str] = frozenset({
    "research",
    "summarize",
    "internal-note",
    "code-search",
})

# These must NEVER be auto-run by cron, regardless of other flags.
BLOCKED_SIDE_EFFECTS: frozenset[str] = frozenset({
    "email",
    "publish",
    "push",
    "deploy",
    "payment",
    "trade",
    "restart",
    "credential",
    "public-post",
})

ALLOWED_STATUSES: frozenset[str] = frozenset({
    "open",
    "in_progress",
    "done",
    "blocked",
    "abandoned",
})

DEFAULT_MAX_ATTEMPTS = 3
SCHEMA_VERSION = 1

_store_lock = threading.Lock()


def store_path(hermes_home: Optional[Path] = None) -> Path:
    """Return the path to the open-thread ledger for the current profile."""
    home = Path(hermes_home) if hermes_home is not None else get_hermes_home()
    return home / "open_threads.json"


@dataclass
class OpenThread:
    """A single open thread the agent can pick up while AFK."""

    id: str
    title: str
    description: str = ""
    safety: str = "safe"
    side_effects: List[str] = field(default_factory=list)
    status: str = "open"
    result_summary: Optional[str] = None
    created_at: str = ""
    updated_at: str = ""
    attempt_count: int = 0
    last_attempted_at: Optional[str] = None
    max_attempts: int = DEFAULT_MAX_ATTEMPTS
    created_by: Optional[str] = None
    session_key: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "OpenThread":
        # Tolerate forward-compat extras gracefully.
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        clean = {k: v for k, v in data.items() if k in known}
        clean.setdefault("side_effects", [])
        if not isinstance(clean.get("side_effects"), list):
            clean["side_effects"] = list(clean["side_effects"] or [])
        return cls(**clean)

    def is_eligible(self) -> bool:
        """Return True iff cron may auto-run this thread tonight."""
        if self.status != "open":
            return False
        if self.safety != "safe":
            return False
        if self.attempt_count >= self.max_attempts:
            return False
        side_effects = set(self.side_effects or [])
        if not side_effects.issubset(ALLOWED_SIDE_EFFECTS):
            return False
        if side_effects & BLOCKED_SIDE_EFFECTS:
            return False
        return True


def _now_iso() -> str:
    return _hermes_now().isoformat()


def load(hermes_home: Optional[Path] = None) -> List[OpenThread]:
    """Load all threads from disk. Returns empty list when ledger missing."""
    path = store_path(hermes_home)
    if not path.exists():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("open_threads ledger unreadable at %s: %s", path, exc)
        return []
    threads_raw = raw.get("threads") if isinstance(raw, dict) else raw
    if not isinstance(threads_raw, list):
        return []
    return [OpenThread.from_dict(t) for t in threads_raw if isinstance(t, dict)]


def save(threads: Iterable[OpenThread], hermes_home: Optional[Path] = None) -> None:
    """Persist threads atomically."""
    path = store_path(hermes_home)
    payload = {
        "version": SCHEMA_VERSION,
        "threads": [t.to_dict() for t in threads],
    }
    atomic_json_write(path, payload)


def add(
    title: str,
    description: str = "",
    safety: str = "safe",
    side_effects: Optional[List[str]] = None,
    *,
    session_key: Optional[str] = None,
    created_by: Optional[str] = None,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    hermes_home: Optional[Path] = None,
) -> OpenThread:
    """Create and persist a new open thread. Returns the stored thread."""
    title = (title or "").strip()
    if not title:
        raise ValueError("title is required")
    if safety not in {"safe", "needs_review", "blocked"}:
        raise ValueError(
            f"safety must be one of safe/needs_review/blocked, got {safety!r}"
        )
    side_effects = list(side_effects or [])
    blocked_overlap = set(side_effects) & BLOCKED_SIDE_EFFECTS
    if blocked_overlap:
        # Allow recording, but force safety down so cron will not pick it.
        safety = "blocked"
    now = _now_iso()
    thread = OpenThread(
        id=uuid.uuid4().hex[:12],
        title=title,
        description=(description or "").strip(),
        safety=safety,
        side_effects=side_effects,
        status="open",
        created_at=now,
        updated_at=now,
        max_attempts=int(max_attempts),
        created_by=created_by,
        session_key=session_key,
    )
    with _store_lock:
        threads = load(hermes_home)
        threads.append(thread)
        save(threads, hermes_home)
    return thread


def update(
    thread_id: str,
    *,
    status: Optional[str] = None,
    result_summary: Optional[str] = None,
    safety: Optional[str] = None,
    side_effects: Optional[List[str]] = None,
    bump_attempt: bool = False,
    hermes_home: Optional[Path] = None,
) -> Optional[OpenThread]:
    """Update fields on an existing thread. Returns the updated thread or None."""
    if status is not None and status not in ALLOWED_STATUSES:
        raise ValueError(
            f"status must be one of {sorted(ALLOWED_STATUSES)}, got {status!r}"
        )
    with _store_lock:
        threads = load(hermes_home)
        target: Optional[OpenThread] = None
        for t in threads:
            if t.id == thread_id:
                target = t
                break
        if target is None:
            return None
        if status is not None:
            target.status = status
        if result_summary is not None:
            target.result_summary = result_summary
        if safety is not None:
            target.safety = safety
        if side_effects is not None:
            target.side_effects = list(side_effects)
        if bump_attempt:
            target.attempt_count += 1
            target.last_attempted_at = _now_iso()
        target.updated_at = _now_iso()
        save(threads, hermes_home)
        return target


def abandon(
    thread_id: str,
    reason: Optional[str] = None,
    *,
    hermes_home: Optional[Path] = None,
) -> Optional[OpenThread]:
    """Mark a thread abandoned. Reason is stored in result_summary."""
    summary = f"abandoned: {reason}" if reason else "abandoned"
    return update(
        thread_id,
        status="abandoned",
        result_summary=summary,
        hermes_home=hermes_home,
    )


def list_threads(
    status: Optional[str] = "open",
    *,
    hermes_home: Optional[Path] = None,
) -> List[OpenThread]:
    """Return threads, optionally filtered by status. ``status=None`` returns all."""
    threads = load(hermes_home)
    if status is None:
        return threads
    return [t for t in threads if t.status == status]


def eligible_threads(
    *, hermes_home: Optional[Path] = None
) -> List[OpenThread]:
    """Return threads cron is allowed to pick up under MVP rules."""
    return [t for t in load(hermes_home) if t.is_eligible()]


def pick_one(*, hermes_home: Optional[Path] = None) -> Optional[OpenThread]:
    """Return the oldest eligible thread, marking the attempt before returning.

    Bumping ``attempt_count`` *before* the prompt fires guarantees we won't
    spin on the same broken thread if the cron agent crashes mid-run.
    """
    with _store_lock:
        threads = load(hermes_home)
        eligible = [t for t in threads if t.is_eligible()]
        if not eligible:
            return None
        # Oldest first by updated_at, then created_at.
        eligible.sort(key=lambda t: (t.last_attempted_at or "", t.created_at))
        chosen = eligible[0]
        chosen.attempt_count += 1
        chosen.last_attempted_at = _now_iso()
        chosen.updated_at = chosen.last_attempted_at
        save(threads, hermes_home)
        return chosen
