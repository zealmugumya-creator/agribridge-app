-- AgriBridge migration 0005 — Offer lifecycle & atomic acceptance
-- Implements Phase 5 steps 8–15: create a purchase request, send offers to a
-- controlled batch of ranked farmers, accept (atomically reserving stock and
-- preventing conflicting allocations), reject, and time out expired offers.
-- Depends on 0001 (listings.reserved_qty, order_events, create_order_atomic
-- primitives) and 0004 (purchase_requests, order_offers, match_eligible_farmers).
--
-- SAFETY: additive only. Review against the live schema before applying.

begin;

-- ── Create a purchase request (idempotent) ───────────────────────────────────
create or replace function public.create_purchase_request(
  p_buyer_id uuid, p_product text, p_quantity numeric, p_unit text,
  p_delivery_address text, p_delivery_district text, p_target_price numeric,
  p_idempotency_key uuid default null
) returns jsonb language plpgsql security definer set search_path = public as $$
declare v_id uuid; v_existing jsonb;
begin
  if auth.uid() is not null and auth.uid() <> p_buyer_id then
    raise exception 'forbidden: buyer_id does not match authenticated user' using errcode='42501';
  end if;
  if p_quantity is null or p_quantity <= 0 then
    raise exception 'quantity must be positive' using errcode='22023';
  end if;

  if p_idempotency_key is not null then
    select jsonb_build_object('id', id, 'status', status) into v_existing
      from public.purchase_requests where idempotency_key = p_idempotency_key limit 1;
    if v_existing is not null then return v_existing; end if;
  end if;

  insert into public.purchase_requests
    (buyer_id, product, quantity, unit, delivery_address, delivery_district,
     target_price, idempotency_key)
  values
    (p_buyer_id, p_product, p_quantity, coalesce(p_unit,'kg'), p_delivery_address,
     p_delivery_district, p_target_price, p_idempotency_key)
  returning id into v_id;

  insert into public.order_events (order_ref, event_type, actor, payload)
  values (v_id::text, 'request.created', p_buyer_id,
          jsonb_build_object('product', p_product, 'quantity', p_quantity));

  return jsonb_build_object('id', v_id, 'status', 'open');
end; $$;
revoke all on function public.create_purchase_request(uuid,text,numeric,text,text,text,numeric,uuid) from public;
grant execute on function public.create_purchase_request(uuid,text,numeric,text,text,text,numeric,uuid)
  to authenticated, service_role;

-- ── Send offers to the top-N ranked eligible farmers (steps 5, 8, 9) ─────────
create or replace function public.match_and_offer(p_request_id uuid)
returns jsonb language plpgsql security definer set search_path = public as $$
declare
  req public.purchase_requests%rowtype;
  cfg public.matching_config%rowtype;
  m record;
  v_offers int := 0;
  v_deadline timestamptz;
begin
  select * into req from public.purchase_requests where id = p_request_id for update;
  if not found then raise exception 'request not found' using errcode='P0002'; end if;
  if req.status in ('assigned','fulfilled','cancelled') then
    return jsonb_build_object('request_id', p_request_id, 'offers', 0, 'note', req.status);
  end if;
  select * into cfg from public.matching_config where id = 1;
  v_deadline := now() + make_interval(secs => cfg.offer_response_seconds);

  for m in
    select * from public.match_eligible_farmers(req.product, req.quantity, req.delivery_district)
    order by rank asc limit greatest(1, cfg.offer_batch_size)
  loop
    insert into public.order_offers
      (request_id, farmer_id, listing_id, offered_qty, unit_price, rank, score,
       status, response_deadline)
    values
      (p_request_id, m.farmer_id, m.listing_id, req.quantity, m.unit_price, m.rank,
       m.score, 'sent', v_deadline);
    v_offers := v_offers + 1;

    insert into public.order_events (order_ref, event_type, payload)
    values (p_request_id::text, 'offer.sent',
            jsonb_build_object('farmer_id', m.farmer_id, 'listing_id', m.listing_id,
                               'rank', m.rank, 'score', m.score,
                               'deadline', v_deadline));

    -- Enqueue a farmer notification for this offer (drained by the worker).
    perform public.enqueue_notification(
      'push','farmer', m.farmer_id, 'offer.received', 'info',
      'New offer: ' || req.product,
      req.quantity || ' ' || req.unit || ' of ' || req.product,
      jsonb_build_object('request_id', p_request_id, 'product', req.product,
                         'quantity', req.quantity),
      'offer:' || p_request_id || ':' || m.farmer_id, p_request_id::text);
  end loop;

  update public.purchase_requests
    set status = case when v_offers > 0 then 'offered' else 'unmatched' end,
        updated_at = now()
    where id = p_request_id;

  if v_offers = 0 then
    -- Step 11: no farmer can fulfil — alert the administrator honestly.
    insert into public.order_events (order_ref, event_type, payload)
    values (p_request_id::text, 'matching.failed',
            jsonb_build_object('product', req.product, 'quantity', req.quantity));
    perform public.enqueue_notification(
      'push','admin', null, 'matching.failed', 'warning',
      'No farmer matched', req.product || ' x' || req.quantity || ' unmatched',
      jsonb_build_object('request_id', p_request_id, 'product', req.product),
      'matchfail:' || p_request_id, p_request_id::text);
    perform public.enqueue_notification(
      'discord','admin', null, 'matching.failed', 'warning',
      'No eligible farmer for request',
      jsonb_build_object('request_id', p_request_id, 'product', req.product,
                         'quantity', req.quantity),
      'discord:matchfail:' || p_request_id, p_request_id::text);
  end if;

  return jsonb_build_object('request_id', p_request_id, 'offers', v_offers);
end; $$;
revoke all on function public.match_and_offer(uuid) from public, authenticated;
grant execute on function public.match_and_offer(uuid) to service_role;

-- ── Accept an offer: atomically reserve stock, block conflicts (steps 12–13) ─
create or replace function public.accept_offer(p_offer_id uuid, p_farmer_id uuid)
returns jsonb language plpgsql security definer set search_path = public as $$
declare
  off public.order_offers%rowtype;
  req public.purchase_requests%rowtype;
  v_prev numeric;
  v_order_ref text;
begin
  select * into off from public.order_offers where id = p_offer_id for update;
  if not found then raise exception 'offer not found' using errcode='P0002'; end if;
  if off.farmer_id <> p_farmer_id then
    raise exception 'forbidden: offer belongs to another farmer' using errcode='42501';
  end if;
  if off.status <> 'sent' then
    raise exception 'offer no longer open (status=%)', off.status using errcode='22023';
  end if;
  if off.response_deadline < now() then
    update public.order_offers set status='timed_out' where id=p_offer_id;
    raise exception 'offer has expired' using errcode='22023';
  end if;

  select * into req from public.purchase_requests where id = off.request_id for update;
  if req.status in ('assigned','fulfilled','cancelled') then
    raise exception 'request already %', req.status using errcode='40001';
  end if;

  -- Atomically reserve the listing stock; lose the race => conflict, no oversell.
  select reserved_qty into v_prev from public.listings where id = off.listing_id for update;
  update public.listings
    set reserved_qty = reserved_qty + off.offered_qty,
        stock_updated_at = now(),
        stock_status = case
          when (quantity_kg - (reserved_qty + off.offered_qty)) <= 0 then 'unavailable'
          else stock_status end
    where id = off.listing_id
      and is_available = true
      and (quantity_kg - reserved_qty) >= off.offered_qty;
  if not found then
    raise exception 'stock no longer available for listing %', off.listing_id using errcode='40001';
  end if;

  insert into public.stock_changes
    (listing_id, changed_by, previous_qty, new_qty, delta, reason, order_ref)
  values (off.listing_id, p_farmer_id, coalesce(v_prev,0), coalesce(v_prev,0)+off.offered_qty,
          off.offered_qty, 'offer_acceptance', off.request_id::text);

  v_order_ref := 'PR-' || upper(substr(replace(off.request_id::text,'-',''),1,8));

  update public.order_offers set status='accepted', responded_at=now() where id=p_offer_id;
  -- Expire sibling offers for the same request (only one farmer wins).
  update public.order_offers set status='expired'
    where request_id = off.request_id and id <> p_offer_id and status='sent';
  update public.purchase_requests set status='assigned', updated_at=now()
    where id = off.request_id;

  insert into public.order_events (order_ref, event_type, actor, payload)
  values (off.request_id::text, 'offer.accepted', p_farmer_id,
          jsonb_build_object('offer_id', p_offer_id, 'farmer_id', p_farmer_id,
                             'listing_id', off.listing_id, 'qty', off.offered_qty));

  perform public.enqueue_notification(
    'push','admin', null, 'offer.accepted', 'info', 'Offer accepted',
    req.product || ' assigned to a farmer', jsonb_build_object('request_id', off.request_id),
    'accepted:' || off.request_id, off.request_id::text);

  return jsonb_build_object('request_id', off.request_id, 'status','assigned',
                            'order_ref', v_order_ref, 'farmer_id', p_farmer_id);
end; $$;
revoke all on function public.accept_offer(uuid, uuid) from public;
grant execute on function public.accept_offer(uuid, uuid) to authenticated, service_role;

-- ── Reject an offer (step 9) ─────────────────────────────────────────────────
create or replace function public.reject_offer(p_offer_id uuid, p_farmer_id uuid, p_reason text default null)
returns boolean language plpgsql security definer set search_path = public as $$
declare off public.order_offers%rowtype;
begin
  select * into off from public.order_offers where id = p_offer_id for update;
  if not found then raise exception 'offer not found' using errcode='P0002'; end if;
  if off.farmer_id <> p_farmer_id then
    raise exception 'forbidden' using errcode='42501';
  end if;
  if off.status <> 'sent' then return true; end if;   -- idempotent
  update public.order_offers set status='rejected', responded_at=now() where id=p_offer_id;
  insert into public.order_events (order_ref, event_type, actor, payload)
  values (off.request_id::text, 'offer.rejected', p_farmer_id,
          jsonb_build_object('offer_id', p_offer_id, 'reason', p_reason));
  return true;
end; $$;
revoke all on function public.reject_offer(uuid, uuid, text) from public;
grant execute on function public.reject_offer(uuid, uuid, text) to authenticated, service_role;

-- ── Timeout sweep (step 10): expire overdue sent offers ──────────────────────
create or replace function public.expire_overdue_offers()
returns int language plpgsql security definer set search_path = public as $$
declare n int;
begin
  with expired as (
    update public.order_offers set status='timed_out'
    where status='sent' and response_deadline < now()
    returning id, request_id
  )
  select count(*) into n from expired;

  -- Any request whose offers are all now closed and none accepted -> back to
  -- 'open' so the worker can re-match to the next eligible farmer (step 10).
  update public.purchase_requests pr set status='open', updated_at=now()
  where pr.status='offered'
    and not exists (select 1 from public.order_offers o
                    where o.request_id=pr.id and o.status in ('sent','accepted'));

  if n > 0 then
    insert into public.order_events (event_type, payload)
    values ('offers.timed_out', jsonb_build_object('count', n));
  end if;
  return coalesce(n,0);
end; $$;
revoke all on function public.expire_overdue_offers() from public, authenticated;
grant execute on function public.expire_overdue_offers() to service_role;

-- ── Worker claim: requests needing (re)matching ──────────────────────────────
create or replace function public.claim_unmatched_requests(p_limit int default 10)
returns setof public.purchase_requests
language plpgsql security definer set search_path = public as $$
begin
  return query
  with cte as (
    select id from public.purchase_requests
    where status in ('open') or (status='offered' and not exists
        (select 1 from public.order_offers o where o.request_id=purchase_requests.id and o.status='sent'))
    order by created_at asc
    limit greatest(1, coalesce(p_limit,10))
    for update skip locked
  )
  select pr.* from public.purchase_requests pr join cte on cte.id = pr.id;
end; $$;
revoke all on function public.claim_unmatched_requests(int) from public, authenticated;
grant execute on function public.claim_unmatched_requests(int) to service_role;

commit;
