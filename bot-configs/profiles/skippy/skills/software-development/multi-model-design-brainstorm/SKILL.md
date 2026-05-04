---
name: multi-model-design-brainstorm
description: Run a structured multi-round brainstorm with two or more frontier LLMs in isolated sessions, keep all prompts and responses on disk for audit, and distill convergence/disagreement into an actionable synthesis. Use for architectural decisions, product shape exploration, and any design-heavy question where one model's opinion isn't enough.
---

# Multi-Model Design Brainstorm

## When to use
- Architectural decisions with real trade-offs (storage model, data primitives, framework shape)
- Product direction questions where one model will overfit to its training priors
- Any "should we build X and if so how" question before writing code
- When the user explicitly wants to see model disagreement, not consensus

Do NOT use for:
- Simple factual questions
- Small code changes
- Tasks where the answer is obvious and you're just stalling

## Core principle
Two frontier models asked the same question in different sessions will converge on some ideas and diverge on others. The divergence is signal. Preserve everything; synthesize after.

## Directory structure
```
<project>/
├── README.md                    # framing, current thesis, constraints
└── docs/
    ├── plans/
    │   ├── YYYY-MM-DD-context-for-<models>.md    # shared context doc
    │   └── YYYY-MM-DD-<project>-mvp.md           # planning threads
    └── brainstorms/
        ├── YYYY-MM-DD-brainstorm-audit-summary.md  # index
        ├── <model-a>/
        │   ├── round1-prompt.md
        │   ├── round1-response.md
        │   ├── round1-raw.txt
        │   ├── round2-prompt.md
        │   ├── round2-response.md
        │   ├── round2-raw.txt
        │   ├── round3-prompt.md
        │   ├── round3-response.md
        │   └── round3-raw.txt
        └── <model-b>/
            └── (same structure)
```

## Process

### 1. Build the context doc
Single markdown file with:
- Objective (what we're deciding)
- External reference product if any (e.g. what Cloudflare shipped)
- Current system context (what already exists)
- Problems to solve (numbered list)
- Working thesis (your current best guess)
- Integration/constraint notes
- Explicit "ask for the model" section — tell them not to just restate the reference product

### 2. Planning threads doc
Split the problem into orthogonal threads (product, architecture, backend, MVP constraints). Gives each model scaffolding without dictating the answer.

### 3. Use isolated sessions per model
- Each model gets its own Hermes session — record the session ID in the audit summary
- If using Opus, set up a dedicated profile (e.g. `~/.hermes/profiles/opus-planning`) that routes through the local billing proxy. This avoids cross-contaminating your main session and keeps billing separate
- Use explicit provider/model selection so the audit is reproducible

### 4. Three rounds
- **Round 1**: open exploration. Ask for breadth — multiple architectural shapes, product wedges, non-obvious angles
- **Round 2**: narrow. Pick 2–3 directions from round 1 responses and ask the model to pressure-test them
- **Round 3**: converge. Ask for a single recommendation with MVP boundary, risks, open questions, and explicit "what's out of scope"

### 5. Save everything
For each round, save three files:
- `roundN-prompt.md` — the prompt you sent
- `roundN-response.md` — the formatted response
- `roundN-raw.txt` — raw terminal/session output (includes spinner noise, session banners, provider hints — useful for auditing what the model actually received)

### 6. Write the audit summary
Markdown file listing:
- Project name and context doc path
- Per-model: session ID, and paths to every prompt/response/raw file
- Notes on how each session was configured (profile, model, routing)

### 7. Synthesize
After all rounds done, read each model's round-3 output and extract:
- Where they converged (= high-confidence design choices)
- Where they diverged (= real trade-offs you have to pick)
- What each model got RIGHT that the other missed
- What NEITHER model addressed (= your job to add)

## Pitfalls

- **Don't let the models see each other's output.** Cross-contamination defeats the purpose. Separate sessions, separate profiles if possible.
- **Resist the urge to summarize early.** Raw files exist precisely so you can re-read the actual reasoning later, not just your distillation.
- **Opus will hedge toward "ship something small." Gemini will hedge toward "event log everything."** Know their priors going in.
- **Models forget prior rounds unless you resume the session.** Use session resume (`↻ Resumed session <id>`) explicitly.
- **Don't ask "which is better?"** Both are valuable. Synthesize, don't pick.
- **Neither model will think about your specific strategic goals unless you tell them.** Governor integration, MCP exposure, federation angles — add these yourself in the synthesis step.

## Deliverable
A synthesis document that:
1. States the recommended architecture (one sentence)
2. Lists the strong ideas worth stealing from each model
3. Calls out what both models missed
4. Proposes the v0.1 spec / next concrete step

## Example
See `~/Dropbox/Code/hermes-artifacts/` for a complete run done by Nagatha (2026-04-16): Cloudflare Artifacts evaluation → Hermes-native artifacts design. Opus + Gemini, 3 rounds each, all prompts/responses preserved, audit summary in `docs/brainstorms/2026-04-16-brainstorm-audit-summary.md`.
