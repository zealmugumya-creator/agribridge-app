-- AgriBridge migration 0010 — Service requests (cold storage, refrigerated transport, loans)
-- Until now the "Book Storage", "Book Vehicle" and loan buttons only showed a toast:
-- nothing was saved, so the admin console had nothing to list. One table keeps all
-- three request types together so admin can track every one of them.
--
-- Writes come ONLY from the backend (service role), which takes user_id from the
-- verified session, never from the request body. Users can read their own rows.
--
-- SAFETY: additive only (one new table). Rollback: 0010_service_requests.down.sql

begin;

create table if not exists public.service_requests (
  id uuid primary key default gen_random_uuid(),
  kind text not null,                      -- cold_storage | refrigerated_transport | loan
  user_id uuid references auth.users(id) on delete set null,
  requester_name text,
  requester_phone text,
  provider_name text,                      -- facility / vehicle company / loan product
  amount_ugx numeric,                      -- loans: amount requested
  details jsonb not null default '{}'::jsonb,   -- pallets, pickup, destination, dates, ...
  status text not null default 'pending',
  admin_notes text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  constraint service_requests_kind_chk
    check (kind in ('cold_storage','refrigerated_transport','loan')),
  constraint service_requests_status_chk
    check (status in ('pending','confirmed','completed','rejected','cancelled'))
);

create index if not exists idx_service_requests_kind_created
  on public.service_requests (kind, created_at desc);
create index if not exists idx_service_requests_user
  on public.service_requests (user_id, created_at desc);
create index if not exists idx_service_requests_pending
  on public.service_requests (created_at desc) where status = 'pending';

alter table public.service_requests enable row level security;

drop policy if exists service_requests_owner_read on public.service_requests;
create policy service_requests_owner_read on public.service_requests
  for select to authenticated using (user_id = auth.uid());

drop policy if exists service_requests_service on public.service_requests;
create policy service_requests_service on public.service_requests
  for all to service_role using (true) with check (true);

grant select on public.service_requests to authenticated;
grant all on public.service_requests to service_role;

commit;
