-- Rollback for 0008_enforce_profile_name_phone.
-- This reverses only the constraints, index and RPC introduced by that
-- migration. It intentionally does not alter existing farmer profile data.

begin;

revoke execute on function public.update_my_profile(text, text, text) from authenticated;
drop function if exists public.update_my_profile(text, text, text);

drop index if exists public.farmers_phone_unique;
alter table public.farmers
  drop constraint if exists farmers_phone_format,
  drop constraint if exists farmers_full_name_nonempty;

commit;
