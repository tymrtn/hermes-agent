---
name: governor-workspace-canonicalization
description: Reconcile the local Governor workspace when Rust is canonical but legacy Python trees still exist. Use for repo/layout cleanup, doc correction, and safe migration staging when `governor2/`, `public/`, and `prototype/` coexist and cause canonical-source confusion.
---

# Governor workspace canonicalization

Use this when the Governor workspace has mixed-era material and the goal is to make the Rust implementation canonical without accidentally breaking the active Rust repo or misrepresenting the Python line.

## When to use
- The user says Governor should mean the Rust implementation.
- `governor2/` exists alongside `public/` and/or `prototype/`.
- Docs or skills still describe Python as public/canonical.
- You need to reorganize safely under active Governor dogfood enforcement.

## Core facts
- Treat `governor2/` as the canonical Governor implementation unless the user explicitly says otherwise.
- Treat `public/` and `prototype/` as legacy/reference-only if both still exist in the same umbrella workspace.
- `governor2/` may be its own git repo with active uncommitted work; avoid risky repo-root moves unless explicitly requested and safe.
- Governor dogfood may block directory moves/copies as review-required filesystem mutations.

## Recommended approach
1. Inspect first.
   - Check whether `governor2/` is its own git root.
   - Check whether the local/public GitHub story already points at Rust releases.
   - Search for high-signal docs that still call Python public/canonical.

2. Write the plan before mutating.
   - Save a concrete plan in `.hermes/plans/`.
   - Prefer a low-risk path: fix canonical messaging first, then perform physical moves after review/approval if needed.

3. Update high-signal guidance immediately.
   Patch these first if present:
   - workspace `README.md`
   - workspace `CLAUDE.md`
   - `governor2/CLAUDE.md`
   - Governor health/status docs
   - calibration skills that still point at Python/public as canonical

4. Mark legacy trees explicitly.
   - Update `public/README.md` and `prototype/README.md` to say reference-only.
   - If needed, patch the Python entrypoint header/docstring so future readers cannot mistake it for the canonical product.

5. Attempt physical separation only after messaging is corrected.
   Suggested target paths:
   - `archive/python-public-reference/`
   - `archive/python-prototype/`

6. If filesystem moves are blocked by Governor review:
   - Do not claim the move happened.
   - Create an `archive/README.md` that records intended destination and current limitation.
   - Report clearly that canonical-source correction is complete but physical separation still requires reviewed execution.

## Verification
- Search for phrases like:
  - "published to GitHub"
  - "published Python version"
  - "open-source governor CLI"
  - references implying `public/` is canonical
- Confirm `governor2/` still has a valid `.git` root and normal `git status` output.
- Distinguish historical audit notes from current canonical guidance; patch the latter first.

## Pitfalls
- Do not infer that local `public/` matches the current GitHub canonical repo without checking the live repo/release page.
- Do not move or rename the Rust repo root casually if it has active uncommitted work.
- Do not say the reorganization is complete if Governor blocked the actual move.
- Historical docs like audit reports may still mention Python as public; that is lower priority than current operator docs.

## Good outcome
A successful pass leaves no ambiguity about which implementation is canonical, even if the legacy trees have not yet been physically moved.