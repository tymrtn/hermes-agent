---
name: fellowship-application-packet
description: Build submission-ready fellowship/accelerator/research application packets for Tyler by extracting all form questions, parallelizing requirements/positioning/artifact work, staging drafts and uploads in Dropbox, and surfacing only Tyler-only gates.
tags: [fellowship, application, research, accelerator, writing-samples, todoist, tyler]
triggers: ["fellowship application", "apply to Astra", "application is due", "researcher application", "accelerator application", "grant application", "program application"]
---

# Fellowship / Research Program Application Packet

## When to use
Use when Tyler needs to apply to a fellowship, research program, accelerator, grant, or selective program — especially when the deadline is close and the application has multiple free-text fields, uploads, references, or logistics gates.

This is a **doing** workflow, not a planning workflow. Build the packet immediately, then surface only true Tyler-only blockers.

## Core pattern

1. **Extract the exact application surface**
   - Open the public program page with `web_extract`.
   - Open the actual form with `browser_navigate` when linked forms are dynamic.
   - Use `document.body.innerText` and `Array.from(document.querySelectorAll('a'))` via `browser_console` to capture all visible questions and linked prep docs.
   - If the form links a Google Doc with full question text, extract/read that too.
   - Compare live form vs prep doc: conditional questions often differ.

2. **Create the dependency map**
   Classify each item as:
   - Skippy can draft/assemble;
   - Tyler must decide/provide;
   - external portal/upload/submission gate;
   - legal/personal accuracy gate;
   - reference/permission gate.

3. **Parallelize the hard parts**
   Use `delegate_task` in parallel for:
   - requirements/dependencies/checklist;
   - positioning / strongest narrative frame;
   - artifact hunt / writing sample candidates;
   - target-program research if mentor/org fit matters.

4. **Build the packet in Dropbox**
   Create a dedicated folder:
   - `~/Dropbox/Applications/<Program>-<Year>/`

   Stage:
   - working draft in form order: `.md`, `.docx`, optionally `.html`;
   - cleaned writing sample(s): `.md`, `.docx`, optionally `.pdf` if rendering works;
   - resume/CV copy;
   - any checklist or answer matrix.

5. **Use Tyler-shaped positioning**
   For AI safety / governance / research programs, Tyler’s reusable frame is:

   > Tyler builds and studies the operational trust layer AI agents need before institutions can safely delegate to them.

   Default project hierarchy for AI governance/safety applications:
   - Lead with **Governor** as operational trust / contextual action adjudication / executable AI governance.
   - Use **Tribunal** as empirical model-behavior / LLM-as-judge reliability support.
   - Use **AIPREF / Aposema / Copyright.sh / Clef** as concrete governance testbeds for preference signaling, provenance, creator rights, and standards.
   - Avoid generic “I care about AI safety” language. Use concrete mechanisms: authority, consent, provenance, contextual justification, preference signaling, adjudication, auditability, recourse.

6. **Render robustly**
   - Try PDF via `pandoc` only if a PDF engine is available.
   - On Tyler’s Mac, `pandoc` may fail if `pdflatex` is missing. Do not waste time installing TeX unless PDF is essential.
   - Prefer DOCX fallback:
     ```bash
     pandoc draft.md -o draft.docx
     pandoc draft.md -s -o draft.html
     ```
   - Existing resume PDFs often live under `~/Dropbox/Skippy/resumes/`.

7. **Update Todoist when there is a real artifact**
   Use raw comments if needed:
   ```bash
   HOME=/Users/wondermonkey todo raw POST /comments --body '{"task_id":"<id>","content":"Packet staged in ... Remaining gates: ..."}'
   ```
   Note: on this machine the Todoist CLI needs `HOME=/Users/wondermonkey`.

8. **Final handoff**
   Return:
   - clickable Todoist links if requested, not raw URLs;
   - staged file paths and `MEDIA:` attachments for key files when on Telegram;
   - the exact Tyler-only blockers, grouped tightly;
   - recommended workstream/frame/strategy.

## Astra 2026 specifics learned

Astra application public/program page:
- Deadline: May 3, 2026, 11:59pm Anywhere on Earth.
- Program dates: September 14, 2026 – February 5, 2027.
- Commitment: full-time, 40 hrs/week.
- Workstreams: Empirical Research and Strategy & Governance.
- Resume upload required.
- Writing samples encouraged; highly encouraged for Strategy & Governance.
- Two references required; they may be contacted within a week if Tyler advances.
- Reference strategy for Tyler when a second conventional reference is hard: prefer a directly relevant collaborator/co-author or former business partner who can speak to judgment, independent execution, product/technical sense, persistence, and ambiguous early-stage work. Avoid family references if possible. Tom is an acceptable stale fallback only if no better option; BMI colleagues are weak for research-oriented applications because they mainly evidence employment/reliability/software work, not Tyler's independent research agenda. For Astra 2026, Nick Vincent became the strongest Reference 2 because he is co-author on the CMI Trap SSRN paper and can speak to research judgment, technical/legal reasoning, and unusual AI governance mechanisms. Relationship blurb pattern: `<Name> is my collaborator/co-author on <paper/project>. They can speak to my research judgment, technical/legal reasoning, independent execution, and ability to develop unusual governance mechanisms for AI systems.`
- Logistics gates: US work authorization, country of residence, full-time availability, preferred location, timeline constraints.
- Submission cannot be edited after final submit.

For Astra-like AI safety applications:
- Recommend **Strategy & Governance primary** for Tyler.
- Select **Empirical Research secondary** when allowed, using Tribunal.
- Primary thesis, corrected by Tyler during Astra 2026: **AI systems are crossing the human value layer; Tyler's work is about making them accountable to the systems that define ownership, authority, evidence, and responsibility** — especially property, copyright/IP, licensing, provenance, institutional authority, auditability, and recourse.
- Lead public-safe artifact: **CMI Trap / Compound Statutory Liability Entrapment in Inference-Time AI Pipelines** (SSRN). This is stronger than generic Governor framing because it makes AI retrieval governance concrete through copyright-management information, §1201/§1202, parser behavior, provenance, and enforceable accountability.
- Use **Tribunal** as the sharp empirical/accountability second blade: contested-judgment reliability, narrative/accountability pressure, LLM-as-judge in no-ground-truth domains. Include/attach only when Tyler accepts pseudonym or identity-linkage risk.
- Position Governor as supporting agent-action governance infrastructure, not the sole centerpiece, unless the application specifically asks for deployed agent controls.
- Use executable AI governance / contextual trust control for delegated agents as the operational mechanism under that thesis.
- When Tyler says the application does not reflect his approach, look for the economic/legal accountability spine before polishing prose. His AI safety angle is often: AI systems crossing human value/ownership/authority systems without accountability.

## Known Tyler artifacts from Astra run

- Resume source/copy used: `~/Dropbox/Skippy/resumes/tyler_martin_resume.pdf`
- Governor position paper source: `~/Dropbox/Code/u1ff9e/u1ff93.com/docs/governor-position-paper-v0.md`
  - **Important:** Governor is the strongest application narrative, but Tyler corrected that shareable Governor writing is mostly patent-adjacent/non-public. Do **not** upload Governor-derived writing samples unless Tyler explicitly approves. Use Governor as project description / research agenda narrative instead.
- Sovereign Stack sources:
  - `~/Dropbox/Code/sovereign-stack/README.md`
  - `~/Dropbox/Code/sovereign-stack/MANIFESTO.md`
  - `~/Dropbox/Code/sovereign-stack/PROJECTS.md`
  - Strong candidate for Astra-like writing sample if cleaned as an overview: personal data sovereignty, agent-native infrastructure, user-controlled terms, AI preference/licensing rails. Avoid uploading raw manifesto tone alone if it sounds too launch-page/flamethrower.
- Moment of Creation / Music 2031 sources:
  - `~/Dropbox/Projects/MoCreation/Music 2031.md`
  - `~/Dropbox/Projects/MoCreation/part3_now_what.md`
  - Strong safe Strategy & Governance writing samples: AI licensing, rights infrastructure, provenance, coordination failures, creator access.
- CMI Trap / Compound Statutory Liability Entrapment paper:
  - Canonical source found at `~/Dropbox/Projects/Aposema/papers/Compound Statutory Liability Entrapment in Inference-Time AI Pipelines v5-gemini.md`.
  - Public SSRN: `https://ssrn.com/abstract=6432898`; DOI `http://dx.doi.org/10.2139/ssrn.6432898`.
  - Authors: Tyler Martin; Nicholas Vincent. Useful because it is directly Astra-relevant: inference-time AI retrieval governance, DMCA §1201 anti-circumvention, §1202 CMI removal, structural CMI entanglement, forensic provenance, publisher-rights enforcement.
  - For Astra-like uploads, CMI Trap may be stronger and safer than Tribunal because it shows technical/legal governance mechanism without foregrounding Tribunal's institutional-threat/pseudonymity risks.
  - Export command used successfully: `/opt/homebrew/bin/pandoc '<source.md>' -o '~/Dropbox/Applications/Astra-2026/writing-samples/CMI-Trap-CSLE-v5.docx'`.
- pB11 technical sample:
  - `~/Dropbox/Projects/u1f99e/docs/pb11-directed-exhaust/pB11_Directed_Exhaust_v9_Martin.pdf`
  - Use only as optional technical depth; impressive but off-thesis for AI safety/governance.
- Tribunal paper source: `~/Dropbox/Projects/tribunal/repo/papers/20260502-tribunal-position-paper-v0.6.md`
  - Good empirical AI sample if Tyler is comfortable sharing and the paper is clean enough not to distract reviewers.
- AIPREF strategy brief: `~/Dropbox/Projects/Aposema/memos/AIPREF_Strategy_Research.md`

Astra packet was staged at:
- `~/Dropbox/Applications/Astra-2026/astra-application-working-draft.md`
- `~/Dropbox/Applications/Astra-2026/astra-application-working-draft.docx`
- `~/Dropbox/Applications/Astra-2026/tyler_martin_resume.pdf`
- `~/Dropbox/Applications/Astra-2026/writing-samples/sovereign-stack/Sovereign-Stack-Overview.docx`
- `~/Dropbox/Applications/Astra-2026/writing-samples/Music-2031.docx`
- `~/Dropbox/Applications/Astra-2026/writing-samples/Moment-of-Creation-Part-3.docx`
- `~/Dropbox/Applications/Astra-2026/writing-samples/pB11_Directed_Exhaust_v9_Martin.pdf`
- Non-upload Governor-derived sample moved under `~/Dropbox/Applications/Astra-2026/do-not-upload/`

## Deadline-close finalization pattern

When the deadline is live and the application is mostly drafted, switch from writing mode to submission mode:

1. Re-open/read the staged working draft and final handoff.
2. Verify the live application URL from the official page. If a shortlink is blocked by browser/tooling, resolve it with a minimal terminal/urllib probe and surface approval if the governor flags the shortener.
3. If the form/browser tool hangs on Airtable or similar, open the form in Tyler's actual Mac browser with `open '<url>'` instead of spending the deadline fighting automation.
4. Create a single final paste sheet in exact form order, including file paths for uploads and recommended yes/no choices.
5. Open the paste sheet locally for Tyler with `open -a TextEdit <file>`.
6. If Tyler provides a “final pasted application” file for review, audit the actual pasted text before giving a readiness verdict. On macOS this may be a Finder alias to an `.rtfd` bundle, not plain text. Resolve aliases and extract RTF text instead of assuming the path is unreadable:
   ```bash
   osascript -e 'tell application "Finder" to get POSIX path of (original item of (POSIX file "/path/to/alias" as alias) as alias)'
   /usr/bin/textutil -convert txt -stdout '/path/to/target.rtfd/TXT.rtf'
   ```
   Then check for missing required fields, stale TODOs, wrong upload/link order, reference fields, logistics selections, and any sensitive-sharing choices.
7. Reduce the final blocker list to only truly user-only fields: references, identity/legal facts, logistics truth, sensitive sharing/external partner consent, final submit.

For Astra 2026 specifically:
- Final paste sheet path used: `~/Dropbox/Applications/Astra-2026/20260503-astra-final-paste-sheet.md`.
- Live form resolved from `https://bit.ly/astra-fellowship` to Airtable: `https://airtable.com/appE5l9KZFoe1Lggq/pagTeupHFeidmZnyg/form`.
- Do not wait for Tribunal v0.7; no v0.7 existed. Export v0.6 if needed: `~/Dropbox/Applications/Astra-2026/writing-samples/20260502-tribunal-position-paper-v0.6.docx`.
- Recommended upload order at close: Data Sovereignty Human Astra version, Music 2031, Tribunal v0.6 only if Empirical Research is selected.

## Pitfalls

- Do not stop at a timeblocked plan. Tyler explicitly expects execution: extract questions, draft answers, stage files, update task state.
- Do not trust public page alone; forms and linked prep docs may differ.
- Near deadline, do not waste time making automation fill an Airtable form if the browser tool is hanging. Open the live form for Tyler and provide exact paste-ready content.
- Do not hide required reference/logistics gates in prose. Surface them as the blocker list.
- Do not paste raw URLs when Tyler asks for links; use markdown clickable links.
- Do not over-AI-polish application prose. Some programs explicitly warn that AI assistance makes applications generic. Preserve Tyler’s concrete, weird, builder-specific edge.
- Be honest in AI-assistance disclosure: extraction, organization, drafting from Tyler’s own work, Tyler review/editing.
- If PDF generation fails because `pdflatex` is missing, switch to DOCX/HTML. Magnificent agents adapt; lesser machines install a TeX distribution under deadline pressure and die of shame.
