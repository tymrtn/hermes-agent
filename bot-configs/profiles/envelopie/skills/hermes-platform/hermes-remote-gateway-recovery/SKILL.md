---
name: hermes-remote-gateway-recovery
description: Recover and diagnose Hermes gateway/profile migrations on another Mac over Tailscale SSH, especially after launchd reloads leave bots silent or Telegram polling conflicts appear.
version: 1.0.0
author: Envelopie
license: MIT
metadata:
  hermes:
    tags: [hermes, gateway, launchd, tailscale, ssh, macos, migration, recovery]
    related_skills: [hermes-agent, tailscale-serve-local-dashboard, hermes-profile-path-isolation, systematic-debugging]
---

# Hermes Remote Gateway Recovery over Tailscale SSH

Use this when Tyler says a bot stopped responding after being moved/ported to another Mac, or when a Hermes gateway/profile migration over Tailscale partly succeeded but some bots are silent.

## Principles

- Diagnose before restarting everything. Multiple gateways using the same Telegram bot token will fight over `getUpdates` and produce polling conflicts.
- Do not print secrets. Redact token/password/API-key/cookie/authorization values and email addresses in logs.
- On macOS, LaunchAgents may show a running PID with a previous negative exit code; read logs for actual state.
- During a live migration, decide which machine owns each Telegram bot token before enabling the whole gang.

## Tailscale SSH access pattern

1. Check the node is visible and reachable:

```bash
tailscale status | grep -F '<100.x.y.z>' || true
tailscale ping --timeout=5s --c 3 <100.x.y.z>
```

2. If `tailscale ssh` fails on host-key strict checking, use ordinary SSH to accept/check the host key and an explicit key/user. Example that worked for Tyler's MacBook Pro:

```bash
ssh-keyscan -T 10 -t ed25519 <host>.ts.net
ssh -o StrictHostKeyChecking=accept-new <host>.ts.net 'echo ok'
ssh -o BatchMode=yes \
  -o ConnectTimeout=10 \
  -o StrictHostKeyChecking=yes \
  -o IdentitiesOnly=yes \
  -i /Users/wondermonkey/.ssh/wonderbookneo_ed25519 \
  tylermartin@<host>.ts.net 'printf "user=%s host=%s home=%s\n" "$(whoami)" "$(hostname)" "$HOME"'
```

3. If ordinary SSH reaches the box but says permission denied, try the known real user/key combinations before assuming the node is unreachable. In the observed case, `tylermartin` with `wonderbookneo_ed25519` worked while other usernames did not.

## Remote triage commands

Run read-only probes first:

```bash
ssh ... '
set +e
printf "remote user=%s host=%s home=%s pwd=%s\n" "$(whoami)" "$(hostname)" "$HOME" "$PWD"

printf "\nHermes processes:\n"
ps auxww | egrep -i "nagatha|hermes|openclaw|claude|codex|tailscale ssh|ssh .*wondermonkey|rsync|scp" \
  | egrep -v "egrep|grep" \
  | sed -E "s/(token|api[_-]?key|password|secret)=([^ ]+)/\\1=[REDACTED]/gi" \
  | head -80

printf "\nLaunchAgents:\n"
for f in ~/Library/LaunchAgents/ai.hermes.gateway*.plist; do [ -e "$f" ] && printf "%s\n" "$f"; done

printf "\nlaunchctl:\n"
launchctl list | grep -E "ai\\.hermes\\.gateway($|-)|hermes" || true

printf "\nprofile dirs:\n"
find ~/.hermes/profiles -maxdepth 1 -type d 2>/dev/null | sort
'
```

## Logs to inspect

LaunchAgent stdout/stderr paths are usually:

```text
~/.hermes/logs/gateway.log
~/.hermes/logs/gateway.error.log
~/.hermes/logs/agent.log
~/.hermes/profiles/<profile>/logs/gateway.log
~/.hermes/profiles/<profile>/logs/gateway.error.log
~/.hermes/profiles/<profile>/logs/agent.log
~/.hermes/profiles/<profile>/logs/errors.log
```

Tail them with redaction:

```bash
ssh ... '
for f in ~/.hermes/logs/gateway.log ~/.hermes/logs/gateway.error.log ~/.hermes/logs/agent.log ~/.hermes/profiles/*/logs/*.log; do
  [ -f "$f" ] || continue
  echo "===== $f ====="
  tail -80 "$f" \
    | sed -E "s/(token|api[_-]?key|password|secret|authorization|cookie)([=: ]+)([^ ]+)/\\1\\2[REDACTED]/gi" \
    | sed -E "s/[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\\.[A-Za-z]{2,}/[EMAIL]/g"
done
'
```

Important log signatures:

- `Received SIGTERM/SIGINT — initiating shutdown`: a gateway was killed, often by a launchd reload.
- `Connected to Telegram (polling mode)` and `Gateway running with 1 platform(s)`: gateway is alive.
- `Conflict: terminated by other getUpdates request`: two processes/machines are polling the same Telegram bot token.
- `httpx.ConnectError` / `ReadTimeout`: network/Tailscale/VPN/DNS issue, not necessarily Hermes config.
- `Invalid authentication credentials`: provider credential issue.
- Anthropic `temperature is deprecated` or `tool_use ids without tool_result`: model/client compatibility or malformed transcript issue; do not misdiagnose as gateway offline.

## LaunchAgent inspection

Check whether launchd has each gateway loaded:

```bash
ssh ... '
for label in ai.hermes.gateway ai.hermes.gateway-skippy ai.hermes.gateway-klasificados ai.hermes.gateway-nagovernor ai.hermes.gateway-envelopie ai.hermes.gateway-spanorama ai.hermes.gateway-scorandum ai.hermes.gateway-rocinante; do
  echo "===== $label ====="
  launchctl print "gui/$(id -u)/$label" 2>&1 \
    | egrep "state =|last exit code|pid =|program =|path =|reason =|runs =|KeepAlive|spawn type|domain =" \
    | sed -n "1,80p"
done
'
```

## Safe recovery: restart one gateway, not the whole gang

If the main gateway is dead and you need to restore user contact quickly:

```bash
ssh ... '
label=ai.hermes.gateway
pl="$HOME/Library/LaunchAgents/$label.plist"
plutil -lint "$pl" || exit 1
launchctl bootstrap "gui/$(id -u)" "$pl" 2>&1 || true
launchctl kickstart -k "gui/$(id -u)/$label" 2>&1 || true
sleep 5
launchctl list | grep -E "ai\\.hermes\\.gateway($|-)|hermes" || true
tail -40 ~/.hermes/logs/agent.log
'
```

Verify with:

```bash
grep -E "Connected to Telegram|Gateway running|Received SIGTERM|Telegram polling conflict" ~/.hermes/logs/agent.log | tail -10
```

## Migration-specific caution

When porting Hermes profiles between machines, do **not** blindly bootstrap every copied `ai.hermes.gateway-*.plist`. Start with the main gateway or the one bot the user needs, then check conflicts. If old and new machines both poll the same Telegram token, both will appear flaky.

Recommended sequence:

1. Confirm old-machine ownership of each bot token.
2. Stop the old gateway for a profile.
3. Start the new gateway for that profile.
4. Verify `Connected to Telegram` and absence of repeated polling conflicts.
5. Repeat profile-by-profile.
