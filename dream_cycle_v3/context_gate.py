"""The no-new-context gate: a Dream Cycle brief repeats only when what it reads changed, and it
counts as delivered only when cron says it was.

New context is what the cycle consumes:

- the conversation, as the gateway's activity fingerprint (the conversation's own rows, minus
  compaction handoffs and the mirrored cron brief; ``gateway/scheduled_compression.py``). A new date
  or cycle id is not new conversation. An answer that does not pin the conversation's state (no
  gateway, a pending or deferred attempt, any failure) is never read as "unchanged". Membership is
  DURABLE, not reconstructed each night: the checkpoint carries one fingerprint per conversation and
  tonight's answer is merged over it, so a conversation the gateway stops reporting on — aged out of
  its activity window, past its retention cap, or its routing entry swept by housekeeping — keeps
  the state it was last known in. Only an answer that really moved moves the digest. The cost is
  that a conversation deleted for good holds its last fingerprint in the checkpoint forever; it
  silences nothing, because a replacement conversation is a new ``session_ref``;
- the canonical records it reads: the operator registry, the thread seed and the tracker snapshots,
  as content digests;
- the collected sources it ingests: the migrated roots' candidates the store holds plus tonight's
  window of them (``runtime.consumed_sources``), by source and content. A file rewritten with the
  same bytes is the same candidate, so bookkeeping churn is not new context; a source a cycle
  ingested but never delivered a brief for still differs from the last delivered state.

Deliberately NOT context: collected roots that are only recorded, never ingested (the sessions root
is the conversation, and also changes with the cycle's own rewrite and metadata stamp), the cycle's
own reports, manifests and store rows, and time-driven carry-forward transitions.

Delivery is acknowledged by cron, never assumed. A brief written to stdout is a pending delivery
(``runtime_delivery_pending``), keyed by the cron execution that ran the script
(``cron.executions.EXECUTION_ID_ENV``). The next run reads that execution's ``delivery_outcome`` in
the executions ledger: ``delivered`` becomes the checkpoint (``runtime_context_checkpoint``) later
runs are judged against; anything else leaves the brief pending, and an unchanged night sends the
same brief again instead of compressing or rebuilding it. A run outside cron has no execution to ask,
so it records nothing and silences nothing.

The gate is a lightweight poll that runs before anything substantive (``decide_poll``): no
manifest, no store write but delivery bookkeeping, no compression, no report. Only its ``cycle``
answer runs the substantive cycle.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping

from .canonical import canonical_json
from .store import ContinuityStore

CHECKPOINT_EVENT_TYPE = "runtime_context_checkpoint"
PENDING_EVENT_TYPE = "runtime_delivery_pending"


@dataclass(frozen=True)
class ContextGate:
    #: ``new`` (deliver), ``unchanged`` (stay silent), or ``unsettled`` (deliver; never checkpoint).
    state: str
    fingerprint: str | None = None
    #: The conversation membership this answer is built on, to be carried into the next checkpoint.
    sessions: Mapping[str, str] | None = None

    @property
    def skip(self) -> bool:
        return self.state == "unchanged"


@dataclass(frozen=True)
class Delivery:
    """One emitted output: pending until cron acknowledges it, then the checkpoint."""
    seq: int
    run_id: str
    cycle_date: str
    fingerprint: str | None
    execution_id: str | None
    text_sha256: str | None
    #: Per-conversation activity fingerprints this output was built from (``{session_ref: digest}``).
    sessions: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class Poll:
    #: ``silent`` (nothing new, last output delivered), ``redeliver`` (nothing new, last output not
    #: delivered), ``wait`` (nothing new, last output still in cron's delivery queue) or ``cycle``.
    action: str
    #: Why, for the status line: ``unchanged``, ``undelivered``, ``in_flight``, ``sources``,
    #: ``conversation``, ``changed`` or ``first``.
    reason: str
    fingerprint: str | None = None
    #: The conversation membership this answer is built on, to be carried into the next checkpoint.
    sessions: Mapping[str, str] | None = None


def canonical_digest(*, registry: Iterable[Mapping[str, Any]] | None,
                     threads: Iterable[Mapping[str, Any]] | None, trackers: Iterable[Any],
                     sources: Iterable[str] = ()) -> str:
    """Content digest of the records one cycle reads: canonical records and the ids of the
    consumed sources. No timestamps of its own: a file rewritten with the same entries, or a
    tracker re-read with the same items, digests the same."""
    body = {
        "registry": list(registry) if registry is not None else None,
        "threads": list(threads) if threads is not None else None,
        "trackers": sorted(([t.adapter, t.source_locator, t.status, t.detail, t.items_payload()]
                            for t in trackers), key=canonical_json),
        "sources": sorted(sources),
    }
    return hashlib.sha256(canonical_json(body).encode("utf-8")).hexdigest()


def _sessions_digest(sessions: Mapping[str, str]) -> str:
    """The conversation state of a MEMBERSHIP: one activity fingerprint per conversation."""
    pairs = sorted([str(ref), str(digest)] for ref, digest in sessions.items())
    return hashlib.sha256(json.dumps(pairs, separators=(",", ":")).encode("utf-8")).hexdigest()


def conversation_state(conversation: Mapping[str, Any],
                       known: Mapping[str, str] | None) -> tuple[str | None, Mapping[str, str]]:
    """The conversation's settled state and the membership it stands for.

    ``off`` when the job does not compress, None when tonight's answer does not pin the state.
    Otherwise the digest of *known* (the membership the last delivered brief was built from) updated
    by the conversations tonight's answer really covers: one the gateway no longer lists is not one
    that changed, so the passage of time, a retention cap and routing housekeeping cannot move the
    digest on their own. Only talk can."""
    if not conversation.get("enabled"):
        return "off", {}
    activity = conversation.get("activity")
    if not isinstance(activity, Mapping) or not activity.get("settled"):
        return None, {}
    tonight = activity.get("sessions")
    merged = {str(k): str(v) for k, v in (known or {}).items()}
    merged.update({str(k): str(v) for k, v in (tonight or {}).items()})
    return _sessions_digest(merged), merged


def _fingerprint(conversation_state: str, canonical: str) -> str:
    return hashlib.sha256(canonical_json(
        {"conversation": conversation_state, "canonical": canonical}).encode("utf-8")).hexdigest()


def decide(conversation: Mapping[str, Any], canonical: str, last: str | None, *,
           undelivered: bool = False, known: Mapping[str, str] | None = None) -> ContextGate:
    """After a cycle ran: whether its output has anything a reader has not already been sent.
    *undelivered*: the cycle rebuilt an output cron never delivered, so it goes out regardless.
    *known*: the conversation membership the last delivered output was built from."""
    state, sessions = conversation_state(conversation, known)
    if state is None:
        return ContextGate("unsettled")
    fingerprint = _fingerprint(state, canonical)
    # A compression that really ran is new by construction: its handoff has not been delivered.
    if conversation.get("claim") != "applied" and not undelivered and fingerprint == last:
        return ContextGate("unchanged", fingerprint, sessions)
    return ContextGate("new", fingerprint, sessions)


def decide_poll(conversation: Mapping[str, Any], canonical: str, *, new_sources: int,
                checkpoint: str | None, pending: Delivery | None, pending_state: str | None,
                known: Mapping[str, str] | None = None) -> Poll:
    """Before any substantive work: run a cycle, stay silent, or resend what never arrived.
    *new_sources* (tonight's sources the store does not hold yet) only names why a cycle runs.
    *known*: the conversation membership the last delivered output was built from."""
    unresolved = pending if (pending is not None and pending_state != "delivered") else None
    # An output still owed to a reader is compared against the membership IT was built from, never
    # against the last DELIVERED checkpoint's: the question there is "is this still the right brief",
    # and a conversation that has since dropped out of the gateway's answer must not make tonight's
    # state differ from the pending brief's. Being the baseline is not being acknowledged — the
    # pending row stays pending, and only cron's ledger can resolve it.
    state, sessions = conversation_state(conversation,
                                         unresolved.sessions if unresolved is not None else known)
    if state is None:
        return Poll("cycle", "conversation")
    fingerprint = _fingerprint(state, canonical)
    changed = "sources" if new_sources else "changed"
    if unresolved is not None:
        if pending.fingerprint != fingerprint:
            return Poll("cycle", changed, fingerprint, sessions)
        if pending_state == "in_flight":
            return Poll("wait", "in_flight", fingerprint, sessions)
        return Poll("redeliver", "undelivered", fingerprint, sessions)
    if fingerprint == checkpoint:
        return Poll("silent", "unchanged", fingerprint, sessions)
    return Poll("cycle", "first" if checkpoint is None else changed, fingerprint, sessions)


def _latest(store: ContinuityStore, event_type: str, *, profile: str, mode: str,
            output: str) -> Delivery | None:
    rows = store._conn.execute(
        "SELECT e.event_seq AS seq, e.run_id AS run_id, e.payload AS payload FROM events e "
        "JOIN runs r ON e.run_id = r.run_id WHERE e.event_type = ? AND r.profile = ? "
        "ORDER BY e.event_seq DESC", (event_type, profile)).fetchall()
    for row in rows:
        payload = json.loads(row["payload"])
        if (payload.get("profile"), payload.get("mode"), payload.get("output")) == (profile, mode,
                                                                                    output):
            raw = payload.get("conversation_sessions")
            return Delivery(seq=row["seq"], run_id=row["run_id"],
                            cycle_date=payload.get("cycle_date") or "",
                            fingerprint=payload.get("context_fingerprint"),
                            execution_id=payload.get("execution_id"),
                            text_sha256=payload.get("text_sha256"),
                            sessions={str(k): str(v) for k, v in raw.items()}
                            if isinstance(raw, Mapping) else {})
    return None


def last_checkpoint(store: ContinuityStore, *, profile: str, mode: str, output: str) -> str | None:
    """The context fingerprint of the last acknowledged delivery for this profile, mode and output.
    Kept per output: a status line that went out is not the brief going out."""
    checkpoint = _latest(store, CHECKPOINT_EVENT_TYPE, profile=profile, mode=mode, output=output)
    return checkpoint.fingerprint if checkpoint else None


def checkpoint_sessions(store: ContinuityStore, *, profile: str, mode: str,
                        output: str) -> Mapping[str, str]:
    """The conversation membership the last acknowledged delivery was built from.

    Empty before the first acknowledged delivery, which is the honest answer: there is nothing to
    carry forward and the first night is ``first`` regardless."""
    checkpoint = _latest(store, CHECKPOINT_EVENT_TYPE, profile=profile, mode=mode, output=output)
    return checkpoint.sessions if checkpoint else {}


def baseline_sessions(store: ContinuityStore, pending: Delivery | None, *, profile: str, mode: str,
                      output: str) -> Mapping[str, str]:
    """The membership the most recent output a reader is owed, or has been sent, was built from.

    An unresolved *pending* output supersedes the checkpoint as the baseline WITHOUT being treated as
    delivered: it is the newest statement of what is known about each conversation (it was itself
    built by merging over the checkpoint), and a rebuild that fell back to the older checkpoint would
    drop a conversation the pending brief covered."""
    if pending is not None:
        return pending.sessions
    return checkpoint_sessions(store, profile=profile, mode=mode, output=output)


def unresolved_pending(store: ContinuityStore, *, profile: str, mode: str,
                       output: str) -> Delivery | None:
    """The last emitted output, when cron has not yet been seen to deliver it."""
    pending = _latest(store, PENDING_EVENT_TYPE, profile=profile, mode=mode, output=output)
    checkpoint = _latest(store, CHECKPOINT_EVENT_TYPE, profile=profile, mode=mode, output=output)
    if pending is None or (checkpoint is not None and checkpoint.seq > pending.seq):
        return None
    return pending


def delivery_state(execution_id: str | None) -> str:
    """What cron recorded for the run that emitted an output: ``delivered``, ``in_flight`` (its
    send is still queued for a gateway) or ``undelivered`` (failed, suppressed, not configured,
    unknown, or no such run). Only a run that completed delivers its output: a failed one (a
    timeout, a non-zero exit) delivers cron's failure notice, which is not the brief."""
    if not execution_id:
        return "undelivered"
    from cron import delivery_queue, executions

    row = executions.get_execution(execution_id)
    if row is None:
        return "undelivered"
    if row.get("status") in ("claimed", "running"):
        return "in_flight"
    if row.get("status") != "completed":
        return "undelivered"
    outcome = row.get("delivery_outcome")
    if outcome == "queued":
        queued = delivery_queue.get_status(execution_id)
        status = queued.get("status") if queued else None
        if status == "delivered":
            return "delivered"
        return "in_flight" if status in ("pending", "delivering") else "undelivered"
    return "delivered" if outcome == "delivered" else "undelivered"


def record_pending(store: ContinuityStore, *, run_id: str, profile: str, mode: str, output: str,
                   cycle_date: str, fingerprint: str | None, execution_id: str, text_sha256: str,
                   sessions: Mapping[str, str] | None = None) -> None:
    emitted_at = datetime.now(timezone.utc).isoformat()
    with store.transaction() as conn:
        store._emit_event(conn, entity_type="run", entity_id=run_id,
                          event_type=PENDING_EVENT_TYPE,
                          payload={"profile": profile, "mode": mode, "output": output,
                                   "cycle_date": cycle_date, "context_fingerprint": fingerprint,
                                   "execution_id": execution_id, "text_sha256": text_sha256,
                                   "conversation_sessions": dict(sessions or {}),
                                   "emitted_at": emitted_at},
                          run_id=run_id, now=emitted_at)


def record_checkpoint(store: ContinuityStore, pending: Delivery, *, profile: str, mode: str,
                      output: str) -> None:
    """*pending* was delivered: from now on it is what an unchanged night is compared against."""
    acknowledged_at = datetime.now(timezone.utc).isoformat()
    with store.transaction() as conn:
        store._emit_event(conn, entity_type="run", entity_id=pending.run_id,
                          event_type=CHECKPOINT_EVENT_TYPE,
                          payload={"profile": profile, "mode": mode, "output": output,
                                   "cycle_date": pending.cycle_date,
                                   "context_fingerprint": pending.fingerprint,
                                   "execution_id": pending.execution_id,
                                   # The membership that output stood for becomes the baseline the
                                   # next night merges its answer over.
                                   "conversation_sessions": dict(pending.sessions or {}),
                                   "acknowledged_at": acknowledged_at},
                          run_id=pending.run_id, now=acknowledged_at)
