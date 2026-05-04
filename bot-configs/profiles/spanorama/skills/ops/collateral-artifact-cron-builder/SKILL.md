---
name: collateral-artifact-cron-builder
description: Build reviewable non-code collateral artifacts in autonomous cron slots, with Todoist SSOT updates and Telegram-accessible MEDIA attachments.
version: 1.0.0
author: Spanorama
---

# Collateral Artifact Cron Builder

Use when an autonomous cron slot needs to produce a non-code business artifact: partner one-pager, approval packet, PDF, HTML/Markdown collateral, launch copy, outreach attachment, or reviewable sales/support asset.

## Core pattern

1. **Fetch Todoist first, but preserve the human intent**
   - Fetch the target task and comments.
   - Identify the plain-language outcome the user wants, for example: “make partners take Dorado seriously,” not merely “satisfy these checklist items.”
   - A groomer/go-signal is useful when it exists, but do not let missing grooming become process theater. If the task is clear enough and low-risk, build the useful artifact; if context is genuinely missing, repair the story or research it rather than stopping.

2. **Inspect source docs before writing**
   - Read the task description and every required repo/source reference.
   - Preserve promise boundaries, approval rules, and acceptance criteria.
   - For SpainExpat/Dorado collateral, do not publish publicly or send externally without Tyler approval.
   - Treat missing facts inside the artifact scope as work to do, not blockers to report. Example: if credibility metrics are missing from a partner one-pager, research social reach, traffic estimates, content footprint, historical proof, and local evidence before declaring anything blocked.

3. **Use research subagents when credibility depends on evidence**
   - Split evidence collection by surface: live web/site, social/X, and local repo/database artifacts.
   - If requested tools such as Firecrawl or xitter are unavailable, explicitly record that capability status, then continue with public/browser/local fallbacks rather than stopping.
   - Distinguish first-party metrics, public estimates, historical claims, and qualitative proof. Never invent numbers.
   - Feed usable proof bullets back into the collateral story and link the source report in Todoist.

4. **Create durable repo artifacts**
   - Prefer `docs/outreach/` for partner/outreach collateral.
   - Prefer `ops/artifacts/` for internal review packets.
   - Use deterministic filenames with date and story slug.
   - Keep editable source (`.md`, `.html`, or `.tex`) as well as exported PDF when possible.

4. **Make it accessible to Tyler**
   - If final delivery is Telegram or another chat channel, include `MEDIA:/absolute/path` in the final response for the review artifact.
   - Do not report only a local filesystem path when the prompt requires an accessible artifact.

5. **Verify before reporting**
   - Confirm files exist and are non-empty.
   - For PDFs, run `pdfinfo` and verify page count.
   - Use `pdftotext` to spot-check that the generated PDF contains the intended content.

6. **Update Todoist before ending**
   - Add a bot-attributed Todoist comment with artifact paths, verification result, no-send/no-publish status, and next gate.
   - If Tyler review/approval is now required, add `needs-approval` while preserving existing labels.

## PDF generation notes on macOS

Tool availability varies. Check first:

```bash
command -v pandoc || true
command -v tectonic || true
command -v cupsfilter || true
command -v pdfinfo || true
command -v pdftotext || true
command -v rsvg-convert || true
command -v magick || true
```

Pitfalls and fixes:

- `cupsfilter` may fail for HTML to PDF with: `No filter to convert from text/html to application/pdf`.
- `pandoc` may be installed while LaTeX engines are missing. If `pandoc ... --pdf-engine=xelatex` fails with `find_executable`, do not burn the slot installing TeX unless that is explicitly allowed; keep Markdown/HTML and use a bounded fallback.
- `pandoc` + `tectonic` can generate a PDF from Markdown, but default formatting may spill a supposed one-pager into 2 pages.
- For a true one-page PDF, create compact TeX with explicit `geometry`, small font, `multicol`, tight list spacing, and run `tectonic`.
- If no PDF engine is available and the task still needs a reviewable attachment, generate a simple valid PDF from Markdown using Python stdlib only: strip frontmatter/Markdown markers, wrap lines, write basic PDF objects with Helvetica fonts, then verify with `file`, byte size, and byte-search or `pdftotext` if available. Label it as a fallback PDF if it is visually plain.
- If Tyler objects that a plain text PDF is not partner-grade, switch from document formatting to designed collateral: create an SVG/HTML source with brand colors, logo, stat cards, rounded cards, offer pills, and a CTA footer; export SVG to PDF/PNG with `rsvg-convert`. Use `width="8.5in" height="11in" viewBox="0 0 ..."` in SVG so the PDF is letter-sized. ImageMagick `magick` may fail on SVG font handling or embedded data URIs; prefer `rsvg-convert` when available.
- If no brand guide exists, pull working design cues from the live website and asset files: logo, typography feel, palette, layout motifs, decorative elements. Save a small working mini brand guide next to the artifact so future runs do not rediscover the same cues.
- Verify the result with `pdfinfo artifact.pdf | grep '^Pages:'` when available. If `pdfinfo` is unavailable, at least run `file artifact.pdf`, check size, and spot-check that key proof phrases exist in the PDF bytes or extracted text.
- For designed PDFs, also render/export a PNG preview and run `vision_analyze` to catch text collisions, overflow, cramped footer/CTA regions, and brand/readability issues. Patch the SVG/source and re-export until the visual check is clean.
- Keep Markdown/HTML/SVG as source collateral even if the final PDF is generated from TeX or a fallback path. Back up broken/plain prior PDFs before replacing the canonical review path.

## Todoist verification notes

- Some Todoist comment listing calls return oldest comments first even after adding a fresh comment. Do not assume `find_comments(limit=3)` proves your new comment is absent or present. Use the add-comment response IDs as evidence, or paginate/sort if you need to inspect the latest comments.
- To verify description updates, use `fetch_object` on the task after `update_tasks`; for comments, the `add_comments` structured response is usually the most reliable immediate confirmation.

## Minimal success report

```text
Founder read:
- Bet: <commercial/customer-visible value>
- Sweep: <artifact created>
- Evidence: <Todoist link and MEDIA:/absolute/path>
- Decision: ready for Tyler review / blocked / revise
- Next: <single next gate>
```

Never claim publication or sending unless it actually happened and was approved.
