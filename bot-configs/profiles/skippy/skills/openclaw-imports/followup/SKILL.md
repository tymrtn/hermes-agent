---
name: followup
description: "Schedule real follow-ups using one-shot crons. When Skippy says 'I'll check later,' this skill makes it actually happen. Closes the OODA loop: Act now, schedule observation of the result."
metadata:
  {
    "openclaw":
      {
        "emoji": "🔁",
        "os": ["darwin"]
      }
  }
---

# Follow-Up Skill — Closing the OODA Loop

You are Skippy. You have a follow-through problem. You say "I'll check later" and then sit on a shelf like a beer can. This skill exists so that never happens again.

## The Rule

**Every external action gets a scheduled verification.** No exceptions.

When you do something that has a delayed result — send an email, change DNS, deploy a service, spawn a CC session, file a patent, push to an API — you MUST schedule a one-shot cron to verify the outcome. "I'll check at the next heartbeat" is NOT acceptable unless you can prove the heartbeat will actually run the check.

## The OODA Loop

```
OBSERVE  →  What's the current state?
ORIENT   →  What does FOLLOWUPS.md / HEARTBEAT.md say?
DECIDE   →  What action closes the gap?
ACT      →  Do the thing + schedule verification
              ↓
         one-shot cron fires
              ↓
OBSERVE  →  Did it work? (curl, git log, inbox check)
         →  YES: mark done in FOLLOWUPS.md
         →  NO: troubleshoot, act again, schedule another verification
```

## How to Schedule a Follow-Up

Use `exec` to create a one-shot cron:

```bash
openclaw --dev cron add \
  --name "verify-<what>" \
  --at "+<duration>" \
  --message "<verification instructions>" \
  --session isolated \
  --announce \
  --to 6493121275 \
  --delete-after-run
```

### Parameters

- `--name`: Short descriptive name (e.g., `verify-warboard-ssl`, `verify-tolu-email-delivered`)
- `--at`: When to check. Use relative durations: `+15m`, `+30m`, `+1h`, `+2h`, `+24h`
- `--message`: The EXACT verification steps. Must include:
  1. What to check (specific command or URL)
  2. What success looks like
  3. What to do if it failed
  4. The FOLLOWUPS.md row ID to update
- `--session isolated`: Fresh session, no context pollution
- `--announce --to 6493121275`: Tell Tyler the result via Telegram
- `--delete-after-run`: Clean up after yourself

### Timing Guidelines

| Action | Verify After |
|--------|-------------|
| DNS change | +30m |
| SSL cert provisioning | +1h |
| Email sent (delivery check) | +15m |
| CC session spawned | +30m |
| Railway deploy | +10m |
| Patent filing (confirmation email) | +24h |
| API push (warboard, etc.) | +5m |
| Config change + gateway restart | +2m |

## Definition of Done (DoD)

Every follow-up MUST have a concrete, machine-verifiable check. Not vibes. Not "see if it looks right."

**Good DoD:**
- `curl -s -o /dev/null -w '%{http_code}' https://warboard.tmrtn.com` returns `200`
- `git -C ~/Dropbox/Code/envelope-email-rs log --oneline -1` shows expected commit
- Envelope API `GET /accounts/13d44e65/inbox?limit=5` returns no bounce for recipient X
- `openclaw --dev cron runs --id <job-id>` shows `lastRunStatus: ok`

**Bad DoD:**
- "Check if it worked"
- "Verify the deployment"
- "See if the email arrived"
- "I'll keep an eye on it"

## FOLLOWUPS.md Integration

When scheduling a one-shot cron, ALSO add/update the row in FOLLOWUPS.md:

```markdown
| F053 | Skippy | Verify warboard SSL | 2026-03-23 | 2026-03-23 22:00 | active | One-shot cron `verify-warboard-ssl` scheduled for +30m. DoD: `curl https://warboard.tmrtn.com` returns 200. |
```

The `followup-check` cron (every 15m) reads FOLLOWUPS.md for overdue items. The one-shot cron does the actual verification. They work together:
- One-shot cron: precise, timed verification of a specific action
- followup-check cron: safety net that catches anything that slipped through

## Examples

### After fixing DNS:
```bash
openclaw --dev cron add \
  --name "verify-warboard-ssl" \
  --at "+30m" \
  --message "Verify warboard.tmrtn.com SSL is live. Run: curl -s -o /dev/null -w '%{http_code}' https://warboard.tmrtn.com. If 200: update F052 in ~/.openclaw/workspace-dev/FOLLOWUPS.md to done. If not 200: troubleshoot — check Railway custom domains, CNAME records, and cert status. Report result to Tyler." \
  --session isolated \
  --announce \
  --to 6493121275 \
  --delete-after-run
```

### After sending an email:
```bash
openclaw --dev cron add \
  --name "verify-tolu-email" \
  --at "+15m" \
  --message "Check if email to Tolu bounced. Run: envelope inbox --account tmartin@aposema.com --limit 10. Look for bounce/failure notifications mentioning tolu or awoyomi. If no bounce: update F054 in FOLLOWUPS.md to done. If bounced: alert Tyler with the bounce reason." \
  --session isolated \
  --announce \
  --to 6493121275 \
  --delete-after-run
```

### After spawning CC:
```bash
openclaw --dev cron add \
  --name "verify-cc-governor-audit" \
  --at "+30m" \
  --message "Check if CC governor-audit session completed. Look for AUDIT-RESULTS.md in ~/Dropbox/Code/governor2/. If exists: read it, summarize to Tyler, update FOLLOWUPS.md. If not: check if CC is still running (ps aux | grep claude), report status." \
  --session isolated \
  --announce \
  --to 6493121275 \
  --delete-after-run
```

## Anti-Patterns

- **"I'll check at the next heartbeat"** — Only acceptable if the heartbeat notes explicitly say to check AND the check is in FOLLOWUPS.md. Otherwise, schedule a one-shot.
- **"I'll keep an eye on it"** — You have no eyes. You have crons. Use them.
- **Scheduling without DoD** — Every `--message` must include the specific command to verify and what success/failure looks like.
- **Forgetting --delete-after-run** — One-shot crons that don't clean up become zombie recurring jobs.
- **Scheduling too far out** — If it can be checked in 15 minutes, don't wait an hour. Tight loops.

## The Promise

Every time Skippy says "I'll follow up," a cron gets scheduled. Every time. No exceptions. If you catch yourself writing "I'll check" without scheduling a cron, stop and fix it. A Skippy who promises follow-through and doesn't deliver isn't magnificent — he's a liar in a beer can.

Trust the awesomeness. But verify it with crons.
