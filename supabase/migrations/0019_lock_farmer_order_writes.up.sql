-- Copyright (C) 2026 zealmugumya-creator. All rights reserved.
--
-- 0019 — Farmers may read their orders; they may not rewrite them.
--
-- base-schema.sql:264 defines
--   create policy orders_farmer_update on public.orders for update to
--     authenticated using (auth.uid() = farmer_id) with check (auth.uid() = farmer_id)
-- and that is the only UPDATE policy on orders. using/with_check are *row*
-- predicates: they say which rows a farmer may touch, not which columns. So any
-- signed-in farmer can PATCH every writable column of their own order rows
-- straight through PostgREST — status, payment_status, payment_ref, farmer_id,
-- notes — with no transition validation and no audit trail.
--
-- Why that is more than untidy: 0016_farmer_settlements attaches
--   create trigger orders_create_payout after insert or update of status,
--     payment_status on public.orders
-- to create_payout_for_order(), which inserts a *pending* payout whenever a row
-- reads delivered + paid with gross > 0. That function is declared
-- `security definer`, so the payout insert does not need the farmer to hold any
-- payout privilege: one direct PATCH of
-- {"status":"delivered","payment_status":"paid"} mints a payout the farmer was
-- never owed. Settlement refuses to disburse without a ledger receipt, so this is
-- not a direct cash leak — but it writes a false financial obligation into the
-- same payouts table the admin Finance view totals as payable.
--
-- No feature needs the policy. Every legitimate order mutation goes through the
-- backend: POST /api/orders/<id>/status validates the role, the transition and
-- the legal gate, then calls the set_order_status RPC with the service role,
-- which bypasses RLS and is therefore unaffected by this migration (app.py:2881).
-- app.py and services/ contain no direct update of the orders table at all — a
-- grep for an orders row write finds none — so nothing behind this policy is in
-- use. Confirmed against the bundle deployed to production on 2026-10-23:
-- static/index.html has no direct write to /rest/v1/orders, and static/admin.html
-- only calls /api/admin/order/<id>/status. Reads keep working —
-- orders_owner_select is untouched, so a farmer still sees their own orders.
--
-- The payout-minting path is demonstrated, not assumed: tests/sql/
-- order_write_lockdown.test.mjs reproduces it against a real in-process Postgres
-- before the drop and shows the service-role status path still paying out after
-- it. This migration is not yet applied to the live project, so the hole is open
-- in production until it is run.
--
-- Drops a policy only. No rows are touched. Safe to re-run.

begin;

drop policy if exists orders_farmer_update on public.orders;

commit;
