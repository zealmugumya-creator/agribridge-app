-- AgriBridge migration 0009 — Web Push subscriptions (Android notification bar)
-- One row per browser/device a signed-in user has allowed notifications on.
-- Written by the backend only (service role), after the caller's identity has
-- been verified from their Supabase session — never from the request body.
begin;

create table if not exists public.web_push_subscriptions (
  id            uuid        primary key default gen_random_uuid(),
  user_id       uuid        not null,
  endpoint      text        not null,
  p256dh        text        not null,
  auth          text        not null,
  user_agent    text,
  created_at    timestamptz not null default now(),
  last_seen_at  timestamptz not null default now(),
  constraint web_push_subscriptions_endpoint_key unique (endpoint)
);

create index if not exists web_push_subscriptions_user_idx
  on public.web_push_subscriptions (user_id);

alter table public.web_push_subscriptions enable row level security;

-- Service role (backend) only; no client-side reads or writes.
drop policy if exists web_push_subscriptions_service on public.web_push_subscriptions;
create policy web_push_subscriptions_service on public.web_push_subscriptions
  for all to service_role using (true) with check (true);

commit;
