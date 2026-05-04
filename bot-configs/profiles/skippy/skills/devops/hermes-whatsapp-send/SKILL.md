---
name: hermes-whatsapp-send
description: Send WhatsApp messages from Hermes by POSTing directly to the local Baileys bridge HTTP API. Use when you need Skippy to actually deliver a message (customer service dispute, outreach, reminder) — not just draft text for Tyler to paste. Companion to hermes-whatsapp-pairing.
tags: [whatsapp, messaging, hermes, bridge, baileys]
triggers: ["send whatsapp", "message movistar", "whatsapp them", "text on whatsapp", "whatsapp the autoescuela"]
---

# Hermes WhatsApp — direct send via bridge

## When to use

Tyler asks you to send a WhatsApp message to someone (a business, a contact, himself). Hermes has no `whatsapp-send` CLI and no `send_message` target for arbitrary WhatsApp numbers — but the bridge itself exposes an HTTP API on localhost that works right now.

Do NOT assume you need to "upgrade the plugin" or wait for a new tool. This works today.

## Core fact

The Baileys bridge (`~/.hermes/hermes-agent/scripts/whatsapp-bridge/bridge.js`) runs as a child of `hermes gateway` and listens on `127.0.0.1:3000`. Endpoints:

- `GET  /health` — returns `{status, queueLength, uptime}`
- `POST /send` — body `{chatId, message, replyTo?}`
- `POST /send-media` — body `{chatId, filePath, mediaType?, caption?, fileName?}`
- `POST /edit` — body `{chatId, messageId, message}`
- `POST /typing` — body `{chatId}`
- `GET  /messages` — **drain-on-read** queue of recent events (emptied every call). Does NOT accept `chatId` filter — any query params are ignored and it returns the full queue or `[]`.
- `GET  /chat/:id` — chat info (name, isGroup, participants). NOT history.
- `GET  /chats?limit=N` — recent chats list (may 404 on older bridge builds; check `grep "app.get" bridge.js` to confirm it's registered).

Default port: 3000. Override by checking the listening port:
```bash
lsof -iTCP -sTCP:LISTEN -P | grep -i node | grep -v 'LISTEN)$' | awk '{print $9}'
```

## chatId format

WhatsApp JIDs, not E.164 phone numbers:

- Individual: `<countrycode><number>@s.whatsapp.net` — no `+`, no spaces, no leading `00`.
  - Spain `638 10 1004` → `34638101004@s.whatsapp.net`
  - US `+1 415 555 0100` → `14155550100@s.whatsapp.net`
- Group: `<groupid>@g.us` (get from `/messages` or `/chat/:id`)
- Self (mode 2 pairing): check `sock.user.id` in bridge logs, or send to your own number JID

## Pre-send checks

1. Bridge alive?
   ```bash
   curl -s http://127.0.0.1:3000/health
   # {"status":"connected", ...}  → good
   # connection refused           → gateway not running; `hermes gateway restart`
   # "status":"disconnected"      → WhatsApp session dropped; repair with hermes-whatsapp-pairing
   ```

2. Gateway is the parent — don't kill it. Just POST to the port.

## Send pattern

```bash
curl -s -X POST http://127.0.0.1:3000/send \
  -H 'Content-Type: application/json' \
  -d '{
    "chatId": "34638101004@s.whatsapp.net",
    "message": "Buenas tardes..."
  }'
```

Response on success: `{"success": true, "messageId": "..."}`.

For multi-line bodies with special characters, write the JSON to a file and use `-d @path` to avoid shell-quoting hell.

## Rules before sending

1. **Confirm with Tyler before firing** if the recipient is a business, legal counterpart, or anyone where tone matters. Show the final text; use `clarify` when available, fall back to A/B/C/D letters.
2. **Do not send DNI/NIE, card numbers, or other sensitive identifiers in the opening message** when writing to a company bot queue — hold them until a human agent takes the chat.
3. **One message, not five.** Consolidate into a single numbered list instead of spamming.
4. **No em dashes** (Tyler's standing preference). Use hyphens or periods.
5. **CC the record.** After sending, log who/when/what in FOLLOWUPS.md or the relevant skill so the thread is traceable.

## Recipient authorization (the "not approved" gotcha)

If the recipient is not on Hermes' approved-users list for WhatsApp, the gateway will **auto-reply to them** on your behalf with: _"This WhatsApp thread is not approved yet. Ask Tyler to approve this phone number for service conversations."_ This happens in `gateway/run.py` (~line 2714) via `whatsapp_service_policy` when `service_conversations` is enabled. It will fire on every inbound message from that number until approved — including Tyler's wife, friends, etc. This is noisy and embarrassing.

**Before sending to a new recipient, add them to the approved list.** PairingStore reads the file on every check — no gateway restart needed.

Approved list path (profile-aware):
```bash
cd ~/.hermes/hermes-agent && source venv/bin/activate
python3 -c "from gateway.pairing import PAIRING_DIR; print(PAIRING_DIR)"
# → ~/.hermes/profiles/<profile>/platforms/pairing
```

Add a user:
```bash
APPROVED=~/.hermes/profiles/skippy/platforms/pairing/whatsapp-approved.json
mkdir -p "$(dirname "$APPROVED")"; [ -f "$APPROVED" ] || echo '{}' > "$APPROVED"
python3 <<'PY'
import json, time, pathlib
p = pathlib.Path.home() / '.hermes/profiles/skippy/platforms/pairing/whatsapp-approved.json'
data = json.loads(p.read_text()) if p.stat().st_size else {}
data['34664895953@s.whatsapp.net'] = {'user_name': 'Yuliya Martin', 'approved_at': time.time()}
p.write_text(json.dumps(data, indent=2, ensure_ascii=False))
PY
```

Use the JID form (`<digits>@s.whatsapp.net`) — same format as `chatId` for sending. The authorization check in `_is_user_authorized` also expands phone↔LID aliases via session-dir mapping files, so the phone JID is sufficient even if the sender arrives as an `@lid`.

**Warning:** adding someone to `whatsapp-approved.json` gives them full bot access (they can command it), not just "stop auto-replying to them." For anything more nuanced (silent-ignore, approved service chat without command access), use the `service_conversations` config block in `config.yaml` instead — `approved_chats` lets the bot converse without elevating the user to operator.

## Common pitfalls

- **Don't strip country code.** `34` stays for Spain, `1` for US/CA, etc. Only the `+` and any `00` international prefix are dropped.
- **Bridge down silently after a network blip.** Always curl `/health` first; `"status":"disconnected"` means pairing needs repairing (see `hermes-whatsapp-pairing`).
- **`hermes whatsapp` is pairing only.** It refuses non-TTY invocation and doesn't send messages. Don't try to pipe a message into it.
- **`send_message` tool lists Telegram targets only.** Do not wait for a WhatsApp target to appear there — the direct HTTP path is the supported path.
- **Group IDs are not phone numbers.** Need to pull from `/messages` or existing chat metadata first.

## Useful reference targets (Spain)

| Business | WhatsApp number | chatId |
|---|---|---|
| Movistar atención al cliente | 638 10 1004 (9–22h) | `34638101004@s.whatsapp.net` |
| Digi atención (primary: tel 1200) | no official WhatsApp as of 2026-04 | — use phone/chat |

Verify current numbers via web search before sending — companies rotate these.

## Verification

After send, `GET /messages?limit=10` may include your outbound event — but see the caveats below about the drain-on-read queue and the inbound filter.

## Receiving replies — important gotchas

**`/messages` is drain-on-read.** Every `GET /messages` call empties the queue (bridge.js uses `messageQueue.splice(0, messageQueue.length)`). If the gateway is running it consumes events continuously, so by the time you curl, the response is almost always `[]`. An empty response does NOT mean "no reply."

**Inbound filter excludes non-listed senders.** bridge.js (~line 249) checks the sender against the list configured via the Hermes gateway env. If the sender isn't on it, the message is discarded before reaching the queue. Tyler's default list only contains his own number, so inbound from businesses (Movistar, autoescuelas, etc.) is not preserved. This is the #1 reason a "no reply" verdict is wrong.

### How to actually check for a reply

1. **Ask Tyler to check WhatsApp on his phone.** This is the authoritative source. Always recommend this first when the answer matters (legal disputes, evidence gathering, etc.).
2. **Grep the bridge log for upsert events** from the sender number (only useful if `WHATSAPP_DEBUG=1` is configured):
   ```bash
   grep -E "upsert.*<number>" ~/.hermes/profiles/<profile>/whatsapp/bridge.log
   ```
   Without debug mode, filtered-out messages leave no trace.
3. **Check the session dir** for lid-mapping / device-list files for the sender — this confirms the handshake (our send was delivered) but says nothing about replies:
   ```bash
   ls ~/.hermes/profiles/<profile>/whatsapp/session/ | grep <number>
   ```
4. **Check gateway logs** for any relay of that number into the self-chat (only works if the sender was on the inbound list):
   ```bash
   grep -i "<number>\|<business name>" ~/.hermes/logs/gateway.log
   ```

### To capture replies going forward

The sender number has to be added to the Hermes gateway's WhatsApp inbound list, then the gateway restarted so the bridge picks it up.

**Always get Tyler's explicit approval before proposing this change** — it alters the trust boundary of the bot. Describe the change, show the before/after, wait for "yes."

### Reporting a "no reply" verdict honestly

When the queue is empty and logs are clean, do NOT say "Movistar didn't reply." Say: "No reply visible in the Hermes pipeline, but the inbound filter may have excluded it — please check WhatsApp on your phone to confirm." State this caveat every time until the sender list is updated.
