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

    passed = sum(1 for _, ok_ in checks if ok_)
    for name, ok_ in checks:
        print(f"  [{'PASS' if ok_ else 'FAIL'}] {name}")
    print(f"\n{passed}/{len(checks)} checks passed.")
    os.unlink(_tmp.name)
    return passed == len(checks)


if __name__ == "__main__":
    import sys
    sys.exit(0 if run() else 1)
