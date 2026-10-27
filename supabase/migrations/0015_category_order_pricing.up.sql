-- AgriBridge migration 0015 — Category-aware order pricing
-- Fixes the checkout regression introduced when the SPA started sending every
-- category through POST /api/orders:
--   * livestock items carry an `animal_listings` UUID, which is NOT a row in
--     `listings`; create_order_atomic raised "listing not found" and the order
--     failed.
--   * supplier items were sent as free-form lines, so the client price was
--     trusted and `orders.farmer_id` was null (the supplier never saw the order).
--
-- This function keeps the produce path (and its stock reservation) unchanged,
-- resolves animal/supply prices from their own catalogue tables server-side,
-- decrements their stock atomically, and records farmer/supplier attribution.
-- Free-form lines (equipment, transport, demo data) keep the 0007a behaviour.
--
-- SAFETY: replaces one function; no schema change, no data change. Requires
-- 0001 and 0007a. Apply before 0002 (which closes direct order inserts).

begin;

-- Polymorphic reference to the animal/supply catalogue row. `orders.listing_id`
-- has a FK to `listings`, so it cannot hold an animal_listings/supplier_products
-- id. This column lets cancellation restore the right stock.
alter table public.orders add column if not exists source_ref uuid;
comment on column public.orders.source_ref is
  'animal_listings.id or supplier_products.id for non-produce orders; null otherwise';

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
  v_item        jsonb;
  v_listing     public.listings%rowtype;
  v_animal      public.animal_listings%rowtype;
  v_product     public.supplier_products%rowtype;
  v_qty         numeric;
  v_unit_price  numeric;
  v_prev        numeric;
  v_order_ref   text;
  v_tracking    text;
  v_total       numeric := 0;
  v_created     jsonb := '[]'::jsonb;
  v_existing    jsonb;
  v_listing_id  uuid;
  v_category    text;
  v_farmer_id   uuid;
  v_phone       text;
  v_name        text;
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

    v_category := lower(coalesce(nullif(v_item->>'item_category',''),
                                 nullif(v_item->>'category',''), 'produce'));

    v_listing_id := null;
    begin
      v_listing_id := (v_item->>'listing_id')::uuid;
    exception when invalid_text_representation then
      v_listing_id := null;
    end;

    if v_listing_id is not null
       and v_category in ('animal','livestock','dairy') then
      -- ── LIVESTOCK: price and owner come from animal_listings ─────────────
      select * into v_animal from public.animal_listings
        where id = v_listing_id
        for update;
      if not found then
        raise exception 'animal listing not found: %', v_listing_id using errcode = 'P0002';
      end if;
      if coalesce(v_animal.status,'active') <> 'active' then
        raise exception 'animal listing % is not active', v_listing_id using errcode = '22023';
      end if;
      if v_qty <> trunc(v_qty) then
        raise exception 'animal quantity must be a whole number' using errcode = '22023';
      end if;
      if coalesce(v_animal.qty,0) < v_qty then
        raise exception 'insufficient stock for animal listing %', v_listing_id using errcode = '22023';
      end if;

      update public.animal_listings
        set qty = qty - v_qty::integer
        where id = v_listing_id and coalesce(qty,0) >= v_qty;
      if not found then
        raise exception 'animal stock race lost for listing %', v_listing_id using errcode = '40001';
      end if;

      v_unit_price := coalesce(v_animal.price,0);
      v_farmer_id  := v_animal.farmer_id;
      if v_farmer_id is not null and not exists
           (select 1 from public.farmers where id = v_farmer_id) then
        v_farmer_id := null;
      end if;
      v_phone := coalesce(v_animal.farmer_phone,
        (select phone from public.farmers where id = v_farmer_id));
      v_name  := coalesce(v_animal.name,'Animal');

      insert into public.orders
        (buyer_id, created_by, buyer_phone, farmer_id, listing_id, quantity_kg,
         price_per_kg, total_price, delivery_address, payment_method,
         item_category, status, payment_status, payment_ref, tracking_code,
         idempotency_key, notes, source_ref)
      values
        (p_buyer_id, p_buyer_id,
         (select phone from public.farmers where id = p_buyer_id),
         v_farmer_id, null, v_qty,
         v_unit_price, v_unit_price * v_qty,
         p_delivery_address, p_payment_method,
         'animal', 'pending', 'unpaid',
         v_order_ref, v_tracking, p_idempotency_key, 'Item: ' || v_name,
         v_listing_id);

      v_total := v_total + (v_unit_price * v_qty);
      v_created := v_created || jsonb_build_object(
        'listing_id', null, 'product_name', v_name, 'quantity', v_qty,
        'unit_price', v_unit_price, 'farmer_id', v_farmer_id,
        'farmer_phone', v_phone);

    elsif v_listing_id is not null and v_category = 'supply' then
      -- ── SUPPLIER INPUTS: price and owner come from supplier_products ──────
      select * into v_product from public.supplier_products
        where id = v_listing_id
        for update;
      if not found then
        raise exception 'supply listing not found: %', v_listing_id using errcode = 'P0002';
      end if;
      if v_product.in_stock is not true then
        raise exception 'supply product % is not in stock', v_listing_id using errcode = '22023';
      end if;
      if v_product.stock_qty is not null and v_product.stock_qty < v_qty then
        raise exception 'insufficient stock for supply product %', v_listing_id using errcode = '22023';
      end if;

      update public.supplier_products
        set stock_qty = stock_qty - v_qty
        where id = v_listing_id
          and (stock_qty is null or stock_qty >= v_qty);
      if not found then
        raise exception 'supply stock race lost for listing %', v_listing_id using errcode = '40001';
      end if;

      v_unit_price := coalesce(v_product.price,0);
      v_farmer_id  := v_product.supplier_id;
      if v_farmer_id is not null and not exists
           (select 1 from public.farmers where id = v_farmer_id) then
        v_farmer_id := null;
      end if;
      v_phone := (select phone from public.farmers where id = v_farmer_id);
      v_name  := v_product.name;

      insert into public.orders
        (buyer_id, created_by, buyer_phone, farmer_id, listing_id, quantity_kg,
         price_per_kg, total_price, delivery_address, payment_method,
         item_category, status, payment_status, payment_ref, tracking_code,
         idempotency_key, notes, source_ref)
      values
        (p_buyer_id, p_buyer_id,
         (select phone from public.farmers where id = p_buyer_id),
         v_farmer_id, null, v_qty,
         v_unit_price, v_unit_price * v_qty,
         p_delivery_address, p_payment_method,
         'supply', 'pending', 'unpaid',
         v_order_ref, v_tracking, p_idempotency_key, 'Item: ' || v_name,
         v_listing_id);

      v_total := v_total + (v_unit_price * v_qty);
      v_created := v_created || jsonb_build_object(
        'listing_id', null, 'product_name', v_name, 'quantity', v_qty,
        'unit_price', v_unit_price, 'farmer_id', v_farmer_id,
        'farmer_phone', v_phone);

    elsif v_listing_id is not null then
      -- ── PRODUCE CATALOGUE: unchanged catalogue price + stock reservation ──
      select * into v_listing from public.listings
        where id = v_listing_id
        for update;

      if not found then
        raise exception 'listing not found: %', v_listing_id using errcode = 'P0002';
      end if;
      if v_listing.is_available is distinct from true then
        raise exception 'listing % is not available', v_listing.id using errcode = '22023';
      end if;

      if (v_listing.quantity_kg - coalesce(v_listing.reserved_qty,0)) < v_qty then
        raise exception 'insufficient stock for listing %', v_listing.id using errcode = '22023';
      end if;

      v_prev := coalesce(v_listing.reserved_qty,0);

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

      v_unit_price := v_listing.price_per_kg;

      insert into public.orders
        (buyer_id, created_by, buyer_phone, farmer_id, listing_id, quantity_kg,
         price_per_kg, total_price, delivery_address, payment_method,
         item_category, status, payment_status, payment_ref, tracking_code,
         idempotency_key)
      values
        (p_buyer_id, p_buyer_id,
         (select phone from public.farmers where id = p_buyer_id),
         v_listing.farmer_id, v_listing.id, v_qty,
         v_unit_price, v_unit_price * v_qty,
         p_delivery_address, p_payment_method,
         coalesce(v_listing.category,'produce'), 'pending', 'unpaid',
         v_order_ref, v_tracking, p_idempotency_key);

      v_phone := coalesce(v_listing.farmer_phone,
        (select phone from public.farmers where id = v_listing.farmer_id));
      v_name  := v_listing.crop_name;

      v_total := v_total + (v_unit_price * v_qty);
      v_created := v_created || jsonb_build_object(
        'listing_id', v_listing.id, 'product_name', v_name, 'quantity', v_qty,
        'unit_price', v_unit_price, 'farmer_id', v_listing.farmer_id,
        'farmer_phone', v_phone);

    else
      -- ── FREE-FORM: no catalogue row (equipment, transport, demo data) ─────
      v_unit_price := coalesce(
        (v_item->>'unit_price')::numeric,
        (v_item->>'price_per_unit')::numeric,
        (v_item->>'price_per_kg')::numeric,
        (v_item->>'price')::numeric,
        0
      );
      v_name := coalesce(v_item->>'product_name', v_item->>'product',
                         v_item->>'name', '—');

      insert into public.orders
        (buyer_id, created_by, buyer_phone, farmer_id, listing_id, quantity_kg,
         price_per_kg, total_price, delivery_address, payment_method,
         item_category, status, payment_status, payment_ref, tracking_code,
         idempotency_key, notes)
      values
        (p_buyer_id, p_buyer_id,
         (select phone from public.farmers where id = p_buyer_id),
         null, null, v_qty,
         v_unit_price, v_unit_price * v_qty,
         p_delivery_address, p_payment_method,
         v_category, 'pending', 'unpaid',
         v_order_ref, v_tracking, p_idempotency_key, 'Item: ' || v_name);

      v_total := v_total + (v_unit_price * v_qty);
      v_created := v_created || jsonb_build_object(
        'listing_id', null, 'product_name', v_name, 'quantity', v_qty,
        'unit_price', v_unit_price, 'farmer_id', null, 'farmer_phone', null);
    end if;
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
grant execute on function public.create_order_atomic(uuid, jsonb, text, text, uuid) to authenticated, service_role;

-- ── Cancellation restores the stock reserved/decremented at order creation ──
-- Same state machine as 0001, plus livestock and supplier stock. Idempotent:
-- a repeat cancellation returns before any stock movement.
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

  if p_new_status = 'cancelled' then
    -- Produce: release the reservation.
    update public.listings l
      set reserved_qty = greatest(0, reserved_qty - o.quantity_kg), stock_updated_at = now()
      from public.orders o
      where o.id = p_order_id and l.id = o.listing_id;

    -- Livestock: put the animals back on the listing.
    update public.animal_listings a
      set qty = coalesce(a.qty,0) + o.quantity_kg::integer
      from public.orders o
      where o.id = p_order_id and o.item_category = 'animal'
        and a.id = o.source_ref;

    -- Supplier inputs: restore the decremented stock.
    update public.supplier_products p
      set stock_qty = coalesce(p.stock_qty,0) + o.quantity_kg
      from public.orders o
      where o.id = p_order_id and o.item_category = 'supply'
        and p.id = o.source_ref;
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
