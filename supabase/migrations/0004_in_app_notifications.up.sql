-- AgriBridge migration 0004 — In-app notification channel + user read access
-- Adds 'in_app' to the notification channel enum so buyers and farmers can
-- receive order status notifications visible in their account notification bell.
-- Also adds an RLS policy so authenticated users can read (not write) their own
-- notification rows, and marks in_app notifications as 'sent' immediately since
-- they are read directly from the DB (no external dispatch needed).
--
-- SAFETY: additive-only. The constraint change uses DROP/ADD which requires a
-- brief ACCESS EXCLUSIVE lock but does not rewrite table data.

begin;

-- ── 1. Expand channel constraint to include 'in_app' ─────────────────────────
alter table public.notification_outbox
  drop constraint if exists notif_channel_chk;

alter table public.notification_outbox
  add constraint notif_channel_chk
    check (channel in ('push', 'discord', 'sms', 'email', 'in_app'));

-- ── 2. Index for per-user notification inbox queries ─────────────────────────
create index if not exists idx_notif_recipient_inbox
  on public.notification_outbox (recipient, created_at desc)
  where channel = 'in_app';

-- ── 3. RLS: authenticated users can read their own in_app notifications ───────
-- notification_outbox is service-role-only by default; open a read-only window
-- for the notification inbox endpoint (called with the user's JWT).
alter table public.notification_outbox enable row level security;

drop policy if exists notif_user_read_own on public.notification_outbox;
create policy notif_user_read_own on public.notification_outbox
  for select to authenticated
  using (recipient = auth.uid() and channel = 'in_app');

-- Service role retains full access (existing behaviour).
drop policy if exists notif_service_all on public.notification_outbox;
create policy notif_service_all on public.notification_outbox
  for all to service_role using (true) with check (true);

-- ── 4. Helper: mark an in_app notification as read (seen) ────────────────────
-- We repurpose the existing 'sent' status to mean "seen by user".
-- The frontend calls POST /api/user/notifications/<id>/read which calls this.
create or replace function public.mark_notification_read(p_id uuid)
returns void language plpgsql security definer set search_path = public as $$
begin
  update public.notification_outbox
    set status = 'sent', updated_at = now()
  where id = p_id
    and channel = 'in_app'
    and recipient = auth.uid();
end; $$;
revoke all on function public.mark_notification_read(uuid) from public;
grant execute on function public.mark_notification_read(uuid) to authenticated;

commit;
