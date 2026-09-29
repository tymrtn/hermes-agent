"""Daily carry-forward: exactly one disposition per selected nonterminal thread.

Deterministic policy, evaluated in fixed order per thread (design §7):

1. already dispositioned today            -> skip (idempotent rerun)
2. external task proves completion        -> close_done + closure proof
3. blocked/waiting, follow-up elapsed     -> continue (revisit, state -> active)
   blocked/waiting, follow-up in future   -> blocked/waiting (defer in place)
4. stale beyond policy                    -> stale_review (state -> stale)
5. already stale                          -> stale_review (pending review)
6. needs external task link               -> needs_link
7. queued behind approval-required SSOT   -> authority_gated (options preserved)
8. otherwise                              -> continue

States never advance silently; only the transitions above change state.
The whole run executes inside one store transaction: it fails loud
(CarryForwardInvariantError) unless every selected thread ends the date with
exactly one disposition row, and any failure rolls back every disposition,
transition, and event of the run.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date as date_type
from typing import Any

from .contracts import is_iso_date, is_iso_datetime, parse_iso_datetime
from .errors import CarryForwardInvariantError, ContractViolation
from .store import ContinuityStore


@dataclass(frozen=True)
class CarryForwardPolicy:
    stale_after_days: int = 14

    def __post_init__(self) -> None:
        if self.stale_after_days <= 0:
            raise ValueError("stale_after_days must be positive")


@dataclass
class CarryForwardReport:
    run_id: str
    disposition_date: str
    selected: int = 0
    dispositioned: int = 0
    already_dispositioned: int = 0
    closed_with_proof: int = 0
    actions: dict[str, int] = field(default_factory=dict)
    threads: list[dict[str, Any]] = field(default_factory=list)
    invariant_ok: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "disposition_date": self.disposition_date,
            "selected": self.selected,
            "dispositioned": self.dispositioned,
            "already_dispositioned": self.already_dispositioned,
            "closed_with_proof": self.closed_with_proof,
            "actions": dict(sorted(self.actions.items())),
            "threads": self.threads,
            "invariant_ok": self.invariant_ok,
        }


def external_completion_map(store: ContinuityStore, run_id: str) -> dict[str, dict[str, Any]]:
    """Map external ref -> item for every 'ok' adapter snapshot of this run."""
    refs: dict[str, dict[str, Any]] = {}
    for row in store.adapter_snapshots_for_run(run_id):
        if row["status"] != "ok":
            continue
        for item in json.loads(row["items"]):
            refs[item["ref"]] = item
    return refs


def _age_days(last_disposition_date: str, today: str) -> int:
    return (date_type.fromisoformat(today)
            - date_type.fromisoformat(last_disposition_date)).days


def _decide(thread: Any, *, today: str, now: str,
            external: dict[str, dict[str, Any]],
            policy: CarryForwardPolicy,
            write_policy_by_project: dict[str, str]) -> dict[str, Any]:
    state = thread["state"]
    ref = thread["external_task_ref"]
    ext = external.get(ref) if ref else None

    if ext is not None and ext.get("state") == "closed":
        return {
            "action": "close_done",
            "reason": f"external task {ref} reports completion "
                      f"(status: {ext.get('status_raw', 'closed')})",
            "state_after": "done",
            "closure_proof": {"kind": "task_event", "reference": ref,
                              "verified_at": now},
        }

    if state in ("blocked", "waiting"):
        follow_up = thread["follow_up_after"] or ""
        if not is_iso_datetime(follow_up):
            raise ContractViolation(
                "carry_forward",
                [f"thread {thread['thread_id']} carries invalid "
                 f"follow_up_after '{follow_up}'"])
        try:
            # Semantic comparison, not lexical: parse the stored datetime and
            # compare calendar dates. Corrupt legacy data fails loud here.
            follow_up_date = parse_iso_datetime(follow_up).date()
        except ValueError:
            raise ContractViolation(
                "carry_forward",
                [f"thread {thread['thread_id']} carries invalid "
                 f"follow_up_after '{follow_up}'"]) from None
        if follow_up_date <= date_type.fromisoformat(today):
            return {
                "action": "continue",
                "reason": f"follow-up window elapsed ({follow_up}); "
                          f"revisiting blocker: {thread['blocked_by']}",
                "state_after": "active",
            }
        return {
            "action": state,
            "reason": f"still {state} by {thread['blocked_by']}; "
                      f"follow up after {follow_up}",
            "state_after": state,
            "blocker": thread["blocked_by"],
            "follow_up_after": follow_up,
        }

    if state == "stale":
        return {
            "action": "stale_review",
            "reason": "stale thread pending review",
            "state_after": "stale",
        }

    if _age_days(thread["last_disposition_date"], today) > policy.stale_after_days:
        return {
            "action": "stale_review",
            "reason": f"no disposition since {thread['last_disposition_date']} "
                      f"(> {policy.stale_after_days} days); routed to review",
            "state_after": "stale",
        }

    if thread["link_disposition"] == "needs_link":
        return {
            "action": "needs_link",
            "reason": "no external task reference; link to task SSOT required",
            "state_after": state,
        }

    if state == "queued" and \
            write_policy_by_project.get(thread["project_id"]) == "approval_required":
        return {
            "action": "authority_gated",
            "reason": "queued behind approval-required task SSOT; options: "
                      f"approve and queue, or dismiss. next action: "
                      f"{thread['normalized_next_action']}",
            "state_after": "queued",
        }

    return {
        "action": "continue",
        "reason": "open thread carried forward; no closure evidence, "
                  "no blocker, within freshness policy",
        "state_after": state,
    }


def run_carry_forward(store: ContinuityStore, *, run_id: str, disposition_date: str,
                      now: str, policy: CarryForwardPolicy | None = None,
                      project_ids: list[str] | None = None) -> CarryForwardReport:
    policy = policy or CarryForwardPolicy()
    # Semantic gates before any thread is selected or any row written.
    if not is_iso_date(disposition_date):
        raise ContractViolation(
            "carry_forward",
            [f"disposition_date '{disposition_date}' is not a valid calendar date"])
    if not is_iso_datetime(now):
        raise ContractViolation(
            "carry_forward", [f"now '{now}' is not a valid ISO-8601 datetime"])

    report = CarryForwardReport(run_id=run_id, disposition_date=disposition_date)

    # One atomic transaction for the whole daily run: every disposition,
    # thread transition, and event commits together or not at all.
    with store.transaction():
        external = external_completion_map(store, run_id)
        threads = store.select_nonterminal_threads(project_ids)
        write_policies = {
            t["project_id"]: (store.get_project(t["project_id"])
                              or {"task_write_policy": "read_only"})["task_write_policy"]
            for t in threads
        }
        report.selected = len(threads)
        selected_ids: list[str] = []

        for thread in threads:
            thread_id = thread["thread_id"]
            selected_ids.append(thread_id)
            if store.get_disposition(thread_id, disposition_date) is not None:
                report.already_dispositioned += 1
                continue
            decision = _decide(thread, today=disposition_date, now=now,
                               external=external, policy=policy,
                               write_policy_by_project=write_policies)
            store.record_disposition(
                thread_id=thread_id,
                disposition_date=disposition_date,
                run_id=run_id,
                action=decision["action"],
                reason=decision["reason"],
                state_after=decision["state_after"],
                now=now,
                blocker=decision.get("blocker"),
                follow_up_after=decision.get("follow_up_after"),
                closure_proof=decision.get("closure_proof"),
            )
            report.dispositioned += 1
            report.actions[decision["action"]] = \
                report.actions.get(decision["action"], 0) + 1
            if decision["action"] == "close_done":
                report.closed_with_proof += 1
            report.threads.append({
                "thread_id": thread_id,
                "action": decision["action"],
                "state_before": thread["state"],
                "state_after": decision["state_after"],
            })

        # Hard invariant (design §15), checked before COMMIT: exactly one
        # disposition per selected thread, else the whole run rolls back.
        failures: list[str] = []
        for thread_id in selected_ids:
            rows = store._conn.execute(
                "SELECT COUNT(*) AS c FROM thread_dispositions WHERE thread_id = ? "
                "AND disposition_date = ?", (thread_id, disposition_date)).fetchone()
            if rows["c"] != 1:
                failures.append(f"{thread_id}: {rows['c']} dispositions")
        if failures:
            raise CarryForwardInvariantError(
                f"disposition invariant violated for {disposition_date}: "
                + "; ".join(failures))
    report.invariant_ok = True
    return report
