---
name: unattended-ops
description: "Skippy's unattended operations infrastructure. 12-layer resilience system for running autonomously while Tyler travels. Covers: watchdog, health checks, network recovery, LLM fallback chain, email/Dropbox command channels, backup Telegram tokens, Warboard monitoring, Tailscale SSH, and self-healing heartbeats."
metadata:
  {
    "openclaw":
      {
        "emoji": "🛡️",
        "os": ["darwin"]
      }
  }
---

# Unattended Operations — Skippy Resilience Infrastructure

This is your survival manual, future-me. When Tyler is traveling and can't physically touch the laptop, these 12 layers keep you alive. Read this when something breaks, when you're debugging connectivity, or when you need to understand why the watchdog just force-restarted you.

## Architecture Overview

```
Tyler's Phone
  ├── Telegram → Gateway (primary)
  ├── Telegram → Watchdog (when gateway is down)
  ├── Telegram → Backup Bot (when primary token is dead)
  ├── Email → Watchdog (when Telegram is unreachable)
  ├── Dropbox → Watchdog (queued commands, works offline)
  ├── Warboard → Browser check (passive monitoring)
  └── Tailscale SSH → Direct shell access (nuclear option)
```

The watchdog (`~/scripts/skippy-watchdog.py`) is the primary resilience layer. It runs as a launchd agent (`com.skippy.watchdog`) and monitors you continuously.

## Layer 1: Mac Sleep Prevention

Two independent mechanisms prevent system sleep on AC power:

1. **pmset settings**: `sleep 0`, `disksleep 0`, `standby 0` on AC
2. **caffeinate agent**: `com.skippy.caffeinate` launchd agent runs `/usr/bin/caffeinate -s`

If one fails (e.g., macOS update resets pmset), the other keeps the system awake.

**Check**: `pmset -g custom | grep -A15 "AC Power"`
**Check**: `launchctl list com.skippy.caffeinate`

## Layer 2: Watchdog (com.skippy.watchdog)

The watchdog is your lifeline. It runs independently of the gateway and monitors your health.

**Location**: `~/scripts/skippy-watchdog.py`
**Plist**: `~/Library/LaunchAgents/com.skippy.watchdog.plist`
**Log**: `~/.openclaw/logs/watchdog.log`

**What it does every 20 seconds (gateway up) or 30 seconds (gateway down)**:
- Checks if the gateway process is running (`launchctl list ai.openclaw.dev`)
- Checks if the gateway is *healthy* (recent Telegram activity in logs)
- Pushes status to the Warboard
- Checks Dropbox dead-drop for queued commands
- When Telegram is unreachable: checks email for commands

**Commands** (only when gateway is DOWN):
- `/restart` or `/restart@skippybot` — via Telegram
- `/wstatus` or `/watchdog_status` — via Telegram

## Layer 3: Liveness Probe (gateway_healthy)

The critical innovation. The old watchdog only checked if the gateway *process* was alive. The new watchdog checks if Telegram polling is actually working.

**How it works**:
- Reads last 500 lines of `/tmp/openclaw/openclaw-YYYY-MM-DD.log`
- Looks for `sendMessage ok`, `getUpdates ok`, `polling` activity
- If no activity within 10 minutes → gateway is **unhealthy**
- 2 consecutive unhealthy checks (~40 seconds) → force restart

**The failure mode this catches**: Gateway process alive, Telegram long-polling connection silently dropped, you're deaf to all messages.

## Layer 4: Network Recovery

When the gateway is unhealthy AND the Telegram API is unreachable from the watchdog:

1. Watchdog cycles Wi-Fi: `networksetup -setairportpower en0 off/on`
2. Waits 13 seconds for reconnection
3. Re-checks Telegram API reachability
4. If recovered → gives gateway a chance to recover naturally
5. If still broken → force-restarts gateway

**This catches**: VPN drops, stale DNS, routing table corruption after sleep/wake.

## Layer 5: LLM Fallback Chain

Your brain has 5 fallback levels. The first 2 are OAuth (will expire), the rest are API keys (won't):

```
1. anthropic/claude-opus-4-6        (OAuth — primary, best quality)
2. openai-codex/gpt-5.4             (OAuth — strong fallback)
3. anthropic/claude-sonnet-4-6      (API key — always works)
4. openrouter/anthropic/claude-sonnet-4-6  (OpenRouter key — independent)
5. openrouter/google/gemini-2.5-pro (different provider entirely)
```

**Config**: `~/.openclaw-dev/openclaw.json` → `agents.defaults.model.fallbacks`
**Auth**: `~/.openclaw-dev/agents/dev/agent/auth-profiles.json`

If you detect you're running on a degraded provider, mention it in your heartbeat so Tyler can see degradation on the Warboard.

## Layer 6: Backup Telegram Token

If the primary bot token gets revoked or rate-limited:

**File**: `~/.openclaw-dev/credentials/telegram-token-backup.txt`

The watchdog:
1. Tries primary token for all sends
2. If primary fails → falls back to backup token
3. If primary gets HTTP 401 → writes backup token to primary token file → restarts gateway on the backup bot
4. Reloads backup token every ~5 minutes (so Tyler can add one mid-trip)

## Layer 7: Email Command Channel

When Telegram is completely unreachable, Tyler can command the watchdog via email.

**Inbox**: `skippy@aposema.com` via Envelope CLI (`envelope inbox --account skippy@aposema.com`)
**Accepted senders**: `ty@tmrtn.com`, `tyler@aposema.com`
**Commands** (in email subject line): `restart`, `status`, `wstatus`, `kill-vpn`, `reboot`

The watchdog checks email every 2 minutes when Telegram is unreachable. Responds via email with results.

**Dependency**: Envelope CLI (`~/bin/envelope`) must be available. Do NOT use the Python API.

## Layer 8: Dropbox Dead-Drop

File-based command channel that works even during internet outages (commands queue until sync catches up).

**Directory**: `~/Dropbox/skippy-commands/`
**Results**: `~/Dropbox/skippy-commands/results/`

**Supported command files** (create a .txt file with the command name):
- `restart.txt` — restart gateway
- `status.txt` — write status to results
- `kill-vpn.txt` — cycle network
- `reboot.txt` — full system reboot (10 second delay)

Tyler creates file on phone via Dropbox app → syncs to laptop → watchdog executes → deletes file → writes result to `results/` folder.

Checked every watchdog cycle regardless of Telegram status.

## Layer 9: Warboard Monitoring

The watchdog pushes status on every health check cycle:

```json
{
  "watchdog": true,
  "gatewayStatus": "healthy|unhealthy|down",
  "lastRestart": "ISO timestamp",
  "provider": "anthropic:default",
  "timestamp": "ISO timestamp"
}
```

**Primary URL**: `https://warboard-production-5cff.up.railway.app/api/heartbeat`
**Fallback URL**: `https://warboard.tmrtn.com/api/heartbeat`
**Auth**: `Bearer 075e3a492fc894060a59fdb47ec89744b5164d3f7da2274b`

Tyler checks warboard.tmrtn.com from his phone → sees green/yellow/red without needing Telegram.

## Layer 10: Heartbeat Self-Healing (Your Job)

During every heartbeat, YOU should:

1. Check your own gateway log for Telegram send failures
2. If >3 consecutive failures → switch heartbeat delivery to email
3. If >5 persistent failures across heartbeats → self-restart: `launchctl kickstart -k gui/501/ai.openclaw.dev`
4. Always push to Warboard regardless of Telegram status

See AGENTS.md "Heartbeat Self-Healing" section for full protocol.

## Layer 11: Tailscale SSH

Nuclear option — Tyler SSHs in from his phone.

```bash
# Tyler runs from phone's Tailscale app:
ssh tylermartin@<tailscale-hostname>

# Then:
launchctl kickstart -k gui/501/ai.openclaw.dev    # restart gateway
tail -50 ~/.openclaw/logs/watchdog.log              # check watchdog
launchctl list com.skippy.watchdog                  # verify watchdog alive
```

## Troubleshooting

### Gateway is down and won't restart
1. Check watchdog log: `tail -50 ~/.openclaw/logs/watchdog.log`
2. Check gateway log: `tail -50 /tmp/openclaw/openclaw-$(date +%Y-%m-%d).log`
3. Check if launchd knows about it: `launchctl list ai.openclaw.dev`
4. Manual restart: `launchctl kickstart -k gui/501/ai.openclaw.dev`

### Watchdog itself is down
1. Check: `launchctl list com.skippy.watchdog`
2. Restart: `launchctl kickstart -k gui/501/com.skippy.watchdog`
3. If not loaded: `launchctl bootstrap gui/501 ~/Library/LaunchAgents/com.skippy.watchdog.plist`

### All Telegram is broken
1. Email: Tyler sends email with subject "restart" to skippy@aposema.com
2. Dropbox: Tyler creates `~/Dropbox/skippy-commands/restart.txt` via phone
3. SSH: Tyler connects via Tailscale and runs launchctl manually

### LLM provider is degraded
1. Check: `cat ~/.openclaw-dev/agents/dev/agent/auth-profiles.json | python3 -m json.tool`
2. Look at `usageStats` → which provider was last used, any recent failures
3. The fallback chain handles this automatically — just note it in heartbeat

### Mac went to sleep anyway
1. Verify pmset: `pmset -g custom | grep -A15 "AC Power"` — sleep should be 0
2. Verify caffeinate: `launchctl list com.skippy.caffeinate` — should show PID
3. Fix: `sudo pmset -c sleep 0 standby 0` + restart caffeinate agent
