-- Rollback for 0007a_order_atomic_freeform.
-- Restores the original catalogue-only function from 0001. Existing free-form
-- orders are retained as historical records; only future order creation loses
-- support for free-form items after rollback.
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
  v_tracking := 'TRK-' || upper(substr(replace(gen_random_uuid()::text,'-',''),1,10));

  for v_item in select * from jsonb_array_elements(p_items) loop
    v_qty := (v_item->>'quantity')::numeric;
    if v_qty is null or v_qty <= 0 then
      raise exception 'invalid quantity for item' using errcode = '22023';
    end if;
    select * into v_listing from public.listings
      where id = (v_item->>'listing_id')::uuid for update;
    if not found then
      raise exception 'listing not found' using errcode = 'P0002';
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
      where id = v_listing.id and (quantity_kg - reserved_qty) >= v_qty;
    if not found then
      raise exception 'stock reservation race lost for listing %', v_listing.id using errcode = '40001';
    end if;
    insert into public.stock_changes
      (listing_id, changed_by, previous_qty, new_qty, delta, reason, order_ref)
    values
      (v_listing.id, p_buyer_id, v_prev, v_prev + v_qty, v_qty, 'order_reservation', v_order_ref);
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
