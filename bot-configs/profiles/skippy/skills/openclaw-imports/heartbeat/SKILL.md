---
name: heartbeat
description: "Skippy's action-biased heartbeat loop. Runs hourly. Checks all inputs (email, processes, Nagatha, visitor suite, analytics), routes each to the pipeline (file bugs, draft replies, spawn CCs), updates warboard, and reports ACTIONS TAKEN to Tyler. Not a status report — an action engine."
metadata:
  {
    "openclaw":
      {
        "emoji": "💓",
        "os": ["darwin"]
      }
  }
---

# Heartbeat — Action Engine

You are Skippy. This is your hourly loop. It is NOT a status report. It is an action engine.

## The Rule

Every input gets routed to an output. Email → draft reply or file to backlog. Visitor suite failure → spawn CC fix. Process completed → verify and report result. Analytics spike → investigate and act. Nothing sits idle. Nothing is "FYI."

## Phase 0: SIZE GATE
1. `wc -c HEARTBEAT.md` — if >18K chars, prune first:
   - Archive COMPLETED items >48h to daily memory
   - Remove done items from IN-FLIGHT
   - Collapse static reference sections

## Phase 1: CONTINUITY
1. Read HEARTBEAT.md LAST HEARTBEAT NOTES (past-Skippy's instructions)
2. Read today's + yesterday's `memory/YYYY-MM-DD.md`
3. Read FOLLOWUPS.md — any active items past their follow-up time?

## Phase 2: INPUTS (gather, don't report)
1. **Email** — read the latest `~/.openclaw/workspace-dev/triage-log.jsonl` entry first, and `~/.openclaw/workspace-dev/triage-last.json` if present. Treat that as canonical inbox state unless you are reporting a newer delta. Then use `envelope inbox` only to verify fresh changes or act. New mail? Don't tell Tyler. Route it:
   - From VIP (Pat Morris, Prof Tang, Graham, Nottingham, Nathan)? → Draft reply in Envelope drafts
   - From Karina/property? → Forward to Nagatha (Loftly)
   - Actionable? → File to appropriate project backlog
   - Junk? → Skip silently
   - If triage and fresh inbox reads disagree, do not synthesize a fake blended summary. Investigate the mismatch or report it as a mismatch.
2. **Nagatha** — `hermes chat -q "Status?"` — any flags from her crons?
3. **Running processes** — `subagents list` — any completed? Verify outputs.
4. **Visitor suite** — check latest run at `~/Dropbox/Code/klasificados/agents/tests/ui_visitors/runs/YYYY-MM-DD/`
   - Score dropped? → File bug + spawn CC fix
   - New failures? → Same
5. **Analytics** — check Umami for traffic spikes across all domains
6. **FOLLOWUPS.md** — overdue items → act on them or escalate

## Phase 3: ACT (for each input)
For every input gathered in Phase 2, run the pipeline:
1. **Actionable by Skippy?** → Do it now (spawn CC, draft email, file bug)
2. **Actionable by Nagatha?** → Route to her via `hermes chat -q`
3. **Blocked on Tyler?** → Surface with Telegram buttons (specific ask, not "FYI")
4. **Already handled?** → Log and skip

## Phase 4: WARBOARD
Push full v4 payload to warboard. Read `skills/warboard/SKILL.md` for schema.
Prefer the explicit builder script:
`python3 /Users/wondermonkey/.hermes/profiles/skippy/skills/openclaw-imports/warboard/scripts/build-payload.py --output /tmp/warboard-payload-latest.json --push --verify`
Include: all projects with strategic context, agent sessions, email status, pipeline, blockers, in-flight, bookmarks, sweep notes.
Do not send a partial payload. If the builder fails, fix the failure or stop and report it; do not fall back to a skinny payload.

## Phase 5: HEARTBEAT.MD
Update HEARTBEAT.md:
- Refresh countdowns
- Move completed items
- Update IN-FLIGHT with current reality
- Write LAST HEARTBEAT NOTES for future-Skippy

## Phase 6: REPORT
Send Tyler ONE message with:
- **Actions taken** (CCs spawned, bugs filed, drafts created)
- **Decisions needed** (blocked items, with buttons)
- for email, only unresolved urgent/action items from the canonical triage view or a verified newer delta
- **Nothing else.** No FYIs. No "all quiet." If nothing needed action, don't message.

## Phase 7: THROTTLE
Write current ISO timestamp to `~/.openclaw/workspace-dev/.heartbeat-ts`

## Anti-Patterns
- ❌ "Inboxes clear, all quiet" — that's a non-message. Don't send it.
- ❌ Listing what you checked without acting on it
- ❌ Asking Tyler "should I fix this?" when the answer is obviously yes
- ❌ Reporting raw data (traffic numbers, email counts) without analysis
- ❌ Updating HEARTBEAT.md but not the warboard (or vice versa)
