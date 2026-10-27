-- Rollback for 0005_offer_lifecycle. Drops only what 0005 added.
-- Order matters: functions referencing the tables must drop cleanly (they don't
-- own the tables, so table drops from 0004.down remain separate).
begin;
drop function if exists public.claim_unmatched_requests(int);
drop function if exists public.expire_overdue_offers();
drop function if exists public.reject_offer(uuid, uuid, text);
drop function if exists public.accept_offer(uuid, uuid);
drop function if exists public.match_and_offer(uuid);
drop function if exists public.create_purchase_request(uuid,text,numeric,text,text,text,numeric,uuid);
commit;
