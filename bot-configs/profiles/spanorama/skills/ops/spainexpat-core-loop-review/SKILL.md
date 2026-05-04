---
name: spainexpat-core-loop-review
description: Run SpainExpat Dorado Club autonomous launch/CRM reviews from Todoist, Envelope, live/repo evidence, and outreach docs. Todoist is the operational SSOT; repo handoff state is deprecated.
tags: [spainexpat, dorado, cron, todoist, envelope, crm, partner-outreach]
triggers:
  - "SpainExpat core loop"
  - "Dorado Club follow-up"
  - "partner outreach follow-up"
  - "SpainExpat Membership Todoist"
  - "partner one-pager"
  - "social reach credibility report"
---

# SpainExpat Core Loop Review

Use this when Spanorama is asked to run a SpainExpat / Dorado Club autonomous review, especially cron jobs involving partner outreach, CRM follow-up, launch state, partner collateral, analytics/traction research, or Todoist grooming.

Todoist project **SpainExpat Membership** is the operational SSOT. `ops/core-loop-state.md` is deprecated; do not read or rewrite it as loop state.

## Purpose

Keep SpainExpat Dorado Club launch work grounded in disk-backed state and the live task/mail surfaces. The operating model is communications, research, writing, design review, emailing, and CRM operations - not default coding work.

North star:
- launch SpainExpat Dorado Club within 2 weeks
- reach $10k MRR within 2 months

## Workflow

1. **Load source state**
   - From the SpainExpat repo root, read the repo operating model (`AGENTS.md`) and `ops/core-loop-protocol.md` before taking action.
   - Do **not** read or write `ops/core-loop-state.md`; it is deprecated even if older instructions mention it.
   - Read any relevant docs named in Todoist, usually under `docs/plans/`, `docs/outreach/`, or `docs/research/`.
   - Do not rely on prior chat context.

2. **Inspect Todoist project**
   - Project: `SpainExpat Membership`.
   - Focus on same-day/overdue launch tasks and these lanes:
     - partner outreach
     - anchor perks
     - waiting-on-partners follow-up
     - approval gates
     - partner collateral / one-pager
     - benefit stack / launch math
   - Always inspect task descriptions and relevant comments before reporting status.

3. **Inspect live mail only as needed**
   - Use Envelope via the profile-local `envelope` wrapper on PATH.
   - Verify accounts first with `envelope accounts list --json`.
   - For SpainExpat, check whether `editor@spainexpat.com` is actually present and live.
   - If `editor@spainexpat.com` is absent from `accounts list`, do not proceed as if this is just a Drafts/Sent read failure or credential-store error. Record the blocker as account restore/re-add needed, then update the mail migration, outreach, approval, and waiting-on-partners Todoist tasks with that current state.
   - If `accounts list` omits `editor@spainexpat.com` but direct commands like `envelope folders --account editor@spainexpat.com --json` return `credential store error: decryption error: aead::Error` rather than `account not found`, still classify the mailbox as unusable and needing credential/account restore. Do not infer that the account is healthy or that Drafts/Sent can be trusted.
   - If auditing outreach state, check Drafts, Sent Items, and targeted searches for partner names/subjects.
   - Useful commands:
     - `envelope folders --account editor@spainexpat.com --json`
     - `envelope draft list --account editor@spainexpat.com --json`
     - `envelope search --account editor@spainexpat.com --folder Drafts 'OR SUBJECT Dorado BODY Dorado' --limit 20 --json`
     - `envelope search --account editor@spainexpat.com --folder 'Sent Items' 'TO info@balcellsgroup.com' --limit 10 --json`
     - `envelope read --account editor@spainexpat.com --folder Drafts <UID> --json`
     - `envelope folders --account partners@spainexpat.com --json`
     - `envelope draft create --account partners@spainexpat.com --from partners@spainexpat.com --to <recipient> --cc ty@tmrtn.com --subject '<subject>' --body '<body>' --json`
     - `envelope read --account partners@spainexpat.com --folder Drafts <UID> --json`
     - `envelope search --account partners@spainexpat.com --folder Sent 'SUBJECT Dorado' --limit 10 --json`
   - Avoid piping untrusted email output directly into an interpreter; read/inspect with `head`, `jq`, or plain output instead.
   - Do not send email without Tyler approval.
   - Drafts are not sent outreach. Waiting-on-partners is only real after an outbound email/contact form was actually sent/submitted.

4. **Classify partner status honestly**
   - Strong enough to count toward launch math only if a partner has agreed to a concrete member advantage.
   - Drafts, collateral, generic interest, or ad/affiliate conversations do not count.
   - Concrete advantages include:
     - free or discounted first consult
     - priority intake / named Dorado response path
     - fixed-fee package
     - member-only review/compliance check
     - something clearly better than public access
   - Weak/noise includes:
     - generic affiliate links
     - ordinary ad-sales enquiries
     - public broker referrals
     - vague “happy to chat”
     - newsletter/content swaps
     - sweeteners that do not explain why Dorado is worth paying for

5. **Update operational surfaces**
   - Todoist is the operational SSOT. If you discover an important status change, update the relevant Todoist task description or add a concise Todoist comment with bot attribution, e.g. `🇪🇸 Spanorama: ...`.
   - Do not rewrite `ops/core-loop-state.md`; it is deprecated.
   - Use repo `docs/` for durable research/collateral/copy artifacts, then link those artifacts from Todoist.
   - For PDF/HTML partner collateral, verify the generated artifact before calling it review-ready: run `pdfinfo <file>` and `pdftotext <file> - | head`/`tail` to confirm page count and full extracted content. A tiny multi-page PDF with only a title/scraps is a failed artifact, not finished collateral. Back up malformed PDFs before rebuilding.
   - If a launch blocker is an approval gate, make it concrete: create or revise the server-side draft/collateral so Tyler can approve a specific UID/path rather than a vague task.
   - If you create a new server-side draft, immediately verify it with `envelope read ... --folder Drafts <UID> --json`, confirm it has not been sent by searching Sent, then update both the outreach task and approval/waiting-on-partners tasks.
   - Before ending, Todoist should reflect current gate/status, blocker if any, owner/lease if any, evidence link/path, and next explicit move.

6. **Report concisely**
   - Lead with the answer.
   - Include:
     - which partners need follow-up now
     - which offers count toward launch math
     - which are weak/noise
     - the single best next outreach move
   - If no actual update exists and the cron prompt allows suppression, return exactly `[SILENT]`.

## Story and blocker handling

- Do not turn missing in-scope work into a stop sign. If a partner collateral story is missing credibility metrics, research the metrics. If outreach lacks contact details, hunt contacts. If context is missing, inspect Todoist/docs/mail.
- Report `blocked` only when the remaining obstacle is outside the slot's authority/tools/time, such as missing credentials, required user approval, unavailable production access, or a genuinely external dependency.
- Keep human intent visible. A story like “Partner One Pager” means “make partners take Dorado seriously,” not “complete a checklist and stop at the first missing datum.”
- Prefer concise builder briefs: intent, boundaries, references, evidence requirements. Avoid over-specifying competent agents into brittle checklist-following.

## Credibility / traction research pattern

When building partner collateral or credibility reports:
- Use subagents for parallel research when useful: live/site footprint, social/community footprint, and local repo/database evidence.
- If Firecrawl or xitter/X CLI are requested, try them first and record tool status. If unavailable, do not stop; use public web/browser/local fallbacks and cite caveats.
- Distinguish first-party metrics, public estimates, historical claims, platform-visible social counts, and qualitative proof.
- Do not hallucinate numbers. Use source tables with value, source URL/path, date checked, and caveat/confidence.
- Feed usable proof bullets back into the relevant Todoist story, rather than leaving the result only in a local report.

## Trust / conversion refresh pattern

When the loop is about trust-refresh, homepage/top-pages, or analytics rather than partner outreach:
- Inspect the Todoist task plus comments first; older comments often contain reproduced live blockers that need re-verification, not rediscovery.
- Verify live behavior with both browser rendering and HTTP/body checks. Blank 200 responses are conversion blockers even when status is technically OK.
- For blank EE routes, do not stop at “missing entry” as a guess. Check the relevant production template read-only, then verify whether the expected `channel_titles`/`pages` entries exist and are open before proposing a fix. A route can have valid page entries and still return 0 bytes because a branch-specific tag/add-on stack fails (e.g. legacy `{exp:member:registration_form ...}`).
- If the expected entry exists, narrow the hypothesis to the branch/tag/add-on that differs from a nearby working route, then create an approval-ready repair plan with backup, diagnostic placeholder, fallback, verification, and rollback steps. Do not patch live templates without explicit approval.
- If a launch-gating conversion blocker is buried in comments or audit docs but lacks a dedicated approval task, create a concrete Todoist approval task in the approval lane with: verified live symptom, evidence/plan path, blocker classifications, owner/lease, safe default, and exactly one decision Tyler can make next.
- Check the live production template source read-only when needed to identify likely insertion/fix surfaces, but do not deploy/publish without explicit approval and rollback path.
- For SpainExpat Lightsail read-only SSH, use the real user key path if profile `~` resolves under Hermes: `-i /Users/wondermonkey/.ssh/LightsailDefaultKey-ca-central-1.pem`. The profile-local `~/.ssh/...` path may not exist. If host key verification blocks a read-only check in an autonomous run, use an isolated known-hosts file rather than modifying the user's default known_hosts, e.g. `-o StrictHostKeyChecking=no -o UserKnownHostsFile=/tmp/spainexpat_known_hosts`.
- Classify each blocker by type: approval, capability, technical, logistical, strategic, or product/conversion.
- Durable audit findings belong in `docs/audits/` and must be linked back into Todoist. Success is a verified Todoist update with evidence URL/path and next explicit move, not just a local note.
- For Umami checks, verify live HTML for `umami` and inspect likely global header/analytics includes. Existing Google Ads/gtag does not imply Umami is installed.

## Known SpainExpat/Dorado specifics

- `editor@spainexpat.com` may be on Amazon WorkMail. Do not assume mailbox absence from old state; verify live. If Envelope account inventory omits it or direct lookup returns `credential store error: decryption error: aead::Error`, report mailbox restore/re-auth as the blocker rather than calling partners “waiting.”
- Be precise about “waiting on partners”: drafts, blocked mail access, or Tyler approval are not partner waits. If Tyler is the blocker, put the concrete ask in his face with the exact decision, UID/path, or account needed.
- Prefer `partners@spainexpat.com` for first-contact partner outreach because it is understandable before Dorado is explained and survives product-name changes. Use `dorado@spainexpat.com` later for member-facing Dorado inquiries/support.
- For SpainExpat outreach, first emails may need to be created as server-side drafts for Tyler review, not sent.
- Preferred business-development mailbox direction is `partners@spainexpat.com` on Migadu. `dorado@spainexpat.com` may be useful later for member-facing Dorado operations. Do not keep fighting the legacy `editor@spainexpat.com` account for new partner outreach if `partners@` is ready.
  - If Migadu domain/mailboxes are reportedly created but `partners@spainexpat.com` and/or `editor@spainexpat.com` are absent from `envelope accounts list` or direct folder checks return `credential store error: decryption error: aead::Error`, treat Migadu admin setup and Envelope operational readiness as separate states. Create/update a concrete Todoist task to configure/re-auth `partners@` in Envelope before first partner outreach, while keeping `editor@` continuity/archive restore as a separate migration blocker.
- Mail readiness can change between runs. If `editor@spainexpat.com` becomes readable again while `partners@spainexpat.com` is still absent, classify the state precisely: `editor@` is a technically usable fallback for draft verification, but `partners@` remains the preferred first-contact BD mailbox and a separate blocker. Re-verify Drafts and Sent before upgrading any approval state.
- If `partners@spainexpat.com` becomes present/readable, do not leave the approval gate pointing at a legacy `editor@` draft that lacks Tyler CC. Create or recreate the review draft in `partners@` with `--cc ty@tmrtn.com`, immediately read it back from Drafts, search partners@ Sent to confirm it was not sent, update outreach/approval/waiting/collateral/mail-readiness Todoist tasks, and complete the `partners@` readiness task if that was its acceptance criterion.
- When mailbox readiness changes, also update durable repo artifacts that still encode the old mail state. Common stale surfaces: `docs/outreach/*partner-email-drafts*.md` frontmatter/status and mailbox blocker sections, and Dorado landing/collateral CTA fallbacks that still point partners to `editor@spainexpat.com`. Search docs for stale strings like `partners@ still not configured`, `not configured in Envelope`, and legacy `editor@` partner CTA text before finalizing.
- When reusing or re-verifying old server-side drafts, inspect `cc_addr` as well as To/Subject/body/flags/Sent status. If Tyler must be CCed and the existing draft lacks `ty@tmrtn.com`/`tyler@aposema.com`, mark the draft as needing edit/recreation before send even if the body is otherwise review-safe.
- Current mail DNS may still point at Amazon/WorkMail/SES (`inbound-smtp.us-west-2.amazonaws.com`, AWS autodiscover); Tyler favors moving SpainExpat mail to Migadu. Prepare migration plans and DNS inventories, but do not change DNS without explicit approval.
- Balcells and Lexidy should normally be pursued in parallel unless a real brand/relationship risk appears. Multiple same-category legal partners can increase member value.
- Lexidy may lack a verified recipient; use a verified email or approved contact-form path.
- A partner one-pager is useful, but if Tyler has flagged missing credibility/traction metrics, do not treat it as finished collateral until fixed or explicitly waived.
- Credibility framing can include nearly 25K public social/community footprint and a 232K+ legacy registered-member base only as a cleanup/reactivation asset, not as active members. Avoid current monthly traffic claims until analytics are verified.
- Dorado should have a coming-soon/waitlist landing page before full launch; keep it honest and non-promissory. A copy artifact is not enough once Tyler asks to “continue” or asks whether Claude Code is building it: start an actual build pass in the WordPress migration theme at `/Users/wondermonkey/Dropbox/Code/SECOM/wordpress/wp-content/themes/spainexpat`, creating a local page template and scoped styling, with no deploy/SFTP unless approved.
- Important live-path distinction: WordPress migration work is not the current production path if SpainExpat is still running legacy ExpressionEngine. If Tyler asks for a page/live launch before WP cutover, first make an explicit WP-vs-EE callout and use or improve the EE tooling rather than assuming the WP theme solves the live need.
- For current-production ExpressionEngine work, treat the EE CLI/MCP (`/Users/wondermonkey/Dropbox/Code/SECOM/ee-mcp`) as a prerequisite safety layer when it is fragile. Before using it to mutate production, harden/verify basics: health/check_config without secret leakage, safer empty/HTML/non-JSON response handling, structured sanitized errors, dry-run/validate mode for create/update, discovery helpers where the Reinos API supports them, idempotent upsert by channel/title/url_title, a direct local CLI wrapper, and mocked aiohttp tests. Do not rely on ad-hoc live mutation scripts as proof.
- Dorado should be packaged for FB group/community partners as a credible revenue-share/community partner program (e.g. 30% first-year commission), with strict approved-copy/legal-claim guardrails.
- Cita support must be framed as guided support/escalation where available, not an unlimited appointment guarantee.

## Claude Code plan-to-execute loop

For Dorado collateral, mail migration, partner-program, or launch-doc work, Tyler is comfortable with Claude Code producing plan files first, then executing them. Use this loop:
1. Start Claude Code in planning mode when the task benefits from decomposition.
2. Save each plan path (usually `/Users/wondermonkey/.claude/plans/...`) into the relevant Todoist task/comment for posterity.
3. Feed the plan back into Claude Code with explicit bypass/execution instructions to write repo artifacts or perform the bounded setup.
4. Verify actual outputs exist (repo docs, Migadu state, Envelope Drafts) before calling the work done.
5. Keep the same hard boundaries in both passes: no sends, no DNS changes, no production deploy/SFTP, no contact imports, no secret exposure unless Tyler explicitly approves that exact action.

## Pitfalls

- Do not call a Drafts-folder message “waiting on partner.”
- Do not count generic affiliate/ad-sales opportunities toward launch math.
- Do not send or submit contact forms without explicit approval.
- Do not leave cross-run findings only in chat or local files; update Todoist.
- Do not rewrite `ops/core-loop-state.md`; it is deprecated and Todoist is the operational SSOT.
- Cron jobs for this loop should load the real `spainexpat-core-loop-review` skill. If a cron output starts with “Skill(s) not found and skipped”, fix the job configuration rather than ignoring the warning.
- When launching Claude Code from Spanorama for SpainExpat/Dorado work, the profile-local Claude auth may be missing even if Tyler’s main Claude is logged in. Check `claude auth status --text`; if Spanorama HOME is unauthenticated, use `HOME=/Users/wondermonkey claude ...` only after confirming that main Claude auth is healthy. Do not report Claude work started until the process is actually running.
- Claude Code may interpret collateral/doc tasks as plan-mode and write only `~/.claude/plans/...` instead of the requested repo artifact. This is not necessarily a failure if the plan→execute loop is intended: save the plan path to Todoist, then run a second bypass execution pass that explicitly writes the target files. Verify target files exist with `search_files`/`read_file` before calling work complete.
- If Tyler authorizes Envelope drafts, Claude agents may create server-side drafts but still must not send. The prompt should explicitly allow draft creation, require `envelope accounts list --json` first, prefer `partners@spainexpat.com`, fall back only to a healthy `editor@spainexpat.com`, and write markdown drafts if no sender account is usable.
- Do not overwrite unrelated repo files or wander into infrastructure unless explicitly asked.

