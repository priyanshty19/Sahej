#!/usr/bin/env python3
"""HTTP-level tests for serve.py. Run: python3 test_server.py

Boots the real ThreadingHTTPServer in-process on an ephemeral port and hits it
with urllib — no dependencies, exercises routing, validation mapping, static
serving, security headers, and traversal protection end-to-end.
"""
import json
import threading
import urllib.request
import urllib.error
from http.server import ThreadingHTTPServer

from serve import Handler


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


def post(base, path, payload):
    data = payload if isinstance(payload, bytes) else json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(base + path, data=data, method="POST",
                                 headers={"Content-Type": "application/json"})
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
        chk("GET / -> 200 landing", code == 200 and b"Sahej" in body)
        chk("/ is html", hdrs.get("Content-Type", "").startswith("text/html"))
        chk("/ has CSP header", "Content-Security-Policy" in hdrs)
        chk("/ has nosniff", hdrs.get("X-Content-Type-Options") == "nosniff")
        chk("/ has frame deny", hdrs.get("X-Frame-Options") == "DENY")

        code, _, body = get(base, "/app")
        chk("GET /app -> 200 tool", code == 200 and b"caseload" in body.lower())

        code, _, body = get(base, "/app", method="HEAD")
        chk("HEAD /app -> 200 empty body", code == 200 and body == b"")

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
        code, body = post(base, "/api/plan", {"mothers": [
            {"id": "a", "name": "Sunita", "profile": {"state": "BR", "birth_date": "2026-06-30"}},
            {"id": "b", "name": "Broken", "profile": {"state": "XX"}},
        ]})
        pl = json.loads(body)
        chk("POST /api/plan -> 200 with plan+totals",
            code == 200 and len(pl["plan"]) == 1 and pl["totals"]["mothers"] == 1)
        chk("plan isolates invalid mothers as errors", len(pl["errors"]) == 1)

        code, body = post(base, "/api/plan", b"not json{")
        chk("plan invalid JSON -> 400", code == 400 and "JSON" in json.loads(body)["error"])

        code, body = post(base, "/api/plan", {"mothers": [{}] * 201})
        chk("plan >200 mothers -> 400", code == 400)

        code, body = post(base, "/nope", {"x": 1})
        chk("POST unknown route -> 404", code == 404)

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

    passed = sum(1 for _, ok in checks if ok)
    for name, ok in checks:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}")
    print(f"\n{passed}/{len(checks)} checks passed.")
    return passed == len(checks)


if __name__ == "__main__":
    import sys
    sys.exit(0 if run() else 1)
