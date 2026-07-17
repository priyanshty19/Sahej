#!/usr/bin/env python3
"""
Sahej scheme catalog — the marketplace layer over data/catalog.json.

Pure stdlib. Loads the curated catalog, augments it with cards derived from the
life-event knowledge bases (childbirth/death) so the marketplace and the
caseworker engine stay one source of truth, and answers faceted queries:

    search(filters) -> ranked scheme cards
    get(scheme_id)  -> full scheme entry
    facet_meta()    -> available filter values for the UI

Filter semantics (all optional; a scheme matches if it does not conflict):
    state     two-letter code — central schemes always match; state schemes
              only in their state
    gender    female|male
    age       years (int) — checked against facets.age_min/age_max
    category  general|obc|sc|st
    religion  hindu|muslim|christian|sikh|buddhist|jain|parsi|other
    bpl       true — include BPL-gated schemes; false/absent hides nothing
              (a non-BPL user still sees them marked, but bpl=false with
              strict=true excludes income-gated schemes)
    income    annual family income in ₹ — excludes schemes whose income_cap
              is below it
    occupation farmer|student|construction_worker|street_vendor|artisan|
              unorganised|fisher|dairy|domestic_worker|gig_worker
    residence rural|urban
    disability true
    life_event childbirth|death
    q         free-text search over names/summaries/tags
    benefit_type cash|pension|insurance|loan|scholarship|housing|in_kind|
              document|service|savings
    include_drafts  include needs_verification bulk-draft entries (status=draft)
"""
import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
CATALOG_PATH = os.path.join(HERE, "data", "catalog.json")

_ANY = ("any",)

try:
    import store  # optional DB backend; when seeded, the catalog loads from the DB
except Exception:  # noqa: BLE001 — catalog must run even if the store can't import
    store = None


def _load_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _db_ready():
    try:
        return store is not None and store.content_ready()
    except Exception:  # noqa: BLE001 — DB optional; fall back to bundled JSON
        return False


def load_catalog(path=None):
    """The marketplace catalog: curated schemes plus life-event cards.

    Reads from the database when it has been seeded (data lives in Supabase),
    otherwise from the bundled JSON — so CI and offline dev work unconfigured.
    An explicit `path` always forces the JSON file (used by tools/tests).
    """
    prefetched = None
    if path is None and _db_ready():
        # One connection for catalog_meta + both life-event KBs, instead of
        # three separate get_reference() calls each paying their own
        # connection handshake against the remote DB — see get_references().
        try:
            _, prefetched = store.get_references(["catalog_meta", "childbirth_schemes", "death_schemes"])
        except Exception:  # noqa: BLE001 — DB optional; fall back to JSON
            prefetched = None
        meta = (prefetched or {}).get("catalog_meta") or {}
        cat = {"version": meta.get("version"), "as_of": meta.get("as_of"),
               "schemes": store.all_schemes(source="catalog")}
    else:
        cat = _load_json(path or CATALOG_PATH)
        cat["schemes"] = list(cat["schemes"])
    cat["schemes"] = cat["schemes"] + _derived_from_kbs(prefetched)
    return cat


def _derived_from_kbs(prefetched=None):
    """Marketplace cards for life-event KB schemes not already in the catalog.

    The childbirth/death KBs remain the source of truth for the caseworker
    engine; here we surface them as browsable cards that deep-link to /asha.
    `prefetched` (name -> doc) lets load_catalog() hand over data it already
    fetched in one connection, instead of this reaching the DB again per name.
    """
    cards = []
    seen = {"pmmvy", "nfbs", "widow_pension"}  # already curated in catalog.json
    use_db = prefetched is not None or _db_ready()
    for event, fname in (("childbirth", "childbirth_schemes.json"),
                         ("death", "death_schemes.json")):
        kb = None
        if use_db:
            kb = prefetched.get(f"{event}_schemes") if prefetched is not None else store.get_reference(f"{event}_schemes")
        if kb is None:
            try:
                kb = _load_json(os.path.join(HERE, "data", fname))
            except OSError:
                continue
        for s in kb.get("schemes", []):
            if s["id"] in seen or s.get("kind") == "state_example":
                continue
            seen.add(s["id"])  # same scheme may appear in both KBs (e.g. death registration)
            cash = sum(c.get("cash_inr") or 0 for c in s.get("components", []))
            cards.append({
                "id": "le_" + s["id"],
                "name": s.get("name", s["id"]),
                "name_hi": s.get("name_hi", ""),
                "level": "central",
                "summary": s.get("summary") or s.get("why") or s.get("name", s["id"]),
                "summary_hi": s.get("summary_hi", ""),
                "benefit": {"type": "cash" if cash else "service",
                            **({"amount_inr": cash} if cash else {})},
                "facets": {"life_event": event,
                           "tags": [event, "family"]},
                "apply": {"at": s.get("apply_at", ""), "url": (s.get("source_urls") or [""])[0]},
                "documents": [], "grievance": s.get("grievance", ""),
                "status": "active",
                "confidence": s.get("confidence", "medium"),
                "needs_verification": bool(s.get("needs_verification")),
                "source_urls": s.get("source_urls") or ["https://www.myscheme.gov.in"],
                "deep_link": "/asha",
            })
    return cards


def _norm(v):
    return str(v or "").strip().lower()


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _list_match(facet_val, wanted):
    """facet list absent or containing 'any' -> no restriction."""
    if not facet_val:
        return True
    vals = [str(x).lower() for x in facet_val]
    if "any" in vals:
        return True
    return wanted in vals


def matches(scheme, f):
    """Does this scheme survive the given filters? Missing profile fields never
    exclude a scheme (browse-friendly); provided fields must not conflict."""
    fc = scheme.get("facets", {})

    state = _norm(f.get("state"))
    if state and scheme.get("level") == "state" and _norm(scheme.get("state")) != state:
        return False

    gender = _norm(f.get("gender"))
    if gender and not _list_match(fc.get("gender"), gender):
        # stand-up-india style either/or facets: category OR gender qualifies
        if fc.get("facet_logic") != "category_or_gender":
            return False

    age = _num(f.get("age"))
    if age is not None:
        if fc.get("age_min") is not None and age < fc["age_min"]:
            return False
        if fc.get("age_max") is not None and age > fc["age_max"]:
            return False

    category = _norm(f.get("category"))
    if category and fc.get("category"):
        cat_ok = _list_match(fc.get("category"), category)
        if fc.get("facet_logic") == "category_or_gender":
            if not cat_ok and not (gender and _list_match(fc.get("gender"), gender)):
                return False
        elif not cat_ok:
            return False

    religion = _norm(f.get("religion"))
    if religion and not _list_match(fc.get("religion"), religion):
        return False

    occupation = _norm(f.get("occupation"))
    if occupation and fc.get("occupation") and not _list_match(fc.get("occupation"), occupation):
        return False

    residence = _norm(f.get("residence"))
    if residence and fc.get("residence") and not _list_match(fc.get("residence"), residence):
        return False

    disability = _norm(f.get("disability")) in ("true", "1", "yes")
    if fc.get("disability") is True and f.get("disability") is not None and not disability:
        return False

    life_event = _norm(f.get("life_event"))
    if life_event and _norm(fc.get("life_event")) not in ("", life_event):
        return False

    btype = _norm(f.get("benefit_type"))
    if btype and _norm(scheme.get("benefit", {}).get("type")) != btype:
        return False

    # income gates: strict exclusion only when the user supplied the field
    income = _num(f.get("income"))
    gate = fc.get("income")
    if income is not None and gate == "income_cap" and fc.get("income_cap_inr"):
        if income > fc["income_cap_inr"]:
            return False
    bpl = f.get("bpl")
    if bpl is not None and _norm(bpl) in ("false", "0", "no") and gate in ("bpl",):
        return False

    q = _norm(f.get("q"))
    if q:
        hay = " ".join([scheme.get("name", ""), scheme.get("name_hi", ""),
                        scheme.get("summary", ""), scheme.get("summary_hi", ""),
                        scheme.get("ministry", ""),
                        " ".join(fc.get("tags", []))]).lower()
        if not all(tok in hay for tok in q.split()):
            return False

    return True


def _annual_value(s):
    b = s.get("benefit", {})
    amt = b.get("amount_inr") or 0
    if b.get("period") == "month":
        return amt * 12
    return amt


def _card(s):
    """Compact card for list views."""
    return {k: s.get(k) for k in
            ("id", "name", "name_hi", "level", "state", "summary", "summary_hi",
             "benefit", "status", "needs_verification", "confidence", "deep_link")
            if s.get(k) is not None} | {"tags": s.get("facets", {}).get("tags", [])}


def search(filters=None, catalog=None, limit=100):
    f = filters or {}
    cat = catalog or load_catalog()
    include_drafts = _norm(f.get("include_drafts")) in ("true", "1", "yes")
    out = []
    for s in cat["schemes"]:
        if s.get("status") == "draft" and not include_drafts:
            continue
        if matches(s, f):
            out.append(s)
    out.sort(key=lambda s: (s.get("status") != "new", -_annual_value(s), s["name"]))
    total = len(out)
    return {"total": total, "version": cat.get("version"), "as_of": cat.get("as_of"),
            "schemes": [_card(s) for s in out[:max(1, min(int(limit or 100), 200))]]}


def get(scheme_id, catalog=None):
    cat = catalog or load_catalog()
    for s in cat["schemes"]:
        if s["id"] == scheme_id:
            return s
    return None


def facet_meta(catalog=None):
    cat = catalog or load_catalog()
    tags, btypes, occupations = set(), set(), set()
    n_draft = 0
    for s in cat["schemes"]:
        if s.get("status") == "draft":
            n_draft += 1
        tags.update(s.get("facets", {}).get("tags", []))
        occupations.update(x for x in s.get("facets", {}).get("occupation", []) if x != "any")
        if s.get("benefit", {}).get("type"):
            btypes.add(s["benefit"]["type"])
    return {
        "version": cat.get("version"), "as_of": cat.get("as_of"),
        "total": len(cat["schemes"]), "drafts": n_draft,
        "benefit_types": sorted(btypes), "tags": sorted(tags),
        "occupations": sorted(occupations),
        "categories": ["general", "obc", "sc", "st"],
        "religions": ["hindu", "muslim", "christian", "sikh", "buddhist", "jain", "parsi", "other"],
        "genders": ["female", "male"],
        "residences": ["rural", "urban"],
        "life_events": ["childbirth", "death"],
    }


if __name__ == "__main__":
    import sys
    f = {}
    for a in sys.argv[1:]:
        if "=" in a:
            k, v = a.split("=", 1)
            f[k] = v
    r = search(f)
    print(f"{r['total']} schemes (catalog v{r['version']})")
    for s in r["schemes"]:
        b = s.get("benefit", {})
        amt = f" ₹{b['amount_inr']:,}" if b.get("amount_inr") else ""
        print(f"  - {s['name']}{amt} [{', '.join(s.get('tags', []))}]"
              + (" ⚠verify" if s.get("needs_verification") else ""))
