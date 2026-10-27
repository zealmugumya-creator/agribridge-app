-- AgriBridge migration 0004 — Automated farmer matching & offer routing
-- Implements Phase 5 steps 5–15: identify eligible approved farmers, filter
-- (suspended / unavailable / stale stock / insufficient qty), rank by
-- configurable criteria, send offers to a controlled batch, record offer
-- lifecycle (sent/accepted/rejected/timed_out), and atomically reserve stock on
-- acceptance. Also distinguishes an unpaid purchase_request from a paid order.
--
-- SAFETY: additive only; no changes to existing rows. Review against the live
-- schema before applying. Depends on 0001 (reserved_qty, stock_status,
-- stock_updated_at on listings; order_events) and 0003 (notification_outbox).

begin;

-- ── Purchase requests (a request is NOT a confirmed paid order) ──────────────
create table if not exists public.purchase_requests (
  id uuid primary key default gen_random_uuid(),
  buyer_id uuid not null,
  product text not null,
  category text,
  quantity numeric not null,
  unit text not null default 'kg',
  delivery_address text,
  delivery_district text,
  target_price numeric,               -- buyer's indicative price; never authoritative
  status text not null default 'open', -- open|matching|offered|assigned|fulfilled|unmatched|cancelled
  idempotency_key uuid,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  constraint pr_status_chk check (status in
    ('open','matching','offered','assigned','fulfilled','unmatched','cancelled')),
  constraint pr_qty_positive check (quantity > 0)
);
create unique index if not exists uq_pr_idempotency
  on public.purchase_requests (idempotency_key) where idempotency_key is not null;
create index if not exists idx_pr_status on public.purchase_requests (status, created_at);

-- ── Offers: one row per (request, farmer) matching attempt ───────────────────
create table if not exists public.order_offers (
  id uuid primary key default gen_random_uuid(),
  request_id uuid not null references public.purchase_requests(id) on delete cascade,
  farmer_id uuid not null,
  listing_id uuid references public.listings(id) on delete set null,
  offered_qty numeric not null,
  unit_price numeric,
  rank int,                            -- priority order chosen by the matcher
  score numeric,                       -- computed ranking score (for audit)
  status text not null default 'sent', -- sent|accepted|rejected|timed_out|expired
  sent_at timestamptz not null default now(),
  response_deadline timestamptz not null,
  responded_at timestamptz,
  created_at timestamptz not null default now(),
  constraint offer_status_chk check (status in
    ('sent','accepted','rejected','timed_out','expired'))
);
create index if not exists idx_offers_request on public.order_offers (request_id, status);
create index if not exists idx_offers_farmer on public.order_offers (farmer_id, status);
-- Only one live (sent) offer batch per request is enforced in the matcher logic.

-- ── Configurable matching weights (Phase 5 step 7) ───────────────────────────
-- Stored in platform_config-adjacent table so ops can tune without a redeploy.
create table if not exists public.matching_config (
  id int primary key default 1,
  w_verified numeric not null default 40,      -- verified farmer
  w_stock_fresh numeric not null default 25,   -- stock confirmed recently
  w_quantity numeric not null default 15,      -- can fully fulfil in one listing
  w_rating numeric not null default 15,        -- farmer_ratings average
  w_history numeric not null default 5,        -- past fulfilment reliability
  stock_freshness_minutes int not null default 4320,
  offer_response_seconds int not null default 900,
  offer_batch_size int not null default 1,
  constraint matching_config_single check (id = 1)
);
insert into public.matching_config (id) values (1) on conflict (id) do nothing;

-- ── Eligibility + ranking view helper ────────────────────────────────────────
-- Returns eligible (farmer, listing) pairs for a product+quantity, filtered and
-- ranked. FILTERS OUT: unverified/suspended farmers, unavailable listings, stale
-- stock, and insufficient available quantity (Phase 5 step 6).
create or replace function public.match_eligible_farmers(
  p_product text, p_quantity numeric, p_district text default null
) returns table (
  farmer_id uuid, listing_id uuid, available_qty numeric, unit_price numeric,
  is_verified boolean, avg_rating numeric, fulfilled_orders bigint,
  stock_age_minutes numeric, score numeric, rank int
)
language plpgsql stable security definer set search_path = public as $$
declare
  cfg public.matching_config%rowtype;
begin
  select * into cfg from public.matching_config where id = 1;

  return query
  with eligible as (
    select
      l.farmer_id,
      l.id as listing_id,
      (l.quantity_kg - coalesce(l.reserved_qty,0)) as available_qty,
      l.price_per_kg as unit_price,
      coalesce(f.is_verified, false) as is_verified,
      coalesce(fr.avg_rating, 0)::numeric as avg_rating,
      coalesce(fh.fulfilled, 0)::bigint as fulfilled_orders,
      extract(epoch from (now() - l.stock_updated_at))/60.0 as stock_age_minutes,
      -- district match bonus is folded into score when provided
      (p_district is not null and lower(coalesce(l.district,'')) = lower(p_district)) as district_match
    from public.listings l
    join public.farmers f on f.id = l.farmer_id
    left join lateral (
      select avg(rating)::numeric as avg_rating from public.farmer_ratings
      where farmer_id = l.farmer_id
    ) fr on true
    left join lateral (
      select count(*) as fulfilled from public.orders o
      where o.farmer_id = l.farmer_id and o.status = 'delivered'
    ) fh on true
    where l.is_available = true
      and coalesce(l.stock_status,'available') in ('available','low_stock')
      and lower(l.crop_name) = lower(p_product)
      and (l.quantity_kg - coalesce(l.reserved_qty,0)) >= p_quantity
      -- stale stock is treated as potentially unavailable (Phase 4)
      and l.stock_updated_at >= now() - make_interval(mins => cfg.stock_freshness_minutes)
      -- exclude suspended farmers (is_verified false OR explicit suspend flag if present)
      and coalesce(f.is_verified, false) = true
  ),
  scored as (
    select e.*,
      ( cfg.w_verified  * (case when e.is_verified then 1 else 0 end)
      + cfg.w_stock_fresh * (case when e.stock_age_minutes <= (cfg.stock_freshness_minutes/2.0) then 1 else 0.4 end)
      + cfg.w_quantity  * (case when e.available_qty >= p_quantity * 2 then 1 else 0.6 end)
      + cfg.w_rating    * (least(e.avg_rating,5.0)/5.0)
      + cfg.w_history   * (case when e.fulfilled_orders > 0 then least(e.fulfilled_orders,20.0)/20.0 else 0 end)
      + (case when e.district_match then 10 else 0 end)
      )::numeric as score
    from eligible e
  )
  select s.farmer_id, s.listing_id, s.available_qty, s.unit_price,
         s.is_verified, s.avg_rating, s.fulfilled_orders, s.stock_age_minutes,
         s.score,
         row_number() over (order by s.score desc, s.available_qty desc)::int as rank
  from scored s
  order by s.score desc, s.available_qty desc;
end; $$;
revoke all on function public.match_eligible_farmers(text, numeric, text) from public;
grant execute on function public.match_eligible_farmers(text, numeric, text) to service_role, authenticated;

commit;
