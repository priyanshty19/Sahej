#!/usr/bin/env python3
"""
Clerk integration for Sahej — backend session verification + profile fetch.

Clerk owns authentication (ASHA = username/password, marketplace = phone/OTP);
this module lets the Python server trust a Clerk session and mirror the user
into Supabase. Two things happen server-side:

  verify_session_token(jwt)  -> claims   (networkless RS256 check via Clerk's
                                          JWKS; raises ClerkError if invalid)
  fetch_clerk_user(user_id)  -> profile  (username/phone/email/name/role, read
                                          from Clerk's Backend API with the
                                          secret key, for the Supabase mirror)

Config comes from the environment (never hard-coded):
  CLERK_PUBLISHABLE_KEY   pk_test_… / pk_live_…  (also handed to the browser)
  CLERK_SECRET_KEY        sk_test_… / sk_live_…  (backend only)
The Clerk instance / JWKS URL / issuer are derived from the publishable key.
"""
import base64
import json
import os
import ssl
import urllib.request

try:
    import jwt  # PyJWT (+cryptography) — verifies Clerk's RS256 session tokens
except Exception:  # noqa: BLE001 — import guarded so the app still boots without Clerk
    jwt = None

PUBLISHABLE_KEY = os.environ.get("CLERK_PUBLISHABLE_KEY", "").strip()
SECRET_KEY = os.environ.get("CLERK_SECRET_KEY", "").strip()
BACKEND_API = os.environ.get("CLERK_BACKEND_API", "https://api.clerk.com/v1").rstrip("/")


class ClerkError(ValueError):
    """User-safe Clerk auth error."""


def frontend_api():
    """Derive the instance's Frontend API host from the publishable key.

    pk_<env>_<base64("<host>$")> -> "<host>" e.g. eager-dane-91.clerk.accounts.dev
    """
    if not PUBLISHABLE_KEY:
        return ""
    try:
        b64 = PUBLISHABLE_KEY.split("_", 2)[2]
        dec = base64.b64decode(b64 + "==").decode("utf-8")
        return dec.rstrip("$")
    except Exception:  # noqa: BLE001
        return ""


def is_configured():
    return bool(PUBLISHABLE_KEY and SECRET_KEY and jwt is not None)


def issuer():
    fa = frontend_api()
    return f"https://{fa}" if fa else ""


def jwks_url():
    fa = frontend_api()
    return f"https://{fa}/.well-known/jwks.json" if fa else ""


def _ctx():
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:  # noqa: BLE001 — fall back to the system trust store
        return ssl.create_default_context()


_jwk_client = None


def _client():
    global _jwk_client
    if _jwk_client is None:
        _jwk_client = jwt.PyJWKClient(jwks_url(), ssl_context=_ctx())
    return _jwk_client


def verify_session_token(token, signing_key=None):
    """Verify a Clerk session JWT and return its claims. Networkless once the
    JWKS is cached. `signing_key` may be injected for offline tests."""
    if jwt is None:
        raise ClerkError("Clerk auth unavailable (PyJWT not installed)")
    if not token:
        raise ClerkError("no session token")
    key = signing_key
    if key is None:
        try:
            key = _client().get_signing_key_from_jwt(token).key
        except Exception as e:  # noqa: BLE001
            raise ClerkError(f"could not load signing key: {e}")
    try:
        return jwt.decode(
            token, key, algorithms=["RS256"], issuer=issuer(), leeway=30,
            options={"verify_aud": False, "require": ["exp", "iat", "sub"]})
    except Exception as e:  # noqa: BLE001 — PyJWT raises several types
        raise ClerkError(f"invalid session: {e}")


def fetch_clerk_user(user_id):
    """Read a user's profile from Clerk's Backend API for the Supabase mirror.
    Role is public_metadata.role if set, else inferred: username -> asha,
    otherwise (phone/OTP) -> consumer."""
    if not SECRET_KEY:
        raise ClerkError("CLERK_SECRET_KEY not configured")
    req = urllib.request.Request(f"{BACKEND_API}/users/{user_id}",
                                 headers={"Authorization": f"Bearer {SECRET_KEY}"})
    try:
        with urllib.request.urlopen(req, timeout=12, context=_ctx()) as r:
            u = json.load(r)
    except Exception as e:  # noqa: BLE001
        raise ClerkError(f"could not fetch Clerk user: {e}")
    username = u.get("username") or ""
    emails = u.get("email_addresses") or []
    phones = u.get("phone_numbers") or []
    email = emails[0].get("email_address") if emails else ""
    phone = phones[0].get("phone_number") if phones else ""
    name = " ".join(x for x in [u.get("first_name"), u.get("last_name")] if x).strip()
    role = (u.get("public_metadata") or {}).get("role") or ("asha" if username else "consumer")
    return {"clerk_user_id": user_id, "username": username, "email": email,
            "phone": phone, "name": name or username, "role": role}
