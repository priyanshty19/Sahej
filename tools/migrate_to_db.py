#!/usr/bin/env python3
"""
Seed the database (Supabase Postgres, or local SQLite) from the bundled JSON.

The JSON files under data/ are the version-controlled *source*; this script
loads them into the tables that store.py owns so that, at runtime, catalog.py
and engine.py read the catalog and knowledge bases from the database instead of
disk. Re-runnable: it replaces the scheme rows and upserts the reference docs.

Usage:
    # local SQLite (dev): writes to data/sahej.db
    python3 tools/migrate_to_db.py

    # Supabase / any Postgres: point DATABASE_URL at the pooled connection string
    DATABASE_URL='postgresql://...pooler.supabase.com:6543/postgres' \\
        python3 tools/migrate_to_db.py

Verify afterwards:
    python3 tools/migrate_to_db.py --check
"""
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
DATA = os.path.join(ROOT, "data")

import store  # noqa: E402 — needs ROOT on the path first


def _load(name):
    with open(os.path.join(DATA, name), encoding="utf-8") as f:
        return json.load(f)


def migrate():
    catalog = _load("catalog.json")
    schemes = [(s["id"], s, "catalog") for s in catalog["schemes"]]
    n = store.replace_schemes(schemes)
    store.upsert_reference("catalog_meta",
                           {"version": catalog.get("version"), "as_of": catalog.get("as_of")})

    refs = {
        "childbirth_schemes": _load("childbirth_schemes.json"),
        "death_schemes": _load("death_schemes.json"),
        "states": _load("states.json"),
    }
    for name, doc in refs.items():
        store.upsert_reference(name, doc)

    backend = "Postgres" if store._PG else f"SQLite ({store.DB_PATH})"
    print(f"Seeded {backend}:")
    print(f"  schemes         : {n} catalog rows")
    for name, doc in refs.items():
        extra = f" ({len(doc.get('schemes', []))} schemes)" if "schemes" in doc else ""
        print(f"  reference_docs  : {name}{extra}")
    print("Done. catalog.py / engine.py will now read from the database.")


def check():
    ready = store.content_ready()
    print(f"content_ready() -> {ready}")
    if not ready:
        print("No schemes seeded yet — run without --check to migrate.")
        return
    schemes = store.all_schemes(source="catalog")
    meta = store.get_reference("catalog_meta") or {}
    print(f"schemes in DB   : {len(schemes)} (catalog v{meta.get('version')})")
    for name in ("childbirth_schemes", "death_schemes", "states"):
        doc = store.get_reference(name)
        ok = "OK" if doc else "MISSING"
        print(f"reference '{name}': {ok}")


if __name__ == "__main__":
    if "--check" in sys.argv[1:]:
        check()
    else:
        migrate()
