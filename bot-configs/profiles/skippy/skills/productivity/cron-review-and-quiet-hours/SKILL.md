---
name: cron-review-and-quiet-hours
description: Repair recurring Hermes cron jobs that nag overnight or say "needs review" without giving Tyler a review artifact/diff.
tags: [cron, todoist, dream-cycle, review, quiet-hours]
---

# Cron Review Artifacts and Quiet Hours

Use when Tyler complains that a cron/reminder:
- ran during the night after he said it should not,
- repeats or nags too much,
- says `needs review` but provides no way to review or apply anything,
- needs a schedule/prompt repair in place.

## Core expectations

1. Update the existing cron job in place. Do not create a duplicate unless Tyler explicitly asks.
2. Use `cronjob(action='list')` first to get the real `job_id`; never guess.
3. If the problem is overnight noise, repair the schedule directly.
4. If the problem is `needs review` with no review path, repair the stored cron prompt so future runs must include an artifact or inline diff.
5. Tell Tyler exactly what changed, briefly.

## Quiet-hours repair

If Tyler says a recurring reminder/progress cron should stop during the night:

1. List jobs:
   ```text
   cronjob(action='list')
   ```
2. Identify the existing job by name.
3. Update its schedule in place.

Common repair for an hourly job that should only run during waking hours in Madrid:

```text
cronjob(action='update', job_id='REAL_ID', schedule='0 8-22 * * *')
```

This changes `0 * * * *` (every hour, including overnight) to hourly only from 08:00 through 22:00 local time.

## Todoist reminder frequency and formatting repair

If Tyler says Todoist/progress reminders are too frequent but still useful, do not just delete them and stop. Preferred repair:

1. List jobs and find the existing reminder/progress job.
2. If already removed, recreate under a clearer name; otherwise update in place.
3. For development projects, have the cron read GitHub Issues/PRs for engineering truth and Todoist only for Tyler-facing actions/decisions. Do not summarize long dev history from Todoist comments.
4. Use morning/evening cadence unless Tyler specifies otherwise:
   ```text
   0 9,18 * * *
   ```
4. Make the output glanceable for Telegram:
   - short enough for one phone screen,
   - bold section headers,
   - emoji anchors/status symbols,
   - no raw Todoist dumps,
   - no manufactured urgency when there is no meaningful delta.

Suggested output template:

```text
**Todoist check — morning/evening**

**📊 Snapshot**
- Due today: N
- Overdue: N
- High priority: N

**🧭 Delta**
- ✅/⚠️/💤 one concise material change, or `💤 none meaningful`

**🎯 Focus**
1. 🔥/➡️ task name — why it matters now
2. ➡️ task name — why it matters now
3. ➡️ task name — why it matters now

**⚠️ Blocked/risky**
- Only include if applicable.
```

Morning should bias toward planning the day; evening should bias toward what slipped, what cleared, and tomorrow's setup.

## Review-artifact repair

If a cron says `Needs review: yes` but provides no diff, file, or apply path, treat that as a run-format bug.

Repair the cron prompt in place so future runs require:

- exact target file/job/skill,
- reason it was not auto-applied,
- exact diff or replacement text,
- risk level,
- one-line apply instruction,
- a `MEDIA:/absolute/path` review artifact when the diff is nontrivial,
- inline diff when under about 80 lines.

For Dream Cycle review packages, preferred path:

```text
/Users/wondermonkey/.hermes/profiles/skippy/workspace/dream-review-YYYY-MM-DD.md
```

Final response from the cron should include:

```text
Needs review: yes, MEDIA:/Users/wondermonkey/.hermes/profiles/skippy/workspace/dream-review-YYYY-MM-DD.md
```

## Important gate behavior

If `skill_manage` blocks a patch with a security/governor gate, do not route around it silently. Surface the gate to Tyler. If a practical cron repair can still be made safely, do that, but distinguish:

- cron prompt fixed now,
- skill patch blocked pending confirmation/review.

## Final user response pattern

Keep it short:

```text
Saved/updated what I could.
- Repaired cron job: [job name], [old behavior] -> [new behavior].
- Saved reusable skill: [skill name].
- Could not patch existing skill [name] because [gate reason].
```
