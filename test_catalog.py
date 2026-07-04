#!/usr/bin/env python3
"""Tests for catalog.py (marketplace faceted search). Run: python3 test_catalog.py"""
from catalog import load_catalog, search, get, facet_meta

CAT = load_catalog()


def ids(r):
    return {s["id"] for s in r["schemes"]}


def run():
    checks = []

    def chk(name, cond):
        checks.append((name, bool(cond)))

    # Catalog integrity
    all_ids = [s["id"] for s in CAT["schemes"]]
    chk("no duplicate scheme ids", len(all_ids) == len(set(all_ids)))
    chk("every scheme has name + summary + status",
        all(s.get("name") and s.get("summary") and s.get("status") for s in CAT["schemes"]))
    chk("every curated scheme has source_urls",
        all(s.get("source_urls") for s in CAT["schemes"]))
    chk("life-event KB schemes derived in", any(s["id"].startswith("le_") for s in CAT["schemes"]))
    chk("catalog has 50+ schemes", len(CAT["schemes"]) >= 50)

    # Unfiltered browse
    r = search({}, catalog=CAT)
    chk("browse returns everything non-draft", r["total"] >= 50)
    chk("cards are compact (no eligibility trees)", all("facets" not in s for s in r["schemes"]))

    # State facet: central always visible, other states' schemes hidden
    r = search({"state": "MP"}, catalog=CAT)
    chk("MP sees central + MP schemes", "pm_kisan" in ids(r) and "ladli_behna_mp" in ids(r))
    chk("MP does not see WB scheme", "lakshmir_bhandar_wb" not in ids(r))
    r = search({"state": "WB"}, catalog=CAT)
    chk("WB sees its own scheme", "lakshmir_bhandar_wb" in ids(r))

    # Gender + age
    r = search({"gender": "male", "age": "45"}, catalog=CAT)
    chk("male 45 excluded from women-only cash", "ladli_behna_mp" not in ids(r)
        and "pmuy" not in ids(r))
    chk("male 45 keeps universal schemes", "ab_pmjay" in ids(r))
    r = search({"age": "70"}, catalog=CAT)
    chk("age 70 excluded from APY (max 40)", "apy" not in ids(r))
    chk("age 70 gets old-age pension", "ignoaps" in ids(r))
    r = search({"age": "30"}, catalog=CAT)
    chk("age 30 excluded from old-age pension", "ignoaps" not in ids(r))

    # Category
    r = search({"category": "general", "occupation": "student"}, catalog=CAT)
    chk("general student excluded from SC scholarship", "post_matric_sc" not in ids(r))
    chk("general student keeps NMMS", "nmms" in ids(r))
    r = search({"category": "sc", "occupation": "student"}, catalog=CAT)
    chk("SC student sees SC post-matric", "post_matric_sc" in ids(r))
    chk("SC student not shown ST scholarship", "post_matric_st" not in ids(r))

    # facet_logic category_or_gender (Stand-Up India)
    r = search({"category": "general", "gender": "female"}, catalog=CAT)
    chk("general woman still sees Stand-Up India", "standup_india" in ids(r))
    r = search({"category": "sc", "gender": "male"}, catalog=CAT)
    chk("SC man still sees Stand-Up India", "standup_india" in ids(r))
    r = search({"category": "general", "gender": "male"}, catalog=CAT)
    chk("general man does not see Stand-Up India", "standup_india" not in ids(r))

    # Occupation + residence
    r = search({"occupation": "farmer"}, catalog=CAT)
    chk("farmer sees PM-Kisan + KCC + PMFBY", {"pm_kisan", "pmmsy_kcc", "pmfby"} <= ids(r))
    chk("farmer not shown street-vendor loan", "pm_svanidhi" not in ids(r))
    r = search({"residence": "urban"}, catalog=CAT)
    chk("urban excluded from rural housing", "pmay_g" not in ids(r))
    chk("urban sees urban housing", "pmay_u" in ids(r))

    # Income / BPL gates
    r = search({"income": "400000", "occupation": "student"}, catalog=CAT)
    chk("income 4L excluded from 2.5L-cap scholarship", "post_matric_sc" not in ids(r))
    chk("income 4L keeps 8L-cap coaching", "free_coaching_scobc" in ids(r))
    r = search({"bpl": "false"}, catalog=CAT)
    chk("explicit non-BPL hides BPL-gated pension", "ignoaps" not in ids(r))
    r = search({"bpl": "true"}, catalog=CAT)
    chk("BPL household sees BPL-gated pension", "ignoaps" in ids(r))

    # Disability + life-event
    r = search({"disability": "true"}, catalog=CAT)
    chk("disability filter keeps UDID + IGNDPS", {"udid", "igndps"} <= ids(r))
    r = search({"life_event": "childbirth"}, catalog=CAT)
    chk("childbirth event surfaces PMMVY card + KB-derived JSY",
        "pmmvy_cat" in ids(r) and "le_jsy" in ids(r))

    # Text search (EN + HI)
    r = search({"q": "pension"}, catalog=CAT)
    chk("text search finds pensions", "ignoaps" in ids(r) and "apy" in ids(r))
    r = search({"q": "पेंशन"}, catalog=CAT)
    chk("hindi text search works", "ignwps" in ids(r))

    # Benefit type + sorting + limit
    r = search({"benefit_type": "scholarship"}, catalog=CAT)
    chk("benefit_type filter", all("scholarship" == get(s["id"], CAT)["benefit"]["type"] for s in r["schemes"]))
    r = search({}, catalog=CAT, limit=5)
    chk("limit respected, total unaffected", len(r["schemes"]) == 5 and r["total"] > 5)

    # get()
    s = get("pm_kisan", CAT)
    chk("get returns full entry with facets+apply", s and "facets" in s and s["apply"]["url"])
    chk("get unknown -> None", get("nope_xyz", CAT) is None)

    # facet_meta
    m = facet_meta(CAT)
    chk("facet_meta lists filter vocabularies",
        "farmer" in m["occupations"] and "sc" in m["categories"] and m["total"] >= 50)

    passed = sum(1 for _, ok in checks if ok)
    for name, ok in checks:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}")
    print(f"\n{passed}/{len(checks)} checks passed.")
    return passed == len(checks)


if __name__ == "__main__":
    import sys
    sys.exit(0 if run() else 1)
