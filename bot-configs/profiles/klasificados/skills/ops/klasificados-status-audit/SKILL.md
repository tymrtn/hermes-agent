---
name: klasificados-status-audit
description: Run a fast but evidence-based Klasificados status investigation across repo docs, live health, payments, analytics, scraper status, GTM instrumentation, testing, and local OpenClaw context.
version: 1.0.0
author: Hermes Agent
---

# Klasificados Status Audit

Use this when Tyler asks for project status, a backgrounder, or a problem-focused operational review of Klasificados.

## Goal

Produce a problem-first status read covering:
- source/repo status
- live health
- scraper status
- analytics instrumentation
- GTM / paid-growth instrumentation
- revenue / Stripe status
- user-flow testing status
- optional OpenClaw / Skippy context

## Required context

Repo root:
- Preferred live Hermes path: `/Users/wondermonkey/Dropbox/Code/klasificados`
- User-facing/macOS path may be written as `~/Dropbox/code/klasificados`, but do not pass literal `~` or the lowercase path as a tool `workdir` if commands fail before running.
- If terminal/read/search tools return `FileNotFoundError` for `~/Dropbox/code/klasificados`, immediately retry with `/Users/wondermonkey/Dropbox/Code/klasificados` rather than stopping or asking Tyler to fix the environment.

Always inspect first when relevant:
- `CLAUDE.md`
- `.claude/skills/`
- `.claude/settings.local.json`
- `.env`

## Workflow

### 1. Read local project guidance first

Read:
- `CLAUDE.md`
- local scraper/architect notes if needed
- `.env` for feature/config presence (do not expose full secrets)

Look specifically for:
- current deployment topology
- scraper v1 vs v2 notes
- payment provider migrations
- analytics/Umami references
- retired infra called out in docs

### 2. Check repo reality, not just docs

Run:
- `git status --short`
- `git branch --show-current`
- `git log --oneline -5`

This catches a common failure mode: docs say one thing, but local main is dirty or half-migrated.

### 3. Search source for the target surfaces

Use content search for:
- Stripe / Square / ATHM
- GTM / Facebook Pixel / Google Ads / gtag / GTM containers
- Umami / analytics
- scraper-v1 / scraper-v2 / direct HTTP / browser rendering / CAPTCHA
- Playwright / smoke / browser / e2e tests

This is usually faster than reading whole files.

### 4. Prefer browser checks for live health endpoints

For these URLs, use browser navigation/snapshot instead of terminal curl when possible:
- `https://klasificados.net/health`
- `https://api.klasificados.net/health`
- `https://scraper-v2-production.up.railway.app/health`
- `https://scraper-api-production-84e0.up.railway.app/health`

Reason: terminal network calls to Railway `.app` domains may trigger approval friction. Browser checks avoid that and are enough for non-destructive health inspection.

Record especially:
- API `stripe_configured`
- API listing count
- scraper running job counts
- db target / scheduling hints from health payloads

### 5. Live page spot-checks for business-critical flows

Use browser on:
- homepage
- `/pricing`
- `/credits`
- `/publicar`
- one representative listings category page (prefer a known-good live route like `/en/listings/vehicles/` instead of guessing slug variants that may 404)

Check for:
- console errors
- mixed language / localization bleed
- payment UI claims vs backend truth
- publish flow gating behavior
- broken or misleading CTAs
- analytics instrumentation parity across critical pages, especially whether `/credits` and `/publicar` include the same Umami script as homepage/pricing

Important finding pattern:
- If `/credits` says Stripe but `/health` says `stripe_configured=false`, treat as revenue-blocking inconsistency.
- On `/credits`, do not stop at text-only checks. Inspect the rendered CTA state in the browser. A common live failure mode is an active-looking `Pagar` button that still appears enabled even when Stripe is not configured.
- Static landing pages may bypass the shared base template entirely. Do not assume homepage/pricing analytics or i18n parity automatically extends to `/credits` or `/publicar`; verify script presence and language behavior page-by-page.
- `/publicar` may partially improve over time (for example, raw probes or source HTML showing a Spanish title) while the browser-rendered page still leaks English in the actual experience (`Post listing for free`, `1 Photos`, `Upload your photos`, `Continue without photos`). Record the exact layer that is still drifting instead of calling the whole page simply English-only.
- Do not trust raw HTML fetches alone for `/publicar` or similar static pages. Client-side language scripts can make the browser-rendered page drift from the fetched source, so browser verification is authoritative when probe output and visible UI disagree.
- Specific recurring failure mode on `/publicar`: the anti-flash language bootstrap and `boriken-i18n.js` can both fall back to `navigator.language`, so an English-preference browser can render the default route in English even when the HTML source itself is Spanish. When this happens, inspect both the inline anti-flash script and `boriken-i18n.js#getLang()`. A durable fix is to give the page an explicit route default such as `data-default-lang="es"` and make both bootstraps honor that before falling back to browser language.
- Also inspect loaded scripts on `/publicar`, not just visible copy. Production can still carry stale payment-era assets even when checkout is meant to be off. A concrete recurring example is `https://sandbox.web.squarecdn.com/v1/square.js` still loading on `/publicar`; treat that as trust/parity drift worth calling out and verifying against intended payment readiness.

### 5.5 Revenue truth probe: inspect JS-bound checkout, not just links

If revenue state is ambiguous, run:
- `python3 scripts/stripe_live_probe.py`
- `python3 scripts/landing_truth_probe.py`

Why:
- `/credits` may expose Stripe checkout through JS (`credits.js`) rather than a visible `href` or `form action`
- a text grep can miss the real issue: the page can render an enabled-looking pay CTA while backend health still says `stripe_configured=false`
- `landing_truth_probe.py` gives a compact, repeatable landing-level read across `/`, `/pricing`, `/credits`, `/publicar`, and `/en/listings/vehicles/` so the audit does not rely only on one-off browser notes

What to look for in the probe output:
- whether `/credits` has an active-looking JS checkout CTA
- whether external scripts contain markers like:
  - `payments/stripe/checkout-session`
  - `payments/stripe/confirm`
  - `stripe-checkout-btn`
  - `window.location.assign(payload.checkout_url)`
- whether the landing probe reports:
  - `stripe_claim_without_live_stripe`
  - `active_checkout_cta_without_live_stripe`
  - `missing_umami`
- whether the probe finds no checkout URLs but still finds JS-bound checkout flow markers

Interpretation rule:
- If API health says `stripe_configured=false` but the probe finds JS checkout flow markers or an active-looking `/credits` pay button, call it a live revenue-truth mismatch, not a harmless copy issue.
- If targeted local tests pass and local templates/routes look fixed, but `stripe_live_probe.py` and browser checks still show the old mismatch on production, conclude the patch is not deployed yet rather than assuming the probe is wrong.

### 6. Run targeted tests with correct import path

Use:
- `PYTHONPATH=src python3 -m pytest ...`

At minimum check:
- `tests/unit/test_analytics.py`
- `tests/browser/test_revenue_pages.py`
- `tests/smoke/test_landing_smoke.py`

Important pitfalls learned:
- Running pytest without `PYTHONPATH=src` can fail with `ModuleNotFoundError: clasificados`.
- Browser/smoke suites may fail during collection, which is itself a status finding.
- Current known failure patterns include Python version/type-hint incompatibility and Playwright import guards that still leave `Page` undefined.
- Specific recurring fixes seen in this repo:
  - `tests/smoke/test_landing_smoke.py` can raise `NameError: Page is not defined` when Playwright is missing; adding `from __future__ import annotations` lets the skip guard work instead of crashing collection.
  - `tests/browser/test_revenue_pages.py` can fail under system Python 3.9 because importing `clasificados.routes.landing` triggers eager imports from `src/clasificados/ops/__init__.py`; lazy-loading ops exports avoids unrelated `str | list[str]` / built-in generic annotation crashes during collection.
  - Revenue page assertions may drift from live copy; if the credits page now says `Bonus 10%` / `Popular` instead of `+10%` / `Most Popular`, treat that as either stale tests or UX drift and verify against the actual landing HTML before deciding which to change.

### 7. Treat GTM as two separate questions

Do not conflate them:
1. Product analytics instrumentation (Umami)
2. Paid growth instrumentation (GTM / Meta Pixel / Google Ads tagging)

A frequent false positive is "analytics exists" when only Umami exists.

Explicitly search app pages for:
- `fbq(`
- `gtag(`
- `googletagmanager`
- `GTM-`

If not present in actual landing/app pages, conclude GTM strategy is documented but not deployed.

### 7.5 Analytics reality check: do not trust the admin endpoint blindly

Current pitfall discovered in production:
- `/admin/analytics` may only return a pointer to the Umami dashboard, not actual counts
- local `.env` may not contain `UMAMI_USER` / `UMAMI_PASSWORD`, so browser access can stop at the Umami login page
- `/admin/sessions/stats` can return all-zero funnel data even when `analytics_events` contains historical rows or `user_sessions` still has rows
- a reusable local truth probe now exists at `scripts/telemetry_truth_probe.py`; prefer running it after direct DB checks so the mismatch is captured consistently
- current repo version of `scripts/telemetry_truth_probe.py` auto-loads the repo `.env` and can be run directly from the repo root without manually exporting `DATABASE_URL` first
- if `scripts/telemetry_truth_probe.py` crashes in the Hermes Python 3.11 environment on missing `psycopg2`, patch it to prefer `psycopg2` when installed but fall back to `psycopg` when that is the available local driver; then re-run the probe and its unit test
- if the probe appears syntactically broken or truncated (for example around the admin auth call or `ADMIN_SECRET` load), treat that as a repo-state bug worth fixing immediately rather than abandoning the telemetry check
- if the admin endpoints return a missing-`secret` validation error, treat that as auth semantics, not telemetry evidence; the probe should record that explicitly instead of aborting

When the user asks specifically about buyer traffic, searches, or contact intents:
1. Hit `/admin/analytics` and `/admin/sessions/stats` first
2. If either is thin or suspicious, query PostgreSQL directly for:
   - `analytics_events`
   - `contact_events`
   - `user_sessions`
3. Pull at minimum:
   - 30d counts for searches, views, contacts
   - 90d counts and most recent timestamps
   - top search queries from `analytics_events.event_data->>'query'`
   - event counts grouped by `analytics_events.event_type` (not `event_name`)
4. Check the operational inboxes for external traffic signals that can contradict first-party telemetry, especially Cloudflare analytics emails like `Congrats on passing 100,000 Pageviews`
5. Treat zero recent data as ambiguous: either low usage or broken capture
6. Say explicitly which it is: low demand, broken telemetry, or both
7. If an external surface (for example Cloudflare email) shows meaningful traffic while first-party telemetry is near-zero, call that a telemetry mismatch immediately rather than framing it as a maybe-quiet site

Useful check pattern:
- if `analytics_events` has rows but `user_sessions` is near-empty, session funnel instrumentation is out of sync
- if `contact_events` is zero all-time, website contact click capture is effectively dead or unused
- if latest analytics row is stale, call out the exact timestamp
- `GET /admin/sessions/stats` may require the admin `secret` query param; if it returns a validation error for missing `secret`, treat that as an auth requirement rather than a product signal
- local Python in this repo may have `psycopg2` installed but not `psycopg`; if `create_db_engine()` fails on `postgresql+psycopg`, query production DB with SQLAlchemy using `DATABASE_URL.replace("postgresql://", "postgresql+psycopg2://", 1)`

### 7.6 Backlog hygiene is part of the audit, not a separate nice-to-have

After confirming live truth, check whether the backlog reflects reality.

Specifically:
- inspect high-signal current stories tied to the findings (for example revenue truth, telemetry, landing trust)
- ensure they have machine-readable frontmatter at minimum:
  - `id`
  - `status`
  - `priority`
- normalize priorities to `P0` / `P1` / `P2` / `P3`
- if a story is still marked `ready` for a production problem that is already demonstrably resolved, move it to the matching status filename and rewrite it to reflect current reality instead of leaving a stale ready item in queue

Concrete example seen in Klasificados:
- `ready-story-269-pricing-page-500.md` was stale once `/pricing` was verified live at HTTP 200; the useful follow-on work belonged in story 280, not in leaving 269 misleadingly ready

Also do a lightweight scan for remaining metadata drift so the report can say whether the repair is complete or partial.

### 7.7 Browser checks on JSON health endpoints: handle "Empty page" correctly

Some health endpoints render as an empty accessibility snapshot even when the JSON body is present.

When that happens:
- use browser navigation first
- then run `browser_console` with an expression like:
  - `JSON.parse(document.body.innerText)`

This worked for:
- `https://klasificados.net/health`
- `https://scraper-api-production-84e0.up.railway.app/health`
- `https://scraper-v2-production.up.railway.app/health`

Do not incorrectly record these as unavailable just because the browser snapshot says `Empty page`.

### 8. Use OpenClaw / Skippy carefully

OpenClaw binary is at:
- `/opt/homebrew/bin/openclaw`

Default agent:
- `main` (`Skippy`)

Useful commands:
- `openclaw --help`
- `openclaw agents list --json`
- `openclaw agent --agent main --json --message "..."`

Pitfall:
- Skippy may return polluted context from prior sessions if not isolated. Treat it as supplemental only, not authoritative, unless you explicitly control session routing/state.

### 9. Report only problems unless asked otherwise

Tyler may ask for a backgrounder but not want a neutral summary. In that case, report:
- what is broken
- what is risky
- what is inconsistent
- what is unverified

Keep it short and rank implicitly by business impact.

## Strong problem patterns to call out

- Revenue UI says Stripe is live, but API health says `stripe_configured=false`
- Codebase shows incomplete migration: Stripe added, but Square/ATH remnants still power seller/posting surfaces
- Browser/smoke tests fail at collection, so live-flow regression coverage is effectively broken
- Umami is installed, but no GTM / Meta Pixel / Google Ads instrumentation exists in live pages
- Docs disagree on scraper/database topology, creating agent/operator confusion
- Dirty working tree on `main` makes status ambiguous
- Language bleed or mixed bilingual rendering on pricing/payment/posting pages hurts trust

## Output style

- Lead with biggest business problem first
- Group by area: revenue, UX, tests, scraper, analytics, GTM, repo hygiene
- Prefer direct statements over narrative
- Do not dump secrets or env values

## Example one-line conclusion

"Biggest current issue: payments are not actually live despite live UI claiming Stripe; next biggest issue: the browser/smoke test layer is broken, so funnel regressions can slip through."