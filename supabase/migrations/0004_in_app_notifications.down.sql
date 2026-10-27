-- Rollback for 0004_in_app_notifications
-- Removes the in_app channel, related index, RLS policies, and helper function.

begin;

drop function if exists public.mark_notification_read(uuid);
drop policy if exists notif_user_read_own on public.notification_outbox;
drop policy if exists notif_service_all on public.notification_outbox;
drop index if exists idx_notif_recipient_inbox;

alter table public.notification_outbox
  drop constraint if exists notif_channel_chk;

alter table public.notification_outbox
  add constraint notif_channel_chk
    check (channel in ('push', 'discord', 'sms', 'email'));

commit;
