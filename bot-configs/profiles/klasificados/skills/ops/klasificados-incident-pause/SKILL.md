---
name: klasificados-incident-pause
description: Stop a runaway background job on Klasificados (enrichment loop, scraper cascade, cost bleed) and capture evidence before deep triage. Use when costs spike, the API times out, or a prior Claude Code session left an admin background task running.
---

# Klasificados — Incident Pause and Evidence Capture

Stop the bleeding first, capture evidence, then hand clean triage notes to Tyler.

## Trigger conditions

- Cost alert from OpenRouter, Anthropic, or Tavily
- api.klasificados.net times out but DNS resolves
- Tyler reports a prior Claude Code session "fixed" enrichment or scraper
- Repeated DB error cascades visible in logs
- A fresh `/scrape/all` with `backfill=true` and high `backfill_concurrency` (>=20) caused api.klasificados.net to stop responding — this is a known blast pattern, not coincidence. Cancel the scrape before debugging the API.

## Scraper architecture facts that will save you cycles

Read these before you "fix" anything scraper-related.

- **Auto-expiration sweep is INTENTIONALLY DISABLED** (Feb 2026). See `scraper/src/scraper_service/api.py` around the comment `Auto-expiration sweep DISABLED`. Do NOT write a time-based sweep that marks `last_seen_at < cutoff` as expired. It was removed because COO pagination caps at ~4k results per query, so listings scraper1 simply couldn't reach were being wrongly expired.
- **Expiration happens via detail-backfill only.** When `backfill=true`, each listing's detail page is fetched. If COO redirects to `/NoAdID.asp` (302) or the page says "Anuncio no disponible", THAT listing is marked `status='expired'`, `expired_at=NOW()`. Per-scrape summary field: `expired_detected`.
- Therefore: to get "truly active listings" you run `/scrape/all` with `backfill=true`. That IS the sweep. No separate endpoint needed.
- `scraper-v2` has a different mechanism (`mark_expired_listings` time-based on `last_seen_at`) that IS wired up, but scraper-v2 is not the primary data source in use by web/api as of now.
- Scraper v1 fans out per subcategory — `/scrape/all` produces ~20+ concurrent jobs, each with its own `backfill_concurrency` worker pool. Total concurrent detail fetches ≈ `N_subcats × backfill_concurrency`. At defaults (20 subcats × 20 backfill) that is 400 concurrent HTTP fetches against COO plus 400 concurrent DB writes against staging. This overloads the stack. Safer starting point: kick off one category at a time, or pass `max_pages` low and `backfill_concurrency=5` if you need the full fanout.

## Railway topology (prod env `production`)

Project: `klasificados` (id `06b660fa-72d0-4ee9-827c-1e82d7d4fb0b`)

Services (look up IDs at incident time with `railway status --json`):
- web-production → klasificados.net (HTML / SSR)
- api-production → api.klasificados.net (JSON API, admin endpoints, enrichment background task)
- scraper-api → scraper-api-production-84e0.up.railway.app (daily scrape + backfill)
- scraper-v2 (next-gen adapters)
- db-production, db-staging (Postgres)

The enrichment runaway almost always lives on api-production.

## Secrets you will need

- ADMIN_SECRET — local .env in the klasificados repo. Loaded via shell var, never printed.
- OPENROUTER_API_KEY — local .env is NOT the same key as Railway's runtime key. Always compare the Railway value (`railway variables --service api-production --kv`) against local by length + head/tail before drawing cost conclusions.
- OpenRouter provisioning key (labeled `sk-or-...ef07` in the repo-level `/openrouter` skill) is required to see per-key activity. Without it, you only get account totals.

Model routing gotcha: `DEFAULT_MODEL = google/gemini-3.1-flash-lite-preview` in `src/clasificados/enrichment.py`, but `get_llm_client()` prefers Anthropic when `ANTHROPIC_API_KEY` is set (which it is on Railway). OpenRouter-format model strings passed to the Anthropic client behave unpredictably and can land on Claude-tier pricing. Always check both Anthropic and OpenRouter consoles when accounting for a cost spike.

## Step 1 — Confirm scope (30 seconds)

```bash
cd /Users/tylermartin/Dropbox/code/klasificados
git log --all --since="48 hours ago" --pretty=format:"%h %an %ar %s" | head -30
ps aux | grep "claude --dangerously-skip-permissions" | grep -v grep | head -20
ls -lat ~/.claude/projects/-Users-tylermartin-Dropbox-Code-klasificados/*.jsonl | head -5
```

Look for commits like "no limits", "max parallelism", "concurrency", "while True".

## Step 2 — Pull live logs BEFORE restarting

`railway logs` streams forever. Wrap in a subshell with sleep+kill to grab a slice:

```bash
cd /Users/tylermartin/Dropbox/code/klasificados
(railway logs --service api-production --environment production 2>&1 & PID=$!; sleep 20; kill $PID 2>/dev/null) 2>&1 | tail -100
```

Signatures that indicate a retry-loop (this is the bleed pattern):
- `Error applying enrichment for <uuid>: ... current transaction is aborted, commands ignored until end of transaction block`
- `Cache write failed ...: NotNullViolation: null value in column "id" of relation "product_knowledge"`
- Tavily POST /search chains per listing
- Nominatim 429s (noise, not cost)

The NotNull bug means listings never get marked enriched, so they get reprocessed on the next pass and each pass re-pays OpenRouter + Tavily + EPA lookups. That's where "should have cost $X" becomes 3-5x.

## Step 3 — Try graceful stop (often fails when service is hung)

Use curl to POST to `/admin/enrichment/stop?secret=$ADMIN` on api.klasificados.net and `/cancel?secret=$ADMIN` on the scraper-api host. Load the secret into a shell variable; never echo it.

If api.klasificados.net times out AND the direct Railway host (9fo603hj.up.railway.app) also times out while DNS resolves fine, the service is crashed or deadlocked, not "too saturated". Skip to Step 4 — don't waste minutes retrying curls.

### Scraper cancel — the right and wrong endpoints

- RIGHT: `POST https://scraper-api-production-84e0.up.railway.app/cancel?secret=$ADMIN_SECRET` — cancels all running scrapes, returns `{"status":"cancel_requested","cancelled":N,"scrape_ids":[...]}`.
- WRONG: `POST .../scrape/cancel?secret=...` — this hits `@app.post("/scrape/{category}")` with `category="cancel"` and STARTS a NEW bogus scrape named "cancel". Easy footgun. Always use `/cancel` (no `/scrape/` prefix).
- The main API's `/admin/scrape/stop` on api.klasificados.net is NOT a reliable proxy to the scraper cancel. When api-production is saturated it times out before reaching the scraper. Prefer the direct scraper URL for cancellation.

After cancel, re-poll `GET /status?secret=$ADMIN_SECRET`. If `active_count` stays at 20 with NEW scrape_ids you have never seen, the scraper daily orchestrator is respawning replacements — set `DAILY_SCRAPE_ENABLED=false` (Step 5) before re-issuing cancel, or they will come right back.

## Step 4 — Hard restart via Railway redeploy

The enrichment background task is in-process only (no persistence), so redeploy kills it cleanly.

```bash
cd /Users/tylermartin/Dropbox/code/klasificados
railway redeploy --service api-production --yes
sleep 40
curl -sS --max-time 15 "https://api.klasificados.net/health"
```

Verify with `GET /admin/enrichment/status` → expect `running: false`.

CLI gotchas:
- `railway redeploy` does NOT accept `--environment`. Only `--service NAME --yes`; it uses the linked environment.
- `railway service list` is not a command. Use `railway status --json` to enumerate services.
- `railway logs` has no `-n` / `--tail` flag.

## Step 5 — Prevent auto-restart while triaging

Disable the scraper daily cron:
```bash
railway variables --service scraper-api --environment production --set "DAILY_SCRAPE_ENABLED=false"
railway redeploy --service scraper-api --yes
sleep 40 && curl -sS "https://scraper-api-production-84e0.up.railway.app/health"
# Verify: "daily_scrape_enabled": false
```

api-production scheduler is usually already off. Confirm `SCHEDULER_CRON_ENABLED` value on the service.

There is currently NO `ENRICHMENT_DISABLED` env kill switch in the code. `/admin/enrichment/start` still works for anyone with the admin secret. If you suspect leakage, rotate ADMIN_SECRET.

## Step 6 — Cost surface snapshot

OpenRouter:
- `GET https://openrouter.ai/api/v1/auth/key` with Bearer token → key label, lifetime usage, monthly/weekly/daily, BYOK usage, rate limit
- `GET https://openrouter.ai/api/v1/credits` → account totals
- `GET https://openrouter.ai/api/v1/activity` requires a management/provisioning key; runtime keys get 403
- Per-key attribution needs the provisioning key

Anthropic: check the console directly — the enrichment client-preference means spend may have landed there even though the code looks OpenRouter-first.

Tavily: check dashboard. One Tavily search per listing × retry-loop multiplier adds up fast.

## Why actual cost balloons past a naive estimate

If Tyler expected ~$X for N listings × 1 LLM call and the bill is 3-5x, suspect in this priority order:

1. **`ENRICHMENT_REASONING_EFFORT` on Gemini models (often THE dominant multiplier)** — code default is `"low"` (src/clasificados/enrichment.py line ~68). When set, the OpenRouter call passes `extra_body["reasoning_effort"]` which allocates a thinking-token budget on Gemini-family models. Rough magnitude on Gemini 2.5/3.1 Flash with `low`: ~20K reasoning tokens per listing billed on top of the completion. At 48k listings that is ~960M extra tokens paid at reasoning-tier rates. Check `railway variables --service api-production --json | grep -i REASONING`. If unset on Railway the code default (`"low"`) applies and you are still paying for reasoning — to fully disable, change the code default to empty string, not just unset the env var (line ~2181 only skips `extra_body["reasoning_effort"]` when `reasoning_effort` is empty/falsy).
2. **Vision input multiplier** — `DEFAULT_ENRICHMENT_IMAGE_LIMIT = 3` in `src/clasificados/enrichment.py` (was 8 before fix on 2026-04-17) and `DEFAULT_ENRICHMENT_IMAGE_DETAIL = "high"`. Each listing sends up to N images at high detail to the vision model. At 8 images × high detail, input tokens per call balloon ~5-10x vs text-only. On Railway, CHECK that `ENRICHMENT_IMAGE_LIMIT` is set to the intended product value (usually 3) — if the env var is missing, the code default wins. Verify via `railway variables --service api-production --kv | grep -i image`.
3. DB write failure loop — listings never marked enriched, so they re-enter the queue and re-pay every external call. Compounds the per-call cost by the number of passes.
4. Per-listing cost stack — enrichment chains classify → localize → Tavily web search → product_knowledge cache → Nominatim geocode → search-vector refresh. LLM calls in 2-3 of those steps.
5. Broken knowledge cache — same NotNull bug blocks the dedup cache, so every vehicle re-pays Tavily + EPA even when make/model/year was resolved hundreds of times. 48k listings probably map to under 2k unique keys; a working cache would drop Tavily cost ~95%.
6. No per-run budget ceiling — async engine accepts `limit=0` as "ALL" at concurrency=20 with no cost cap.

**Provider routing reality (corrected 2026-04-17)**: Despite `get_llm_client()` preferring Anthropic when `ANTHROPIC_API_KEY` is set, in practice ~100% of enrichment spend lands on OpenRouter. The `analyze_listing_with_llm` path explicitly overrides the client to OpenRouter when the model string contains a `/` and does not start with `anthropic/` (see lines ~2050). `DEFAULT_MODEL = google/gemini-3.1-flash-lite-preview` triggers this override every time. Check OpenRouter first, then Anthropic console only if OpenRouter totals don't match the reported spike.

## Pitfalls

- Do not assume the local .env OpenRouter key is what production uses. Compare lengths and head/tail against `railway variables`.
- Do not trust "api.klasificados.net timed out" as "the service is saturated". If direct Railway host also times out with DNS working fine, the service is dead — redeploy immediately. Tyler will call this out if you dither.
- Do not pipe remote JSON into a Python interpreter from the sandbox; save to a temp file and read it instead. The security scanner blocks agent-invoked pipe-to-interpreter patterns.
- `railway redeploy` takes only `--service X --yes`, no environment flag.
- `railway logs` streams forever; always wrap in a time-boxed subshell.
- The background enrichment task has no persistence, so redeploy is sufficient to stop it. Do not touch DB state to "stop" it.
- Stopping enrichment does not fix the loop. If you retrigger while the NotNull bug is live, costs restart. Fix the DB cache bug before any re-run.
- Do not assume your cost theory is right when the user gives you contradicting real numbers (e.g., "100% is on Gemini Flash on OpenRouter"). Trust the dashboard and look for the real multiplier (usually vision image count, not provider mismatch).

## Coordinating with a parallel live Claude Code session

Tyler often runs Claude Code sessions in parallel to your Hermes/ops session on the same repo. If you see an active process like `claude --dangerously-skip-permissions --resume` whose `lsof cwd` is `/Users/tylermartin/Dropbox/Code/klasificados`, it is likely mid-fix on the same incident. Racing it on code changes creates merge conflicts and wasted work.

### Detect an active Klasificados Claude Code session

```bash
for pid in $(ps aux | grep "claude --dangerously" | grep -v grep | awk '{print $2}'); do
  cwd=$(lsof -p $pid 2>/dev/null | awk '$4=="cwd"{print $9; exit}')
  if [[ "$cwd" == *klasificados* ]]; then echo "PID=$pid cwd=$cwd"; fi
done
ls -lat ~/.claude/projects/-Users-tylermartin-Dropbox-Code-klasificados/*.jsonl | head -3
```

The jsonl file with the most recent mtime is the active session transcript.

### Extract its plan without disturbing it

Session transcripts are append-only JSONL. Each assistant turn contains `tool_use` blocks. Pull TaskCreate subjects/descriptions and the last assistant text for context:

```python
python3 - <<'PY'
import json
path='/Users/tylermartin/.claude/projects/-Users-tylermartin-Dropbox-Code-klasificados/<SESSION_UUID>.jsonl'
tasks=[]; last_text=[]
for line in open(path, errors='replace'):
    try: o=json.loads(line)
    except: continue
    m=o.get('message') or {}
    if m.get('role')!='assistant': continue
    for p in (m.get('content') or []):
        if isinstance(p, dict):
            if p.get('type')=='tool_use':
                n=p.get('name',''); inp=p.get('input') or {}
                if n=='TaskCreate': tasks.append(inp)
                if n=='TaskUpdate': tasks.append({'_update': inp})
            elif p.get('type')=='text':
                last_text.append(p.get('text',''))
for t in tasks:
    if '_update' in t:
        u=t['_update']; print(f"UPDATE id={u.get('taskId')}: {u.get('status')}")
    else:
        print(f"CREATE: {t.get('subject','?')}")
        d=(t.get('description','') or '')[:200].replace(chr(10),' / ')
        if d: print(f"     > {d}")
print("--- LAST TEXT ---")
if last_text: print(last_text[-1][:1500])
PY
```

### Carve out non-overlapping work

Your contribution should NOT be code edits in files the other session is actively editing. Safe, complementary work:

- Railway env-var pre-provisioning for env vars its plan references but has not yet set (e.g. `ENRICHMENT_MAX_LIMIT`, `ENRICHMENT_MAX_COST_USD`, `ENRICHMENT_MIN_BALANCE_USD`). Only do this if the var name appears in its TaskCreate descriptions — do not pre-guess.
- Pre-flight dependency verification: does `DB_STAGING_URL` exist on api-production? `CLOUDFLARE_BR_TOKEN` on scraper-api? OpenRouter balance sufficient for any paid validation run? These save the other session cycles later.
- Monitoring: poll `git fetch origin` every 10-15 min for its PR landings, review diffs, check merge compatibility with your own open PRs.
- End-to-end validation after its PRs merge — it will plan the validation but you can actually execute it (e.g. `curl -X POST /admin/enrichment/start?limit=100` on a `:free` model, confirm OpenRouter `usage=0`, spot-check VDP quality).
- Post-run cost/quality report and switchback-to-paid-model decision.

### Pitfalls

- There is no direct messaging channel to a local Claude Code session from Hermes. You cannot "chat" with it. You can only observe its transcript and act on adjacent surfaces.
- Do not push commits that touch `src/clasificados/enrichment.py` or `src/clasificados/routes/admin.py` while a Klasificados Claude Code session is active — those are the enrichment hotspots it is almost certainly editing. Keep your PRs to config files, docs, and peripheral modules.
- If you do need to ship a code fix that might overlap, tell Tyler first so he can coordinate which session owns which file.
- The session may not have pushed anything to the remote yet. Absence of new branches ≠ absence of work. Check the jsonl mtime, not just `git log`.

## Clean PRs when the working tree is filthy

```bash
cd /Users/tylermartin/Dropbox/code/klasificados
git fetch origin main --quiet
git worktree add -b fix/<branch-name> /tmp/klas-<short> origin/main
# make your edit under /tmp/klas-<short>/...
cd /tmp/klas-<short>
git add <file> && git commit -m "fix(scope): message"
git push -u origin fix/<branch-name>
gh pr create --title "..." --body "..." --base main
# cleanup
cd /Users/tylermartin/Dropbox/code/klasificados
git worktree remove /tmp/klas-<short> --force
```

The initial `git worktree add` checkout emits a lot of "Updating files: N%" progress spam — that's normal, not an error.

## Evidence summary template (handoff to Tyler)

```
Pause actions:
- api-production: redeployed -> enrichment running=false (verified via /admin/enrichment/status)
- scraper-api: DAILY_SCRAPE_ENABLED=false, redeployed (verified via /health)
- scraper-v2: <status or N/A>
- No changes to web-production or DBs.

Root cause hypothesis:
- Trigger: <commit or PR that introduced the runaway>
- Loop signature: <DB error pattern captured from logs>
- Cost surface: OpenRouter $X + Anthropic $Y + Tavily $Z (pending per-key split if no provisioning key)

Not yet done:
- Fix product_knowledge.id NotNull bug (blocks any safe re-enrichment)
- Route enrichment to explicit cheap model instead of Anthropic-preferred client
- Add per-run cost ceiling (max_cost_usd parameter)
- Add ENRICHMENT_DISABLED env kill switch
- Rotate OpenRouter + Anthropic keys if evidence of leakage
- Verify Tavily dashboard spend
```

## Verification checklist before closing the incident

- [ ] GET /health on api.klasificados.net returns 200
- [ ] GET /admin/enrichment/status returns running: false
- [ ] scraper-api /health shows daily_scrape_enabled: false
- [ ] No active Claude Code process holding an /admin/enrichment/start call open
- [ ] Log slice saved for post-mortem (e.g., /tmp/klasificados-incident-YYYYMMDD.log)
- [ ] Tyler briefed with evidence template filled in
