---
name: claude-code-background-delegation
description: Delegate a Claude Code print-mode task that will take more than 2 minutes without blocking the Hermes conversation. Uses background terminal + cron polling so the agent can keep working and self-report when done.
---

# Claude Code Background Delegation (Hermes)

## When to use

- Multi-file or multi-hour coding tasks (per the `cc-delegation` protocol)
- Any Claude Code print-mode run expected to exceed ~2 minutes
- Situations where you want to keep talking to the user while CC works
- Tasks that need verification (curl a deploy, check git log) and GitHub Issue/PR hygiene after completion; Todoist only for Tyler-facing follow-up/approval tasks

Do NOT use for quick single-file edits — just do those inline.

## Why not just `terminal()` foreground?

Hermes foreground `terminal` calls block the conversation and can time out. `process(action='wait')` is capped (commonly 60s), so you can't reliably wait on a 10-minute CC run. You need a background process + an out-of-band poller.

## The pattern

### Step 1 — Write task spec to a file

Interpolating a large prompt inline is fragile (quoting, newlines, special chars).

```
write_file(path="/tmp/cc-task.md", content="<full task spec>")
```

Spec should include: objective, files to edit, verification steps, done criteria.

### Step 2 — Verify you're on the expected plan

Run `claude auth status` and confirm the account + subscription match expectations. If a stray API key env var is set in the shell profile, it can silently override the OAuth session. Unset it if you want to stay on plan.

### Step 3 — Spawn CC in background

```
terminal(
  background=True,
  command='cd /abs/path/to/repo && claude -p "$(cat /tmp/cc-task.md)" \\
    --permission-mode bypassPermissions \\
    --max-turns 50 \\
    --output-format json \\
    > /tmp/cc-out.json 2> /tmp/cc-out.err'
)
```

Returns a `session_id` like `proc_d3f1a58e1adb`. Save this — the cron needs it.

Flags:
- `--permission-mode bypassPermissions` — required for unattended runs
- `--max-turns 50` — hard cap; prevents runaway loops
- `--output-format json` — parseable result
- Redirect stdout + stderr separately so you can diagnose failures

### Step 4 — Schedule a poll cron

```
cronjob(
  action='create',
  schedule='*/3 * * * *',
  repeat=6,
  prompt='''
Poll Claude Code delegation.
Process: proc_d3f1a58e1adb
Log: /tmp/cc-out.json (stdout), /tmp/cc-out.err (stderr)

1. process(action=poll, thread_id=proc_d3f1a58e1adb)
2. If still running AND uptime < 1500s: report uptime, exit.
3. If still running AND uptime >= 1500s: kill it, report timeout, cancel this cron.
4. If exited:
   a. read_file /tmp/cc-out.json — extract result, num_turns, exit_code
   b. Run verification (curl endpoint, git log --oneline -5, whatever applies)
   c. Update the related GitHub Issue/PR with commit hash, verification evidence, artifacts, and remaining blocker/closeout
   d. Close or retitle only the related Tyler-facing Todoist action, if one exists
   e. Report to user: issue/PR link, commit hash, verification output, what was done
   f. Cancel this cron: cronjob(action=delete, job_id=<this_job_id>)
'''
)
```

Every 3 minutes × 6 reps = 18 min total budget. Adjust if the task is bigger.

### Step 5 — Return control to user immediately

After spawning + scheduling, reply to the user: "Delegated to Claude Code, background process `proc_xxx`. Will report back in a few minutes." Don't sit and poll.

## Reporting cost correctly

CC's JSON output includes `total_cost_usd`. **This is the API-equivalent list price, not what you pay on Max/Pro subscription.** On subscription, actual cost is $0.

When reporting back to the user:
- ✅ "Finished in 3m42s. $0 on Max / ~$1.20 API-equivalent."
- ✅ "Finished in 3m42s, 47 turns." (omit cost entirely)
- ❌ "Finished in 3m42s. Cost: $1.20." (wrong — implies real spend)

## Pitfalls

### Harness workspace gremlin
On some boxes (when Hermes was configured with a different user's home path), `background=true` fails first try with `FileNotFoundError` on a `/Users/<someone>/.hermes/profiles/<agent>/workspace` path. The harness auto-creates the path on retry. Not fatal, but noisy. If persistent, `mkdir -p` that path once.

### Don't forget the tracking ledger split
Tyler treats stale tracking as a trust cost. For dev work, the cron MUST update the linked GitHub Issue/PR with evidence and close/retitle any Tyler-facing Todoist action only when that human action is actually done.

### Don't re-poll when the cron already exists
If you spawn a delegation and immediately start `process(action=poll, ...)` yourself in the conversation, you're racing the cron and wasting turns. Spawn, schedule, step away.

### Verify the deploy, not just the build
"CC exited clean" ≠ "feature works." Curl the live endpoint, check git log, diff the files. The cron's step 4b is non-negotiable.

## Example: warboard /api/autonomous delegation (real run)

- Task spec: 2966 bytes → `/tmp/warboard-autonomous-task.md`
- Spawn: background CC, process `proc_d3f1a58e1adb` (PID 93660)
- Cron: `*/3 * * * *` × 6 reps
- Result: exit 0, commit `cbc957b` pushed, live endpoint verified via curl
- Updated: GitHub issue/PR with commit `cbc957b` and live endpoint verification
- Closed/retitled: related Tyler-facing Todoist action `6gQhG74RC647GWRm`
- Cron auto-cleared after successful completion
- Total user-facing wait: zero (kept talking about unrelated work during the run)
