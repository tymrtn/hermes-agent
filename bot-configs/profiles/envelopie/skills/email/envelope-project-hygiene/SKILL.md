---
name: envelope-project-hygiene
description: "Clean and de-confuse the Envelope project tree on Tyler's Dropbox-backed Mac: distinguish canonical Rust repo from legacy Python artifacts, archive stale prototypes safely, prevent Dropbox build-cache backlogs, and leave agent-readable tombstones/handoffs."
version: 1.0.0
author: Envelopie
license: MIT
metadata:
  hermes:
    tags: [Envelope, cleanup, Dropbox, archive, Rust, Python, handoff]
    related_skills: [envelope-release-ops, claude-code]
---

# Envelope Project Hygiene

Use this when Tyler asks what confusing Envelope files are, wants repo/project cleanup, sees Dropbox backlog, or asks whether old Python/legacy folders are active.

## Canonical layout

On wondermonkey, the Envelope project root is usually:

```text
/Users/wondermonkey/Dropbox/Code/envelope-email
```

The canonical active implementation is the Rust repo:

```text
/Users/wondermonkey/Dropbox/Code/envelope-email/u1f4e7-repo
```

Historical Python-era directories are not the active dashboard/runtime. Treat these as legacy unless Tyler explicitly says otherwise:

```text
app/
cli/
core/
templates/
static/
tests/
envelope-py/ or _internal/archive/envelope-py-archived-YYYYMMDD/
```

## First checks

Before deleting or moving anything, measure and identify:

```bash
ROOT=/Users/wondermonkey/Dropbox/Code/envelope-email
RUST=$ROOT/u1f4e7-repo

du -sh "$ROOT"/* 2>/dev/null | sort -hr | head -30
find "$ROOT" -path '*/target/*' -prune -o -type f -size +20M -print0 2>/dev/null | xargs -0 du -sh 2>/dev/null | sort -hr | head -30
find "$ROOT" -type d \( -name target -o -name __pycache__ -o -name .pytest_cache -o -name .venv -o -name venv \) 2>/dev/null | sort
```

Also check git status before archive/move operations:

```bash
cd "$ROOT" && git status --short --branch
cd "$RUST" && git status --short --branch
```

## Dropbox backlog cleanup

Cargo build artifacts are safe to remove and are usually the largest Dropbox problem. `target/debug/deps`, `target/debug/incremental`, and release deps are build cache, not source, mail, credentials, or shipped library files.

Preferred cleanup:

```bash
export HOME=/Users/wondermonkey
export PATH=/Users/wondermonkey/.cargo/bin:/Users/wondermonkey/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$PATH
cd /Users/wondermonkey/Dropbox/Code/envelope-email/u1f4e7-repo
cargo clean
```

Then prevent the next build from recreating a Dropbox storm:

```bash
ROOT=/Users/wondermonkey/Dropbox/Code/envelope-email
RUST=$ROOT/u1f4e7-repo
mkdir -p "$RUST/target"
xattr -w com.dropbox.ignored 1 "$RUST/target"
```

Legacy Python virtualenvs are also safe to keep local-only:

```bash
for p in "$ROOT/venv" "$ROOT/envelope-py/.venv" "$ROOT/_internal/archive/envelope-py-archived-"*/.venv; do
  [ -e "$p" ] && xattr -w com.dropbox.ignored 1 "$p" || true
done
```

Verify:

```bash
for p in "$RUST/target" "$ROOT/venv" "$ROOT/envelope-py/.venv"; do
  [ -e "$p" ] && printf '%s\t' "$p" && xattr -p com.dropbox.ignored "$p" 2>/dev/null || true
done
```

Known good result from 2026-05-03: cleaning `u1f4e7-repo/target` reduced the Rust repo from about 5.3GB to about 19MB.

## Generated Python cache cleanup

Safe to remove:

```text
__pycache__/
.pytest_cache/
.mypy_cache/
.ruff_cache/
.DS_Store
```

Use Python rather than fragile shell globs:

```python
from pathlib import Path
import shutil
root = Path('/Users/wondermonkey/Dropbox/Code/envelope-email')
for name in ['__pycache__', '.pytest_cache', '.mypy_cache', '.ruff_cache']:
    for p in root.rglob(name):
        if p.is_dir():
            shutil.rmtree(p, ignore_errors=True)
for p in root.rglob('.DS_Store'):
    if p.is_file():
        p.unlink(missing_ok=True)
```

## Archiving legacy Python prototype

If `envelope-py/` exists and README/pyproject identify it as the old Python prototype, archive it so agents do not confuse it with the active dashboard/runtime.

Before moving, remove runtime/generated artifacts from the archive copy/source:

```bash
ROOT=/Users/wondermonkey/Dropbox/Code/envelope-email
SRC="$ROOT/envelope-py"
DEST="$ROOT/_internal/archive/envelope-py-archived-$(date +%Y%m%d)"

rm -rf "$SRC/.venv"
rm -f "$SRC/envelope.db" "$SRC/test_envelope.db"
find "$SRC" \( -name __pycache__ -o -name .pytest_cache -o -name .mypy_cache -o -name .ruff_cache \) -type d -prune -exec rm -rf {} +
mkdir -p "$(dirname "$DEST")"
mv "$SRC" "$DEST"
```

Create a root tombstone so future agents and Tyler can find it:

```text
ENVELOPE_PY_ARCHIVED.md
```

Minimum tombstone content:

```markdown
# Envelope Python Prototype Archived

The former `envelope-py/` directory was the original Python prototype of Envelope, not the current dashboard and not the active CLI/runtime.

It has been moved to `_internal/archive/envelope-py-archived-YYYYMMDD/`.

The active Envelope implementation is `u1f4e7-repo/`.

Runtime artifacts were intentionally removed before archiving: `.venv/`, `envelope.db`, `test_envelope.db`, and generated Python cache directories.

Use the archive only for historical reference. Do not build new Envelope work from it.
```

Update project `CLAUDE.md` to state:

- `u1f4e7-repo/` is canonical Rust Envelope.
- `_internal/archive/envelope-py-archived-YYYYMMDD/` is historical only.
- remaining Python-era top-level dirs are legacy unless Tyler explicitly asks otherwise.

## Claude handoff for risky in-flight work

When cleanup reveals in-flight feature spikes, especially under `/tmp`, preserve them before shutdown or cleanup:

1. Save a durable patch under the project root or `_internal/handoffs/`.
2. Write a handoff describing current state, blockers, recovery steps, and definition of done.
3. Add/update `CLAUDE.md` so Claude Code auto-loads the right context.
4. If useful, add `.claude/commands/<task>.md` with the exact recovery/fix prompt.

For the IMAP migration spike, the pattern was:

```bash
cd /private/tmp/envelope-imap-migrate
git add -N crates/cli/src/commands/migrate.rs crates/email/src/migrate.rs crates/store/src/migration.rs
git diff --binary > /Users/wondermonkey/Dropbox/Code/envelope-email/20260503-envelope-imap-migrate-spike.patch
git reset -- crates/cli/src/commands/migrate.rs crates/email/src/migrate.rs crates/store/src/migration.rs >/dev/null
```

## Verification after cleanup/archive

Run:

```bash
ROOT=/Users/wondermonkey/Dropbox/Code/envelope-email

test ! -e "$ROOT/envelope-py" && echo 'root envelope-py archived'
test -f "$ROOT/ENVELOPE_PY_ARCHIVED.md" && echo 'tombstone present'
du -sh "$ROOT" "$ROOT/u1f4e7-repo" "$ROOT/u1f4e7-repo/target" 2>/dev/null || true
find "$ROOT" -type d \( -name target -o -name __pycache__ -o -name .pytest_cache \) 2>/dev/null | sort | head -50
```

Report concrete sizes and exact paths. The user cares about reducing Dropbox churn and agent confusion, not abstract cleanup virtue.
