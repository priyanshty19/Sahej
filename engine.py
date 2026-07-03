#!/usr/bin/env python3
"""
Sahej — childbirth benefit resolver (v0.2).

Takes a mother's full profile and returns the personalised set of benefits she is
owed: a deadline-ordered timeline mapped onto the ASHA worker's home-visit schedule,
with blocking (prerequisites), already-claimed tracking, document aggregation, urgency,
sensitive-case handling, state opt-outs and migrant warnings. Pure stdlib.

CLI:
    python3 engine.py --state BR --birth-date 2026-06-01 --delivery institutional_public \\
        --child-number 1 --child-sex girl --mother-age 24 --area rural
    python3 engine.py ... --json
"""
from __future__ import annotations

import argparse
import json
import os
from datetime import date, datetime

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
LIFE_EVENTS = ("childbirth", "death")
KB_PATH = os.path.join(DATA_DIR, "childbirth_schemes.json")  # back-compat alias

# Option lists the UI/CLI use to render the intake form (childbirth event).
META = {
    "delivery_type": ["institutional_public", "institutional_private_empanelled", "institutional_private", "home"],
    "child_sex": ["girl", "boy"],
    "category": ["general", "obc", "sc", "st"],
    "birth_outcome": ["live", "stillbirth", "neonatal_death"],
    "maternal_outcome": ["alive", "deceased"],
    "area": ["rural", "urban"],
}

# Option lists for the death (survivor-support) event.
DEATH_META = {
    "relation_to_deceased": ["spouse", "child", "parent", "sibling", "other"],
    "applicant_sex": ["female", "male"],
    "area": ["rural", "urban"],
}

# -----------------------------------------------------------------------------
# Predicate evaluation
# -----------------------------------------------------------------------------

_OPS = {
    "eq": lambda a, b: a == b,
    "ne": lambda a, b: a != b,
    "lt": lambda a, b: a is not None and a < b,
    "le": lambda a, b: a is not None and a <= b,
    "gt": lambda a, b: a is not None and a > b,
    "ge": lambda a, b: a is not None and a >= b,
    "in": lambda a, b: a in b,
    "nin": lambda a, b: a not in b,
}


def _eval(node, profile):
    """Recursively evaluate an eligibility node -> (ok, reason_for_first_failure)."""
    if node is None:
        return True, None
    if "field" in node:
        ok = _OPS[node["op"]](profile.get(node["field"]), node["value"])
        return ok, (None if ok else node.get("label", f"{node['field']} {node['op']} {node['value']}"))
    if "all" in node:
        for child in node["all"]:
            ok, why = _eval(child, profile)
            if not ok:
                return False, why
        return True, None
    if "any" in node:
        reasons = []
        for child in node["any"]:
            ok, why = _eval(child, profile)
            if ok:
                return True, None
            reasons.append(why)
        return False, " or ".join(r for r in reasons if r) or "no option matched"
    if "not" in node:
        ok, _ = _eval(node["not"], profile)
        return (not ok), None
    return True, None


# -----------------------------------------------------------------------------
# Profile derivation
# -----------------------------------------------------------------------------

PROFILE_DEFAULTS = {
    "state": None,
    "delivery_state": None,        # where she delivered (defaults to home state)
    "birth_date": None,
    "delivery_type": "institutional_public",
    "c_section": False,
    "child_number": 1,
    "child_sex": "girl",
    "multiple_birth": 1,           # 1 single, 2 twins, 3 triplets
    "birth_outcome": "live",       # live | stillbirth | neonatal_death
    "maternal_outcome": "alive",   # alive | deceased
    "premature": False,
    "low_birth_weight": False,
    "mother_age_years": 25.0,
    "category": "general",         # general | obc | sc | st
    "bpl": False,
    "single_mother": False,
    "mother_disability": False,
    "child_disability": False,
    "govt_employee": False,
    "has_aadhaar": True,
    "has_bank_account": True,
    "claimed": [],                 # component ids already received/done
    "applied": {},                 # component id -> application date (YYYY-MM-DD)
}

DEATH_DEFAULTS = {
    "state": None,
    "death_date": None,
    "deceased_age_years": 45.0,
    "applicant_age_years": 40.0,
    "applicant_sex": "female",
    "relation_to_deceased": "spouse",
    "was_breadwinner": True,
    "accidental_death": False,
    "deceased_had_bank_account": True,
    "formal_sector": False,
    "construction_worker": False,
    "area": "rural",
    "bpl": False,
    "has_aadhaar": True,
    "has_bank_account": True,
    "claimed": [],
    "applied": {},
}

STUCK_AFTER_DAYS = 45  # applied but unpaid this long -> escalate


def _parse_applied(raw):
    """Accept {'cid': 'YYYY-MM-DD'} or 'cid:YYYY-MM-DD,cid2:...' -> dict."""
    if not raw:
        return {}
    if isinstance(raw, dict):
        return dict(raw)
    out = {}
    for part in str(raw).split(","):
        if ":" in part:
            cid, _, d = part.partition(":")
            out[cid.strip()] = d.strip()
    return out


def _state_index(kb):
    return {s["code"]: s for s in kb.get("states", [])}


class ProfileError(ValueError):
    """Invalid input profile — message is safe to show to the user."""


def _validate(p, kb, life_event="childbirth"):
    codes = set(_state_index(kb))
    if not p.get("state"):
        raise ProfileError("state is required (e.g. state=BR)")
    if p["state"] not in codes:
        raise ProfileError(f"unknown state code '{p['state']}' — use one of the 36 state/UT codes from /api/meta")

    if life_event == "death":
        for field, opts in DEATH_META.items():
            if p.get(field) is not None and p[field] not in opts:
                raise ProfileError(f"invalid {field} '{p[field]}' — expected one of: {', '.join(opts)}")
        try:
            p["deceased_age_years"] = float(p.get("deceased_age_years", 45.0))
            p["applicant_age_years"] = float(p.get("applicant_age_years", 40.0))
        except (TypeError, ValueError):
            raise ProfileError("deceased_age_years and applicant_age_years must be numbers")
        if not 0 <= p["deceased_age_years"] <= 120:
            raise ProfileError("deceased_age_years must be between 0 and 120")
        if not 10 <= p["applicant_age_years"] <= 110:
            raise ProfileError("applicant_age_years must be between 10 and 110")
        return

    if p.get("delivery_state") and p["delivery_state"] not in codes:
        raise ProfileError(f"unknown delivery_state code '{p['delivery_state']}'")
    for field in ("delivery_type", "child_sex", "birth_outcome", "maternal_outcome", "area", "category"):
        if p.get(field) is not None and p[field] not in META[field]:
            raise ProfileError(f"invalid {field} '{p[field]}' — expected one of: {', '.join(META[field])}")
    try:
        p["child_number"] = int(p.get("child_number", 1))
        p["multiple_birth"] = int(p.get("multiple_birth", 1))
        p["mother_age_years"] = float(p.get("mother_age_years", 25.0))
    except (TypeError, ValueError):
        raise ProfileError("child_number and multiple_birth must be whole numbers; mother_age_years a number")
    if not 1 <= p["child_number"] <= 15:
        raise ProfileError("child_number must be between 1 and 15")
    if not 1 <= p["multiple_birth"] <= 4:
        raise ProfileError("multiple_birth must be between 1 (single) and 4")
    if not 10 <= p["mother_age_years"] <= 70:
        raise ProfileError("mother_age_years must be between 10 and 70")


def build_profile(raw, kb, as_of=None, life_event="childbirth"):
    p = dict(DEATH_DEFAULTS if life_event == "death" else PROFILE_DEFAULTS)
    p.update({k: v for k, v in raw.items() if v is not None})
    _validate(p, kb, life_event)

    as_of = as_of or date.today()
    p["future_birth"] = False
    date_field = "death_date" if life_event == "death" else "birth_date"
    if p.get(date_field):
        bd = p[date_field]
        if isinstance(bd, str):
            try:
                bd = datetime.strptime(bd, "%Y-%m-%d").date()
            except ValueError:
                raise ProfileError(f"invalid {date_field} '{p[date_field]}' — expected YYYY-MM-DD")
        delta = (as_of - bd).days
        if delta < 0:
            # A future date: show the plan ahead of the event instead of failing.
            p["future_birth"] = True
            delta = 0
        p["days_since_birth"] = delta  # the event clock — name kept for rule/back-compat
        p[date_field] = bd.isoformat()
    else:
        p["days_since_birth"] = 0
    p["days_since_event"] = p["days_since_birth"]
    p["life_event"] = life_event

    p["delivery_state"] = p.get("delivery_state") or p.get("state")
    sidx = _state_index(kb)
    state = sidx.get(p.get("state"), {})
    delivery_state = sidx.get(p.get("delivery_state"), {})
    # JSY cash follows where she delivered; opt-outs follow her home state.
    p["state_lps"] = bool(delivery_state.get("lps"))
    p["state_name"] = state.get("name", p.get("state"))
    p["is_migrant"] = bool(p.get("delivery_state") and p.get("delivery_state") != p.get("state"))
    p["claimed"] = list(p.get("claimed") or [])
    applied = {}
    for cid, d in _parse_applied(p.get("applied")).items():
        try:
            ad = datetime.strptime(d, "%Y-%m-%d").date()
        except (TypeError, ValueError):
            raise ProfileError(f"invalid applied date for '{cid}' — expected YYYY-MM-DD")
        applied[cid] = {"date": ad.isoformat(), "days_ago": max(0, (as_of - ad).days)}
    p["applied"] = applied
    return p


# -----------------------------------------------------------------------------
# Resolution
# -----------------------------------------------------------------------------

def _component_cash(component, profile):
    if component.get("cash_inr") is not None:
        return int(component["cash_inr"])
    table = component.get("cash_table")
    if table:
        key = f"{profile.get('area', 'rural')}_{'lps' if profile.get('state_lps') else 'hps'}"
        return int(table.get(key, 0))
    return 0


def _map_to_visit(deadline_day, visit_days):
    if deadline_day is None:
        return None
    if deadline_day <= 0:
        return 0
    earlier = [v for v in visit_days if v <= deadline_day]
    return max(earlier) if earlier else visit_days[0]


def load_kb(path=None, life_event="childbirth"):
    if path is None:
        if life_event not in LIFE_EVENTS:
            raise ProfileError(f"unknown life_event '{life_event}' — expected one of: {', '.join(LIFE_EVENTS)}")
        path = os.path.join(DATA_DIR, f"{life_event}_schemes.json")
    with open(path, "r", encoding="utf-8") as f:
        kb = json.load(f)
    if "states" not in kb:  # shared reference data lives in data/states.json
        with open(os.path.join(DATA_DIR, "states.json"), "r", encoding="utf-8") as f:
            kb["states"] = json.load(f)["states"]
    return kb


def _visit_days(kb):
    rail = kb.get("checkpoints") or kb.get("asha_hbnc_visits") or {}
    return rail.get("visit_days", [])


def resolve(raw_profile, kb=None, as_of=None, life_event=None):
    raw_profile = dict(raw_profile)
    life_event = life_event or raw_profile.pop("life_event", None) or "childbirth"
    raw_profile.pop("life_event", None)
    kb = kb or load_kb(life_event=life_event)
    profile = build_profile(raw_profile, kb, as_of, life_event)
    sidx = _state_index(kb)
    state = sidx.get(profile.get("state"), {})
    optout = set(state.get("optout", []))
    visit_days = _visit_days(kb)
    days = profile["days_since_birth"]
    claimed = set(profile["claimed"])

    comp_title = {}            # comp_id -> title, for resolving prerequisites
    for sch in kb["schemes"]:
        for c in sch.get("benefit_components", []):
            comp_title[c["id"]] = c["title"]

    timeline, not_eligible, flagged = [], [], []
    sensitive_mode = (
        life_event == "death"
        or profile.get("birth_outcome") in ("stillbirth", "neonatal_death")
        or profile.get("maternal_outcome") == "deceased"
    )

    for scheme in kb["schemes"]:
        if scheme["id"] in optout:
            not_eligible.append({"scheme_id": scheme["id"], "name": scheme["name"],
                                 "reason": f"{profile.get('state_name')} does not run this central scheme (runs its own)"})
            continue
        ok, reason = _eval(scheme.get("eligibility"), profile)
        if not ok:
            not_eligible.append({"scheme_id": scheme["id"], "name": scheme["name"],
                                 "reason": reason or "eligibility not met"})
            continue

        for comp in scheme.get("benefit_components", []):
            c_ok, _ = _eval(comp.get("eligibility"), profile)
            if not c_ok:
                continue
            cash = _component_cash(comp, profile)
            needs_verify = bool(comp.get("needs_verification") or scheme.get("confidence") == "low")
            done = comp["id"] in claimed
            deadline = comp.get("deadline_day")
            days_remaining = (deadline - days) if deadline is not None else None
            timeline.append({
                "scheme_id": scheme["id"], "scheme_name": scheme["name"], "scheme_name_hi": scheme.get("name_hi"),
                "authority": scheme.get("authority"), "category": scheme.get("category"),
                "component_id": comp["id"], "title": comp["title"], "title_hi": comp.get("title_hi"),
                "phase": comp.get("phase"), "cash_inr": cash,
                "deadline_day": deadline, "days_remaining": days_remaining,
                "asha_visit_day": _map_to_visit(deadline, visit_days),
                "documents": comp.get("documents", []), "depends_on": comp.get("depends_on", []),
                "is_gateway": bool(comp.get("is_gateway")), "sensitive": bool(comp.get("sensitive")),
                "note": comp.get("note"), "confidence": scheme.get("confidence"),
                "needs_verification": needs_verify, "source_urls": scheme.get("source_urls", []),
                "apply_at": scheme.get("apply_at"), "apply_via": scheme.get("apply_via"),
                "grievance": scheme.get("grievance"),
                "done": done, "blocked_by": [], "status": "done" if done else "upcoming",
                "applied_date": None, "days_since_applied": None, "stuck": False,
            })
            if needs_verify and not done:
                flagged.append({"scheme": scheme["name"], "title": comp["title"]})

    # Second pass: blocking + status now that we know what's claimed/applied.
    applied_map = profile["applied"]
    total_cash = remaining_cash = 0
    overdue, urgent, stuck = [], [], []
    for it in timeline:
        total_cash += it["cash_inr"]
        blocked = [comp_title.get(dep.split(".")[-1], dep.split(".")[-1])
                   for dep in it["depends_on"] if dep.split(".")[-1] not in claimed]
        it["blocked_by"] = blocked
        if it["done"]:
            it["status"] = "done"
            continue
        remaining_cash += it["cash_inr"]
        ap = applied_map.get(it["component_id"])
        if ap:
            it["status"] = "applied"
            it["applied_date"] = ap["date"]
            it["days_since_applied"] = ap["days_ago"]
            if ap["days_ago"] >= STUCK_AFTER_DAYS:
                it["stuck"] = True
                stuck.append(it)
            continue
        dr = it["days_remaining"]
        if blocked:
            it["status"] = "blocked"
        elif dr is None:
            it["status"] = "ongoing"
        elif dr < 0:
            it["status"] = "overdue"
            overdue.append(it)
        elif dr <= 3:
            it["status"] = "urgent"
            urgent.append(it)
        else:
            it["status"] = "upcoming"

    timeline.sort(key=lambda x: (x["deadline_day"] is None, x["deadline_day"] or 0))

    by_visit = {}
    for it in timeline:
        key = "ongoing" if it["asha_visit_day"] is None else it["asha_visit_day"]
        by_visit.setdefault(key, []).append(it)

    # Aggregated, de-duplicated document checklist (outstanding items only).
    docs = {}
    for it in timeline:
        if it["done"]:
            continue
        for d in it["documents"]:
            docs.setdefault(d, set()).add(it["scheme_name"])
    documents = [{"document": d, "needed_for": sorted(s)} for d, s in sorted(docs.items())]

    # Cross-cutting alerts.
    alerts = []
    has_cash = any(it["cash_inr"] > 0 for it in timeline if not it["done"])
    if has_cash and not profile.get("has_bank_account"):
        alerts.append({"level": "blocker", "text": "No Aadhaar-linked bank account — required to receive ANY cash. Open/seed one first."})
    if has_cash and not profile.get("has_aadhaar"):
        alerts.append({"level": "blocker", "text": "No Aadhaar — required for most cash transfers. Enrol first."})
    if profile.get("future_birth"):
        alerts.append({"level": "warn", "text": ("Date is in the future — showing the plan ahead of the event."
                       if life_event != "childbirth" else
                       "Birth date is in the future — showing the plan for the expected delivery. Recheck after the birth.")})
    if profile.get("is_migrant"):
        alerts.append({"level": "warn", "text": f"Migrant case: delivered in {sidx.get(profile['delivery_state'], {}).get('name', profile['delivery_state'])} but resident of {profile.get('state_name')}. Claim JSY where she delivered; state schemes follow her home state — check portability."})
    if sensitive_mode:
        alerts.append({"level": "sensitive", "text": "Sensitive case — handle with care. Only entitlements that apply are shown; lead with support, not paperwork."})
    if overdue:
        alerts.append({"level": "overdue", "text": f"{len(overdue)} action(s) past deadline — act today and check if a late process still applies."})
    if stuck:
        alerts.append({"level": "stuck", "text": f"{len(stuck)} application(s) pending over {STUCK_AFTER_DAYS} days without payment — escalate via the grievance channel on the card."})
    if urgent:
        alerts.append({"level": "urgent", "text": f"{len(urgent)} action(s) due within 3 days."})

    upcoming = [it for it in timeline if it["status"] in ("overdue", "urgent", "upcoming", "blocked")]
    next_action = min((it for it in upcoming if it["days_remaining"] is not None),
                      key=lambda x: x["days_remaining"], default=None)

    return {
        "profile": profile,
        "summary": {
            "eligible_count": len(timeline),
            "outstanding_count": sum(1 for it in timeline if not it["done"]),
            "applied_count": sum(1 for it in timeline if it["status"] == "applied"),
            "stuck_count": len(stuck),
            "overdue_count": len(overdue),
            "urgent_count": len(urgent),
            "total_cash_inr": total_cash,
            "remaining_cash_inr": remaining_cash,
            "needs_verification_count": len(flagged),
            "sensitive_mode": sensitive_mode,
        },
        "timeline": timeline,
        "by_asha_visit": by_visit,
        "documents": documents,
        "alerts": alerts,
        "next_action": next_action,
        "not_eligible": not_eligible,
        "flagged_for_verification": flagged,
        "visit_days": visit_days,
        "life_event": life_event,
        "kb_version": kb.get("version"),
        "kb_as_of": kb.get("as_of"),
    }


def work_plan(mothers, kb=None, as_of=None):
    """Aggregate a caseload into the worker's daily plan (any mix of life events).

    mothers: [{"id": ..., "name": ..., "profile": {<flat resolve() profile,
              optionally with life_event>}}]
    Returns entries sorted most-urgent-first with per-case rollups + totals.
    """
    as_of = as_of or date.today()
    kbs = {"childbirth": kb} if kb else {}

    entries, errors = [], []
    totals = {"mothers": 0, "overdue": 0, "urgent": 0, "stuck": 0,
              "visits_today": 0, "remaining_cash_inr": 0}

    for m in mothers:
        mid, name = m.get("id"), m.get("name") or "(unnamed)"
        profile = dict(m.get("profile") or {})
        event = profile.get("life_event") or "childbirth"
        try:
            if event not in kbs:
                kbs[event] = load_kb(life_event=event)
            r = resolve(profile, kb=kbs[event], as_of=as_of, life_event=event)
        except ProfileError as e:
            errors.append({"id": mid, "name": name, "error": str(e)})
            continue
        visit_days = _visit_days(kbs[event])
        s, days = r["summary"], r["profile"]["days_since_birth"]
        visit_today = days in visit_days
        next_visit = next((v for v in visit_days if v >= days), None)
        top = [{"title": it["title"], "status": it["status"], "cash_inr": it["cash_inr"],
                "deadline_day": it["deadline_day"]}
               for it in r["timeline"]
               if it["status"] in ("overdue", "urgent", "stuck", "upcoming", "applied")][:3]
        score = (s["overdue_count"] * 100 + s["stuck_count"] * 60 + s["urgent_count"] * 30
                 + (20 if visit_today else 0) + (1 if s["remaining_cash_inr"] else 0))
        entries.append({
            "id": mid, "name": name, "event": event, "day": days, "visit_today": visit_today,
            "next_visit_day": next_visit, "hbnc_complete": next_visit is None,
            "overdue": s["overdue_count"], "urgent": s["urgent_count"],
            "stuck": s["stuck_count"], "applied": s["applied_count"],
            "outstanding": s["outstanding_count"],
            "remaining_cash_inr": s["remaining_cash_inr"],
            "sensitive": s["sensitive_mode"], "top_actions": top, "score": score,
        })
        totals["mothers"] += 1
        totals["overdue"] += s["overdue_count"]
        totals["urgent"] += s["urgent_count"]
        totals["stuck"] += s["stuck_count"]
        totals["visits_today"] += 1 if visit_today else 0
        totals["remaining_cash_inr"] += s["remaining_cash_inr"]

    entries.sort(key=lambda e: -e["score"])
    return {"as_of": as_of.isoformat(), "plan": entries, "errors": errors, "totals": totals}


def meta(kb=None):
    kb = kb or load_kb()
    death_kb = load_kb(life_event="death")
    return {
        "states": kb.get("states", []),
        "visit_days": _visit_days(kb),
        "options": META,
        "life_events": LIFE_EVENTS,
        "events": {
            "childbirth": {"options": META, "visit_days": _visit_days(kb),
                           "kb_version": kb.get("version")},
            "death": {"options": DEATH_META, "visit_days": _visit_days(death_kb),
                      "kb_version": death_kb.get("version")},
        },
        "kb_version": kb.get("version"),
        "kb_as_of": kb.get("as_of"),
    }


# -----------------------------------------------------------------------------
# CLI report
# -----------------------------------------------------------------------------

def _rupees(n):
    return "Rs " + format(int(n), ",d")


def print_report(result):
    p, s = result["profile"], result["summary"]
    print("=" * 66)
    print("  SAHEJ — benefits owed to this mother & newborn")
    print("=" * 66)
    print(f"  {p.get('state_name')} ({'LPS' if p.get('state_lps') else 'HPS'}) · {p.get('area')} · {p.get('delivery_type')}")
    print(f"  Child #{p.get('child_number')} ({p.get('child_sex')}) · outcome: {p.get('birth_outcome')} · "
          f"mother: {p.get('maternal_outcome')}, age {p.get('mother_age_years')}")
    print(f"  Born {p.get('birth_date')} (day {p.get('days_since_birth')})")
    print("-" * 66)
    print(f"  {s['outstanding_count']} actions outstanding · remaining cash {_rupees(s['remaining_cash_inr'])}"
          f" (of {_rupees(s['total_cash_inr'])})")
    for a in result["alerts"]:
        print(f"  [{a['level'].upper()}] {a['text']}")
    print("=" * 66)

    labels = {0: "At delivery / day 0", "ongoing": "Ongoing (no hard date)"}
    for v in result["visit_days"]:
        labels[v] = f"ASHA visit — day {v}"
    for key in [0] + result["visit_days"] + ["ongoing"]:
        items = result["by_asha_visit"].get(key)
        if not items:
            continue
        print(f"\n  ▸ {labels.get(key, key)}")
        for it in items:
            tags = (["DONE"] if it["done"] else []) + [it["status"].upper()]
            if it["is_gateway"]:
                tags.append("GATEWAY")
            if it["needs_verification"]:
                tags.append("VERIFY")
            cash = f"  [{_rupees(it['cash_inr'])}]" if it["cash_inr"] else ""
            mark = "x" if it["done"] else " "
            print(f"      [{mark}] {it['title']}{cash}  {' '.join(tags)}")
            if it["blocked_by"]:
                print(f"          ↳ do first: {', '.join(it['blocked_by'])}")
            if it["documents"]:
                print(f"          docs: {', '.join(it['documents'])}")

    if result["documents"]:
        print("\n  ▸ Documents to gather:")
        for d in result["documents"]:
            print(f"      - {d['document']}  (for: {', '.join(d['needed_for'])})")
    if result["not_eligible"]:
        print("\n  Not eligible (and why):")
        for ne in result["not_eligible"]:
            print(f"      - {ne['name']}: {ne['reason']}")
    print()


def _csv(v):
    return [x for x in v.split(",") if x] if v else []


def main():
    ap = argparse.ArgumentParser(description="Sahej childbirth benefit resolver")
    ap.add_argument("--state", default="BR")
    ap.add_argument("--delivery-state", dest="delivery_state", default=None)
    ap.add_argument("--birth-date", dest="birth_date", default=None)
    ap.add_argument("--asof", default=None)
    ap.add_argument("--delivery", dest="delivery_type", default="institutional_public", choices=META["delivery_type"])
    ap.add_argument("--c-section", dest="c_section", action="store_true")
    ap.add_argument("--child-number", dest="child_number", type=int, default=1)
    ap.add_argument("--child-sex", dest="child_sex", default="girl", choices=META["child_sex"])
    ap.add_argument("--multiple-birth", dest="multiple_birth", type=int, default=1)
    ap.add_argument("--birth-outcome", dest="birth_outcome", default="live", choices=META["birth_outcome"])
    ap.add_argument("--maternal-outcome", dest="maternal_outcome", default="alive", choices=META["maternal_outcome"])
    ap.add_argument("--premature", action="store_true")
    ap.add_argument("--low-birth-weight", dest="low_birth_weight", action="store_true")
    ap.add_argument("--mother-age", dest="mother_age_years", type=float, default=25.0)
    ap.add_argument("--area", default="rural", choices=META["area"])
    ap.add_argument("--category", default="general", choices=META["category"])
    ap.add_argument("--bpl", action="store_true")
    ap.add_argument("--single-mother", dest="single_mother", action="store_true")
    ap.add_argument("--mother-disability", dest="mother_disability", action="store_true")
    ap.add_argument("--child-disability", dest="child_disability", action="store_true")
    ap.add_argument("--govt-employee", dest="govt_employee", action="store_true")
    ap.add_argument("--no-aadhaar", dest="has_aadhaar", action="store_false")
    ap.add_argument("--no-bank", dest="has_bank_account", action="store_false")
    ap.add_argument("--claimed", default="", help="comma-separated component ids already received")
    ap.add_argument("--applied", default="", help="cid:YYYY-MM-DD,... application dates")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    raw = {k: v for k, v in vars(args).items() if k not in ("asof", "json", "claimed", "applied")}
    raw["claimed"] = _csv(args.claimed)
    raw["applied"] = args.applied
    as_of = datetime.strptime(args.asof, "%Y-%m-%d").date() if args.asof else None
    result = resolve(raw, as_of=as_of)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print_report(result)


if __name__ == "__main__":
    main()
