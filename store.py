#!/usr/bin/env python3
"""
Sahej storage layer — one small data model, two interchangeable backends.

Holds worker accounts (phone + PIN, PBKDF2-hashed), login sessions, and each
worker's caseload. The browser's localStorage remains the offline source of
truth; the server copy exists so a worker can move phones, a supervisor can
(later) aggregate, and each case gets an unguessable share token that powers
the mother-facing page at /m/<token>.

Backend selection (decided once, at import, from the environment):
  * DATABASE_URL set to a postgres:// URL  -> Neon / any Postgres (production).
  * otherwise                              -> SQLite on local disk (dev + tests).

The SQLite path keeps Sahej dependency-free for local work and CI; the Postgres
path is what runs on Vercel, where the filesystem is read-only and every request
may hit a fresh instance. The SQL is written once with `?` placeholders and a
thin wrapper translates for whichever driver is active.

Env:
  DATABASE_URL   postgres connection string (use Neon's pooled -pooler host).
  SAHEJ_DB       override the SQLite file path (tests point it at a temp file).
"""
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import time

DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
_PG = DATABASE_URL.startswith(("postgres://", "postgresql://"))

HERE = os.path.dirname(os.path.abspath(__file__))
if _PG:
    import psycopg2
    import psycopg2.extras
    _UNIQUE_ERRORS = (psycopg2.IntegrityError,)
    DB_PATH = None
else:
    # On Vercel without a database the only writable place is /tmp (ephemeral,
    # per-instance) — good enough to boot, though real data needs DATABASE_URL.
    _default = "/tmp/sahej.db" if os.environ.get("VERCEL") else os.path.join(HERE, "data", "sahej.db")
    DB_PATH = os.environ.get("SAHEJ_DB") or _default
    _UNIQUE_ERRORS = (sqlite3.IntegrityError,)

SESSION_DAYS = 30
PBKDF2_ITERS = 200_000
MAX_FAILED = 5
LOCK_SECONDS = 300
MAX_CASES = 500
MAX_PROFILE_BYTES = 8_192

# Consumer OTP login (passwordless)
OTP_TTL = 600            # a code is valid for 10 minutes
OTP_MAX_ATTEMPTS = 5     # wrong tries before the code is burned
OTP_RESEND_SECONDS = 30  # minimum gap between sends to one number
CONSUMER_SESSION_DAYS = 60

_PHONE_RE = re.compile(r"^[6-9]\d{9}$")
_PIN_RE = re.compile(r"^\d{4,8}$")
_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


class StoreError(ValueError):
    """User-safe storage/validation error."""


# --- backend-agnostic connection ---------------------------------------------

class _Conn:
    """Wraps a sqlite3 or psycopg2 connection behind one small interface.

    execute(sql, params) accepts `?` placeholders and dict-keyed rows for both
    drivers; iterate the returned cursor or call fetchone()/fetchall().
    """

    def __init__(self, raw):
        self._raw = raw

    def execute(self, sql, params=()):
        if _PG:
            cur = self._raw.cursor()
            cur.execute(sql.replace("?", "%s"), params)
            return cur
        return self._raw.execute(sql, params)

    def commit(self):
        self._raw.commit()

    def rollback(self):
        try:
            self._raw.rollback()
        except Exception:  # noqa: BLE001
            pass

    def close(self):
        self._raw.close()


_initialized = False


def _connect():
    global _initialized
    if _PG:
        raw = psycopg2.connect(DATABASE_URL, connect_timeout=5,
                               cursor_factory=psycopg2.extras.RealDictCursor)
    else:
        raw = sqlite3.connect(DB_PATH, timeout=10)
        raw.row_factory = sqlite3.Row
        raw.execute("PRAGMA journal_mode=WAL")
        raw.execute("PRAGMA foreign_keys=ON")
    con = _Conn(raw)
    if not _initialized:
        _init(con)
        _initialized = True
    return con


# Schema differs only in column types and the auto-increment key.
_SCHEMA_PG = [
    """CREATE TABLE IF NOT EXISTS workers(
        id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        phone TEXT UNIQUE NOT NULL,
        name TEXT NOT NULL,
        pin_hash BYTEA NOT NULL,
        salt BYTEA NOT NULL,
        failed_attempts INTEGER NOT NULL DEFAULT 0,
        locked_until DOUBLE PRECISION NOT NULL DEFAULT 0,
        created_at DOUBLE PRECISION NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS sessions(
        token_hash TEXT PRIMARY KEY,
        worker_id BIGINT NOT NULL REFERENCES workers(id) ON DELETE CASCADE,
        created_at DOUBLE PRECISION NOT NULL,
        expires_at DOUBLE PRECISION NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS cases(
        id TEXT NOT NULL,
        worker_id BIGINT NOT NULL REFERENCES workers(id) ON DELETE CASCADE,
        name TEXT NOT NULL DEFAULT '',
        profile TEXT NOT NULL,
        updated_at DOUBLE PRECISION NOT NULL,
        deleted INTEGER NOT NULL DEFAULT 0,
        share_token TEXT UNIQUE NOT NULL,
        PRIMARY KEY (worker_id, id))""",
    "CREATE INDEX IF NOT EXISTS idx_cases_share ON cases(share_token)",
    # --- content: the scheme catalog + life-event knowledge bases, so the data
    #     lives in the database (Supabase) rather than being read from disk. ---
    """CREATE TABLE IF NOT EXISTS schemes(
        id TEXT PRIMARY KEY,
        doc TEXT NOT NULL,
        source TEXT NOT NULL DEFAULT 'catalog',
        updated_at DOUBLE PRECISION NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS reference_docs(
        name TEXT PRIMARY KEY,
        doc TEXT NOT NULL,
        updated_at DOUBLE PRECISION NOT NULL)""",
    # --- leads: consumer mobile-number capture before viewing a scheme
    #     (OTP verification comes later; for now we just record the interest). ---
    """CREATE TABLE IF NOT EXISTS leads(
        id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        mobile TEXT NOT NULL,
        name TEXT NOT NULL DEFAULT '',
        scheme_id TEXT NOT NULL DEFAULT '',
        locale TEXT NOT NULL DEFAULT 'en',
        context TEXT NOT NULL DEFAULT '',
        verified INTEGER NOT NULL DEFAULT 0,
        created_at DOUBLE PRECISION NOT NULL)""",
    "CREATE INDEX IF NOT EXISTS idx_leads_mobile ON leads(mobile)",
    # --- consumer accounts: passwordless, phone + OTP (no PIN) ---
    """CREATE TABLE IF NOT EXISTS consumers(
        id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        mobile TEXT UNIQUE NOT NULL,
        name TEXT NOT NULL DEFAULT '',
        profile TEXT NOT NULL DEFAULT '{}',
        created_at DOUBLE PRECISION NOT NULL,
        last_login_at DOUBLE PRECISION NOT NULL DEFAULT 0)""",
    """CREATE TABLE IF NOT EXISTS otp_codes(
        mobile TEXT PRIMARY KEY,
        code_hash BYTEA NOT NULL,
        salt BYTEA NOT NULL,
        expires_at DOUBLE PRECISION NOT NULL,
        attempts INTEGER NOT NULL DEFAULT 0,
        last_sent_at DOUBLE PRECISION NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS consumer_sessions(
        token_hash TEXT PRIMARY KEY,
        consumer_id BIGINT NOT NULL REFERENCES consumers(id) ON DELETE CASCADE,
        created_at DOUBLE PRECISION NOT NULL,
        expires_at DOUBLE PRECISION NOT NULL)""",
    # --- Clerk mirror: every Clerk user (ASHA username/pw or consumer phone/OTP)
    #     is synced here so Supabase stays the single source of truth. ---
    """CREATE TABLE IF NOT EXISTS clerk_users(
        clerk_user_id TEXT PRIMARY KEY,
        role TEXT NOT NULL DEFAULT 'consumer',
        username TEXT NOT NULL DEFAULT '',
        phone TEXT NOT NULL DEFAULT '',
        email TEXT NOT NULL DEFAULT '',
        name TEXT NOT NULL DEFAULT '',
        created_at DOUBLE PRECISION NOT NULL,
        last_seen_at DOUBLE PRECISION NOT NULL DEFAULT 0)""",
    "CREATE INDEX IF NOT EXISTS idx_clerk_users_phone ON clerk_users(phone)",
]

_SCHEMA_SQLITE = """
    CREATE TABLE IF NOT EXISTS workers(
        id INTEGER PRIMARY KEY,
        phone TEXT UNIQUE NOT NULL,
        name TEXT NOT NULL,
        pin_hash BLOB NOT NULL,
        salt BLOB NOT NULL,
        failed_attempts INTEGER NOT NULL DEFAULT 0,
        locked_until REAL NOT NULL DEFAULT 0,
        created_at REAL NOT NULL
    );
    CREATE TABLE IF NOT EXISTS sessions(
        token_hash TEXT PRIMARY KEY,
        worker_id INTEGER NOT NULL REFERENCES workers(id) ON DELETE CASCADE,
        created_at REAL NOT NULL,
        expires_at REAL NOT NULL
    );
    CREATE TABLE IF NOT EXISTS cases(
        id TEXT NOT NULL,
        worker_id INTEGER NOT NULL REFERENCES workers(id) ON DELETE CASCADE,
        name TEXT NOT NULL DEFAULT '',
        profile TEXT NOT NULL,
        updated_at REAL NOT NULL,
        deleted INTEGER NOT NULL DEFAULT 0,
        share_token TEXT UNIQUE NOT NULL,
        PRIMARY KEY (worker_id, id)
    );
    CREATE INDEX IF NOT EXISTS idx_cases_share ON cases(share_token);
    CREATE TABLE IF NOT EXISTS schemes(
        id TEXT PRIMARY KEY,
        doc TEXT NOT NULL,
        source TEXT NOT NULL DEFAULT 'catalog',
        updated_at REAL NOT NULL
    );
    CREATE TABLE IF NOT EXISTS reference_docs(
        name TEXT PRIMARY KEY,
        doc TEXT NOT NULL,
        updated_at REAL NOT NULL
    );
    CREATE TABLE IF NOT EXISTS leads(
        id INTEGER PRIMARY KEY,
        mobile TEXT NOT NULL,
        name TEXT NOT NULL DEFAULT '',
        scheme_id TEXT NOT NULL DEFAULT '',
        locale TEXT NOT NULL DEFAULT 'en',
        context TEXT NOT NULL DEFAULT '',
        verified INTEGER NOT NULL DEFAULT 0,
        created_at REAL NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_leads_mobile ON leads(mobile);
    CREATE TABLE IF NOT EXISTS consumers(
        id INTEGER PRIMARY KEY,
        mobile TEXT UNIQUE NOT NULL,
        name TEXT NOT NULL DEFAULT '',
        profile TEXT NOT NULL DEFAULT '{}',
        created_at REAL NOT NULL,
        last_login_at REAL NOT NULL DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS otp_codes(
        mobile TEXT PRIMARY KEY,
        code_hash BLOB NOT NULL,
        salt BLOB NOT NULL,
        expires_at REAL NOT NULL,
        attempts INTEGER NOT NULL DEFAULT 0,
        last_sent_at REAL NOT NULL
    );
    CREATE TABLE IF NOT EXISTS consumer_sessions(
        token_hash TEXT PRIMARY KEY,
        consumer_id INTEGER NOT NULL REFERENCES consumers(id) ON DELETE CASCADE,
        created_at REAL NOT NULL,
        expires_at REAL NOT NULL
    );
    CREATE TABLE IF NOT EXISTS clerk_users(
        clerk_user_id TEXT PRIMARY KEY,
        role TEXT NOT NULL DEFAULT 'consumer',
        username TEXT NOT NULL DEFAULT '',
        phone TEXT NOT NULL DEFAULT '',
        email TEXT NOT NULL DEFAULT '',
        name TEXT NOT NULL DEFAULT '',
        created_at REAL NOT NULL,
        last_seen_at REAL NOT NULL DEFAULT 0
    );
    CREATE INDEX IF NOT EXISTS idx_clerk_users_phone ON clerk_users(phone);
"""


def _init(con):
    try:
        if _PG:
            for stmt in _SCHEMA_PG:
                con.execute(stmt)
        else:
            con._raw.executescript(_SCHEMA_SQLITE)
        con.commit()
    except Exception:  # noqa: BLE001 — concurrent cold starts may race on CREATE
        con.rollback()
    _migrate(con)


def _migrate(con):
    """Additive, idempotent column migrations for databases created before a
    column existed (e.g. a live Supabase instance from before `profile` was
    added to consumers). CREATE TABLE IF NOT EXISTS never adds columns to an
    existing table, so this covers that gap — each ALTER is independent and
    a 'column already exists' error is expected and ignored on every run
    after the first."""
    for stmt in (
        "ALTER TABLE consumers ADD COLUMN profile TEXT NOT NULL DEFAULT '{}'",
        # Clerk-provisioned ASHA workers link back to their Clerk user so the
        # existing caseload machinery works without a phone/PIN.
        "ALTER TABLE workers ADD COLUMN clerk_user_id TEXT",
    ):
        try:
            con.execute(stmt)
            con.commit()
        except Exception:  # noqa: BLE001 — column already present after the first run
            con.rollback()


# --- hashing helpers ----------------------------------------------------------

def _hash_pin(pin, salt):
    return hashlib.pbkdf2_hmac("sha256", pin.encode("utf-8"), bytes(salt), PBKDF2_ITERS)


def _token_hash(token):
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def normalize_phone(phone):
    p = re.sub(r"[\s()-]", "", str(phone or ""))
    if p.startswith("+91"):
        p = p[3:]
    elif p.startswith("91") and len(p) == 12:
        p = p[2:]
    elif p.startswith("0") and len(p) == 11:
        p = p[1:]
    if not _PHONE_RE.match(p):
        raise StoreError("enter a valid 10-digit Indian mobile number")
    return p


def _check_pin_format(pin):
    if not _PIN_RE.match(str(pin or "")):
        raise StoreError("PIN must be 4-8 digits")


# --- accounts -----------------------------------------------------------------

def create_worker(phone, name, pin):
    phone = normalize_phone(phone)
    _check_pin_format(pin)
    name = str(name or "").strip()[:60]
    if not name:
        raise StoreError("name is required")
    salt = secrets.token_bytes(16)
    con = _connect()
    try:
        con.execute(
            "INSERT INTO workers(phone, name, pin_hash, salt, created_at) VALUES(?,?,?,?,?)",
            (phone, name, _hash_pin(str(pin), salt), salt, time.time()))
        con.commit()
    except _UNIQUE_ERRORS:
        con.rollback()
        raise StoreError("this number is already registered — sign in instead")
    finally:
        con.close()
    return {"phone": phone, "name": name}


def verify_login(phone, pin):
    phone = normalize_phone(phone)
    _check_pin_format(pin)
    con = _connect()
    try:
        row = con.execute("SELECT * FROM workers WHERE phone=?", (phone,)).fetchone()
        if row is None:
            raise StoreError("no account with this number — register first")
        now = time.time()
        if row["locked_until"] > now:
            wait = int(row["locked_until"] - now) + 1
            raise StoreError(f"too many wrong PINs — try again in {wait} seconds")
        if not hmac.compare_digest(_hash_pin(str(pin), row["salt"]), bytes(row["pin_hash"])):
            failed = row["failed_attempts"] + 1
            locked = now + LOCK_SECONDS if failed >= MAX_FAILED else 0
            con.execute("UPDATE workers SET failed_attempts=?, locked_until=? WHERE id=?",
                        (0 if locked else failed, locked, row["id"]))
            con.commit()
            raise StoreError("wrong PIN")
        con.execute("UPDATE workers SET failed_attempts=0, locked_until=0 WHERE id=?", (row["id"],))
        con.commit()
        return {"id": row["id"], "phone": row["phone"], "name": row["name"]}
    finally:
        con.close()


# --- sessions -----------------------------------------------------------------

def create_session(worker_id):
    token = secrets.token_urlsafe(32)
    now = time.time()
    con = _connect()
    try:
        con.execute("INSERT INTO sessions(token_hash, worker_id, created_at, expires_at) VALUES(?,?,?,?)",
                    (_token_hash(token), worker_id, now, now + SESSION_DAYS * 86_400))
        con.execute("DELETE FROM sessions WHERE expires_at < ?", (now,))
        con.commit()
    finally:
        con.close()
    return token


def get_session(token):
    if not token:
        return None
    con = _connect()
    try:
        row = con.execute(
            "SELECT w.id, w.phone, w.name FROM sessions s JOIN workers w ON w.id=s.worker_id "
            "WHERE s.token_hash=? AND s.expires_at > ?",
            (_token_hash(token), time.time())).fetchone()
        return dict(row) if row else None
    finally:
        con.close()


def delete_session(token):
    if not token:
        return
    con = _connect()
    try:
        con.execute("DELETE FROM sessions WHERE token_hash=?", (_token_hash(token),))
        con.commit()
    finally:
        con.close()


# --- caseload sync ------------------------------------------------------------

def sync_cases(worker_id, cases, deleted=None):
    """Last-write-wins merge of the client caseload into the server copy.

    `cases` — live entries: {id, name, profile, updated_at(ms)}.
    `deleted` — tombstones: {id, updated_at(ms)}.
    Returns {"cases": [...merged live, each with share_token...], "deleted": [ids]}.
    """
    if not isinstance(cases, list) or len(cases) > MAX_CASES:
        raise StoreError(f"expected a list of at most {MAX_CASES} cases")
    con = _connect()
    try:
        for c in cases:
            if not isinstance(c, dict):
                raise StoreError("each case must be an object")
            cid = str(c.get("id") or "")
            if not _ID_RE.match(cid):
                raise StoreError("case id must be 1-64 letters/digits/-/_")
            profile = c.get("profile")
            if not isinstance(profile, dict):
                raise StoreError(f"case {cid}: profile must be an object")
            pj = json.dumps(profile, ensure_ascii=False)
            if len(pj.encode("utf-8")) > MAX_PROFILE_BYTES:
                raise StoreError(f"case {cid}: profile too large")
            name = str(c.get("name") or "")[:120]
            try:
                ts = float(c.get("updated_at") or 0)
            except (TypeError, ValueError):
                ts = 0.0
            row = con.execute("SELECT updated_at FROM cases WHERE worker_id=? AND id=?",
                              (worker_id, cid)).fetchone()
            if row is None:
                con.execute(
                    "INSERT INTO cases(id, worker_id, name, profile, updated_at, deleted, share_token) "
                    "VALUES(?,?,?,?,?,0,?)",
                    (cid, worker_id, name, pj, ts, secrets.token_urlsafe(12)))
            elif ts >= row["updated_at"]:
                con.execute(
                    "UPDATE cases SET name=?, profile=?, updated_at=?, deleted=0 "
                    "WHERE worker_id=? AND id=?",
                    (name, pj, ts, worker_id, cid))
        for d in (deleted or []):
            if not isinstance(d, dict):
                continue
            cid = str(d.get("id") or "")
            if not _ID_RE.match(cid):
                continue
            try:
                ts = float(d.get("updated_at") or 0)
            except (TypeError, ValueError):
                ts = 0.0
            con.execute(
                "UPDATE cases SET deleted=1, updated_at=? WHERE worker_id=? AND id=? AND updated_at <= ?",
                (ts, worker_id, cid, ts))
        con.commit()
        live, gone = [], []
        for row in con.execute(
                "SELECT id, name, profile, updated_at, deleted, share_token FROM cases "
                "WHERE worker_id=? ORDER BY updated_at DESC", (worker_id,)):
            if row["deleted"]:
                gone.append(row["id"])
            else:
                live.append({"id": row["id"], "name": row["name"],
                             "profile": json.loads(row["profile"]),
                             "updated_at": row["updated_at"], "share": row["share_token"]})
        return {"cases": live, "deleted": gone}
    finally:
        con.close()


def get_case_by_share(token):
    if not token or not re.match(r"^[A-Za-z0-9_-]{8,64}$", token):
        return None
    con = _connect()
    try:
        row = con.execute(
            "SELECT id, name, profile, updated_at FROM cases WHERE share_token=? AND deleted=0",
            (token,)).fetchone()
        if row is None:
            return None
        return {"id": row["id"], "name": row["name"], "profile": json.loads(row["profile"])}
    finally:
        con.close()


# --- content: schemes + reference docs (the catalog lives in the database) ----
#
# One page load (explore.html's dataReady) fans out into /api/facets + /api/meta,
# which between them used to call content_ready()/get_reference() ~10-12 times —
# each one opening its own fresh Postgres connection (no pooling on the stdlib
# path). On a cold serverless instance talking to a remote DB that's easily
# several seconds of pure connection overhead, enough to blow the frontend's
# fetch timeout before the (fast, reliable) JSON fallback ever gets a chance.
# These are small, rarely-changing reference docs, so an in-process cache with a
# short TTL — invalidated immediately on write — cuts that down to ~1 real
# connection per distinct name per TTL window, on every backend.
_CACHE_TTL = 20.0
_content_ready_cache = {"value": None, "ts": 0.0}
_ref_cache = {}  # name -> (value, ts)


def content_ready():
    """True when the schemes table has been seeded — the signal for catalog.py /
    engine.py to read from the database instead of the bundled JSON. Any failure
    (no DB, unreachable, not yet migrated) returns False so the app still boots
    from disk; this keeps CI and offline dev working with zero configuration."""
    now = time.time()
    if _content_ready_cache["value"] is not None and now - _content_ready_cache["ts"] < _CACHE_TTL:
        return _content_ready_cache["value"]
    try:
        con = _connect()
    except Exception:  # noqa: BLE001 — DB optional; fall back to JSON
        _content_ready_cache.update(value=False, ts=now)
        return False
    try:
        row = con.execute("SELECT COUNT(*) AS n FROM schemes").fetchone()
        ready = bool(row and row["n"] > 0)
    except Exception:  # noqa: BLE001
        ready = False
    finally:
        con.close()
    _content_ready_cache.update(value=ready, ts=now)
    return ready


def db_health():
    """Small production diagnostic: report backend reachability without secrets."""
    out = {"backend": "postgres" if _PG else "sqlite",
           "database_configured": bool(DATABASE_URL) if _PG else bool(DB_PATH)}
    con = None
    try:
        con = _connect()
        out["ok"] = True
        out["content_ready"] = content_ready()
        for table in ("schemes", "reference_docs", "workers", "cases", "leads", "consumers", "clerk_users"):
            try:
                row = con.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()
                out[table] = int(row["n"]) if row else 0
            except Exception as e:  # noqa: BLE001
                out[table] = {"error": type(e).__name__}
    except Exception as e:  # noqa: BLE001
        out["ok"] = False
        out["error"] = type(e).__name__
    finally:
        if con is not None:
            con.close()
    return out


def replace_schemes(schemes):
    """Seed/refresh the catalog: (id, doc-dict, source) tuples, replacing all rows."""
    now = time.time()
    con = _connect()
    try:
        con.execute("DELETE FROM schemes")
        for sid, doc, source in schemes:
            con.execute(
                "INSERT INTO schemes(id, doc, source, updated_at) VALUES(?,?,?,?)",
                (str(sid), json.dumps(doc, ensure_ascii=False), str(source or "catalog"), now))
        con.commit()
        return len(schemes)
    finally:
        con.close()
        _content_ready_cache.update(value=None, ts=0.0)


def all_schemes(source=None):
    con = _connect()
    try:
        if source:
            rows = con.execute("SELECT doc FROM schemes WHERE source=? ORDER BY id", (source,))
        else:
            rows = con.execute("SELECT doc FROM schemes ORDER BY id")
        return [json.loads(r["doc"]) for r in rows]
    finally:
        con.close()


def get_scheme(scheme_id):
    con = _connect()
    try:
        row = con.execute("SELECT doc FROM schemes WHERE id=?", (str(scheme_id),)).fetchone()
        return json.loads(row["doc"]) if row else None
    finally:
        con.close()


def upsert_reference(name, doc):
    now = time.time()
    con = _connect()
    try:
        # portable upsert: delete-then-insert keeps one code path for both drivers.
        con.execute("DELETE FROM reference_docs WHERE name=?", (str(name),))
        con.execute("INSERT INTO reference_docs(name, doc, updated_at) VALUES(?,?,?)",
                    (str(name), json.dumps(doc, ensure_ascii=False), now))
        con.commit()
    finally:
        con.close()
    _ref_cache.pop(str(name), None)


def get_reference(name):
    key = str(name)
    now = time.time()
    cached = _ref_cache.get(key)
    if cached is not None and now - cached[1] < _CACHE_TTL:
        return cached[0]
    con = _connect()
    try:
        row = con.execute("SELECT doc FROM reference_docs WHERE name=?", (key,)).fetchone()
        value = json.loads(row["doc"]) if row else None
    finally:
        con.close()
    _ref_cache[key] = (value, now)
    return value


# --- leads: consumer mobile capture (OTP-ready) -------------------------------

def create_lead(mobile, name="", scheme_id="", locale="en", context=""):
    mobile = normalize_phone(mobile)
    con = _connect()
    try:
        con.execute(
            "INSERT INTO leads(mobile, name, scheme_id, locale, context, created_at) "
            "VALUES(?,?,?,?,?,?)",
            (mobile, str(name or "")[:120], str(scheme_id or "")[:64],
             str(locale or "en")[:8], str(context or "")[:120], time.time()))
        con.commit()
    finally:
        con.close()
    return {"mobile": mobile}


# --- consumer accounts: passwordless phone + OTP ------------------------------

def _gen_otp():
    return f"{secrets.randbelow(1_000_000):06d}"


def request_otp(mobile):
    """Create/refresh a one-time code for this number and return it, so the
    caller can deliver it (dev: shown in the response; prod: handed to an SMS
    provider). Rate-limited to one send per OTP_RESEND_SECONDS."""
    mobile = normalize_phone(mobile)
    now = time.time()
    con = _connect()
    try:
        row = con.execute("SELECT last_sent_at FROM otp_codes WHERE mobile=?", (mobile,)).fetchone()
        if row and now - row["last_sent_at"] < OTP_RESEND_SECONDS:
            wait = int(OTP_RESEND_SECONDS - (now - row["last_sent_at"])) + 1
            raise StoreError(f"please wait {wait} seconds before requesting another code")
        code = _gen_otp()
        salt = secrets.token_bytes(16)
        con.execute("DELETE FROM otp_codes WHERE mobile=?", (mobile,))
        con.execute(
            "INSERT INTO otp_codes(mobile, code_hash, salt, expires_at, attempts, last_sent_at) "
            "VALUES(?,?,?,?,0,?)",
            (mobile, _hash_pin(code, salt), salt, now + OTP_TTL, now))
        con.commit()
        return {"mobile": mobile, "code": code}
    finally:
        con.close()


def verify_otp(mobile, code):
    """Check the code; on success create-or-fetch the consumer and return it.
    Wrong/expired codes raise StoreError; wrong tries are capped."""
    mobile = normalize_phone(mobile)
    code = re.sub(r"\D", "", str(code or ""))
    now = time.time()
    con = _connect()
    try:
        row = con.execute("SELECT * FROM otp_codes WHERE mobile=?", (mobile,)).fetchone()
        if row is None or row["expires_at"] < now:
            raise StoreError("this code has expired — request a new one")
        if row["attempts"] >= OTP_MAX_ATTEMPTS:
            con.execute("DELETE FROM otp_codes WHERE mobile=?", (mobile,))
            con.commit()
            raise StoreError("too many wrong attempts — request a new code")
        if not hmac.compare_digest(_hash_pin(code, row["salt"]), bytes(row["code_hash"])):
            con.execute("UPDATE otp_codes SET attempts=attempts+1 WHERE mobile=?", (mobile,))
            con.commit()
            raise StoreError("wrong code — check and try again")
        con.execute("DELETE FROM otp_codes WHERE mobile=?", (mobile,))
        crow = con.execute("SELECT id, name FROM consumers WHERE mobile=?", (mobile,)).fetchone()
        if crow is None:
            con.execute("INSERT INTO consumers(mobile, name, created_at, last_login_at) VALUES(?,?,?,?)",
                        (mobile, "", now, now))
            crow = con.execute("SELECT id, name FROM consumers WHERE mobile=?", (mobile,)).fetchone()
        else:
            con.execute("UPDATE consumers SET last_login_at=? WHERE id=?", (now, crow["id"]))
        con.execute("UPDATE leads SET verified=1 WHERE mobile=?", (mobile,))  # their captures are now verified
        con.commit()
        return {"id": crow["id"], "mobile": mobile, "name": crow["name"]}
    finally:
        con.close()


CONSUMER_PROFILE_FIELDS = ("who", "state", "age", "gender", "category", "occupation",
                          "occupation_other", "bpl", "disability", "rural")


def _clean_consumer_profile(profile):
    """Whitelist + coerce the For You wizard's facets before they touch the DB
    — never trust the client's JSON shape directly."""
    if not isinstance(profile, dict):
        return {}
    out = {}
    for k in CONSUMER_PROFILE_FIELDS:
        v = profile.get(k)
        if v in (None, ""):
            continue
        if k in ("bpl", "disability", "rural"):
            out[k] = bool(v)
        elif k == "age":
            try:
                out[k] = int(v)
            except (TypeError, ValueError):
                continue
        else:
            out[k] = str(v)[:60]
    return out


def register_consumer(mobile, name="", profile=None):
    """Create-or-fetch a consumer record immediately from a mobile number alone
    — no OTP. This is a data-collection gate, not an identity check: it exists
    so the marketplace can log who's interested in a scheme without making a
    visitor wait on an SMS. Any eligibility facets captured in the For You
    wizard (state/age/gender/category/occupation/bpl/disability/rural) are
    merged into the consumer's stored profile so they persist server-side,
    not just in the browser's localStorage. Returns the same shape as
    verify_otp() plus the merged profile dict."""
    mobile = normalize_phone(mobile)
    name = str(name or "").strip()[:60]
    incoming = _clean_consumer_profile(profile)
    now = time.time()
    con = _connect()
    try:
        crow = con.execute("SELECT id, name, profile FROM consumers WHERE mobile=?", (mobile,)).fetchone()
        if crow is None:
            con.execute(
                "INSERT INTO consumers(mobile, name, profile, created_at, last_login_at) VALUES(?,?,?,?,?)",
                (mobile, name, json.dumps(incoming, ensure_ascii=False), now, now))
            crow = con.execute("SELECT id, name, profile FROM consumers WHERE mobile=?", (mobile,)).fetchone()
        else:
            try:
                existing = json.loads(crow["profile"] or "{}")
            except (TypeError, ValueError):
                existing = {}
            merged = {**existing, **incoming}
            final_name = name if (name and not crow["name"]) else crow["name"]
            con.execute("UPDATE consumers SET name=?, profile=?, last_login_at=? WHERE id=?",
                        (final_name, json.dumps(merged, ensure_ascii=False), now, crow["id"]))
            crow = {"id": crow["id"], "name": final_name, "profile": json.dumps(merged, ensure_ascii=False)}
        con.commit()
        try:
            prof_out = json.loads(crow["profile"] or "{}")
        except (TypeError, ValueError):
            prof_out = {}
        return {"id": crow["id"], "mobile": mobile, "name": name or crow["name"], "profile": prof_out}
    finally:
        con.close()


def set_consumer_name(consumer_id, name):
    name = str(name or "").strip()[:60]
    con = _connect()
    try:
        con.execute("UPDATE consumers SET name=? WHERE id=?", (name, consumer_id))
        con.commit()
    finally:
        con.close()
    return {"name": name}


def create_consumer_session(consumer_id):
    token = secrets.token_urlsafe(32)
    now = time.time()
    con = _connect()
    try:
        con.execute("INSERT INTO consumer_sessions(token_hash, consumer_id, created_at, expires_at) VALUES(?,?,?,?)",
                    (_token_hash(token), consumer_id, now, now + CONSUMER_SESSION_DAYS * 86_400))
        con.execute("DELETE FROM consumer_sessions WHERE expires_at < ?", (now,))
        con.commit()
    finally:
        con.close()
    return token


def get_consumer_session(token):
    if not token:
        return None
    con = _connect()
    try:
        row = con.execute(
            "SELECT c.id, c.mobile, c.name FROM consumer_sessions s JOIN consumers c ON c.id=s.consumer_id "
            "WHERE s.token_hash=? AND s.expires_at > ?", (_token_hash(token), time.time())).fetchone()
        return dict(row) if row else None
    finally:
        con.close()


def delete_consumer_session(token):
    if not token:
        return
    con = _connect()
    try:
        con.execute("DELETE FROM consumer_sessions WHERE token_hash=?", (_token_hash(token),))
        con.commit()
    finally:
        con.close()


# --- Clerk mirror: keep Supabase the source of truth for Clerk-authed users ---

def upsert_clerk_user(user):
    """Mirror a Clerk user (from clerk_auth.fetch_clerk_user) into Supabase.
    `user` = {clerk_user_id, role, username, phone, email, name}. A consumer
    (phone) is also bridged into the consumers table and their leads marked
    verified, so the existing consumer machinery keeps working."""
    now = time.time()
    uid = str(user.get("clerk_user_id") or "")
    if not uid:
        raise StoreError("clerk_user_id required")
    role = str(user.get("role") or "consumer")
    username = str(user.get("username") or "")[:60]
    phone = str(user.get("phone") or "")
    email = str(user.get("email") or "")[:120]
    name = str(user.get("name") or "")[:60]
    con = _connect()
    try:
        exists = con.execute("SELECT clerk_user_id FROM clerk_users WHERE clerk_user_id=?", (uid,)).fetchone()
        if exists is None:
            con.execute(
                "INSERT INTO clerk_users(clerk_user_id, role, username, phone, email, name, created_at, last_seen_at) "
                "VALUES(?,?,?,?,?,?,?,?)", (uid, role, username, phone, email, name, now, now))
        else:
            con.execute(
                "UPDATE clerk_users SET role=?, username=?, phone=?, email=?, name=?, last_seen_at=? WHERE clerk_user_id=?",
                (role, username, phone, email, name, now, uid))
        norm, consumer_id = "", None
        if role == "consumer" and phone:
            try:
                norm = normalize_phone(phone)  # handles E.164 +91…
            except StoreError:
                norm = ""
            if norm:
                c = con.execute("SELECT id FROM consumers WHERE mobile=?", (norm,)).fetchone()
                if c is None:
                    con.execute("INSERT INTO consumers(mobile, name, created_at, last_login_at) VALUES(?,?,?,?)",
                                (norm, name, now, now))
                    c = con.execute("SELECT id FROM consumers WHERE mobile=?", (norm,)).fetchone()
                else:
                    con.execute("UPDATE consumers SET last_login_at=?, name=COALESCE(NULLIF(?,''), name) WHERE id=?",
                                (now, name, c["id"]))
                consumer_id = c["id"]
                con.execute("UPDATE leads SET verified=1 WHERE mobile=?", (norm,))
        con.commit()
        return {"clerk_user_id": uid, "role": role, "username": username,
                "phone": phone, "email": email, "name": name, "mobile": norm,
                "consumer_id": consumer_id}
    finally:
        con.close()


def worker_for_clerk(clerk_user_id, name=""):
    """Find or provision the workers row for a Clerk-authenticated ASHA user, so
    the existing caseload/sync machinery (keyed on worker_id) works unchanged.
    These rows never use phone/PIN login — the phone column holds a synthetic
    unique value and the PIN hash is random and unused."""
    now = time.time()
    con = _connect()
    try:
        row = con.execute("SELECT id, name FROM workers WHERE clerk_user_id=?",
                          (str(clerk_user_id),)).fetchone()
        if row is not None:
            return {"id": row["id"], "name": row["name"]}
        con.execute(
            "INSERT INTO workers(phone, name, pin_hash, salt, created_at, clerk_user_id) "
            "VALUES(?,?,?,?,?,?)",
            ("clerk:" + str(clerk_user_id), (name or "ASHA")[:60],
             secrets.token_bytes(32), secrets.token_bytes(16), now, str(clerk_user_id)))
        con.commit()
        row = con.execute("SELECT id, name FROM workers WHERE clerk_user_id=?",
                          (str(clerk_user_id),)).fetchone()
        return {"id": row["id"], "name": row["name"]}
    finally:
        con.close()


def get_clerk_user(clerk_user_id):
    con = _connect()
    try:
        row = con.execute("SELECT * FROM clerk_users WHERE clerk_user_id=?", (str(clerk_user_id),)).fetchone()
        return dict(row) if row else None
    finally:
        con.close()
