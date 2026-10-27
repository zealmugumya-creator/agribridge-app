-- Rollback for 0007_legal_consent. Drops only what 0007 added.
-- WARNING: this DELETES acceptance and consent evidence. Take a backup first and
-- do not run it in production once users have accepted documents.
begin;

drop function if exists public.record_legal_acceptance(uuid,text,text,text,text,text,jsonb);

drop view if exists public.marketing_consent_current;

drop trigger if exists trg_legal_accept_append_only on public.legal_acceptances;
drop trigger if exists trg_marketing_consent_append_only on public.marketing_consent_events;
drop trigger if exists trg_publication_log_append_only on public.legal_publication_log;
drop trigger if exists trg_legal_versions_immutable on public.legal_document_versions;

drop table if exists public.privacy_requests;
drop table if exists public.marketing_consent_events;
drop table if exists public.legal_acceptances;
drop table if exists public.legal_publication_log;
drop table if exists public.legal_document_versions;

drop function if exists public._append_only();
drop function if exists public._legal_versions_immutable();

commit;
