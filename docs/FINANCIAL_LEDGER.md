# AgriBridge — Financial Ledger

**Last updated:** 2026-10-22
**Migrations:** `0013_ledger_core` (applied live and verified), `0016_farmer_settlements`
(applied live on 2026-10-22; endpoint not yet exercised live),
`0017_pesapal_payments` (applied live on 2026-10-22; no Pesapal credentials)
**Backend:** `services/ledger.py`, wired into `_flw_verify_and_mark`,
`/api/admin/orders/<id>/confirm-delivery`, `/api/admin/payouts/<id>/settle` and
Pesapal recurring notifications in `app.py`

This document describes what the ledger records today, the accounting rules it
enforces, how a failed posting is detected, and what is deliberately not yet
implemented. It does not claim that refunds are live. Farmer settlements are
implemented in code and migration 0016 is applied, but the settle endpoint has
not been exercised against live money. Pesapal recurring charges are implemented
and migration 0017 is applied; the Pesapal gateway itself has no live
credentials in this environment, so no live charge exists.

---

## 1. What is implemented

| Event | Trigger | Ledger effect | Status |
|---|---|---|---|
| Customer payment received | Flutterwave or Pesapal server-side verification (webhook/callback, direct card, OTP completion) | Debit `PROVIDER_CLEARING`; credit `FARMER_PAYABLE`, `CUSTOMER_REFUND_PAYABLE` and/or `SUSPENSE_OVERPAYMENT` | **Implemented** |
| Admin reads journals / balances | `GET /api/admin/finance/*` | Read-only | **Implemented** |
| Journal reversal | `reverse_journal()` SQL function | Mirror entry + audit row | **Database only** — no admin route yet |
| Farmer settlement | Delivered + paid order creates a pending payout; admin settle route with a provider reference | Debit `FARMER_PAYABLE`; credit `COMMISSION_REVENUE` and `PROVIDER_CLEARING`/`COD_CASH_HELD` | **Implemented — 0016 applied live; not exercised live** |
| Pesapal recurring charge | Verified RECURRING IPN carrying `subscription_transaction_info` | Debit `PROVIDER_CLEARING`; credit `SUSPENSE_OVERPAYMENT` | **Implemented — 0017 applied live; no live credentials** |
| Admin reads recurring charges | `GET /api/admin/finance/recurring` | Read-only | **Implemented — requires 0017** |
| Transporter settlement | — | Not posted | **Not implemented** |
| Cash-on-delivery collected | Admin delivery confirmation, order method `cash` | Debit `COD_CASH_HELD`; same credits as a provider receipt | **Implemented** |
| Customer refund execution | — | Refund liability is recognised; money movement is manual | **Not implemented** |

The ledger is the accounting record. The `payouts` table is the operational
settlement record; migration 0016 creates it automatically from delivered, paid
orders and refuses to mark a payout paid until its settlement journal exists.

---

## 2. Chart of accounts

Seeded once by migration 0013. Amounts are `numeric(18,2)`; accounts do not
carry per-currency sub-accounts, so only UGX may be posted.

| Code | Type | Normal side | Meaning |
|---|---|---|---|
| `PROVIDER_CLEARING` | asset | debit | Money received by the payment provider, not yet settled to the bank |
| `COD_CASH_HELD` | asset | debit | Cash collected on delivery, not yet banked |
| `FARMER_PAYABLE` | liability | credit | Owed to farmers |
| `TRANSPORTER_PAYABLE` | liability | credit | Owed to transporters |
| `CUSTOMER_REFUND_PAYABLE` | liability | credit | Owed back to customers |
| `SUSPENSE_OVERPAYMENT` | liability | credit | Unallocated receipts / overpayments |
| `COMMISSION_REVENUE` | revenue | credit | AgriBridge commission |
| `DELIVERY_REVENUE` | revenue | credit | AgriBridge delivery income |
| `LOGISTICS_EXPENSE` | expense | debit | Logistics cost |

---

## 3. Customer-payment posting rules

Every verified receipt produces exactly one journal:

```
reference:   payment:<checkout_ref>
kind:        customer_payment
payment_ref: <checkout_ref>
actor:       null (system event, not an admin action)
currency:    UGX
```

Lines:

| Account | Side | Amount | When |
|---|---|---|---|
| `PROVIDER_CLEARING` | debit | Amount verified by Flutterwave | Always |
| `FARMER_PAYABLE` | credit | Product value of orders whose status is not `cancelled` | If non-zero |
| `CUSTOMER_REFUND_PAYABLE` | credit | Product value of `cancelled` orders in the same checkout | If non-zero |
| `SUSPENSE_OVERPAYMENT` | credit | Verified amount minus order total | If non-zero |

Zero-amount lines are omitted. The database rejects journals with fewer than two
lines, non-positive amounts, more than two decimals, unknown accounts, or
unbalanced debits/credits.

### Amount source

Order value is computed by `services/finance.py:order_total`, which prefers the
server-generated `total_amount` column (`quantity_kg * price_per_kg`), then the
legacy `total_price`, then quantity × price. The payment floor and the ledger
both use this function. A client-supplied amount can never lower the charge.

### Why commission is not recognised at receipt

`platform_config.commission_pct` applies to **payouts**, and no payout exists at
payment time. The full product value is therefore credited to
`FARMER_PAYABLE`. When a settlement is posted, the commission moves from
`FARMER_PAYABLE` to `COMMISSION_REVENUE` and only the net amount leaves through
`PROVIDER_CLEARING`. Recognising commission at receipt *and* at settlement would
overstate revenue. The posting metadata records
`"commission_policy": "recognized_at_settlement"`.

Current live configuration: `platform_config.commission_pct = 0`. No commission
is invented.

### Cancelled and overpaid checkouts

* A cancelled order in a paid checkout is still money received. It is credited
  to `CUSTOMER_REFUND_PAYABLE`, not to the farmer. The order's
  `payment_status` is still set to `paid` because the money did arrive; a later
  refund must reverse the liability.
* Anything received above the sum of order totals goes to
  `SUSPENSE_OVERPAYMENT`. It is never credited to a farmer or to revenue.
* Underpayment fails verification and posts nothing.

### Cash on delivery

`POST /api/admin/orders/<id>/confirm-delivery` first moves the order to
`delivered` through `set_order_status`, then handles payment:

* Already `paid` → nothing further is posted.
* Method `cash` and unpaid → one journal per checkout:

  ```
  reference:   cod:<checkout_ref>
  kind:        cod_collection
  debit:       COD_CASH_HELD   amount = order total
  credit:      FARMER_PAYABLE / CUSTOMER_REFUND_PAYABLE / SUSPENSE_OVERPAYMENT
  ```

  The order is marked `paid` only after the journal posts. A ledger failure
  returns `503` and leaves the order unpaid so the admin can retry; the retry is
  idempotent. The driver is assumed to have collected exactly the order total.
* Any other unpaid method → the order stays `unpaid` and the response says so.
  Confirming a delivery never invents a gateway payment.

Cash held on delivery is an asset until it is banked; a banking transfer is not
implemented yet.

### Pesapal recurring charges

A recurring charge is money received for a new subscription cycle. No goods have
been priced or dispatched for that cycle, so there is no order to credit and no
farmer payable to create. `ledger.post_recurring_charge` therefore posts:

```
reference:   recurring:<pesapal_tracking_id>
kind:        recurring_payment
payment_ref: null
actor:       null

debit:       PROVIDER_CLEARING   amount
credit:      SUSPENSE_OVERPAYMENT  amount
```

Rules:

* Only a **completed** transaction with a positive UGX amount is posted.
* The charge row in `subscription_charges` is written first; a ledger failure
  leaves the money recorded and raises `pesapal.recurring_ledger_failed`.
* `recurring:<tracking_id>` is unique, so a retried IPN never double-posts.
* `payment_ref` is null on purpose: reconciliation groups receipts by that
  column, and a recurring charge is not a checkout receipt.
* The money stays a liability until an operator allocates it to an order. No
  allocation workflow exists yet; do not create a fake order to clear it.
* A recurring IPN never re-marks the original checkout paid.

The admin Finance → Recurring view reports paid charges and the total with no
ledger reference (`paid_amount_without_ledger`). See
[`docs/PAYMENTS_PESAPAL.md`](PAYMENTS_PESAPAL.md) §7.

---

## 4. Farmer settlements (migration 0016)

> **Deployment status:** migration 0016 was applied to the live project on
> 2026-10-22. The settle endpoint has not yet been exercised against a live
> payout, because no live receipt exists to settle in this environment.

### Payout creation

`create_payout_for_order()` (trigger `orders_create_payout`, after insert or
update of `status`/`payment_status`) creates **one** pending `payouts` row when
an order is `delivered` **and** `payment_status = 'paid'` and has a
`farmer_id`. It:

* takes gross from the generated `total_amount` (falling back to `total_price`);
* snapshots `platform_config.commission_pct`, clamped to 0–100;
* computes `commission_amount = round(gross * pct / 100, 2)` and
  `net_amount = gross - commission`;
* inserts with `ON CONFLICT (order_id) DO NOTHING`, so re-updating an order can
  never duplicate or reprice a payout.

Migration 0016 backfills the same rows for orders that were already delivered
and paid. A payout is an obligation, not a payment; the backfill posts no
journal.

### Settlement posting

`POST /api/admin/payouts/<payout_id>/settle` (admin token) requires a
`provider_ref` — an operator-entered mobile-money or bank reference proving the
transfer happened. It refuses unless:

* the payout is `pending`;
* the order is still `delivered` and `paid`;
* the payout gross still equals the order total;
* the checkout has exactly **one** posted receipt journal
  (`customer_payment` or `cod_collection`);
* that receipt's debit account is unambiguous (`PROVIDER_CLEARING` or
  `COD_CASH_HELD`).

The journal is:

```
reference:   settlement:<payout_id>
kind:        farmer_settlement
payment_ref: null
```

| Account | Side | Amount |
|---|---|---|
| `FARMER_PAYABLE` | debit | gross |
| `COMMISSION_REVENUE` | credit | commission, if non-zero |
| receipt's asset account | credit | net, if non-zero |

`payment_ref` is deliberately null: reconciliation groups active journals by
`payment_ref`, so reusing the checkout reference would be reported as a
duplicate receipt. The order, payout and provider reference are recorded in the
journal metadata.

Only after the journal returns is `mark_payout_paid()` called. That SQL
function re-checks that a posted `settlement:<payout_id>` journal exists, then
updates the payout to `paid` with `paid_at`, `provider` and `provider_ref` in
one conditional update. If the journal posts but the status update fails, the
API returns `503` with `retryable: true`; retrying posts nothing new and marks
the same payout. If two operators race, the second sees `already_settled`.

The admin UI's Settle action asks for the channel and reference, shows the net
amount and farmer, and reloads the table after a successful post.

### What a settlement does not do

It does not verify the transfer with a bank or mobile-money API, and it does
not move money. It records a transfer the operator states has happened, with a
reference, and refuses to record it without one.

---

## 5. Idempotency and concurrency

* The journal reference is derived only from the checkout reference:
  `payment:<tx_ref>`. Flutterwave and Pesapal attempt suffixes (`~abc123`) are
  stripped by `_base_ref` before use. COD uses `cod:<ref>` and recurring charges
  use `recurring:<tracking_id>`, so the events can never collide.
* `ledger_journals.reference` is `UNIQUE`. `post_journal` returns the existing
  journal id and posts nothing when the reference already exists, including
  when two webhook deliveries race.
* A retry after an ambiguous timeout is therefore safe.

---

## 6. Failure handling

Order of operations in `_flw_verify_and_mark`:

1. Verify the transaction with Flutterwave (server-to-server). The webhook body
   alone is never trusted.
2. Require `status = successful`, matching checkout reference, **explicit**
   `currency = UGX`, and `received >= order total`.
3. Mark every order row with that `payment_ref` as `paid`. If this write fails,
   the function returns failure and posts nothing.
4. Post the journal.
5. Send Discord, in-app notification and receipt (best effort).

Pesapal follows the same path: `_pesapal_verify_and_mark` calls
`GetTransactionStatus`, requires a completed status and a matching echoed
reference, then delegates to `_mark_checkout_paid` for the amount, currency,
order-marking and ledger steps. Callback/IPN fields are never evidence on their
own.

If step 4 fails:

* The payment is **not** reported as failed. The money was verified and the
  order is paid; telling the buyer otherwise risks a second payment.
* A `payment_ledger_post_failed` row is written to `system_events` and a
  Discord alert (`payment.ledger_failed`) is sent.
* The admin Reconciliation view reports `missing_ledger_entry` for the paid
  order, so the gap cannot disappear silently.

> **Expected after first deploy:** every order paid before the ledger was wired
> will appear as `missing_ledger_entry`. That is a historical gap, not a new
> bug. Do not "fix" it by inventing journals; backfill only from verified
> provider records.

---

## 7. Reconciliation checks

`services/finance.py:build_reconciliation` compares paid orders with active
journals sharing the same `payment_ref` and reports:

| Code | Severity | Meaning |
|---|---|---|
| `missing_ledger_entry` | error | Paid order, no active payment journal |
| `amount_mismatch` | error | Journal total differs from order total |
| `duplicate_ledger_posting` | error | More than one active journal for a reference |
| `payment_reversed_but_order_paid` | warning | Journal reversed, order still `paid` |
| `ledger_payment_without_paid_order` | warning | Customer payment journal with no paid order |
| `paid_order_without_payment_ref` | warning | Cannot be reconciled |
| `refund_without_reversal` | warning | Order marked refunded, no reversal journal |

A journal is "active" when `status = 'posted'` and `kind <> 'reversal'`.

Recurring journals have `payment_ref = NULL` and therefore never participate in
this receipt reconciliation. Their missing-posting check is the admin Recurring
view's `paid_amount_without_ledger` (`docs/PAYMENTS_PESAPAL.md` §7). Settlement
journals also have `payment_ref = NULL` for the same reason and are checked by
payout state instead.

---

## 8. Read-only verification (Supabase SQL Editor)

```sql
-- Account balances: assets/expenses debit-normal, liabilities/revenue credit-normal
select code, name, account_type, normal_side, total_debits, total_credits, balance
from public.ledger_account_balances
order by code;

-- The 20 most recent journals with their totals
select j.reference, j.kind, j.payment_ref, j.status, j.posted_at,
       coalesce(sum(l.amount) filter (where l.side = 'debit'), 0)  as debits,
       coalesce(sum(l.amount) filter (where l.side = 'credit'), 0) as credits
from public.ledger_journals j
left join public.ledger_lines l on l.journal_id = j.id
group by j.id
order by j.posted_at desc
limit 20;

-- Paid payment refs that have no active journal (historical gaps or failed posts)
select o.payment_ref,
       count(*) as paid_orders,
       sum(coalesce(o.total_amount, o.total_price, 0)) as order_total
from public.orders o
where o.payment_status = 'paid'
  and coalesce(o.payment_ref, '') <> ''
  and not exists (
    select 1 from public.ledger_journals j
    where j.payment_ref = o.payment_ref
      and j.status = 'posted'
      and j.kind <> 'reversal'
  )
group by o.payment_ref
order by order_total desc;

-- Recurring charges and whether each has a ledger journal
select c.provider_tracking_id, c.account_ref, c.amount, c.currency,
       c.status, c.ledger_reference, c.created_at
from public.subscription_charges c
order by c.created_at desc
limit 50;

-- Paid recurring charges with no ledger reference
select count(*) as paid_without_ledger, coalesce(sum(amount), 0) as amount
from public.subscription_charges
where status = 'paid' and coalesce(ledger_reference, '') = '';
```

Expected result for a UGX 50,000 checkout with no commission:

```
PROVIDER_CLEARING  debit  50000.00
FARMER_PAYABLE     credit 50000.00
```

Expected result for a UGX 20,000 Pesapal recurring charge:

```
PROVIDER_CLEARING      debit  20000.00
SUSPENSE_OVERPAYMENT   credit 20000.00
```

RLS allows admins to read the ledger through an authenticated admin session;
the SQL Editor runs with the service role.

---

## 9. Deliberately not implemented yet

* Transporter payout execution and postings.
* Banking transfers from `COD_CASH_HELD` and `PROVIDER_CLEARING` to the bank.
* A backend route to call `reverse_journal` for refunds and disputes.
* Transporter agreed charge and delivery fee capture (needed before
  `TRANSPORTER_PAYABLE` / `DELIVERY_REVENUE` can hold real money).
* An admin UI to edit `platform_config.commission_pct` (SQL only today).
* A workflow for payouts marked `failed` or `disputed`; only `pending` payouts
  can be settled.
* Partial settlement or netting several orders into one payout.
* Allocating `SUSPENSE_OVERPAYMENT` from a Pesapal recurring charge to a real
  order, and a cancellation path for a Pesapal subscription mandate. The charge
  is recorded and visible in the admin Recurring view, but reconciliation of the
  liability is manual until this workflow exists.

No dashboard figure is fabricated while these are missing: the Finance API
returns `available: false` with a reason for transporter payables and delivery
revenue.
