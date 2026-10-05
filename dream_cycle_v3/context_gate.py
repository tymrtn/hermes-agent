"""The no-new-context gate: a delivered Dream Cycle brief repeats only when what it reads changed.

New context is exactly what the brief is built from:

- the conversation, as the gateway's activity fingerprint (the conversation's own rows, minus
  compaction handoffs and the mirrored cron brief; ``gateway/scheduled_compression.py``). A new date
  or cycle id is not new conversation. An answer that does not pin the conversation's state (no
  gateway, a pending or deferred attempt, any failure) is never read as "unchanged";
- the canonical records it reads: the operator registry, the thread seed and the tracker snapshots,
  as content digests.

Deliberately NOT context: collected read-root sources (windowed, ingested every night by the
deterministic store pass, never rendered in the brief; the sessions root also changes with the
cycle's own rewrite and metadata stamp), the cycle's own reports, manifests and store rows, and
time-driven carry-forward transitions.

The checkpoint is a ``runtime_context_checkpoint`` event, recorded under the runtime lock only after
the brief was written out, and only when the conversation's state was settled. A failed, deferred or
undelivered run leaves the last checkpoint where it was, so the next run is judged against the last
brief that actually went out.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping

from .canonical import canonical_json
from .store import ContinuityStore

CHECKPOINT_EVENT_TYPE = "runtime_context_checkpoint"


@dataclass(frozen=True)
class ContextGate:
    #: ``new`` (deliver), ``unchanged`` (stay silent), or ``unsettled`` (deliver; never checkpoint).
    state: str
    fingerprint: str | None = None

    @property
    def skip(self) -> bool:
        return self.state == "unchanged"

    @property
    def settled(self) -> bool:
        return self.fingerprint is not None


def canonical_digest(*, registry: Iterable[Mapping[str, Any]] | None,
                     threads: Iterable[Mapping[str, Any]] | None, trackers: Iterable[Any]) -> str:
    """Content digest of the canonical records one cycle read. No timestamps of its own: a file
    rewritten with the same entries, or a tracker re-read with the same items, digests the same."""
    body = {
        "registry": list(registry) if registry is not None else None,
        "threads": list(threads) if threads is not None else None,
        "trackers": sorted(([t.adapter, t.source_locator, t.status, t.detail, t.items_payload()]
                            for t in trackers), key=canonical_json),
    }
    return hashlib.sha256(canonical_json(body).encode("utf-8")).hexdigest()


def decide(conversation: Mapping[str, Any], canonical: str, last: str | None) -> ContextGate:
    """Whether this cycle has anything a reader has not already been sent."""
    if conversation.get("enabled"):
        activity = conversation.get("activity")
        if not isinstance(activity, Mapping) or not activity.get("settled"):
            return ContextGate("unsettled")
        conversation_state = activity.get("fingerprint")
    else:
        conversation_state = "off"
    fingerprint = hashlib.sha256(canonical_json(
        {"conversation": conversation_state, "canonical": canonical}).encode("utf-8")).hexdigest()
    # A compression that really ran is new by construction: its handoff has not been delivered.
    if conversation.get("claim") != "applied" and fingerprint == last:
        return ContextGate("unchanged", fingerprint)
    return ContextGate("new", fingerprint)


def last_checkpoint(store: ContinuityStore, *, profile: str, mode: str, output: str) -> str | None:
    """The context fingerprint of the last delivery for this profile, mode and output. Kept per
    output: a status line that went out is not the brief going out."""
    rows = store._conn.execute(
        "SELECT e.payload AS payload FROM events e JOIN runs r ON e.run_id = r.run_id "
        "WHERE e.event_type = ? AND r.profile = ? ORDER BY e.event_seq DESC",
        (CHECKPOINT_EVENT_TYPE, profile)).fetchall()
    for row in rows:
        payload = json.loads(row["payload"])
        if (payload.get("profile"), payload.get("mode"), payload.get("output")) == (profile, mode,
                                                                                    output):
            return payload.get("context_fingerprint")
    return None


def record_checkpoint(store: ContinuityStore, *, run_id: str, profile: str, mode: str, output: str,
                      cycle_date: str, fingerprint: str) -> None:
    # Wall clock in the payload: every delivery is its own record, and the newest one wins.
    delivered_at = datetime.now(timezone.utc).isoformat()
    with store.transaction() as conn:
        store._emit_event(conn, entity_type="run", entity_id=run_id,
                          event_type=CHECKPOINT_EVENT_TYPE,
                          payload={"profile": profile, "mode": mode, "output": output,
                                   "cycle_date": cycle_date,
                                   "context_fingerprint": fingerprint,
                                   "delivered_at": delivered_at},
                          run_id=run_id, now=delivered_at)
