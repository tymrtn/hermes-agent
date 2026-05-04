---
name: todoist-live-triage
description: "Live triage pattern for existing Todoist tasks: read comments and verify reality before surfacing blockers, rescheduling, or closing."
tags: [todoist, triage, comments, verification]
triggers: ["todoist triage", "hourly todoist loop", "review overdue todoist tasks", "action engine on todoist"]
---

# Todoist live triage

Use this when working an existing Todoist queue, especially in cron loops or action-engine reviews.

## Why

Task titles, descriptions, and even earlier system notes drift out of date. The freshest state is often in recent Todoist comments plus a live check in the real system.

Do not surface a blocker, ask Tyler for a decision, or keep parroting an old dependency until you check the current task thread and verify reality.

## Core pattern

For each candidate task:

```bash
todo raw GET /tasks/<TASK_ID>
todo raw GET '/comments?task_id=<TASK_ID>'
```

If Tyler gives new task details in chat, **search Todoist for an existing parent before creating anything**. Prefer updating/commenting the parent task over creating separate tiny tasks. Avoid “minutia explosions”: marbete/title/photo/price/document gates for one sale belong as concise comments or subtasks under the existing sale task, not as multiple loose tasks in another project.

If you accidentally create duplicate small tasks, repair it immediately:
1. find the existing parent task and read its comments;
2. add one concise comment summarizing the new gates/details;
3. delete the duplicate loose tasks;
4. report the parent task as the durable record.

If the Todoist task is a development story, bug, QA gate, deploy gate, or code review item, treat any linked GitHub Issue/PR as the canonical engineering record. Todoist should carry only the human-facing action/decision, due date, priority, and link.

Then classify:
1. **Fresh comments already resolve the blocker** → act on the new state, not the stale card text.
2. **Live system check can resolve it now** → do the check, comment the result, then close/reschedule/update.
3. **Still blocked on Tyler** → only then surface it.
4. **Blocked on third party or dependency** → reschedule and comment the reason.
5. **Stale/irrelevant** → propose delete or rescope.

## High-value habit

When the task is about infrastructure, bots, or mail, pair Todoist comments with a live verification command.

Examples:

### Hermes bot health
```bash
~/.hermes/hermes-agent/venv/bin/hermes -p <profile> status
HERMES_HOME="$HOME/.hermes/profiles/<profile>" ~/.hermes/hermes-agent/venv/bin/hermes chat -q 'Reply with exactly OK' -Q --max-turns 1
```

If both are healthy enough for the actual task, comment that evidence and close the card.

### Envelope / mail task
Do not trust an older "still blocked" note if inbox access now works. Run a real mailbox command and update the task thread from that result.

## Deadline / sprint cleanup pattern

When Tyler asks to "come back with our plan" or says weekend/deadline tasks need readjustment:
1. Check live time first (`date`) so relative Todoist due strings are interpreted correctly.
2. Pull both `todo overview` and a focused list such as `todo list --filter '@weekend-sprint | overdue | today | tomorrow'`.
3. Close clearly completed tasks before rescheduling anything. Example: submitted applications should be closed, not moved forward.
4. Reschedule only the small, clearly relevant sprint set. Do **not** bulk-edit an entire stale overdue graveyard unless Tyler explicitly asks; that usually creates fake order and hides old project drift.
5. Report the resulting plan as a short execution sequence with times, and separately call out any larger stale backlog that still needs a consolidation pass.

## Reporting rule

If you report a Todoist task upward, report the **current verified state**, not the historical wording of the card.

## Example outcome shapes

- **Close**: comment with the verification evidence, then `todo done <TASK_ID>`
- **Reschedule**: comment with the specific blocked dependency, then `todo update <TASK_ID> --due 'tomorrow 3pm'`
- **Escalate**: include only the decision Tyler actually needs now

## Pitfall

A task can contain stale blocker text even when the underlying system is already fixed. Comments are cheap. False escalations are expensive. Check first, monkey.
