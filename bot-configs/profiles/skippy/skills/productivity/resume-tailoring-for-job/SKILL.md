---
name: resume-tailoring-for-job
description: Produce a job-tailored 1-page resume for Tyler from his existing CV archive + project dossier + the target job description. Two-subagent parallel research (dossier + JD parse) then synthesis to markdown + pandoc/weasyprint PDF. Use when Tyler shares a job URL and asks for a resume.
tags: [resume, cv, job-application, pandoc, weasyprint, tyler, subagent]
triggers: ["resume for", "put together a resume", "apply for", "tailor my CV", "job application", "one-pager resume"]
---

# Resume Tailoring for a Target Job

## When to Load
Tyler shares a job URL (Ashby, Greenhouse, Lever, LinkedIn, company careers page) and asks Skippy to build a resume. Or he asks to refresh his CV. This skill produces a tightly-targeted 1-page PDF, not a generic dump.

## Tyler's source material
- **CV archive**: `~/Documents/CVs + Resumes/` — multiple old CVs as PDFs (.pages files also exist but pdftotext works on the PDFs).
- **Recent positioning**: tmrtn.com (personal) + u1f99e.com (Lobster Labs umbrella for his AI projects).
- **GitHub**: github.com/tymrtn.
- **Project READMEs**: `~/Dropbox/Code/` has the bulk (Clef Music Provenance, expatriator, klasificados, openclaw, SECOM, governor).
- **USPTO folder**: `~/Dropbox/USPTO/` — 9 patent folders. For resumes, use TITLES ONLY unless Tyler says otherwise.
- **Standing identity** (memory): BMI senior web engineer since 2022; Clef, Governor, Aposema, Expatriator, Klasificados, Hermes, Envelope as active agentic projects.

## macOS sandbox gotcha (read this first)

`~/Documents`, `~/Desktop`, and `~/Downloads` are protected by macOS. The Hermes venv python (`/Users/tylermartin/.hermes/hermes-agent/venv/bin/python*`) must be granted **Full Disk Access** in System Settings → Privacy & Security. If `ls ~/Documents` returns "Operation not permitted", tell Tyler to:

1. Open System Settings → Privacy & Security → **Full Disk Access** (not Files & Folders — that one is reactive)
2. Click **+**, then **Cmd+Shift+G** in the picker, paste `/Users/tylermartin/.hermes/hermes-agent/venv/bin/` and pick `python3` (or `python`)
3. Toggle on

The Files & Folders page does NOT have a + button — apps have to ask, and Hermes gets denied silently. Full Disk Access is the right panel.

Alternative if Tyler refuses permission: have him `cp -r ~/Documents/CVs\ +\ Resumes ~/Dropbox/` once. Dropbox is always readable.

## Workflow: two parallel subagents + synthesis

### Phase 1 — Find the CVs first, fast

```bash
ls -lt ~/Documents/CVs\ +\ Resumes/*.pdf | head -10
```

Pick the 2–3 most recent PDFs. Don't convert .pages files; find the PDF export alongside each.

### Phase 2 — Spawn two subagents in parallel via delegate_task

**Subagent A: Build Tyler's current dossier**
- Toolsets: `["file", "terminal", "browser", "web"]`
- Extract text from recent CVs via `pdftotext`
- Scrape tmrtn.com + u1f99e.com
- List GitHub repos via GitHub API (not web scrape — faster, no truncation):
  `curl -s "https://api.github.com/users/tymrtn/repos?per_page=100&sort=updated" | jq ...`
- Read top-level READMEs for the active projects listed in Tyler's memory
- Harvest patent TITLES only from `ls ~/Dropbox/USPTO/ | grep -i Patent`
- Output: `/tmp/tyler_dossier.md` with sections: Current Positioning, BMI, Agentic Projects (ranked by relevance), Patents (titles), Earlier Career (condensed), Skills, Links

**Subagent B: Parse the JD**
- Toolsets: `["browser", "web", "file"]`
- `web_extract` the job URL first, fallback to `browser_navigate` if truncated (Ashby pages often truncate mid-content; browser gets the "Not a Fit" and comp sections)
- Output: `/tmp/<job>_jd.md` with: Title, Comp, Location, Responsibilities, Requirements, Named Technologies, Repeated Keywords (signal what they grade for), Tone. Then a RESUME TARGETING GUIDE with 6–8 must-include keywords, 3–5 proof points mapped to Tyler's projects, things to de-emphasize, and a summary-line tone recommendation.

Both subagents run ~70 iterations max. Total wall clock ~10 minutes.

### Phase 3 — Synthesize to markdown

Read both output files, then write to `/tmp/resume_<target>/tyler_martin_resume.md`. Structure:

1. **Header** (1 line): Name + role title + location + contacts on one line to save space.
2. **Summary** (2–3 lines): Use the JD's keyword register verbatim. If they say "composable primitives", you say "composable primitives." If they say "autonomous agents", use that phrase. No generic "passionate engineer" language.
3. **Experience**: BMI first (current anchor), then Lobster Labs as founder/solo engineer umbrella.
4. **Agentic & Open-Source Projects**: 5–7 projects, ranked by JD relevance. Each = 1 paragraph. Lead each with the bold project name and a one-line "what it is", then: stack/agent surface/role/live URL. Include MCP/agent keywords because everyone grades for them now.
5. **Patents**: Default to a SINGLE italic one-liner at the end of the Projects section, NOT a numbered list. Tyler's preference (confirmed 2026-04-17): patents visually overweight a resume unless directly relevant. Use the form `*Adjacent IP work: 6 patent applications (USPTO — U.S. Patent & Trademark Office) filed and under examination in AI content provenance / royalty attribution / agent policy (Clef, Aposema, Governor).*` — spell out USPTO on first use since hiring managers outside IP law won't recognize the acronym. Only use a full numbered section if Tyler explicitly asks OR if the role is IP-law-adjacent.
6. **Skills**: Grouped. AI/Agent Infra first (MCP, Claude Agent SDK, Claude Code, tool-calling, RAG, prompt engineering, structured outputs). Then languages/stack/shipping.
7. **Earlier Career**: One dense line with all pre-2022 roles separated by "·". Saves vertical space.

### Phase 4 — Render to PDF at exactly 1 page

```bash
# Tools available on Tyler's machine
which pandoc weasyprint
# /opt/homebrew/bin/pandoc and /opt/homebrew/bin/weasyprint
```

Render command:
```bash
cd /tmp/resume_<target>
pandoc tyler_martin_resume.md -o tyler_martin_resume.pdf --pdf-engine=weasyprint -c style.css
pdfinfo tyler_martin_resume.pdf | grep Pages  # MUST say "Pages: 1"
```

If page count is 2+:
- Tighten `style.css` first (smaller margins/fonts) before cutting content
- Put patent titles in a 2-column `ol { column-count: 2; column-gap: 14pt; }` — saves 4 lines
- Collapse earlier career to one horizontal line with `·` separators
- Only after CSS tuning, cut content

### Phase 5 — Deliver

```bash
mkdir -p ~/Dropbox/Skippy/resumes
cp /tmp/resume_<target>/tyler_martin_resume.* ~/Dropbox/Skippy/resumes/
```

Then send to Telegram as `MEDIA:/tmp/resume_<target>/tyler_martin_resume.pdf` — Tyler CANNOT see files by path on Telegram. MEDIA is required.

## CSS that reliably fits 1 page

```css
@page { size: letter; margin: 0.4in 0.5in; }
body { font-family: "Helvetica Neue", "Arial", sans-serif; font-size: 9pt; line-height: 1.25; color: #1a1a1a; }
h1 { font-size: 16pt; margin: 0; letter-spacing: -0.3pt; }
h1 + p { margin: 0 0 2pt 0; font-size: 8.5pt; }
h2 { font-size: 9.5pt; text-transform: uppercase; letter-spacing: 0.5pt; margin: 7pt 0 3pt 0; padding-bottom: 1pt; border-bottom: 1px solid #333; color: #000; }
hr { display: none; }
p { margin: 2pt 0; }
ul { margin: 2pt 0; padding-left: 14pt; }
li { margin: 1pt 0; }
strong { color: #000; }
a { color: #0b5ed7; text-decoration: none; }
em { color: #444; }
ol { padding-left: 16pt; margin: 2pt 0; column-count: 2; column-gap: 14pt; }
ol li { break-inside: avoid; }
```

Weasyprint will warn about some modern CSS (`gap: min()`, `overflow-x: auto`) — those are coming from GitHub-flavored markdown defaults and can be ignored.

## Voice / Tone Rules

- **Always lead with the JD's voice.** If they say "ships production agents," you say that. Mirror their verbs.
- **No "passionate/cutting-edge/results-driven"** — AI-coded filler.
- **Use present-tense active verbs**: "ships", "authors", "runs", "maintains" — not "responsible for" or "worked on".
- **Patents framed as BUILDER output, not RESEARCH.** Applied roles want shipping IP, not academic posture. Say "9 USPTO filings" not "research publications."
- **BMI line is the credibility anchor.** Even if it's "just" senior web engineer, it's 3+ years at a major rights org. Don't downplay.
- **Older career = 1 dense line.** MCS · Parallel 18 · MiGym · Actigram · Curelator · ZenCash · Shotwell. Dates optional.

## Pitfalls

- **pdftotext misses .pages** — only works on PDFs. Find the PDF alongside the .pages.
- **web_extract truncates Ashby at ~5000 chars** — fall back to browser_navigate for the full JD, including comp and disqualifiers.
- **GitHub repos page truncation** — use the API, not browser scraping. `curl https://api.github.com/users/tymrtn/repos?per_page=100` is faster and complete.
- **Don't fabricate metrics.** If dossier lacks "10K users" style numbers, don't invent them. Tyler reviews before send.
- **Target the seniority precisely.** If JD says "Senior IC, no management language," cut VP/Director framing from Tyler's older career. He's over-credentialed for IC roles — don't flag that unless asked.
- **Patents: TITLES ONLY, and default to a single italic one-liner** unless Tyler says otherwise. Tyler confirmed 2026-04-17: full patent sections overweight the resume for most roles. One-liner form documented in Phase 3 above.
- **Spell out USPTO** on first mention — "patent applications (USPTO — U.S. Patent & Trademark Office)". Don't assume hiring managers know the acronym.
- **Count only ACTUALLY FILED patents.** As of 2026-04-17 that's 6 at USPTO (1, 2, 3, 4, 5, 8). Others like 6/7/9 are in drafting or concept. Never claim "9 USPTO filings" — that was a hallucination corrected mid-session.
- **Project URLs are canonical.** Always use `klasificados.net` (the public site), NOT `api.klasificados.net` (internal). Tyler corrected this explicitly 2026-04-17. When in doubt about which domain is the public-facing one for a Lobster Labs project, ask rather than guess — `api.*`, `www.*`, and bare domain can all exist with different purposes.
- **Always confirm project attribution.** wp-ai-image-gen is standalone, NOT Aposema. drforth.ai is standalone. Don't bundle projects for narrative convenience.
- **Location line matters for remote-first US roles.** Use: "San Juan, PR (USA) + Madrid · US + Canadian citizen · Remote-ready for US/CA" — signals domestic worker (PR is US), dual citizenship removes work-auth friction for US+CA.
- **The BMI killer line** is Musark + 20+ Claude Skills + Award Show workflow (80h → 8–10h). This is the single most valuable evidence Tyler has for "ships AI tools non-technical teammates rely on" — it's a metric, a named system, and a measurable reduction. Adopters are **Award production staff, content teams, and QA** (confirmed 2026-04-17) — use those groups, not generic "non-technical ops".
- **drforth.ai** is Tyler's OG Telegram-native productivity/performance agent (Anthropic + OpenRouter, 8+ custom MCP tools). Include in agentic projects list, belongs between Expatriator and any other Telegram-native work.
- **IETF AIPREF** — Tyler is an active contributor to the IETF AI Preferences Working Group (AIPREF). Include as a single italic line in the Agentic Projects section for AI/rights/standards-adjacent roles: `*Active contributor to the **IETF AI Preferences Working Group (AIPREF)** — shaping standards for how AI systems signal and respect content-owner preferences.*`
- **Open the final markdown in Cursor after render** so Tyler can make manual edits before PDF regeneration: `open -a Cursor ~/Dropbox/Skippy/resumes/tyler_martin_resume.md`. Tyler prefers editing the source directly over round-tripping through Skippy for small tweaks.

## Skills NOT to load

- `cold-email-anti-ai` is for sales outreach voice — **not resumes**. Resume voice is different (builder-direct, no Zeigarnik tricks).

## After synthesis

Offer to also draft:
1. Cover letter (brief, in JD's voice)
2. Ashby/Greenhouse application short-answer fields
3. LinkedIn message to the hiring manager if findable

Don't produce these automatically — Tyler may want to write them himself or skip.

## Known good output example

TLDR.tech Senior Software Engineer Applied AI — produced Apr 17 2026. Saved to `~/Dropbox/Skippy/resumes/tyler_martin_resume.pdf`. 1 page, 9pt Helvetica, BMI + 6 agentic projects + 9 patent titles + skills + condensed earlier career. Targeted the "ships production AI agents and composable Claude Skills" phrase verbatim from the JD.
