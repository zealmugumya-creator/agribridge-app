-- 0016_farmer_settlements.up.sql
-- Copyright (C) 2026 zealmugumya-creator. All rights reserved.
--
-- Farmer settlements, phase 1:
--   * create exactly one pending payout per delivered, paid order (commission
--     snapshotted from platform_config at that moment);
--   * backfill payouts for orders that already meet the rule;
--   * make the only supported way to mark a payout paid require a posted
--     `settlement:<payout_id>` journal, so the operational record cannot get
--     ahead of the accounting record.
--
-- Apply after 0015. Safe to re-run: every object is replaced or created
-- idempotently and the backfill uses ON CONFLICT (order_id) DO NOTHING.
-- This migration does not post any settlement journal by itself and does not
-- move money. Settlements are posted by the admin API with an operator-supplied
-- provider reference, after the receipt is verified in the ledger.
begin;

-- ── Ledger audit dependency ─────────────────────────────────────────────────
-- post_journal() writes admin_audit_log when an actor uuid is supplied. That
-- table is declared in 0002, which may not be applied yet, so the first
-- admin-initiated ledger write would fail at runtime. Create the same shape
-- here; 0002 later adds its is_admin()-based read policy on top.
create table if not exists public.admin_audit_log (
  id bigint generated always as identity primary key,
  actor uuid,
  action text not null,
  target text,
  metadata jsonb not null default '{}'::jsonb,
  ip text,
  created_at timestamptz not null default now()
);
create index if not exists idx_admin_audit_actor
  on public.admin_audit_log (actor, created_at desc);

alter table public.admin_audit_log enable row level security;
drop policy if exists admin_audit_service_0016 on public.admin_audit_log;
create policy admin_audit_service_0016 on public.admin_audit_log
  for all to service_role using (true) with check (true);

-- ── Commission configuration must stay a percentage ─────────────────────────
-- NOT VALID: enforces future writes without failing on legacy rows. The
-- trigger below also clamps, so a bad historical value cannot block delivery.
do $$
begin
  if not exists (
    select 1 from pg_constraint
    where conname = 'platform_config_commission_pct_range'
      and conrelid = 'public.platform_config'::regclass
  ) then
    alter table public.platform_config
      add constraint platform_config_commission_pct_range
      check (commission_pct >= 0 and commission_pct <= 100) not valid;
  end if;
end $$;

-- ── One payout per delivered, paid order ────────────────────────────────────
create or replace function public.create_payout_for_order()
returns trigger
language plpgsql
security definer
set search_path = public, pg_temp
as $$
declare
  v_gross      numeric(18,2);
  v_pct        numeric(6,2);
  v_commission numeric(18,2);
begin
  if new.farmer_id is null
     or coalesce(new.status, '') <> 'delivered'
     or coalesce(new.payment_status, '') <> 'paid' then
    return new;
  end if;

  v_gross := round(coalesce(nullif(new.total_amount, 0), new.total_price, 0)::numeric, 2);
  if v_gross <= 0 then
    return new;
  end if;

  select coalesce(commission_pct, 0) into v_pct
    from public.platform_config where id = 1;
  v_pct := least(greatest(coalesce(v_pct, 0), 0), 100);
  v_commission := round(v_gross * v_pct / 100.0, 2);

  insert into public.payouts (
    order_id, farmer_id, gross_amount, commission_pct,
    commission_amount, net_amount, status
  ) values (
    new.id, new.farmer_id, v_gross, v_pct,
    v_commission, v_gross - v_commission, 'pending'
  )
  on conflict (order_id) do nothing;

  return new;
end $$;

revoke all on function public.create_payout_for_order() from public;

drop trigger if exists orders_create_payout on public.orders;
create trigger orders_create_payout
  after insert or update of status, payment_status on public.orders
  for each row execute function public.create_payout_for_order();

-- ── Backfill: real obligations already earned ───────────────────────────────
-- Derived only from orders that are delivered and paid. Orders whose receipt
-- never reached the ledger will be refused at settlement time, not paid twice.
insert into public.payouts (
  order_id, farmer_id, gross_amount, commission_pct,
  commission_amount, net_amount, status
)
select s.order_id, s.farmer_id, s.gross, s.pct,
       round(s.gross * s.pct / 100.0, 2),
       s.gross - round(s.gross * s.pct / 100.0, 2),
       'pending'
from (
  select o.id as order_id,
         o.farmer_id,
         round(coalesce(nullif(o.total_amount, 0), o.total_price, 0)::numeric, 2) as gross,
         least(greatest(coalesce(c.commission_pct, 0), 0), 100) as pct
  from public.orders o
  left join public.platform_config c on c.id = 1
  where o.status = 'delivered'
    and o.payment_status = 'paid'
    and o.farmer_id is not null
) s
where s.gross > 0
on conflict (order_id) do nothing;

-- ── Mark paid only against a posted settlement journal ──────────────────────
-- Returns false when the payout is not pending or the journal is missing.
-- The backend uses the result to distinguish "settled now" from "already done".
create or replace function public.mark_payout_paid(
  p_payout_id   uuid,
  p_provider    text,
  p_provider_ref text
) returns boolean
language plpgsql
security definer
set search_path = public, pg_temp
as $$
declare
  v_rows integer;
begin
  if p_payout_id is null then
    return false;
  end if;
  if coalesce(btrim(p_provider_ref), '') = '' then
    return false;
  end if;
  if not exists (
    select 1 from public.ledger_journals
    where reference = 'settlement:' || p_payout_id::text
      and status = 'posted'
      and kind = 'farmer_settlement'
  ) then
    return false;
  end if;

  update public.payouts
     set status = 'paid',
         paid_at = now(),
         provider = nullif(btrim(p_provider), ''),
         provider_ref = btrim(p_provider_ref)
   where id = p_payout_id
     and status = 'pending';
  get diagnostics v_rows = row_count;
  return v_rows = 1;
end $$;

revoke all on function public.mark_payout_paid(uuid, text, text) from public;
grant execute on function public.mark_payout_paid(uuid, text, text) to service_role;

commit;
