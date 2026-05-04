---
name: railway-cli-deploy
description: Deploy changes to Railway services, set env vars, and verify prod. Use when shipping to any of Tyler's Railway-hosted services (warboard, envelope, expatriator, klasificados, trading-dashboard, redline, family-book, antimemetic-demo). Covers the gotcha that pushing to GitHub does NOT always auto-deploy — explicit `railway redeploy -y` is often required.
version: 1.0.0
author: Skippy
license: MIT
metadata:
  hermes:
    tags: [railway, deploy, cli, env, prod, devops]
    related_skills: [finding-tools-on-tylers-mac, iterm-osascript-driver]
---

# Railway CLI Deploy Workflow

Tyler runs many projects on Railway. This is the proven end-to-end flow.

## Prerequisites

- `railway` CLI installed. On wonderbookneo it lives at `~/.nvm/versions/node/v22.22.2/bin/railway` and is NOT on the Hermes shell PATH by default. See `finding-tools-on-tylers-mac`.
- Logged in as `ty@tmrtn.com` — verify with `railway whoami`.

## Known projects

- `warboard` → warboard-production-5cff.up.railway.app (custom domain warboard.tmrtn.com)
- `envelope-email`, `expatriator`, `klasificados`, `trading-dashboard`, `redline`, `family-book`, `antimemetic-demo`

All under workspace "Tyler Martin's Projects".

## Step-by-step deploy

### 1. Link the project (one-time per directory)

Project link is stored in `.railway/config` inside the project dir. First run needs the interactive picker → route through iTerm (see `iterm-osascript-driver`):

```
cd ~/Dropbox/Code/<project> && railway link
```

Interactive sequence:
1. Workspace → Enter (default "Tyler Martin's Projects")
2. Project → type filter (e.g. "warboard"), Enter
3. Environment → Enter (default "production")
4. Service → Enter (default = first service, usually same name as project)

After linking, `railway status` confirms the project.

### 2. Set env vars

```
railway variables --set KEY=value
```

No Enter/confirm needed. Multiple at once:
```
railway variables --set KEY1=v1 KEY2=v2
```

Verify with plain `railway variables`.

### 3. Deploy

**KEY GOTCHA:** pushing to GitHub does NOT reliably auto-trigger a Railway deploy, even when GitHub integration is connected. Observed on warboard 2026-04-19: push succeeded, prod instance unchanged 5+ min later (same uptime). Always use explicit redeploy:

```
railway redeploy -y
```

`-y` skips the confirmation prompt. Completes in ~5–10s on Railway's side; takes 30–90s for the new container to come up.

Alternative (deploys local uncommitted code):
```
railway up
```

Use `up` for testing without committing. For normal workflow, commit + push + `railway redeploy -y` keeps git as source of truth.

### 4. Verify prod

Poll until new code is live:
```
for i in 1 2 3 4 5 6 7 8 9 10; do
  resp=$(curl -s https://<prod-url>/api/<new-endpoint>)
  echo "[$i] $resp"
  echo "$resp" | grep -q <marker> && echo "LIVE" && break
  sleep 15
done
```

Good signals:
- `/api/health` shows low `uptime` (seconds, not hours) → new container.
- New route responds with expected payload instead of catch-all HTML.
- Added HTML element IDs are in `curl https://<url>/` output.

## Common operations

| Task | Command |
|------|---------|
| View logs | `railway logs` |
| Stream logs | `railway logs -f` |
| Open dashboard | `railway open` |
| Run shell cmd in prod context | `railway run <cmd>` |
| Set var from .env file | `railway variables --set-file .env` |
| Unset var | `railway variables --remove KEY` |

## Hermes-specific workflow

The Hermes `terminal()` subprocess usually can't run `railway` directly (not on PATH, no interactive TTY for `railway link`). Use iTerm driver:

```
osascript <<'EOF'
tell application "iTerm"
  tell current window
    tell current session
      write text "railway variables --set KEY=value && railway redeploy -y"
    end tell
  end tell
end tell
EOF
sleep 10
osascript -e 'tell application "iTerm" to tell current window to tell current session to get contents' | tail -10
```

After the first `railway link`, subsequent commands in the same dir work non-interactively and can sometimes be run via plain `terminal()` with full path:
```
PATH="$HOME/.nvm/versions/node/v22.22.2/bin:$PATH" railway redeploy -y
```

## Pitfalls

1. **Redeploy plus env change:** `railway variables --set` then `railway redeploy` in quick succession works, but verify the var is actually set first by querying a `/api/health`-style endpoint that reflects config.
2. **GitHub auto-deploy is unreliable** on some Railway projects. Always assume you need `railway redeploy -y`.
3. **Custom domains cache at edge** (Fastly in Railway's case). `curl -sI https://<custom>.tmrtn.com/` shows `cache-control: max-age=0` for API routes, but static HTML may cache. Hard reload in Telegram mini-app requires force-close of the app.
4. **Express catch-all routes** (e.g. `app.get('*', ...)`) will return HTML for unknown paths. If a new route seems missing, curl with `-i` to see the response — HTML response = route doesn't exist in prod yet.
5. **Interactive prompts in Hermes terminal hang forever** — always route through iTerm.
6. **Don't `railway link` twice** in the same dir — it'll re-prompt. Check `railway status` first.

## Reference: warboard Todoist deploy 2026-04-19

Sequence used, wall time ~2 min from first edit to verified prod:

- local edits to server routes + docs, committed and pushed
- `railway link` via iTerm (first time on new machine)
- `railway variables --set TODOIST_TOKEN=<token>`
- `railway redeploy -y` (pushing alone did NOT deploy)
- poll `/api/tasks/health` every 15s until `configured: true`
- hard-refresh Telegram mini app on phone (Telegram caches static HTML aggressively)
