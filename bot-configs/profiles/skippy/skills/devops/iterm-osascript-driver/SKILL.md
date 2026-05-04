---
name: iterm-osascript-driver
description: Drive iTerm2 via AppleScript to run commands in Tyler's GUI shell session. Use when Hermes terminal subprocess can't reach user-session auth (keychain, OAuth tokens, GUI login state) — Claude CLI, Railway CLI, gh CLI sometimes need this. Also handles interactive multi-step prompts (fuzzy-finder pickers, login flows).
version: 1.0.0
author: Skippy
license: MIT
metadata:
  hermes:
    tags: [macos, iterm, applescript, terminal, cli, auth, escape-hatch]
    related_skills: [claude-cli-oauth-fix]
---

# iTerm Driver via osascript

## When to load this skill

- A CLI tool works in Tyler's terminal but 401s/fails when run via Hermes `terminal()`.
- A CLI is installed in `~/.nvm/versions/.../bin/` and isn't on the Hermes shell PATH.
- You need to drive an interactive multi-step prompt (Railway link, gh auth login, npm login, etc.).
- You need to run something in Tyler's keychain/GUI session context.

## Core pattern

### Send a command and read output
```
# Send command to current iTerm session:
osascript <<'EOF'
tell application "iTerm"
  activate
  tell current window
    tell current session
      write text "your command here"
    end tell
  end tell
end tell
EOF

# Wait for completion, then read screen contents:
sleep 3
osascript -e 'tell application "iTerm" to tell current window to tell current session to get contents'
```

The `get contents` returns the entire visible buffer as text, including the prompt before and after your command.

### Open a new tab first (don't pollute current session)
```
osascript <<'EOF'
tell application "iTerm"
  activate
  tell current window
    create tab with default profile
    tell current session
      write text "cd /path/to/project"
    end tell
  end tell
end tell
EOF
```

### Drive interactive prompts (fuzzy-finder, multi-step pickers)

`write text` with `newline no` = type without pressing Enter (perfect for filter/search pickers).
`write text ""` with default newline = press Enter alone.

```
# Type filter text into a fuzzy picker:
osascript -e 'tell application "iTerm" to tell current window to tell current session to write text "warboard" newline no'

# Press Enter to confirm selection:
osascript -e 'tell application "iTerm" to tell current window to tell current session to write text ""'
```

Real example (Railway link with workspace → project → environment → service picker):
```
osascript -e '... write text "railway link"'                    # start
sleep 2
osascript -e '... write text ""'                                 # accept default workspace
sleep 1
osascript -e '... write text "warboard" newline no'              # filter
osascript -e '... write text ""'                                 # select
sleep 1
osascript -e '... write text ""'                                 # accept production env
sleep 1
osascript -e '... write text ""'                                 # accept warboard service
```

## Read-then-decide loop

After every step, read contents to verify the prompt advanced before sending the next keystroke. Don't fire-and-forget.

```bash
# After each write text:
sleep 2
contents=$(osascript -e 'tell application "iTerm" to tell current window to tell current session to get contents')
echo "$contents" | tail -10
# Inspect for the next expected prompt before continuing
```

## Use cases

### Claude CLI when Hermes terminal can't auth
See `claude-cli-oauth-fix` skill. Quick path:
```
osascript -e 'tell application "iTerm" to tell current window to tell current session to write text "cd ~/project && claude -p \"your task\" --max-turns 20"'
sleep 90  # let Claude work
osascript -e 'tell application "iTerm" to tell current window to tell current session to get contents' | tail -100
```

### Railway CLI when not linked
Railway stores project link in `.railway/config` per-directory. First-time setup needs interactive picker. After `railway link` succeeds, subsequent `railway variables --set X=Y`, `railway redeploy -y`, `railway logs` all work non-interactively.

### Any nvm-installed CLI not on PATH
Hermes terminal often doesn't source nvm. Tools installed via `npm install -g` while a node version is active end up at `~/.nvm/versions/node/vX.Y.Z/bin/<tool>`. iTerm has Tyler's full PATH; just shell out.

## Pitfalls

1. **`write text ""` ≠ no-op** — it sends an Enter keystroke. Use only when you want to press Enter alone.
2. **`newline no` is the modifier** for typing without submitting. Order matters: `write text "x" newline no`.
3. **Always activate iTerm first** if it might be backgrounded: `tell application "iTerm" to activate`.
4. **`get contents` returns visible buffer only** — long output may be scrolled out of view. For full history use the iTerm `text` (entire buffer) attribute or pipe to a file: `your_command | tee /tmp/out.log`.
5. **Race conditions** — always `sleep` between `write text` and `get contents`. 1–3s for fast commands, longer for network calls or builds.
6. **Don't assume the current session is empty** — open a new tab if you need a clean state.
7. **Sensitive output is visible to Tyler** — anything you run in iTerm is on his screen. Don't dump huge secrets there.
8. **One window assumption** — `current window` / `current session` targets whatever's focused. If Tyler is using iTerm actively, you'll interfere. Better: spawn a new tab with `create tab with default profile`.
9. **Heredoc vs -e** — `osascript <<'EOF' ... EOF` is cleaner for multi-line scripts; `osascript -e '...'` is fine for one-liners. Don't mix quoting styles inside `-e` carelessly.

## Verification before kicking off long jobs

If using this to run a long-running CLI (Claude Code, Railway deploy, etc.), verify with a quick `say hi` or `whoami` first:
```
osascript -e '... write text "claude -p \"reply with: ok\" --max-turns 1"'
sleep 10
osascript -e '... get contents' | tail -5
# Confirm "ok" appears, not 401
```

## Don't do

- Don't use this when `terminal()` would work fine — it's slower and pollutes Tyler's screen.
- Don't drive heavy automation through iTerm — it's an escape hatch, not a primary tool.
- Don't assume `current window` is the right one if Tyler has multiple iTerm windows open.
