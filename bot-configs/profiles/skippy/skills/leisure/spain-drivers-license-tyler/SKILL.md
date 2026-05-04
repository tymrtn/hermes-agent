---
name: spain-drivers-license-tyler
description: Tyler + Yuliya getting Spanish driving licenses (Madrid). Context, constraints, and outreach state for the autoescuela inquiry workflow. Load when drafting replies to autoescuelas or discussing license logistics.
tags: [spain, madrid, drivers-license, autoescuela, tyler, yuliya]
triggers: ["autoescuela", "driving school", "carnet B", "driver's license spain", "permiso conducir"]
---

# Spain Driver's License — Tyler + Yuliya

## Core facts (memorize — these change the math in every reply)

- **Both starting from zero in Spain.** No US↔ES license exchange agreement. Full teórico + práctico required.
- **Tyler:** permiso B (manual). 30 years driving experience across US, UK, Canada, Puerto Rico, Spain (on international permit). Speaks Spanish at C1. The teórico is not a concern. Target: minimum practicals, cheapest path to exam.
- **Yuliya:** permiso B automático. Also experienced. Needs a school that does automatics (many Madrid schools don't).
- **Tyler drives everything:** manual, roundabouts in his sleep, claims top-tier parallel parking. Instructors will sign him off fast. Don't plant doubt about skill or language in replies.

## The real market (Madrid, 2026)

Typical pricing structure across all schools:
- Matrícula + teórico + libro + app: 30–200 € (loss-leader)
- Gestiones con Tráfico: ~30 €
- Práctica (45 min): 28–35 €
- Examen práctico: 50–80 €
- Tasas DGT (2 convocatorias): ~94 €
- Bonos de 5/10 prácticas: 170 € / 330 € typical

Packs look cheap because they include only 2–5 practicals. True totals:
- Best case experienced driver: **~450–550 €**
- Realistic (10 practicals): ~650–750 €
- With one retake: ~850–950 €

Teórico exam wait: ~1.5 months after requesting.

## Outreach state (as of 2026-04-17 evening)

Master inquiry email sent from `ty@tmrtn.com` via Envelope to 16 schools. Contact forms submitted on 3 more. Subject: "Consulta carnet B — pareja americana".

**Schools contacted (emailed):**
Palomero, Prado, Lara, Autoescuela 2000, Monte, Goya, Moncloa, Mcluni, Retiro, Abril, San Bernardo, Briones, Malasaña, Arenal, GALA, Chamberí.

**Contact form submitted:** Fitipaldi, Rayo, Montero Espinosa.

**Blocked / dead:** Isla (Cloudflare), Xtreme + GALA García de Paredes (reCAPTCHA), Lealtad / Fuencarral / Ideal / Jucar / La Ermita (no reachable form or site).

**Replies received:**

- **Autoescuela Monte** (C/ Padilla 71, Salamanca, 914014574) — replied 2026-04-17. Manual only (NO automático, disqualified for Yuliya). For Tyler: matrícula+teórico 145€, práctica 35€, bono 10 = 330€, bono 5 = 170€, tasas 94€, examen práctico 79€, gestión 30€. Packs: 2 prácticas 206€ / 5 prácticas 306€. Teórico presencial Spanish only. Exam wait 1.5 months. Office Mon-Thu 10:30-13:30 + 16:30-20:00, Fri 10:30-17:30. Second reply (16:13) was non-specific on practicals — dodged pack question. Follow-up sent asking wait-time + honest pack recommendation.

- **Autoescuela MC Luni / Formación MC Luni** (C/ Hermano Garate 13, Tetuán, 915793464, Conchi Rivas) — replied 2026-04-17. DOES offer automático. Pricing from OCR'd PDFs:
  - Manual matrícula options: 69€ (3 prácticas, 3mo teórico) or 350€ (12 prácticas, 6mo teórico)
  - Manual práctica 35€ (sáb 42€), bono 10 = 340€, gestión 45€, examen práctico 95€
  - Automático: clase 39€, bono 10 = 380€, gestión 45€, examen 95€
  - Tasa DGT 94.05€. IVA 21% incluido.
  - Teórico app-only (Spanish or English). NO presencial teórico.
  - Teórico exam wait 1.5 months from request.
  - ⚠️ **3-4 month wait for practicals after passing teórico**, depending on schedule. This is the critical concern driving the follow-up wave.
  - Automático instructor only takes students who speak Spanish (Yuliya OK at functional level).
  - Hours: mornings 8-14, afternoons 16-20.

- **Autoescuela Lara** (sol@autoescuelalara.com, Usera + Sol branches) — replied 2026-04-17. Intensivos only at Sol branch.
  - Curso 1: 123€ (3 prácticas incluidas) / Curso 2: 437€ (14 prácticas incluidas)
  - Both include matrícula, pack bienvenida, teórico online ilimitado + presencial L-J (Marqués de Vadillo 19:30-20:30 / Usera 18:00-18:45), WhatsApp profesor 24/7, 6mo test online
  - Clase suelta 38.90€, bono 6 = 227€, bono 11 = 407€
  - Tasa DGT 94.05€, gestión 48€/examen, examen 102€
  - Clases prácticas L-V 7:15-22:00, sábados variable
  - Intensivos: mañana 10-13 / tarde 18:15-21:15, sólo en sede Sol
  - Solo en castellano. Did NOT mention automático — follow-up sent asking.

## Market signal: practicals wait time

Mc Luni flagged 3-4 month wait for practicals after teórico. This is potentially **the #1 timeline killer** — worse than teórico exam wait. Need to confirm if this is market-wide or Mc Luni being slow. Every future inquiry and follow-up MUST ask:
1. Time from teórico pass → first práctica available
2. Time from práctica readiness → DGT exam slot
3. Whether specific franjas (early mornings, intensivos) cut the wait

Follow-ups sent 2026-04-17 evening to Monte, Mc Luni, Lara with this as lead question.

## Rules for drafting autoescuela replies

1. Short, direct, Spanish. Match their tone — these are small local businesses, not VCs.
2. Do NOT apply the cold-email-anti-ai skill here. That skill is scoped to cold outreach / bizdev / pitch emails where voice and credibility matter. Autoescuela replies are transactional admin.
3. Default send identity: **ty@tmrtn.com** via Envelope CLI.
4. Always mention: 30 years driving, multiple countries, C1 Spanish. This anchors them toward minimum-practicals quote.
5. Ask three things: how many practicals they'd recommend for an experienced driver, next teórico course start, next available examen date.
6. If the school doesn't do automático, confirm and continue for Tyler only.
7. Tyler has delegated transactional email drafting+sending to Skippy. Cold outreach still requires Tyler to write final.

## Envelope command pattern

```bash
envelope send --account ty@tmrtn.com \
  --to <recipient> \
  --subject "RE: Consulta carnet B — pareja americana" \
  --body "..."

# Check inbox for replies
envelope inbox --account ty@tmrtn.com --limit 30

# Read a specific reply
envelope read --account ty@tmrtn.com <UID>

# Download attachment (common — schools send price sheets as .docx)
envelope attachment list --account ty@tmrtn.com <UID>
envelope attachment download --account ty@tmrtn.com <UID> "<filename>" --output /tmp/...
```

## Template (proven) — Tyler's standard follow-up after initial quote

```
Hola,

Gracias por la información y la rapidez.

[Automático note if relevant: "Entendido lo del automático — mi mujer buscará otra escuela para eso. Yo sí estoy interesado en el manual con vosotros."]

Un par de preguntas antes de matricularme:

1. Llevo 30 años conduciendo (EE.UU., Reino Unido, Canadá, Puerto Rico y aquí en España con permiso internacional) y hablo español C1, así que el teórico no me preocupa. Para un caso así, ¿cuántas prácticas soléis recomendar antes del examen? ¿Sería viable el pack de 5 y alguna suelta si el profesor lo ve necesario?

2. ¿Cuándo empieza el próximo curso teórico presencial y cuándo sería la siguiente convocatoria de examen teórico disponible?

Gracias,
Tyler Martin
```

## Current open decisions

- Waiting on replies from 15 other schools + 2nd-round replies from Monte/Mc Luni/Lara (as of 2026-04-17 evening).
- Yuliya needs automático — filter replies for schools that offer it.
- Tyler prefers cheapest path + fastest exam slot.
- When 5+ replies are in, normalize into comparison table (include wait time column, not just prices).
- Nudge non-responders Monday if silence persists past 48h.

## 2026-04-20 Monday follow-up check — status

- **Envelope credential store is broken as of ~2026-04-19 22:34**. `envelope inbox/search/accounts list` all return `failed to decrypt credentials: decryption error: aead::Error`. Affects every account, both `--credential-store file` and `--credential-store keychain`.
- **Root cause (confirmed 2026-04-20):** master key in `~/Library/Application Support/envelope-email/credentials.json` was regenerated at Apr 19 22:34 while `envelope.db` (last touched Apr 19 21:13) still holds AES-GCM blobs encrypted with the OLD key. Classic key/data mismatch.
- **Not a version issue.** Installed is 0.4.1, latest GitHub release is 0.4.1 — 0.5.0 does not exist. Reinstalling / `brew upgrade tymrtn/envelope/u1f4e7` will not fix this.
- **`credentials.json` is NOT in Dropbox** — lives under `~/Library/Application Support/envelope-email/`. Dropbox version history cannot restore it.
- **Fix path being executed:** `scp` pre-22:34 `credentials.json` from old machine `tylers-macbook-pro` (Tailscale `100.105.150.28`) to new machine `wonderbookneo`. Fallback: remove + re-add all 15 accounts (requires every app password). See skill `envelope-credential-recovery` for the full diagnostic + recovery procedure.
- **DB sync window covered**: the local thread DB was last updated 2026-04-18 23:41Z, so Fri Apr 17 18:15 CEST → Sat Apr 18 23:41 UTC (~29h) is fully indexed. **No autoescuela replies arrived in that window.** Sun Apr 19 00:00 UTC → Mon Apr 20 ~07:00 UTC is UNSYNCED — any weekend replies are unknown until envelope is repaired.
- **Non-responders still silent at DB cutoff**: Palomero, Prado, Autoescuela 2000, Goya, Moncloa, Retiro, Abril, San Bernardo, Briones, Malasaña, Arenal, GALA, Chamberí, Fitipaldi, Rayo, Montero Espinosa. Weekends are largely dead for this sector — Monday afternoon is the real first-reply window. Don't nudge yet; give it until Tue AM once envelope is fixed.
- **Wait-time follow-ups (sent late Fri to Monte, Mc Luni, Lara)**: no response visible in synced window. Need live inbox after fix to confirm.

## Shortlist recommendation (provisional, based on Apr 17 data only)

For **Tyler (manual)**, ranked:
1. **Autoescuela Monte** — cheapest sticker (bono 5 = 170€, matrícula 145€, gestión 30€, tasas 94€, examen 79€ → ~518€ best-case). Salamanca location. Teórico presencial Spanish only (fine for Tyler). Wait time unknown until they reply to Fri follow-up.
2. **Mc Luni** — matches Monte on practicals (35€/clase, bono 10 = 340€) with matrícula 69€ (3-prac option). BUT 3-4mo post-teórico practical wait is the disqualifier if confirmed market-wide. App-only teórico.
3. **Lara** — intensivos option at Sol (10-13 or 18:15-21:15) could compress the practical wait dramatically. Curso 2 at 437€ bundles 14 practicals — expensive but eliminates uncertainty. Worth Tyler's time if Mc Luni's 3-4mo wait is confirmed.

For **Yuliya (automático)**: only Mc Luni confirmed so far (380€ bono 10, 95€ examen). Need more automático-offering schools — half the non-responders may not do automatic at all.

**Key unknown driving the decision**: whether the 3-4mo practical wait is Mc Luni-specific or Madrid-wide. Until 3+ schools answer that question, shortlist is premature.
