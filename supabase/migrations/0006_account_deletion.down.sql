-- Rollback for 0006_account_deletion. Drops only what 0006 added.
-- NOTE: this does NOT restore anonymized personal data — that is only possible
-- from a database backup taken before the deletion was performed (Rule 7).
begin;
drop function if exists public.mark_account_purged(uuid);
drop function if exists public.request_account_deletion(uuid);
drop function if exists public._acct_anonymize(text,text,uuid,jsonb);
-- Leave farmers.deleted_at in place if other code adopted it; drop the ledger.
drop table if exists public.account_deletion_requests;
do $$ begin
  if to_regclass('public.farmers') is not null then
    alter table public.farmers drop column if exists deleted_at;
  end if;
end $$;
commit;
