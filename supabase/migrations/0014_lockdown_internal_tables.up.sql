-- Copyright (C) 2026 zealmugumya-creator. All rights reserved.
--
-- 0014 — Close public/excessive access on internal operations tables.
--
-- Root cause: migrations/0008_transporters_and_system_events.sql created policies
-- named "service role full access" with no TO clause. Such a policy applies to
-- PUBLIC (anon + authenticated), not just the service role. Combined with
-- Supabase's default table grants this exposed internal data and writes.
--
-- Verified against the live project on 2026-10-21 before this fix:
--   * anon GET /rest/v1/system_events returned internal events (user ids,
--     order-failure details);
--   * an anon INSERT on system_events reached a NOT NULL constraint (23502),
--     proving INSERT privilege rather than a permission rejection.
--
-- This migration changes grants and policies only. No rows are touched.
-- It is safe to re-run.

begin;

-- ── system_events: backend-only operational log ─────────────────────────────
alter table public.system_events enable row level security;

drop policy if exists "service role full access" on public.system_events;
drop policy if exists system_events_service_role on public.system_events;
create policy system_events_service_role on public.system_events
  for all to service_role using (true) with check (true);

revoke all on public.system_events from public;
revoke all on public.system_events from anon, authenticated;
grant all on public.system_events to service_role;

-- ── transporters: backend full access, transporter sees/updates own row only ─
alter table public.transporters enable row level security;

drop policy if exists "service role full access" on public.transporters;
drop policy if exists transporters_service_role on public.transporters;
create policy transporters_service_role on public.transporters
  for all to service_role using (true) with check (true);

-- Re-assert the owner policies: legitimate transporter self-service must keep
-- working even if the earlier policies were dropped by hand.
drop policy if exists "transporter read own" on public.transporters;
create policy "transporter read own" on public.transporters
  for select to authenticated using (supabase_uid = auth.uid());

drop policy if exists "transporter update own availability" on public.transporters;
create policy "transporter update own availability" on public.transporters
  for update to authenticated using (supabase_uid = auth.uid())
  with check (supabase_uid = auth.uid());

revoke all on public.transporters from public;
revoke all on public.transporters from anon;
revoke all on public.transporters from authenticated;
grant select, update on public.transporters to authenticated;
grant all on public.transporters to service_role;

commit;
