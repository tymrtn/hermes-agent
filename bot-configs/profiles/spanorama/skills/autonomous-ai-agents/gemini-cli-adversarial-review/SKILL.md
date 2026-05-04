---
name: gemini-cli-adversarial-review
description: Use Google Gemini CLI for blunt/adversarial product or plan critique, especially when you want an external sanity check before turning an idea into an execution plan.
version: 1.0.0
author: Spanorama
license: MIT
---

# Gemini CLI adversarial review

Use this when you want a candid outside critique of a product concept, launch plan, pricing idea, or strategy doc.

## When to use
- Product concept pressure-test
- Pricing / packaging sanity check
- Pre-launch risk review
- "Pitch this to Gemini and ask for honest/adversarial feedback"

## Key findings from real use
1. **Gemini CLI may exist only through `npx`**
   - Check with:
   ```bash
   npx -y @google/gemini-cli --help
   ```

2. **Auth may depend on the correct HOME**
   - On Tyler's machine, Gemini OAuth settings lived in:
   ```
   /Users/wondermonkey/.gemini/settings.json
   ```
   - Running from the Hermes profile home failed auth.
   - Working pattern:
   ```bash
   HOME=/Users/wondermonkey npx -y @google/gemini-cli ...
   ```

3. **Gemini workspace restrictions matter**
   - Gemini could not read `/tmp/...` if that path was outside the allowed workspace.
   - Fix: write the brief/prompt source file **inside the repo workspace** first, then ask Gemini to read that file.
   - Example:
   ```bash
   /Users/wondermonkey/Dropbox/Code/SECOM/spainexpat.com/docs-spainexpat-pass-brief.md
   ```

4. **Do not assume the agent will gracefully recover from bad file scope**
   - If you point Gemini at an out-of-workspace file, it may thrash around searching unrelated files.
   - In practice it can even return a confident critique of the wrong document after failing to read the requested file.
   - Treat any such run as invalid; discard it and rerun only after placing the input doc in-workspace.
   - Put the input doc in-workspace before invoking Gemini.

## Recommended workflow

### 1) Prepare a blunt brief inside the project workspace
Include:
- concept summary
- audience
- proposed components
- constraints
- explicit questions for critique

Example prompt tail:
```text
What I want from you:
1. Give near-adversarial feedback on this concept.
2. Identify the biggest risks, bad assumptions, and likely failure modes.
3. Tell me what should be included vs discounted vs upsold.
4. Tell me what the MVP should be for a 2-week push.
5. Tell me what NOT to do.
6. Propose a pragmatic 2-week launch-prep sequence.
7. Keep it blunt, structured, and operator-friendly.
```

### 2) Run Gemini headless
From repo root:
```bash
HOME=/Users/wondermonkey \
  npx -y @google/gemini-cli \
  -p "Read path/to/brief.md and give the requested feedback. Be candid and somewhat adversarial." \
  --output-format text
```

### 3) Save the critique back into the repo
Create a companion notes file such as:
```text
path/to/gemini-feedback.md
```
Extract the key objections and operational constraints.

### 4) Turn critique into an operator plan
Create a plan doc that includes:
- decision / non-decision
- what is core vs support vs upsell
- launch gates / hard rules
- phased execution over 1-2 weeks
- risks and mitigations
- task breakdown suitable for Todoist

### 5) Push the plan into Todoist as SSOT
Recommended pattern:
- create sections first
- add tasks by phase
- use `Waiting on Tyler` and `Waiting on Partners`
- add a project comment pointing to the source plan doc

### 6) Add cron reviews
Useful recurring jobs:
- daily sprint review
- every-2-days partner follow-up review
- twice-weekly trust / launch readiness review

## Good use cases
- Product memberships
- Launch offers
- Marketplace packaging
- Premium service bundling
- Pre-mortem / red-team critique before execution

## Pitfalls
- Do not rely on public-affiliate math alone; Gemini will call out fake value claims.
- Do not feed Gemini files outside its workspace.
- Do not assume local Claude CLI is available/authenticated; verify separately.
- Do not turn raw Gemini output directly into tasks without synthesis.

## Verification checklist
- Gemini CLI help works
- Auth works with the correct HOME
- Brief file is inside workspace
- Critique saved to repo
- Plan doc saved to repo
- Todoist updated as SSOT
- Cron reviews scheduled if the project is ongoing
