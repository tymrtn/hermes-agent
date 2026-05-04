---
name: claude-cli-oauth-fix
description: Fix Claude CLI returning 401 authentication_error even when auth status shows a valid Max/OAuth login. Happens when ANTHROPIC_API_KEY is set to a non-Anthropic key (e.g. xAI, OpenRouter).
version: 1.0.0
author: Skippy
license: MIT
metadata:
  hermes:
    tags: [claude-code, oauth, auth, env-vars, pitfall]
    related_skills: [claude-code]
---

# Claude CLI OAuth Fix — 401 with Valid Login

## Trigger

Load this skill when:
- `claude -p "..."` returns `API Error: 401 Invalid authentication credentials` in ~500ms
- `claude auth status --text` shows a valid login (e.g. "Login method: Claude Max account")
- You're on Tyler's machine (ANTHROPIC_API_KEY is known-hijacked to xAI)

## Root cause

Claude CLI precedence: `ANTHROPIC_API_KEY` env var wins over stored OAuth credentials at `~/.claude/.credentials.json`. If the env var is set to a key from another provider (xAI, OpenRouter, etc. via Anthropic-compatible endpoint), CLI tries to auth with it and Anthropic rejects it — while `auth status` still truthfully reports the OAuth login exists.

Other contaminating vars to check: `ANTHROPIC_AUTH_TOKEN`, `ANTHROPIC_TOKEN`, `CLAUDE_CODE_OAUTH_TOKEN`.

## Fix — scrub env before invoking

**Preferred (nuclear, safest):**
```
env -i HOME="$HOME" PATH="$PATH" TERM="$TERM" SHELL="$SHELL" USER="$USER" claude -p "..."
```
Use this for print-mode automation. `env -i` wipes everything, then you whitelist only what the CLI actually needs.

**Surgical (if you need other env vars passed through):**
```
env -u ANTHROPIC_API_KEY -u ANTHROPIC_AUTH_TOKEN -u ANTHROPIC_TOKEN -u CLAUDE_CODE_OAUTH_TOKEN claude -p "..."
```

## Verify OAuth creds exist first

```
python3 -c "import json,time; d=json.load(open('$HOME/.claude/.credentials.json')); c=d['claudeAiOauth']; print('expires in days:', (c['expiresAt']-int(time.time()*1000))/86400000)"
```

If the cred file is missing or expired, the above fixes won't help — run `claude` interactively once to re-auth via browser OAuth.

## Background processes

Same pattern works with Hermes `terminal(background=true)`:
```
terminal(
  command='cd /path && env -i HOME="$HOME" PATH="$PATH" TERM="$TERM" claude -p "..." --max-turns 20 --output-format json > /tmp/out.json 2>&1',
  background=true,
  notify_on_complete=true,
  workdir='/path'
)
```

Pitfall: Hermes background processes need an existing `workdir`. If you get `FileNotFoundError: '/Users/tylermartin/.hermes/profiles/skippy/workspace'` (wrong user or missing profile dir), pass an explicit `workdir` parameter pointing to a real directory.

## Verification

```
env -i HOME="$HOME" PATH="$PATH" TERM="$TERM" claude -p "say hi in 3 words" --max-turns 1
```
Should return a short greeting, not a 401.

## When env scrubbing doesn't fix it (Hermes terminal specifically)

Observed on Tyler's machine (`wonderbookneo`, macOS 26.x), April 2026:
- Hermes `terminal()` had NO `ANTHROPIC_API_KEY` in env (verified with `env | grep -i anthropic` → empty).
- `~/.claude/.credentials.json` valid, OAuth scopes include `user:inference`, expires ~365 days out.
- Tyler's normal iTerm shell: `claude -p "hi"` works.
- Hermes terminal subprocess (running as user `wondermonkey`): same command 401s every time.
- `claude doctor` hangs to 30s timeout.
- Even `env -i HOME=$HOME PATH=$PATH ...` (the "nuclear" fix above) returned 401 in background mode while working in foreground.

Root cause is unconfirmed but isolating signals point to one of:
- macOS Keychain / session credential broker the CLI consults that requires the calling process to share the user's GUI login session (Hermes subprocess may not).
- Some second auth handshake the CLI does (Anthropic OAuth refresh against a dependent service) blocked by sandbox/network policy in the Hermes process tree.

### Workaround: drive iTerm via osascript

When Hermes terminal can't get Claude CLI to authenticate, use Tyler's iTerm where OAuth works. AppleScript can spawn a tab, send commands, and read screen contents.

```
osascript <<'EOF'
tell application "iTerm"
  activate
  tell current window
    create tab with default profile
    tell current session
      write text "cd /path/to/project && claude -p 'task here' --max-turns 20"
    end tell
  end tell
end tell
EOF

# Wait, then read output:
sleep 5
osascript -e 'tell application "iTerm" to tell current window to tell current session to get contents'
```

For interactive prompts (e.g. `railway link`'s multi-step picker), drive each step:
```
# Type into prompt without submitting:
osascript -e 'tell application "iTerm" to tell current window to tell current session to write text "warboard" newline no'
# Press Enter:
osascript -e 'tell application "iTerm" to tell current window to tell current session to write text ""'
```

`newline no` = type without pressing Enter (useful for fuzzy-finder pickers). Empty string with default newline = press Enter.

### Why this works

iTerm runs inside Tyler's GUI session and inherits the keychain/auth state the CLI needs. Hermes subprocess does not. Until the root cause is found, iTerm is the reliable escape hatch for any CLI that depends on user-session auth (Claude CLI, Railway CLI when not yet linked, gh auth in some configs).

## Hermes repair-loop pitfall

Observed May 2026 on Skippy/Hermes: `~/.hermes/scripts/skippy_billing_proxy_repair.py` was running every 300s and, when the local Claude/OpenClaw billing proxy reported `token_expired`, it kicked `ai.hermes.gateway-skippy`. That killed live Telegram chats with `Gateway shutting down — Your current task will be interrupted` while doing nothing to refresh Claude auth.

Fix applied: the repair script now treats expired/rejected Claude OAuth as an `auth_gate` and does **not** restart the Telegram gateway when that is the reason the proxy probe failed. Do not tell Tyler to solve this by running `claude /login` in his shell as if it automatically updates Hermes' daemon/session context; first verify which HOME/keychain/session the failing process actually uses.

## Don't do

- Don't `unset ANTHROPIC_API_KEY` globally — Tyler's other tools (including Hermes itself) depend on it pointing to xAI.
- Don't edit `.zshrc`/`.zshenv` to remove the export.
- Don't run `claude auth logout` — OAuth creds are fine; the problem is env var precedence (or session auth, see above).
- Don't assume "env -i" is sufficient on Hermes — verify with a 1-turn test before kicking off a long background job.
- Don't keep restarting user-facing gateways to fix provider auth. Provider auth failures are an auth gate, not a transport failure.
