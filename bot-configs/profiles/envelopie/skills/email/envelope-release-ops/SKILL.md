---
name: envelope-release-ops
description: Clean and package the Envelope Rust CLI for release without accidentally shipping Cargo build artifacts; includes target cleanup, size gates, and Tyler Mac toolchain quirks.
version: 1.0.0
author: Envelopie
license: MIT
metadata:
  hermes:
    tags: [Envelope, Rust, release, packaging, cargo, cleanup]
---

# Envelope Release Operations

Use this when preparing Envelope releases, investigating large `target/` directories, checking binary/package size, or cleaning Rust build artifacts in `/Users/wondermonkey/Dropbox/Code/envelope-email/u1f4e7-repo`.

## Repo and toolchain

Canonical repo path used in recent Envelope work:

```text
/Users/wondermonkey/Dropbox/Code/envelope-email/u1f4e7-repo
```

Hermes profile shells may not see Tyler's real Rust/Codex toolchain because `HOME` points at the profile home. Before concluding Cargo/Rust/Codex is missing, run commands with:

```bash
export HOME=/Users/wondermonkey
export PATH=/Users/wondermonkey/.cargo/bin:/Users/wondermonkey/.local/bin:$PATH
```

## Understanding `target/` size

Large `target/debug` directories are normal Cargo build cache, not shippable Envelope runtime state.

Typical sources:

- `target/debug/deps` — compiled dependencies, test binaries, `.rlib`s, debug-heavy artifacts.
- `target/debug/incremental` — incremental compilation cache.
- `target/debug/build` — build script outputs.
- `target/release/deps` — release dependency artifacts; do not package the directory.

In one Envelope session, `target/debug` reached ~15–16GB after repeated `cargo test --workspace`, `cargo clippy`, and Codex build passes. It was safe to delete.

## Cleanup procedure

First measure:

```bash
du -sh target target/debug target/release 2>/dev/null || true
du -sh target/debug/* 2>/dev/null | sort -hr | head -30
```

Preferred cleanup:

```bash
export HOME=/Users/wondermonkey
export PATH=/Users/wondermonkey/.cargo/bin:/Users/wondermonkey/.local/bin:$PATH
cargo clean
```

If `cargo clean` is unavailable because of profile `HOME`, retry with the exports above.

If `cargo clean` or `rm -rf target/debug` times out because the repo is in Dropbox and the tree has many tiny files, remove the largest subtrees first, preferably with Python `shutil.rmtree`:

```python
from pathlib import Path
import shutil
repo = Path('/Users/wondermonkey/Dropbox/Code/envelope-email/u1f4e7-repo')
for rel in ['target/debug/deps', 'target/debug/incremental', 'target/debug/build']:
    p = repo / rel
    if p.exists():
        shutil.rmtree(p, ignore_errors=True)
```

Then re-check:

```bash
du -sh target target/debug 2>/dev/null || true
find target/debug -type f 2>/dev/null | wc -l
```

Successful cleanup can leave `target/` as a tiny empty directory, e.g. `8.0K target`.

## Dropbox backlog prevention

Because Envelope lives under Dropbox on Tyler's Mac, deleting `target/` is only half the fix: the next Rust build will recreate thousands of files and can create another Dropbox sync backlog. After cleaning, recreate an empty `target/` directory and mark it Dropbox-ignored with the macOS extended attribute:

```bash
ROOT=/Users/wondermonkey/Dropbox/Code/envelope-email
RUST=$ROOT/u1f4e7-repo
mkdir -p "$RUST/target"
xattr -w com.dropbox.ignored 1 "$RUST/target"

# Legacy Python virtualenvs are also safe to keep local-only if present.
for p in "$ROOT/venv" "$ROOT/envelope-py/.venv"; do
  [ -e "$p" ] && xattr -w com.dropbox.ignored 1 "$p" || true
done

for p in "$RUST/target" "$ROOT/venv" "$ROOT/envelope-py/.venv"; do
  [ -e "$p" ] && printf '%s\t' "$p" && xattr -p com.dropbox.ignored "$p" 2>/dev/null || true
done
```

On 2026-05-03 this cleanup reduced `/Users/wondermonkey/Dropbox/Code/envelope-email/u1f4e7-repo` from ~5.3GB to ~19MB and marked `u1f4e7-repo/target`, `venv`, and `envelope-py/.venv` with `com.dropbox.ignored=1`.

## Release size sanity

Envelope is a Rust email CLI, not a cloud Gmail replacement. A release artifact near hundreds of MB is almost certainly a packaging mistake.

Known good local raw binary size on Tyler's Mac:

```text
/Users/wondermonkey/.local/libexec/envelope-rust  ~9.4M  Mach-O arm64
```

`/Users/wondermonkey/.local/bin/envelope` should remain a wrapper that sets the shared Envelope HOME and execs that raw binary. Release/install work must not overwrite the wrapper permanently; copy the new binary to `/Users/wondermonkey/.local/libexec/envelope-rust` and leave public entrypoints routed to the singleton store.

Recommended size gates:

- stripped macOS arm64 binary: ideal <15MB, hard ceiling <25MB
- `.tar.gz` release package: ideal <10MB, hard ceiling <20MB
- anything near 400MB means the release process probably included `target/release`, `target/release/deps`, debug symbols, or the whole `target/` tree.

Check actual artifacts, not directories:

```bash
du -sh /Users/wondermonkey/.local/bin/envelope 2>/dev/null || true
file /Users/wondermonkey/.local/bin/envelope 2>/dev/null || true
find . -path './target' -prune -o -type f -size +20M -print0 | xargs -0 du -sh 2>/dev/null | sort -hr | head -30
```

## Packaging rule

A normal Envelope release package should contain only:

```text
envelope
LICENSE
README.md or short install note
optional completions/manpage if already generated and small
```

Never package:

- `target/`
- `target/release/`
- `target/release/deps`
- `target/debug`
- incremental or build caches
- local SQLite DBs
- credentials or profile state

## Verification commands

```bash
export HOME=/Users/wondermonkey
export PATH=/Users/wondermonkey/.cargo/bin:/Users/wondermonkey/.local/bin:$PATH
cargo build --release -p envelope-email
strip target/release/envelope || true
ls -lh target/release/envelope
```

Then build the tarball from explicit files only. Do not use a broad `tar target/release` pattern.

## Release version and artifact naming

Before calling a release complete, verify the workspace/package version and artifact name match the intended release. In the v0.6.0 OTP runtime work, the packaging script produced a healthy-size tarball but still named it `v0.5.0` because `workspace.package.version` had not been bumped. Size alone is not enough.

Checklist:

```bash
grep -n 'version = ' Cargo.toml crates/*/Cargo.toml
./scripts/package-release.sh
find dist -maxdepth 2 -type f -print0 | xargs -0 du -sh 2>/dev/null | sort -hr
```

Confirm:

- `workspace.package.version` is the intended release version.
- tarball and directory names include the intended version.
- stale `dist/` artifacts from prior versions are not uploaded by accident.
- `/dist` is gitignored unless the project intentionally commits release artifacts.

## macOS tarball hygiene

On macOS, `tar` can silently include AppleDouble `._*` metadata files when source files have extended attributes, especially in Dropbox-backed directories. A normal `tar -tzf` listing may look clean enough at a glance, but Python `tarfile` inspection can reveal entries like:

```text
._envelope-v0.6.0-darwin-arm64
._LICENSE
._README.md
._envelope
```

This is a release blocker for Envelope: the artifact should be boring and portable. Build tarballs with `COPYFILE_DISABLE=1`, and inspect them with Python before declaring release-ready:

```bash
COPYFILE_DISABLE=1 tar -C "$DIST_DIR" -czf "$TARBALL" "$(basename "$PACKAGE_ROOT")"
python3 - <<'PY'
import tarfile, pathlib
p = pathlib.Path('dist/envelope-v0.6.0-darwin-arm64.tar.gz')
with tarfile.open(p, 'r:gz') as tf:
    names = tf.getnames()
print('\n'.join(names))
bad = [n for n in names if '/._' in n or n.startswith('._') or '__MACOSX' in n or 'target/' in n or '/deps/' in n]
print('BAD_ENTRIES=' + repr(bad))
raise SystemExit(1 if bad else 0)
PY
```

Preferred packaging script behavior:

- remove and recreate `dist/` at the start of packaging, so stale prior-version artifacts cannot be uploaded by mistake;
- copy only `envelope`, `LICENSE`, and `README.md` into a staging directory;
- create the archive with `COPYFILE_DISABLE=1`;
- enforce binary and tarball size gates;
- verify with Python `tarfile` that no `._*`, `__MACOSX`, `target/`, or `deps/` entries exist.

## Final release-shaped checkpoint

Before saying an Envelope release is ready, verify the live repo state instead of relying on prior session memory:

```bash
export HOME=/Users/wondermonkey
export PATH=/Users/wondermonkey/.cargo/bin:/Users/wondermonkey/.local/bin:$PATH

git branch --show-current
git status --short --branch
git log --oneline --decorate --max-count=8
grep -n 'version = ' Cargo.toml crates/*/Cargo.toml
cargo fmt --check
cargo test --workspace
./scripts/package-release.sh
python3 - <<'PY'
import tarfile, pathlib
p = next(pathlib.Path('dist').glob('envelope-v*-darwin-arm64.tar.gz'))
with tarfile.open(p, 'r:gz') as tf:
    names = tf.getnames()
bad = [n for n in names if '/._' in n or n.startswith('._') or '__MACOSX' in n or 'target/' in n or '/deps/' in n]
print('NAMES=' + repr(names))
print('BAD_ENTRIES=' + repr(bad))
print('TARBALL_BYTES=' + str(p.stat().st_size))
raise SystemExit(1 if bad else 0)
PY
```

If `cargo test --workspace` only emits warnings, remove trivial release-noise warnings before packaging when the fix is obviously safe. In one v0.6.0 pass, removing an unused `axum::http::StatusCode` import from `crates/dashboard/src/handlers/folders.rs` made the release output clean and was committed separately.

Treat untracked active planning docs carefully. For Envelope, active build/release plans belong in Todoist comments by default; do not commit repo docs just because they are present unless Tyler explicitly wants durable architecture/product docs in the repo.

A healthy v0.6.0 macOS arm64 package measured around:

```text
binary: 9.47 MiB
tarball: 4.55 MiB
tar contents: package dir, LICENSE, README.md, envelope
```

## Product/operator note

When Tyler flags a large release or target size, answer concretely with measured sizes. Distinguish build cache size from shippable artifact size. The right Envelope story is a small, operator-grade CLI; large artifacts erode trust before the user even runs it.
