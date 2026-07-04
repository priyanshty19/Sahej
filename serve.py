#!/usr/bin/env python3
"""
Zero-dependency server for Sahej.

    python3 serve.py                 # http://localhost:8000
    PORT=9000 HOST=0.0.0.0 python3 serve.py   # LAN demo

Routes:
    GET  /                        -> landing page (the story)
    GET  /app                     -> the ASHA tool (PWA single-page app)
    GET  /m/<token>               -> the mother's own page (share link, no login)
    GET  /api/meta                -> states list + form option sets
    GET  /api/resolve?<profile>   -> personalised benefit result
    GET  /api/me                  -> current signed-in worker (cookie session)
    GET  /api/mother/<token>      -> sanitised plan for the mother's page
    POST /api/register|login|logout -> phone + PIN accounts (PBKDF2, rate-limited)
    POST /api/sync                -> offline-first caseload sync (last-write-wins)
    POST /api/plan                -> Today work plan across a caseload
    GET  /<static>                -> files under web/ (manifest, sw.js, icons)
All backed by the same Python engine the CLI and tests use — one source of truth.
"""
import json
import os
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, unquote

import catalog as catalog_mod
import store
from engine import resolve, meta, work_plan, ProfileError
from store import StoreError

_CATALOG = None


def _catalog():
    global _CATALOG
    if _CATALOG is None:
        _CATALOG = catalog_mod.load_catalog()
    return _CATALOG

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
    def _headers(self, code, ctype, length, cache="no-store", extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(length))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "strict-origin-when-cross-origin")
        self.send_header("Cache-Control", cache)
        if ctype.startswith("text/html"):
            self.send_header("Content-Security-Policy", CSP)
        for k, v in (extra or []):
            self.send_header(k, v)
        self.end_headers()

    def _send(self, code, body, ctype, cache="no-store", extra=None):
        data = body.encode("utf-8") if isinstance(body, str) else body
        self._headers(code, ctype, len(data), cache, extra)
        if self.command != "HEAD":
            self.wfile.write(data)

    def _json(self, code, obj, extra=None):
        self._send(code, json.dumps(obj, ensure_ascii=False), "application/json; charset=utf-8",
                   extra=extra)

    # -- sessions ---------------------------------------------------------------
    def _session_token(self):
        cookie = SimpleCookie(self.headers.get("Cookie", ""))
        return cookie["sahej"].value if "sahej" in cookie else None

    def _worker(self):
        return store.get_session(self._session_token())

    @staticmethod
    def _cookie(token, clear=False):
        age = 0 if clear else store.SESSION_DAYS * 86_400
        secure = "; Secure" if os.environ.get("SAHEJ_SECURE") else ""
        return ("Set-Cookie",
                f"sahej={token or ''}; Path=/; Max-Age={age}; HttpOnly; SameSite=Lax{secure}")

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
        if path == "/api/schemes":
            qs = parse_qs(parsed.query)
            filters = {k: v[0] for k, v in qs.items()}
            try:
                return self._json(200, catalog_mod.search(filters, catalog=_catalog(),
                                                          limit=filters.get("limit", 100)))
            except (ValueError, TypeError):
                return self._json(400, {"error": "invalid filter value"})
        if path == "/api/facets":
            return self._json(200, catalog_mod.facet_meta(catalog=_catalog()))
        if path.startswith("/api/scheme/"):
            sid = path[len("/api/scheme/"):]
            s = catalog_mod.get(sid, catalog=_catalog())
            if s is None:
                return self._json(404, {"error": "unknown scheme id"})
            return self._json(200, s)
        if path == "/api/resolve":
            try:
                return self._json(200, resolve(_profile_from_query(parse_qs(parsed.query))))
            except ProfileError as e:
                return self._json(400, {"error": str(e)})
            except Exception:  # noqa: BLE001 — never leak internals
                return self._json(500, {"error": "internal error — check server logs"})
        if path == "/api/me":
            w = self._worker()
            if not w:
                return self._json(401, {"error": "not signed in"})
            return self._json(200, {"worker": {"name": w["name"], "phone": w["phone"]}})
        if path.startswith("/m/") and "/" not in path[3:]:
            return self._file("mother.html")
        if path.startswith("/api/mother/"):
            return self._mother(path[len("/api/mother/"):])
        if path == "/healthz":
            return self._json(200, {"ok": True})

        # Static assets: one path segment, whitelisted extensions, long cache for icons.
        rel = path.lstrip("/")
        if rel and "/" not in rel and os.path.splitext(rel)[1].lower() in MIME:
            cache = "public, max-age=86400" if rel.startswith("icon") or rel.endswith((".png", ".svg", ".ico")) else "no-store"
            return self._file(rel, cache)

        return self._json(404, {"error": "not found"})

    # -- the mother's page (share-token capability, read-only) -------------------
    def _mother(self, token):
        case = store.get_case_by_share(token)
        if case is None:
            return self._json(404, {"error": "this link is not active — ask your ASHA didi for a new one"})
        profile = dict(case["profile"])
        event = profile.get("life_event") or "childbirth"
        try:
            r = resolve(profile, life_event=event)
        except ProfileError:
            return self._json(422, {"error": "this plan needs an update — ask your ASHA didi to open it once"})
        return self._json(200, {
            "name": case["name"], "life_event": r["life_event"],
            "summary": r["summary"], "next_action": r["next_action"],
            "timeline": r["timeline"], "documents": r["documents"],
            "alerts": r["alerts"], "visit_days": r["visit_days"],
            "kb_version": r["kb_version"],
        })

    def do_GET(self):
        self._route()

    def do_HEAD(self):
        self._route()

    # -- POST router --------------------------------------------------------------
    def _body(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
        except ValueError:
            length = 0
        if not 0 < length <= MAX_BODY:
            return None
        try:
            body = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None
        return body if isinstance(body, (dict, list)) else None

    def do_POST(self):
        path = urlparse(self.path).path
        handler = {"/api/plan": self._post_plan,
                   "/api/register": self._post_register,
                   "/api/login": self._post_login,
                   "/api/logout": self._post_logout,
                   "/api/sync": self._post_sync}.get(path)
        if handler is None:
            return self._json(404, {"error": "not found"})
        try:
            return handler()
        except (ProfileError, StoreError) as e:
            return self._json(400, {"error": str(e)})
        except Exception:  # noqa: BLE001 — never leak internals
            return self._json(500, {"error": "internal error — check server logs"})

    def _post_plan(self):
        body = self._body()
        if body is None:
            return self._json(400, {"error": f"JSON body required, max {MAX_BODY // 1024} KB"})
        mothers = body.get("mothers") if isinstance(body, dict) else body
        if not isinstance(mothers, list) or len(mothers) > MAX_MOTHERS:
            return self._json(400, {"error": f"expected {{mothers: [...]}} with at most {MAX_MOTHERS} entries"})
        return self._json(200, work_plan(mothers))

    def _post_register(self):
        body = self._body()
        if not isinstance(body, dict):
            return self._json(400, {"error": "JSON body required"})
        w = store.create_worker(body.get("phone"), body.get("name"), body.get("pin"))
        full = store.verify_login(body.get("phone"), body.get("pin"))
        token = store.create_session(full["id"])
        return self._json(200, {"worker": {"name": w["name"], "phone": w["phone"]}},
                          extra=[self._cookie(token)])

    def _post_login(self):
        body = self._body()
        if not isinstance(body, dict):
            return self._json(400, {"error": "JSON body required"})
        try:
            w = store.verify_login(body.get("phone"), body.get("pin"))
        except StoreError as e:
            return self._json(401, {"error": str(e)})
        token = store.create_session(w["id"])
        return self._json(200, {"worker": {"name": w["name"], "phone": w["phone"]}},
                          extra=[self._cookie(token)])

    def _post_logout(self):
        store.delete_session(self._session_token())
        return self._json(200, {"ok": True}, extra=[self._cookie(None, clear=True)])

    def _post_sync(self):
        w = self._worker()
        if not w:
            return self._json(401, {"error": "sign in to sync"})
        body = self._body()
        if not isinstance(body, dict):
            return self._json(400, {"error": "JSON body required"})
        merged = store.sync_cases(w["id"], body.get("cases") or [], body.get("deleted") or [])
        return self._json(200, merged)


if __name__ == "__main__":
    print(f"Sahej running at http://{HOST}:{PORT}  (Ctrl-C to stop)")
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
