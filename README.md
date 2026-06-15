# Sahej · सहज

**A proactive benefit co-pilot for India's welfare last mile — starting with childbirth.**

Every year, India leaves roughly **₹1 lakh crore (~$12B)** of welfare money unspent
because eligible people never claim it. The benefits exist; the *navigation* is broken.
Existing tools (myScheme, Haqdarshak, government chatbots) are **reactive** — you have
to know to ask. Sahej is **proactive**: a *life event* automatically surfaces every
benefit it unlocks, with deadlines, on a schedule that already runs.

## The core insight

For childbirth, a trusted human is **already** at the mother's door on a fixed schedule:
ASHA workers make **Home-Based Newborn Care visits on days 3, 7, 14, 21, 28 and 42**,
paid ₹250/newborn. Those days line up with the benefit deadlines. Sahej rides that rail —
each visit becomes a checklist of *exactly what this mother is owed today*.

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
