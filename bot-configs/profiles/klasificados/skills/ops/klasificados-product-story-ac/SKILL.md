---
name: klasificados-product-story-ac
description: Write and repair Klasificados product story definitions, acceptance criteria, and Todoist review tasks so they express user/taste outcomes, edge cases, and screenshot evidence instead of developer jargon or pedantic technical DoD.
version: 1.0.0
author: Nagaklas
---

# Klasificados Product Story ACs and Todoist Review Tasks

Use this whenever creating or editing Klasificados backlog stories, Todoist review tasks, visual QA packets, or cron closeouts for product-facing work.

## Core principle

Acceptance criteria are not a checklist of obvious technical mechanics. They should describe the user-experienced outcome, product taste, brand fit, and meaningful edge cases.

Technical proof still matters, but it belongs under evidence / QA notes, not as the headline AC unless the edge case itself is technical.

## Todoist task structure

For review/approval tasks, avoid huge overflowing titles.

Use:

1. Short title
   - Good: `Review story-309: entity hero images on insights pages`
   - Bad: a paragraph of branch names, commit IDs, test results, and blockers

2. Description sections
   - `Story definition`
   - `Point of the story`
   - `Current status`
   - `Acceptance criteria`
   - `Screenshot evidence` when visual/product work
   - `Technical evidence, secondary`
   - `Decision needed` or `Next move`

3. Plain operator language
   - Do not write unexplained developer shorthand in the primary blocker.
   - Replace phrases like `DB-backed browser smoke` with: `open the real/staging page in a browser using real data and confirm the image appears`.
   - Keep branch names, commits, and paths in the evidence section.

## Story definition pattern

Use classic story phrasing, but make it product-specific:

```text
As a [real user/context],
I want [experienced capability or feeling],
so that [business/user behavior changes].
```

Example:

```text
As a gringo landing on an entity curator page from a backlink,
I want the page to make Condado feel desirable, specific, and trustworthy at a glance,
so I keep exploring Klasificados instead of bouncing.
```

## Acceptance criteria standard

Good ACs confirm:
- user-experienced outcome
- taste / brand fit
- important edge cases
- responsiveness / device-specific experience when relevant
- localization / Boriquen mode behavior when relevant
- graceful fallbacks

Bad ACs over-index on:
- HTTP 200
- image tag exists
- metadata exists
- test file passed
- page loaded
- implementation details that are already ordinary DoD

Those can be included as technical evidence, not the primary ACs.

## Visual/product AC examples

For Klasificados visual stories, write ACs like:

- Condado real estate page evokes intrigue and aspirational luxury while still feeling authentically Condado, not generic luxury stock-photo slop.
- Condado rentals feels adjacent but distinct: livable, sunny, neighborhood-specific, and useful to someone imagining the move.
- The treatment visually matches Klasificados style: warm, practical, Boricua-aware, premium but approachable, not overdesigned SaaS gloss.
- Mobile keeps the same feeling: strong crop, readable title, no awkward image/text collision.
- Boriquen mode adds a unique, on-the-nose local flourish without becoming parody or clutter.
- Pages without a suitable asset degrade gracefully and still feel intentional, not broken or half-rendered.
- Repeated entity pages do not feel like a wallpaper dump; a meaningful sample across categories feels curated enough to ship.

## Screenshot evidence requirement

For visual/product stories, do not ask Tyler to approve from prose alone.

Require screenshots that demonstrate the ACs, saved or linked from a durable repo path such as:

```text
ops/qa/story-NNN/screenshots/
ops/qa/story-NNN/visual-evidence-YYYYMMDD.md
```

Screenshot set should match the ACs. For example:
- desktop hero / primary state
- mobile hero / responsive state
- adjacent category or variant
- Boriquen/local flourish
- non-primary example proving repeated pages do not feel generic
- fallback/edge case proving graceful degradation

When possible, use browser/vision assessment or Playwright screenshots and write a concise evidence report.

## Evidence report pattern

Create a markdown report with:

- verdict
- screenshot list
- assessment per AC
- product caveats
- technical proof captured alongside visuals
- acceptance status
- next recommended action

Separate taste findings from technical facts.

Example distinction:

```text
Acceptance status:
- Condado real estate aspirational/authentic: pass with polish caveat.
- Mobile: pass.
- Boriquen flourish: pass.
- No-hero fallback: pass with copy caveat.

Technical proof:
- figure.overview-hero present.
- og:image/twitter:image present.
- fallback page has no broken image slot.
```

## Common Klasificados style cues

Use existing project signals until a fuller style guide exists:

- warm, practical, Boricua-aware
- premium yet approachable
- editorial marketplace, not generic SaaS
- Caribbean marketplace soul, not touristy
- local language can be playful but must remain usable
- Boriquen mode should be specific and witty, not parody
- avoid stock-photo slop and wallpaper dumps
- visual polish must support trust and exploration, not just decoration

## Closeout rule

When you repair a bad Todoist task or AC set:
- update the Todoist task immediately
- keep the title short
- move details into description
- add screenshot evidence requirements if visual
- preserve technical paths/commits as secondary evidence
- if the finding changes a reusable workflow, patch this skill or the cron-design skill
