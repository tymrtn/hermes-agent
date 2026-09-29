"""Typed adapter results.

Adapters observe external task systems and can do exactly three things:
return items ('ok'), report a missing/unconfigured source ('unavailable'),
or report a failing source ('error'). Environmental problems never raise —
one broken source must not sink a whole run — and nothing here can write.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..canonical import fingerprint_obj
from ..sanitize import sanitize_text

ADAPTER_STATUSES = ("ok", "unavailable", "error")


@dataclass(frozen=True)
class TaskItem:
    item_id: str
    ref: str                      # e.g. "kanban:hermes:T-1001", "github:owner/repo#7"
    title: str
    state: str                    # 'open' | 'closed'
    status_raw: str
    assignee: str | None = None
    updated_at: str | None = None
    url: str | None = None

    def __post_init__(self) -> None:
        if self.state not in ("open", "closed"):
            raise ValueError(f"TaskItem.state must be open|closed, got {self.state!r}")

    def to_dict(self) -> dict[str, Any]:
        # This dict is what gets PERSISTED (snapshots, events, reports).
        # Every externally supplied string crosses the persistence boundary
        # through the fail-closed sanitizer. Normal task ids/refs remain
        # byte-identical; secret/PII-shaped values are withheld or redacted.
        return {
            "item_id": sanitize_text(self.item_id),
            "ref": sanitize_text(self.ref),
            "title": sanitize_text(self.title),
            "state": self.state,
            "status_raw": sanitize_text(self.status_raw),
            "assignee": (sanitize_text(self.assignee) or None
                         if self.assignee is not None else None),
            "updated_at": (sanitize_text(self.updated_at) or None
                           if self.updated_at is not None else None),
            "url": (sanitize_text(self.url) or None
                    if self.url is not None else None),
        }


@dataclass(frozen=True)
class AdapterResult:
    adapter: str
    source_locator: str
    status: str
    detail: str | None = None
    items: tuple[TaskItem, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if self.status not in ADAPTER_STATUSES:
            raise ValueError(f"bad adapter status {self.status!r}")
        if self.status != "ok" and self.items:
            raise ValueError("only 'ok' results may carry items")
        if self.status != "ok" and not self.detail:
            raise ValueError(f"'{self.status}' results require a detail reason")

    @property
    def fingerprint(self) -> str:
        return fingerprint_obj({
            "adapter": self.adapter, "source_locator": self.source_locator,
            "status": self.status, "detail": self.detail,
            "items": [i.to_dict() for i in self.items],
        })

    def items_payload(self) -> list[dict[str, Any]]:
        return [i.to_dict() for i in sorted(self.items, key=lambda i: i.ref)]

    @staticmethod
    def ok(adapter: str, source_locator: str, items: list[TaskItem]) -> "AdapterResult":
        return AdapterResult(adapter=adapter, source_locator=source_locator,
                             status="ok",
                             items=tuple(sorted(items, key=lambda i: i.ref)))

    @staticmethod
    def unavailable(adapter: str, source_locator: str, reason: str) -> "AdapterResult":
        return AdapterResult(adapter=adapter, source_locator=source_locator,
                             status="unavailable", detail=reason)

    @staticmethod
    def error(adapter: str, source_locator: str, reason: str) -> "AdapterResult":
        return AdapterResult(adapter=adapter, source_locator=source_locator,
                             status="error", detail=reason)
