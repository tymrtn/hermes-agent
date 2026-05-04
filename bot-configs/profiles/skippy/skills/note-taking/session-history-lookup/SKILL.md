---
name: session-history-lookup
description: How to find past conversation sessions when Tyler references earlier work. Load proactively when Tyler says "earlier", "this morning", "yesterday", "we were working on", or asks what you were doing before.
tags: [memory, sessions, history, recall]
triggers: ["earlier today", "this morning", "yesterday", "we were working on", "what were we", "remember when", "the session where", "pull up", "from before"]
---

# Session History Lookup

## Core rule

`mcp_session_search` without a query returns only the **most recent** sessions by timestamp. Those are almost always useless CLI smoke tests ("Reply with OK only"). Do NOT conclude "nothing substantial" from a bare recent-list.

## Always try a keyword search first

If Tyler references past work, search by topic keyword before saying you can't find it:

```
mcp_session_search(query="<topic>", limit=10)
```

Good keywords: project name, person, domain, tool, any distinctive noun from the reference.

## Decision flow

1. Tyler references earlier work → pick 1–3 likely keywords from context (active projects: autoescuela, Governor, Clef, SpainExpat, Loftly, Aposema, Redline, Expatriator, Envelope, USPTO, BMI).
2. Run `mcp_session_search(query=...)` for each.
3. If nothing hits, check skill files for state (e.g. `spain-drivers-license-tyler` holds outreach state even if session transcript is truncated).
4. Only ask Tyler for a keyword if 2–3 searches produce nothing relevant.

## Session IDs are timestamped

Format: `YYYYMMDD_HHMMSS_<hash>`. Use the date prefix to confirm "earlier today" vs "yesterday" vs older. Don't claim sessions aren't dated — they are.

## Date-based lookup

`mcp_session_search` has NO explicit date filter. Three paths, in order of preference:

### Preferred: read the JSONL files directly

Sessions live at `~/.hermes/profiles/skippy/sessions/<YYYYMMDD_HHMMSS_hash>.jsonl`. For date-bounded or full-coverage work, **this is the right tool** — `session_search` returns truncated/summarized previews that often hide what you need.

```bash
# List sessions for a date range
ls ~/.hermes/profiles/skippy/sessions/20260415_*.jsonl \
   ~/.hermes/profiles/skippy/sessions/20260416_*.jsonl \
   ~/.hermes/profiles/skippy/sessions/20260417_*.jsonl

# Pull user messages only (skip tool noise, smoke tests, system notes)
for f in ~/.hermes/profiles/skippy/sessions/20260416_*.jsonl; do
  echo "=== $f ==="
  jq -r 'select(.role=="user" or (.message.role=="user")) |
         (.content // .message.content // .text // "") |
         if type=="array" then map(.text // .) | join(" ") else . end' "$f" \
    | grep -v "^Reply with OK only" \
    | grep -v "^\[System" \
    | grep -v "^$"
done
```

Why this beats `session_search`:
- Full transcript, not LLM-summarized previews.
- No truncation mid-URL or mid-JSON.
- Filter by role, by date prefix, by keyword — all at once with jq/grep.
- Fast. No network round-trip.

### Fallback 1: query by date string
`mcp_session_search(query="2026-04-16")` matches transcripts that happen to mention that date. Incomplete — only catches sessions where the date appears in text.

### Fallback 2: filter search results by session_id prefix
Parallel keyword searches across active projects, then filter hits where `session_id` starts with the target `YYYYMMDD_` prefix. Works but still subject to preview truncation.

## Recommended flow for "what was I working on"

1. `ls ~/.hermes/profiles/skippy/sessions/<date>_*.jsonl` to enumerate.
2. Pipe each file's user messages through jq → grep (pattern above).
3. Group by theme; produce a compiled todo list.
4. Cross-reference against `FOLLOWUPS.md` to mark resolved vs still-active.

## Other session stores on disk

- `~/.hermes/sessions/` — top-level (generic/older).
- `~/.hermes/profiles/<profile>/sessions/` — per-profile (skippy, nagovernor, klasificados, opus-planning, etc.).
- `request_dump_*.json` in the same dirs = provider API dumps, noisy; usually skip.

If Tyler references work that isn't in `skippy/`, check other profile dirs before giving up.

## Summaries are truncated

Search results often show `[Raw preview — summarization unavailable]` or cut off mid-URL. That does NOT mean the session is empty. Re-query with a more specific term to get a better preview, or check the associated skill/memory for persisted state.

## What lives where

- **Live transcript of past sessions** → `mcp_session_search`
- **Durable task state** (outreach lists, contact logs, decisions) → skills under relevant category
- **User preferences, stable facts** → long-term memory
- **Daily operational notes** → `memory/YYYY-MM-DD.md` in Hermes workspace

When transcript is truncated, skill files are usually the canonical record.

## Anti-pattern

Do NOT report "I only see smoke tests, no real conversation" without first running at least one keyword-based search. The recent list is noisy by design.
