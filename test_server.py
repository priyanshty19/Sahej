#!/usr/bin/env python3
"""HTTP-level tests for serve.py. Run: python3 test_server.py

Boots the real ThreadingHTTPServer in-process on an ephemeral port and hits it
with urllib — no dependencies, exercises routing, validation mapping, static
serving, security headers, and traversal protection end-to-end.
"""
import json
import os
import tempfile
import threading
import urllib.request
import urllib.error
from http.server import ThreadingHTTPServer

_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp.close()
os.environ["SAHEJ_DB"] = _tmp.name

from serve import Handler  # noqa: E402 — must come after SAHEJ_DB is set


def start_server():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


def get(base, path, method="GET"):
    req = urllib.request.Request(base + path, method=method)
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


def post(base, path, payload, cookie=None):
    data = payload if isinstance(payload, bytes) else json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if cookie:
        headers["Cookie"] = cookie
    req = urllib.request.Request(base + path, data=data, method="POST", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


def get_c(base, path, cookie=None):
    headers = {"Cookie": cookie} if cookie else {}
    req = urllib.request.Request(base + path, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def run():
    srv, base = start_server()
    checks = []

    def chk(name, cond):
        checks.append((name, bool(cond)))

    try:
        # Pages
        code, hdrs, body = get(base, "/")
        chk("GET / -> 200 explore marketplace", code == 200 and b"Sahej" in body
            and b"api/schemes" in body)
        chk("/ is html", hdrs.get("Content-Type", "").startswith("text/html"))
        chk("/ has CSP header", "Content-Security-Policy" in hdrs)
        chk("/ has nosniff", hdrs.get("X-Content-Type-Options") == "nosniff")
        chk("/ has frame deny", hdrs.get("X-Frame-Options") == "DENY")

        code, _, body = get(base, "/app")
        chk("GET /app -> 200 tool", code == 200 and b"caseload" in body.lower())

        code, _, body = get(base, "/app", method="HEAD")
        chk("HEAD /app -> 200 empty body", code == 200 and body == b"")

        code, _, body = get(base, "/asha")
        chk("GET /asha -> 200 same tool", code == 200 and b"caseload" in body.lower())

        code, _, body = get(base, "/about")
        chk("GET /about -> 200 story page", code == 200 and b"Sahej" in body)

        # Marketplace API
        code, _, body = get(base, "/api/schemes")
        r = json.loads(body)
        chk("GET /api/schemes -> 50+ cards", code == 200 and r["total"] >= 50)

        code, _, body = get(base, "/api/schemes?occupation=farmer&state=MP")
        r = json.loads(body)
        chk("schemes filter via query", code == 200
            and any(s["id"] == "pm_kisan" for s in r["schemes"])
            and all(s["id"] != "lakshmir_bhandar_wb" for s in r["schemes"]))

        code, _, body = get(base, "/api/scheme/pm_kisan")
        chk("scheme detail route", code == 200 and json.loads(body)["apply"]["url"])
        code, _, _ = get(base, "/api/scheme/nope_xyz")
        chk("unknown scheme -> 404", code == 404)

        code, _, body = get(base, "/api/facets")
        chk("facets route", code == 200 and "farmer" in json.loads(body)["occupations"])

        code, _, body = get(base, "/scheme/pm_kisan")
        chk("GET /scheme/<id> serves explore shell", code == 200 and b"api/schemes" in body)

        # API
        code, _, body = get(base, "/api/meta")
        meta = json.loads(body)
        chk("GET /api/meta -> 200, 36 states", code == 200 and len(meta["states"]) == 36)

        code, _, body = get(base, "/api/resolve?state=BR&birth_date=2026-06-01&child_number=1&child_sex=girl&mother_age_years=24&area=rural")
        r = json.loads(body)
        chk("resolve valid -> 200 with summary", code == 200 and r["summary"]["eligible_count"] > 0)
        chk("resolve returns timeline+documents+alerts",
            all(k in r for k in ("timeline", "documents", "alerts", "by_asha_visit")))

        code, _, body = get(base, "/api/resolve?state=XX")
        chk("unknown state -> 400 with clear error",
            code == 400 and "unknown state code" in json.loads(body)["error"])

        code, _, body = get(base, "/api/resolve?state=BR&birth_date=junk")
        chk("bad date -> 400", code == 400 and "YYYY-MM-DD" in json.loads(body)["error"])

        code, _, body = get(base, "/api/resolve?state=BR&child_number=99")
        chk("out-of-range child_number -> 400", code == 400)

        code, _, _ = get(base, "/healthz")
        chk("healthz -> 200", code == 200)

        # Applied lifecycle over the GET API
        code, _, body = get(base, "/api/resolve?state=BR&birth_date=2026-05-01&applied=jsy_delivery_cash:2026-05-10")
        r = json.loads(body)
        chk("resolve with applied param -> applied_count",
            code == 200 and r["summary"]["applied_count"] == 1)

        # POST /api/plan
        code, _, body = post(base, "/api/plan", {"mothers": [
            {"id": "a", "name": "Sunita", "profile": {"state": "BR", "birth_date": "2026-06-30"}},
            {"id": "b", "name": "Broken", "profile": {"state": "XX"}},
        ]})
        pl = json.loads(body)
        chk("POST /api/plan -> 200 with plan+totals",
            code == 200 and len(pl["plan"]) == 1 and pl["totals"]["mothers"] == 1)
        chk("plan isolates invalid mothers as errors", len(pl["errors"]) == 1)

        code, _, body = post(base, "/api/plan", b"not json{")
        chk("plan invalid JSON -> 400", code == 400 and "JSON" in json.loads(body)["error"])

        code, _, body = post(base, "/api/plan", {"mothers": [{}] * 201})
        chk("plan >200 mothers -> 400", code == 400)

        code, _, body = post(base, "/nope", {"x": 1})
        chk("POST unknown route -> 404", code == 404)

        # Death life event over the API
        code, _, body = get(base, "/api/resolve?life_event=death&state=BR&death_date=2026-06-20&bpl=true&deceased_age_years=42&applicant_age_years=45")
        r = json.loads(body)
        chk("resolve life_event=death -> survivor items",
            code == 200 and r["life_event"] == "death" and r["summary"]["sensitive_mode"]
            and any(it["component_id"] == "register_death" for it in r["timeline"]))

        code, _, body = get(base, "/api/resolve?life_event=wedding&state=BR")
        chk("unknown life_event -> 400", code == 400 and "life_event" in json.loads(body)["error"])

        code, _, body = get(base, "/api/meta")
        m2 = json.loads(body)
        chk("meta lists both life events", set(m2.get("life_events", [])) == {"childbirth", "death"})

        code, _, body = post(base, "/api/plan", {"mothers": [
            {"id": "a", "name": "Birth", "profile": {"state": "BR", "birth_date": "2026-06-30"}},
            {"id": "b", "name": "Death", "profile": {"life_event": "death", "state": "UP",
                                                     "death_date": "2026-06-20", "bpl": True}},
        ]})
        pl = json.loads(body)
        chk("plan handles mixed life events",
            code == 200 and {e["event"] for e in pl["plan"]} == {"childbirth", "death"})

        # Accounts: register -> cookie -> me -> sync -> mother page
        code, hdrs, body = post(base, "/api/register",
                                {"phone": "9876501234", "name": "Asha Devi", "pin": "1234"})
        cookie = (hdrs.get("Set-Cookie") or "").split(";")[0]
        chk("register -> 200 + session cookie",
            code == 200 and cookie.startswith("sahej=") and len(cookie) > 20)
        chk("register cookie is HttpOnly", "HttpOnly" in hdrs.get("Set-Cookie", ""))

        code, _, body = post(base, "/api/register",
                             {"phone": "9876501234", "name": "Dup", "pin": "9999"})
        chk("duplicate register -> 400", code == 400 and "already registered" in json.loads(body)["error"])

        code, body = get_c(base, "/api/me", cookie=cookie)
        chk("me with cookie -> worker", code == 200 and json.loads(body)["worker"]["name"] == "Asha Devi")
        code, body = get_c(base, "/api/me")
        chk("me without cookie -> 401", code == 401)

        code, _, body = post(base, "/api/login", {"phone": "9876501234", "pin": "0000"})
        chk("wrong PIN -> 401", code == 401 and "wrong PIN" in json.loads(body)["error"])

        code, _, body = post(base, "/api/sync", {"cases": []})
        chk("sync without session -> 401", code == 401)

        code, _, body = post(base, "/api/sync", {"cases": [
            {"id": "s1", "name": "Sunita", "updated_at": 1000,
             "profile": {"state": "BR", "birth_date": "2026-06-01", "child_number": 1}},
        ]}, cookie=cookie)
        sy = json.loads(body)
        chk("sync -> merged caseload with share token",
            code == 200 and sy["cases"][0]["id"] == "s1" and len(sy["cases"][0]["share"]) >= 8)
        share = sy["cases"][0]["share"]

        code, body = get_c(base, f"/api/mother/{share}")
        mo = json.loads(body)
        chk("mother API -> sanitised plan, no login needed",
            code == 200 and mo["name"] == "Sunita" and mo["summary"]["eligible_count"] > 0
            and "profile" not in mo)
        code, body = get_c(base, "/api/mother/AAAAAAAAAAAA")
        chk("bogus share token -> 404", code == 404)

        code, hdrs2, body = get(base, f"/m/{share}")
        chk("mother page /m/<token> serves html", code == 200
            and hdrs2.get("Content-Type", "").startswith("text/html"))

        code, _, body = post(base, "/api/logout", {}, cookie=cookie)
        chk("logout clears session", code == 200)
        code, body = get_c(base, "/api/me", cookie=cookie)
        chk("session dead after logout", code == 401)

        # Static / PWA assets
        code, hdrs, body = get(base, "/manifest.webmanifest")
        chk("manifest served with manifest MIME",
            code == 200 and "manifest" in hdrs.get("Content-Type", "") and json.loads(body)["short_name"] == "Sahej")

        code, hdrs, _ = get(base, "/sw.js")
        chk("sw.js served as javascript", code == 200 and "javascript" in hdrs.get("Content-Type", ""))

        code, hdrs, body = get(base, "/icon-192.png")
        chk("icon-192 served as png with cache", code == 200
            and hdrs.get("Content-Type") == "image/png"
            and "max-age" in hdrs.get("Cache-Control", "")
            and body[:8] == b"\x89PNG\r\n\x1a\n")

        # Not found + traversal
        code, _, _ = get(base, "/nope")
        chk("unknown route -> 404", code == 404)
        code, _, body = get(base, "/../engine.py")
        chk("dotted path -> 404, no source leak", code == 404 and b"def resolve" not in body)
        code, _, body = get(base, "/..%2Fengine.py")
        chk("encoded traversal -> 404, no source leak", code == 404 and b"def resolve" not in body)
        code, _, _ = get(base, "/data/childbirth_schemes.json")
        chk("paths outside web/ -> 404", code == 404)
    finally:
        srv.shutdown()
        try:
            os.unlink(_tmp.name)
        except OSError:
            pass

    passed = sum(1 for _, ok in checks if ok)
    for name, ok in checks:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}")
    print(f"\n{passed}/{len(checks)} checks passed.")
    return passed == len(checks)


if __name__ == "__main__":
    import sys
    sys.exit(0 if run() else 1)
