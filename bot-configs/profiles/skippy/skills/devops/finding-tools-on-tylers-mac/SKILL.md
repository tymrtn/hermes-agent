---
name: finding-tools-on-tylers-mac
description: How to locate CLI tools on Tyler's Macs (wonderbookneo and others) before claiming a tool is missing. Load when a `which <tool>` comes back empty or a subprocess says command not found.
version: 1.0.0
author: Skippy
license: MIT
metadata:
  hermes:
    tags: [macOS, CLI, environment, path, railway, nvm]
    related_skills: [claude-code]
---

# Finding Tools on Tyler's Mac

Tyler's expectation: when he says "X CLI is installed," it is. If my subprocess can't find it, the problem is my PATH, not the machine.

Do NOT respond with "X isn't installed" until you've checked every location below.

## Check order

1. **nvm-managed tools** (most common miss):
   ```
   ls ~/.nvm/versions/node/*/bin/<tool> 2>/dev/null
   ```
   Railway CLI, Vercel, Netlify, Wrangler, and most Node-based CLIs live here. My subprocess PATH does NOT include nvm bins by default.

2. **Homebrew** (Apple Silicon): `/opt/homebrew/bin/<tool>`, `/opt/homebrew/sbin/<tool>`
3. **Intel Homebrew / manual installs**: `/usr/local/bin/<tool>`
4. **User local**: `~/.local/bin/<tool>`, `~/.cargo/bin/<tool>`
5. **pipx/pyenv**: `~/.local/pipx/venvs/*/bin/<tool>`, `~/.pyenv/shims/<tool>`
6. **App bundles**: `/Applications/<App>.app/Contents/MacOS/<bin>`
7. **Global search** (last resort):
   ```
   find / -maxdepth 6 -name "<tool>" -type f 2>/dev/null | grep -v Trash | head
   ```

If found outside PATH, invoke with absolute path or add to PATH for the command:
```
PATH="$HOME/.nvm/versions/node/v22.22.2/bin:$PATH" railway status
```

## Interactive prompts — use iTerm via osascript

Many CLIs (railway link, claude login, gh auth) have interactive TUIs that fight with stdin piping. Drive them in iTerm instead:

```
osascript <<'EOF'
tell application "iTerm"
  activate
  tell current window
    create tab with default profile
    tell current session
      write text "cd ~/project && railway link"
    end tell
  end tell
end tell
EOF
```

Read output back:
```
osascript <<'EOF'
tell application "iTerm"
  tell current window
    tell current session
      set c to contents
    end tell
  end tell
end tell
return c
EOF
```

Send Enter / arrow keys by writing empty lines or control chars. For arrow-key menus, type the option text to filter then press Enter.

## Pitfalls

- `which` in my subprocess reflects MY shell PATH, not Tyler's login shell. Always check the filesystem directly.
- `command -v` same limitation.
- `type <tool>` in a fresh bash -c won't source `.zshrc` / `.zshenv` — nvm init never runs.
- Don't run `nvm use` in my subprocess — nvm is a shell function, not an executable.
- If Tyler says "it's installed," assume he's right and search harder. Don't push back without evidence.

## Known locations on wonderbookneo (new Mac)

- `railway` → `~/.nvm/versions/node/v22.22.2/bin/railway`
- `claude` → `~/.local/bin/claude` (OAuth at `~/.claude/.credentials.json`)
- `gh` → `/opt/homebrew/bin/gh` (hosts config in `~/.config/gh/hosts.yml`)
