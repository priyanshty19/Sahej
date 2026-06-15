# Sahej — Product Plan (MVP)

*Owner's plan. Scope: the childbirth life-event, done completely — every realistic
case an ASHA worker will hit in the field, plus the product surface to act on it.*

## 1. Who it's for

- **Primary user: the ASHA worker.** Uses it on a cheap Android phone, often offline,
  during her routine HBNC home visits (days 3/7/14/21/28/42). She manages a *caseload*
  of mothers, not one.
- **Beneficiary: the new mother & newborn.** She receives the cash and entitlements.
- **Buyer/partner: State Health Mission / NGO / CSR.** Pays so the service is free to the poor.

## 2. The job to be done

Turn a birth into: *"For THIS mother, here is every benefit she's owed, how much, by which
deadline, what documents she needs, what to do first — tracked until the money lands."*

## 3. Scenario matrix — every case the engine must handle

### A. Who the mother is
| Dimension | Values | Effect |
|---|---|---|
| State / UT | all 28 + 8 | LPS vs HPS (JSY amount), state schemes, **opt-outs (e.g. West Bengal ≠ PMMVY)** |
| Area | rural / urban | JSY amount |
| Social category | general / OBC / SC / ST | Gates JSY in HPS, some state schemes |
| Income | BPL / APL | Gates JSY (HPS), NFBS, several state schemes |
| Age | <18, 18y7m–55, >55 | PMMVY age gate |
| Govt/PSU employee | yes / no | PMMVY exclusion |
| Single mother | widow/divorced/abandoned/unmarried | Extra pointers (widow pension, etc.) |
| Disability (mother/child) | yes / no | Disability schemes pointer (UDID) |
| Migrant | delivered outside home state | Warn: claim JSY where delivered; portability caveats |
| Aadhaar / bank | present / missing | **Hard blocker** for all cash — surface first |

### B. The delivery & the child
| Dimension | Values | Effect |
|---|---|---|
| Delivery type | public / private-empanelled / private / home | JSY & JSSK eligibility |
| C-section | yes / no | JSSK covers it free; flag overcharging |
| Parity (child number) | 1, 2, 3+ | PMMVY (1st child; 2nd only if girl) |
| Child sex | girl / boy | PMMVY 2nd-child rule; girl-child schemes |
| Multiple birth | single / twins / triplets | JSY per delivery; immunization per child; PMMVY counts once |
| Prematurity / low birth weight | yes / no | SNCU free care, extra ASHA visits, RBSK |
| **Birth outcome** | live / **stillbirth** / **neonatal death** | Live-only gates (immunization, PMMVY 3rd); death registration; compassionate mode |
| **Maternal outcome** | alive / **deceased** | JSY/JSSK to family; **NFBS** (₹20,000) if breadwinner & BPL; compassionate mode |

### C. Where she is in the journey (state, not just eligibility)
- Which installments/actions are **already claimed** → don't re-prompt; compute *remaining* cash.
- Which actions are **blocked** by an unmet prerequisite (e.g. PMMVY 3rd needs birth registration **completed**).
- Which actions are **overdue** vs **urgent (≤3 days)** vs upcoming, relative to today.

### D. Sensitive cases (handle with care, not as an error)
- Stillbirth / infant death: drop the cheerful framing, show only what applies (delivery
  entitlement, death registration, any bereavement support), gentle copy.
- Maternal death: route benefits to the family/guardian, surface NFBS, never address "the mother."

## 4. Schemes in the MVP knowledge base

**Central:** PMMVY, JSY, JSSK (incl. SNCU sick-newborn), Birth Registration, Death
Registration, Universal Immunization, RBSK (child screening), NFBS (family benefit on
breadwinner death), and a disability pointer (UDID).
**State (representative, flagged `needs_verification`):** Tamil Nadu (Dr. Muthulakshmi),
Odisha (MAMATA), Madhya Pradesh (Ladli Laxmi), Delhi (Ladli), West Bengal (own scheme +
PMMVY opt-out). The model is built so any state's schemes plug in.

> **Data honesty:** amounts/conditions are research-grade drafts. Every rule carries
> `confidence`, `source_urls`, and a `needs_verification` flag surfaced in the UI. A
> partner verifies against current Government Orders before real use. Not legal advice.

## 5. Product features (MVP)

1. **All states/UTs** selector (grouped states / UTs, LPS·HPS marked).
2. **Full intake form** covering the scenario matrix above (progressive — advanced fields collapsed).
3. **Personalised timeline** grouped by ASHA visit, with cash, deadlines, docs, dependencies, urgency.
4. **Caseload** — save many mothers, reopen them (localStorage; offline-friendly).
5. **Progress tracking** — tick off each action; "remaining cash" updates; persists per mother.
6. **Document checklist** — aggregated, de-duplicated, "which scheme needs each".
7. **Urgency & reminders** — overdue / due-soon banners; "next action".
8. **Language toggle** — English ⇄ हिन्दी (UI + scheme names); architecture ready for Bhashini's 22 languages.
9. **Share** — one-tap WhatsApp/printable summary for the family.
10. **Grievance guidance** — if a JSSK entitlement was charged for, show how to escalate.
11. **Eligibility transparency** — "not eligible & why", and "verify" flags shown, never hidden.

## 6. Architecture

`data/childbirth_schemes.json` (knowledge + all states) → `engine.py` (pure resolver:
eligibility, blocking, documents, urgency, sensitive-mode) → `serve.py` (`/api/resolve`,
`/api/meta`) → `web/index.html` (offline-tolerant SPA, localStorage caseload). One engine,
shared by CLI, tests, and web. `test_engine.py` proves every scenario above.

## 7. Beyond MVP (not in this build)

Voice intake (Bhashini), WhatsApp bot delivery, real auth/DB & multi-ASHA dashboards,
auto-submission to government portals, and the next life events (death/survivor, disability,
job loss) on the same engine.
