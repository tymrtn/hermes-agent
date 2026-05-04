---
name: envelope-enterprise-commercial-slice
description: "Build or update Envelope’s enterprise/commercial surface: research-backed positioning, static landing page, practical checkout/intake flow, local CLI license activation, and Hermes operator assets."
version: 1.0.0
author: Nagovernor
license: MIT
metadata:
  hermes:
    tags: [envelope, enterprise, licensing, landing-page, checkout, gtm, hermes-agent]
---

# Envelope Enterprise Commercial Slice

Use this when working on Envelope’s buyer-facing commercial layer.

## When to use

- User wants an enterprise landing page for Envelope
- User wants licensing / checkout / evaluation flow around Envelope
- User wants GTM docs or operator workflows for selling Envelope
- User wants Envelope positioned for semi-autonomous agent harnesses rather than as a generic email client

## Core framing

Envelope is **primarily a mailbox runtime for semi-autonomous agents**.

Do **not** lead with:
- generic email client
- generic AI assistant
- transactional email vendor replacement
- hosted email API abstraction

Lead with:
- BYO mailbox
- any IMAP provider
- no DNS changes
- local / self-hosted control model
- CLI + JSON
- MCP
- watch / IMAP IDLE
- OTP extraction
- thread-aware workflows
- rules, snooze, scheduling, contacts

Strong-fit named harnesses:
1. OpenClaw
2. Hermes Agent
3. Claude Code
4. Codex CLI
5. OpenHands

## Proven implementation pattern

### 1. Ground on the repo first
Read:
- `README.md`
- `CHANGELOG.md`
- existing license or store code
- CLI command surface in `crates/cli/src/main.rs`

Important finding from prior work:
- Envelope already had a local `license_keys` SQLite table and store helpers in `crates/store/src/license_store.rs`
- CLI had `license` subcommands wired in `main.rs`, but activation/status were placeholders
- This means you should extend the existing product path, not invent a parallel licensing mechanism

## 2. Research before writing copy
Create project docs under:
- `docs/research/agent-harness-fit.md`
- `docs/research/email-competitor-matrix.md`

Keep public competitor claims conservative.

Safe competitor framing:
- vs terminal mail clients: Envelope is built for automation/agents, not only human terminal mail
- vs hosted email infra: Envelope works with the mailbox you already have and does not require DNS changes
- vs remote email MCP projects: Envelope includes MCP but is broader than an email bridge
- do **not** claim deliverability, security superiority, or replacement for Nylas/SendGrid without proof

## 3. Build a static enterprise site, not fake SaaS theater
Recommended paths:
- `site/enterprise/index.html`
- `site/enterprise/checkout.html`
- `site/enterprise/styles.css`
- `site/enterprise/app.js`
- `site/enterprise/checkout.js`
- `site/enterprise/README.md`

Recommended approach:
- use plain static HTML/CSS/JS
- no build tooling unless explicitly requested
- landing page sells the category and fit
- checkout page is an **honest intake / estimator / request generator** unless a real payment backend exists

Good pattern for checkout page:
- plan selector
- seats input
- deployment model
- runtime/harness selector
- requested features
- notes / procurement text
- generated JSON request payload
- copy-to-clipboard
- mailto draft for operator handoff

Do **not** pretend there is a Stripe-backed purchase flow if there isn’t one.

## 4. Implement local license activation around the existing store
Recommended file:
- `crates/cli/src/commands/license.rs`

Wire into:
- `crates/cli/src/commands/mod.rs`
- `crates/cli/Cargo.toml`
- `crates/cli/src/main.rs`

Proven approach:
- use a simple encoded activation payload with prefix like `envl1_`
- payload fields: `token`, `licensee`, `expires_at`, `features`, optional `plan`, optional `issued_at`
- parse with base64url JSON payload
- validate `expires_at` as RFC3339 and reject expired keys
- store using the existing `db.store_license(...)`
- expose both text and `--json` output for activation and status

This is suitable for:
- evaluation keys
- enterprise pilot keys
- local commercial activation

It is **not** a full remote licensing backend.

## 5. Add an operator-side issuance tool
Recommended path:
- `scripts/issue_license.py`

Behavior:
- accept `--licensee`, `--plan`, `--expires-at`, repeated `--feature`
- emit a valid `envl1_...` key matching the CLI activation parser

This lets a Hermes/Nagatha operator issue time-boxed evaluation or enterprise keys without building backend infrastructure first.

## 6. Add GTM and operator docs in-repo
Recommended files:
- `docs/gtm/envelope-enterprise-gtm.md`
- `agents/hermes-envelope-commercial-operator.md`

Operator doc should cover:
- inbound licensing requests
- evaluation key issuance
- ecosystem / registry outreach
- follow-up rules
- tone constraints

## 7. Verification checklist
Run:
```bash
cargo test -p envelope-email license
cargo check -p envelope-email
cargo build -p envelope-email
python3 scripts/issue_license.py --licensee "Acme Corp" --plan enterprise --expires-at 2099-12-31T23:59:59Z --feature compose --feature mcp
```

Then do an isolated activation check with a fresh HOME:
```bash
TMP_HOME=$(mktemp -d)
KEY=$(python3 scripts/issue_license.py --licensee "Acme Corp" --plan enterprise --expires-at 2099-12-31T23:59:59Z --feature compose --feature mcp)
HOME="$TMP_HOME" ./target/debug/envelope license activate "$KEY" --json
HOME="$TMP_HOME" ./target/debug/envelope license status --json
```

Important finding:
- `cargo run` can fail under a temp `HOME` because `rustup` keys off HOME. Use the built binary (`./target/debug/envelope`) for isolated-license verification instead.

## Pitfalls

### 1. README/license mismatch
If public copy says things like:
- “free for personal use”
- “free for solopreneurs”

but the actual license text says non-commercial only, do **not** paper over the mismatch.

Document it explicitly and avoid broad public claims until the license and marketing copy are aligned.

### 2. Don’t invent a fake enterprise backend
If there is no real billing or remote activation service, build:
- static intake
- honest estimator
- operator-issued evaluation/commercial keys

That is better than fake checkout flows.

### 3. Use the existing local license store
Because Envelope already has SQLite license storage, extend that path instead of building an unrelated config-file or dashboard-only license mechanism.

## Deliverable set that worked well
A good first commercial slice includes:
- research docs
- enterprise landing page
- enterprise checkout/intake page
- CLI `license activate` / `license status`
- `issue_license.py`
- GTM doc
- Hermes commercial operator doc

## Summary line
For Envelope commercialization work, build the smallest honest commercial surface around the existing local product: **research-backed positioning, static enterprise site, practical intake flow, local license activation, and operator-run issuance/workflows.**
