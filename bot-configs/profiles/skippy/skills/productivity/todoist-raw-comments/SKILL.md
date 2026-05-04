---
name: todoist-raw-comments
description: Add and read Todoist task comments via todo.py raw REST calls when the main CLI lacks first-class comment commands.
---

# Todoist raw comments

Use this when you need to leave progress notes on an existing Todoist task from Hermes.

## Why
`todo.py` exposes task/project CRUD directly, but may not expose comment commands as first-class subcommands. The REST escape hatch works reliably.

## Commands

Add a comment to a task:

```bash
todo raw POST /comments --body '{"task_id":"<TASK_ID>","content":"your note here"}'
```

List comments for a task:

```bash
todo raw GET /comments?task_id=<TASK_ID>
```

Get a specific task first if you need to confirm state:

```bash
todo raw GET /tasks/<TASK_ID>
```

## Usage notes
- Keep comment text plain and concise.
- Prefer timestamped progress notes when running autonomous loops.
- Do **not** use Todoist comments as the long-form engineering ledger for development work. For dev stories, bugs, QA, PRs, deploy gates, and bot handoffs, write durable evidence to the linked GitHub Issue/PR and leave only a concise Todoist pointer/action.
- Use Todoist comments to record human-action state: why Tyler is blocked, what decision is needed, what follow-up is due, or which GitHub issue contains the evidence.
- For user-facing reports, link tasks as `https://app.todoist.com/app/task/<TASK_ID>` instead of leading with raw IDs.

## Example

```bash
todo raw POST /comments --body '{"task_id":"6gR3jFxgfr96XWr5","content":"Skippy audit 2026-04-23 17:12 CEST: signup page returns HTTP 200 with 0-byte body; primary CTA is broken."}'
```

## Pitfall
Do not assume a task with `note_count: 0` lacks comment support. You can still create comments through the raw `/comments` endpoint.
