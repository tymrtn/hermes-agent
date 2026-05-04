---
name: envelope-events-scope
description: Scope guard and implementation guidance for Envelope Events / OTP agent-loop work, especially avoiding MCP push scope creep.
version: 1.0.0
author: Envelopie
license: MIT
metadata:
  hermes:
    tags: [Envelope, events, OTP, agent-loop, scope, MCP]
---

# Envelope Events Scope Guard

Use this when planning or building Envelope event workflows, OTP handling, agent notifications, mailbox triage loops, or v0.6.0-style runtime features.

## Core lesson

Do **not** silently expand “push the notification to the agent” into MCP push, MCP notifications, or long-lived MCP subscriptions.

For Envelope Events / OTP agent-loop work, distinguish three separate layers:

1. **Event delivery**
   - stdout / JSONL
   - file append
   - signed webhook

2. **Agent control**
   - list events
   - ack events
   - execute actions such as mark-handled, move, archive, delete, tag

3. **MCP push / notification transport**
   - long-lived MCP subscriptions
   - server-initiated MCP notifications
   - secure OTP payload push over MCP

Only layers 1 and 2 are default scope. Layer 3 is out of scope unless Tyler explicitly asks for MCP push.

## Default v0.6.0-style scope

In scope:
- structured Envelope Events
- deterministic OTP detection/classification
- redacted persisted/logged summaries by default
- secure OTP payloads delivered only when explicitly requested
- stdout/JSONL stream
- file append stream
- signed webhook if practical
- CLI action callback path: events list/ack and actions execute
- request/response MCP tools only if they fit existing MCP patterns without push semantics

Out of scope by default:
- MCP push
- MCP notifications
- long-lived MCP subscriptions
- MCP secure-payload push
- desktop app / native notifications
- clipboard writes in Envelope core
- mandatory LLM / Apple Intelligence dependency

## Clipboard stance

Clipboard/desktop behavior is a downstream helper concern, not Envelope core. Envelope core should emit events and accept actions. Local helpers may decide how to handle clipboard managers, pasteboard marker types, and auto-clear behavior.

## Delegation prompt guard

When delegating implementation to Claude Code or Codex, include explicit language:

> DO NOT implement MCP push, MCP notifications, long-lived MCP subscriptions, or MCP secure-payload push. MCP is request/response only if touched at all. Default event delivery is stdout/JSONL, file append, and signed webhook.

## OTP event runtime hardening checklist

When implementing or reviewing Envelope OTP/event runtime work, verify these invariants explicitly:

1. Redact at insertion and at every output boundary. Do not trust old DB rows or future callers to already be clean. `events list`, `events ack --json`, stdout JSONL, webhook bodies, file/command payloads, and action audit output should defensively re-redact `subject`, `snippet`, and `payload` before serialization.
2. Treat legacy unredacted rows as a release blocker. Add regression tests that insert a raw OTP-shaped persisted event row and prove CLI output does not include the code.
3. Idempotency keys for IMAP watch must include real mailbox identity, especially UIDVALIDITY, plus UID and event type. Avoid placeholders like `uidvalidity-unknown`; UID reuse after UIDVALIDITY changes can suppress valid events. If using Message-ID in the key, hash it rather than exposing the raw header. Also track current UIDVALIDITY during a watch session and reset the in-memory UID watermark when UIDVALIDITY changes, otherwise lower-UID messages after a mailbox reset can be missed even though idempotency keys are technically scoped correctly.
4. OTP redaction must cover common renderings: contiguous digits (`123456`), hyphen/space split (`123-456`, `123 456`), and fully spaced digits (`1 2 3 4 5 6`). Keep extractor and redactor behavior aligned.
5. Snippet previews must truncate on UTF-8 character boundaries, never byte-slice strings from mail bodies.
6. Event store list/read paths should propagate row mapping errors instead of silently dropping bad rows with `filter_map(|r| r.ok())`.
7. `mark-handled` for the v0.6.0 cutline is local audit only. It must not mutate the mailbox or deliver OTP material.

## Why this matters

MCP exists in Envelope and is an attractive agent surface, but adding push semantics is a separate transport feature. Folding it into OTP/events makes the plan broader, riskier, and less aligned with the intended MVP: a clean mailbox runtime event loop that works across remote agent surfaces without assuming an MCP client is connected.