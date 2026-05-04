---
name: bulk-vendor-inquiries-spain
description: Send transactional inquiry emails to 10+ Spanish local businesses (autoescuelas, asesorías, gestorías, clínicas, etc.) in one pass. Discover contacts, batch-send via Envelope, fall back to web forms for captcha/form-only sites. NOT cold outreach — these are buyer-side inquiries where the recipient WANTS the lead.
tags: [email, outreach, spain, bulk, envelope, vendor-discovery, transactional]
triggers: ["research and contact all", "send inquiries to", "ask for quotes from", "contact every", "autoescuela", "gestoría", "asesoría", "clínica", "Madrid driving school", "vendor outreach Spain"]
---

# Bulk Vendor Inquiries (Spain)

## When to Load
Tyler asks Skippy to research a category of Spanish local businesses (driving schools, lawyers, gestorías, clinics, contractors, etc.) and contact them all for prices/availability. Different from `cold-email-anti-ai` — this is BUYER-side, transactional. Recipients want leads.

## Core Insight
Spanish small businesses split into three discoverability tiers:
1. **Email-listed** (~50%) — `info@`, `contacto@`, `informacion@` on /contacto, /aviso-legal, or footer.
2. **Form-only** (~25%) — WPForms/Contact Form 7, often with reCAPTCHA.
3. **Phone-only or broken** (~25%) — old WordPress, dead domains, NXDOMAIN, or Cloudflare bot challenge.

Plan for all three from the start. Don't waste budget pushing past captchas.

## Workflow

### 1. Build the universe (target 20+ to land 15)
- Use `web_search` for `"autoescuela" Madrid centro listado`, `lomejordelbarrio.com`, `madrid.plus/ciudad/madrid/<category>`, Yelp-Madrid pages.
- Pull names/addresses/phones from these aggregators — they often surface businesses with NO web presence.
- Reddit r/askspain, r/Madrid, r/GoingToSpain for expat-recommended chains.

### 2. Scrape emails fast via curl (web_extract token may die mid-task)
Don't loop with `web_extract` for 20 sites — it's slow and can fail auth. Use a single Python script in `execute_code` that curls each site:

```python
import subprocess, re
schools = {"Name": ["https://site/contacto/", "https://site/", "https://site/aviso-legal/"], ...}
email_re = re.compile(rb'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,6}')
# curl -sLk -A "Mozilla/5.0" --max-time 12 <url>
# Try /contacto first, then /, then /aviso-legal/
# Filter out: wordpress., sentry., wixpress, @2x., noreply, no-reply, .png/.jpg/.css extensions
```

Aviso legal pages almost ALWAYS have a real email (legal requirement). If /contacto fails, try /aviso-legal/ and /politica-de-privacidad/.

### 3. Confirm hits via web_search for the holdouts
For schools where curl returned nothing, search `"<school name>" email contacto Madrid` — directory sites (cylex, autoescuelastop, recuperalospuntos) leak real emails.

### 4. Batch-send via Envelope
Single Python loop calling `envelope send --account ty@tmrtn.com --to ... --subject ... --body ...`. ~4-5 sec/email. 16 sends = ~70 sec. Don't draft individually.

### 5. Browser-fill forms for the captcha/form-only ones
Delegate this to a subagent with `browser` toolset and ~100 iterations. Subagent must:
- Try /contacto, /contact, /contactanos, /formulario
- Look for required GDPR/cookies checkbox before submit
- Check `console` for "reCAPTCHA failed" — flag as CAPTCHA_BLOCKED and move on
- Some forms collect ONLY name+phone (no message field) — fill anyway, note follow-up needed by phone
- Confirm submission via "Gracias" / "mensaje enviado" text

### 6. Final report categories
- SUBMITTED (email or form confirmed)
- CAPTCHA_BLOCKED (recommend skipping or phoning)
- SITE_DOWN / NO_FORM_FOUND (recommend phoning)

## Inquiry Email Template (Spanish, transactional)

Keep it short, structured, list-numbered. Spanish vendors respond well to numbered questions.

```
Subject: Consulta <servicio> — <breve contexto>

Hola,

<una frase de contexto: quién soy, qué quiero>

¿Podríais enviarme por email:

1. Precio de <X>
2. Precio por <unidad>
3. Estimación de coste total
4. <pregunta específica del caso>
5. Próximas fechas de inicio y flexibilidad de horarios

Gracias,
<nombre>
```

DO NOT apply `cold-email-anti-ai` rules here — those are for sales outreach to people who don't expect contact. This is the opposite: lead-gen forms exist BECAUSE vendors want these inquiries. Polite, structured, transactional wins.

## Pitfalls
- **web_search/web_extract auth tokens can die mid-session.** If both error with "Unauthorized: Invalid token", browser still works but is slow. Curl is the reliable fallback.
- **Don't loop web_extract over 20 URLs** — it's slow and bills more. Curl in a single execute_code script is 10x faster.
- **Skip Yelp** — JS-required, content blocked from extract.
- **Cloudflare bot challenge** on small Spanish sites is more common than expected. Don't burn 30 min trying to bypass; phone instead.
- **reCAPTCHA v2/v3** kills automated form fills. Note and move on.
- **Aggregator sites lie about emails** — they sometimes show partial like `aut***ala..com`. Verify on the actual school site or via `web_search`.
- **Skill: cold-email-anti-ai is the WRONG playbook here.** Don't load it for buyer-side inquiries.

## Envelope CLI for batch send
```bash
envelope accounts list   # confirm sender exists
envelope send --account ty@tmrtn.com --to "vendor@example.com" --subject "Consulta X" --body "..."
```
Returns "Sent to ..." + Message-ID on success. Loop in Python; track results in a JSON list.

## When to Recommend Phone Follow-up
After the batch sends, surface the unreachable ones to Tyler with their phone numbers so he can decide whether to call. Don't auto-place phone calls.

## Reading Replies (Envelope attachment gotcha)
Replies often arrive with a Word doc or PDF price sheet attached. Use `envelope attachment list --account <acct> <UID>` to see names, then:

```bash
# CORRECT — takes FILENAME, not index
envelope attachment download --account ty@tmrtn.com 198726 "INFORMACION PERMISO B.docx" --output /tmp/precios.docx
```

Passing `0` as the filename fails with "attachment '0' not found". To extract docx text without Word:
```python
import zipfile, re
with zipfile.ZipFile(path) as z:
    xml = z.open('word/document.xml').read().decode('utf-8', errors='ignore')
text = re.sub(r'<[^>]+>', ' ', xml)
text = re.sub(r'\s+', ' ', text)
```

## Pricing Reality Check — Spanish SMB "Pack" Trap
Spanish vendors advertise a low pack price as a loss-leader. The pack almost never covers the full service. For autoescuelas specifically:
- Pack ~200-300€ usually includes matrícula + theory + 2-5 practical classes ONLY
- NOT included: tasas DGT (~94€), exam fee (~79€), extra practicals (~35€ each), retake fees
- Realistic totals are 2-3x the advertised pack

When Tyler asks "why do you say €680 when the pack says €306?" — he's right to push. Always break down:
1. **Best case** (pack + mandatory fees only) — the cheapest honest floor
2. **Realistic** (pack + typical extra classes + fees) — what most people actually pay
3. **Worst case** (one retake) — the risk number

Don't inflate estimates to be "safe." Give all three scenarios with what's in each.

## Experienced-Driver Signal
When the buyer is highly experienced (multi-country driving history), the honest answer is the BEST case, not the average. Ask about their driving background before quoting. A 15-year driver with UK/EU experience legitimately needs 2-5 practicals, not 20. The teórico (theory exam) is usually the harder hurdle for them — 30 Spanish questions, 3 errors max — flag this instead of hand-wringing about practicals.

## Follow-up Reply (after first quote arrives)

When a vendor replies with pricing, Tyler's second message narrows the ask. Template:

```
Hola,

Gracias por la información y la rapidez.

[If something is disqualifying for one party, confirm and continue for the other: "Entendido lo del <X> — <persona> buscará otra escuela para eso. Yo sí estoy interesado con vosotros."]

Un par de preguntas antes de matricularme:

1. <credibility anchor — experience, language level, relevant background>. Para un caso así, ¿qué recomendáis? ¿Sería viable <cheapest option> y <add-on> si hiciera falta?

2. ¿Próximo inicio / próxima convocatoria?

Gracias,
<nombre>
```

**DO NOT plant doubt about the buyer's capabilities.** If Tyler tells you he speaks C1 Spanish or has 30 years driving experience, lead with that as a STRENGTH — don't ask if they have English support or extra help. Vendors quote based on signals; project confidence to anchor toward minimum-practicals pricing.

## Reuseable Across Categories
Same workflow applies to:
- Gestorías (immigration paperwork)
- Asesorías fiscales (tax)
- Clínicas dentales / médicas
- Contratistas / reformas
- Academias de idiomas
- Veterinarios
- Cualquier servicio local con web propia
