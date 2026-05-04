---
name: klasificados-visual-review-evidence
description: Prepare Klasificados visual/design story review tasks with taste-bearing ACs, accessible screenshot evidence, and provenance checks before asking Tyler to approve.
version: 1.0.0
author: Nagaklas
---

# Klasificados Visual Review Evidence

Use this when preparing or reviewing a Klasificados visual/product story, especially landing pages, insight/entity pages, hero images, listing cards, Boriquen mode, or other UI work where taste matters.

## Core rule

Do not ask Tyler to approve visual work from prose alone.

A review task is not ready unless it includes:
1. the story's user-experience point,
2. taste-bearing acceptance criteria,
3. accessible screenshots or image links demonstrating those ACs,
4. known caveats and a clear decision request.

Repo-local paths are not enough in Todoist. Tyler cannot see `ops/qa/...png` from the Todoist UI. Attach screenshots, link to accessible hosted images, or deliver the images in chat using `MEDIA:` and make Todoist summarize what each screenshot demonstrates. Repo paths may appear only as secondary technical evidence.

## AC style

Acceptance criteria should describe the experience and important edge cases, not obvious implementation checks.

Bad headline ACs:
- image returns 200
- tag exists
- page loads
- screenshot captured

Good headline ACs:
- Condado real estate evokes intrigue and aspirational luxury while remaining authentically Condado.
- Condado rentals feels livable, sunny, neighborhood-specific, and useful to someone imagining the move.
- The treatment matches Klasificados style: warm, practical, Boricua-aware, premium but approachable.
- Mobile preserves the same feeling with a strong crop, readable title, and no awkward image/text collision.
- Boriquen mode adds a specific local flourish without parody or clutter.
- No-hero or weak-data pages degrade gracefully and still feel intentional.

Technical checks belong under `Evidence` or `QA notes`, not as the main ACs.

## Screenshot evidence checklist

For a visual story, capture the smallest useful set:
- desktop primary happy path,
- mobile primary happy path,
- one adjacent/variant page,
- one edge/fallback state,
- Boriquen/local flourish if relevant,
- before/after or side-by-side when judging redesign quality.

For each screenshot, state what it proves or fails to prove. Example:
- `desktop Condado real estate`: tests aspirational/authentic Condado feel.
- `mobile Condado real estate`: tests crop, title readability, and above-fold balance.
- `Toyota no-hero fallback`: tests graceful fallback without broken-looking blank hero.

## Provenance before regeneration

Before declaring generated assets bad or regenerating them, investigate what was already built.

Check:
1. current branch and worktrees: `git worktree list --porcelain`;
2. branches matching the story/domain: `git branch --all | grep -Ei 'hero|story-306|visual|entity'`;
3. relevant commits across all history: `git log --all --oneline --grep='hero\|story-306\|entity'`;
4. local scripts such as `scripts/generate_entity_heroes.py`;
5. asset commits and dimensions/provenance metadata;
6. local generated-image caches or prunable Codex worktrees if prototype quality may live outside the committed branch;
7. session history for prior prototype/model calibration.

For story-309 specifically, useful known anchors:
- `f4db7ed` added `scripts/generate_entity_heroes.py` for story-306.
- `c7d105a` added the 75 generated hero assets for story-306.
- PR #58 / branch `feat/overnight-seo-curator-20260421` carried the original batch.
- The script default was Codex image generation with `gpt-5.4-mini` and reasoning `low`, but PNG metadata did not prove which model generated a specific asset.
- Some committed assets were `1536x1024`, while the intended hero ratio was `1536x864`; page CSS using `height:auto` let those oversized images dominate above the fold.

## Review-task structure

Use this structure in Todoist descriptions:

```text
Story definition
As a [user], I want [experienced outcome], so [business/user result].

Point of the story
This is about [taste/user perception], not merely [technical fact].

Current status
What is proven in plain English.

Acceptance status
- AC 1: pass/fail/caveat.
- AC 2: pass/fail/caveat.

Screenshot evidence
Accessible image links or attached screenshots, each with what it demonstrates.

Technical evidence, secondary
Branch, commit, worktree, report path, tests.

Decision needed
Accept direction / move to staging / send back for specific polish.
```

Keep the title short. Do not stuff a full status report into the Todoist title.

## Visual doctrine / style-guide artifacts

When the story is a visual style guide, brand doctrine, or AC template rather than a rendered UI change:

1. Build it as a durable docs artifact, preferably in a clean worktree when the root repo is noisy.
2. Cross-check every “current truth” claim against actual CSS/templates/backlog artifacts. Do not let target-state taste doctrine masquerade as current implementation.
3. Label differences explicitly: “current CSS does X; future polish may choose Y.” This is especially important for token and component rules such as listing-card price color/weight.
4. Run an adversarial verifier against the doc and the source CSS/templates. Ask it to find unsupported token claims, internal contradictions, and “authoritative” language that overstates repo reality.
5. Patch the doc until the verifier’s blockers are resolved, then record both the original finding and the fix in the handoff.
6. The next gate for a brand doctrine doc is Tyler/DUX taste review, not browser QA, unless code or visible pages also changed.

## Insight/entity route verification and blocker drilldown

For Klasificados insight/entity pages, a visible hero pass on adjacent routes does not prove the accepted route is deployable. Verify every route named in the story, including sparse-data and no-hero fallback routes.

When an `/insights/{category}/{entity}/` route returns 404 or `Entity overview not found`:

1. Treat it as `PLAYWRIGHT_VERIFICATION` failure until explained; do not silently replace the route.
2. Query the `entity_overviews` table read-only for the category/entity slug and nearby aliases. Check `category`, `entity_slug`, `canonical_name`, `status`, `listing_count`, and `search_template`.
3. Compare the result to the route logic in `get_or_generate_entity_overview_by_slug`: only published records meeting `min_publish_listings()` render directly; draft or sparse records can produce a legitimate 404 under current rules.
4. Classify the blocker precisely:
   - `DATA_RISK` when the route exists only as sparse/draft data and publishing it would expose weak public insight pages.
   - `SPEC_GAP` when the accepted route in the story is wrong or ambiguous.
   - `PLAYWRIGHT_VERIFICATION` when the data should render but browser/route evidence still fails.
5. The review packet must name the exact route, environment, data source, query result, and safe default. For example: “`/insights/vacation-rentals/boqueron-vacation-rentals/` returns HTTP 404 because matching `vacation_rentals/boqueron-vacation-rentals` rows are `draft` with `listing_count=2`, below publish threshold. Safe default: do not deploy until data/product routing is decided.”
6. If a small unrelated browser resource error is safely fixable, such as `/favicon.ico` returning 404, fix it and rerun tests/route checks, but do not upgrade the story gate if a required accepted route still fails.

## Category/listing page above-fold rule

For category listing pages (`/listings/{category}/?...`), the primary job is helping buyers search and scan listings. Do not let SEO/insight modules bury the marketplace content.

When Tyler says “don’t bury the good stuff,” implement and verify this order:

```text
page header/title/count
subcategory chips
search/filter controls
sort control
listings grid OR no-results block
pagination, when present
Popular Insights / entity-links
```

Regression tests should render `listings/list.html` through the real `clasificados.routes.listings.jinja_env` and assert source order for:
- populated listings with pagination,
- populated listings without clickable pagination,
- empty/no-results state,
- `top_entity_pages=[]` omission,
- entity link label + href preservation when populated.

Browser evidence should open a real local/staging route such as `/listings/vehicles/?sub=suvs` and confirm title/chips/filters/results appear before any `entity-links` section. If local data has no entity links, combine DOM order evidence with the populated-template regression test.

## Common failure modes

- Listing local screenshot paths but not making screenshots visible.
- Calling a visual story ready because a route renders.
- Treating technical DoD as acceptance criteria.
- Forgetting mobile and edge/fallback states.
- Failing to compare against the prototype quality Tyler saw.
- Regenerating assets without checking prior branches, worktrees, and generator scripts.
- Allowing oversized images to consume the entire above fold.
- Calling a visual doctrine “authoritative” when it mixes current repo truth with target-state guidance.
- Leaving style-guide contradictions between broad brand rules and component-specific current CSS, such as saying red owns prices while current listing cards use black price text.
- Treating adjacent successful insight routes as proof that a sparse accepted route is valid.
- Reporting “no errors” after fixing secondary resource errors while a required insight route still returns HTTP 404.
