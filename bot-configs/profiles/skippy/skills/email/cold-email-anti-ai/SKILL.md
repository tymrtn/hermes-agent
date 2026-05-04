---
name: cold-email-anti-ai
description: Write cold outreach emails that don't sound AI-generated. Iterative lessons from real drafting sessions — specific anti-patterns to avoid, human-sounding structures, and Tyler's ranked playbook.
tags: [email, outreach, cold-email, sales, bizdev]
triggers: ["cold email", "outreach email", "pitch email", "draft email to", "reach out to", "write an email"]
---

# Cold Email Anti-AI Playbook

## When to Load
Any time Skippy drafts cold outreach, sales emails, bizdev emails, or pitch emails.

## Core Principle
AI writes at a 5/10 on every dimension — never too casual, never too direct, never too funny. Humans have edges. Pick a direction and commit.

## The AI Tells — Kill These on Sight

### Openings That Scream AI
- "I hope this email finds you well"
- "I came across your company and was impressed by..."
- Any variation of opening with flattery about their recent news, deals, or launches — even specific flattery. "Two major label deals in three months — congrats" is the same move as "I noticed your company," just dressed up.
- Commenting on their press releases back to them is pandering, not personalization.

### The Contrast Construction (Tyler's #1 Catch)
AI loves "I didn't do X, but I did see Y" because it sounds good as a contrasting statement. Real people don't talk in TED talk arcs. Just state what happened and let the reader connect the dots.

BAD: "I didn't build the royalty systems, but I sat close enough to see the gap"
GOOD: "I was an engineer at BMI for years. Royalty accounting is the entire economic backbone of the music industry. Nothing like it exists yet for AI music."

No false modesty. No humble-brag arc. Just the sequence.

### Buzzwords
Kill: streamline, leverage, cutting-edge, robust, seamless, game-changer, comprehensive, optimize, empower, revolutionize, "drive results," "boost your ROI"

### Structural Tells
- Three bullet points listing benefits (the AI trifecta)
- "Would you be open to a quick 15-minute call?"
- Perfect grammar with zero personality
- Every sentence the same rhythm and length
- Compliment → pivot → pitch structure

## What Actually Works

### Tyler's Ranked Rules (from cold-outreach-rules-2026.md)
1. Blank or absurd subject line
2. One-sentence email if possible
3. Short, unserious, slightly funny — under 60 words
4. Zero links or attachments in first email
5. Zeigarnik cliffhanger — leave the thought unfinished
6. Skip first-name fluff; open with the exact "why now" signal
7. One pain point per email, never a feature dump
8. Soft CTA: "Worth a quick look?" not "book a 15-min call"

### Name People, Not Just Companies
- Name competitor CEOs, product leads, specific executives
- "Mikey Shulman is fighting the RIAA. David Ding is signing deals with Warner" — this signals you actually know the landscape
- No AI would risk naming specific names like that
- It's the difference between "your competitors" and knowing who sits in which chair

### Lead With the Prize, Not the Threat
- AI defaults to fear-selling (lawsuits, audits, liability) because threats are easy to structure
- The real pitch is usually the carrot: what becomes possible if this works
- "You get all the publishers, all the artists" beats "you'll survive the audit"
- Frame the upside as market-size expansion, not compliance

### Good News / Bad News
- Natural human framing that AI never uses (trained to be diplomatic)
- "Good news: X. Bad news: Y" immediately feels like a person talking
- Name the specific competitive dynamics — who's winning, who's losing, why

### Insider Knowledge
- Drop one line showing proximity to the world, not credentials
- "I was an engineer at BMI for years" is context, not a resume
- Don't explain what you didn't do — just say what you saw and what you built
- Proximity > credentials. "I sat close enough to see the gap" > "I'm an expert in"

### Tone
- Use contractions
- Casual transitions: "so," "anyway," "honestly"
- Be slightly imperfect — dashes, fragments, asides
- Read it out loud. If you wouldn't say it at a bar, rewrite it.
- Uneven paragraph lengths. One line. Then maybe three together.

## Drafting Process
1. Skippy does the research: names, papers, competitive intel, arXiv IDs, news, deal history
2. Skippy proposes the strategic angle and key ingredients
3. Tyler writes the final draft — his voice is always better. Skippy provides ingredients, Tyler cooks.
4. Skippy cleans typos only. Do not touch Tyler's voice, word choices, rhythm, or casual style.
5. If Skippy must draft: write in Tyler's voice, not Skippy's. Study his actual writing patterns. When in doubt, write shorter and more casual.
6. Deliver drafts via Envelope CLI (`envelope draft create`), not markdown files. Tyler edits in his email client.

### Batch Drafting via Claude CLI
When Tyler wants multiple alternative drafts (e.g. "3 each"), delegate to Claude CLI with the full anti-AI rules baked into the prompt:
- Phase 1 (plan): `cat << 'PROMPT' | claude -p --permission-mode plan --print --max-turns 15 --model sonnet`
- Phase 2 (execute): same prompt with `--permission-mode bypassPermissions`
- Include ALL anti-AI rules in the prompt itself — Claude CLI has no access to this skill
- Include the current drafts as context so it knows what to improve on
- Include specific company intel (deals, valuations, partnerships) so drafts reference real facts
- Specify different ANGLES per draft, not just rewordings — e.g. patent angle, competitor contrast, product-user angle
- Output to a markdown file in the project's biz-dev/drafts/ directory

### Self-Review Checklist (if Skippy drafts)
1. Re-read every opening line — is it flattery in disguise? Cut it.
2. Check for contrast constructions ("I didn't X, but Y") — flatten them.
3. Check for threat-leading — flip to prize-leading if possible.
4. Are people named by name, or just by company? Name them.
5. Is there one line of personal context (not credentials)? Add it.
6. Word count check: under 60 words ideal, 80 max (120-150 if density is earned).
7. Read it out loud. Does it sound like a text you'd send a colleague?

## Envelope CLI — Drafts & Send
```bash
# Create a draft in the server's Drafts folder
envelope draft create --account admin@clef.pro --to "recipient@example.com" --subject "" --body "email body here"

# Create draft with sender identity override (--from)
envelope draft create --account admin@clef.pro --from "tyler@clef.pro" --to "recipient@example.com" --subject "" --body "email body here"

# Send with sender identity override
envelope send --account admin@clef.pro --from "tyler@clef.pro" --to "recipient@example.com" --subject "test" --body "hello"

# List drafts
envelope draft list --account admin@clef.pro

# Discard a draft by UID
envelope draft discard --account admin@clef.pro <uid>

# Send a draft
envelope draft send --account admin@clef.pro <draft-id>
```
- Use blank subject with `--subject ""`
- `--from` overrides the From header while SMTP auth uses `--account` credentials (sender identity feature)
- For Clef outreach: authenticate with admin@clef.pro, send as tyler@clef.pro using `--from`
- If IMAP sync fails, draft is saved locally — check account credentials
- Migadu accounts: the IMAP/SMTP username must match an actual mailbox in Migadu admin
- Use `Re: Subject` as subject line to increase open rates (sneaky but effective)

## Trackable Links for Outreach
Instead of raw URLs in emails, create redirect pages on your own domain for click tracking:
- Create `public/arxiv/2510.08062/index.html` (or similar) with meta refresh + JS redirect
- Include Umami/analytics script with a 1-second delay so the pageview fires before redirect
- Add a fallback `<a>` link for disabled JS
- **Netlify gotcha**: `netlify.toml` redirects override `_redirects` file. SPA catch-all (`/* /index.html 200`) eats custom redirects. Fix: use static HTML files in `public/` instead of redirect rules, OR add `force = true` to specific redirects above the catch-all
- Check which repo Netlify actually deploys from — there may be multiple clones (e.g. `forks/clef-pro-landing` vs `website/`)

```html
<!DOCTYPE html>
<html>
<head>
<script defer src="https://umami.tmrtn.com/script.js" data-website-id="your-id"></script>
<meta http-equiv="refresh" content="1;url=https://target-url.com">
<script>setTimeout(function(){window.location.href="https://target-url.com";},1000);</script>
</head>
<body><a href="https://target-url.com">Click here if not redirected</a></body>
</html>
```

### Open With a Question, Not a Statement
- "Did you see Sony's Attribution-by-Design paper from October?" is instantly human
- No AI cold email ever opens by asking about a specific arxiv paper
- Works as a knowledge test: if they've read it, you're credible. If they haven't, you just handed them intel.
- Reference specific authors by name — it proves you actually read it
- Any published research, news event, or industry paper works as a hook

### Close With a One-Pager, Not a Call
- "I put together a one-pager" is lower friction than "let's hop on a call"
- They can read it in 60 seconds without committing to anything
- Make it an offer, not an attachment: "happy to send it over" — keeps zero-attachment rule

### The Vulnerability Close
- Answer the silent question: "why are you emailing me instead of doing this yourself?"
- "Honestly I'd build this out myself on open source engines, but this fits better with a company that already has the publisher relationships"
- This is flattering without being pandery — you're not saying they're amazing, you're saying they have something you don't
- The word "honestly" signals a human dropping their guard
- **Critical**: Don't overstate relationships. "Already partnered" when you had one conversation and got free credits is a credibility killer if the recipient checks. "Built prototypes on Mureka" is honest. "Already partnered with Mureka" is not. State what you DID, not what you wish the relationship was.
- Flattery that's a user opinion works: "I've used your APIs and I know it's better" — only a builder says that. Totally different from "I was impressed by your innovative platform."

### Stacking Pitch Layers
Best cold emails layer multiple sources of credibility in sequence:
1. Third-party validation (published research, a competitor's move)
2. Competitive urgency (someone else lost/failed at this exact thing)
3. Insider knowledge (proximity to the industry, named people)
4. The prize (what becomes possible — market expansion, not compliance)
Each layer makes the next more credible. Don't lead with yourself.

### Word Count Flexibility
- Under 60 words is ideal for simple outreach
- When the email carries real density (named people, specific research, insider context, a clear prize), 120-150 words can work
- The test isn't word count — it's whether every sentence carries weight
- If you can cut a sentence without losing information, cut it

## Anti-Patterns Discovered in Session
| Draft Problem | What Happened | Fix |
|---|---|---|
| Specific flattery openings | "Two major label deals — congrats" still reads as AI | Lead with the problem or insight, not their news |
| Humble-brag contrast | "Didn't build X, but saw Y" | Just state the facts in sequence |
| Fear-first framing | Every draft about lawsuits and audits | Lead with what becomes possible (the prize) |
| Company names only | "Suno" and "Udio" without naming who | Name CEOs and execs — shows real knowledge |
| Diplomatic tone | AI avoids taking sides or naming names | Good news/bad news, name the winners and losers |
| Generic CTA | "Would you be open to a call?" | Offer a one-pager they can read in 60 seconds |
| No reason for the email | Reader wonders "why not do it yourself?" | Vulnerability close: say honestly why you need them |
| Statement opening | "Sony published a paper..." is declarative | Ask a question: "Did you see Sony's paper?" — tests knowledge |
| Flat pitch | One reason to care | Stack layers: validation → urgency → insider knowledge → prize |
