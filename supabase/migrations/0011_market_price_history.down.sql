-- Rollback for 0011_market_price_history. Drops only what 0011 added.
-- WARNING: deletes all stored price snapshots (the live board then shows no change % until it rebuilds).
begin;

drop policy if exists market_price_history_service on public.market_price_history;
drop table if exists public.market_price_history;

commit;
