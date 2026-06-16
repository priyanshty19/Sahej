# Sahej · सहज

**Making sure no new mother in India misses the government help she's entitled to.**

## What is this? (in plain words)

When a baby is born in India, the government offers the mother several things — cash
payments, free hospital care, free vaccines for the baby, and more. But most families
never receive all of it. Not because the money isn't there, but because:

- nobody tells them which schemes they actually qualify for,
- the forms and rules are confusing and scattered across departments, and
- each benefit has a deadline that quietly passes.

So thousands of crores meant for poor families go unused every year, and mothers go
without help they had every right to.

**Sahej fixes the navigation.** It's a simple tool for the local government health
worker — the *ASHA* — who already visits every new mother at home. The worker enters a
few basic details about the mother and baby, and Sahej instantly shows:

- **every benefit she's owed**,
- **how much money** each one is,
- **which documents** she needs, and
- **the date by which to claim each one** —

laid out as a step-by-step checklist she can tick off over her routine visits, so nothing
slips through the cracks.

> **In one line:** Sahej turns a birth into a clear, dated checklist of every benefit the
> mother is owed — so the help actually reaches her.

### A quick example

Enter *a first-time mother in Bihar who delivered at a government hospital*. Sahej replies:
she's owed about **₹6,400** — claim the **₹1,400** delivery cash now, **register the birth
by day 21** (that unlocks another **₹2,000**), get the baby's first vaccines, and so on.
Change her state or situation and the answer updates automatically.

## Why this approach works (the core insight)

For childbirth, a trusted person is **already** at the mother's door on a fixed schedule:
ASHA workers make home visits on **days 3, 7, 14, 21, 28 and 42** after birth (and are paid
a small amount for each newborn they follow). Those visit days line up almost exactly with
the benefit deadlines. So Sahej doesn't need to invent a new app habit or a new field
force — it simply hands a smart checklist to someone who's already there, turning each
visit into *"here's exactly what this mother is owed today."*

## What's in this MVP (v0.2)

| File | What it is |
|------|------------|
| `PRODUCT_PLAN.md` | The owner's plan: personas, the full scenario matrix, feature set, roadmap. |
| `data/childbirth_schemes.json` | **The asset.** Structured, sourced rules + all **36 states/UTs** (LPS·HPS, opt-outs). |
| `engine.py` | Pure-stdlib resolver: eligibility, blocking, claimed-tracking, urgency, documents, sensitive-mode, migrants. CLI + `meta()`. |
| `test_engine.py` | **46 checks** across the whole scenario matrix. |
| `serve.py` | Zero-dependency server: `/api/meta` + `/api/resolve` (same engine as CLI/tests). |
| `web/index.html` | Full SPA: caseload, progress tracking, language toggle, docs checklist, alerts, share. |

## Run it

```bash
python3 test_engine.py     # 46 scenario checks
python3 engine.py --state BR --birth-date 2026-06-01 --child-number 1 --child-sex girl \
    --area rural --mother-age 24                     # CLI report
python3 engine.py --birth-outcome stillbirth --state BR    # sensitive case
python3 serve.py           # open http://localhost:8000 (web app)
```

## Scenarios the engine handles

- **All 36 states/UTs**, with LPS/HPS JSY amounts and **central-scheme opt-outs** (e.g. West Bengal ≠ PMMVY).
- **Parity & sex**: 1st child, 2nd-girl (₹6,000), 2nd-boy (no PMMVY); girl-child state schemes.
- **Delivery**: public / private-empanelled / private / home; C-section; JSSK entitlements.
- **Category & income**: JSY gated to BPL/SC/ST in High-Performing states.
- **Risk**: premature / low-birth-weight → SNCU + extra visits; disability → UDID pointer.
- **Sensitive outcomes**: stillbirth, neonatal death, maternal death → death registration, NFBS, compassionate mode (no cheerful framing, only what applies).
- **Migrants** (delivered outside home state), **missing Aadhaar/bank** (hard blocker), **govt employees**, **age & 270-day window**.
- **Journey state**: already-claimed items, blocked-by-prerequisite, overdue / due-soon urgency.

## Schemes encoded

PMMVY, JSY, JSSK (incl. SNCU), Birth Registration, **Death/Stillbirth Registration**,
Universal Immunization, **RBSK**, **NFBS** (survivor benefit), a **disability/UDID** pointer,
and representative **state** schemes (Tamil Nadu, Odisha, Madhya Pradesh, West Bengal).

## Honesty by design

This domain punishes hallucinated rules — a wrong "you qualify" costs a mother a day's
wage. Every rule carries `confidence`, `source_urls`, and a `needs_verification` flag,
surfaced in the UI. **Amounts/conditions are research-grade drafts — confirm against
current Government Orders before real use.** Not medical or legal advice.

## Beyond this MVP

Voice intake (Bhashini), WhatsApp delivery, real auth/DB & multi-ASHA dashboards,
auto-submission to government portals, and the next life events (death/survivor,
disability, job loss) on the same engine.
