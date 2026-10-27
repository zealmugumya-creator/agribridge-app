-- Rollback for 0002_admin_identity_rls. Drops policies/functions/tables added
-- by 0002. NOTE: dropping a policy does not disable RLS on pre-existing tables;
-- orders/payouts keep whatever RLS they had before (enable stays on).
begin;

drop policy if exists orders_service on public.orders;
drop policy if exists orders_admin_update on public.orders;
drop policy if exists orders_owner_read on public.orders;
drop policy if exists orders_no_direct_insert on public.orders;
drop policy if exists payouts_service on public.payouts;
drop policy if exists payouts_no_public_write on public.payouts;

drop policy if exists audit_insert_admin on public.admin_audit_log;
drop policy if exists audit_service_all on public.admin_audit_log;
drop policy if exists audit_read_admin on public.admin_audit_log;
drop policy if exists user_roles_service on public.user_roles;
drop policy if exists user_roles_no_public on public.user_roles;

drop function if exists public.assign_role(uuid, text, uuid);
drop function if exists public.is_admin();
drop function if exists public.is_admin(uuid);

drop table if exists public.admin_audit_log;
drop table if exists public.user_roles;

commit;
