# AgriBridge — Pesapal API 3.0 Payments

**Last updated:** 2026-10-22
**Status:** implemented in the repository and covered by tests. Migration
`0017_pesapal_payments` **was applied to the live project on 2026-10-22** and
read-only probes confirm all three tables exist and reject `anon` with `42501`.
No live Pesapal credentials are configured, so **no live transaction has been
processed or verified**. No claim in this document should be read as live
payment evidence.

This build adds Pesapal as a hosted-checkout provider and records Pesapal
recurring/subscription charges when the gateway reports them. The server is the
only party holding the consumer key and secret.

---

## 1. What is implemented

| Capability | State |
|---|---|
| Hosted checkout redirect (card / mobile money / bank, as enabled on the Pesapal account) | Implemented |
| Server-side transaction verification with `GetTransactionStatus` | Implemented |
| Order marked paid only after amount + currency + reference checks | Implemented |
| Idempotent ledger receipt `payment:<checkout_ref>` | Implemented |
| Browser return route and IPN webhook | Implemented |
| Recurring/subscription charges recorded and posted to suspense | Implemented |
| Admin Finance → Recurring view | Implemented (requires migration 0017) |
| Creating or cancelling a subscription mandate | **Not implemented** — the mandate is created by the customer inside Pesapal's hosted flow |
| Scheduling recurring charges from AgriBridge | **Not implemented** — the mandate/schedule is managed by Pesapal; AgriBridge has no scheduler of its own and only records each charge it is notified about |
| Allocating a recurring charge to an order | **Not implemented** — money stays in `SUSPENSE_OVERPAYMENT` until an operator allocates it |
| Refunds through Pesapal | **Not implemented** |

Pesapal's recurring feature is controlled by the Pesapal account. The code does
not assume it is enabled; a normal one-off checkout behaves exactly the same if
the account has no subscription support.

---

## 2. Checkout flow

```
Buyer SPA
  POST /api/pay/initiate {provider: "pesapal", order_ref, amount, email, phone}
    → verified Supabase session
    → order belongs to caller
    → amount floor = server-computed order total
  RequestToken (cached in-process for 240 s)
  RegisterIPN (once; id persisted in payment_gateway_state)
  SubmitOrderRequest
      id              = "<order_ref>~<random>"   (unique attempt)
      currency        = UGX
      amount          = integer UGX
      account_number  = "<order_ref>"             (stable recurring reference)
      callback_url    = <callback base>/api/pay/pesapal/callback
      notification_id = <IPN id>
    → { redirect_url, order_tracking_id }
Browser follows the Pesapal-hosted page
Pesapal calls GET/POST /api/pay/webhook/pesapal          (IPN)
Pesapal redirects the browser to /api/pay/pesapal/callback
Both routes call GetTransactionStatus(orderTrackingId)
  completed + echoed reference matches?
    → _mark_checkout_paid()
        → orders.payment_status = 'paid'
        → ledger journal payment:<order_ref>
        → receipt / notifications (first successful mark only)
Browser is redirected to https://agribrige.com/?pay=done&provider=pesapal&status=...
```

The callback and IPN parameters are **hints only**. Amount, currency, completion
state and merchant reference are read from `GetTransactionStatus`; a forged
request cannot mark an order paid.

`POST /api/pay/initiate` still enforces the normal contract: a caller can only
pay their own checkout, and a client-supplied amount may add a delivery fee but
can never lower the server-computed order total. `POST /api/pay/providers`
advertises `pesapal: true` only when both consumer credentials exist.

---

## 3. Configuration

| Variable | Required | Meaning |
|---|---|---|
| `PESAPAL_CONSUMER_KEY` | yes | Pesapal API key. Server-only. |
| `PESAPAL_CONSUMER_SECRET` | yes | Pesapal API secret. Server-only. |
| `PESAPAL_ENV` | no | `sandbox` (default) or `live`. Any value other than `live`/`production`/`prod` uses the sandbox host. |
| `PESAPAL_CALLBACK_BASE_URL` | recommended | Public HTTPS origin of the **Flask API**, not the static site. Defaults to `https://agribridge-1-og7a.onrender.com`. |
| `PESAPAL_IPN_ID` | no | Pin an IPN id already registered in the Pesapal dashboard. When empty, the backend registers one and persists it. |
| `PUBLIC_BASE_URL` | existing | Customer-facing site used for the final browser redirect. |

Base URLs used by the client (`services/pesapal.py`):

* sandbox — `https://cybqa.pesapal.com/pesapalv3`
* live — `https://pay.pesapal.com/v3`

Credentials and IPN ids are **per environment**. A sandbox key or a pinned
sandbox `PESAPAL_IPN_ID` must not be carried into live.

With either credential missing:

* `/api/pay/providers` reports `pesapal: false` and the SPA hides the option;
* `/api/pay/initiate` still returns a controlled `enabled: false` response if a
  stale client asks for it;
* no token request, IPN registration or order submission is attempted.

---

## 4. Migration 0017

`supabase/migrations/0017_pesapal_payments.up.sql` creates three
service-role-only tables and widens the order payment-method constraint.

| Table | Purpose |
|---|---|
| `payment_gateway_state` | One row per provider. Stores the registered `ipn_id` and `ipn_url` so every API worker reuses the same IPN. |
| `payment_subscriptions` | One row per Pesapal recurring correlation id, with account reference, optional buyer, last amount and status. |
| `subscription_charges` | One row per recurring charge, keyed uniquely by `(provider, provider_tracking_id)`, with the ledger reference once posted. |

All three have RLS enabled, `service_role` policies, and all privileges revoked
from `anon` and `authenticated`. The migration also drops old
`orders.payment_method` check constraints and adds a `NOT VALID` constraint that
accepts `mtn_momo, airtel_money, cash, bank, card, momo, airtel, pesapal,
direct, mobile_money`. Existing rows are not rewritten.

`0017_pesapal_payments.down.sql` removes the tables and restores the legacy
four-value constraint. Dropping the tables erases subscription/charge history;
it is a development rollback, not a financial archive operation.

**Apply order:** after 0015 and 0016 (see `docs/OPERATIONS_SETUP.md` §2). The
migration is safe to re-run: all objects are guarded. On the live project, 0015,
0016 and 0017 are all applied and the API/SPA carrying this code were deployed
on 2026-10-22 (`orders.source_ref` returns 200, confirming 0015). Livestock and
supplier checkout now price via the category-aware `create_order_atomic`.

Without 0017:

* the IPN id is cached in process memory only and the state write is skipped
  with a warning; a restart registers a new id;
* recurring notifications cannot be recorded, so the webhook returns an
  unsuccessful acknowledgement and Pesapal retries;
* Admin → Finance → Recurring returns `503 recurring_not_initialized` with the
  migration name in `fix`.

---

## 5. IPN and callback registration

The backend registers the IPN through `POST /api/URLSetup/RegisterIPN` with:

* URL — `PESAPAL_CALLBACK_BASE_URL + /api/pay/webhook/pesapal`
* method — `POST`

The returned id is stored in `payment_gateway_state` and reused. If
`PESAPAL_IPN_ID` is set, registration is skipped entirely.

Operational notes:

* The API origin must be publicly reachable over HTTPS. The marketing site is a
  static Cloudflare Worker that does not proxy `/api`, so pointing the callback
  base at `https://agribrige.com` would fail.
* After moving from sandbox to live, clear `PESAPAL_IPN_ID` or replace it with
  the live-environment id; an id from the other environment is not valid.
* With multiple gunicorn workers, two workers may both register an IPN on their
  very first request after a cold boot. The database record converges on the
  last id. To avoid duplicate registrations entirely, pin `PESAPAL_IPN_ID`
  once it is known, or register the endpoint in the Pesapal dashboard first.
* Pesapal expects the acknowledgement as JSON, not an HTTP error. The webhook
  always answers HTTP 200 with:

  ```json
  {
    "orderNotificationType": "IPNCHANGE",
    "orderTrackingId": "<tracking id>",
    "orderMerchantReference": "<checkout ref>",
    "status": 200
  }
  ```

  `status` is `500` when verification or recording failed, which asks Pesapal
  to retry. The HTTP status stays 200 so the body is parsed.

The browser callback route always redirects to `PUBLIC_BASE_URL` with
`?pay=done&provider=pesapal&status=paid|pending|failed|recurring&tx_ref=...`.
It is rate-limited and never returns raw gateway data.

---

## 6. Verification and amount rules

A notification changes state only when all of these hold:

1. `status_code == "1"` or `payment_status_description == "completed"`.
2. The **base** checkout reference (the part before `~`) matches one of the
   echoed `merchant_reference`, `account_number` or `id` values.
3. The order rows exist. If all are already paid, no new receipt or buyer
   notification is sent; the ledger post is still attempted idempotently.
4. The verified currency is explicitly `UGX`.
5. The verified amount is at least the sum of server-computed order totals.

A pending or failed transaction changes nothing; the webhook acknowledges it so
the gateway stops retrying, and the browser shows the failure. Underpayment,
a mismatched reference, a non-UGX currency or an unknown order logs
`payment.amount_mismatch` / `pesapal.unverified_callback` and posts nothing.

Repeated notifications are safe:

* `orders` are marked paid idempotently by `payment_ref`;
* the ledger reference `payment:<checkout_ref>` is unique, so a retry returns
  the existing journal instead of double-posting;
* receipts, Discord messages and in-app notifications are sent only when the
  checkout transitions from unpaid to paid.

If the order is marked paid but the ledger post fails, the payment is **not**
reported as failed. The order stays paid, `payment_ledger_post_failed` is
recorded, Discord is alerted, and the Finance → Reconciliation view reports
`missing_ledger_entry`. That avoids charging the buyer twice over a bookkeeping
fault.

---

## 7. Recurring / subscription charges

Pesapal sends a recurring notification with a non-empty
`subscription_transaction_info` object when it charges a mandate:

```json
{
  "subscription_transaction_info": {
    "correlation_id": "...",
    "account_reference": "...",
    "amount": "..."
  }
}
```

`_record_pesapal_recurring()` then:

1. Rejects non-completed statuses, missing/invalid tracking ids, non-positive
   amounts and any currency other than UGX.
2. Upserts `payment_subscriptions` on `(provider, provider_correlation_id)`,
   storing the account reference and, when the reference matches an order, the
   buyer id.
3. Inserts `subscription_charges` keyed uniquely on
   `(provider, provider_tracking_id)`. A concurrent duplicate is treated as
   already recorded.
4. Posts one ledger journal through `ledger.post_recurring_charge()`.

Journal shape:

```
reference:   recurring:<tracking_id>
kind:        recurring_payment
payment_ref: null
actor:       null
currency:    UGX

debit   PROVIDER_CLEARING      amount
credit  SUSPENSE_OVERPAYMENT   amount
```

No order is created and no farmer is credited: goods for the new cycle have not
been priced or dispatched. The metadata records
`"allocation_policy": "suspense_until_order_allocated"`.

The journal deliberately has `payment_ref = NULL`. Reconciliation groups active
journals by `payment_ref`; reusing the checkout reference would appear as a
duplicate receipt for that checkout. A recurring charge never re-marks the
original order paid.

Failure behaviour:

* The charge row is written **before** the ledger post. If posting fails, the
  charge remains recorded (so the money is not lost), a
  `pesapal.recurring_ledger_failed` event and Discord alert are emitted, and the
  admin Recurring view reports the amount in `paid_amount_without_ledger`.
* Notification failures never affect recording.
* A retried IPN with an existing `ledger_reference` exits without another post.

### Admin view

`GET /api/admin/finance/recurring` (admin token) returns up to 2,000
subscriptions and 5,000 charges plus:

* subscription / active subscription counts;
* charge and paid-charge counts;
* total charged;
* `paid_amount_without_ledger` — paid charges with no ledger reference.

The admin UI exposes this as **Finance → Recurring**. There is no UI to allocate
suspense to an order yet; that is an operator/back-office task tracked in
`docs/FINANCIAL_LEDGER.md` §9.

---

## 8. Sandbox → live checklist

1. Confirm `0017_pesapal_payments.up.sql` is applied (it was applied live on
   2026-10-22; re-running is safe) and that the SQL suite still passes.
2. Obtain **sandbox** consumer key and secret from Pesapal. Set
   `PESAPAL_CONSUMER_KEY`, `PESAPAL_CONSUMER_SECRET`, `PESAPAL_ENV=sandbox`, and
   leave `PESAPAL_IPN_ID` empty.
3. Set `PESAPAL_CALLBACK_BASE_URL` to the public Render API origin. Deploy the
   API.
4. Confirm `GET /api/pay/providers` returns `"pesapal": true`.
5. Buy a low-value sandbox order through the site. Confirm:
   * the order becomes `paid`;
   * one `payment:<checkout_ref>` journal exists and balances;
   * the browser returns with `status=paid`;
   * a replayed IPN does not create a second journal or another receipt.
6. If recurring is enabled on the test account, complete a mandate and confirm
   a `recurring:<tracking_id>` journal debits `PROVIDER_CLEARING` and credits
   `SUSPENSE_OVERPAYMENT`, with `payment_ref` NULL.
7. Switch to the **live** account: replace both credentials, set
   `PESAPAL_ENV=live`, clear or replace `PESAPAL_IPN_ID`, restart.
8. Repeat step 5 with a real small payment before enabling the method for all
   buyers. Only then is a live Pesapal payment verified.

**Do not enable live keys before 0017 is applied** if recurring is expected;
otherwise recurring charges cannot be durably recorded.

---

## 9. Read-only verification queries

Run in the Supabase SQL Editor (service role):

```sql
-- Recent Pesapal receipt journals
select j.reference, j.kind, j.payment_ref, j.status, j.posted_at,
       coalesce(sum(l.amount) filter (where l.side = 'debit'), 0)  as debits,
       coalesce(sum(l.amount) filter (where l.side = 'credit'), 0) as credits
from public.ledger_journals j
left join public.ledger_lines l on l.journal_id = j.id
where j.reference like 'payment:%' or j.reference like 'recurring:%'
group by j.id
order by j.posted_at desc
limit 20;

-- Recurring charges and whether each has a ledger journal
select c.provider_tracking_id, c.account_ref, c.amount, c.currency,
       c.status, c.ledger_reference, c.created_at
from public.subscription_charges c
order by c.created_at desc
limit 50;

-- Paid charges with no ledger reference (should normally be empty)
select count(*) as paid_without_ledger, coalesce(sum(amount), 0) as amount
from public.subscription_charges
where status = 'paid' and coalesce(ledger_reference, '') = '';
```

Expected for a UGX 50,000 checkout with no commission:

```
PROVIDER_CLEARING  debit  50000.00
FARMER_PAYABLE     credit 50000.00
```

Expected for a UGX 20,000 recurring charge:

```
PROVIDER_CLEARING      debit  20000.00
SUSPENSE_OVERPAYMENT   credit 20000.00
```

---

## 10. Failure modes and operator response

| Symptom | Likely cause | Response |
|---|---|---|
| Pesapal option missing on checkout | Either credential empty, or `/api/pay/providers` cached | Set both keys in Render, restart, reload. The provider response is `no-store`; the SPA cache expires after 2 minutes. |
| "Pesapal could not start this payment" | Token/SubmitOrder failure | Check `pesapal.submit_failed` logs and `system_events` `pesapal_initiate_failed`. No order state changed. |
| IPN returns `status: 500` | Status fetch failed, reference mismatch, or 0017 missing for recurring | Check `pesapal.status_unavailable`, `pesapal.unverified_callback`, `pesapal.recurring_*`. Pesapal will retry. |
| Order paid, no ledger journal | Ledger RPC failed | Finance → Reconciliation shows `missing_ledger_entry`; `payment_ledger_post_failed` event. Do not re-charge; backfill only from a verified provider record. |
| Recurring charge missing from admin | 0017 not applied | Apply 0017; `payment_subscriptions` returns 503 `recurring_not_initialized` until then. |
| Recurring amount in `paid_amount_without_ledger` | Charge recorded, journal failed | Investigate `pesapal.recurring_ledger_failed`; post the missing `recurring:<tracking>` journal through the normal ledger path (do not invent an order). |
| Browser shows `status=pending` but money moved | Callback raced the IPN | The IPN/status poll will settle it; verify with the queries above before intervening. |

Log fields intentionally omit keys, tokens, phone numbers and raw gateway
payloads.

---

## 11. Test evidence (repository only)

* `tests/test_pesapal.py` — 19 tests: environment selection, token caching,
  redirect/IPN validation, ownership and amount floor, initiate payload,
  verified-completion rules, underpayment/failure handling, callback vs status
  reference, webhook acknowledgement, recurring suspense posting and
  idempotency, bad money/currency rejection.
* `tests/test_pesapal_ui.py` — static UI/security checks: checkout gating,
  Pesapal panel, return outcomes, admin recurring wiring, route registration,
  and no server secret in browser assets.
* `tests/e2e/test_consent_ui.py` — browser test proving the option is hidden
  until the server enables it and that selecting it shows the hosted-checkout
  explanation.
* `tests/sql/pesapal.test.mjs` — 24 PGlite assertions: migration re-apply,
  payment-method constraint, RLS/deny for anon and authenticated, uniqueness,
  FK behaviour, and down migration.
* Full suite at the time of writing: **417 passed**, including 22 Playwright
  tests; both ruff versions clean.

These results do **not** prove Pesapal works live. That requires the checklist
in §8 and a real verified transaction.
