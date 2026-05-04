---
name: git-history-via-gh-api-dropbox-placeholder
description: Read git history of a repo that lives in a Dropbox (or iCloud) online-only folder where .git is a zero-byte placeholder. Use the gh CLI + GitHub REST API to fetch commits, branches, and PRs without needing the local .git hydrated.
when_to_use: Triggered when a repo path under ~/Dropbox, ~/iCloud, or similar cloud-sync directory returns "not a git repository" even though .git exists, or when `.git/HEAD` is 0 bytes. Also use when you need quick commit history and don't want to wait for cloud-sync hydration.
---

# Read git history when .git is a cloud-sync placeholder

## Trigger / symptom

You try `git -C <path> log` and get:

```
fatal: not a git repository (or any of the parent directories): .git
```

…even though `ls <path>/.git` shows files. Check file sizes:

```bash
stat -f "%z" <path>/.git/HEAD
```

If the size is **0** and you see `com.dropbox.placeholder` in `xattr -l`, the `.git` directory is an online-only Dropbox placeholder. iCloud uses similar placeholders with `com.apple.CloudDocs`. `brctl download` only works for iCloud CloudDocs paths, not Dropbox.

On this machine, you cannot force Dropbox to hydrate from the shell reliably — the user has to right-click → "Make available offline" in Finder. Don't block on that. Go remote.

## Fast path: use gh CLI against the remote

Works if `gh auth status` is already logged in.

1. Find the repo (owner/name):
   ```bash
   gh repo list <likely-owner> --limit 50 | grep -i <repo-name-fragment>
   ```
   If you don't know the owner, try `gh api user --jq .login` or grep the user's memory/notes.

2. Recent commits on default branch:
   ```bash
   gh api repos/<owner>/<repo>/commits -X GET -f per_page=25 \
     --jq '.[] | "\(.sha[0:7]) \(.commit.author.date[0:19]) \(.commit.author.name) — \(.commit.message | split("\n")[0])"'
   ```

3. Commits on a specific branch:
   ```bash
   gh api repos/<owner>/<repo>/commits -X GET -f sha=<branch-name> -f per_page=25 --jq '...'
   ```

4. List branches (e.g. looking for a feature branch the user named):
   ```bash
   gh api repos/<owner>/<repo>/branches -X GET -f per_page=100 --jq '.[].name'
   ```

5. Open PRs for a branch:
   ```bash
   gh pr list --repo <owner>/<repo> --head <branch-name> --state all
   ```

6. Diff / files for a commit:
   ```bash
   gh api repos/<owner>/<repo>/commits/<sha> --jq '.files[] | "\(.status) \(.filename)"'
   ```

## Pitfalls

- `gh repo list` without `--limit` defaults to 30 results; raise it when grepping.
- `gh api` pagination: default `per_page` is 30. Use `-f per_page=100` and loop with `-f page=N` for deep history.
- Branch names with slashes (e.g. `scraper-pipeline-cloudflare-recovery`) work fine as `-f sha=<name>` to the commits endpoint, but URL-encode if you construct the path manually.
- `git clone` into /tmp is an option if the repo is small, but for just reading history the API is faster and doesn't touch disk.
- Don't suggest `brctl download` for Dropbox paths — it errors with "Path is outside of any CloudDocs app library."
- Klasificados specifically lives at `/Users/wondermonkey/Dropbox/Code/klasificados` (capital C in Code) and is `tymrtn/klasificados` on GitHub.

## Verification

After fetching, confirm you got real data by checking the latest SHA against what the user expects (e.g. "does the last commit mention the feature they just merged?"). If the API returns empty, double-check owner/repo spelling and that the branch exists (`gh api .../branches`).
