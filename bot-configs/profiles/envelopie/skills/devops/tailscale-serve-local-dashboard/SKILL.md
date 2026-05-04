---
name: tailscale-serve-local-dashboard
description: Expose a local web dashboard (e.g., Envelope on localhost:3141) to other devices on the tailnet via Tailscale Serve — HTTPS, tailnet-only, not public. Use when the user wants to reach a local dev UI from another Mac/phone/laptop without ngrok or public tunnels.
---

# Tailscale Serve for local dashboards

Use `tailscale serve` (NOT `funnel` — funnel is public internet). Serve exposes a localhost port over HTTPS within your tailnet only, using a Tailscale-issued cert.

## When to use

- User wants to access local dashboard (Envelope, Jupyter, Grafana, etc.) from a second device on the same tailnet
- User specifically asked about Tailscale tunneling
- Telegram/Slack localhost URL not clickable and user wants mobile access (see pitfalls)

## Prerequisites

- Tailscale installed and logged in on the host machine (`tailscale status` works)
- Target client devices are on the same tailnet
- MagicDNS enabled in tailnet admin (required for `.ts.net` hostnames to resolve on clients)

## Steps

1. **Confirm the app is listening locally.**
   ```
   lsof -iTCP:PORT -sTCP:LISTEN
   ```
   Localhost-only binding is fine — Tailscale proxies from the host side.

2. **Start Serve.**
   ```
   tailscale serve --bg --https=443 http://localhost:PORT
   ```
   - `--bg` persists across reboots
   - `--https=443` uses standard HTTPS port with auto-cert
   - Use `--https=8443` if 443 is already claimed

3. **If Serve isn't enabled on the tailnet, you'll get a URL like:**
   ```
   https://login.tailscale.com/f/serve?node=<NODEID>
   ```
   Send that to the user to click and approve (one-time tailnet admin toggle). Then re-run step 2.

4. **Grab the public-within-tailnet URL:**
   ```
   tailscale serve status
   ```
   Outputs something like `https://<hostname>.<tailnet>.ts.net`

5. **Share the URL with label format:**
   ```
   [Envelope dashboard (Tailscale)](https://wonders-macbook-1.tail87a011.ts.net)
   ```

## Verification

- From the host: `tailscale serve status` should show the proxy mapping
- From another tailnet device: hit the `.ts.net` URL in a browser, should load the dashboard with a valid HTTPS cert

## Restarting Tailscale on macOS

When Tyler asks to restart Tailscale on wondermonkey/macOS, verify state before and after:

```bash
printf 'status_before=\n'; tailscale status --peers=false 2>&1 || true
printf 'processes_before=\n'; pgrep -fl 'Tailscale|tailscaled' || true
osascript -e 'tell application "Tailscale" to quit' 2>/dev/null || true
sleep 3
if pgrep -fl '/Applications/Tailscale.app/Contents/MacOS/Tailscale' >/dev/null; then
  pkill -f '/Applications/Tailscale.app/Contents/MacOS/Tailscale' || true
  sleep 2
fi
open -a Tailscale
for i in $(seq 1 15); do
  tailscale status --peers=false >/tmp/tailscale-status-after.txt 2>/tmp/tailscale-status-after.err && break
  sleep 2
done
printf 'processes_after=\n'; pgrep -fl 'Tailscale|tailscaled' || true
printf 'status_after=\n'; cat /tmp/tailscale-status-after.err /tmp/tailscale-status-after.txt 2>/dev/null || true
printf 'ip_after='; tailscale ip -4 2>/dev/null || true
```

A minor `client version != tailscaled server version` warning can appear after app/daemon updates; report it, but do not treat it as failure if `tailscale status` and `tailscale ip -4` work.

## Tailscale SSH diagnostics

When asked whether this node can SSH to a tailnet IP:

```bash
tailscale status | grep -F '<100.x.y.z>' || true
tailscale ping --timeout=5s --c 3 <100.x.y.z> 2>&1 || true
tailscale whois <100.x.y.z> 2>&1 || true
tailscale ssh <100.x.y.z> 'printf "connected user=%s host=%s pwd=%s\n" "$(whoami)" "$(hostname)" "$(pwd)"' 2>&1
```

Interpretation:
- `no matching peer` / `peer not found`: wrong IP, different tailnet, expired/removed/shared-node issue, or wrong Tailscale account.
- ping works but `tailscale ssh` times out: Tailscale layer can see the node, but SSH policy/service is the blocker. Check Tailscale SSH enablement, ACLs, or macOS Remote Login if using ordinary SSH.
- ping via DERP with `direct connection not established` is still reachability, but with relay latency; mention it.

## Teardown

```
tailscale serve --https=443 off
```

Or reset everything:
```
tailscale serve reset
```

## Pitfalls

- **Serve not enabled on tailnet** — first run prompts with an approval URL. User must click it once. Command hangs until approved; kill with Ctrl-C if needed and re-run after.
- **`.ts.net` hostname doesn't resolve on client** — MagicDNS must be on in tailnet admin ([Tailscale DNS settings](https://login.tailscale.com/admin/dns)). Without it, fall back to raw tailnet IP `https://100.x.y.z/` (cert name won't match, browser warning).
- **404 on the tunnel URL** — the dashboard's root path may not be `/`. Check with `curl -sS http://localhost:PORT/` locally; if root returns 200, the tunnel will too. If user hits `/dashboard` or `/admin`, confirm the app's actual route.
- **Can't curl `.ts.net` from the host itself** — expected on some macOS setups; test from another tailnet device instead.
- **Offline client devices** show in `tailscale status` as "offline, last seen Xh ago" — they need to be online and connected to reach the URL.
- **Don't use `tailscale funnel`** unless user explicitly wants public internet exposure. Funnel opens the URL to the whole internet.

## Related quirks

- Telegram won't linkify `localhost` or `127.0.0.1` URLs regardless of markdown format — another reason Tailscale Serve is useful for mobile access to local UIs.
