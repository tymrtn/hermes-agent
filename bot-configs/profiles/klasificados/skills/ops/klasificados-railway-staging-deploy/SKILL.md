---
name: klasificados-railway-staging-deploy
description: Deploy and smoke a Klasificados story branch to Railway staging without touching production, including common Railway staging variable/schema pitfalls.
version: 1.0.0
author: Nagaklas
---

# Klasificados Railway Staging Deploy

Use when Tyler says to send a Klasificados story/branch to staging, or when a production gate should be proven on Railway staging first.

## Core rule

Staging is not just a URL. Prove all three:

1. the intended code is deployed to the staging service,
2. the service is using the staging database / staging variables, not production,
3. the exact route smokes successfully on the public staging URL.

Do not mark staging passed from a local merge probe alone.

## Known Railway identifiers

Discover live values rather than hardcoding if possible:

```bash
railway project list --json
railway service list
railway domain
```

As of the first successful use:

- Railway project: `klasificados`
- Project ID: `06b660fa-72d0-4ee9-827c-1e82d7d4fb0b`
- Staging environment name: `staging`
- Staging API service name: `api-production`
- Staging API URL: `https://api-production-stg.up.railway.app`

The service name is confusing: `api-production` exists in the staging environment too. Always check `railway status` says `Environment: staging` before deploying.

## Safe deploy pattern

1. Use a clean temporary worktree from current `origin/main`.
2. Merge the story branch with `--no-commit --no-ff`.
3. Run targeted tests in the merge worktree.
4. Link Railway to staging API service.
5. Deploy with `railway up --detach --environment staging --service api-production`.
6. Wait for `railway service status` to report `SUCCESS`.
7. Smoke the actual staging URL.
8. Save a redacted evidence artifact under `ops/qa/story-NNN/YYYYMMDD-staging/`.
9. Update Todoist with the staging URL, deployment ID, smoke result, and evidence path.

Example:

```bash
git fetch origin main STORY_BRANCH
git worktree add --detach /private/tmp/klasificados-story-NNN-staging origin/main
cd /private/tmp/klasificados-story-NNN-staging
git merge --no-commit --no-ff origin/STORY_BRANCH
PYTHONPATH=src UV_CACHE_DIR=/tmp/uv-cache uv run pytest TARGETED_TESTS -q
railway link --project 06b660fa-72d0-4ee9-827c-1e82d7d4fb0b --environment staging --service api-production --json
railway up --detach --environment staging --service api-production --message "stage story NNN short title"
railway service status
```

## Upload-size trap

`railway up` can fail with `413 Payload Too Large` if the worktree includes local caches, huge agent test runs, generated images, or `.venv`.

Create or repair `.railwayignore` in the temporary worktree before upload. Safe exclusions used successfully:

```text
.venv
.git
node_modules
agents
tests
test_data
mvp
*.md
.env
.env.*
__pycache__
.pytest_cache
htmlcov
.eggs
dist
build
scraper
scraper-v2
checkpoints
logs
*.mp4
*.log
.dev-api.log
src/clasificados/static/images/insights
landing/img
docs
legal
demos
assets
```

Important: do **not** exclude `src/clasificados/ops`. The API imports `clasificados.ops.*`; excluding it made admin routes unavailable.

## Variable truth trap

Do not trust stale aggregate variables like `DB_STAGING_URL` without verifying against the actual `db-staging` service variables.

Check metadata without printing secrets:

```bash
railway variables --service api-production --environment staging --kv
railway variables --service db-staging --environment staging --kv
```

Compare hashes/lengths only. In one incident, `api-production` staging had a stale `DB_STAGING_URL`; the actual `db-staging` service `DATABASE_URL` was different. The API kept falling back to stub mode until `api-production` staging `DATABASE_URL` was set to the current `db-staging` service `DATABASE_URL`.

Set secret-bearing variables with stdin, not command-line values:

```bash
railway variable set DATABASE_URL --stdin --skip-deploys --service api-production --environment staging
railway variable set DB_STAGING_URL --stdin --skip-deploys --service api-production --environment staging
railway variable set ADMIN_SECRET --stdin --skip-deploys --service api-production --environment staging
```

Then trigger a real deploy/restart. A plain restart may keep stale runtime state or logs ambiguous.

## Redeploy trap

If `railway up` says `no changes detected in watch paths, build will skip`, variable fixes may not produce a new running image. Trigger a deployment by setting a harmless staging-only variable:

```bash
railway variable set STAGING_REDEPLOY_TOKEN=$(date +%s) --service api-production --environment staging
```

Then wait for the new deployment to reach `SUCCESS`.

## Staging DB schema trap

A fresh or drifted `db-staging` can fail app startup and force stub mode even when credentials are correct.

Observed fixes before Story 168 could smoke:

```sql
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE TABLE IF NOT EXISTS contact_events (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    listing_id UUID NOT NULL,
    buyer_hash VARCHAR(64) NOT NULL,
    channel VARCHAR(20) NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    metadata JSONB DEFAULT '{}'::jsonb
);
CREATE INDEX IF NOT EXISTS idx_contact_events_listing ON contact_events(listing_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_contact_events_created ON contact_events(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_contact_events_buyer ON contact_events(buyer_hash, listing_id);
```

Use the actual `db-staging` `DATABASE_PUBLIC_URL` and local psql if needed:

```bash
/opt/homebrew/opt/libpq/bin/psql "$DATABASE_PUBLIC_URL" -v ON_ERROR_STOP=1 -c 'CREATE EXTENSION IF NOT EXISTS vector;'
```

If psql path moved, discover it with Python/glob or `which`; known candidates include `/opt/homebrew/opt/libpq/bin/psql` and `/opt/homebrew/opt/postgresql@16/bin/psql`.

## Smoke standard

For API/admin route staging proof:

- `/health` returns 200 and says `database: connected`, not `stub`.
- The target route returns the expected status on `https://api-production-stg.up.railway.app`.
- Logs since the smoke have no unwanted side-effect markers such as `Lazy trigger:`, `phone backfill`, `Scheduled task:`, or `daily backfill`.
- Save a redacted JSON summary under `ops/qa/story-NNN/YYYYMMDD-staging/staging-smoke-summary.json`.

Story 168 example successful smoke:

```text
/health 200 database=connected
/admin/contact-stats?secret=<ADMIN_SECRET> 200
/admin/contact-stats/?secret=<ADMIN_SECRET> 200
No lazy backfill / phone backfill / scheduler log hits
```

## Todoist comment pattern

Keep it concise:

```text
Story NNN staged on Railway staging.

Staging URL: https://api-production-stg.up.railway.app/ROUTE?secret=<ADMIN_SECRET>
Deployment: DEPLOYMENT_ID
Smoke: /health 200 connected; ROUTE 200; no lazy backfill/phone backfill/scheduler log hits.
Staging repairs applied: ...
Evidence: ops/qa/story-NNN/YYYYMMDD-staging/staging-smoke-summary.json
```

With local `todo` CLI, comments use `--body`:

```bash
todo raw POST /comments --body '{"task_id":"TASK_ID","content":"..."}'
```

Do not use `--data`; this CLI rejects it.

## Final Telegram format

Use Tyler's compressed format:

```text
168: staged, smoke passed. Prod untouched.
https://api-production-stg.up.railway.app/admin/contact-stats?secret=<ADMIN_SECRET>
```

Only add a second line for another story if it has a separate action/status.
