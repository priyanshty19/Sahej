#!/usr/bin/env python3
"""
Zero-dependency server for Sahej.

    python3 serve.py                 # http://localhost:8000
    PORT=9000 HOST=0.0.0.0 python3 serve.py   # LAN demo

Routes:
    GET /                         -> landing page (the story)
    GET /app                      -> the ASHA tool (PWA single-page app)
    GET /api/meta                 -> states list + form option sets
    GET /api/resolve?<profile>    -> personalised benefit result
    GET /<static>                 -> files under web/ (manifest, sw.js, icons)
All backed by the same Python engine the CLI and tests use — one source of truth.
"""
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, unquote

from engine import resolve, meta, work_plan, ProfileError

MAX_BODY = 262_144      # 256 KB — a caseload of hundreds fits well under this
MAX_MOTHERS = 200

HERE = os.path.dirname(os.path.abspath(__file__))
WEB = os.path.join(HERE, "web")
PORT = int(os.environ.get("PORT", "8000"))
HOST = os.environ.get("HOST", "127.0.0.1")

_BOOL = {"true": True, "1": True, "on": True, "yes": True, "false": False, "0": False, "": False}
_BOOL_FIELDS = ["c_section", "bpl", "single_mother", "mother_disability", "child_disability",
                "govt_employee", "has_aadhaar", "has_bank_account", "premature", "low_birth_weight",
                "was_breadwinner", "accidental_death", "deceased_had_bank_account",
                "formal_sector", "construction_worker"]
_INT_FIELDS = ["child_number", "multiple_birth"]
_STR_FIELDS = ["life_event", "state", "delivery_state", "birth_date", "delivery_type", "child_sex",
               "birth_outcome", "maternal_outcome", "area", "category",
               "death_date", "relation_to_deceased", "applicant_sex"]
_FLOAT_FIELDS = ["mother_age_years", "deceased_age_years", "applicant_age_years"]

MIME = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".webmanifest": "application/manifest+json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
    ".txt": "text/plain; charset=utf-8",
}

# Pages get a strict-but-workable CSP (inline styles/scripts are part of the
# self-contained pages by design; no third-party origins are ever allowed).
CSP = "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; base-uri 'self'; form-action 'self'"


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
            p[f] = v  # engine validates and coerces
    for f in _FLOAT_FIELDS:
        v = g(f)
        if v not in (None, ""):
            p[f] = v
    for f in _BOOL_FIELDS:
        v = g(f)
        if v is not None:
            p[f] = _BOOL.get(str(v).lower(), f in ("has_aadhaar", "has_bank_account"))
    claimed = g("claimed", "")
    p["claimed"] = [c for c in claimed.split(",") if c] if claimed else []
    p["applied"] = g("applied", "") or ""
    return p


class Handler(BaseHTTPRequestHandler):
    server_version = "Sahej/1.0"

    def log_message(self, *args):
        pass

    # -- responses ------------------------------------------------------------
    def _headers(self, code, ctype, length, cache="no-store"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(length))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "strict-origin-when-cross-origin")
        self.send_header("Cache-Control", cache)
        if ctype.startswith("text/html"):
            self.send_header("Content-Security-Policy", CSP)
        self.end_headers()

    def _send(self, code, body, ctype, cache="no-store"):
        data = body.encode("utf-8") if isinstance(body, str) else body
        self._headers(code, ctype, len(data), cache)
        if self.command != "HEAD":
            self.wfile.write(data)

    def _json(self, code, obj):
        self._send(code, json.dumps(obj, ensure_ascii=False), "application/json; charset=utf-8")

    def _file(self, relpath, cache="no-store"):
        """Serve a file strictly from within web/ (no traversal)."""
        full = os.path.realpath(os.path.join(WEB, relpath))
        if not full.startswith(os.path.realpath(WEB) + os.sep) or not os.path.isfile(full):
            self._json(404, {"error": "not found"})
            return
        ext = os.path.splitext(full)[1].lower()
        with open(full, "rb") as f:
            self._send(200, f.read(), MIME.get(ext, "application/octet-stream"), cache)

    # -- routing ---------------------------------------------------------------
    def _route(self):
        parsed = urlparse(self.path)
        path = unquote(parsed.path)

        if path in ("/", "/landing", "/landing.html"):
            return self._file("landing.html")
        if path in ("/app", "/app.html", "/index.html"):
            return self._file("index.html")
        if path == "/api/meta":
            return self._json(200, meta())
        if path == "/api/resolve":
            try:
                return self._json(200, resolve(_profile_from_query(parse_qs(parsed.query))))
            except ProfileError as e:
                return self._json(400, {"error": str(e)})
            except Exception:  # noqa: BLE001 — never leak internals
                return self._json(500, {"error": "internal error — check server logs"})
        if path == "/healthz":
            return self._json(200, {"ok": True})

        # Static assets: one path segment, whitelisted extensions, long cache for icons.
        rel = path.lstrip("/")
        if rel and "/" not in rel and os.path.splitext(rel)[1].lower() in MIME:
            cache = "public, max-age=86400" if rel.startswith("icon") or rel.endswith((".png", ".svg", ".ico")) else "no-store"
            return self._file(rel, cache)

        return self._json(404, {"error": "not found"})

    def do_GET(self):
        self._route()

    def do_HEAD(self):
        self._route()

    def do_POST(self):
        if urlparse(self.path).path != "/api/plan":
            return self._json(404, {"error": "not found"})
        try:
            length = int(self.headers.get("Content-Length", 0))
        except ValueError:
            length = 0
        if not 0 < length <= MAX_BODY:
            return self._json(400, {"error": f"body required, max {MAX_BODY // 1024} KB"})
        try:
            body = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return self._json(400, {"error": "invalid JSON body"})
        mothers = body.get("mothers") if isinstance(body, dict) else body
        if not isinstance(mothers, list) or len(mothers) > MAX_MOTHERS:
            return self._json(400, {"error": f"expected {{mothers: [...]}} with at most {MAX_MOTHERS} entries"})
        try:
            return self._json(200, work_plan(mothers))
        except Exception:  # noqa: BLE001 — never leak internals
            return self._json(500, {"error": "internal error — check server logs"})


if __name__ == "__main__":
    print(f"Sahej running at http://{HOST}:{PORT}  (Ctrl-C to stop)")
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
