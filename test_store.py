#!/usr/bin/env python3
"""Tests for store.py (accounts, sessions, caseload sync). Run: python3 test_store.py"""
import os
import tempfile

_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp.close()
os.environ["SAHEJ_DB"] = _tmp.name

import store  # noqa: E402 — must come after SAHEJ_DB is set
from store import (StoreError, create_worker, verify_login, create_session,  # noqa: E402
                   get_session, delete_session, sync_cases, get_case_by_share,
                   normalize_phone)


def run():
    checks = []

    def chk(name, cond):
        checks.append((name, bool(cond)))

    def raises(fn, msg_part):
        try:
            fn()
            return False
        except StoreError as e:
            return msg_part in str(e)

    # Phone normalization
    chk("phone: +91 stripped", normalize_phone("+91 98765 43210") == "9876543210")
    chk("phone: leading 0 stripped", normalize_phone("09876543210") == "9876543210")
    chk("phone: bad number rejected", raises(lambda: normalize_phone("12345"), "10-digit"))

    # Registration
    w = create_worker("9876543210", "Asha Devi", "1234")
    chk("register works", w["phone"] == "9876543210" and w["name"] == "Asha Devi")
    chk("duplicate phone rejected", raises(lambda: create_worker("9876543210", "X", "9999"), "already registered"))
    chk("bad PIN rejected", raises(lambda: create_worker("9123456789", "Y", "abc"), "4-8 digits"))
    chk("empty name rejected", raises(lambda: create_worker("9123456789", "  ", "1234"), "name"))

    # Login
    ok = verify_login("9876543210", "1234")
    chk("login ok returns worker", ok["name"] == "Asha Devi" and ok["id"])
    chk("wrong PIN rejected", raises(lambda: verify_login("9876543210", "0000"), "wrong PIN"))
    chk("unknown phone rejected", raises(lambda: verify_login("9111111111", "1234"), "register first"))

    # Lockout after repeated failures
    create_worker("9222222222", "Lockme", "5678")
    for _ in range(store.MAX_FAILED - 1):
        try:
            verify_login("9222222222", "0000")
        except StoreError:
            pass
    chk("lockout kicks in", raises(lambda: verify_login("9222222222", "0000"), "wrong PIN")
        and raises(lambda: verify_login("9222222222", "5678"), "try again in"))

    # Sessions
    tok = create_session(ok["id"])
    sess = get_session(tok)
    chk("session round-trips", sess and sess["id"] == ok["id"] and sess["name"] == "Asha Devi")
    chk("bogus token -> None", get_session("nope") is None)
    delete_session(tok)
    chk("logout kills session", get_session(tok) is None)

    # Sync: insert, LWW update, tombstone delete, share token
    wid = ok["id"]
    r = sync_cases(wid, [
        {"id": "m1", "name": "Sunita", "profile": {"state": "BR"}, "updated_at": 1000},
        {"id": "m2", "name": "Radha", "profile": {"state": "UP"}, "updated_at": 2000},
    ])
    chk("sync inserts cases", {c["id"] for c in r["cases"]} == {"m1", "m2"})
    share = next(c["share"] for c in r["cases"] if c["id"] == "m1")
    chk("share token assigned", len(share) >= 8)

    r = sync_cases(wid, [{"id": "m1", "name": "Sunita Kumari", "profile": {"state": "BR", "bpl": True},
                          "updated_at": 1500}])
    m1 = next(c for c in r["cases"] if c["id"] == "m1")
    chk("newer client edit wins", m1["name"] == "Sunita Kumari" and m1["profile"]["bpl"] is True)
    chk("share token stable across updates", m1["share"] == share)

    r = sync_cases(wid, [{"id": "m1", "name": "Old Phone", "profile": {"state": "BR"}, "updated_at": 500}])
    m1 = next(c for c in r["cases"] if c["id"] == "m1")
    chk("older client edit loses", m1["name"] == "Sunita Kumari")

    r = sync_cases(wid, [], deleted=[{"id": "m2", "updated_at": 3000}])
    chk("tombstone deletes case", "m2" in r["deleted"] and all(c["id"] != "m2" for c in r["cases"]))
    r = sync_cases(wid, [{"id": "m2", "name": "Radha", "profile": {"state": "UP"}, "updated_at": 4000}])
    chk("newer re-add undeletes", any(c["id"] == "m2" for c in r["cases"]))

    chk("bad case id rejected", raises(lambda: sync_cases(wid, [{"id": "../x", "profile": {}}]), "case id"))
    chk("non-dict profile rejected", raises(lambda: sync_cases(wid, [{"id": "ok1", "profile": "x"}]), "profile"))
    chk("oversize caseload rejected",
        raises(lambda: sync_cases(wid, [{}] * (store.MAX_CASES + 1)), "at most"))

    # Share lookup (mother page)
    got = get_case_by_share(share)
    chk("share lookup returns case", got and got["name"] == "Sunita Kumari" and got["profile"]["state"] == "BR")
    chk("bogus share -> None", get_case_by_share("AAAAAAAAAAAA") is None)
    chk("malformed share -> None", get_case_by_share("<script>") is None)
    sync_cases(wid, [], deleted=[{"id": "m1", "updated_at": 9000}])
    chk("deleted case unreachable via share", get_case_by_share(share) is None)

    # Cross-worker isolation
    w2 = create_worker("9333333333", "Other", "4321")
    ok2 = verify_login("9333333333", "4321")
    r2 = sync_cases(ok2["id"], [])
    chk("workers see only their own cases", r2["cases"] == [])

    # Content store: catalog + reference docs (the DB-backed catalog path)
    chk("content_ready False before seeding", store.content_ready() is False)
    n = store.replace_schemes([("s1", {"id": "s1", "name": "One"}, "catalog"),
                               ("s2", {"id": "s2", "name": "Two"}, "catalog")])
    chk("replace_schemes returns count", n == 2)
    chk("content_ready True after seeding", store.content_ready() is True)
    chk("all_schemes returns docs", sorted(s["id"] for s in store.all_schemes()) == ["s1", "s2"])
    chk("get_scheme by id", store.get_scheme("s2")["name"] == "Two")
    chk("get_scheme missing -> None", store.get_scheme("nope") is None)
    store.upsert_reference("states", {"states": [{"code": "BR", "name": "Bihar"}]})
    chk("reference roundtrip", store.get_reference("states")["states"][0]["code"] == "BR")
    store.upsert_reference("states", {"states": []})  # upsert overwrites
    chk("reference upsert overwrites", store.get_reference("states")["states"] == [])
    ready, docs = store.get_references(["states", "does_not_exist"])
    chk("get_references reports ready", ready is True)
    chk("get_references returns doc for known name", docs["states"]["states"] == [])
    chk("get_references returns None for missing name", docs["does_not_exist"] is None)
    store.upsert_reference("does_not_exist", {"x": 1})
    _, docs2 = store.get_references(["does_not_exist"])
    chk("get_references sees fresh write after invalidation", docs2["does_not_exist"]["x"] == 1)
    chk("replace_schemes replaces, not appends",
        store.replace_schemes([("only", {"id": "only"}, "catalog")]) == 1 and len(store.all_schemes()) == 1)

    # Leads: consumer mobile capture
    chk("create_lead normalizes phone", store.create_lead("+91 98765 00011", scheme_id="pm_kisan")["mobile"] == "9876500011")
    chk("create_lead rejects bad mobile", raises(lambda: store.create_lead("12345"), "10-digit"))

    # Consumer OTP login (passwordless)
    cm = "9812300045"
    otp = store.request_otp(cm)
    chk("request_otp returns 6-digit code", otp["code"].isdigit() and len(otp["code"]) == 6)
    chk("verify_otp wrong code rejected", raises(lambda: store.verify_otp(cm, "000000" if otp["code"] != "000000" else "111111"), "wrong code"))
    chk("request_otp rate-limited within window", raises(lambda: store.request_otp(cm), "wait"))
    con = store.create_lead(cm, scheme_id="ayushman")  # a lead to be verified on login
    cons = store.verify_otp(cm, otp["code"])
    chk("verify_otp correct -> consumer", cons["mobile"] == cm and cons["id"] > 0)
    chk("verify_otp burns the code", raises(lambda: store.verify_otp(cm, otp["code"]), "expired"))
    tok = store.create_consumer_session(cons["id"])
    chk("consumer session roundtrip", store.get_consumer_session(tok)["mobile"] == cm)
    store.set_consumer_name(cons["id"], "Meena")
    chk("consumer name persists", store.get_consumer_session(tok)["name"] == "Meena")
    store.delete_consumer_session(tok)
    chk("consumer logout clears session", store.get_consumer_session(tok) is None)
    otp2 = store.request_otp("9812300099")
    cons2 = store.verify_otp("9812300099", otp2["code"])
    chk("distinct number -> distinct consumer", cons2["id"] != cons["id"])

    # Consumer registration without OTP (mobile-only data-collection gate)
    rc = store.register_consumer("+91 98123 00077", name="Asha")
    chk("register_consumer normalizes phone + keeps name", rc["mobile"] == "9812300077" and rc["name"] == "Asha")
    rc2 = store.register_consumer("9812300077")
    chk("register_consumer on repeat visit keeps existing name", rc2["name"] == "Asha" and rc2["id"] == rc["id"])
    rc3 = store.register_consumer("9812300088")
    chk("register_consumer with no name -> empty name", rc3["name"] == "")
    rtok = store.create_consumer_session(rc["id"])
    chk("register_consumer session roundtrips", store.get_consumer_session(rtok)["mobile"] == "9812300077")

    # Consumer profile facets (For You wizard answers) persist server-side
    rc4 = store.register_consumer("9812300111", name="Meena", profile={
        "state": "BR", "age": "45", "gender": "female", "category": "obc",
        "occupation": "farmer", "bpl": True, "disability": False, "rural": True,
        "junk_field": "should be dropped"})
    chk("register_consumer stores whitelisted profile facets",
        rc4["profile"]["state"] == "BR" and rc4["profile"]["gender"] == "female"
        and rc4["profile"]["category"] == "obc" and rc4["profile"]["occupation"] == "farmer"
        and rc4["profile"]["bpl"] is True and rc4["profile"]["rural"] is True)
    chk("register_consumer coerces age to int", rc4["profile"]["age"] == 45)
    chk("register_consumer drops unknown fields", "junk_field" not in rc4["profile"])
    chk("register_consumer keeps explicit false flags (a real answer, not absence)",
        rc4["profile"]["disability"] is False)
    # a later visit with only a name should not erase the previously stored facets
    rc5 = store.register_consumer("9812300111", name="")
    chk("register_consumer merges: later call without profile keeps earlier facets",
        rc5["profile"]["state"] == "BR" and rc5["profile"]["occupation"] == "farmer")
    rc6 = store.register_consumer("9812300111", profile={"age": "46"})
    chk("register_consumer merges: new facet added, old facets retained",
        rc6["profile"]["age"] == 46 and rc6["profile"]["state"] == "BR")

    passed = sum(1 for _, ok_ in checks if ok_)
    for name, ok_ in checks:
        print(f"  [{'PASS' if ok_ else 'FAIL'}] {name}")
    print(f"\n{passed}/{len(checks)} checks passed.")
    os.unlink(_tmp.name)
    return passed == len(checks)


if __name__ == "__main__":
    import sys
    sys.exit(0 if run() else 1)
