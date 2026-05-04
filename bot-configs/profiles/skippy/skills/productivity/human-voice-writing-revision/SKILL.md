---
name: human-voice-writing-revision
description: Revise Tyler-authored or Tyler-facing essays/application materials that sound like AI slop into a human, specific, thinking-in-public voice. Use when Tyler criticizes writing as AI-ish, sterile, committee-safe, too polished, or asks to match Moment of Creation style.
version: 1.0.0
author: Skippy
license: MIT
metadata:
  hermes:
    tags: [writing, editing, voice, applications, anti-ai-slop]
---

# Human Voice Writing Revision

## Trigger

Use this skill when:
- Tyler says writing sounds like "AI slop," "committee-safe," "generic," "too polished," or similar.
- Rewriting essays, application answers, manifesto-style pieces, Sovereign Stack/Data Sovereignty/Aposema/AIPREF materials, or Moment of Creation-adjacent writing.
- Selecting or reshaping writing samples for fellowships/applications where authenticity and project taste matter.

## Goal

Preserve Tyler's actual ideas and edge while removing the tells of LLM-generated prose: thesis-first structure, glossy abstraction, balanced triads, mechanical transitions, and standalone quotable punchlines.

The target voice is: smart person explaining a messy thing to another smart person over drinks. Specific, human, technically literate, and willing to show uncertainty mid-argument.

## Default Workflow

1. **Find the real source voice before rewriting.**
   - If working on Moment of Creation-adjacent writing, read `/Users/wondermonkey/Dropbox/Projects/MoCreation/moment_of_creation_style_guide_1.md`.
   - Read 1-2 nearby source pieces, especially `/Users/wondermonkey/Dropbox/Projects/MoCreation/part3_now_what.md` or the actual source draft Tyler supplied.
   - Do not infer style from memory when files are available.

2. **Diagnose what failed.**
   Look for:
   - Starts with a thesis instead of a scene, quote, concrete observation, or felt problem.
   - Abstract nouns stacked together: governance, infrastructure, sovereignty, provenance, trust, alignment, ecosystem, etc.
   - Parallel triads or symmetrical lists.
   - Mechanical transitions: "to be clear," "that said," "it is worth noting," "in conclusion."
   - Sentences that sound tweetable or like a slide title.
   - Generic moralizing instead of grounded examples.

3. **Rewrite from the inside out.**
   - Start with a concrete observation, moment, or tension.
   - Use first person where appropriate: "I think," "I keep coming back to," "I am not sure," "my bias is..."
   - Let uncertainty appear in the middle, not as a disclaimer at the end.
   - Use specific projects, companies, laws, standards, examples, and failure modes.
   - Keep sections uneven if the idea wants that.
   - Prefer explanation over slogans.

4. **Add the sharp bridge when available.**
   For Sovereign Stack / Aposema / AIPREF / creator-rights / AI scraping pieces, consider whether the **CMI trap** belongs:
   - CMI = copyright management information.
   - DMCA §1202 can make stripping or altering rights/authorship metadata legally relevant under the right facts.
   - AI scraping turns metadata loss from clerical mess into evidentiary infrastructure failure.
   - Provenance without machine-readable permissions is incomplete; permissions without provenance float away from the work.
   - This frames Aposema/AIPREF as authority/provenance infrastructure, not merely preference signaling.
   - Avoid overclaiming legal certainty. Phrase as a legal/evidentiary fault line, not guaranteed liability.

5. **Run anti-slop checks before delivering.**
   Search the final draft for:
   - em dashes (`—`) if the Moment guide applies; remove them.
   - "to be clear," "it is worth noting," "in conclusion," "furthermore," "moreover."
   - three consecutive sentences with the same shape.
   - too-perfect punchlines.
   - section headers that sound like clever slogans.

6. **Deliver both artifact and judgment.**
   - Say plainly if the previous draft was wrong and why.
   - Provide the revised file path/media.
   - Identify any remaining user-only fingerprinting needed.

## Pitfalls

- Do not sanitize the original into safer institutional language unless explicitly requested.
- Do not remove anger, stakes, or weirdness just because the audience is formal.
- Do not turn a manifesto into a grant application by default.
- Do not overstate legal claims around CMI/DMCA §1202; use researched citations or cautious phrasing for legal/financial claims.
- Do not use Governor patent-adjacent material as a public writing sample unless Tyler explicitly says it is public and uploadable.

## Verification Snippets

For a Markdown draft:

```bash
python3 - <<'PY'
from pathlib import Path
p=Path('DRAFT.md')
s=p.read_text()
checks={
  'em_dash':'—' in s,
  'to_be_clear':'to be clear' in s.lower(),
  'worth_noting':'worth noting' in s.lower(),
  'in_conclusion':'in conclusion' in s.lower(),
  'furthermore':'furthermore' in s.lower(),
  'moreover':'moreover' in s.lower(),
}
print(checks)
PY
```

For DOCX generation when LaTeX is unavailable, use Pandoc DOCX/HTML instead of PDF:

```bash
/opt/homebrew/bin/pandoc draft.md -o draft.docx
/opt/homebrew/bin/pandoc draft.md -o draft.html
```

## Example Outcome From Astra Work

The useful course correction was:
1. User flagged a Data Sovereignty/Sovereign Stack rewrite as AI slop.
2. Read the Moment of Creation style guide and source writing.
3. Rewrote the piece in first person, beginning from a concrete observation rather than a thesis.
4. Added a "CMI trap" section connecting DMCA §1202, provenance, machine-readable permissions, AIPREF/Aposema, and AI scraping.
5. Regenerated DOCX and logged the change to the relevant task.
