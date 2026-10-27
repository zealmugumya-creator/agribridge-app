-- AgriBridge migration 0015 DOWN — restore the 0007a create_order_atomic.
-- WARNING: this reopens the livestock checkout regression (animal UUIDs are
-- looked up in `listings` and fail) and drops supplier attribution. Use only
-- to roll back the function itself, then redeploy a client that inserts orders
-- directly (no longer supported by 0002).

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
  if auth.uid() is not null and auth.uid() <> p_buyer_id then
    raise exception 'forbidden: buyer_id does not match authenticated user'
      using errcode = '42501';
  end if;

  if p_items is null or jsonb_array_length(p_items) = 0 then
    raise exception 'order must contain at least one item' using errcode = '22023';
  end if;

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

    v_listing_id := null;
    begin
      v_listing_id := (v_item->>'listing_id')::uuid;
    exception when invalid_text_representation then
      v_listing_id := null;
    end;

    v_is_freeform := (v_listing_id is null);

    if not v_is_freeform then
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

      v_total   := v_total + (v_unit_price * v_qty);
      v_created := v_created || jsonb_build_object(
        'listing_id', v_listing.id, 'quantity', v_qty,
        'unit_price', v_unit_price, 'farmer_id', v_listing.farmer_id);

    else
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

-- Restore the 0001 state machine (produce-only stock release).
create or replace function public.set_order_status(p_order_id uuid, p_new_status text)
returns boolean
language plpgsql security definer set search_path = public as $$
declare v_cur text;
begin
  select status into v_cur from public.orders where id = p_order_id for update;
  if not found then raise exception 'order not found' using errcode='P0002'; end if;
  if v_cur = p_new_status then return true; end if;

  if not (
    (v_cur = 'pending'    and p_new_status in ('confirmed','cancelled')) or
    (v_cur = 'confirmed'  and p_new_status in ('in_transit','cancelled')) or
    (v_cur = 'in_transit' and p_new_status = 'delivered')
  ) then
    raise exception 'illegal order transition % -> %', v_cur, p_new_status using errcode='22023';
  end if;

  update public.orders set status = p_new_status, updated_at = now() where id = p_order_id;

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

-- The polymorphic catalogue reference only exists from 0015 onward.
alter table public.orders drop column if exists source_ref;

commit;
