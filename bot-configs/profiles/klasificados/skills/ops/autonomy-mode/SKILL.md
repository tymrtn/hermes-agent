---
name: autonomy-mode
description: Build recurring cron workflows that preserve context across fresh sessions by reading and rewriting a shared state document each run.
version: 1.0.0
author: Hermes Agent
---

# Autonomy Mode

Use this when a user wants scheduled work to behave like a continuous operating loop instead of isolated cron prompts.

## When to use

Use this skill when:
- a cron runs repeatedly and each run needs context from the previous run
- the user wants one core loop rather than time-of-day themed jobs
- cron sessions start fresh and cannot rely on chat history
- the process should leave a durable handoff for the next run

## Core idea

Do not rely on modifying the next cron job each time.

Prefer a stable shared state surface:
1. a protocol file that defines the loop
2. a state file that each run reads first and overwrites at the end
3. one recurring cron prompt that explicitly uses both files

This is more robust than chaining prompt edits because:
- cron runs are stateless
- the contract is inspectable by humans
- the loop can be audited and repaired manually
- multiple Hermes agents can share the same handoff format

## Recommended structure

Create two files in the target project or ops directory:
- `ops/core-loop-protocol.md`
- `ops/core-loop-state.md`

Protocol file contents should define:
- purpose of the loop
- cadence
- required read order
- required output sections
- rules and safety limits

State file contents should be concise and rolling, not archival.

Recommended sections:
- Last updated
- Current assessment
- What changed this loop
- Active priorities for next loop
- Blockers / needs input
- Deferred / watchlist

Add domain-specific sections as needed, for example:
- Inbox queue
- Drafts pending approval
- Metrics / health signals
- Open incidents
- Active experiments

## Cron prompt pattern

The recurring cron prompt should explicitly instruct the run to:
1. read the project guidance
2. read the protocol file
3. read the shared state file
4. perform the highest-value checks/actions for this loop
5. create or refine concrete backlog items when gaps, bugs, or growth opportunities are found
6. complete at least one meaningful implementation, fix, or delegated build step unless blocked by approvals, missing credentials, or a live incident
7. overwrite the shared state file with the new handoff
8. deliver a concise summary focused on deltas, work completed, and next priorities

Key language to include:
- "This cron runs in a fresh session with no chat context"
- "Run the core loop, not a one-off routine"
- "Focus on deltas since the last loop"
- "Overwrite the shared state file at the end of every run"

## Default implementation pattern

1. Inspect existing cron jobs.
2. Identify disconnected or redundant schedules.
3. Create protocol and state files.
4. Create one recurring cron at the desired cadence.
5. Remove old time-sliced jobs if they are replaced by the loop.
6. Confirm the final active cron list.

## Practical rules

- Keep one core loop unless there is a strong reason for separate loops. For Klasificados specifically, Tyler expects the full multi-slot autonomous team cadence (plan, grooming, definition, 6 dev/integration slots, checkpoint, postmortem), not one collapsed every-2-hours loop.
- The state file should contain live handoff context, not an ever-growing log.
- Rewrite stale sections aggressively.
- Prefer short, operationally useful summaries over narrative history.
- If email or outbound communication is involved, preserve approval rules in both the cron prompt and the protocol file.
- If the loop depends on files in a repo, use tilde-shorthand paths (`~/path/to/repo`), not hardcoded `/Users/<name>/...`. Cron prompts outlive the machine they were authored on; a username change (e.g., moving between `tylermartin` and `wondermonkey`) silently breaks every `read_file` and `find` in the run.
- Put the state file (`ops/core-loop-state.md`) under version control. If `ops/` is untracked (common when the loop was added ad hoc), state transitions have no history — you cannot diff what actually changed between runs, and any Dropbox/sync conflict silently overwrites the handoff. `git add ops/` early.

## Pitfalls

Avoid these patterns:
- separate morning/afternoon/evening prompts that drift apart
- relying on chat memory between cron runs
- appending endlessly to the state file until it becomes unusable
- storing secrets in the state file
- vague prompts that do not force the run to rewrite the handoff
- hardcoding machine-specific absolute paths (`/Users/<name>/...`) in the cron prompt — use `~/` so prompts survive host/user changes
- leaving `ops/` untracked in git — state drift becomes invisible and unrecoverable

## Diagnosing a broken loop

### Path failure symptom

If a cron run reports "I cannot execute this cron" or "bootstrap files missing," the most likely cause is username drift in hardcoded paths. Symptoms:
- run returns near-instantly with a refusal message
- the run mentions `find ~ returned no results` or `does not exist on this host`
- `last_status` may still be `ok` because the session completed (just with no work)

Fix: edit `~/.hermes/profiles/<profile>/cron/jobs.json` directly, replace every `/Users/<old-user>/` with `~/` or repo-relative paths, save. Next scheduled run picks up the change automatically.

### Provider / quota failure symptom

Not every cron `error` is a bad prompt. Before changing prompts, inspect the request dump files:

- `~/.hermes/profiles/<profile>/sessions/request_dump_cron_<job_id>_*.json`

These often contain the real provider error (`out of extra usage`, auth failure, 429, etc.) even when the cron list view only shows `last_status: error`.

If the dump shows billing/auth/provider exhaustion:
- do **not** rewrite the prompt first
- verify the model/provider separately with a tiny direct CLI call
- only change the cron prompt after ruling out provider problems

### Rollout rule for a new multi-slot loop

If you design a full-day schedule (plan, grooming, definition, dev slots, postmortem), do **not** necessarily activate every slot on day one.

Safer pattern:
1. document the full target schedule in the protocol
2. activate only the minimum proving set first (often `06:00` plan and `21:00` postmortem)
3. confirm state rewrite + digest generation + delivery actually work
4. expand to more autonomous slots later

Document both clearly so the protocol distinguishes between:
- **target operating cadence**
- **currently active cron set**

### Existing agent-team audit before scaffolding

Before creating persona/subagent files for a cron-driven team, inspect the repo for an existing `.claude/agents/` set. If one already exists, reuse and extend it rather than creating a second parallel team layer.

Good pattern:
- `team/*.md` = human-readable charters
- `.claude/agents/*.md` = executable subagent prompts

Only add the genuinely missing role(s).

### Digest/file-output gotcha

If a helper script already writes its own output file, do **not** also redirect stdout into that same file from cron. You will overwrite the real structured artifact with a log summary.

Prefer:
- script writes artifact itself
- stdout remains a short summary for cron logs

## Minimal example

Protocol file:
- defines the loop purpose, cadence, read order, and required end-of-run update

State file:
- holds current assessment, changes, next priorities, blockers

Cron:
- runs every 2 hours
- reads both files
- does the work
- overwrites the state file
- reports only material changes

## Verification

After setup:
- list cron jobs and confirm only the intended loop remains active
- inspect the created protocol and state files
- verify the cron prompt explicitly references both files
- after the first run, confirm the state file was rewritten rather than ignored

## Inspecting a cron's full prompt

`cronjob list` only returns a truncated `prompt_preview`. To read the full prompt, read the profile's jobs file directly:

```
~/.hermes/profiles/<profile>/cron/jobs.json
```

Parse with Python/jq and pull `jobs[*].prompt` by `id`. Useful when auditing what a paused or long-running loop actually instructs, or when debugging why a run did/didn't do something.

## Verifying claims about past runs

Before asserting "the loop ran overnight" or "X happened at N am":
1. `cronjob list` → check `state`, `last_run_at`, `last_status`, `paused_at`
2. If `state == paused`, nothing ran. Say so plainly.
3. For evidence of actual work, inspect the state file's `Last updated` and `What changed this loop`, and/or list cron session files in `~/.hermes/profiles/<profile>/sessions/session_cron_<job_id>_*.json`.

Never claim a loop did work without checking. Paused jobs are a frequent failure mode — a resume step can be missed for days.

## Good outcome

A good autonomy mode loop behaves like an operating system for the task:
- every run starts with inherited context
- every run leaves cleaner context for the next one
- the human can inspect the handoff file at any time
- the loop continues to improve without depending on hidden session state
