-- Rollback for 0009_web_push_subscriptions. Drops only what 0009 added.
begin;

drop policy if exists web_push_subscriptions_service on public.web_push_subscriptions;
drop table if exists public.web_push_subscriptions;

commit;
