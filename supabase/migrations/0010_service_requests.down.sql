-- Rollback for 0010_service_requests. Drops only what 0010 added.
-- WARNING: this deletes every saved cold storage / transport / loan request.
begin;

drop policy if exists service_requests_service on public.service_requests;
drop policy if exists service_requests_owner_read on public.service_requests;
drop table if exists public.service_requests;

commit;
