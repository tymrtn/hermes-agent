---
name: warboard
description: Push full dashboard updates to Tyler's Warboard (warboard.tmrtn.com). Use when updating the Warboard, pushing heartbeat data, or when Tyler says "update warboard." Reads the v4 payload schema, builds the full payload with all sections (projects, agents, email, pipeline, blockers, in-flight, lists, bookmarks, sweep notes), and POSTs to the API. Also handles polling for Tyler's pending actions.
---

# Warboard Skill

Push structured dashboard updates to `warboard.tmrtn.com`. The Warboard is Tyler's strategic command center — every item connects to a North Star ($50M acquisition, $20K/mo revenue, 1K stars, June 30 deadline).

## API

**Endpoint:** `POST https://warboard.tmrtn.com/api/heartbeat`
**Fallback:** `POST https://warboard-production-5cff.up.railway.app/api/heartbeat`
**Auth:** `Authorization: Bearer 075e3a492fc894060a59fdb47ec89744b5164d3f7da2274b`
**Content-Type:** `application/json`

**Poll Tyler's actions:** `GET https://warboard.tmrtn.com/api/actions/pending` (same auth)
**Complete action:** `POST https://warboard.tmrtn.com/api/actions/:id/complete` (same auth)

## What "Update Warboard" Means

1. Read HEARTBEAT.md for current state
2. Read today's and yesterday's memory files for recent activity
3. Check running processes (`sessions_list`, `subagents list`)
4. Build the FULL v4 payload (see `references/payload-schema.md`)
5. Every project gets a `details` field explaining how it connects to the North Stars
6. POST to the API
7. Also update HEARTBEAT.md with current state

**Never push a partial payload.** The Warboard replaces everything on each push. Missing fields render as empty sections.

## Canonical Repo & Builder Path

**Canonical repo:** `~/Dropbox/Code/warboard-hermes` → `github.com/tymrtn/warboard-hermes` (Hermes-native fork; old `tymrtn/warboard` is archived as of 2026-05-01).

**Local archive:** `~/Dropbox/Code/warboard.archived-20260501` (read-only reference, do not edit).

Builder script (still pinned to the Skippy profile path):

`/Users/wondermonkey/.hermes/profiles/skippy/skills/openclaw-imports/warboard/scripts/build-payload.py`

Typical command:

```bash
python3 /Users/wondermonkey/.hermes/profiles/skippy/skills/openclaw-imports/warboard/scripts/build-payload.py \
  --output /tmp/warboard-payload-latest.json \
  --push --verify
```

This exists specifically to stop the recurring failure mode where a heartbeat sends only `countdown`, `blockers`, `inFlight`, and `bookmarks`, leaving the rest of v4 null.

**Phase 1 vNext design** lives in `~/Dropbox/Code/warboard-hermes/docs/plans/` and `docs/DESIGN.md` — that's the source of truth for any new panes (weather, exercise, hero progress, today's progress, critical path, calendar, goals).

## Rules

- `companies` array = project portfolio cards. Each needs: name, status (green/yellow/red), metric, detail, details (markdown with strategic context), items (expandable list), progress (0-100 optional).
- `agents` array = CC/Codex/Gemini/Hermes sessions. Each needs: engine, task, status (running/done/error), company, timestamp, detail (what it accomplished and why it matters).
- `lists` array = arbitrary card lists. Use for "Tyler's Plate", "Recent Wins", "Decisions Needed", etc.
- `email` object needs `accounts` (per-account status), `outbound` (sent/failed/queued), `agent` (Nagatha status).
- `pipeline` array = revenue/milestone tracking with progress bars.
- `sweepNotes` = markdown summary of what changed since last push. This is what Tyler reads first.
- Every blocker and in-flight item gets a `detail` field with markdown context.
- Status values: use string "green"/"yellow"/"red" for companies. Use emoji for blocker status.

## Quick Push (for heartbeats)

When pushing during a routine heartbeat, use `/Users/tylermartin/.openclaw/workspace-dev/skills/warboard/scripts/build-payload.py` and keep the payload complete. Only build inline if the script is unavailable, and if you build inline you must still include ALL sections because the API replaces the entire state.

## Project Colors (for reference)

Aposema=#8B5CF6, Envelope=#3B82F6, Redline=#EF4444, Klasificados=#F59E0B, Expatriator=#14B8A6, Loftly=#EC4899, Governor=#A78BFA, Family Book=#10B981, Nagatha=#60A5FA, Trading=#22C55E
