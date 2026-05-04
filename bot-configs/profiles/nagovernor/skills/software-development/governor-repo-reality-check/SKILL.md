---
name: governor-repo-reality-check
description: Verify the actual canonical/public state of the Governor repos before making positioning or architecture claims. Use when asked which Governor is public, canonical, or safe to publish; when repo naming/layout looks ambiguous (e.g. governor2 nested under governor); or when docs may be stale. Emphasizes filesystem and git evidence over repo guide prose.
---

# Governor repo reality check

Use this when you need to answer questions like:
- Which implementation is actually public?
- Is `governor2` really the successor or just an internal fork?
- Does the directory layout create branding, safety, or disclosure risk?
- What URL is the claimed public repo coming from?

## Principle

Do not trust `CLAUDE.md`, README language, or memory by itself for canonicality questions. Treat them as claims to verify against the actual tree.

## Procedure

1. Inspect the real directory structure under `~/Dropbox/Code/governor`.
   - Use file search, not assumptions.
   - Check for `public/`, `prototype/`, `governor2/`, `nvidia-package/`, `docs/`.

2. Verify the implementation language from actual entrypoints.
   - Read `public/governor` and look at the shebang/imports.
   - Check whether `public/` contains Rust project markers like `Cargo.toml`, `src/*.rs`, or other build metadata.
   - If absent, do not describe it as Rust just because another doc implies a rewrite exists.

3. Distinguish doc claims from filesystem facts.
   - Read `CLAUDE.md` or repo docs only after you know the tree.
   - If docs say “published Python version is at ../public/”, verify that directly.
   - Call out mismatches explicitly.

4. Verify public URL claims from the right source.
   - If there is no `.git` directory in `public/`, do not claim git metadata proves anything.
   - Read `public/README.md`, install scripts, and release references for claimed URLs.
   - Phrase carefully: “the README points to …”, not “git confirms …” unless git actually confirms it.

5. Check repo boundaries and nesting risk.
   - Determine whether `governor2/` is a nested git repo with its own `.git`.
   - Determine whether the parent `governor/` is also a repo or just a container directory.
   - Flag the risk clearly if the legacy/public implementation and the proprietary successor live under the same brand root.

6. Report in three buckets.
   - Actual state
   - Claimed state in docs
   - Risk / recommended cleanup

## Output template

Use this structure:

- Actual state:
  - `public/` contains …
  - `governor2/` contains …
  - public implementation language is …
  - public URL evidence comes from …
- Docs currently claim:
  - …
- Mismatch / risk:
  - …
- Recommendation:
  - rename / split / demote legacy / clarify canonical product line

## Pitfalls

- Do not infer “public repo” from a README clone URL alone.
- Do not infer “canonical Governor” from the directory name alone.
- Do not say the public version is Rust unless you found Rust project structure in `public/`.
- Do not ignore nested-repo confusion; it is both a product-positioning problem and a potential disclosure hygiene problem.

## Why this matters

For Governor, branding, repo structure, and public/private boundaries are product work. A sloppy answer here can misstate what is open, what is proprietary, and what buyers or developers are actually seeing.