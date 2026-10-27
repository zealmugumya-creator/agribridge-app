-- AgriBridge migration 0006 — Account deletion / GDPR-style erasure (Phase 10;
-- fixes audit finding H6: no account deletion flow, a Google Play blocker).
--
-- DESIGN & SAFETY
--  * User-initiated and self-scoped: the RPC only acts on the caller's own id
--    (auth.uid()) unless the caller is a server-controlled admin (Rule 9).
--  * NON-destructive to the financial ledger. `orders`/`payouts`/`reviews` are
--    retained (they are pseudonymous, keyed by uuid, and legally/financially
--    required). Personal identifiers are ANONYMIZED, not hard-deleted, so the
--    action is reversible from a backup (Rule 7) and never silently destroys
--    accounting history (Rule 6).
--  * SCHEMA-DEFENSIVE: this migration was authored without live access to the
--    shared Supabase project. Every UPDATE is guarded by to_regclass() and
--    information_schema column checks, so it applies cleanly even if a table or
--    column differs from the AgriBridge model, and it will NOT touch unrelated
--    applications' tables (Rule 5).
--  * The irreversible step — purging the GoTrue auth.users row — is NOT done
--    here. It is performed by the backend only when ACCOUNT_HARD_DELETE_ENABLED
--    is explicitly set, after operator review of FK ON DELETE behaviour and a
--    fresh backup (Rule 8: no destructive prod op without approval).
--
-- Review before applying.

begin;

-- ── Deletion request ledger (audit + operator queue) ─────────────────────────
create table if not exists public.account_deletion_requests (
  user_id uuid primary key,
  requested_by uuid,
  status text not null default 'requested',   -- requested|anonymized|purged|failed
  requested_at timestamptz not null default now(),
  anonymized_at timestamptz,
  purged_at timestamptz,
  note text
);
create index if not exists idx_acct_del_status on public.account_deletion_requests (status, requested_at desc);
alter table public.account_deletion_requests enable row level security;
drop policy if exists acct_del_service on public.account_deletion_requests;
create policy acct_del_service on public.account_deletion_requests
  for all to service_role using (true) with check (true);
-- A user may read their own request status; nobody else's.
drop policy if exists acct_del_self_read on public.account_deletion_requests;
create policy acct_del_self_read on public.account_deletion_requests
  for select to authenticated using (user_id = auth.uid());

-- ── Tombstone marker on the profile (additive, reversible) ───────────────────
-- Only added if the AgriBridge `farmers` profile table exists.
do $$ begin
  if to_regclass('public.farmers') is not null then
    alter table public.farmers add column if not exists deleted_at timestamptz;
  end if;
end $$;

-- ── Schema-defensive column anonymizer (private helper) ──────────────────────
-- Applies `p_map` (column -> replacement text) to the row(s) in `p_table` where
-- `p_id_col = p_id`, silently skipping any table or column that does not exist.
create or replace function public._acct_anonymize(
  p_table text, p_id_col text, p_id uuid, p_map jsonb
) returns void language plpgsql security definer set search_path = public as $$
declare
  v_set text := '';
  k text; v text;
begin
  if p_id is null then return; end if;
  if to_regclass('public.' || p_table) is null then return; end if;
  if not exists (
    select 1 from information_schema.columns
    where table_schema='public' and table_name=p_table and column_name=p_id_col
  ) then return; end if;

  for k, v in select * from jsonb_each_text(p_map) loop
    if exists (
      select 1 from information_schema.columns
      where table_schema='public' and table_name=p_table and column_name=k
    ) then
      -- A NULL replacement clears the value; otherwise store the tombstone text.
      if v is null then
        v_set := v_set || format(' %I = NULL,', k);
      else
        v_set := v_set || format(' %I = %L,', k, v);
      end if;
    end if;
  end loop;
  if v_set = '' then return; end if;
  v_set := rtrim(v_set, ',');
  execute format('update public.%I set %s where %I = $1', p_table, v_set, p_id_col) using p_id;
end; $$;
revoke all on function public._acct_anonymize(text,text,uuid,jsonb) from public;
grant execute on function public._acct_anonymize(text,text,uuid,jsonb) to service_role;

-- ── User-facing erasure RPC (self-scoped) ────────────────────────────────────
create or replace function public.request_account_deletion(p_user_id uuid)
returns jsonb language plpgsql security definer set search_path = public as $$
declare
  v_actor uuid := auth.uid();
begin
  if p_user_id is null then
    raise exception 'user id required' using errcode='22023';
  end if;
  -- Self-service, or a server-verified admin acting on someone's behalf.
  if v_actor is not null and v_actor <> p_user_id and not public.is_admin(v_actor) then
    raise exception 'forbidden' using errcode='42501';
  end if;

  insert into public.account_deletion_requests (user_id, requested_by, status)
  values (p_user_id, coalesce(v_actor, p_user_id), 'anonymizing')
  on conflict (user_id) do update
    set requested_by = coalesce(v_actor, p_user_id), status = 'anonymizing',
        requested_at = now();

  -- Profile: strip personal identifiers, leave a tombstone.
  perform public._acct_anonymize('farmers', 'id', p_user_id, jsonb_build_object(
    'name', 'Deleted user', 'phone', null, 'email', null));
  if to_regclass('public.farmers') is not null
     and exists (select 1 from information_schema.columns
                 where table_schema='public' and table_name='farmers' and column_name='deleted_at') then
    update public.farmers set deleted_at = now() where id = p_user_id;
  end if;

  -- Free-text / contact PII in supporting features (each no-ops if absent).
  perform public._acct_anonymize('contact_messages', 'user_id', p_user_id,
    jsonb_build_object('name','Deleted user','email',null,'phone',null,'message','[deleted by user]'));
  perform public._acct_anonymize('reviews', 'buyer_id', p_user_id,
    jsonb_build_object('reviewer_name','Deleted user'));
  perform public._acct_anonymize('ussd_sessions', 'user_id', p_user_id,
    jsonb_build_object('phone', null));
  perform public._acct_anonymize('vet_bookings', 'user_id', p_user_id,
    jsonb_build_object('contact_name','Deleted user','contact_phone',null));
  perform public._acct_anonymize('disease_reports', 'user_id', p_user_id,
    jsonb_build_object('farmer_name','Deleted user','phone',null));

  -- Drop push tokens + role so the account can no longer receive admin alerts.
  if to_regclass('public.admin_devices') is not null then
    delete from public.admin_devices where user_id = p_user_id;
  end if;

  update public.account_deletion_requests
    set status = 'anonymized', anonymized_at = now()
    where user_id = p_user_id;

  insert into public.admin_audit_log (actor, action, target, metadata)
  values (coalesce(v_actor, p_user_id), 'account.anonymized', p_user_id::text,
          jsonb_build_object('hard_delete_pending', true));

  return jsonb_build_object('status', 'anonymized', 'user_id', p_user_id);
end; $$;
revoke all on function public.request_account_deletion(uuid) from public;
grant execute on function public.request_account_deletion(uuid) to authenticated, service_role;

-- ── Purge marker (service_role only; called after the GoTrue admin delete) ────
create or replace function public.mark_account_purged(p_user_id uuid)
returns void language plpgsql security definer set search_path = public as $$
begin
  update public.account_deletion_requests
    set status = 'purged', purged_at = now() where user_id = p_user_id;
  insert into public.admin_audit_log (actor, action, target, metadata)
  values (p_user_id, 'account.purged', p_user_id::text, '{}'::jsonb);
end; $$;
revoke all on function public.mark_account_purged(uuid) from public, authenticated;
grant execute on function public.mark_account_purged(uuid) to service_role;

commit;

-- ══════════════════════════════════════════════════════════════════════════
-- OPERATOR NOTE — hard purge (irreversible). Only after: (1) a fresh backup,
-- (2) reviewing FK ON DELETE behaviour on orders/payouts/reviews so purging
-- auth.users cannot cascade-destroy the financial ledger, and (3) explicit
-- approval. Then, with the service role:
--     delete from auth.users where id = '<USER-UUID>';
--     update public.account_deletion_requests set status='purged', purged_at=now()
--       where user_id = '<USER-UUID>';
-- The backend performs this automatically ONLY when ACCOUNT_HARD_DELETE_ENABLED
-- is truthy; by default it stops at 'anonymized' (safe + reversible).
-- ══════════════════════════════════════════════════════════════════════════
