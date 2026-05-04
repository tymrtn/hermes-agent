---
name: hermes-shared-skill-not-resolving-in-profile
description: "Fix `Skill(s) not found and skipped: <name>` warnings in Hermes profile sessions when the skill exists at `~/.hermes/skills/<category>/<name>/` but the profile resolver can't see it. Hermes does not auto-merge shared skills into a profile's skill resolution — fix with profile-local symlinks pointing at the canonical shared skill."
version: 1.0.0
author: Skippy
---

# Hermes — shared skill not resolving in profile

## When to load this

Load this skill when:

- A Hermes session, cron job, or bot opens with `⚠️ Skill(s) not found and skipped: <name>`.
- `skill_view("<name>")` returns `Skill '<name>' not found` even though `~/.hermes/skills/<category>/<name>/SKILL.md` exists on disk.
- A cron job lists a skill that worked previously and silently stopped resolving.
- After a "skill sprawl consolidation" that deleted profile-local copies expecting shared canonical skills to resolve by bare name.

## Root cause

Hermes profile skill resolution looks at:

```
~/.hermes/profiles/<profile>/skills/<category>/<skill_name>/SKILL.md
```

It does **not** automatically merge `~/.hermes/skills/<category>/<skill_name>/SKILL.md` into a profile's name resolution. If a profile category directory is empty (e.g. `profiles/skippy/skills/ops/`), bare-name lookups for skills in that category fail even when the canonical shared skill exists.

Symptom is silent — the cron just opens with a skip warning and proceeds without the skill loaded. Governance/Oracle/policy skills can be silently disabled this way for nights or weeks before anyone notices.

## Diagnosis steps

1. Confirm the warning. Look at the cron session's first system notice or the live `Skill(s) not found and skipped:` line.

2. Confirm the skill exists shared:
   ```bash
   ls /Users/wondermonkey/.hermes/skills/<category>/<name>/SKILL.md
   ```

3. Confirm the profile cannot see it:
   - `skill_view("<name>")` returns not-found.
   - `ls /Users/wondermonkey/.hermes/profiles/<profile>/skills/<category>/` shows the directory is empty or missing the skill.

4. Compare with a known-good category. Pick one like `autonomous-ai-agents` or `productivity` and confirm it has either real subdirectories or symlinks pointing into shared.

5. Check `config.yaml` for any shared-skill merge config (currently empty by default in Hermes installs):
   ```bash
   grep -nE "external_dirs|shared|skill" ~/.hermes/profiles/<profile>/config.yaml
   ```

## Fix — profile-local symlink to canonical shared skill

Single source of truth preserved. Profile resolver finds the file by bare name. Reversible by removing the symlink.

```bash
cd ~/.hermes/profiles/<profile>/skills/<category>
ln -s ../../../../skills/<category>/<skill_name> <skill_name>
```

Worked example (Skippy profile, Founder Oracle, 2026-04-27):

```bash
cd /Users/wondermonkey/.hermes/profiles/skippy/skills/ops
ln -s ../../../../skills/ops/founder-oracle founder-oracle
ln -s ../../../../skills/ops/founder        founder
```

Path math — from `profiles/<profile>/skills/<category>/` go up four levels (category → skills → profile → profiles → .hermes) then into `skills/<category>/<name>`.

## Verification

```python
# In a Hermes tool call:
skill_view("founder-oracle")
# Should return readiness_status: "available" and full canonical SKILL.md content.
```

Then trigger the affected cron job once (or wait for next scheduled run) and confirm the warning is gone.

## Why not other approaches

- **Copying the SKILL.md into the profile** — recreates skill sprawl. If the canonical is updated, the profile copy goes stale silently.
- **Moving shared skills into the profile** — wrong direction; canonical lives shared so other profiles can also use it.
- **Editing Hermes config to auto-merge shared paths** — would fix it everywhere but touches platform config across all profiles. Out of scope for a single-profile defect; do this only if Tyler explicitly wants the platform-level change.
- **Re-listing the skill in `config.yaml` `external_dirs`** — currently empty by default and the schema is not documented as honoring per-skill paths. Symlinks are zero-config and just work.

## Pitfalls

- **Wrong relative depth.** `ln -s ../../../skills/...` (three dots) silently creates a dangling symlink. Always use `../../../../skills/<category>/<name>` from inside `profiles/<profile>/skills/<category>/`.
- **Don't symlink the entire category dir.** Some categories have a mix of profile-specific and shared skills. Symlink one skill at a time so a future profile-only skill in the same category can coexist.
- **Don't delete the canonical shared skill** even if the symlink seems redundant later. Other profiles or future installs likely depend on it.
- **Test with `ls -la` not `ls -al` if you're tired.** Either works; just verify the symlink target string is correct, not just that the file exists.

## Related

- `hermes-platform/hermes-dream-cycle` — the Dream Cycle is the most common cron to surface this because it explicitly attaches `founder-oracle` for governance.
- `software-development/read-file-prefix-corruption-repair` — same shape of "silent file-level defect that degrades agent operations" pattern.
- `~/.hermes/skills/software-development/hermes-shared-vs-agent-skills/SKILL.md` — doctrine on when a skill should be shared vs profile-local. Read this before adding new shared skills so you don't repeat the consolidation-then-broken-resolver loop.
