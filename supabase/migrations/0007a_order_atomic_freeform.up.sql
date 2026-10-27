-- AgriBridge migration 0007 — Allow free-form (non-listing) order items
-- Root cause: create_order_atomic required every item to have a valid UUID listing_id.
-- Equipment, transport, supply, animal items submitted from the UI send listing_id=null.
-- The null cast to UUID finds no listing row → raises 'listing not found'.
--
-- Fix: when listing_id is null in an item, treat it as a free-form line item.
--   • Price is taken from the item's own "unit_price" field (client-supplied, trusted
--     for free-form items only — there is no catalogue row to verify against).
--   • No stock reservation or stock_changes audit entry is written (nothing to reserve).
--   • The order row is inserted with listing_id = NULL, farmer_id = NULL.
--   • All other validation (quantity > 0, delivery address, payment method) still applies.
--
-- SAFETY: additive — only replaces the function, no schema changes.

begin;

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
  v_item       jsonb;
  v_listing    public.listings%rowtype;
  v_qty        numeric;
  v_unit_price numeric;
  v_prev       numeric;
  v_order_ref  text;
  v_tracking   text;
  v_total      numeric := 0;
  v_created    jsonb := '[]'::jsonb;
  v_existing   jsonb;
  v_listing_id uuid;
  v_is_freeform boolean;
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

    -- Determine if this is a catalogue listing or a free-form item.
    v_listing_id := null;
    begin
      v_listing_id := (v_item->>'listing_id')::uuid;
    exception when invalid_text_representation then
      v_listing_id := null;
    end;

    v_is_freeform := (v_listing_id is null);

    if not v_is_freeform then
      -- ── CATALOGUE ITEM: look up listing, reserve stock ────────────────────
      select * into v_listing from public.listings
        where id = v_listing_id
        for update;

      if not found then
        raise exception 'listing not found: %', v_listing_id using errcode = 'P0002';
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

      -- Server-side price from catalogue row.
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

      v_total   := v_total + (v_unit_price * v_qty);
      v_created := v_created || jsonb_build_object(
        'listing_id', v_listing.id, 'quantity', v_qty,
        'unit_price', v_unit_price, 'farmer_id', v_listing.farmer_id);

    else
      -- ── FREE-FORM ITEM: no catalogue row, take price from item JSON ────────
      -- Accept unit_price or price_per_kg or price field names.
      v_unit_price := coalesce(
        (v_item->>'unit_price')::numeric,
        (v_item->>'price_per_unit')::numeric,
        (v_item->>'price_per_kg')::numeric,
        (v_item->>'price')::numeric,
        0
      );

      insert into public.orders
        (buyer_id, created_by, buyer_phone, farmer_id, listing_id, quantity_kg,
         price_per_kg, total_price, delivery_address, payment_method,
         item_category, status, payment_status, payment_ref, tracking_code,
         idempotency_key)
      values
        (p_buyer_id, p_buyer_id,
         (select phone from public.farmers where id = p_buyer_id),
         null, null, v_qty,
         v_unit_price, v_unit_price * v_qty,
         p_delivery_address, p_payment_method,
         coalesce(v_item->>'category', v_item->>'item_category','produce'),
         'pending', 'unpaid',
         v_order_ref, v_tracking, p_idempotency_key);

      v_total   := v_total + (v_unit_price * v_qty);
      v_created := v_created || jsonb_build_object(
        'listing_id', null,
        'product_name', coalesce(v_item->>'product_name', v_item->>'product', v_item->>'name','—'),
        'quantity', v_qty,
        'unit_price', v_unit_price,
        'farmer_id', null);
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
grant execute on function public.create_order_atomic(uuid, jsonb, text, text, uuid) to authenticated;

commit;
