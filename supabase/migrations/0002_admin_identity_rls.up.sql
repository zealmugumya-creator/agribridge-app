-- AgriBridge migration 0002 — Server-controlled admin identity + least-privilege RLS
-- Fixes C3 (shared-password admin, no identity/MFA/audit) and hardens C5 (RLS).
--
-- DESIGN
--  * Roles live in a server-controlled table `user_roles`, NOT in client-writable
--    user_metadata. A user cannot grant themselves a role: the only writer is a
--    SECURITY DEFINER function callable by service_role, plus the audit trigger.
--  * `is_admin(uuid)` is the single authorization predicate used by RLS + backend.
--  * The super-admin is assigned by UUID (never by trusting an email string). Run
--    the assignment block at the bottom MANUALLY with the service role after
--    reading the verified UUID from auth.users (do NOT invent a UUID).
--
-- SAFETY: additive; does not drop existing policies. Review before applying.
--
-- ✅ APPLY-ORDER NOTE (updated 2026-10-21):
-- The `orders_no_direct_insert` policy below closes the authenticated INSERT
-- path on `orders`. The web app no longer inserts orders directly: produce,
-- livestock and supplier checkout all go through `POST /api/orders`, and
-- migration 0015 makes `create_order_atomic` resolve animal/supply prices and
-- owners from their own catalogue tables. Apply 0015 FIRST, deploy this client
-- bundle, then apply this migration. Applying 0002 against an older client
-- (one that still inserts directly) would break checkout and the shared
-- `payment_ref` reconciliation used by the payment webhook.

begin;

-- ── Role store (trusted, server-controlled) ──────────────────────────────────
create table if not exists public.user_roles (
  user_id uuid primary key references auth.users(id) on delete cascade,
  role text not null default 'user',           -- 'user' | 'admin' | 'superadmin'
  granted_by uuid,
  granted_at timestamptz not null default now(),
  constraint user_roles_role_chk check (role in ('user','admin','superadmin'))
);

-- ── Admin audit log (logins + sensitive actions, Phase 3) ────────────────────
create table if not exists public.admin_audit_log (
  id bigint generated always as identity primary key,
  actor uuid,
  action text not null,
  target text,
  metadata jsonb not null default '{}'::jsonb,
  ip text,
  created_at timestamptz not null default now()
);
create index if not exists idx_admin_audit_actor on public.admin_audit_log (actor, created_at desc);

-- ── Authorization predicate ──────────────────────────────────────────────────
create or replace function public.is_admin(p_uid uuid) returns boolean
language sql stable security definer set search_path = public as $$
  select exists (
    select 1 from public.user_roles
    where user_id = p_uid and role in ('admin','superadmin')
  );
$$;
create or replace function public.is_admin() returns boolean
language sql stable security definer set search_path = public as $$
  select public.is_admin(auth.uid());
$$;
revoke all on function public.is_admin(uuid) from public;
revoke all on function public.is_admin() from public;
grant execute on function public.is_admin(uuid) to authenticated, service_role;
grant execute on function public.is_admin() to authenticated, service_role;

-- ── Role assignment (privileged; service_role only) ──────────────────────────
create or replace function public.assign_role(p_user_id uuid, p_role text, p_granted_by uuid)
returns void language plpgsql security definer set search_path = public as $$
begin
  if p_role not in ('user','admin','superadmin') then
    raise exception 'invalid role %', p_role using errcode='22023';
  end if;
  if not exists (select 1 from auth.users where id = p_user_id) then
    raise exception 'auth user % does not exist', p_user_id using errcode='P0002';
  end if;
  insert into public.user_roles (user_id, role, granted_by)
  values (p_user_id, p_role, p_granted_by)
  on conflict (user_id) do update set role = excluded.role,
    granted_by = excluded.granted_by, granted_at = now();
  insert into public.admin_audit_log (actor, action, target, metadata)
  values (p_granted_by, 'role.assigned', p_user_id::text, jsonb_build_object('role', p_role));
end; $$;
revoke all on function public.assign_role(uuid, text, uuid) from public, authenticated;
grant execute on function public.assign_role(uuid, text, uuid) to service_role;

-- ── RLS: lock the role store down completely ─────────────────────────────────
alter table public.user_roles enable row level security;
drop policy if exists user_roles_no_public on public.user_roles;
create policy user_roles_no_public on public.user_roles
  for all using (false) with check (false);           -- nobody via PostgREST
drop policy if exists user_roles_service on public.user_roles;
create policy user_roles_service on public.user_roles
  for all to service_role using (true) with check (true);

alter table public.admin_audit_log enable row level security;
drop policy if exists audit_read_admin on public.admin_audit_log;
create policy audit_read_admin on public.admin_audit_log
  for select to authenticated using (public.is_admin(auth.uid()));
drop policy if exists audit_service_all on public.admin_audit_log;
create policy audit_service_all on public.admin_audit_log
  for all to service_role using (true) with check (true);
-- Insert allowed for authenticated admins (so the backend can log as the actor).
drop policy if exists audit_insert_admin on public.admin_audit_log;
create policy audit_insert_admin on public.admin_audit_log
  for insert to authenticated with check (public.is_admin(auth.uid()));

-- ── RLS: close the public INSERT path on financial/PII tables (C1/C5) ────────
-- Orders must be created only by the SECURITY DEFINER RPC (create_order_atomic)
-- or the service role — never by an arbitrary authenticated browser insert.
alter table public.orders enable row level security;
drop policy if exists orders_no_direct_insert on public.orders;
create policy orders_no_direct_insert on public.orders
  for insert to authenticated with check (false);      -- use create_order_atomic()
drop policy if exists orders_owner_read on public.orders;
create policy orders_owner_read on public.orders
  for select to authenticated
  using (buyer_id = auth.uid() or farmer_id = auth.uid() or public.is_admin(auth.uid()));
drop policy if exists orders_admin_update on public.orders;
create policy orders_admin_update on public.orders
  for update to authenticated using (public.is_admin(auth.uid())) with check (public.is_admin(auth.uid()));
drop policy if exists orders_service on public.orders;
create policy orders_service on public.orders for all to service_role using (true) with check (true);

-- payouts: owner/admin read only; no public write.
alter table public.payouts enable row level security;
drop policy if exists payouts_no_public_write on public.payouts;
create policy payouts_no_public_write on public.payouts
  for insert to authenticated with check (false);
drop policy if exists payouts_service on public.payouts;
create policy payouts_service on public.payouts for all to service_role using (true) with check (true);

commit;

-- ══════════════════════════════════════════════════════════════════════════
-- MANUAL SUPER-ADMIN ASSIGNMENT — run separately, with the service role.
-- 1) Confirm the account exists AND is email-verified; copy its UUID:
--      select id, email, email_confirmed_at
--      from auth.users where email = 'zealmugumya@gmail.com';
--    If email_confirmed_at IS NULL, the identity is NOT verified — stop and
--    have the owner verify the account first (mandate Phase 3).
-- 2) Assign the role (paste the real UUID; do NOT invent one):
--      select public.assign_role('<VERIFIED-UUID>', 'superadmin', null);
-- 3) Enable MFA for that account in Supabase Auth (Auth → user → require MFA),
--    and rotate ADMIN_PASSWORD out once Supabase-Auth admin login is live.
-- This is intentionally NOT automated: granting admin to anyone who supplies the
-- email string is exactly what mandate Rule 9 forbids.
-- ══════════════════════════════════════════════════════════════════════════
