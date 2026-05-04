---
name: mac-storage-reclamation
description: Safely reclaim disk space on Tyler's Mac/Hermes machines by measuring first, identifying large backups/caches, deleting only rebuildable or redundant data, and verifying free space afterward.
tags: [macos, storage, cleanup, hermes, disk-space, caches]
triggers: ["free space", "find 15GB", "disk full", "not enough storage", "256GB", "cleanup mac", "reclaim space"]
---

# Mac Storage Reclamation

Use when Tyler asks to free disk space on a Mac/Hermes host, especially a small 256GB machine.

## Rules

1. Measure before deleting.
2. Delete only safe rebuildable caches or redundant rotating backups without asking.
3. Do not delete projects, Dropbox files, app state, credentials, current Hermes data, session history, conversation transcripts, chat exports, or unknown large folders without Tyler approval.
4. Treat Hermes session/conversation material as legal-retention data. It is not cache. Verify it survived after cleanup when relevant.
5. Keep at least the two newest Hermes full backups unless Tyler explicitly authorizes more aggressive pruning.
6. Report before/after free space and exactly what was removed.

## Fast triage

Start with live disk state; do not guess.

```bash
date
df -h / /System/Volumes/Data 2>/dev/null || df -h /
printf 'HOME=%s\n' "$HOME"
```

Avoid broad `du -xhd 1 /Users/wondermonkey` on near-full machines; it can hang or time out under Dropbox/iCloud/App Support. Use targeted probes with timeouts instead.

```bash
python3 - <<'PY'
import subprocess
paths = [
'/Users/wondermonkey/Downloads',
'/Users/wondermonkey/Dropbox/Code',
'/Users/wondermonkey/.hermes',
'/Users/wondermonkey/.cache',
'/Users/wondermonkey/.npm',
'/Users/wondermonkey/.local',
'/Users/wondermonkey/Library/Caches',
'/Users/wondermonkey/Library/Application Support',
'/Users/wondermonkey/Library/Developer',
'/Users/wondermonkey/Library/Containers/com.docker.docker',
'/Users/wondermonkey/.Trash',
]
for p in paths:
    r = subprocess.run(['bash','-lc',f'[ -e {p!r} ] && /usr/bin/du -xsh {p!r}'], capture_output=True, text=True, timeout=20)
    if r.stdout.strip(): print(r.stdout.strip())
PY
```

If a parent is large, enumerate direct children with per-child timeouts so one giant folder does not stall the whole pass:

```bash
python3 - <<'PY'
import os, subprocess, time
parents = ['/Users/wondermonkey/.hermes', '/Users/wondermonkey/Library/Caches', '/Users/wondermonkey/.npm', '/Users/wondermonkey/.cache', '/Users/wondermonkey/Library/Application Support']
for parent in parents:
    print('\n##', parent)
    try: names = os.listdir(parent)
    except Exception as e:
        print('ERR', e); continue
    for name in sorted(names):
        p=os.path.join(parent,name)
        try:
            r=subprocess.run(['/usr/bin/du','-xsh',p], capture_output=True, text=True, timeout=8)
            if r.stdout.strip(): print(r.stdout.strip())
        except subprocess.TimeoutExpired:
            print('TIMEOUT', p)
PY
```

## Known safe wins from Neo cleanup

On `wonderbookneo` in May 2026, the main safe win was old Hermes rotating backups:

- `~/.hermes/backups` was ~15GB.
- Keeping only the two newest `hermes-full-*.tgz` backups and checksums, plus clearing rebuildable caches, raised free space from ~4.4GB to ~19GB.

Inspect backups before pruning:

```bash
python3 - <<'PY'
import os, subprocess, time
p='/Users/wondermonkey/.hermes/backups'
for name in sorted(os.listdir(p)):
    fp=os.path.join(p,name)
    st=os.stat(fp)
    r=subprocess.run(['/usr/bin/du','-xsh',fp], capture_output=True, text=True, timeout=10)
    size=(r.stdout.strip().split('\t')[0] if r.stdout.strip() else '?')
    print(f"{size}\t{time.strftime('%Y-%m-%d %H:%M', time.localtime(st.st_mtime))}\t{fp}")
PY
```

Prune old full backups while keeping the two newest:

```bash
cd /Users/wondermonkey/.hermes/backups
python3 - <<'PY'
from pathlib import Path
files=sorted(Path('.').glob('hermes-full-*.tgz'), key=lambda p:p.stat().st_mtime, reverse=True)
keep=set(files[:2])
for tgz in files[2:]:
    print('delete', tgz)
    tgz.unlink()
    sha=Path(str(tgz)+'.sha256')
    if sha.exists(): sha.unlink()
print('kept', [str(p) for p in files[:2]])
PY
```

## Safe rebuildable cache cleanup

These are usually safe to remove; they may cause tools to redownload/rebuild later.

```bash
npm cache clean --force >/dev/null 2>&1 || true
python3 -m pip cache purge >/dev/null 2>&1 || true
brew cleanup -s >/dev/null 2>&1 || true
rm -rf /Users/wondermonkey/Library/Caches/Homebrew/* 2>/dev/null || true
rm -rf \
  /Users/wondermonkey/Library/Caches/com.openai.codex \
  /Users/wondermonkey/Library/Caches/ms-playwright \
  /Users/wondermonkey/Library/Caches/BraveSoftware \
  /Users/wondermonkey/Library/Caches/ru.keepcoder.Telegram \
  /Users/wondermonkey/Library/Caches/node-gyp \
  /Users/wondermonkey/Library/Caches/pip \
  /Users/wondermonkey/Library/Caches/com.apple.python \
  /Users/wondermonkey/Library/Caches/SiriTTS \
  /Users/wondermonkey/.cache/uv \
  /Users/wondermonkey/.cache/huggingface \
  /Users/wondermonkey/.cache/codex-runtimes 2>/dev/null || true
```

## Verify

```bash
sync || true
df -h /System/Volumes/Data 2>/dev/null || df -h /
du -xsh /Users/wondermonkey/.hermes/backups 2>/dev/null || true
```

If Hermes sessions/legal-retention data could have been affected, verify live session store still exists:

```bash
du -xsh /Users/wondermonkey/.hermes/sessions 2>/dev/null || true
python3 - <<'PY'
from pathlib import Path
p=Path('/Users/wondermonkey/.hermes/sessions')
files=[x for x in p.rglob('*') if x.is_file()] if p.exists() else []
print('session_files', len(files))
if files:
    print('oldest', min(files, key=lambda x:x.stat().st_mtime))
    print('newest', max(files, key=lambda x:x.stat().st_mtime))
PY
```

Final report should include:
- free space before
- free space after
- categories removed
- anything intentionally preserved
- whether session/conversation history was preserved, if legal-retention data was in scope

## Pitfalls

- `/Users/wondermonkey/Library/Application Support/Claude/vm_bundles` can be huge, but it may be app/Claude state. Do not delete without approval.
- Dropbox and project folders can be slow or large; do not delete or mass-offload without approval.
- Cache deletion can log Tyler out of some apps or trigger redownloads; prefer package/tool caches first.
- `rm` may trip an approval/governor gate; treat it as meaningful and surface it if not already approved by the user's cleanup request.
