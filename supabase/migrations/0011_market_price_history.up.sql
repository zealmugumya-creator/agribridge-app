-- AgriBridge migration 0011 — Market price history (powers the live price board)
-- The live board averages what farmers are asking on the platform. To show a real
-- "change since yesterday" we keep a snapshot of each crop's price every ~10 minutes.
-- Written ONLY by the backend (service role). Public reads go through GET /api/market/live.
--
-- SAFETY: additive only (one new table). Rollback: 0011_market_price_history.down.sql

begin;

create table if not exists public.market_price_history (
  id bigint generated always as identity primary key,
  crop text not null,                 -- lower-case crop name
  district text,                      -- null = platform-wide
  avg_price numeric not null,         -- UGX per kg
  min_price numeric,
  max_price numeric,
  sellers integer not null default 0,
  total_qty_kg numeric,
  captured_at timestamptz not null default now()
);

create index if not exists idx_market_price_history_crop_time
  on public.market_price_history (crop, captured_at desc);
create index if not exists idx_market_price_history_time
  on public.market_price_history (captured_at desc);

alter table public.market_price_history enable row level security;

drop policy if exists market_price_history_service on public.market_price_history;
create policy market_price_history_service on public.market_price_history
  for all to service_role using (true) with check (true);

grant all on public.market_price_history to service_role;

commit;
