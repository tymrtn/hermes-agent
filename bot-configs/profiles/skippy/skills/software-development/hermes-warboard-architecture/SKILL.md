---
name: hermes-warboard-architecture
description: Default architecture and repo conventions for the Hermes-native Warboard rebuild/fork. Use when Tyler wants to redesign, fork, or extend Warboard beyond the legacy heartbeat push flow.
---

# Hermes Warboard Architecture

Use this when Tyler asks to:
- fork Warboard
- build a Hermes-native Warboard
- redesign dashboard panes or data sources
- decide whether data belongs in Hermes vs Warboard
- decide whether Tailscale/custom API is needed

## Current known locations

- Legacy Warboard repo: `~/Dropbox/Code/warboard`
- Hermes-native fork: `~/Dropbox/Code/warboard-hermes`
- GitHub repo: `tymrtn/warboard-hermes`

## Core architectural rule

Split responsibilities cleanly:

### Warboard owns
- presentation/UI
- thin edge proxy endpoints safe to expose from the app
- direct fetches for low-risk public-ish data sources
- Telegram Mini App frontend behavior

### Hermes owns
- credentialed aggregation
- scheduled collectors
- cross-system rollups
- business metrics computation
- heartbeat payload generation
- anything that would otherwise stuff secrets or brittle integrations into the frontend app

If unsure, prefer putting the logic in Hermes and sending Warboard already-computed values.

## Default recommendations

### 1) Do not make the public frontend depend on Tailscale
Warboard is phone-facing and should stay reachable over normal public HTTPS.

Tailscale is only potentially useful for a **private Hermes → Warboard posting path**.
It is not the right foundation for the frontend.

### 2) Operational source-of-truth split
Use the right source for each pane:
- **GitHub Issues/PRs** for development stories, bugs, QA gates, deploy gates, commit/PR evidence, and engineering blockers.
- **Todoist** for Tyler-facing actions: outreach, follow-ups, approvals, high-level ideas, reminders, admin, and concrete decisions.

Warboard may render both, but should not recreate either source of truth internally.

### 3) Goals/progress definitions belong in Hermes config
Store metric definitions in Hermes, not in the public dashboard app.

Suggested shape per project:
- project name
- rank/importance
- top 1–2 metrics
- source adapter
- optional target
- direction (`up` / `down`)

Hermes should resolve the live values on a schedule and push them to Warboard.
Warboard should render them.

## Pane-by-pane ownership

### Best handled directly in Warboard
- Weather (for example Open-Meteo)
- Calendar via iCal feeds
- Todoist-backed exercise card
- GitHub-backed dev critical path card
- Todoist-backed human-action card

### Best handled by Hermes collectors
- MRR left to replace
- patent counts / submissions
- GitHub stars
- traffic / analytics rollups
- bot user counts
- deploy / commit / email rollups
- cross-project progress hero metrics

## Good default layout direction

Preferred top-of-dashboard order for the next live-command-center slice:
1. Live hero metrics with per-section freshness state
2. Today’s Progress / output
3. Critical Path from GitHub Issues/PRs plus Tyler action queue from Todoist
4. Active Blockers
5. Agents
6. Email
7. Market
8. Weather / Calendar / Goals once live
9. legacy or secondary sections lower down / collapsed

Remove or demote the old BMI contract-end countdown if it no longer matches real operating priorities. Do not let decorative/static pipeline cards consume prime mobile real estate until the data is live.

## First useful slice

When Tyler points at the Telegram Mini App and says the Warboard needs work, prioritize making it live over redesign polish:

1. Define a v5 or v4.1 data contract with section-level freshness metadata: `updatedAt`, `source`, `status=fresh|stale|error|unavailable`, and optional `errorSummary`.
2. Replace all-or-nothing heartbeat replacement with additive/partial section updates that preserve last-good data.
3. Remove fake frontend fallbacks; `0 agents`, email counts, market state, and pipeline/goals must be either fresh real data or explicit empty/unavailable states.
4. Keep GitHub as SSOT for dev critical path and Todoist as SSOT for human actions / exercise / completed-today; expose safe phone actions for complete, reschedule, and priority where appropriate.
5. Route agents, email, market, goals, deploys, commits, and cross-system progress through Hermes collectors.
6. Harden `/api/action` before broader rollout: auth/scoping, typed action schema, owner, sourceSection, dedupeKey, expected SLA, result status/message, updatedAt.
7. Make top pane order config-driven so Tyler can reprioritize without editing frontend HTML.

## Practical workflow

1. Inspect the legacy app in `~/Dropbox/Code/warboard`
2. Fork into `~/Dropbox/Code/warboard-hermes`
3. Keep new repo private by default
4. Write the architecture plan into `docs/plans/`
5. Implement additive API/UI changes in phases
6. Keep Hermes collectors separate from presentation concerns

## Suggested implementation phases

1. Foundation / naming / config cleanup
2. New hero + Today’s Progress
3. Exercise + Critical Path
4. Weather + Calendar
5. Goals cards + Hermes metric collectors

## Pitfalls

- Do not treat legacy OpenClaw assumptions as runtime truth; Hermes is the active runtime.
- Do not put business secrets or multi-system aggregation logic into the dashboard app unless there is a compelling reason.
- Do not let Warboard become a second task database when Todoist already exists.
- Do not force Tailscale into the frontend path just because Hermes can use it.
- Watch for stale placeholder behavior in `public/index.html`: hardcoded BMI/patent/pipeline/agent/email/market values can make the board look functional while it is not live.
- `builder/build-payload.py` may contain hardcoded bearer tokens and markdown-derived/static values; move secrets to env/config and replace markdown as source-of-truth with Hermes collectors.
- Public unauthenticated `/api/action` is an operational risk. Do not ship action handling without auth/scoping and visible result lifecycle.
- A Netlify static publish config can conflict with the Railway/Express deployment path and produce stale frontends; document Railway as canonical or remove the footgun.
- All-or-nothing heartbeat validators make one collector failure blank or freeze the board. Prefer section-level freshness and last-good rendering.
