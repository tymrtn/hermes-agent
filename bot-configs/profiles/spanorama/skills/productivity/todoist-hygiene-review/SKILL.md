---
name: todoist-hygiene-review
description: Clean up stale Todoist task graveyards for Spanorama-owned communications work by finding, labeling, updating, closing, and creating recurring hygiene tasks.
tags: [todoist, task-management, spanorama, hygiene, cleanup]
triggers: ["Todoist graveyard", "stale Todoist tasks", "organize Todoist", "clean up tasks", "maintain Todoist", "review Spanorama tasks"]
---

# Todoist hygiene review

Use this when Tyler says there is a Todoist task graveyard, asks Spanorama to maintain Todoist, or points out that instructions were updated but Todoist itself was not.

## Principle
Do not only update memory/instructions. Actually inspect and modify Todoist.

Todoist is an operational surface, not a note to self. If Tyler asks for Todoist hygiene, perform concrete Todoist actions and verify the result.

Use Todoist projects as the primary structure for business/workstream ownership (SpainExpat, Expatriator, Loftly). Use labels only as secondary owner/routing/status metadata (`spanorama`, `needs-approval`, `email`, `migration`). Do not imply a label replaces proper project organization.

## Workflow
1. Search for a dedicated Spanorama project first.
   - `find_projects(searchText="Spanorama")`
   - If none exists, search tasks broadly for `spanorama`, `spainexpat`, `expatriator`, `loftly`, and stale historical owner labels like `nagatha` when ownership may have changed.
2. Check whether a `spanorama` label exists.
   - If missing, create a favorite `spanorama` label.
3. Triage candidate tasks.
   - Add `spanorama` label to active Spanorama-owned communications tasks.
   - Remove stale ownership labels such as `nagatha` when Tyler has corrected ownership.
   - Close tasks whose blocker/status is resolved.
   - Clear due dates from blocked/non-actionable trackers.
   - Reschedule real follow-up/migration work to a plausible date.
   - Keep Tyler approval gates due now if Tyler action is genuinely needed.
4. Make tasks self-contained.
   - Description should include current state, evidence/refs, blocker/owner, next explicit move, and acceptance criteria when applicable.
   - One task should represent one decision/action.
5. Add a recurring hygiene task if none exists.
   - Example: `Spanorama: weekly Todoist hygiene review`
   - Due: `every Monday`
   - Labels: `spanorama`, `bot-only`
   - Description should require closing resolved tasks, removing stale dates, keeping approval gates current, and removing stale ownership references.
6. Verify with a filtered task query.
   - Query `labels=["spanorama"]` and report the active count plus what changed.

## Pitfalls
- Memory updates are not Todoist updates. If the request concerns Todoist, use Todoist tools.
- A broad overview call may fail with Todoist 502/503. Retry with narrower searches instead of stopping.
- Search projects before assuming labels are needed. Tyler may already have project structure; keep labels secondary to projects.
- Do not complete approval gates just because they are old; if Tyler still needs to decide, keep them open and due/current.
- Do not send email during a hygiene review. Draft/report/escalate only.
- Avoid "decision packet" language. When presenting follow-up work, give concise options and offer to start immediately.
- Do not say Spanorama can do an email cutover while migration tooling is still being prepared. Offer safe adjacent work only: verification checklist, Todoist cleanup, draft tightening, or signup-page review.

## Report format
Keep it short:
- `Done:` bullets for concrete modifications.
- Include the verified active `@spanorama` count.
- Mention any tasks intentionally left open because Tyler approval is still required.
