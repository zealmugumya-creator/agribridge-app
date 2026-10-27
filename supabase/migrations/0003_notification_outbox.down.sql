-- Rollback for 0003_notification_outbox. Drops only what 0003 added.
begin;

drop function if exists public.resolve_notification(uuid, boolean, text);
drop function if exists public.claim_notifications(int);
drop function if exists public.enqueue_notification(text,text,uuid,text,text,text,text,jsonb,text,text);

drop policy if exists admin_devices_service on public.admin_devices;
drop policy if exists admin_devices_owner on public.admin_devices;
drop table if exists public.admin_devices;
drop table if exists public.notification_outbox;

commit;
