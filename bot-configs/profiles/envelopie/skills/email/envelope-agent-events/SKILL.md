---
name: envelope-agent-events
description: Design and evaluate Envelope workflows that emit agent-addressable mail events for Hermes/OpenClaw/Telegram/local helpers, including OTP, triage, junk-rule, and urgency loops.
version: 1.0.0
author: Envelopie
license: FSL-1.1-ALv2
metadata:
  hermes:
    tags: [Envelope, email, agents, OTP, clipboard, Hermes, OpenClaw]
---

# Envelope Agent Events

Use this skill when discussing or planning Envelope features that turn mailbox activity into agent workflows: OTP handling, triage, junk rules, urgency/importance routing, response heuristics, notifications, or human-in-the-loop actions.

## Core framing

Do not frame OTP handling as merely “copy code to clipboard.” Treat it as the first polished demo of Envelope as a mailbox runtime that emits agent-addressable mail events and routes them to the correct surface.

Envelope core should produce structured events such as:

```json
{
  "type": "mail.otp.detected",
  "account": "tyler@example.com",
  "message_id": "...",
  "from": "security@github.com",
  "service": "GitHub",
  "code": "123456",
  "confidence": 0.97,
  "ttl_seconds": 180,
  "actions": ["copy", "done", "archive", "move_to_2fa"]
}
```

Useful event families:

- `mail.received`
- `mail.otp.detected`
- `mail.urgent.detected`
- `mail.reply_suggested`
- `mail.junk_rule_candidate`
- `mail.followup_needed`
- `mail.vendor_receipt_detected`
- `mail.human_attention_required`

## Remote-runtime rule

Clipboard and notifications belong to the active user surface, not automatically to the mailbox host.

Envelope may be running on Neo while Tyler is operating from Telegram, a phone, or a work Mac. Therefore:

1. Envelope watches mail and emits events.
2. Hermes/OpenClaw or another router receives the event.
3. Telegram/desktop helper/local webhook decides how to surface it.
4. Clipboard writes happen only on a trusted active local surface.

For Telegram, send the code formatted for tap-to-copy with action buttons such as Done, Archive, Ignore. Telegram cannot safely set the user’s clipboard directly.

## OTP extraction policy

Use deterministic OTP parsing first:

- auditable
- fast
- cheap
- works without Apple Intelligence / remote LLMs
- less likely to hallucinate

Use Apple Foundation Models, local LLMs, or remote Hermes/OpenClaw models for:

- classifying weird emails
- identifying the service/vendor
- urgency/importance triage
- junk-rule candidates
- response heuristics
- summarization and enrichment

Do not rely on a generative model as the sole OTP extractor.

## Flycut / macOS clipboard considerations

Tyler uses Flycut heavily. Flycut repo location:

`/Users/wondermonkey/Dropbox/Code/flycut`

Relevant Flycut behavior observed from the repo:

- Uses `NSPasteboard generalPasteboard`.
- Polls pasteboard `changeCount` about once per second.
- Tracks text and image pasteboard types.
- Records source app metadata.
- Suppresses self-writes using a pasteboard change counter.
- Has skip logic for pasteboard types and password-like contents.

Security implication: if Envelope simply copies `123456` as plain text, Flycut may persist the OTP in clipboard history.

Preferred macOS helper behavior:

1. Save current pasteboard item/types if feasible.
2. Write OTP as plain text plus a custom pasteboard marker type, e.g. `com.envelope.otp.transient`.
3. Configure or patch Flycut to skip that marker type.
4. Show notification with Done/Archive/Ignore.
5. On Done or timeout, restore prior clipboard only if the clipboard still equals the OTP.
6. If the clipboard changed, leave it alone.
7. Move/archive/delete the source email according to policy.

## Product architecture

Separate Envelope into layers:

1. **Envelope core**
   - Rust CLI first
   - `watch --json` / `events tail` event streams
   - deterministic code detector
   - rules/classifier hooks
   - safe message actions, starting with `mark_handled`

2. **Agent/event delivery**
   - stdout / JSONL
   - file append
   - signed HTTP webhook
   - command hook: configured argv, event JSON on stdin, no shell interpolation
   - Hermes/OpenClaw-native bridge that consumes one of the above

3. **Compatibility tooling**
   - MCP is useful for competitive parity and request/response agent-tool compatibility, especially Claude Code-style environments.
   - MCP is not Envelope’s primary surface.
   - Hermes and OpenClaw do not use MCP natively.
   - Do not design Envelope→Hermes/OpenClaw push around MCP, MCP notifications, or long-lived MCP subscriptions.

4. **Surface-specific helpers**
   - macOS desktop helper for clipboard + native notifications
   - Telegram actions for remote operation
   - future Windows/Linux helpers if needed

This preserves the right commercial/product story: Envelope is programmable mailbox infrastructure for semi-autonomous agents, not a bag of email-client conveniences.

## v0.6.0 OTP/event cutline

When scoping the first OTP event runtime, keep the release small and security-hard:

- Ship a redacted event bus first.
- Ship exactly one secure OTP retrieval/delivery path, not several half-secure paths.
- Ship one safe action first: `mark_handled` with explicit actor id, idempotency, and stale-event guard.
- Defer broad `events ack` / cursor semantics unless a concrete consumer requires it.
- Defer generic event-platform ambitions (`RuleMatched`, broad runtime health events, full route CRUD) unless nearly free.

Security rules from Codex pressure testing:

- OTP codes MUST NOT be delivered over stdout, file append, command hooks, environment variables, logs, or persisted event rows.
- Stdout and file append are never secure channels. They are redacted-only.
- Command hooks are redacted-only in the initial implementation. They should execute argv directly, with no shell interpolation, empty environment by default, explicit non-secret env allowlist, bounded timeout, and child stdout/stderr capture redacted.
- Secure webhook delivery needs replay protection: timestamp, key id, delivery id, HMAC over `timestamp || "." || body`, retries reusing the same delivery id, and receiver skew guidance.
- Fanout needs per-delivery state such as `event_deliveries`; a single `events.delivery_status` is wrong for multiple destinations.
- Types containing OTPs need redacted `Debug` and must not flow through generic serialization except inside the explicit secure path.

Implementation review lessons from the first OTP runtime build:

- Do not stop at adding safe event types/classifiers. Verify the actual live runtime path (`watch`, stdout, webhook, persistence) uses them. A disconnected safe pipeline can coexist with an unsafe legacy path that still persists and emits raw `subject`/`snippet`.
- In review, specifically inspect `crates/cli/src/commands/watch.rs` or its successor for raw `subject`, `snippet`, `payload`, `println!`, webhook body, file append, and `db.insert_event` calls. The path should redact before persistence/serialization and use idempotent insertion (`insert_event_idempotent` or equivalent), suppressing delivery on duplicate inserts.
- Store-level idempotency tests are not enough. Add/runtime-test duplicate IDLE re-fires so one mailbox event produces one persisted event and one delivery.
- SQLite migrations that add event-runtime columns should be partial-upgrade safe: use `up_with_hook`/`pragma_table_info` checks before `ALTER TABLE ... ADD COLUMN`, then create indexes/tables with `IF NOT EXISTS`. Unconditional `ADD COLUMN` can brick partially patched user databases.
- Redaction should be defense-in-depth, not merely extractor-shaped. Cover contiguous OTPs and common separated forms such as `123-456` and `123 456`; if extractor formats expand, redaction tests must expand with them.
