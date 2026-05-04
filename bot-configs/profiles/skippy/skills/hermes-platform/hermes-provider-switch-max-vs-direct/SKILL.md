---
name: hermes-provider-switch-max-vs-direct
description: Switch a Hermes profile between direct Anthropic API billing and the local Claude Max billing proxy. Covers how to tell which is active, where the knob lives, and the custom_providers vs model.provider distinction that makes this non-obvious.
tags: [hermes, anthropic, billing, claude-max, proxy, cost]
triggers: ["extra usage billing", "am I on max plan", "switch to max proxy", "direct anthropic vs proxy", "which provider is skippy using", "stop burning api credits"]
---

# Hermes provider switch — Max proxy vs direct Anthropic

Hermes profiles can route Claude traffic through either:

- **`anthropic`** — direct Anthropic API, billed per-token on the API key. Expensive for Opus.
- **`claude-max-proxy`** — local billing proxy at `http://127.0.0.1:18801/v1` that rides the user's Claude Max subscription. Free under plan.

The knob is in `~/.hermes/profiles/<profile>/config.yaml`. It's easy to get wrong because defining the proxy as a `custom_providers` entry does NOT make it active — `model.provider` must also point to it.

## 1. Diagnose which provider is actually active

```bash
# Is the proxy even running?
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:18801/ --max-time 3
lsof -iTCP:18801 -sTCP:LISTEN -n | head
launchctl list | grep -i billing    # look for ai.hermes.openclaw-billing-proxy
```

```bash
# Which provider is the profile pointed at?
grep -A3 "^model:" ~/.hermes/profiles/<profile>/config.yaml
```

Look for the top-level `model:` block:

```yaml
model:
  default: claude-opus-4-7
  provider: anthropic           # <-- direct API, billed per token
```

vs

```yaml
model:
  default: claude-opus-4-7
  provider: claude-max-proxy    # <-- rides Max subscription
```

**The proxy being defined under `custom_providers` is not enough.** If `model.provider` is `anthropic`, you're on direct billing even though the proxy is running and healthy.

## 2. Verify the custom provider is wired

```bash
grep -B1 -A4 "claude-max-proxy" ~/.hermes/profiles/<profile>/config.yaml
```

Should look roughly like:

```yaml
custom_providers:
- name: claude-max-proxy
  models:
  - claude-opus-4-7
  - claude-sonnet-4-6
  base_url: http://127.0.0.1:18801/v1
```

If missing, the switch below won't work — the proxy has to be defined under `custom_providers` first.

## 3. Switch to Max proxy

Use `patch` (preferred) to change only the provider line:

```
old_string:
model:
  default: claude-opus-4-7
  provider: anthropic

new_string:
model:
  default: claude-opus-4-7
  provider: claude-max-proxy
```

Don't touch `api_mode: anthropic_messages` — that stays the same; the proxy speaks the Messages API.

## 4. Apply the change

The change takes effect on the **next turn** (or next gateway restart). The current turn finishes on whatever provider was active when it started. If you want an immediate cutover, bounce the gateway:

```bash
# find the gateway pid
cat ~/.hermes/profiles/<profile>/gateway.pid
# restart via whatever the profile's launchd label is, or just let the next turn pick it up
```

## 5. Switch back to direct API

Reverse: `provider: claude-max-proxy` → `provider: anthropic`. Useful when:

- Debugging the billing proxy itself (don't route through the thing you're debugging)
- Running benchmarks that need stable latency (the proxy adds a hop)
- Max plan is rate-limited and you need to push through with API credits

## Gotchas

- **Defining ≠ selecting.** Adding `claude-max-proxy` under `custom_providers` is necessary but not sufficient. Always grep `model.provider` to confirm.
- **Proxy health ≠ profile using it.** The proxy can be running (pid listed by `lsof`, `launchctl list` shows it loaded) while every profile still hits the direct API. Check each profile separately.
- **Multiple profiles, different providers.** Skippy, Nagatha, Klasificados, Spanorama, etc. each have their own `config.yaml`. Audit each one independently.
- **Don't set `base_url` under the top-level `model:` block.** The `base_url` lives under the matching `custom_providers` entry; `model.provider` just references it by name.
- **`api_mode` stays `anthropic_messages`.** The proxy translates Messages API → Max backend. Don't flip it to `openai_chat_completions`.
- **Billing proxy gotchas** are in separate skills — see `skippy-billing-proxy-new-mac-recovery`, `claude-keychain-proxy-repair`, `openclaw-billing-proxy`.

## Related

- `hermes-anthropic-oauth-compat` — OAuth/Claude Code emulation plumbing
- `billing-proxy-auth-sync` — repairing Max auth inside the proxy
- `openclaw-anthropic-auth-repair` — rotating Anthropic auth when the proxy breaks
