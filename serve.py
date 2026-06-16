#!/usr/bin/env python3
"""
Tiny zero-dependency demo server for Sahej.

    python3 serve.py        # then open http://localhost:8000

Routes:
    GET /                         -> landing page (the story)
    GET /app                      -> the ASHA tool (single-page app)
    GET /api/meta                 -> states list + form option sets
    GET /api/resolve?<profile>    -> personalised benefit result
All backed by the same Python engine the CLI and tests use — one source of truth.
"""
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

from engine import resolve, meta

HERE = os.path.dirname(os.path.abspath(__file__))
PORT = int(os.environ.get("PORT", "8000"))

_BOOL = {"true": True, "1": True, "on": True, "yes": True, "false": False, "0": False, "": False}
_BOOL_FIELDS = ["c_section", "bpl", "single_mother", "mother_disability", "child_disability",
                "govt_employee", "has_aadhaar", "has_bank_account"]
_INT_FIELDS = ["child_number", "multiple_birth"]
_STR_FIELDS = ["state", "delivery_state", "birth_date", "delivery_type", "child_sex",
               "birth_outcome", "maternal_outcome", "area", "category"]


def _profile_from_query(qs):
    def g(key, default=None):
        return qs.get(key, [default])[0]

    p = {}
    for f in _STR_FIELDS:
        v = g(f)
        if v not in (None, ""):
            p[f] = v
    for f in _INT_FIELDS:
        v = g(f)
        if v not in (None, ""):
            p[f] = int(v)
    v = g("mother_age_years")
    if v not in (None, ""):
        p["mother_age_years"] = float(v)
    # Booleans: default True for has_aadhaar/has_bank_account, else False.
    for f in _BOOL_FIELDS:
        v = g(f)
        if v is not None:
            p[f] = _BOOL.get(str(v).lower(), f in ("has_aadhaar", "has_bank_account"))
    claimed = g("claimed", "")
    p["claimed"] = [c for c in claimed.split(",") if c] if claimed else []
    return p


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _send(self, code, body, ctype):
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _json(self, code, obj):
        self._send(code, json.dumps(obj, ensure_ascii=False), "application/json; charset=utf-8")

    def _serve_file(self, filename):
        with open(os.path.join(HERE, "web", filename), "rb") as f:
            self._send(200, f.read(), "text/html; charset=utf-8")

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path in ("/", "/landing", "/landing.html"):
            self._serve_file("landing.html")
            return
        if parsed.path in ("/app", "/app.html", "/index.html"):
            self._serve_file("index.html")
            return
        if parsed.path == "/api/meta":
            self._json(200, meta())
            return
        if parsed.path == "/api/resolve":
            try:
                self._json(200, resolve(_profile_from_query(parse_qs(parsed.query))))
            except Exception as e:  # noqa: BLE001 — surface errors to the demo client
                self._json(400, {"error": str(e)})
            return
        self._json(404, {"error": "not found"})


if __name__ == "__main__":
    print(f"Sahej demo running at http://localhost:{PORT}  (Ctrl-C to stop)")
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
