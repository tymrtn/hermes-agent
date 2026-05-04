---
name: autonomous-agent-cron-design
description: Design autonomous cron-driven agent systems that use Todoist as SSOT, treat each slot as one story, orchestrate Claude Code/Codex, and stay resilient to dirty branches, restarts, and context loss.
version: 1.0.0
author: Nagaklas
---

# Autonomous Agent Cron Design

Use this when designing or repairing a cron-driven autonomous agent loop for a real project or business. This skill is about making cron sessions behave like a disciplined operating system, not a stream of vague status reports.

## Core doctrine

### 1. SSOT first
For day-to-day execution, define one explicit source of truth and force every cron to consult it first.

Recommended stack:
- **GitHub Issues/PRs** = canonical engineering tracker when the project has switched engineering work to GitHub
- **Todoist** = same-day operational SSOT and task bus, including prioritization, approvals, Tyler comments, slot routing, and migrated context
- **Repo backlog / stories** = durable specs and acceptance criteria when still in use
- **`ops/core-loop-state.md`** = rolling handoff between fresh cron sessions
- **`git log` / worktrees / branches** = what actually happened in code

Rules:
- If it is engineering implementation state, it belongs in GitHub Issues/PRs once GitHub is the engineering tracker.
- If it is same-day operational routing, prioritization, or approval state, it belongs in Todoist.
- If the work crosses GitHub and Todoist, both surfaces must be updated before closeout.
- Any GitHub Issue/PR label, marker, body note, or comment indicating an associated Todoist task must include the direct Todoist task URL; any Todoist task routing to GitHub should include the direct GitHub URL.
- If it is a durable feature or bug spec, it belongs in the chosen backlog/story surface.
- If it is cross-run memory, it belongs in the shared state file.
- If it is not reflected in one of those surfaces, it is not tracked.

### 2. One slot = one story
A cron slot is not a review bucket.

Use this standard:
- **one slot = one story**
- **one story = the smallest user-meaningful, acceptance-testable, potentially deployable unit of functionality or improvement**
- **acceptance criteria describe the user's experienced outcome and important edge cases, not obvious implementation checks**

For product/UX stories, ACs must include taste and brand judgment where relevant. Do not reduce acceptance to pedantic technical DoD like "returns 200" or "image tag exists" unless that is genuinely the edge case. Good ACs say what the experience should feel like and what would make it shippable: e.g. "Condado real estate evokes intrigue and aspirational luxury while remaining authentically Condado," "matches Klasificados style and brand guide," "mobile keeps the same feel," and "Boriquen mode adds a specific local flourish without parody." Technical probes belong under evidence or QA checks, not as the headline acceptance criteria.

For visual/product ACs, Todoist updates must request screenshot evidence that demonstrates the ACs. Screenshots should be saved in a durable repo path such as `ops/qa/story-NNN/`, but repo-local paths alone are not review evidence in Todoist. Attach screenshots, provide accessible image links, or deliver them in chat with `MEDIA:`; the Todoist task should summarize what each visible screenshot demonstrates. Use the `klasificados-visual-review-evidence` skill for visual/design review tasks. Do not ask Tyler to review a visual story from prose alone.

This means a cron slot is not successful just because it:
- reviewed outputs
- inspected repo state
- wrote a vague handoff
- narrated priorities
- created a QA packet but did not run the QA that was available in the slot
- created a deploy-prep packet but did not make the go/no-go decision it had enough evidence to make

For Klasificados dev/integration slots, the minimum Definition of Done for code-affecting work is an opened GitHub pull request with tests/evidence in the PR body. A pushed branch, local commit, QA packet, or "ready for review" note is not DoD.

A slot succeeds only when it ends in one of these states:
- pull request opened, with direct PR URL, linked issue/Todoist task when available, test commands/results, and review evidence in the PR body
- merged to `main` and verified, when deployment is authorized and safe
- deployed and production-smoked, when the slot is a deploy slot and deployment is authorized and safe
- a story is blocked by a concrete external dependency the slot could not resolve: Tyler decision, unavailable credential, unavailable non-production write target, absent staging surface, test environment failure, or actual deploy/data risk with evidence
- an implementation is reverted or explicitly abandoned because QA proved it unsafe

`READY_FOR_QA`, `READY_FOR_REVIEW`, `READY_FOR_STAGING`, `branch pushed`, `tests passed`, and `packet written` are failure states for a dev slot unless the slot names the exact external blocker that made opening a PR impossible. A packet is evidence, not delivery.

### 2.5 Optional Hermes Kanban layer

Hermes Kanban can be useful as an internal agent execution queue, but do not treat it as a replacement for project trackers without a bridge.

Use it for:
- durable cross-profile worker tasks
- dispatcher-owned work queues
- comments/dependencies between agents
- work that must survive restarts and be visible to multiple profiles

Do not use it as the sole truth for Tyler-facing or engineering tracking unless the project explicitly switches. For Klasificados-style split tracking, the safe model is:
- GitHub Issues/PRs = engineering truth
- Todoist = ops/prioritization/approval truth
- Hermes Kanban = internal agent execution queue

Before relying on Kanban, verify the local Hermes install actually includes it:

```bash
hermes version
hermes kanban --help
hermes plugins list
```

If `hermes kanban` is unavailable or the kanban plugin is absent, the local Hermes install is behind or missing the plugin; plan an update/install step before designing automation around it. A Kanban bridge should enforce reciprocal links among Kanban tasks, GitHub Issues/PRs, and Todoist tasks, and produce drift reports for orphaned items.

### 3. Resolve urgent small blockers first, then still deliver one story
Each slot may first resolve urgent small work if it is quick and obvious:
- bugs
- tasks
- well-understood tech debt
- stale worktree noise
- simple blocker removal

But that never replaces the story requirement.

Correct standard:
1. resolve urgent small work fast
2. still choose one story
3. still leave one story-sized deliverable

## Team composition

Default autonomous team:
- **Cron agent / PM-COO orchestrator** — selects story, routes work, checks SSOT, verifies truth, updates state, reports
- **Claude Code** — primary builder for code stories
- **Codex** — adversarial QA, verifier, secondary builder when useful

Recommended rule:
- The cron agent is **not** the default implementer for code work.
- The cron agent is the trigger, coordinator, verifier, and closer.
- Claude Code should handle most code implementation when available.
- If Claude Code exits non-zero, exits 143/terminated, produces an empty output file, is logged out in the cron context, or fails to produce a verifiable artifact within the slot budget, fall back to Codex CLI in the same worktree instead of stopping.
- Codex should pressure-test outputs, sanity-check assumptions, perform adversarial verification, and act as the implementation/recovery fallback when Claude Code is unavailable or inconclusive.

## Scheduling pattern

### Default daily cadence
A strong cadence looks like:
- 06:00 plan
- 08:00 groom
- 09:00 define
- 11:00 / 12:00 / 14:00 / 16:00 / 18:00 dev-or-integration slots
- 20:00 integrate / deploy prep
- 21:00 postmortem

Adapt the exact grid to the project, but keep role clarity.

### Slot types

#### 21:00 postmortem / day-close slots

Day-close must re-check external truth before leaving review tasks open or closing them:
- A task that was `NEEDS_TYLER_REVIEW` earlier in the day may have been merged or resolved outside the current cron context. Before carrying it as blocked, re-check the real PR/branch state with `gh pr view <PR> --json state,mergeCommit,mergedAt,url`, confirm the merge commit is on `origin/main` when relevant, and run a bounded read-only production smoke for affected public health/page URLs if the project auto-deploys from `main`.
- If the change is already shipped and safe to classify as done, update the Todoist task description/comment to `DONE` with the PR URL, merge commit, smoke result, and evidence path, then close it with `todo done TASK_ID`. Todoist raw updates/comments can still repair a completed task afterward if the first close left stale `NEEDS_TYLER_REVIEW` text.
- If the smoke is only read-only HTTP health/page proof, state that explicitly. Do not convert it into full Playwright/browser acceptance evidence.

#### PM / checkpoint slots
Allowed outputs:
- one story rerouted
- one blocker decision
- one go/no-go gate
- one explicit story split
- one deploy-readiness decision

#### Dev / integration slots
Allowed outputs:
- one code story launched and verified
- one QA artifact for one story, only when it includes the actual QA the slot could run itself
- one deploy-prep artifact for one story, only when the slot has classified deploy blockers it can classify itself
- one GTM/support package tied to one story

Dev/integration slots must not treat `READY_FOR_QA` as a comfortable stopping point. The cron operator owns the QA it can run inside the slot: targeted tests, Codex adversarial review, git diff inspection, browser/Playwright-equivalent route checks, console/network checks, and static asset probes when relevant. `READY_FOR_QA` is acceptable only when the remaining QA is blocked by a concrete external surface such as no public staging URL, unavailable non-production write target, auth/credential failure, missing data fixture, or explicit Tyler taste decision. In that case the slot must name the exact blocker and list the QA already completed; vague "next QA lane" handoffs are failure.

When a dev+GTM slot is blocked before code can begin because GitHub/Claude/Codex credentials are unavailable, it still needs a story-sized closeout. Use this pattern:
- select the scheduled story only after inspecting the complete Todoist task object and comments;
- prove the credential blocker with exact command exits for GitHub remote truth, Claude Code auth, and Codex/verifier auth;
- run only safe deterministic/read-only probes that do not imply implementation readiness, and explicitly caveat them as environment or baseline evidence;
- write a blocker packet for that story under `ops/handoffs/YYYYMMDD-HHMM-story-NNN-...md` with `BLOCKED_ENV` plus blocker type `ENV_FAILURE` in the Gate section;
- if the slot includes GTM/customer-growth responsibility, create a draft-only GTM artifact tied to the same story or today's sprint, clearly stating no external email/DM/promo was sent and what approval is required before use;
- update the story task and close the bot-only slot task after the artifact exists;
- rewrite state so the next slot does not relaunch the same story unless the exact auth conditions changed.

If Codex and Claude CLI are both unavailable before work but an independent sanity check is still useful, use a fresh `delegate_task` reviewer with the relevant facts pasted inline rather than local paths only. If that reviewer reports a protocol issue such as a missing blocker type, repair the packet/state/Todoist immediately and record the review as part of the evidence trail.

#### Definition / spec-hardening slots
Allowed outputs:
- one executable brief packet for the next dev slots, saved under `ops/handoffs/YYYYMMDD-HHMM-definition-briefs.md` or another durable repo-local handoff path
- repaired Todoist comments/descriptions on the affected story and slot tasks that point builders to the durable brief
- a rewritten `ops/core-loop-state.md` that names the selected stories, canonical statuses, owners, blockers, branch/worktree guidance, verification expectations, non-goals, and next gates
- explicit deferral or replacement of stories that cannot be made executable in the slot

Definition slots should not create duplicate top-level Todoist tasks when existing story/slot tasks already exist. Update/comment the existing tasks, then close the bot-only definition slot task if the brief and state rewrite are complete. The artifact is the hardened executable brief plus task-bus repair, not a generic planning note.

If grooming finds open GitHub PRs or branches that have no durable backlog story or Todoist task, do not route dev/review capacity to them as anonymous PR review work. First create or repair one durable story/task that explains the user/business outcome, acceptance criteria, non-goals, merge-order guidance, PR links, verification expectations, and deploy/migration risk. Only then reroute a blocked dev slot to that replacement story. This is especially useful when a blocked afternoon lane needs replacement work: the definition artifact is the backlog/Todoist routing object plus executable brief, not just a note that PRs exist.

### Klasificados multi-slot correction

For Klasificados specifically, do not collapse the system into a single every-two-hours generic core loop. Tyler expects an explicit autonomous product/design/dev/marketing team cadence with separate cron jobs: 06:00 plan, 08:00 grooming, 09:00 definition/spec, six dev/integration slots (11/12/14/16/18/20), 15:30 checkpoint, and 21:00 postmortem. The grooming slot's purpose is to prevent waste: it must identify blocked stories, unblock them if possible, or mark/deprioritize them so dev slots only pick up actually-ready stories.

## Blocking pattern for builders

### Prefer Pattern B when slots have enough slack
If the cron interval is wide enough (for example 2 hours), the cleanest model is:
- cron selects story
- cron launches Claude Code
- cron waits for completion with a hard timeout
- cron launches Codex if needed
- cron waits for Codex too within remaining budget
- cron verifies reality via Todoist + git/worktree evidence
- cron reports completion truthfully

This avoids fake progress caused by asynchronous dispatch summaries.

Recommended builder budget:
- around **90 minutes** when cron spacing is 2 hours

Hermes orchestration note:
- if you launch Claude Code or Codex as a background process and then use `process(action="wait")`, Hermes may clamp each wait call to a short maximum (for example 60 seconds).
- For Pattern B, this still works — just loop with repeated short `wait` or `poll` calls until the process exits, rather than assuming one giant wait call will block for the full builder budget.
- Prefer redirecting builder output to a durable file inside the worktree (`ops/handoffs/...json` or `.md`) so the cron can verify artifacts after exit without relying on in-flight terminal output.
- Budget the orchestrator's tool-call limit, not just wall-clock time. If a background builder produces no stdout, an empty redirected output file, and no file/status changes after several wait cycles, stop waiting, record the builder as stalled/timed out, inspect partial artifacts, and either complete the smallest verification directly or reroute. Do not spend the whole cron on repeated empty waits.
- If both Claude Code and Codex implementation/recovery stall on a packaging or verification task, the orchestrator may complete the narrow deterministic artifact directly instead of launching another builder, as long as it records the builder failures, runs the required probes/tests itself, and still sends the result through adversarial verification. This is especially appropriate for QA packets, root-branch diff checks, test transcripts, and state/Todoist repair work.
- If a verifier blocks only because the required packet or evidence artifact is missing, repair the packet/artifact directly and re-verify the final surfaces instead of treating the verifier output as the final slot result. The verifier failure should become part of the evidence trail, not the stopping point, when the missing item is safely repairable within the slot.

### Pattern B rules
For code stories:
1. launch Claude Code with a complete builder brief
2. **pin the model explicitly** for builder runs — do not rely on Claude Code defaults
3. block and wait up to the configured timeout
4. if useful, run Codex afterward and wait too
5. verify outcome before reporting
6. if builder timed out or failed, say so plainly

Recommended Claude Code builder invocation pattern:
- `claude -p --model claude-opus-4-7 --permission-mode bypassPermissions --output-format json`
- Do **not** set an arbitrary `--max-turns` cap for the main builder pass unless you are intentionally running a tiny diagnostic or planning probe.
- If your local CLI supports an explicit 1M-context Opus selector, standardize on that exact string and test it directly before putting it into cron prompts.

Why this matters:
- leaving `--model` unspecified can silently route a run onto Haiku or another cheaper default path
- a turn cap like `--max-turns 40` can kill a productive run for the wrong reason; for story execution the real control surface should be elapsed-time timeout, not an arbitrary turn budget
- the JSON result may still show a small Haiku entry in `modelUsage` even when Opus is pinned; treat the pinned Opus entry as the primary builder model and the small Haiku entry as internal auxiliary usage unless proven otherwise

Never report a story as completed just because a builder was launched.

## Productivity resilience: dirty branches and collisions

Cron systems must assume the repo may be messy.

Required behavior:
1. inspect current branch
2. inspect dirty files
3. inspect active worktrees
4. inspect recent commits / likely active agent ownership
5. if the active surface is dirty or claimed, do **not** stall

Recovery options:
- branch / worktree cleanly from `main` for a non-colliding story-sized unit
- take a different unaffected story surface
- produce a non-code story artifact if code ownership is blocked

Prunable/stale worktree recovery:
- A path listed in old state/Todoist can still exist on disk but no longer be a git repository after `git worktree prune`, temp cleanup, or partial deletion. Do not treat path existence as enough.
- Verify with `git -C <worktree> status --short --branch` before launching a builder. If it returns `fatal: not a git repository`, recover from the root repo: `git fetch origin --prune`, `git worktree prune`, verify the remote branch with `git ls-remote --heads origin <branch>`, then create a fresh deterministic worktree from the remote branch or clean base.
- Record the old path as invalid in `ops/core-loop-state.md` and Todoist so the next cron does not reuse it.
- Preload any same-day `ops/handoffs/` brief from the dirty/root checkout into the fresh worktree before launching Claude/Codex, and commit/push any new evidence packet if it is the review surface.

Do not let a dirty branch or stale worktree collapse the slot into commentary.

## Required verification surfaces

Before claiming success on a story, verify against real surfaces:
- `todo overview`
- `todo list --project <Project> --limit 50`
- `git status --short`
- `git log --oneline -5`
- `git worktree list`
- any story-specific artifact file or evidence path

For code stories, also verify:
- branch/worktree exists where expected
- changed files or commits are present if success is claimed
- Todoist reflects owner / blocker / next gate
- the selected clean base actually contains the story brief / backlog spec / handoff files the builder prompt depends on
- the intended test gate can run in the chosen environment (for example: project deps available, `uv`/venv usable, cache writable, no hidden network dependency for basic test startup)

### Preflight before spending a builder slot

Do this before launching Claude Code or Codex on a code story:
1. confirm the story markdown/spec exists on the branch or worktree the builder will actually see
2. confirm any handoff artifact referenced in the prompt is either committed on that base or copied into the clean worktree first
3. run a minimal test/runtime environment check with the smallest useful command, for example:
   - `python -c "import ..."` for ambient env
   - or `PYTHONPATH=src UV_CACHE_DIR=/tmp/uv-cache uv run python -c "import ..."` when `uv` is the real project path and the repo uses `src/` layout
4. if the clean worktree is missing critical story docs that only exist on a dirty root surface, do **not** burn the builder slot yet; first reconcile the docs onto the clean base or preload them explicitly
5. if the test gate needs network/downloads and the sandbox is restricted, mark that as a real blocker and route around it before launching the main builder

Preload and import-path pitfalls:
- If the root checkout contains fresh `ops/` state/handoffs or backlog specs that are absent from the story worktree, copy only the required files into the worktree before launching the builder, and record that in the artifact. After the slot, copy any new worktree-local `ops/handoffs/` or `ops/qa/` artifacts back to the durable root `ops/` surface if they are not committed, so fresh cron sessions can see them.
- In Python `src/` layout repos with multiple active worktrees, bare `pytest` or `uv run pytest` can import the package from a different checkout through ambient environment/PYTHONPATH leakage. Use `PYTHONPATH=src` for targeted probes, and treat a bare-pytest mismatch as an environment/probe hazard until reproduced with the intended import path.

This preflight exists to prevent a common waste pattern: the builder burns a full slot on missing local story files or an unready env, produces no diff, and leaves only an expensive timeout.

## Required artifact types

A cron slot must leave one of these, tied to one story:
- Claude Code implementation result
- Codex QA result
- staging review packet
- acceptance checklist with evidence
- blocker-removal handoff
- deploy-prep package
- GTM/support package tied to the story
- explicit story reroute / split / go-no-go packet

Not enough by itself:
- repo inspection
- backlog review
- worktree creation only
- handoff stub with no next gate

## Reporting hygiene for builder runs

PHONE_DIGEST_V1: Tyler-facing Telegram cron reports must use Tyler's compressed format exactly for status/deploy/review reports: one line per item, decision/action first, one raw visible URL only if useful, omit everything else unless Tyler asks.

Approval blockers are never useful as naked task/story IDs. If the report says Tyler deploy approval, review, taste call, route/data decision, or any other Tyler action is needed, the same item must include:
- the direct Todoist/GitHub/staging/review URL needed for that action, and
- a short note saying where test instructions/evidence live.

If no review URL or test instructions exist yet, do not ask Tyler to approve. Say `No review link/test packet exists yet` and create or repair the packet first.

Example:

```text
168: ready, needs deploy approval. Test instructions: Todoist task description/comments.
https://github.com/tymrtn/klasificados/compare/main...nagaklas/story-168-contact-volume-dashboard

308: not ready. Visual/spec mismatch. No action from you.
```

Bad:

```text
168 still needs Tyler deploy approval.
```

Put detailed logs, probes, test transcripts, caveats, and branch archaeology into Todoist comments, GitHub issues/PRs, or repo artifacts, not Telegram.

Do not surface raw builder telemetry as executive reporting unless it is directly useful.

By default, omit:
- raw `num_turns`
- raw `max-turns` settings
- `total_cost_usd` when the builder is running on a subscription workflow rather than a directly metered API workflow

Instead report in plain operator language first, with developer terms only as supporting evidence:
- story chosen
- user/business outcome
- builder and verifier used
- completion status: completed / timed out / failed
- what has actually been proven
- what has not yet been proven, stated in normal English
- next gate
- supporting evidence: visible raw URLs for Tyler-facing Telegram links, not hidden Markdown links; include only the one or two links needed for the decision. Local branch/worktree/commit paths, staging/prod URLs, changed files, artifact paths, and screenshots or `MEDIA:` attachments are supporting evidence, not the main report.

For Telegram, do not hide destinations behind Markdown link text. Tyler wants raw visible URLs so the target is inspectable before tapping. Prefer:

```text
Compare: https://github.com/owner/repo/compare/main...branch
Todoist: https://app.todoist.com/app/task/TASK_ID
```

Avoid:

```markdown
[compare](https://github.com/owner/repo/compare/main...branch)
[Todoist task](https://app.todoist.com/app/task/TASK_ID)
```

If a branch is not pushed, say "no GitHub link exists yet" and either push a review branch or do not ask Tyler to review it.

Avoid unexplained jargon in Tyler-facing Todoist titles/descriptions and Telegram summaries. For example, write "we have not yet opened the real or staging page in a browser using real data to confirm the image appears." Technical shorthand should not be used in Tyler-facing summaries.

If you must include debug telemetry or developer shorthand, put it in a secondary debug appendix rather than the main summary.

Every story worked in a cron slot should end with Todoist reflecting reality in a usable task shape.

Todoist task-title and section rules:
- Use Todoist project sections when available: `Stories` for user-meaningful product/story work, `Bugs` for regressions/incidents/user-reported broken behavior, and `Tasks` for operational chores/checkpoints/subtasks.
- Todoist uses P1/P2/P3/P4 only. Do not write P0 in Todoist titles/descriptions; translate repo `P0` to Todoist `P1` / urgent.
- Titles must be short, human-readable nouns/imperatives, not status reports.
- Do not prefix task titles with bot names, flags, timestamps, branch names, test counts, or full closeout summaries.
- A good title is `Story 320/321: package XPath parser integration for staging review`, not `Nagaklas: Klasificados Lane A - story-320/321 12:00 cleanup is locally QA-packaged...`.
- Status, evidence, links, test counts, branch names, and caveats belong in the task description or comments.
- If the item is only dev bookkeeping, label it `bot-only` and keep it terse; create a separate Tyler-facing `needs-approval` task only when Tyler has a clear decision to make.
- If a cron output is merely a cleanup/checkpoint/subtask, do not promote it into a noisy top-level Tyler task; attach it as a comment/description update to the parent story/task instead.

Each story task should show:
- story selected
- current owner
- blocker if any
- current status / gate
- next explicit move
- whether the story is currently claimed by an active cron/builder lane

Preferred pattern:
- builders update Todoist directly via `todo` / `todo raw`
- if they do not, the cron must do it before ending

A cron slot is not done until Todoist matches reality.

### Claim / lease discipline (critical)
A cron must not naively re-select a story that another recent cron already launched unless it is explicitly acting as the verifier or recovery pass for that same story.

Before choosing or acting on a task, inspect the complete Todoist task object, not just title text:
- `content` and `description`
- section/project
- due date, deadline, duration, recurrence if present
- priority (Todoist P1/P2/P3/P4 only; translate repo P0 to Todoist P1)
- labels, especially `blocker`, `needs-approval`, `bot-only`, `tyler-only`, domain labels, and owner labels
- parent/subtask relationship
- all comments via `todo raw GET /comments?task_id=TASK_ID`

Tyler comments and labels are authoritative Founder Oracle/task-bus input. If a comment or label conflicts with a stale title/description, honor the newest concrete user input and repair the task before routing it.

Use an explicit lease model in Todoist or the state file, for example:
- `claimed by 15:30 checkpoint`
- `builder running`
- `awaiting verifier`
- `retry after env fix`

Rules:
- if a story is already in `builder running`, the next dev slot should not relaunch the same builder blindly
- the next slot should either verify, unblock, or choose a different story
- only one active builder lane per story unless the slot is explicitly a controlled retry
- retries must cite the exact prior failure reason and what changed since the last run

## Long-run specificity contract

Autonomous cron systems must be designed for tens of thousands of cycles. Vague wording creates stale or ambiguous queue state.

Require these protocol surfaces in project-specific prompts/files:

1. **Canonical statuses** — define and reuse a fixed vocabulary such as `READY_FOR_SPEC`, `READY_FOR_DEV`, `CLAIMED`, `BUILDER_RUNNING`, `READY_FOR_QA`, `NEEDS_PLAYWRIGHT_VERIFICATION`, `READY_FOR_STAGING`, `STAGING_READY_FOR_REVIEW`, `NEEDS_TYLER_REVIEW`, `BLOCKED_ENV`, `BLOCKED_DEPENDENCY`, `DEPRIORITIZED`, `DONE`. Do not allow near-synonyms to drift across runs.
2. **Blocker taxonomy** — every blocker needs type, plain-English reason, owner, next action, and retry/deadline. Useful types: `TYLER_DECISION`, `TASTE_REVIEW`, `PLAYWRIGHT_VERIFICATION`, `PLAYWRIGHT_STAGING_WRITE_VERIFICATION`, `TEST_FAILURE`, `ENV_FAILURE`, `MERGE_COLLISION`, `DATA_RISK`, `DEPLOY_RISK`, `PAYMENT_RISK`, `LEGAL_RISK`, `SPEC_GAP`, `DEPENDENCY`, `UNKNOWN`. `UNKNOWN` lasts one cycle only.
3. **Playwright/browser verification definition** — never use opaque shorthand in user-facing reports. Spell it out: Playwright or equivalent browser automation opened the actual local/staging/prod route using the same data path users see, with exact URL/environment/timestamp, screenshots for UI claims, console/network errors checked where relevant, and Todoist/handoff updated. If the missing item is write-safety, say `PLAYWRIGHT_STAGING_WRITE_VERIFICATION` and identify the non-production database target needed. Playwright is the expected tool; do not report this as if browser access is missing. If there is no staging URL/real-data route, say that plainly.
4. **Ready gates** — define `READY_FOR_DEV`, `READY_FOR_STAGING`, `STAGING_READY_FOR_REVIEW`, and `DONE` gates. A pushed branch alone is not done. Staging review needs staging URL and complete evidence packet.
5. **Review packet schema** — gate, decision needed, Todoist link, code links, staging URL, prod URL, evidence, risk, next move. Never ask a human to review from a local path alone. If a verifier says a packet overstates readiness, repair the packet and downgrade the canonical status before closeout; do not bury the verifier failure as a caveat. If a verifier finds broken or missing evidence links, copy the referenced worktree-local evidence into the durable root ops surface when safe, patch the packet to state where the evidence now lives, and keep the verifier finding in the audit trail. If the verifier identifies a missing route/category/data slice, run the smallest missing browser/data check inside the same slot when safe, save both positive and negative screenshots/evidence, and let the negative evidence define the next blocker rather than deleting it. A small deterministic defect found during verification, such as a wrong visible label, may be fixed and committed in the same slot only if it is narrow, independently reviewed, and does not convert the story to a stronger gate than the remaining evidence supports. If the blocker depends on secret-bearing environment facts, such as `STAGING_DATABASE_URL` equaling `DATABASE_URL`, save a separate redacted proof artifact with presence flags and short hashes rather than only narrating the claim in prose; then cite that artifact in the packet before asking Codex to verify. Keep raw probe transcripts aligned with the packet examples: if a verifier says examples are stale or from a different sample, rewrite the packet and rerun verification. Classify successful tests plus unsafe dry-run output as `DATA_RISK`, not `TEST_FAILURE`; reserve `TEST_FAILURE` for an actual failing automated/manual test gate.
6. **Commit/link hygiene for review packets** — do not write self-referential final commit hashes into a packet before the final commit exists. Prefer stable wording such as "packet path on branch" plus latest branch commit in Todoist/final report, or add a follow-up metadata commit without rewriting history. Avoid `commit --amend` plus force-push in autonomous cron runs unless explicit approval exists; if metadata is wrong after a push, make a normal corrective commit. Before writing Todoist comments, state files, or final reports, verify the full commit SHA with `git rev-parse HEAD` and, if pushed, `git ls-remote --heads origin <branch>`; do not hand-copy or infer full SHAs from abbreviated log output. If an independent verifier returns PASS with a stale-reference caveat after the packet is committed, repair the packet with a clearly labeled post-packet commit correction, make a normal follow-up commit, push it, then use the corrected final SHA in Todoist/state/final reports. Treat the verifier caveat as a repair task, not as harmless debug text, when it can confuse the next cron slot about which tree is reviewable. After staging files, verify the `git add` exit code and inspect `git diff --cached --name-status` / `git diff --cached --stat` before every commit. A failed `git add` can still leave a partial index from earlier operations, especially around deleted duplicate backlog files or preloaded untracked specs. If a deletion path is ambiguous or already staged, prefer `git add -A <scope>` or `git rm --ignore-unmatch <path>` and then verify the full intended staged set. Do not commit a partial index just because one deletion is staged; if that happens, make a normal follow-up commit and use the final pushed SHA in Todoist/state/final reports.
7. **Remote evidence truthfulness** — clickable GitHub branch/compare links only prove the remote branch state. If the reviewed tree contains a local merge commit, untracked packet, or unpushed evidence, either push the review branch before asking for review or state that no GitHub link exists for that tree. Do not label a local-only merge as reviewable through a stale remote compare link.
8. **Cron closeout schema** — blockers first, slot result (`completed`, `advanced`, `rerouted`, `blocked`, `incident`, `no-op failure`), story/status, what changed, evidence, next action. If there was no artifact or explicit reroute, call it failure.
7. **Lease cleanup** — claims include owner/timestamp/branch/worktree/next handoff. Claims older than one dev slot without evidence must be continued, cleared, recovered, blocked, or deprioritized.
8. **Duplication guard** — before creating a task, search by story id, title keywords, branch, and backlog filename; update/comment existing tasks when possible.
9. **Drift repair** — a cron that sees protocol drift should repair the smallest safe surface immediately: title, section, status, blocker type, missing links, stale claim, or missing next gate.
10. **Literal language** — developer/operator output and protocol examples must use literal language. Do not use metaphors, idioms, jokes, slogans, nicknames, rhetorical labels, or technical shorthand that hides the required action or evidence. Use wording that names the concrete action, status, artifact, owner, environment, data source, or risk.

## Prompt design rules

A good cron prompt should say all of the following explicitly:
- this is a fresh session with no chat context
- read the four bootstrap files first
- Todoist is SSOT
- one slot = one story
- resolve urgent small blockers first, then still deliver one story
- inspect branch/worktree reality first
- inspect whether the candidate story is already claimed by a recent cron/builder lane
- if code story: launch Claude Code and wait
- run Codex when verification pressure is needed
- run the QA that is possible in the same slot; do not stop at `READY_FOR_QA` unless a concrete external blocker remains
- if Tyler has approved deployment contingent on no errors, classify any warnings yourself or create a narrow blocker with evidence; do not punt to a generic future deploy operator
- update Todoist before ending
- rewrite `ops/core-loop-state.md`
- inspection or commentary alone is failure

## Hardening against restarts and context loss

Cron systems fail when they depend on hidden session memory. Harden them like distributed systems.

### 1. Fresh-session-safe design
Assume every run starts from zero memory.

Every run must re-read:
1. project guidance
2. agent instructions
3. protocol file
4. rolling state file
5. Todoist SSOT

### 2. Durable state surfaces
### Durable state surfaces
- Todoist for same-day execution
- `ops/core-loop-state.md` for rolling handoff
- repo-local handoff artifacts under `ops/`
- git/worktrees for actual code truth

### Claim / lease discipline
Before selecting a story, inspect:
- Todoist owner / blocker / next gate
- `ops/core-loop-state.md`
- recent repo-local handoffs under `ops/`
- active worktrees / branches

Do not naively re-select a story another recent cron already launched. If a story is already effectively claimed (`builder running`, `awaiting verifier`, `retry after env fix`), the next slot must either:
- verify,
- unblock,
- or choose a different story.

Retries require an explicit reason and a change in conditions (for example: model pin fixed, bad turn cap removed, env issue resolved, missing docs restored). Re-running the same story without changed conditions is failure.

Do not rely on:
- in-chat promises
- unstated builder state
- implicit memory of what happened last slot

### 3. Idempotent reruns
A restarted cron should be able to rerun without corrupting the system.

Design prompts so that reruns:
- inspect current Todoist + git state first
- detect whether a story is already in progress, done, or blocked
- continue, verify, or close instead of duplicating work blindly

### 4. Explicit status model
Prefer explicit gates such as:
- queued
- in progress
- ready for QA
- verified
- staging-ready
- blocked
- timed out
- failed

This makes recovery after restart possible.

### 5. Deterministic naming
Use deterministic, inspectable names for artifacts where possible:
- branch names must be `bot_name/story-id-story-name`, for example `nagaklas/story-309-entity-hero-render-prod` or `claudedev/story-320-xpath-parser-integration`; avoid timestamp-first or opaque suffixes unless a collision requires a short suffix
- worktree directory names should mirror the branch/story enough to be obvious, for example `/tmp/klasificados-story-309-entity-hero-render-prod`
- handoff filenames including date + story id
- Todoist tasks named by story and gate

This reduces ambiguity after interruption and makes GitHub/Telegram review links readable.

### 6. Timeout honesty
A timeout is not success.

If Claude Code or Codex times out:
- record timeout explicitly in Todoist/state
- verify partial outputs via git/worktree and artifacts
- inspect the actual diff, because a timed-out builder may still have edited files before producing an empty or missing JSON/report artifact
- rerun the smallest relevant tests/probes yourself before trusting or discarding partial work
- if a verifier finds a blocker after a timed-out follow-up, report the artifact as useful evidence but blocked for commit rather than successful
- if the timed-out builder left a useful partial diff, run the fallback builder in the same worktree with a prompt that names the partial files, asks it to finish only the story acceptance criteria, and requires a durable result packet; then run independent review before committing
- set the next gate clearly
- do not call the story completed

Builder orchestration pitfall:
- Do not wrap a long Claude Code builder in `execute_code` with a timeout and rely on the Python wrapper to write stdout after completion. If the outer script times out, Hermes can kill the wrapper before it writes the redirected artifact, leaving no builder output file. Prefer `terminal(background=true)` or a direct shell command that redirects stdout/stderr to a worktree-local artifact before the process starts, then poll/wait and inspect the artifact plus git status. If this pitfall already happened, treat it as `timed out with no artifact`, inspect the worktree for partial changes, and immediately run Codex fallback or a narrow deterministic verifier.

### 6.5 Browser evidence for static/front-end routes

For front-end stories that involve static pages or `/static/...` assets, do not rely on `file://` browser verification when the page uses absolute asset paths such as `/static/js/...`. A `file://` URL may silently load the wrong paths or fail to execute the intended script. Start a local HTTP server from the correct web root and verify the actual route over `http://127.0.0.1:<port>/...`.

Recommended pattern:
1. Run the smallest local server from the story worktree, for example `python3 -m http.server <port> --directory landing`.
2. Navigate to `http://127.0.0.1:<port>/<page>.html`, not `file://...`, so absolute `/static/...` references resolve correctly.
3. Seed only non-production local state needed for the flow, such as a test seller token in `localStorage`; do not submit real listings or write production data.
4. Capture browser evidence for the exact AC states, console errors, failed network/write requests, and screenshot paths under `ops/qa/story-NNN/`.
5. If visual content is below the fold, use DOM inspection plus screenshots after scrolling/zooming; do not trust a cropped screenshot that hides the asserted fields.
6. Treat `browser_vision` screenshots as viewport evidence only, not full-page proof. The visual model can report that an element is absent when it exists below the current viewport or after a scripted scroll state was not captured. For UI ACs, combine: accessibility snapshot, DOM inspection (`document.querySelector(...).innerText/getBoundingClientRect()`), explicit `scrollIntoView({block:"center"})`, and then a screenshot that visibly includes the asserted element. If DOM evidence and a screenshot disagree, record the disagreement and use Playwright/full-page screenshots or targeted screenshot generation before claiming visual readiness.
7. If Playwright is not installed in the worktree, a cron may install it locally for the verification artifact, for example `npm install --no-save playwright@<version>` followed by `npx playwright install chromium`; remove `node_modules` and package-lock/package changes afterward unless the project intentionally tracks them. If `npx playwright node` is unavailable or reports `unknown command 'node'`, use a temporary package prefix under the evidence directory (`npm install --prefix ops/qa/story-NNN/<run>/pw-node --no-save playwright@<version>`) and run a normal `node` script that imports Playwright from that prefix. If Playwright installs `chromium-<rev>` but still complains that `chromium_headless_shell-<rev>` is missing, launch with `executablePath` pointed at the cached Chrome for Testing binary (for example on macOS: `~/Library/Caches/ms-playwright/chromium-<rev>/chrome-mac-arm64/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing`). Record this workaround in the evidence packet and remove the temporary prefix directory afterward. When intercepting browser requests, match API paths precisely with `new URL(request.url()).pathname` rather than broad substring checks such as `url.includes('/alerts')`; broad checks can accidentally intercept `/static/js/alerts.js`, prevent the application script from loading, and cause false negatives like missing `window.KlasificadosAvisos`. If the UI schedules focus with `setTimeout`, wait briefly after opening the modal before measuring focus-trap behavior; otherwise the probe can race the intended focus call. If verifying stacked PRs as separate cached branches, state that the evidence does not prove a combined merge tree until a merge-order branch is built and tested.
8. Treat write-safety evidence as path-specific. A text-only preview probe with no write requests does not prove photo-upload safety if selecting a file triggers `/upload`, `/listings`, `/payments`, or `/api` requests. State exactly which path was exercised and whether uploads were mocked, local-only, expected, or absent.
8. Compare evidence against the literal backlog AC, not only the latest definition brief. If the backlog says a UI element must be below the form or visible on step 3 mobile, a step 2 screenshot is useful evidence but not proof of that AC.
9. If browser evidence contains a console/resource error, even a known local placeholder such as an unresolved SDK script, do not claim a clean console pass. Classify it as blocking or non-blocking with the exact URL/source and reason.
10. If Codex or another adversarial verifier finds that a QA packet overstates readiness, rewrite the packet to the downgraded gate, commit/push the blocker evidence if useful, update Todoist/state, and report `NEEDS_PLAYWRIGHT_VERIFICATION` or the exact blocker instead of asking for staging review.
11. If the browser evidence is local-only, report it as local browser proof, not staging proof.
12. For front-end filter/search bugs where the available local data fixture is absent or intentionally empty, separate request-construction proof from result-count proof. It is acceptable to monkeypatch the browser-side request boundary (for example `callMcpTool` or `window.fetch`) to capture the exact payload generated by the real UI state, as long as the packet states that this proves only browser request construction, not backend result quality. Record the route, selected UI state, captured payload, data-source limitation, console/resource errors, and screenshot under `ops/qa/...`; pair this with deterministic unit tests for backend handler/query behavior before claiming readiness. For Klasificados alert/favorites flows, do not infer localStorage key names from source snippets that may be redacted or transformed in tool output. Instrument `Storage.prototype.getItem` or inspect the runtime flow to discover the actual key, seed only a fake local token, and intercept the write boundary before any `/alerts`, `/favorites`, `/listings`, `/payments`, or other API write can leave the browser. A passing packet for this pattern must explicitly say: local browser request-construction proof only; no real API write; not staging/database persistence; not email delivery.
13. For UI/route stories whose AC depends on real listing/entity data, a local app health check is not enough. If the app starts but logs database connection failures, falls back to stub data, or the required routes return `404 Listing not found` / `500 Internal Server Error`, classify the story as `BLOCKED_ENV` plus `NEEDS_PLAYWRIGHT_VERIFICATION`, even when targeted unit tests pass. Save the negative screenshots, HTTP status/body snippets, and server log under `ops/qa/story-NNN/`, write a blocker packet, and do not ask for visual/staging review until a staging URL or read-only database target exercises the same data path users see. Production route `200` responses can be useful comparison evidence, but they do not verify an unmerged branch.
### 6.6 API/admin route smoke and write-safety evidence

For API/admin stories, do not assume a read-only endpoint is write-safe just because the handler's SQL is read-only. FastAPI/Starlette middleware, app lifespan hooks, and lazy schedulers can run before the endpoint handler and may start background write jobs.

Recommended pattern:
1. Smoke the exact route with the app's real auth surface. For Klasificados admin routes, the shared `verify_admin` dependency currently expects `?secret=<ADMIN_SECRET>` unless the route says otherwise; do not assume `x-admin-secret` works.
2. Capture logs around the request and search for scheduler/write signals such as `Lazy trigger:`, `Scheduled task:`, `phone backfill`, `enrichment`, `scrape`, `INSERT`, `UPDATE`, or background task startup.
3. If a read-only/admin QA route triggers background write work, classify it as `DEPLOY_RISK`, not a passed smoke. Implement the smallest route-specific skip or guard, then test both the skipped route and normal routes that must still trigger the scheduler.
4. Cover canonical and redirect/trailing-slash variants when middleware behavior matters, because middleware sees `request.url.path` before routing/redirect completion.
5. Save redacted proof under `ops/qa/story-NNN/`: route, environment, timestamp, auth/DB presence flags, short hashes for secret-bearing DB targets, status code, response summary, scheduler log excerpt, and side-effect classification.
6. Treat a Codex model/CLI compatibility error during verifier setup as tool unavailable, not as a code verdict. Record the exact error and use deterministic tests plus an independent reviewer fallback on the actual diff and evidence.

### 6.7 Cross-story test-contract blocker repair

When one story's test-contract drift blocks another story's full-suite gate, treat the blocker as a narrow integration artifact rather than relaunching the blocked story blindly.

Recommended pattern:
1. Identify the blocking tests by exact file and test name, and determine whether they describe desired behavior or stale expectations.
2. Repair or explicitly reclassify the contract in the owning story branch, not in the dependent story branch, unless the dependent branch owns the tested behavior.
3. Run the smallest targeted test gate on the owning branch first.
4. Run static/diff review and an independent reviewer on the contract change, especially when changing tests rather than production code.
5. Commit and push the owning branch so the reviewable remote evidence matches the claimed tree.
6. Merge the pushed owning branch into a local diagnostic integration branch with the dependent story branch, then rerun the dependent full-suite gate.
7. If the diagnostic branch is local-only, say so clearly. Do not ask for review through a stale remote compare link for the local merge tree.
8. Copy the diagnostic transcript into a durable `ops/qa/story-NNN/...` path on a pushed review branch or otherwise make it visible to fresh cron sessions.
9. Update both Todoist tasks: the owning story gets the repaired contract status and any remaining blockers; the dependent story gets whether its prior `TEST_FAILURE`/`DEPENDENCY` blocker is cleared.

Use this for cases like Klasificados Story 115 being blocked by Story 320/321 scraper HTML simplifier/XPath test drift: the successful artifact is not only the test edit, but the dependent full-suite transcript after a local diagnostic merge, plus truthful remote/local evidence labels.

### 7. Prefer repo-local durable artifacts over `/tmp` for anything important
`/tmp` is acceptable for scratch experiments and disposable prototypes.
If the artifact matters for future slots, put it somewhere durable and inspectable in or near the repo, for example:
- `ops/handoffs/`
- `ops/reviews/`
- `ops/qa/`

Keep `ops/` limited to runtime operating artifacts needed by fresh autonomous sessions. It is not a general documentation directory. Durable product specs belong in the backlog/Todoist, engineering docs in `docs/`, design docs in `docs/design/`, and process doctrine in project guidance or skills. Every `ops/` artifact should have a story id or clear operating purpose, and stale artifacts should be identified during the daily closeout.

### 8. State rewrite is mandatory
A slot is incomplete until `ops/core-loop-state.md` is rewritten with:
- last updated
- current assessment
- what changed this loop
- work completed this loop
- active priorities / next gate
- blockers

### 9. Cron filesystem/tool fallback
Cron runs may start with a stale or case-sensitive working-directory mismatch, especially on macOS paths such as `~/Dropbox/code/klasificados` vs `~/Dropbox/Code/klasificados`. If `terminal`, `read_file`, or `search_files` fail with `FileNotFoundError` for the expected project root, do not stop or report failure immediately.

Recovery pattern:
1. Use `execute_code` with `Path.home() / "Dropbox/code/klasificados"` and `Path.exists()` to confirm the real path.
2. Run required boot commands from Python `subprocess.run(..., cwd=root)` when the terminal wrapper is stuck on a bad default cwd.
3. If `terminal()` still fails with the stale `~/Dropbox/code/klasificados` path even when an absolute workdir is supplied, assume the terminal wrapper session is poisoned for this run. Also treat repeated `terminal()` results with `exit_code=-1` and empty output, including when called from inside `execute_code`, as a poisoned wrapper rather than as evidence that the underlying command failed. Use `execute_code` + Python `subprocess.run(..., cwd=<actual root or worktree>)` for git, tests, Todoist, Claude/Codex one-shots, commits, pushes, and verification commands until terminal works again.
4. For local web/browser proof while terminal is poisoned, start the server from `execute_code` with `subprocess.Popen(..., cwd=<actual worktree>, start_new_session=True)`, write logs to `ops/qa/story-NNN/...`, poll a health URL from Python, then use browser tools against `http://127.0.0.1:<port>/...`. Kill the server with a bounded `pkill`/process cleanup before closeout.
5. Read and write required repo files with Python `Path.read_text()` / `Path.write_text()` if the file tools inherit the bad cwd.
6. Continue to produce the required durable artifact, Todoist updates, and state rewrite.
7. In the final report, mention only if the fallback affected confidence or left a blocker; otherwise treat it as routine cron resilience.

Verifier fallback pattern:
- Subagents can inherit the same stale working-directory problem. If `delegate_task` returns a path/access failure instead of a substantive review, do not treat that as a code or QA verdict.
- Retry verification from the parent session using `execute_code` + Python `subprocess.run(..., cwd=<actual worktree>)` for deterministic checks, and if an independent LLM verdict is still needed, invoke Claude Code directly from that same Python subprocess with explicit `--model`, `--permission-mode`, and a durable output artifact.
- If Codex is unavailable because the configured model/account/CLI version fails before work, record the exact failure and use Claude Code verifier fallback plus deterministic probes rather than stopping or claiming Codex PASS.
- If both Codex and Claude CLI fail before work because of authentication (`401 Unauthorized`, `Missing bearer`, `Not logged in`, or equivalent), do not keep retrying the same tools. Record both failures as verifier-tool unavailable, then use deterministic evidence plus `delegate_task` with the relevant evidence pasted inline for an independent verdict. Because the subagent may not have repo access, include the commands/results, route statuses, screenshots/log summaries, and acceptance criteria in the prompt rather than local paths only.
- Clean up verifier-generated drift before closeout. For example, if running a digest/probe rewrites an ops artifact with a new timestamp or live endpoint result, either commit that intentionally or revert it before pushing the review branch.

Credential/task-bus fallback pattern:
- If `todo overview` or `todo list` fails because `TODOIST_API_KEY` is missing, do not invent Todoist state or create duplicate tasks elsewhere. Use the latest `ops/core-loop-state.md`, same-day definition brief, and durable backlog files as fallback routing inputs; explicitly state that Todoist comments could not be inspected; write the packet/state update with a next action to repair Todoist credential visibility.
- If `gh` is logged out or `git fetch` / `git ls-remote` fails with an authentication prompt, do not claim fresh remote truth, open a PR, or ask for review through a stale compare link. Use cached remote-tracking branches only for local tests, label the branch evidence as cached/local, and record GitHub auth as `ENV_FAILURE` with the exact command failure.

This fallback is for deterministic repo/Todoist/state work and bounded verifier calls. For long-running servers, builds, or interactive agents, prefer normal `terminal`/process tools once a valid workdir is available.

## Common failure modes and fixes

### Failure: no story-sized artifact
Symptoms:
- lots of reading
- lots of reviewing
- no artifact
- no story selected

Fix:
- enforce one slot = one story
- require artifact + Todoist update + next gate
- treat inspection alone as failure

### Failure: fake progress via async builders
Symptoms:
- cron launches builder
- immediately reports success
- no verified output yet

Fix:
- use blocking pattern with timeout when slot spacing allows
- verify via Todoist + git/worktree before reporting completion

### Failure: dirty branch paralysis
Symptoms:
- cron only comments on repo mess
- avoids all progress

Fix:
- require productivity resilience
- branch/worktree from `main` or choose unaffected surface

### Failure: context loss after restart
Symptoms:
- duplicated work
- forgotten story state
- stale next steps

Fix:
- re-read protocol/state/Todoist every run
- use explicit status gates
- store durable handoffs in repo

## Minimal good outcome
A good autonomous cron system does this every slot:
1. reads durable context
2. consults SSOT
3. resolves urgent small blockers quickly
4. chooses one story
5. launches or completes the right work
6. verifies truthfully
7. updates Todoist
8. rewrites shared state
9. reports only what really happened

## Best concise operator mantra
**Todoist is SSOT. One slot = one story. Resolve urgent small blockers first, then still deliver one story. For code stories, launch Claude Code, wait, verify with Todoist + git, and only then report.**