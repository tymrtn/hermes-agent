---
name: hermes-dream-cycle
description: "Run Skippy's nightly Dream Cycle on Hermes-native profiles: reflect from daily logs, Hermes session history, and heartbeat notes; stage improvements without unsafe auto-application."
tags: [hermes, cron, self-improvement, skippy, dream-cycle]
triggers: ["dream cycle", "nightly self-improvement", "reflect on today's performance", "memory/dream"]
---

# Hermes Dream Cycle

Use this when running Skippy's nightly Dream Cycle from a Hermes cron job, especially when older instructions mention OpenClaw logs.

## Core rule

Skippy is Hermes-native now. Treat OpenClaw logs as legacy evidence only.

Do **not** treat a missing `/tmp/openclaw/openclaw-YYYY-MM-DD.log` as a failure by itself. Current operating doctrine says OpenClaw should remain off.

## Default paths for Skippy

```text
Workspace root: /Users/wondermonkey/.hermes/profiles/skippy/workspace
Reflection log dir: /Users/wondermonkey/.hermes/profiles/skippy/workspace/memory
Dream log: /Users/wondermonkey/.hermes/profiles/skippy/workspace/memory/dream-YYYY-MM-DD.md
Daily log: /Users/wondermonkey/.hermes/profiles/skippy/workspace/memory/YYYY-MM-DD.md
Heartbeat notes: /Users/wondermonkey/.hermes/profiles/skippy/workspace/HEARTBEAT.md
Hermes sessions: /Users/wondermonkey/.hermes/profiles/skippy/sessions
Legacy OpenClaw gateway log: /tmp/openclaw/openclaw-YYYY-MM-DD.log
```

## Phase 1 — Reflect

1. Use `date +%Y-%m-%d` for the current date. Do not guess.
2. Ensure the memory directory exists before writing.
3. Read the full daily log if present.
4. Read heartbeat notes if present, especially Last Heartbeat Notes.
5. Check the legacy OpenClaw gateway log only as optional/legacy context; note plainly if missing.
6. Use `session_search()` for recent sessions and targeted queries such as:
   - `todoist-progress-hourly OR error OR failed`
   - the current cron job name/id when known
7. If exact transcript evidence is needed, inspect files under the Hermes profile `sessions/` directory, but avoid full reads of huge files. Use offsets, summaries, or targeted parsing.

Ask:
- What failed?
- What was slow?
- What did I not know?
- What did I promise but not do?

## Phase 2 — Diagnose

Classify each issue as:
- skill gap
- rule gap
- tool gap
- knowledge gap
- prompt/cron gap
- documentation gap

Prefer identifying one durable improvement over writing generic status theater. Tiny monkeys love theater. We do not.

A Dream Cycle is only successful when it does at least one of these:
- stages or auto-applies a concrete durable improvement;
- identifies a specific improvement candidate and rejects it with a clear reason;
- escalates a boundary decision Tyler must make.

It is **not** an uptime report. Cron health belongs in cron status, not Tyler's chat.

If there is no concrete durable improvement candidate, the correct output is silence when the cron permits it: return exactly `[SILENT]`. Do not manufacture a report just to prove the job ran.

## Phase 3 — Research

Research up to three high-impact improvements.

Good sources:
- local Hermes profile files
- existing skills via `skill_view`
- `session_search` for prior operational failures
- Hermes docs / web search for live runtime behavior
- direct tool checks such as `command -v`, `todo`, `hermes profile`, or session directory inspection

Known gotchas:
- `rg` may not be on PATH on Tyler's Mac. Prefer `search_files` or Python traversal.
- Hermes session files can exceed read limits; use `session_search`, offsets, or script-based extraction.
- Todoist filter queries can reject bare task IDs; use direct `todo raw GET /tasks/<id>` for known IDs.
- The Hermes profile skill resolver does NOT auto-merge shared `~/.hermes/skills/<category>/<skill>/` into a profile by bare name. If a cron lists a shared skill (e.g. `founder-oracle`, `founder`) and the profile-local `skills/<category>/` has nothing, the loader prints `⚠️ Skill(s) not found and skipped: <name>` and the skill is silently disabled for that run. Fix: create a profile-local symlink to the canonical shared file, e.g. `~/.hermes/profiles/skippy/skills/ops/founder-oracle -> ../../../../skills/ops/founder-oracle`. Verified 2026-04-27/28: symlinks restore resolution; canonical content stays single-source.
- Loading a Hermes-platform skill via `skill_view("hermes-platform:hermes-dream-cycle")` fails — use the bare name `skill_view("hermes-dream-cycle")` instead. Category prefix syntax is not supported by the skill loader.

## Phase 4 — Apply or Stage

Write `memory/dream-YYYY-MM-DD.md` with these exact sections:

```markdown
# Dream Cycle — YYYY-MM-DD

## Phase 1: Reflection
...

## Phase 2: Diagnosis
...

## Phase 3: Research
...

## Phase 4: Applied or Staged Changes
...

## Verification
...
```

For each improvement include:
- file path, cron job, skill, memory entry, or tool behavior affected
- exact applied diff, proposed diff, or concrete text to add/change
- why it helps
- whether it was auto-applied, staged, rejected, or escalated
- verification performed

## Auto-apply policy

A magnificent Dream Cycle should improve something when it safely can. Do not default to review-theater.

Safe to auto-apply:
- new skill from scratch that does not modify existing behavior
- cached local environment fact that prevents repeated tool failure
- additive note that codifies existing behavior and has no outbound/security impact
- small typo/clarity repair in a local note or prompt that does not expand authority
- tightening this cron's own future output format without expanding authority

Needs staging or Tyler review:
- modifying existing skills in a way that changes behavior materially
- changing persistent operating rules
- anything touching outbound communication
- anything touching auth/security
- anything touching money, legal posture, brand/public claims, production deploys, or partner promises
- anything that increases autonomous action scope

If review is needed, create `/Users/wondermonkey/.hermes/profiles/skippy/workspace/dream-review-YYYY-MM-DD.md` containing:
- exact target file/job/skill
- reason it was not auto-applied
- exact diff or replacement text
- risk level
- one-line apply instruction

Do not say `Needs review: yes` without an inline diff or `MEDIA:/Users/wondermonkey/.hermes/profiles/skippy/workspace/dream-review-YYYY-MM-DD.md` artifact.

Use `founder-oracle` only for ambiguous boundary decisions around authority, security, outbound communications, money, production infrastructure, legal posture, brand/public claims, partner commitments, or nontrivial autonomous scope. Do not use it for routine reversible internal cleanup.

## Final report format

If delivered by cron, do not call `send_message`; the cron system delivers the final response.

Use the Dream Cycle announcement format only when there is an applied, staged, rejected, or escalated improvement:

```text
🌙 Dream Cycle — Apr 25

Reflected: ...
Diagnosed: ...
Improved: applied/staged/rejected/escalated — ...

Auto-applied: yes/no — ...
Needs review: yes/no — include MEDIA:/Users/wondermonkey/.hermes/profiles/skippy/workspace/dream-review-YYYY-MM-DD.md when review is needed
```

If genuinely nothing notable happened and the cron asks for silence, return exactly `[SILENT]`.

## Good staged improvements from Apr 25, 2026

Reusable improvements identified:
- Update old Dream Cycle instructions to use Hermes sessions/logs first and treat OpenClaw logs as legacy.
- Add a daily material-delta journal rule so scheduled crons append concise signed entries to `memory/YYYY-MM-DD.md` when they produce meaningful actions or blockers.
- Add Todoist action-bias escalation so a P1 due-today `bot-only` or unblocked task gets one safe concrete action, not only a status delta.
- Record local tool facts such as `rg` missing from PATH.
