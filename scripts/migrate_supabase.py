#!/usr/bin/env python3
# Copyright (C) 2026 Mugumya Zeal. All rights reserved.

"""Copy AgriBridge users and data from the OLD shared Supabase project to a NEW one.

Run on your own machine; nothing passes through any third party.
  pip install psycopg2-binary requests
  set OLD_DB_URL=postgresql://postgres.<oldref>:<pw>@<pooler-host>:5432/postgres
  set NEW_DB_URL=postgresql://postgres.<newref>:<pw>@<pooler-host>:5432/postgres
  set NEW_SUPABASE_URL=https://<newref>.supabase.co
  set NEW_SERVICE_KEY=<new project service_role / secret key>
  python scripts/migrate_supabase.py            # dry run: counts only, writes nothing
  python scripts/migrate_supabase.py --apply    # does the copy

Safe to re-run: existing users/rows are skipped (ON CONFLICT DO NOTHING).
Only accounts that belong to AgriBridge are copied (ids found in farmers, buyers or
supplier_products.supplier_id); other apps' users in the shared project are left behind.
Passwords are carried over as bcrypt hashes, so people keep their existing password.
One-time codes (otp_code) are never copied.
"""
from __future__ import annotations

import argparse
import os
import sys

import psycopg2
import psycopg2.extras as pgx
import requests

# Parents before children (foreign keys). platform_config/ussd_sessions/fraud_flags are skipped.
TABLES = ["farmers", "buyers", "listings", "animal_listings", "market_prices", "training_videos",
          "supplier_products", "cart_sessions", "supply_orders", "disease_reports", "vet_bookings",
          "community_posts", "contact_messages", "price_alerts", "orders", "deliveries",
          "reviews", "payouts"]
NEVER_COPY = {"otp_code", "otp_expires_at"}


def need(name: str) -> str:
    v = os.environ.get(name, "").strip()
    if not v:
        sys.exit(f"Missing environment variable {name}")
    return v


def copy_users(old, base, key, apply):
    with old.cursor(cursor_factory=pgx.RealDictCursor) as c:
        c.execute("""select id from public.farmers union select id from public.buyers
                     union select supplier_id from public.supplier_products where supplier_id is not null""")
        ids = [r["id"] for r in c.fetchall()]
        c.execute("""select id, email, phone, encrypted_password, email_confirmed_at, phone_confirmed_at,
                            raw_user_meta_data, raw_app_meta_data
                     from auth.users where id = any(%s::uuid[])""", (ids,))
        users = c.fetchall()
    print(f"auth users to copy: {len(users)} (of {len(ids)} AgriBridge ids; "
          f"{len(ids) - len(users)} have no auth account)")
    if not apply:
        return
    h = {"apikey": key, "Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    made = skipped = failed = 0
    for u in users:
        uid = str(u["id"])
        if requests.get(f"{base}/auth/v1/admin/users/{uid}", headers=h, timeout=30).status_code == 200:
            skipped += 1
            continue
        app_meta = {k: v for k, v in (u["raw_app_meta_data"] or {}).items() if k not in ("provider", "providers")}
        body = {"id": uid, "user_metadata": u["raw_user_meta_data"] or {}, "app_metadata": app_meta}
        if u["email"]:
            body.update(email=u["email"], email_confirm=bool(u["email_confirmed_at"]))
        if u["phone"]:
            body.update(phone=u["phone"], phone_confirm=bool(u["phone_confirmed_at"]))
        if u["encrypted_password"]:
            body["password_hash"] = u["encrypted_password"]
        r = requests.post(f"{base}/auth/v1/admin/users", headers=h, json=body, timeout=30)
        if r.status_code in (200, 201):
            made += 1
        else:
            failed += 1
            print(f"  FAILED {uid}: {r.status_code} {r.text[:200]}")
    print(f"auth users: created {made}, already there {skipped}, failed {failed}")


def copy_table(old, new, table, apply):
    with new.cursor() as c:
        c.execute("""select column_name, udt_name from information_schema.columns
                     where table_schema='public' and table_name=%s and is_generated='NEVER'
                     order by ordinal_position""", (table,))
        newcols = {n: t for n, t in c.fetchall()}
    if not newcols:
        print(f"{table:18} missing in NEW project - run base-schema.sql first")
        return False
    with old.cursor() as c:
        c.execute("""select column_name from information_schema.columns
                     where table_schema='public' and table_name=%s""", (table,))
        oldcols = {r[0] for r in c.fetchall()}
    cols = [n for n in newcols if n in oldcols and n not in NEVER_COPY]
    with old.cursor() as c:
        c.execute(f"select {', '.join(chr(34)+x+chr(34) for x in cols)} from public.{table}")
        rows = c.fetchall()
    print(f"{table:18} {len(rows)} rows")
    if apply and rows:
        wrapped = [tuple(pgx.Json(v) if newcols[n] in ("json", "jsonb") and v is not None else v
                         for n, v in zip(cols, row, strict=True)) for row in rows]
        with new.cursor() as c:
            pgx.execute_values(
                c, f"insert into public.{table} ({', '.join(chr(34)+x+chr(34) for x in cols)}) "
                   f"values %s on conflict do nothing", wrapped)
        new.commit()
    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="really write (default: dry run)")
    a = ap.parse_args()
    old = psycopg2.connect(need("OLD_DB_URL"))
    new = psycopg2.connect(need("NEW_DB_URL"))
    old.set_session(readonly=True)
    print("MODE:", "APPLY" if a.apply else "DRY RUN (nothing is written)")
    copy_users(old, need("NEW_SUPABASE_URL").rstrip("/"), need("NEW_SERVICE_KEY"), a.apply)
    ok = all([copy_table(old, new, t, a.apply) for t in TABLES])
    if a.apply:
        with old.cursor() as o, new.cursor() as n:
            o.execute("select commission_pct from public.platform_config where id=1")
            row = o.fetchone()
            if row:
                n.execute("update public.platform_config set commission_pct=%s where id=1", (row[0],))
        new.commit()
    print("Done." if ok else "Finished with problems above.")


if __name__ == "__main__":
    main()
