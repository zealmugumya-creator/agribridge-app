-- Copyright (C) 2026 zealmugumya-creator. All rights reserved.
--
-- 0019 rollback: restores orders_farmer_update exactly as base-schema.sql
-- declares it. Rolling this back reopens the ability to PATCH arbitrary columns
-- of one's own orders, including the delivered + paid combination that mints a
-- pending payout, so it should only be run alongside a replacement policy that
-- restricts writable columns. No rows are touched.

begin;

drop policy if exists orders_farmer_update on public.orders;

create policy orders_farmer_update on public.orders
  for update to authenticated using (auth.uid() = farmer_id)
  with check (auth.uid() = farmer_id);

commit;
