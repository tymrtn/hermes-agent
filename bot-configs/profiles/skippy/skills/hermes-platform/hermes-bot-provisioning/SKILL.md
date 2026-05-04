---
name: hermes-bot-provisioning
description: End-to-end provisioning of a new Hermes-native bot profile (Telegram + SOUL + skills + shared API creds). Use when Tyler asks to spin up a new named bot/persona on Hermes.
---

# Hermes Bot Provisioning

Canonical sequence for standing up a new Hermes-native bot profile so nothing gets skipped.

## Prerequisites
- Hermes installed; Skippy's profile healthy (use as clone source).
- Tyler's Telegram chat ID (numeric — already in Skippy's allowed_users: 6493121275).
- BotFather token from Tyler for the new bot.
- Optional: skill-specific API creds (Alpaca, X, etc.).

## Sequence

### 1. Create profile (clone from Skippy)
```bash
cp -R ~/.hermes/profiles/skippy ~/.hermes/profiles/<botname>
```
Edit `~/.hermes/profiles/<botname>/config.yaml`:
- `profile.name: <botname>`
- Telegram allowed_users (keep Tyler 6493121275)
- Bump port if needed (Skippy=18801, increment from there)

### 2. Write SOUL
Replace `~/.hermes/profiles/<botname>/SOUL.md` with persona-specific identity. Skippy's SOUL is the template — keep operating discipline section, swap voice/identity/north-star.

### 3. Telegram token
```bash
# Verify token works
curl -s "https://api.telegram.org/bot<TOKEN>/getMe" | jq

# Store in Keychain
security add-generic-password -s telegram-bot-token -a <botname> -w '<TOKEN>' -U

# Add to profile .env
echo "TELEGRAM_BOT_TOKEN=<TOKEN>" >> ~/.hermes/profiles/<botname>/.env
```

### 4. Install skills
```bash
cp -R ~/.hermes/profiles/skippy/skills/<category>/<skill> \
      ~/.hermes/profiles/<botname>/skills/<category>/
```
Base toolkit (apple, devops, github, research, etc.) is inherited via the clone.

### 5. Shared API creds
- **X/Twitter**: `~/.config/x-cli/.env` (chmod 600) — all bots inherit. x-cli requires all 5 vars (X_API_KEY, X_API_SECRET, X_BEARER_TOKEN, X_ACCESS_TOKEN, X_ACCESS_TOKEN_SECRET) even for read-only.
- **Alpaca**: Keychain `alpaca-api-key` / `alpaca-secret`. Default to paper trading.
- **Other**: Keychain by service name; reference in profile `.env` via `security find-generic-password -s ... -a ... -w`.

### 6. Launch as launchd service
Use Hermes's gateway installer (mirrors Skippy's setup). Verify:
```bash
launchctl list | grep hermes.<botname>
tail -f ~/.hermes/profiles/<botname>/logs/gateway.log
```

### 7. Smoke test
- Send `/start` from Tyler's Telegram → expect persona-flavored greeting.
- Verify allowed_users gate (try from another account → should reject).
- Test one skill end-to-end.

## Pitfalls
- Update `profile.name` in config.yaml after clone — affects logs/launchd label.
- Telegram bot tokens are per-bot; never reuse across profiles.
- Skills must be copied, not symlinked (Hermes walks the dir).
- After SOUL changes, restart the gateway (launchctl kickstart) so the new prompt is in scope.
- X API free tier: 100 reads/mo total across ALL bots sharing the creds — heavy sentiment scraping will burn it fast.

## Provisioned bots (as of 2026-05-01)
- Skippy (Tyler's primary)
- 🪲 Scorandum (@scorandumbot) — Jeraptha bookie, 10x/12mo mandate, skills: polymarket, kalshi-trade, xitter
- 🛩️ Rocinante (@rocinant3bot) — travel/logistics, skills: find-nearby, tripgenie, tour-planner, travel-agent-joi
