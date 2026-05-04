---
name: spain-telco-dispute
description: Tyler playbook for disputing unauthorized cancellations, silent price hikes, or billing errors with Spanish telcos (Movistar, Digi, Vodafone, Orange, Yoigo, O2). Load when Tyler references a telco problem, a lost line, a surprise charge, or wants to file a hoja de reclamaciones.
tags: [spain, telco, movistar, digi, vodafone, dispute, reclamacion, omic, setsi]
triggers: ["movistar", "digi", "vodafone", "orange", "yoigo", "hoja de reclamaciones", "baja sin aviso", "telco", "fibra cancelada", "portabilidad"]
---

# Spain Telco Dispute Playbook

Tyler has had recurring issues with Spanish telcos — Movistar cancelled his fibre in April 2026 with no notice, no verification, and closed his account so he couldn't even log in. Digi install was pending at the time. This skill captures the working approach.

## The regulatory stack (know before you write)

1. **OMIC** (Oficina Municipal de Información al Consumidor) — local municipal consumer office. Free, accepts forms online or in person. Madrid: https://www.madrid.es/portal/site/munimadrid
2. **Secretaría de Estado de Telecomunicaciones (SETSI / SETID)** — sector-specific regulator. The binding escalation for telecoms. https://avantel.mineco.gob.es/
3. **Hoja de reclamaciones** — every business with public customer service must provide one on request. You can demand it in person at any store; the company's refusal is itself a violation.
4. **AEPD** — if the telco processed your data without authorization (e.g. executed a cancellation with no ID check), this is a GDPR angle too.

Mention OMIC + SETSI by name in the opening complaint. It changes the tone from "angry customer" to "customer who knows the escalation path."

## The two-trap opener

When you don't yet know what happened, use these two questions to force a useful on-the-record answer:

1. **"¿Qué canal solicitó la baja y qué identidad se registró?"**
   If the answer is a competitor's portability request, your cancel was triggered by a portability-in — you may have authorized that indirectly (by signing up with the new operator) but you did NOT authorize losing service. That's still Movistar's error, but it reframes the fight.

2. **"¿Qué verificación de identidad se aplicó antes de ejecutar la baja?"**
   If the answer is "none" or "the portability request itself" you have a GDPR angle: they took a destructive action on a data subject's account without authenticating the request. Article 5(1)(f) integrity & confidentiality.

Both questions force them to commit to a fact on the record before they've heard your side. Do NOT volunteer information that lets them guess your angle.

## What to hold back in the opener

Do NOT put these in the first message:
- DNI/NIE (wait until a human agent takes the chat)
- Full address (nice for final paperwork, not needed for account lookup)
- Admissions ("I think I may have signed up with Digi" → weaponizable)
- Legal threats ("I'll sue" → gets you ticket-bounced instead of escalated)

DO put in the first message:
- Name + phone line associated
- "Escribo porque..." statement of fact
- The numbered on-the-record questions (4 is the sweet spot)
- Explicit mention of hoja de reclamaciones + OMIC + SETSI
- Request for written response in the same channel

## Proven template — Movistar unauthorized cancellation

Send to WhatsApp 638 10 1004 (9-22h) or the chat on movistar.es:

```
Buenas tardes. Soy [Nombre]. Escribo porque mi servicio de fibra Movistar
ha sido dado de baja sin previo aviso ni verificación por mi parte. No he
recibido ningún email, SMS ni llamada confirmando o solicitando la baja,
y la cuenta de cliente aparece cerrada, por lo que tampoco puedo consultar
la información yo mismo.

Necesito por favor que me indiquen por escrito en este chat:

1. Fecha y hora exacta en que se tramitó la baja.
2. Canal por el que se solicitó (teléfono, web, tienda, portabilidad
   entrante de otro operador, etc.) y la identidad del solicitante.
3. Motivo registrado en el sistema.
4. Qué verificación de identidad se aplicó antes de ejecutar la baja.

Esta información la necesito para presentar una hoja de reclamaciones
y, si procede, elevar el caso a la OMIC y a la Secretaría de Estado de
Telecomunicaciones.

Para identificar la cuenta puedo facilitarles DNI/NIE y número de
teléfono asociado en cuanto un agente humano tome el chat.

Gracias.
```

Bot replies will try to deflect. Repeat the questions verbatim until a human takes over.

## Channels and response times (2026)

| Operator | WhatsApp | Phone | Web chat | Notes |
|---|---|---|---|---|
| Movistar | 638 10 1004 (9-22h) | 1004 | movistar.es | First response usually bot |
| Digi | — | 1200 (free from Digi), 642 642 642 | digimobil.es | Slow on email, fast on phone |
| Vodafone | 607 100 007 | 1443 | vodafone.es | |
| Orange | 683 11 00 00 | 1470 | orange.es | |

## Escalation path

1. Day 0: open the complaint via the operator's WhatsApp or chat with the template above. Save the chat transcript (screenshot + `GET /chat/:id` from the bridge).
2. Day 1-3: if they don't fix it, request the hoja de reclamaciones explicitly. Every operator must provide one.
3. Day 4+: submit the hoja via OMIC online form. Attach the chat transcript.
4. Day 10+: if still unresolved, file with SETSI. This is the binding escalation — operators take SETSI complaints seriously because they cost real money in admin sanctions.
5. Parallel: if identity verification was missing, file an AEPD complaint. Separate track, but pressure stacks.

## Tyler-specific context

- **Primary line:** +34 664 895 954 (Tyler's Spanish mobile).
- **Wife:** Yuliya. Some utilities may be in her name; check before claiming ownership.
- **Address:** Madrid (exact street not stored in memory; pull from utility bills in mailbox if needed, but not required to open the complaint).
- **Billing emails:** Movistar did NOT surface in the Envelope-connected accounts as of Apr 2026 — Movistar may be billed paper-only or to a different email. Do not waste time searching ty@tmrtn.com for `from:movistar`.
- **Digi status:** Contract 113866384 signed April 2026. Installation pending. SIM delivered but defective (replacement needed separately via 1200).

## After sending

1. Update `FOLLOWUPS.md` with a new entry: ID, date opened, channel, expected response time.
2. Save the conversation transcript when a human agent replies (for reclamación evidence).
3. If Movistar admits the cancel was triggered by Digi's portability-in: that's actually normal Spanish telecom flow and Tyler likely authorized it by signing Digi's contract. The real grievance is then the lack of notification + closed online account, not the cancel itself. Reframe the complaint accordingly.
