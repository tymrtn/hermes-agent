---
name: nagatha-comms
description: Send messages to Nagatha (Hermes agent) and receive replies. Use when Tyler asks to talk to Nagatha, ask Nagatha something, check what Nagatha is doing, coordinate with Nagatha, or relay information between agents. Triggers on "ask Nagatha", "tell Nagatha", "message Nagatha", "check with Nagatha", "DM Nagatha", "what's Nagatha doing", "coordinate with Hermes".
---

# Nagatha Comms — Skippy → Nagatha

Send messages to the Nagatha/Hermes agent and receive replies via the Hermes CLI.

## How It Works

Nagatha runs on Hermes (a separate gateway from OpenClaw). Communication uses the `hermes chat` CLI in one-shot query mode. Each call starts a fresh Hermes session with Nagatha's full context (memory, skills, tools).

## Send a Message

```bash
~/.hermes/hermes-agent/venv/bin/hermes chat -q "<message>" --quiet --max-turns 5
```

- `--quiet` suppresses banners and spinners; returns only the response
- `--max-turns 5` prevents runaway tool loops (increase for complex tasks)
- Default model and personality (Nagatha) are set in Hermes config

## Examples

### Quick check-in
```bash
~/.hermes/hermes-agent/venv/bin/hermes chat -q "What have you been working on today?" --quiet --max-turns 3
```

### Relay information
```bash
~/.hermes/hermes-agent/venv/bin/hermes chat -q "Tyler says to hold off on the SpainExpat email blast until he reviews the draft." --quiet --max-turns 2
```

### Ask for a status report
```bash
~/.hermes/hermes-agent/venv/bin/hermes chat -q "Give me a status report on all email accounts — unread count, anything urgent, any snooze items due." --quiet --max-turns 8
```

### Coordinate on a task
```bash
~/.hermes/hermes-agent/venv/bin/hermes chat -q "I'm about to send outreach emails for Redline. Check if there are any pending replies in the snooze queue from previous outreach before I send new ones." --quiet --max-turns 6
```

## Guidelines

- Always use `--quiet` to get clean output without kaomoji spinners
- Set `--max-turns` proportional to task complexity (2-3 for simple queries, 5-8 for tasks needing tool use)
- Timeout: allow 30-60 seconds for simple queries, up to 120s for complex tasks
- Nagatha's responses may include tool output noise at the start; parse for the actual reply
- If Hermes CLI is unresponsive, check process status: `ps aux | grep hermes`
- Nagatha's logs are at `~/.hermes/logs/gateway.log` for debugging

## What Nagatha Owns

- Email operations (inbox triage, snooze, replies) via Envelope CLI
- SpainExpat, Loftly, Expatriator email campaigns
- SECOM forum content mining
- Snooze/follow-up tracking in Envelope's SQLite DB

## Auth Troubleshooting (Codex/OpenAI OAuth)

Nagatha uses `openai-codex` provider via device_code OAuth. This token expires and cannot auto-refresh — it has only an `id_token`, no `refresh_token`.

### Symptoms
- Error: `Provider authentication failed: Codex token refresh failed with status 401`
- Nagatha stops responding or errors on all queries

### Diagnosis
```bash
# Check token state
python3 -c "
import json
d=json.load(open('$HOME/.hermes/auth.json'))
p = d.get('providers', {}).get('openai-codex', {})
for k,v in p.items():
    s = str(v)
    if len(s) > 60: s = s[:30] + '...' + s[-15:]
    print(f'  {k}: {s}')
"
# Look for: last_refresh date (if old = expired), auth_mode: chatgpt

# Check credential pool
hermes auth list
# Should show: openai-codex (1 credentials) → device_code oauth
```

### Fix
The `hermes login` command was removed. Use `hermes setup model` to re-run the OAuth device code flow. This requires Tyler to visit a URL in his browser and authorize.

```bash
hermes setup model
# Select openai-codex provider, follow the device code flow
```

### Key paths
- Token storage: `~/.hermes/auth.json` → `providers.openai-codex`
- Credential pool: `hermes auth list` shows all providers
- Config: `~/.hermes/config.yaml` (first line: `provider: openai-codex`)
- Nagatha workspace: `~/.openclaw/workspace-nagatha/`

### Pitfall
- `hermes login --provider openai-codex` no longer works (removed in v0.8+)
- The token only has `id_token` + `auth_mode: chatgpt`, no refresh token — expiry is inevitable
- Nagatha is NOT in the OpenClaw agents list — she's the Hermes default agent. Don't look for her in `openclaw.json`.

## What Skippy Should NOT Delegate to Nagatha

- Coding tasks (use Claude Code / Codex)
- Aposema patent or IP work
- OpenClaw configuration changes
- Anything requiring OpenClaw tools (message, sessions, etc.)
