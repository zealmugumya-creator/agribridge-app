-- 0017_pesapal_payments.up.sql
-- Copyright (C) 2026 zealmugumya-creator. All rights reserved.
--
-- Pesapal API 3.0 payment plumbing:
--   * payment_gateway_state stores the registered IPN id so it survives
--     restarts (Pesapal must not accumulate duplicate notification URLs);
--   * payment_subscriptions records the correlation id Pesapal returns for a
--     recurring authorisation;
--   * subscription_charges records every IPN cycle exactly once. Recurring
--     money is posted to SUSPENSE_OVERPAYMENT until operations allocate it to
--     a real order, so no farmer payable is invented;
--   * orders.payment_method accepts the values the checkout already sends
--     (card/momo/airtel) and the new `pesapal` value.
--
-- Every table is service-role only: the browser never needs to read gateway
-- state, subscription ids or charge history. Safe to re-run.
begin;

-- ── Gateway state (IPN registration) ────────────────────────────────────────
create table if not exists public.payment_gateway_state (
  provider   text primary key,
  config     jsonb not null default '{}'::jsonb,
  updated_at timestamptz not null default now()
);

alter table public.payment_gateway_state enable row level security;
drop policy if exists payment_gateway_state_service_0017 on public.payment_gateway_state;
create policy payment_gateway_state_service_0017 on public.payment_gateway_state
  for all to service_role using (true) with check (true);
revoke all on public.payment_gateway_state from anon, authenticated;
grant all on public.payment_gateway_state to service_role;

-- ── Recurring subscriptions ─────────────────────────────────────────────────
create table if not exists public.payment_subscriptions (
  id                      uuid primary key default gen_random_uuid(),
  provider                text not null,
  provider_correlation_id text not null,
  account_ref             text not null,
  buyer_id                uuid,
  amount                  numeric(18,2) not null default 0,
  currency                text not null default 'UGX',
  status                  text not null default 'active'
    check (status in ('pending','active','cancelled','unknown')),
  last_charged_at         timestamptz,
  created_at              timestamptz not null default now(),
  updated_at              timestamptz not null default now(),
  unique (provider, provider_correlation_id)
);
create index if not exists payment_subscriptions_account_ref_idx
  on public.payment_subscriptions (account_ref);

alter table public.payment_subscriptions enable row level security;
drop policy if exists payment_subscriptions_service_0017 on public.payment_subscriptions;
create policy payment_subscriptions_service_0017 on public.payment_subscriptions
  for all to service_role using (true) with check (true);
revoke all on public.payment_subscriptions from anon, authenticated;
grant all on public.payment_subscriptions to service_role;

-- ── One row per recurring IPN cycle ─────────────────────────────────────────
create table if not exists public.subscription_charges (
  id                  uuid primary key default gen_random_uuid(),
  subscription_id     uuid references public.payment_subscriptions(id) on delete set null,
  provider            text not null,
  provider_tracking_id text not null,
  confirmation_code   text,
  account_ref         text,
  amount              numeric(18,2) not null,
  currency            text not null default 'UGX',
  status              text not null
    check (status in ('pending','paid','failed')),
  ledger_reference    text,
  created_at          timestamptz not null default now(),
  unique (provider, provider_tracking_id)
);
create index if not exists subscription_charges_created_idx
  on public.subscription_charges (created_at desc);

alter table public.subscription_charges enable row level security;
drop policy if exists subscription_charges_service_0017 on public.subscription_charges;
create policy subscription_charges_service_0017 on public.subscription_charges
  for all to service_role using (true) with check (true);
revoke all on public.subscription_charges from anon, authenticated;
grant all on public.subscription_charges to service_role;

-- ── Let checkout persist every payment method it offers ─────────────────────
-- The original check only allowed the legacy USSD values, so a card or Pesapal
-- checkout could be rejected by the database even though the API accepted it.
-- NOT VALID: legacy rows are never rescanned, new writes are enforced.
do $$
declare
  r record;
begin
  for r in
    select conname
      from pg_constraint
     where conrelid = 'public.orders'::regclass
       and contype = 'c'
       and pg_get_constraintdef(oid) ilike '%payment_method%'
  loop
    execute format('alter table public.orders drop constraint %I', r.conname);
  end loop;

  if not exists (
    select 1 from pg_constraint
     where conname = 'orders_payment_method_allowed'
       and conrelid = 'public.orders'::regclass
  ) then
    alter table public.orders
      add constraint orders_payment_method_allowed
      check (payment_method is null or payment_method in (
        'mtn_momo','airtel_money','cash','bank','card','momo','airtel',
        'pesapal','direct','mobile_money')) not valid;
  end if;
end $$;

commit;
