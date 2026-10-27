-- AgriBridge migration 0001 — Server-authoritative orders + atomic stock
-- Fixes audit findings C1 (client-computed totals), C2 (no reservation/oversell),
-- C4 (client-assumed success). Adds a durable order-event log for idempotency
-- (Phase 5) and a stock-change audit trail (Phase 4).
--
-- SAFETY: additive only. Uses IF NOT EXISTS guards so it will not clobber the
-- existing schema. Does not modify or delete existing rows. Review against the
-- live schema before applying (see docs/IMPLEMENTATION_STATUS.md — DB access is
-- an external blocker). Apply with the Supabase CLI:
--   supabase db push    (or paste into the SQL editor as a versioned migration)

begin;

-- ── Guarded column additions (no-op if already present) ──────────────────────
-- listings: availability + freshness fields used by stock reservation & matching.
alter table public.listings add column if not exists reserved_qty numeric not null default 0;
alter table public.listings add column if not exists stock_status text not null default 'available';
alter table public.listings add column if not exists stock_updated_at timestamptz not null default now();
alter table public.listings add column if not exists unit text;               -- already used by admin editor
alter table public.listings add column if not exists availability_date date;  -- Phase 4 harvest/readiness

-- orders: server-side integrity fields.
alter table public.orders add column if not exists idempotency_key uuid;
alter table public.orders add column if not exists created_by uuid;           -- auth.uid() that placed it
alter table public.orders add column if not exists updated_at timestamptz not null default now();

-- stock_status must be a known value; CHECK added only if not already enforced.
do $$ begin
  alter table public.listings
    add constraint listings_stock_status_chk
    check (stock_status in ('available','low_stock','unavailable','temporarily_unavailable'));
exception when duplicate_object or duplicate_table then null; end $$;

create index if not exists idx_listings_available on public.listings (crop_name)
  where is_available = true;
create index if not exists idx_orders_buyer on public.orders (buyer_id);
create index if not exists idx_orders_farmer on public.orders (farmer_id);
create index if not exists idx_orders_status on public.orders (status);

-- ── Durable order-event log (idempotency + audit trail, Phase 5) ─────────────
create table if not exists public.order_events (
  id uuid primary key default gen_random_uuid(),
  idempotency_key uuid,
  order_ref text,
  event_type text not null,          -- order.created, stock.reserved, offer.sent, ...
  actor uuid,                        -- auth.uid() or null for system
  payload jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now()
);
create unique index if not exists uq_order_events_idempotency
  on public.order_events (idempotency_key) where idempotency_key is not null;
create index if not exists idx_order_events_ref on public.order_events (order_ref);

-- ── Stock-change audit (who/when/prev/new, Phase 4) ──────────────────────────
create table if not exists public.stock_changes (
  id uuid primary key default gen_random_uuid(),
  listing_id uuid not null references public.listings(id) on delete cascade,
  changed_by uuid,
  previous_qty numeric not null,
  new_qty numeric not null,
  delta numeric not null,
  reason text not null,              -- 'order_reservation','farmer_update','admin_adjust','cancellation'
  order_ref text,
  created_at timestamptz not null default now()
);
create index if not exists idx_stock_changes_listing on public.stock_changes (listing_id, created_at desc);

-- ── Atomic order creation ────────────────────────────────────────────────────
-- p_items: jsonb array of {"listing_id": "<uuid>", "quantity": <numeric>}
-- Price and totals are recomputed from the listings row — the client value is
-- never trusted. Stock is reserved in the same transaction; insufficient or
-- unavailable stock raises and rolls the whole order back (no oversell).
create or replace function public.create_order_atomic(
  p_buyer_id uuid,
  p_items jsonb,
  p_delivery_address text,
  p_payment_method text,
  p_idempotency_key uuid default null
) returns jsonb
language plpgsql
security definer
set search_path = public
as $$
declare
  v_item jsonb;
  v_listing public.listings%rowtype;
  v_qty numeric;
  v_prev numeric;
  v_order_ref text;
  v_tracking text;
  v_total numeric := 0;
  v_created jsonb := '[]'::jsonb;
  v_existing jsonb;
begin
  -- Authorization: the caller may only create orders for themselves.
  if auth.uid() is not null and auth.uid() <> p_buyer_id then
    raise exception 'forbidden: buyer_id does not match authenticated user'
      using errcode = '42501';
  end if;

  if p_items is null or jsonb_array_length(p_items) = 0 then
    raise exception 'order must contain at least one item' using errcode = '22023';
  end if;

  -- Idempotency: if this key was already processed, return the prior result.
  if p_idempotency_key is not null then
    select payload into v_existing from public.order_events
      where idempotency_key = p_idempotency_key and event_type = 'order.created'
      limit 1;
    if v_existing is not null then
      return v_existing;
    end if;
  end if;

  v_order_ref := 'AB-' || to_char(now(), 'YYYYMMDD') || '-' || upper(substr(replace(gen_random_uuid()::text,'-',''),1,8));
  v_tracking  := 'TRK-' || upper(substr(replace(gen_random_uuid()::text,'-',''),1,10));

  for v_item in select * from jsonb_array_elements(p_items) loop
    v_qty := (v_item->>'quantity')::numeric;
    if v_qty is null or v_qty <= 0 then
      raise exception 'invalid quantity for item' using errcode = '22023';
    end if;

    -- Lock the listing row to serialize concurrent reservations.
    select * into v_listing from public.listings
      where id = (v_item->>'listing_id')::uuid
      for update;

    if not found then
      raise exception 'listing not found' using errcode = 'P0002';
    end if;
    if v_listing.is_available is distinct from true then
      raise exception 'listing % is not available', v_listing.id using errcode = '22023';
    end if;

    -- Available = on-hand minus already-reserved.
    if (v_listing.quantity_kg - coalesce(v_listing.reserved_qty,0)) < v_qty then
      raise exception 'insufficient stock for listing %', v_listing.id using errcode = '22023';
    end if;

    v_prev := coalesce(v_listing.reserved_qty,0);

    -- Reserve atomically (re-check the guard in the WHERE clause).
    update public.listings
      set reserved_qty = reserved_qty + v_qty,
          stock_updated_at = now(),
          stock_status = case
            when (quantity_kg - (reserved_qty + v_qty)) <= 0 then 'unavailable'
            when (quantity_kg - (reserved_qty + v_qty)) <= (quantity_kg * 0.1) then 'low_stock'
            else stock_status end
      where id = v_listing.id
        and (quantity_kg - reserved_qty) >= v_qty;
    if not found then
      raise exception 'stock reservation race lost for listing %', v_listing.id using errcode = '40001';
    end if;

    insert into public.stock_changes
      (listing_id, changed_by, previous_qty, new_qty, delta, reason, order_ref)
    values
      (v_listing.id, p_buyer_id, v_prev, v_prev + v_qty, v_qty, 'order_reservation', v_order_ref);

    -- Server-side price. total = unit price * qty (never the client figure).
    insert into public.orders
      (buyer_id, created_by, buyer_phone, farmer_id, listing_id, quantity_kg,
       price_per_kg, total_price, delivery_address, payment_method,
       item_category, status, payment_status, payment_ref, tracking_code,
       idempotency_key)
    values
      (p_buyer_id, p_buyer_id,
       (select phone from public.farmers where id = p_buyer_id),
       v_listing.farmer_id, v_listing.id, v_qty,
       v_listing.price_per_kg, v_listing.price_per_kg * v_qty,
       p_delivery_address, p_payment_method,
       coalesce(v_listing.category,'produce'), 'pending', 'unpaid',
       v_order_ref, v_tracking, p_idempotency_key);

    v_total := v_total + (v_listing.price_per_kg * v_qty);
    v_created := v_created || jsonb_build_object(
      'listing_id', v_listing.id, 'quantity', v_qty,
      'unit_price', v_listing.price_per_kg, 'farmer_id', v_listing.farmer_id);
  end loop;

  -- Durable event (also the idempotency record).
  insert into public.order_events (idempotency_key, order_ref, event_type, actor, payload)
  values (p_idempotency_key, v_order_ref, 'order.created', p_buyer_id,
          jsonb_build_object('order_ref', v_order_ref, 'tracking_code', v_tracking,
                             'total_price', v_total, 'items', v_created));

  return jsonb_build_object('order_ref', v_order_ref, 'tracking_code', v_tracking,
                            'total_price', v_total, 'items', v_created);
end;
$$;

revoke all on function public.create_order_atomic(uuid, jsonb, text, text, uuid) from public;
grant execute on function public.create_order_atomic(uuid, jsonb, text, text, uuid) to authenticated;

-- ── Order state machine: enforce legal transitions server-side (Phase 5) ─────
create or replace function public.set_order_status(p_order_id uuid, p_new_status text)
returns boolean
language plpgsql security definer set search_path = public as $$
declare v_cur text;
begin
  select status into v_cur from public.orders where id = p_order_id for update;
  if not found then raise exception 'order not found' using errcode='P0002'; end if;
  if v_cur = p_new_status then return true; end if;  -- idempotent

  if not (
    (v_cur = 'pending'    and p_new_status in ('confirmed','cancelled')) or
    (v_cur = 'confirmed'  and p_new_status in ('in_transit','cancelled')) or
    (v_cur = 'in_transit' and p_new_status = 'delivered')
  ) then
    raise exception 'illegal order transition % -> %', v_cur, p_new_status using errcode='22023';
  end if;

  update public.orders set status = p_new_status, updated_at = now() where id = p_order_id;

  -- Release reservation on cancellation.
  if p_new_status = 'cancelled' then
    update public.listings l
      set reserved_qty = greatest(0, reserved_qty - o.quantity_kg), stock_updated_at = now()
      from public.orders o
      where o.id = p_order_id and l.id = o.listing_id;
  end if;

  insert into public.order_events (order_ref, event_type, payload)
  select payment_ref, 'order.status_changed',
         jsonb_build_object('from', v_cur, 'to', p_new_status)
  from public.orders where id = p_order_id;

  return true;
end; $$;

revoke all on function public.set_order_status(uuid, text) from public;
grant execute on function public.set_order_status(uuid, text) to authenticated, service_role;

commit;
