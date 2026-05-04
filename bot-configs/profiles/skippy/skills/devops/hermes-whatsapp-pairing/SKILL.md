---
name: hermes-whatsapp-pairing
description: Pair Hermes with WhatsApp via the built-in `hermes whatsapp` flow, including the credential-path quirk that breaks pairing without a symlink fix.
---

# Hermes WhatsApp Pairing

## When to use
- User asks to set up / connect / pair WhatsApp with Hermes
- WhatsApp gateway shows "not connected" or self-chat doesn't trigger the agent
- Re-pairing after `hermes whatsapp` succeeded but gateway can't see credentials

## The path mismatch (root cause)
The bridge writes credentials to the **legacy** path:
```
~/.hermes/whatsapp/session/
```
But the gateway loads them from the **per-profile** path:
```
~/.hermes/profiles/<profile>/whatsapp/session/
```
Pairing succeeds (Baileys creates `creds.json`, hundreds of `pre-key-*.json`, etc.) but the gateway sees an empty dir and silently has no WhatsApp.

## Pairing steps

1. User runs at their terminal (requires TTY — won't work over MCP/non-interactive):
   ```
   hermes whatsapp
   ```
2. Prompts:
   - Mode: `1` (separate bot number) or `2` (personal/self-chat — sends as user, can DM anyone)
   - Phone number: international format, **no `+`, no leading `00`**. e.g. `14152187667` for US, `34612345678` for ES.
3. QR appears in terminal. On phone: WhatsApp → Settings → Linked Devices → Link a Device → scan.
4. Wait for `✅ WhatsApp connected!`. Ctrl-C.

## Post-pairing fix (almost always needed)

Check both paths:
```bash
test -f ~/.hermes/profiles/<profile>/whatsapp/session/creds.json && echo OK || echo MISSING
test -f ~/.hermes/whatsapp/session/creds.json && echo LEGACY_HAS_CREDS
```

If profile path is empty but legacy has creds, symlink:
```bash
rmdir ~/.hermes/profiles/<profile>/whatsapp/session
ln -sfn ~/.hermes/whatsapp/session ~/.hermes/profiles/<profile>/whatsapp/session
```

Then restart gateway:
```bash
hermes gateway restart
```

Verify in `~/.hermes/profiles/<profile>/whatsapp/bridge.log`:
- `✅ WhatsApp connected!` = good
- `unexpected error in 'init queries'` (bad-request 400) = cosmetic Baileys noise, ignore

## Pitfalls

- **TTY required.** `hermes whatsapp` refuses pipes/PTY-less subprocesses. The agent cannot run it directly — must instruct the user.
- **Don't strip the country code.** `00` international prefix should be stripped, but the country code (1, 34, 44, etc.) stays.
- **Mode 2 ("self-chat") is misleading.** Inbound UX is via self-DMs, but outbound works to any contact. Pick mode 2 unless user has a dedicated bot number.
- **Restart gateway after symlink** — the bridge subprocess doesn't hot-reload session paths.
- **Don't patch bridge.js to dump QR payloads.** `path` and `fs` are imported at module top; the qr-handler closure has access. But avoid: it adds noise to logs and the symlink fix is faster.

## Verification
Send a WhatsApp message to yourself (self-chat mode) or to the bot number. Hermes should reply within ~5s. If silent, check `bridge.log` and gateway status.
