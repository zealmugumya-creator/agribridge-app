# AgriBridge Uganda

Farm-to-table agri-tech platform connecting Ugandan smallholder farmers directly to
buyers — crops **and** livestock — with live market prices, an AI crop/animal doctor,
delivery tracking, reviews, mobile-money payments, and USSD access for basic phones.

- **Live site:** https://agribrige.com
- **Backend API:** https://agribridge-1-og7a.onrender.com
- **USSD:** `*789#`

> New here? Read this file for how the system works, then **GO-LIVE.md** for the
> accounts/keys/toggles needed to turn everything on.
>
> Engineering docs: **docs/PRODUCTION_AUDIT.md** (findings + evidence),
> **docs/IMPLEMENTATION_STATUS.md** (12-phase progress + verification boundary),
> **docs/OPERATIONS_SETUP.md** (activate every integration, run migrations/worker),
> **docs/PLAY_STORE_RELEASE.md** (Android build + Play submission),
> **docs/RELEASE_READINESS_REPORT.md** (final report + verification boundary).

---

## 1. Architecture at a glance

```
        Buyers / Farmers (phone or web)
                    │
        ┌───────────┴────────────┐
        │                        │
   Web app (PWA)            USSD / SMS
   static/index.html        *789#
        │                        │
        │ (direct, PostgREST)    │ (HTTP)
        ▼                        ▼
   Supabase                 Flask backend (app.py)
   Postgres + Auth          on Render
   + Storage + RLS          · USSD + SMS (Africa's Talking)
        ▲                   · AI assistant (Groq)
        │                   · Payments (Flutterwave)
        └───────── service role ─┘
```

**Key idea:** the web app talks **directly** to the database through Supabase's
auto-generated REST API (PostgREST), using a *publishable* key that is meant to be
public. Security is enforced entirely by **Row Level Security (RLS)** policies in the
database — not by hiding the key. The Flask backend handles only the things a browser
can't: USSD/SMS, the AI proxy, and payment webhooks.

---

## 2. Tech stack

| Layer | Technology | Hosting |
|-------|-----------|---------|
| Web app | Single-file HTML/CSS/JS + PWA (`manifest.json`, `sw.js`) | Cloudflare Pages (from GitHub) |
| Database / Auth / Storage | Supabase (Postgres, GoTrue auth, Storage, PostgREST) | Supabase cloud (project `lvloaweijidgcuezdnmu`) |
| Backend API | Python Flask (`app.py`) | Render (service `agribridge-1`) |
| SMS / USSD | Africa's Talking | — |
| AI assistant | Groq (`llama-3.3-70b-versatile`) | via backend |
| Payments | Flutterwave (pluggable; Pesapal/MTN ready) | via backend |

Repo layout:
```
static/index.html   ← the entire web app (UI + logic)
static/manifest.json, static/sw.js, static/icon-*.png  ← PWA
mobile/             ← Capacitor native shell (see docs/PLAY_STORE_RELEASE.md)
app.py              ← Flask backend (USSD, SMS, AI, payments, admin, orders)
services/           ← backend service modules (config, logging, monitoring,
                      supabase_client, orders, matching, notifications,
                      discord, push, account, worker)
supabase/migrations/← reversible SQL migrations 0001–0012 (up + down)
tests/              ← pytest suite (85 tests)
requirements.txt    ← Python runtime deps
requirements-dev.txt, pytest.ini, ruff.toml  ← dev/lint/test config
render.yaml         ← Render service config
.github/workflows/  ← ci.yml (lint+test) and db-backup.yml (weekly encrypted backup)
docs/               ← PRODUCTION_AUDIT, IMPLEMENTATION_STATUS, PLAY_STORE_RELEASE,
                      OPERATIONS_SETUP
.env.example        ← every env var NAME (no secrets)
GO-LIVE.md          ← accounts/keys/toggles checklist
```

---

## 3. Data model (AgriBridge tables)

> The Supabase project is **shared** with other apps. AgriBridge owns only the tables
> below; ignore `rootnet.*`, `zenith_*`, `fiscal_leads`, `inquiries`, `custodians`,
> `hostels`, `universities`.

| Table | Purpose |
|-------|---------|
| `farmers` | User profiles (farmers, buyers, vendors). Row id = the auth user id. |
| `listings` | Crop/produce listings. `farmer_id` = owner. |
| `animal_listings` | Livestock listings. `farmer_id` = owner. |
| `orders` | One row per item ordered. `buyer_id`, `farmer_id`, `payment_ref`, `tracking_code`. |
| `deliveries` | Created when an order is confirmed; drives tracking. |
| `reviews` | Buyer reviews of a delivered order. Powers `farmer_ratings`. |
| `payouts` | Ledger of money owed to farmers (gross, commission, net). |
| `platform_config` | Single row; `commission_pct` you edit to set the platform fee. |
| `market_prices` | Live crop prices. |
| `price_alerts`, `cart_sessions`, `contact_messages`, `disease_reports`, `supply_orders`, `training_videos`, `vet_bookings`, `fraud_flags`, `ussd_sessions` | Supporting features. |

**Views:** `farmer_ratings` (avg rating + count per farmer).

**Triggers:**
- `handle_new_user` (on `auth.users`) — creates the `farmers` profile on signup,
  server-side, even before email confirmation.
- `sync_delivery_on_order` (on `orders`) — on **confirmed** creates the delivery; on
  **delivered + paid** records a `payouts` row (commission from `platform_config`); on
  **cancelled** marks the delivery failed.

---

## 4. Security model

- **RLS is the boundary.** Every AgriBridge table has owner-scoped policies:
  - Marketplace (`listings`, `animal_listings`) — **public read**, writes only by the
    owning farmer (`auth.uid() = farmer_id`).
  - PII (`orders`, `buyers`, `payouts`) — readable **only** by the owner (buyer/farmer).
  - Profiles created server-side by trigger; the public INSERT path is closed.
- **Keys:**
  - *Publishable/anon key* — safe to ship in the web app. RLS restricts what it can do.
  - *Service role key* — backend only (`SUPABASE_KEY` in Render). Never in the web app.
- **Login required** to place an order or create a listing.
- **Orders are server-authoritative** — `POST /api/orders` recomputes the price from
  the source catalogue (`listings`, `animal_listings` or `supplier_products`) and
  reserves/decrements stock atomically via a `SECURITY DEFINER` Postgres RPC
  (`create_order_atomic`), so a tampered client cannot set its own total or oversell.
  Direct authenticated INSERTs/UPDATEs into `orders` are closed by RLS only after
  migration **0002** is applied; until then the API path is enforced in the client
  and backend, not by the database.
- **Admin authority is server-controlled** — a `user_roles` table + `is_admin()`
  predicate, not a client-side email check or hidden button. The owner is assigned by
  **verified UUID** (`assign_role`), and sensitive actions write to `admin_audit_log`.
- **Reviews are verified** — only a buyer with a *delivered* order from that farmer can review it.
- **Payment webhooks** verify a shared secret **and** re-verify the transaction with the
  gateway server-side before marking an order paid.

---

## 5. Key user flows

**Sign up →** Supabase Auth creates the user → `handle_new_user` trigger writes the
`farmers` profile (name, phone, district from signup) → session token drives all
subsequent DB calls.

**List produce/animal →** logged-in farmer submits → row inserted with
`farmer_id = their id` → appears in the public marketplace.

**Place order →** buyer checks out → one `orders` row per item, carrying `buyer_id`,
`farmer_id`, a shared `payment_ref`, and a `tracking_code` → if an online gateway is
live and a non-cash method is chosen, buyer is redirected to pay.

**Fulfilment →** farmer sees the order in their dashboard → **Confirm** (creates the
delivery) → **Mark Delivered** (records the payout). Buyer sees live status + a **Track**
button; after delivery they can **Rate** the seller.

**Payments →** `/api/pay/initiate` starts a gateway payment; the gateway's webhook
(`/api/pay/webhook/flutterwave`) confirms and flips the order to `paid`.

---

## 6. Backend API (app.py)

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/` , `/health` | GET | Real health check — DB round-trip; 200 or 503 `degraded` |
| `/health/live` | GET | Process liveness (independent of dependencies) |
| `/api/ussd` | POST | Africa's Talking USSD handler (`*789#`) |
| `/api/ai` | POST | AI assistant (Groq); returns `{reply:null}` if unconfigured |
| `/api/crop-doctor` | POST | Crop disease helper |
| `/api/orders` | POST | **Server-authoritative order create** — recomputes price, reserves stock atomically, idempotent (fixes C1/C2/C4); also sends the farmer SMS |
| `/api/orders/<id>/status` | POST | Order state transition via the Postgres state machine (owner/admin only) |
| `/api/requests` | POST | Create a **purchase request** (distinct from a paid order) for matching |
| `/api/offers/<id>/accept` | POST | Farmer accepts an offer — atomic reservation, expires sibling offers |
| `/api/offers/<id>/reject` | POST | Farmer rejects an offer |
| `/api/worker/match` | POST | Admin-only manual matching pass (also run by the worker) |
| `/api/admin/devices` | POST/DELETE | Register/unregister an admin FCM push token (verified admins only) |
| `/api/account/delete` | POST | Self-service account deletion / erasure (Play requirement, H6) |
| `/api/pay/providers` | GET | Which payment methods are enabled |
| `/api/pay/initiate` | POST | Start a payment (Flutterwave live; others ready) |
| `/api/pay/webhook/flutterwave` | POST | Verified payment confirmation → mark order paid |
| `/api/admin/*` | — | Admin login/stats/listings/orders/farmers |

> The durable notification/matching **worker** runs separately: `python -m services.worker`
> (deploy as a Render Background Worker with the same env). See `docs/OPERATIONS_SETUP.md`.

---

## 7. Environment variables (set in Render)

See **GO-LIVE.md** for the full list with where/why. Summary:
`SUPABASE_URL`, `SUPABASE_KEY` (service role), `JWT_SECRET`, `GROQ_API_KEY`,
`AT_USERNAME`, `AT_API_KEY`, `AT_SMS_SENDER`,
`FLW_SECRET_KEY`, `FLW_WEBHOOK_HASH`, `PUBLIC_BASE_URL`.

Backups use two **GitHub** secrets: `SUPABASE_DB_URL`, `BACKUP_PASSPHRASE`.

---

## 8. Setting the platform commission

Supabase → **Table editor** → `platform_config` → set `commission_pct` (e.g. `5` = 5%).
It applies to **future** payouts immediately — no code change or redeploy. Payout rows
are written to the `payouts` ledger as orders are delivered+paid; actual disbursement to
farmers (Flutterwave transfers) is a later phase.

---

## 9. Deploying changes

- **Web app / backend:** push to the `main` branch of `zealmugumya-creator/agribridge`.
  Cloudflare Pages redeploys `static/`; Render redeploys `app.py` automatically.
- **Database:** schema changes are applied as Supabase migrations.
- **Backups:** automatic every Sunday; run manually via GitHub → Actions → "Database backup".

---

## 10. Operations & health

- API up? — `GET https://agribridge-1-og7a.onrender.com/` → 200.
- Payments enabled? — `GET /api/pay/providers`.
- Security review — Supabase → Advisors → Security.
- Data safety — confirm the weekly backup workflow is green in GitHub → Actions.

## Legal documents

Published documents live in `legal/source/` and are built to `static/legal/` (see `legal/README.md`). Official contact details come from `services/business.py`. Open legal items: `docs/LEGAL_PENDING.md`; implementation notes: `docs/LEGAL_CONSENT_IMPLEMENTATION.md`.
