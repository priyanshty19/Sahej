#!/usr/bin/env python3
"""Tests for the Sahej resolver (v0.2). Run: python3 test_engine.py"""
from datetime import date

from engine import resolve, meta, load_kb, work_plan, ProfileError

KB = load_kb()
ASOF = date(2026, 6, 10)  # 9 days after a 2026-06-01 birth


def R(**kw):
    base = {"state": "BR", "delivery_type": "institutional_public", "child_number": 1,
            "child_sex": "girl", "mother_age_years": 24, "area": "rural", "birth_date": "2026-06-01"}
    base.update(kw)
    return resolve(base, as_of=kw.pop("_asof", ASOF))


def ids(r):
    return {it["component_id"] for it in r["timeline"]}


def ne_ids(r):
    return {n["scheme_id"] for n in r["not_eligible"]}


def item(r, cid):
    return next((it for it in r["timeline"] if it["component_id"] == cid), None)


def run():
    checks = []
    def chk(name, cond):
        checks.append((name, bool(cond)))

    # 1. Baseline: Bihar (LPS) first child, girl, public, live.
    r = R()
    chk("1: PMMVY i1/i2/i3 present", {"pmmvy_i1", "pmmvy_i2", "pmmvy_i3"} <= ids(r))
    chk("1: JSY rural-LPS Rs 1400", item(r, "jsy_delivery_cash")["cash_inr"] == 1400)
    chk("1: birth registration is gateway", item(r, "register_birth")["is_gateway"])
    chk("1: total remaining cash Rs 6,400", r["summary"]["remaining_cash_inr"] == 6400)
    chk("1: RBSK child screening present", "rbsk_screening" in ids(r))

    # 2. HPS general non-BPL: JSY should NOT apply (HPS limits to BPL/SC/ST).
    r = R(state="MH", area="urban", child_number=2, child_sex="girl", mother_age_years=29)
    chk("2: second-girl PMMVY Rs 6000 present", item(r, "pmmvy_second_girl")["cash_inr"] == 6000)
    chk("2: no first-child installments", not ({"pmmvy_i1", "pmmvy_i2", "pmmvy_i3"} & ids(r)))
    chk("2: JSY excluded for HPS general non-BPL", "jsy_delivery_cash" not in ids(r))
    chk("2: total cash = Rs 6,000", r["summary"]["remaining_cash_inr"] == 6000)

    # 2b/2c. HPS but BPL or SC -> JSY applies (urban HPS = 600).
    chk("2b: JSY applies for HPS BPL (Rs 600)",
        item(R(state="MH", area="urban", bpl=True), "jsy_delivery_cash")["cash_inr"] == 600)
    chk("2c: JSY applies for HPS SC mother",
        "jsy_delivery_cash" in ids(R(state="MH", area="urban", category="sc")))

    # 3. Second boy -> no PMMVY component qualifies.
    chk("3: no PMMVY for second boy",
        not any(i.startswith("pmmvy") for i in ids(R(child_number=2, child_sex="boy"))))

    # 4. Home delivery -> JSY & JSSK excluded.
    r = R(delivery_type="home")
    chk("4: JSY not eligible (home)", "jsy" in ne_ids(r))
    chk("4: JSSK not eligible (home)", "jssk" in ne_ids(r))
    chk("4: PMMVY still available (home)", "pmmvy_i1" in ids(r))

    # 5/6/7. PMMVY gates.
    chk("5: PMMVY closed after 270 days", "pmmvy" in ne_ids(R(birth_date="2025-08-01")))
    chk("6: PMMVY excluded under-age", "pmmvy" in ne_ids(R(mother_age_years=17)))
    chk("7: PMMVY excluded govt employee", "pmmvy" in ne_ids(R(govt_employee=True)))

    # 8. Tamil Nadu state scheme.
    chk("8: TN scheme present", "tn_mb_cash" in ids(R(state="TN", mother_age_years=26)))

    # 9. ASHA visit mapping valid.
    valid = set(KB["asha_hbnc_visits"]["visit_days"]) | {0, None}
    chk("9: actions map to real visit days", all(it["asha_visit_day"] in valid for it in R()["timeline"]))

    # 10. West Bengal opts out of PMMVY, runs its own.
    r = R(state="WB")
    chk("10: WB opts out of PMMVY", "pmmvy" in ne_ids(r))
    chk("10: WB state scheme surfaced", "wb_maternity_pointer" in ids(r))

    # 11. Stillbirth: no immunization, no PMMVY-3; death registration instead of birth reg; sensitive.
    r = R(birth_outcome="stillbirth")
    chk("11: stillbirth -> no immunization", "birth_doses" not in ids(r))
    chk("11: stillbirth -> no PMMVY 3rd", "pmmvy_i3" not in ids(r))
    chk("11: stillbirth -> death registration present", "register_death" in ids(r))
    chk("11: stillbirth -> no birth registration", "register_birth" not in ids(r))
    chk("11: stillbirth -> sensitive mode", r["summary"]["sensitive_mode"])

    # 12. Neonatal death: both birth and death registration; no immunization.
    r = R(birth_outcome="neonatal_death")
    chk("12: neonatal death -> birth + death registration",
        {"register_birth", "register_death"} <= ids(r))
    chk("12: neonatal death -> no immunization", "birth_doses" not in ids(r))

    # 13. Maternal death + BPL -> NFBS survivor benefit.
    r = R(maternal_outcome="deceased", bpl=True)
    chk("13: maternal death + BPL -> NFBS Rs 20,000", item(r, "nfbs_lumpsum") and item(r, "nfbs_lumpsum")["cash_inr"] == 20000)
    chk("13: maternal death -> sensitive mode", r["summary"]["sensitive_mode"])
    chk("13: NFBS absent without BPL", "nfbs" in ne_ids(R(maternal_outcome="deceased")))

    # 14. Premature / low-birth-weight -> SNCU sick-newborn care.
    chk("14: premature -> SNCU care", "jssk_sick_newborn" in ids(R(premature=True)))
    chk("14: healthy baby -> no SNCU card", "jssk_sick_newborn" not in ids(R()))

    # 15. Disability -> UDID pointer.
    chk("15: child disability -> UDID pointer", "udid_pointer" in ids(R(child_disability=True)))

    # 16. Already-claimed tracking reduces remaining cash.
    r = R(claimed=["pmmvy_i1"])
    chk("16: claimed item marked done", item(r, "pmmvy_i1")["done"])
    chk("16: remaining cash drops by 1000", r["summary"]["remaining_cash_inr"] == 5400)
    chk("16: total cash unchanged", r["summary"]["total_cash_inr"] == 6400)

    # 17. Blocking: PMMVY-3 blocked until birth reg + birth doses are done.
    chk("17: PMMVY-3 blocked when prereqs unclaimed", item(R(), "pmmvy_i3")["status"] == "blocked")
    r = R(claimed=["register_birth", "birth_doses"])
    chk("17: PMMVY-3 unblocked once prereqs claimed", item(r, "pmmvy_i3")["status"] != "blocked")

    # 18. No bank account -> hard blocker alert.
    chk("18: no bank -> blocker alert",
        any(a["level"] == "blocker" for a in R(has_bank_account=False)["alerts"]))

    # 19. Migrant: delivered elsewhere -> warning + JSY follows delivery state.
    r = R(state="MH", delivery_state="BR")  # resident HPS, delivered in LPS
    chk("19: migrant warning raised", any(a["level"] == "warn" for a in r["alerts"]))
    chk("19: JSY amount follows delivery state (LPS rural 1400)",
        item(r, "jsy_delivery_cash")["cash_inr"] == 1400)

    # 20. Overdue: birth registration deadline (day 21) already passed.
    r = R(birth_date="2026-05-01")  # ~40 days at ASOF
    chk("20: birth registration overdue", item(r, "register_birth")["status"] == "overdue")
    chk("20: overdue alert present", any(a["level"] == "overdue" for a in r["alerts"]))

    # 21. Meta exposes all 36 states/UTs.
    chk("21: meta lists 36 states/UTs", len(meta(KB)["states"]) == 36)

    # 22. Validation: bad input raises ProfileError with a clear message.
    def raises(msg_part, **kw):
        try:
            R(**kw)
            return False
        except ProfileError as e:
            return msg_part in str(e)
    chk("22: unknown state rejected", raises("unknown state code", state="XX"))
    chk("22: unknown delivery_state rejected", raises("delivery_state", delivery_state="ZZ"))
    chk("22: bad birth_date rejected", raises("YYYY-MM-DD", birth_date="01-06-2026"))
    chk("22: bad enum rejected", raises("invalid delivery_type", delivery_type="teleport"))
    chk("22: child_number range enforced", raises("child_number", child_number=0))
    chk("22: mother_age range enforced", raises("mother_age_years", mother_age_years=8))
    chk("22: non-numeric child_number rejected", raises("whole numbers", child_number="two"))

    # 23. Future birth date -> plan-ahead mode, not an error.
    r = R(birth_date="2026-07-01")  # 21 days after ASOF
    chk("23: future birth allowed", r["summary"]["eligible_count"] > 0)
    chk("23: future-birth warning raised", any("future" in a["text"] for a in r["alerts"]))
    chk("23: days_since_birth clamped to 0", r["profile"]["days_since_birth"] == 0)

    # 24. String inputs from HTTP query coerce cleanly.
    r = R(child_number="2", mother_age_years="29", child_sex="girl")
    chk("24: string child_number coerced", "pmmvy_second_girl" in ids(r))

    # 25. Application lifecycle: applied -> tracked; >45 days -> stuck + alert.
    r = R(birth_date="2026-05-01", applied="jsy_delivery_cash:2026-05-14", _asof=date(2026, 7, 3))
    it = item(r, "jsy_delivery_cash")
    chk("25: applied status set", it["status"] == "applied")
    chk("25: days since applied computed", it["days_since_applied"] == 50)
    chk("25: stuck after 45 days", it["stuck"])
    chk("25: stuck alert raised", any(a["level"] == "stuck" for a in r["alerts"]))
    chk("25: applied still counts as remaining cash", r["summary"]["remaining_cash_inr"] > 0)
    r = R(applied="jsy_delivery_cash:2026-06-05")  # 5 days at ASOF
    chk("25: fresh application not stuck", not item(r, "jsy_delivery_cash")["stuck"])
    chk("25: received wins over applied",
        item(R(claimed=["jsy_delivery_cash"], applied="jsy_delivery_cash:2026-06-05"), "jsy_delivery_cash")["status"] == "done")
    try:
        R(applied="jsy_delivery_cash:junk")
        chk("25: bad applied date rejected", False)
    except ProfileError:
        chk("25: bad applied date rejected", True)

    # 26. Apply-at / grievance channels present on every scheme.
    chk("26: every scheme has apply_at + grievance",
        all(s.get("apply_at") and s.get("grievance") for s in KB["schemes"]))
    chk("26: channels carried onto timeline items",
        all(it["apply_at"] for it in R()["timeline"]))

    # 28. Death life event: survivor benefits resolve on the same engine.
    def D(**kw):
        base = {"state": "BR", "death_date": "2026-06-20", "bpl": True,
                "deceased_age_years": 42, "applicant_age_years": 45}
        base.update(kw)
        return resolve(base, as_of=date(2026, 7, 4), life_event="death")
    r = D(construction_worker=True, accidental_death=True)
    chk("28: death event resolves with survivor items",
        {"register_death", "nfbs_lumpsum", "heir_certificate", "pmjjby_payout"} <= ids(r))
    chk("28: death is always sensitive mode", r["summary"]["sensitive_mode"])
    chk("28: death registration is the gateway", item(r, "register_death")["is_gateway"])
    chk("28: NFBS blocked until death registered",
        item(r, "nfbs_lumpsum")["status"] == "blocked")
    chk("28: accidental death unlocks PMSBY", "pmsby_payout" in ids(r))
    chk("28: non-accidental death has no PMSBY", "pmsby_payout" not in ids(D()))
    chk("28: BOCW death benefit gated on worker card",
        "bocw_death_cash" in ids(r) and "bocw_death_cash" not in ids(D()))
    chk("28: widow pension for BPL widow 45", "ignwps_pension" in ids(D()))
    chk("28: widow pension age-gated (38 too young for IGNWPS)",
        "widow_pension" in ne_ids(D(applicant_age_years=38)))
    chk("28: NFBS needs BPL", "nfbs" in ne_ids(D(bpl=False)))
    try:
        D(relation_to_deceased="cousin")
        chk("28: death validation catches bad relation", False)
    except ProfileError:
        chk("28: death validation catches bad relation", True)
    try:
        resolve({"state": "BR"}, life_event="wedding")
        chk("28: unknown life_event rejected", False)
    except ProfileError:
        chk("28: unknown life_event rejected", True)

    # 27. work_plan: ordering, totals, per-mother error isolation.
    plan = work_plan([
        {"id": "a", "name": "VisitToday", "profile": {"state": "BR", "birth_date": "2026-06-30"}},
        {"id": "b", "name": "Overdue", "profile": {"state": "MH", "birth_date": "2026-05-20", "area": "urban"}},
        {"id": "c", "name": "Broken", "profile": {"state": "XX"}},
    ], kb=KB, as_of=date(2026, 7, 3))
    chk("27: overdue mother ranked first", plan["plan"][0]["name"] == "Overdue")
    chk("27: visit-today flagged", any(e["visit_today"] for e in plan["plan"]))
    chk("27: invalid mother isolated as error, not crash",
        len(plan["errors"]) == 1 and "unknown state" in plan["errors"][0]["error"])
    chk("27: totals aggregate", plan["totals"]["mothers"] == 2 and plan["totals"]["remaining_cash_inr"] > 0)
    mixed = work_plan([
        {"id": "a", "name": "Birth", "profile": {"state": "BR", "birth_date": "2026-07-01"}},
        {"id": "b", "name": "Death", "profile": {"life_event": "death", "state": "UP",
                                                 "death_date": "2026-06-20", "bpl": True}},
    ], as_of=date(2026, 7, 4))
    chk("27: mixed life events in one plan",
        {e["event"] for e in mixed["plan"]} == {"childbirth", "death"})
    chk("27: empty caseload plan does not crash",
        work_plan([], as_of=date(2026, 7, 4))["totals"]["mothers"] == 0)

    passed = sum(1 for _, ok in checks if ok)
    for name, ok in checks:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}")
    print(f"\n{passed}/{len(checks)} checks passed.")
    return passed == len(checks)


if __name__ == "__main__":
    import sys
    sys.exit(0 if run() else 1)
