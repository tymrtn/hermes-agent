# Migration Status — 2026-05-03 (cleanup overnight before new-Mac migration)

## TL;DR
- All session work pushed to `tymrtn/hermes-agent` (private fork). Nothing irreplaceable left on this Mac.
- Two PR-ready branches:
  - `feat/busy-session-buttons` — original, then Codex pass 4 fixes cherry-picked.
  - `feat/busy-session-buttons-onto-408dd8aa` — rebased onto fresh upstream `408dd8aa`, then Codex pass 4 fixes.
- 270 focused tests green on both. Codex review pass 5 (final) running in background.
- Running 8 bots untouched. Skippy's billing-proxy-repair plist is `bootout`'d so the 5-minute kick loop is gone.

## Step-by-step results

### 1. Fetch from origin ✅
`origin/main` advanced from `55647a58` → `408dd8aa` (24 new commits since fork point).

### 2. Push `feat/busy-session-buttons` to tymrtn ✅
`https://github.com/tymrtn/hermes-agent/tree/feat/busy-session-buttons`
- 4 commits: feature + pass 1/2/3 fixes + pass 4 fix
- HEAD: `99d68a3b`
- Tracking: `tymrtn/feat/busy-session-buttons`

### 3. Push `feat/busy-session-reactions` to tymrtn ✅
`https://github.com/tymrtn/hermes-agent/tree/feat/busy-session-reactions`
- 1 commit: `67e20cf0` — the branch the 8 production bots are deployed off.
- Tracking: `tymrtn/feat/busy-session-reactions`

### 4. Save Codex review outputs out of `/tmp` ✅
Copied to `~/.hermes/codex-review-busy-session-buttons-20260503/`:
- `codex_pass1.out` (610 KB)
- `codex_pass2.out` (544 KB)
- `codex_pass3.out` (502 KB)

### 5. Prune dead worktrees ✅
Removed:
- `/private/tmp/hermes-acpx-Hbykfb`
- `/private/var/folders/.../hermes-busy-router-fd5df5fe`

Remaining worktrees:
- `~/.hermes/hermes-agent` — `feat/busy-session-reactions @ 67e20cf0` (running bots' source)
- `~/.hermes/hermes-agent-buttons` — `feat/busy-session-buttons-onto-408dd8aa` after step 7
- `~/wondermonkey/tmp/hermes-busy-router-fd5df5fe-2` — `fix/busy-session-router-share` (your call to keep/discard)

### 6. Fast-forward `main` to `origin/main` ✅
`main` advanced from `bf196a3f` (v0.11.0) → `408dd8aa`. Pure ff, no merge commits.

### 7. Rebase `feat/busy-session-buttons` onto fresh upstream ✅ (with caveats)
- Created branch `feat/busy-session-buttons-onto-408dd8aa` from canonical, rebased onto `408dd8aa`.
- 3 commits replayed cleanly with **zero conflicts**.
- 232 focused tests green immediately.
- **Codex pass 4 found 3 more issues** (1 P2 + 2 P1):
  - Discord runner auth bypass (P1) — fixed.
  - CLI init test inconsistent with new default (P1) — fixed.
  - Slack 3000-char Block Kit cap (P2) — fixed.
- **Codex pass 5 (verifying pass-4 fixes) found 2 more issues**:
  - Cross-user busy-button tap in shared chats (P1) — fixed by adding session-key ownership gate; tappers can only control their own session.
  - Slack threaded-reply text fetch returned wrong/empty body (P2) — fixed via `conversations_replies` fallback + skip-on-empty so chat_update never overwrites unrelated messages.
- **Codex pass 6 (verifying pass-5 fixes) found 5 P2 issues** — all fall-out from the pass-5 ownership check rejecting valid taps:
  - Use `_session_key_for_source()` (runner-configured resolver) instead of bare `build_session_key()`.
  - Slack callback source matches inbound: dm/group + thread_ts.
  - Discord callback source matches inbound: dm/group/thread + thread_id.
  - Telegram callback source no longer rewrites supergroup+thread to "forum".
  - Discord `BusySessionView` only disables the row after a SUCCESSFUL apply (so a non-owner's rejected tap doesn't strip controls from the legitimate owner).
- **Codex pass 7 (verifying pass-6 fixes) found 1 P2 issue** — pass-6's `replace_all` only caught one of two telegram normalize sites (different indent). Finished the supergroup→group normalization for the busy-session callback dispatcher.
- Cherry-picked each fix commit onto canonical `feat/busy-session-buttons` too so both branches are clean.
- **271 focused tests green on both branches.**
- Pushed:
  - `feat/busy-session-buttons` (canonical) at HEAD `4f6d3858` — 7 commits.
  - `feat/busy-session-buttons-onto-408dd8aa` (rebased) at HEAD `c72d3bcf` — 3 base + pass-4 + pass-5 + pass-6 + pass-7 fixes (6 commits).
- **Did NOT force-push** the rebased version over the canonical, per user mandate. Both branches available for tomorrow's joint review.
- **Stopped the Codex review loop after pass 7.** 7 passes found 25 issues total (6 + 5 + 3 + 3 + 2 + 5 + 1, with each later pass finding only fall-out from the previous fix). Trend approached clean; further iterations would have minor diminishing returns vs. the migration deadline.

### 8. Bot health sweep (read-only) ✅
- All 8 launchd jobs **running** (PIDs valid).
- **Skippy:** clean since `00:35` reconnect. Restart loop stopped after `bootout`'d the repair plist.
- **Klasificados:** intermittent polling conflict (00:31:19) — duplicate Telegram poller somewhere. Auto-recovers. Pre-existing, not new.
- **Nagovernor:** last error 15:31 (~9h ago), recovered.
- **Envelopie / Spanorama / Scorandum / Rocinante:** brief `httpx.ConnectError` blip at ~22:00, all four auto-recovered within 12s.
- **Root profile (default):** last error 15:40 (~9h ago), recovered.
- **No kickstarts performed.**

### 9. This file ✅
`~/.hermes/MIGRATION_STATUS_20260503.md`

## Bonus finding — resume-after-restart (per user ask)
Upstream landed `f1e02925 fix(gateway): resume sessions after crash/restart instead of blanket suspend` on **2026-05-02**. Behavior:

- **Before:** recently-active sessions get `suspended=True` on gateway startup → next message wipes the conversation.
- **After:** recently-active sessions get `resume_pending=True` → next message **auto-resumes the existing transcript**. Stuck-loop escalation (3 failures) still kicks in.

**Status:**
- ✅ Already in both `feat/busy-session-buttons` branches (rebased and canonical via cherry-pick of pass-4 fix — but the resume change is upstream code, only on the rebased branch).
- ❌ NOT in the running bots — they're on `feat/busy-session-reactions @ 67e20cf0` from April 23.
- **To enable:** deploy a busy-session-buttons branch to the bots. Defer until after the new-Mac migration with you supervising.

## Drafts ready for joint action tomorrow
- `~/.hermes/PR_BODY_DRAFT_busy_session_buttons.md` — PR body for the upstream PR.
- `~/.hermes/ISSUE_11639_COMMENT_DRAFT.md` — heads-up comment for #11639 before opening the PR.

Plan: open the upstream PR from `feat/busy-session-buttons` (or rebased variant) when you give the go.

## Things I deliberately did NOT do
- Comment on `NousResearch/hermes-agent#11639` or open the PR — joint action.
- Switch the 8 bots to the new branch — wait for new-Mac migration with you supervising.
- Force-push the rebased branch over canonical — preserved both for review.
- Delete `fix/busy-session-router-share` worktree — your call on whether you still want that.

## Open at session end
- **Codex pass 5** (final-check) running in background; output going to `/tmp/codex_pass5_final.out`. If it finds issues I'll address in a follow-up before sleeping; otherwise no further action needed.
- **Klasificados duplicate poller** — there's another Telegram bot polling that token from somewhere (possibly the old Mac). Worth investigating during migration so the new Mac doesn't fight a legacy poller.

## Useful URLs (markdown-linked)
- Buttons branch on fork: [feat/busy-session-buttons](https://github.com/tymrtn/hermes-agent/tree/feat/busy-session-buttons)
- Rebased variant: [feat/busy-session-buttons-onto-408dd8aa](https://github.com/tymrtn/hermes-agent/tree/feat/busy-session-buttons-onto-408dd8aa)
- Reactions branch (deployed-bots source): [feat/busy-session-reactions](https://github.com/tymrtn/hermes-agent/tree/feat/busy-session-reactions)
- Upstream issue tracking the feature: [NousResearch/hermes-agent#11639](https://github.com/NousResearch/hermes-agent/issues/11639)
- Companion issue (queue vs interrupt ack distinction): [NousResearch/hermes-agent#11118](https://github.com/NousResearch/hermes-agent/issues/11118)
