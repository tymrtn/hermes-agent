---
name: netlify-static-site-hotfix
description: Find, edit, deploy, and verify a locally linked static Netlify site when the user gives only the live domain or vague site name.
version: 1.0.0
author: Hermes Agent
license: MIT
metadata:
  hermes:
    tags: [netlify, static-site, hotfix, website, copy-edit, deploy]
---

# Netlify Static Site Hotfix

Use this when:
- the user wants a quick content or copy change on a live site
- the live domain is known but the local repo/path is not
- the site is a simple static page or small marketing site

## Goal

Locate the actual local source for the live site, edit it safely, deploy to production, and verify the new copy is live.

## Workflow

1. Verify the live site first.
   - Open the domain in the browser tools.
   - Capture a full snapshot and identify exact phrases currently on the page.
   - Use those phrases as search anchors for the local source.

2. Search locally by page-specific content, not by guessed repo name.
   - Search broad likely roots such as `./Dropbox`, `./Code`, or other common work dirs.
   - Look for unique live-copy strings from the page snapshot.
   - This is more reliable than searching for the domain or project name, especially when the local folder has a different name.

3. Confirm the site root before editing.
   - Read the candidate file(s).
   - Look for domain references, form config, and deploy metadata such as:
     - `og:url`
     - `data-netlify="true"`
     - `.netlify/state.json`
     - `netlify.toml`
   - If present, treat that directory as the deploy root.

4. Edit the source directly.
   - For one-page static sites, expect the main file to be `index.html`.
   - Make targeted copy edits with patch, not manual shell editing.
   - If changing messaging, tighten adjacent lines for consistency, not just the requested sentence.
   - Preserve explicitly protected copy the user said not to change.

5. Check deploy readiness.
   - Run `netlify --version`.
   - Run `netlify status --json` in the site directory to confirm:
     - the account
     - linked site name
     - production URL
     - site ID

6. Deploy to production.
   - For static sites with `publish = "."`, use:
     - `netlify deploy --prod --dir=. --message "<short deploy message>"`
   - Use foreground execution and wait for the production URL confirmation.

7. Verify the deploy two ways.
   - Fetch the raw HTML from the live domain and assert the new phrases are present.
   - Re-open the live page in browser tools and confirm the updated text appears in the snapshot.

## Useful commands

```bash
netlify --version
netlify status --json
netlify deploy --prod --dir=. --message "Refine homepage messaging"
```

## Good search strategy

Search for exact strings from the live page, for example:
- a distinctive heading
- a weird quote
- a branded sentence

Avoid starting with:
- guessed repo names
- guessed domains
- broad filename-only searches

## Pitfalls

- The public product repo may not contain the marketing site. The deployed site can live in a separate local folder.
- A site folder may have no git remote configured. Netlify linkage via `.netlify/state.json` can still prove it is the right directory.
- A local file edit does nothing for the live site until you actually deploy. If the user says the change is still visible, check deploy state before re-editing the copy.
- Browser snapshots may lag briefly after deploy; also verify by fetching live HTML directly.
- If the problem is visual punctuation or decoration, inspect CSS pseudo-elements like `::before` and `::after` — the extra characters may not be in the HTML text itself.
- If the user asks for “the site,” they may mean a local marketing site, not the main application repo.
- Sometimes an old parent workspace remains linked to Netlify and only contains a blanket redirect, while the real new site should live in a fresh child folder (for example `project.com/`). In that case, do not keep editing the parent. Create or update the dedicated site folder, add its own `netlify.toml`, then link that exact folder to the existing Netlify site.
- If `netlify` is not installed globally but Node/npm are present, use `npx --yes netlify-cli ...` instead of stopping.
- `netlify status` can fail in the parent even when the account is authenticated. Use `netlify sites:list` to find the existing site ID, then run `netlify link --id <site-id>` from the real publish directory before deploying.
- After linking, Netlify CLI may modify a parent `.gitignore` to account for `.netlify/`. Expect that side effect and verify it is acceptable before committing.

## Success criteria

- Source file located from live-copy anchors
- Copy updated in local source
- Production deploy completed successfully
- Live site verified with both raw HTML and browser snapshot
