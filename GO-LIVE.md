# AgriBridge — Go-Live Checklist

Everything you (Zeal) need to switch AgriBridge from "built" to "fully live".
Nothing here is code — these are accounts, keys, and toggles only you can set.
Each item says **where** and **why**. Do them in any order; none break the live site.

---

## 1. Supabase — data safety (do first)

| Task | Where | Why |
|------|-------|-----|
| **Turn on backups** | GitHub repo → Settings → Secrets and variables → Actions | You currently have no backups. See section 4. |
| **Leaked-password protection** ✅ (done) | Supabase → Authentication → Policies | Blocks known-breached passwords. |

---

## 2. Render — backend environment variables

Service **agribridge-1** → **Environment** tab → add/confirm each, then "Save Changes"
(Render redeploys automatically).

**Already needed (confirm they're set):**
- `SUPABASE_URL` = https://lvloaweijidgcuezdnmu.supabase.co
- `SUPABASE_KEY` = your Supabase **service_role** key (Supabase → Project Settings → API)
- `JWT_SECRET` = a long random string
- `GROQ_API_KEY` = your Groq key (for the AI assistant)

**Africa's Talking (SMS + USSD) — for real messages:**
- `AT_USERNAME` = your live username (not `sandbox`) once approved
- `AT_API_KEY` = your live API key
- `AT_SMS_SENDER` = your approved sender ID (e.g. `AgriBridge`)

> Until Africa's Talking is out of sandbox, order SMS is a safe no-op — the code is
> already wired and starts sending automatically once these are live keys.

**Payments — Flutterwave (only when you're ready to accept online money):**
- `FLW_SECRET_KEY` = Flutterwave → Settings → API Keys → Secret Key
- `FLW_WEBHOOK_HASH` = any secret string you invent (also paste it in Flutterwave, see §5)
- `PUBLIC_BASE_URL` = https://agribrige.com

> Leave the Flutterwave vars empty to keep online payments OFF. With them empty,
> checkout uses Cash on Delivery / pay-farmer-direct exactly as today.

---

## 3. Secrets to rotate (anything shared in plain text is compromised)

- **Groq key** — rotated ✅ (you changed it). Make sure the new one is in Render as `GROQ_API_KEY`.
- **Supabase service key** — if it was ever pasted anywhere public, rotate it in
  Supabase → Project Settings → API, then update `SUPABASE_KEY` in Render.

---

## 4. Backups (free, no paid plan) — GitHub secrets

GitHub → repo **agribridge** → Settings → Secrets and variables → Actions → New repository secret:

1. `SUPABASE_DB_URL` — Supabase dashboard → green **Connect** button (top bar) → change the
   **Client** dropdown to a connection-string view → **Session pooler** tab → copy the URI.
   Replace `[YOUR-PASSWORD]` with your DB password (reset it on Settings → Database if unknown — safe).
2. `BACKUP_PASSPHRASE` — any strong phrase you save somewhere safe (needed to open a backup).

Then: GitHub → **Actions** tab → **Database backup** → **Run workflow** to test.
Runs automatically every Sunday. Restore instructions are in
`.github/workflows/db-backup.yml`.

---

## 5. Flutterwave setup (only when accepting online payments)

1. Create an account at **flutterwave.com** (sandbox works immediately; live needs business/KYC).
2. Copy your **Secret Key** → put it in Render as `FLW_SECRET_KEY` (see §2).
3. Invent a webhook secret → put the **same value** in Render `FLW_WEBHOOK_HASH` **and** in
   Flutterwave → Settings → **Webhooks** → "Secret hash".
4. Flutterwave → Settings → Webhooks → **URL**:
   `https://agribridge-1-og7a.onrender.com/api/pay/webhook/flutterwave`
5. **Test in sandbox first**: place an order choosing MoMo/Airtel → you're sent to Flutterwave →
   pay with a test number → you return to the site and the order flips to **paid**.
6. Only after sandbox works, switch to **live** Flutterwave keys.

> Checkout automatically detects when Flutterwave is enabled. No code change needed —
> add the keys and online payment turns on; remove them and it turns off.

### Optional: on-page card fields (direct charge)

By default the **Card** option sends buyers to Flutterwave's hosted, PCI-compliant
card page. If you specifically need card fields inside our checkout, direct
charging can be switched on — but raw card data then reaches this server, so the
deployment and your Flutterwave account must be **PCI DSS attested**:

1. Flutterwave → Settings → API → copy the **Public key** and **Encryption key**.
2. In Render add `FLW_PUBLIC_KEY`, `FLW_ENCRYPTION_KEY` (must be exactly 24
   characters), and `FLW_DIRECT_CARD=1`.
3. Redeploy. The Card option now shows card number (4-4-4-4), MM/YY, CVC and
   cardholder name. Card data is encrypted with 3DES-ECB before it leaves the
   server, is never logged or stored, and OTP/3-D Secure prompts are handled.
4. Test in Flutterwave sandbox first (test card `4242 4242 4242 4242`, any future
   expiry, CVC `123`). Remove `FLW_DIRECT_CARD` to fall back to the hosted page.

---

## 6. Email receipts (Resend) — optional

Booking + payment confirmation emails are sent by the Supabase Edge Function
`send-receipt`. It's a **safe no-op until you add an email key** — no emails send,
nothing breaks. To turn it on:

1. Create a free account at **resend.com** and verify a sending domain (or use their
   test sender to start).
2. Copy your **API key**.
3. Supabase dashboard → **Edge Functions** → **Manage secrets** (or Project Settings →
   Edge Functions → Secrets) → add:
   - `RESEND_API_KEY` = your Resend key
   - `RESEND_FROM` = e.g. `AgriBridge <orders@yourdomain.com>` (must be a verified sender)

That's it — booking emails send from the web app, payment receipts send from the
payment webhook, both automatically.

---

## 7. Admin dashboard

A private admin panel is live at **https://agribrige.com/admin.html**.

- Log in with your **admin password** = the `ADMIN_PASSWORD` env var in Render.
- **⚠️ Change it now:** it defaults to `agribridge2026`. In Render → `agribridge-1` →
  Environment → set `ADMIN_PASSWORD` to something strong.
- Shows totals (users, listings, orders, paid revenue), all **orders** (with a status
  dropdown you can change), **users**, and **listings**.
- The page is marked `noindex` and every action requires the admin token — but treat
  the URL + password as sensitive.

> SMS receipts (buyer confirmation on booking + payment) also go through Africa's
> Talking — they start sending once §2's AT live keys are set.

---

## 8. Still to come (future phases — not blocking launch)

- **Payout to farmers on delivery** + escrow hold (needs your commission % and Flutterwave payouts).
- **Pesapal / MTN direct** payment options (wired into the router; completed against their sandboxes).

---

_Health check any time:_ `https://agribridge-1-og7a.onrender.com/` should return **200**.
_Payment status:_ `https://agribridge-1-og7a.onrender.com/api/pay/providers` shows which methods are live.
_Email status:_ receipts send only once `RESEND_API_KEY` is set (see §6); otherwise a safe no-op.


---

## Legal & consent rollout (migration 0007)

Do these in order. Details: `docs/LEGAL_CONSENT_IMPLEMENTATION.md`.

1. **Back up** the Supabase database (Dashboard -> Database -> Backups, or `pg_dump`).
2. Apply `supabase/migrations/0007_legal_consent.up.sql` to a **staging** project, then production. Roll back with the `.down.sql` only if no one has accepted anything yet (it deletes acceptance evidence).
3. Deploy the API (Render) and the web app (`static/`, including `legal/` and `business-config.js`). Cloudflare may need a cache purge for `legal/*`, `business-config.js` and `sw.js`.
4. Run `python scripts/publish_legal.py --actor <admin uuid>` with `SUPABASE_URL` and `SUPABASE_KEY` set.
5. Smoke test: register a test farmer (box unticked = blocked; ticked = account), sign in as an older account (review screen appears once), open Settings -> Privacy and legal, send a privacy request while signed out.
6. Only then set `ENFORCE_LEGAL_ACCEPTANCE=true` on Render.

Publishing a new document version: see `legal/README.md`.
