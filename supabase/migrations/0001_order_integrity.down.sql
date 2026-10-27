-- Rollback for 0001_order_integrity. Reversible; drops only what 0001 added.
-- Existing listings/orders rows and their original columns are left untouched.
begin;

drop function if exists public.set_order_status(uuid, text);
drop function if exists public.create_order_atomic(uuid, jsonb, text, text, uuid);

drop table if exists public.stock_changes;
drop table if exists public.order_events;

drop index if exists public.idx_orders_status;
drop index if exists public.idx_orders_farmer;
drop index if exists public.idx_orders_buyer;
drop index if exists public.idx_listings_available;

alter table public.orders drop column if exists updated_at;
alter table public.orders drop column if exists created_by;
alter table public.orders drop column if exists idempotency_key;

alter table public.listings drop constraint if exists listings_stock_status_chk;
alter table public.listings drop column if exists availability_date;
alter table public.listings drop column if exists stock_updated_at;
alter table public.listings drop column if exists stock_status;
alter table public.listings drop column if exists reserved_qty;
-- NOTE: listings.unit is pre-existing (used by the admin editor); left in place.

commit;
