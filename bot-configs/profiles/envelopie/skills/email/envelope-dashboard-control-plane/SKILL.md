---
name: envelope-dashboard-control-plane
description: "Build and verify Envelope dashboard features that surface CLI/runtime power — rules, events, account health, and operator controls — without drifting into toy webmail."
version: 1.0.0
author: Envelopie
license: FSL-1.1-ALv2
metadata:
  hermes:
    tags: [Envelope, dashboard, rules, control-plane, Rust, browser-QA]
---

# Envelope Dashboard Control Plane

Use this skill when Tyler asks to improve the Envelope dashboard, especially to surface advanced runtime/CLI features such as rules, events, watch status, OTP, account health, scheduling, or agent operations.

## Product rule

The dashboard should either:

1. keep up with real CLI/runtime power, or
2. provide critical human GUI functionality that the CLI cannot do well.

Do **not** build generic webmail for its own sake. The dashboard should feel like the control panel for programmable mailboxes.

Preferred framing:

- Iteration 1: CLI parity control plane.
- Iteration 2: human GUI superpowers.
- Iteration 3: agent operations cockpit.

For rules specifically: rules are not a settings detail. They are a first-class runtime surface.

## Current repo and runtime paths

Envelope repo:

```text
/Users/wondermonkey/Dropbox/Code/envelope-email/u1f4e7-repo
```

Canonical command path on wondermonkey:

```text
/Users/wondermonkey/.local/bin/envelope
```

Raw binary install target:

```text
/Users/wondermonkey/.local/libexec/envelope-rust
```

Shared Envelope HOME for dashboard/server runs:

```text
/Users/wondermonkey/.hermes/shared/envelope-home
```

Dashboard port:

```text
http://127.0.0.1:3141
```

## Existing dashboard structure

Key files:

```text
crates/dashboard/src/lib.rs
crates/dashboard/src/handlers/mod.rs
crates/dashboard/src/handlers/*.rs
crates/dashboard/src/assets.rs
crates/dashboard/static/index.html
crates/dashboard/static/dashboard.js
crates/dashboard/static/dashboard.css
```

Dashboard static assets are embedded into the Rust binary with `rust-embed`. After static changes, you must rebuild and reinstall the binary and restart the dashboard process. Refreshing the browser alone can keep showing old embedded assets if the old process is still running.

## TDD pattern that worked

For dashboard surface changes, add a cheap Rust asset-regression test in:

```text
crates/dashboard/src/assets.rs
```

Examples:

- prevent stale version copy from returning;
- assert the dashboard exposes important operator controls such as `Rules Control Plane`, `btn-refresh-rules`, or `btn-reader-test-rules`.

Then run the targeted test and confirm RED before implementation:

```bash
export HOME=/Users/wondermonkey PATH=/Users/wondermonkey/.cargo/bin:/Users/wondermonkey/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$PATH
cargo test -p envelope-email-dashboard dashboard_static_assets_expose_rules_control_plane -- --nocapture
```

If `cargo` is missing in a Hermes/profile shell, explicitly export the real-user HOME/PATH as above before concluding the toolchain is absent.

## Adding a new dashboard runtime surface

1. Inspect current API/router in `crates/dashboard/src/lib.rs`.
2. Add a handler module under `crates/dashboard/src/handlers/`.
3. Export it in `crates/dashboard/src/handlers/mod.rs`.
4. Mount routes under `/api/accounts/{id}/...` or `/api/...` in `lib.rs`.
5. Add UI anchors/buttons in `static/index.html`.
6. Add state/fetch/render code in `static/dashboard.js`.
7. Style it in `static/dashboard.css`.
8. Add/extend asset tests in `assets.rs`.
9. Run JS syntax check, formatting, and dashboard tests.
10. Build, install, restart, and browser-verify.

## Rules control-plane implementation notes

The shipped rules CLI is `envelope rule ...`; the dashboard should use the same DB/rule engine concepts.

Useful current route shape:

```text
GET  /api/accounts/{id}/rules
GET  /api/accounts/{id}/rules/test/{uid}?folder=INBOX
POST /api/accounts/{id}/rules/run
```

A safe first pass should expose both visibility and bounded operator actions:

- list rules per selected account;
- show enabled/disabled, priority, stop, hit count;
- show action and match expression;
- expose copyable equivalent CLI;
- add `Test Rules` in the message reader to dry-run enabled rules against the selected message;
- add bounded `Run enabled` controls for the current real IMAP folder, with explicit limit clamp (`1..=200`) and confirmation before mutation;
- show processed/action counts plus a short result log after a run.

Do not run rules automatically on page load or account select. User-triggered run only. For future high-polish work, add preview/undo before making broader bulk actions feel routine.

For dry-run, fetch the message via IMAP, build `MessageContext` from message metadata plus local tags/scores/contact tags, evaluate enabled rules in priority order, and stop on `rule.stop`.

## Verification commands

From repo root:

```bash
export HOME=/Users/wondermonkey PATH=/Users/wondermonkey/.cargo/bin:/Users/wondermonkey/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$PATH
node --check crates/dashboard/static/dashboard.js
cargo fmt --all
cargo test -p envelope-email-dashboard
cargo build --release -p envelope-email
install -m 0755 target/release/envelope /Users/wondermonkey/.local/libexec/envelope-rust
```

Restart dashboard:

```bash
pid=$(lsof -tiTCP:3141 -sTCP:LISTEN || true)
if [ -n "$pid" ]; then kill $pid || true; fi
export HOME=/Users/wondermonkey/.hermes/shared/envelope-home
/Users/wondermonkey/.local/libexec/envelope-rust serve --port 3141
```

Use Hermes `terminal(background=true)` for the serve process, not shell-level `&` wrappers.

Browser/API smoke checks:

```bash
python3 - <<'PY'
import json, urllib.request
base='http://127.0.0.1:3141/api'
accounts=json.load(urllib.request.urlopen(base+'/accounts'))['accounts']
acct=accounts[0]
rules=json.load(urllib.request.urlopen(base+f"/accounts/{acct['id']}/rules"))['rules']
print('accounts', len(accounts))
print('first', acct.get('username'), acct.get('id'))
print('rules', len(rules))
print('enabled', sum(1 for r in rules if r.get('enabled')))
PY
```

Then use browser tools to verify:

- dashboard shows current version, not stale embedded copy;
- sidebar surface appears and renders real data;
- selected-message dry-run works;
- browser console has no JS errors.

## Product-quality checks

Before calling it done, inspect visually. Watch for:

- advanced controls looking disabled when clickable;
- cramped/wrapping action buttons;
- sidebar panels becoming unreadable with many rules;
- raw JSON overwhelming users without a useful summary;
- stale dashboard process serving old embedded assets.

Tiny polish fixes matter here. The dashboard should surprise and delight by making advanced mailbox runtime behavior legible, not merely available.

## Pitfalls

- `rust-embed` means static asset changes require rebuild/reinstall/restart.
- Hermes/profile shells can miss `cargo`; export real-user HOME/PATH.
- Avoid adding new Envelope command-path shims/symlinks. Use the canonical command path and shared HOME.
- Do not expose credentials or secret payloads in dashboard logs, browser console, or API responses.
- For OTP/security event work, follow `envelope-agent-events` and `envelope-events-scope` redaction rules.
