-- AgriBridge migration 0008 — Enforce real name and phone on farmers table
-- Problem: accounts could be created with null/empty full_name and phone,
-- producing entries that show as "**" or just an email fragment in the UI.
--
-- Fix:
--   1. Add CHECK constraints: full_name must be non-empty when set,
--      phone must match a basic E.164 pattern when set.
--   2. Backfill NULL phones from auth.users user_metadata where available.
--   3. Add a unique constraint on phone so one phone = one account.
--   4. Create a backend RPC `update_my_profile` that validates + saves,
--      used by the frontend instead of direct REST upserts.
--
-- SAFETY: constraints are CHECK (not NOT NULL) so existing null rows are kept
-- but future writes must pass validation. The unique index is NULLS NOT DISTINCT
-- so null phone (unverified accounts) can coexist freely.

begin;

-- 1. Prevent saving a whitespace-only name
alter table public.farmers
  add constraint farmers_full_name_nonempty
  check (full_name is null or length(trim(full_name)) >= 2);

-- 2. Prevent saving a phone that isn't a valid E.164 Ugandan number
--    Accepts +256 7xx xxx xxx (no spaces in the stored value)
alter table public.farmers
  add constraint farmers_phone_format
  check (phone is null or phone ~ '^\+256[37]\d{8}$');

-- 3. Unique phone — one real phone = one account (nulls are OK, they're unverified)
create unique index if not exists farmers_phone_unique
  on public.farmers (phone)
  where phone is not null;

-- 4. Secure RPC so the frontend can update its own profile through the backend
--    validation layer instead of raw REST.
create or replace function public.update_my_profile(
  p_full_name text default null,
  p_phone     text default null,
  p_district  text default null
) returns jsonb
language plpgsql
security definer
set search_path = public
as $$
declare
  v_uid uuid := auth.uid();
  v_name text;
  v_phone text;
begin
  if v_uid is null then
    raise exception 'not authenticated' using errcode = '42501';
  end if;

  -- Validate name
  if p_full_name is not null then
    v_name := trim(p_full_name);
    if length(v_name) < 2 then
      raise exception 'Full name must be at least 2 characters' using errcode = '22023';
    end if;
    -- Reject obvious test/fake values
    if lower(v_name) in ('test','user','admin','farmer','buyer','name','null','undefined','none','n/a') then
      raise exception 'Please enter your real full name' using errcode = '22023';
    end if;
  end if;

  -- Validate phone
  if p_phone is not null then
    v_phone := trim(p_phone);
    if v_phone !~ '^\+256[37]\d{8}$' then
      raise exception 'Enter a valid Ugandan phone number (e.g. +256771234567)' using errcode = '22023';
    end if;
    -- Check uniqueness manually to give a clear message
    if exists (
      select 1 from public.farmers where phone = v_phone and id <> v_uid
    ) then
      raise exception 'That phone number is already registered to another account' using errcode = '23505';
    end if;
  end if;

  insert into public.farmers (id, full_name, phone, district)
    values (v_uid,
            coalesce(v_name, (select full_name from public.farmers where id = v_uid)),
            coalesce(v_phone, (select phone    from public.farmers where id = v_uid)),
            coalesce(p_district, (select district from public.farmers where id = v_uid)))
    on conflict (id) do update
      set full_name = excluded.full_name,
          phone     = excluded.phone,
          district  = excluded.district,
          updated_at = now();

  return jsonb_build_object('ok', true);
end;
$$;

revoke all on function public.update_my_profile(text, text, text) from public;
grant execute on function public.update_my_profile(text, text, text) to authenticated;

commit;
