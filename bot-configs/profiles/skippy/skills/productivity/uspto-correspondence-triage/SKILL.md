---
name: uspto-correspondence-triage
description: Triage USPTO patent correspondence — decode filenames, identify urgent actions (abandonment, missing parts, deadlines), sort into patent folders, look up fees, generate submission-ready PDFs (micro entity certs, petitions), and build action plans with filing instructions. Trigger on any USPTO mail dump, patent folder check, correspondence sorting, or micro entity certification task.
version: 1.1.0
author: Skippy
metadata:
  hermes:
    tags: [USPTO, patents, IP, legal, deadlines, fees]
    related_skills: [ocr-and-documents]
---

# USPTO Correspondence Triage

Use when Tyler asks to check USPTO folders, sort patent mail, or when encountering USPTO PDF files.

## Step 1: Decode USPTO Filenames

USPTO correspondence follows this naming convention:
```
{customer#}_{app#}_{date}_{DOCTYPE}.PDF
```

Example: `207547_63848871_04-14-2026_ABN.PDF`
- Customer: 207547 (Tyler)
- Application: 63/848,871 (63-series = provisional)
- Date: April 14, 2026
- Type: ABN (abandonment)

### Document Type Priority

| Code | Meaning | Urgency |
|------|---------|---------|
| ABN | Notice of Abandonment | CRITICAL — petition to revive needed |
| NTC.MISS.PRT | Notice to File Missing Parts | URGENT — 2-month deadline |
| OA / N417 | Office Action | URGENT — response deadline |
| WELCOME.LET | Welcome Letter | Informational |
| APP.FILE.REC | Application Filing Receipt | Informational — verify accuracy |
| MES.GIB | Miscellaneous | Check content |
| NT.INC.REPLY | Notice of Incomplete Reply | URGENT |
| OA.EMAIL | Office Action Email | URGENT |

## Step 2: Read Scanned PDFs

USPTO correspondence is almost always scanned images (no extractable text). Use the pymupdf → vision pipeline:

```python
import fitz
doc = fitz.open("207547_63848871_04-14-2026_ABN.PDF")
for i, page in enumerate(doc):
    pix = page.get_pixmap(dpi=200)  # 200 DPI sufficient for USPTO letters
    pix.save(f"/tmp/page_{i+1}.png")
    # Then: vision_analyze(image_url=f"/tmp/page_{i+1}.png",
    #   question="Read ALL text. What type of notice? App number? Deadlines? Required actions? Fees?")
```

Always try `page.get_text()` first — if empty, fall back to vision. Batch-convert all pages before calling vision to parallelize.

## Step 3: Sort Into Patent Folders

Tyler's patent portfolio is at `~/Dropbox/USPTO/` with folders like:
```
Patent 1 - Cross-Media Provenance.../correspondence/
Patent 2 – AI Web Usage Metering/correspondence/
...
```

Match application numbers to patents:
- 63/808,528 → Patent 1 (Cross-Media Provenance)
- 63/821,509 → Patent 2 (AI Usage Metering)
- 63/848,871 → Patent 3 (Energy-Seconds / CLEF-002-US)
- 63/984,507 → Patent 4 (Multi-Layer Web Content Protection — CROWN JEWEL)
- 63/984,424 → Patent 5 (Sovereign Domain Agent)
- 64/009,082 → Patent 8 (Blind Attribution-Based Risk Router / Governor)
- 64/011,236 → Patent 9 (EEAA Extended / Epicombinant)

**Always COPY files first, leave originals until Tyler confirms.** Create `correspondence/` subdirectories if missing.

## Step 4: Look Up Fees

Current fee schedule: https://www.uspto.gov/learning-and-resources/fees-and-payment/uspto-fee-schedule

Use browser tool to scrape the fee table. Key micro-entity fees:
- Provisional basic filing: $65
- Surcharge 1.16(g) (late provisional fee): $13
- Petition to revive ≤2yr: $452
- Petition to revive >2yr: $600

Tyler qualifies as micro entity (75% discount). Without it, fees are 4x.

## Step 5: Build Action Plan

For each application with outstanding actions:
1. What's needed (forms, fees, statements)
2. Exact deadline (date mailed + response period)
3. Fees at micro entity rate
4. Filing order (cheapest/simplest first, then complex)

Key forms:
- PTO/SB/15A — Micro Entity Certification
- PTO/SB/64 — Petition for Revival (unintentional delay)

All filings via Patent Center: https://patentcenter.uspto.gov

## Step 6: Generate Submission-Ready PDFs

When certs or petitions are needed, generate actual PDFs using reportlab — not just markdown plans. Tyler needs files he can sign and upload to Patent Center.

```python
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas
from reportlab.lib.units import inch
```

For PTO/SB/15A certs: replicate the form layout (Doc Code: MES.GIB header, 4 certification clauses, signature block). The blank form at `_general/sb0015a.pdf` has no fillable fields — it's a flat PDF. Generate from scratch with reportlab.

For petitions: render the markdown petition text as a formatted PDF with proper headings and word wrap.

Save all generated files to `~/Dropbox/USPTO/_general/micro_entity_certs/` and include a `FILING_INSTRUCTIONS.md` with step-by-step for Patent Center submission.

## Step 7: NTC.MISS.PRT Response Strategy

When a patent gets a Notice to File Missing Parts for missing micro entity certification:

1. **The $65 provisional fee IS the correct micro entity fee** — entity status is a matter of fact at filing, not dependent on when the cert is submitted
2. **File PTO/SB/15A referencing the specific application number**
3. **For safety, pay the $13 surcharge (37 CFR 1.16(g))** — even though arguably no surcharge is due, $13 is cheap insurance vs another abandonment
4. **If already abandoned**: file Petition under 37 CFR 1.181 (Office error — no fee) before falling back to 37 CFR 1.137 revival ($263-465)
5. **Preemptive filing**: if a patent has no NTC.MISS.PRT yet but no cert on file, file the cert NOW before the notice arrives

### The Patent 3 Lesson (April 2026)
Patent 3 (63/848,871) was abandoned because:
- $65 paid at filing (correct micro entity fee)
- NTC.MISS.PRT issued because no entity status cert was on file
- Applicant filed PTO/SB/15A but did NOT pay $13 surcharge
- Office held the response insufficient and abandoned the application
- 37 CFR 1.181 petition argues no surcharge was owed (cert confirms pre-existing status)

**Key takeaway: ALWAYS scan the entire portfolio for missing certs when any NTC.MISS.PRT arrives. The same gap likely exists on other recently-filed apps.**

## Step 8: Extract App Numbers from N417 Receipts

When app numbers are unknown, extract them from N417.pdf or N417.PYMT.pdf receipts:
```python
from pypdf import PdfReader
r = PdfReader('Patent X/N417.PYMT.pdf')
text = r.pages[0].extract_text()
# Look for "APPLICATION #" line — format: 63/xxx,xxx or 64/xxx,xxx
```

N417 receipts (unlike NTC.MISS.PRT) contain extractable text. The NTC/ABN/APP.FILE.REC PDFs are typically scanned images requiring vision analysis.

## Pitfalls

1. **Runtime path mismatch is common** — current Hermes may run as `/Users/wondermonkey` while older sessions and Dropbox paths used `/Users/tylermartin`. If `~/Dropbox/USPTO` is missing, do not conclude the packet does not exist. First check `/Users/wondermonkey/Dropbox`, `/Users/tylermartin/Dropbox` if mounted, and use `session_search` / targeted session-file search for app numbers and generated artifact paths. If the USPTO tree is genuinely not synced on the current runtime, record that as the blocker and ask Tyler to make the folder available rather than recreating legal packets from memory.
2. **Provisionals expire in 12 months** — if reviving an abandoned provisional, check whether it's still within the 12-month window for filing a non-provisional. Even if abandoned, priority can be claimed via 35 USC 119(e) if a non-provisional is filed within 12 months of the provisional filing date (MPEP 211.01(b) — provisional need not be pending)
2. **Micro entity must be established per-application** — cert on one app doesn't cover others
3. **NTC.MISS.PRT deadline is 2 months from mailing** — mark it immediately
4. **The $65 fee IS correct** — if micro entity cert is filed, no fee deficiency exists. But pay the $13 surcharge anyway to avoid a fight
5. **Check for swapped files** — USPTO sometimes delivers docs that land in wrong folders
6. **Always push files to Tyler via MEDIA:** — never just cite local paths on Telegram
7. **Section 112 enablement** — when claiming priority from an abandoned provisional, the provisional spec must adequately support the non-provisional claims. This is the real failure mode, not the procedural stuff
8. **APP.FILE.REC and NTC.MISS.PRT are scanned images** — pypdf extract_text() returns empty. Use vision or OCR
