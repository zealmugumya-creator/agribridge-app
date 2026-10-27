-- Rollback for 0004_farmer_matching. Drops only what 0004 added.
begin;
drop function if exists public.match_eligible_farmers(text, numeric, text);
drop table if exists public.matching_config;
drop table if exists public.order_offers;
drop table if exists public.purchase_requests;
commit;
