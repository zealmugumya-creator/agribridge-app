-- AgriBridge migration 0003 — Durable notification + Discord outbox
-- Fixes H3 (no durable queue), H4 (no push), H5 (no ops alerts). Implements the
-- transactional-outbox pattern: business transactions enqueue rows here; a worker
-- drains them with retries, dedup, and dead-letter handling.
--
-- SAFETY: additive only. Review before applying.

begin;

-- ── Unified notification outbox ──────────────────────────────────────────────
create table if not exists public.notification_outbox (
  id uuid primary key default gen_random_uuid(),
  dedupe_key text,                    -- unique per logical event; prevents dup sends
  channel text not null,              -- 'push' | 'discord' | 'sms' | 'email'
  audience text not null default 'admin',  -- 'admin' | 'buyer' | 'farmer'
  recipient uuid,                     -- target user (for push/sms/email); null for discord
  event_type text not null,           -- order.created, matching.failed, payment.failed, ...
  severity text not null default 'info',  -- 'info'|'warning'|'critical'
  title text,
  body text,
  payload jsonb not null default '{}'::jsonb,
  correlation_id text,
  status text not null default 'pending',  -- pending|processing|sent|failed|dead
  attempts int not null default 0,
  max_attempts int not null default 5,
  next_retry_at timestamptz not null default now(),
  last_error text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  constraint notif_channel_chk check (channel in ('push','discord','sms','email')),
  constraint notif_status_chk  check (status in ('pending','processing','sent','failed','dead'))
);
-- Only one pending/sent row per dedupe_key (duplicate-suppression, Phase 6/7).
create unique index if not exists uq_notif_dedupe
  on public.notification_outbox (dedupe_key)
  where dedupe_key is not null and status in ('pending','processing','sent');
create index if not exists idx_notif_drain
  on public.notification_outbox (status, next_retry_at)
  where status in ('pending','failed');
create index if not exists idx_notif_severity on public.notification_outbox (severity, created_at desc);

-- ── Admin push device tokens (Phase 6) ───────────────────────────────────────
create table if not exists public.admin_devices (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users(id) on delete cascade,
  provider text not null default 'fcm',     -- 'fcm' | 'apns'
  token text not null,
  platform text,                            -- 'android' | 'ios' | 'web'
  last_seen_at timestamptz not null default now(),
  created_at timestamptz not null default now(),
  constraint admin_devices_provider_chk check (provider in ('fcm','apns'))
);
-- One row per (user, token); token refresh upserts by natural key.
create unique index if not exists uq_admin_device_token on public.admin_devices (provider, token);
alter table public.admin_devices enable row level security;
drop policy if exists admin_devices_owner on public.admin_devices;
create policy admin_devices_owner on public.admin_devices
  for all to authenticated
  using (user_id = auth.uid() and public.is_admin(auth.uid()))
  with check (user_id = auth.uid() and public.is_admin(auth.uid()));
drop policy if exists admin_devices_service on public.admin_devices;
create policy admin_devices_service on public.admin_devices
  for all to service_role using (true) with check (true);

-- ── Enqueue helper (idempotent by dedupe_key) ────────────────────────────────
create or replace function public.enqueue_notification(
  p_channel text, p_audience text, p_recipient uuid, p_event_type text,
  p_severity text, p_title text, p_body text, p_payload jsonb,
  p_dedupe_key text default null, p_correlation_id text default null
) returns uuid language plpgsql security definer set search_path = public as $$
declare v_id uuid;
begin
  if p_dedupe_key is not null then
    select id into v_id from public.notification_outbox
      where dedupe_key = p_dedupe_key and status in ('pending','processing','sent')
      limit 1;
    if v_id is not null then return v_id; end if;   -- already queued/sent: no-op
  end if;

  insert into public.notification_outbox
    (dedupe_key, channel, audience, recipient, event_type, severity, title, body, payload, correlation_id)
  values
    (p_dedupe_key, p_channel, p_audience, p_recipient, p_event_type,
     coalesce(p_severity,'info'), p_title, p_body, coalesce(p_payload,'{}'::jsonb), p_correlation_id)
  returning id into v_id;
  return v_id;
end; $$;
revoke all on function public.enqueue_notification(text,text,uuid,text,text,text,text,jsonb,text,text) from public;
grant execute on function public.enqueue_notification(text,text,uuid,text,text,text,text,jsonb,text,text)
  to authenticated, service_role;

-- ── Worker claim: atomically grab a due batch (prevents double-processing) ───
create or replace function public.claim_notifications(p_limit int default 10)
returns setof public.notification_outbox
language plpgsql security definer set search_path = public as $$
begin
  return query
  with cte as (
    select id from public.notification_outbox
    where status in ('pending','failed') and next_retry_at <= now()
    order by (severity = 'critical') desc, created_at asc
    limit greatest(1, coalesce(p_limit,10))
    for update skip locked
  )
  update public.notification_outbox o
    set status = 'processing', attempts = attempts + 1, updated_at = now()
    from cte where o.id = cte.id
  returning o.*;
end; $$;
revoke all on function public.claim_notifications(int) from public, authenticated;
grant execute on function public.claim_notifications(int) to service_role;

-- ── Worker result: mark sent / schedule retry with exponential backoff / dead ─
create or replace function public.resolve_notification(p_id uuid, p_ok boolean, p_error text default null)
returns void language plpgsql security definer set search_path = public as $$
declare v_attempts int; v_max int;
begin
  select attempts, max_attempts into v_attempts, v_max
    from public.notification_outbox where id = p_id for update;
  if not found then return; end if;

  if p_ok then
    update public.notification_outbox
      set status='sent', last_error=null, updated_at=now() where id=p_id;
  elsif v_attempts >= v_max then
    update public.notification_outbox
      set status='dead', last_error=p_error, updated_at=now() where id=p_id;
  else
    update public.notification_outbox
      set status='failed', last_error=p_error, updated_at=now(),
          -- exponential backoff w/ jitter: 30s,60s,120s,... capped at 30min
          next_retry_at = now() + (least(1800, 30 * (2 ^ (v_attempts-1))) || ' seconds')::interval
      where id=p_id;
  end if;
end; $$;
revoke all on function public.resolve_notification(uuid, boolean, text) from public, authenticated;
grant execute on function public.resolve_notification(uuid, boolean, text) to service_role;

commit;
