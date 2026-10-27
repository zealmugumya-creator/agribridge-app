-- AgriBridge migration 0007 — Versioned legal documents, acceptance records,
-- marketing consent, and privacy requests.
--
-- DESIGN & SAFETY
--  * Additive only: creates new tables/functions. Touches no existing table
--    (Rule 5/6). Safe to re-run (IF NOT EXISTS / OR REPLACE).
--  * Acceptance and consent history is APPEND-ONLY. Triggers reject UPDATE and
--    DELETE, so a user (or a bug) cannot rewrite or erase evidence. Old
--    versions are preserved when a newer version is published.
--  * Users can only READ their own rows (RLS). All writes go through the
--    backend with the service role, which first verifies the user's session.
--    The anon/authenticated roles have no INSERT/UPDATE/DELETE privilege.
--  * `user_id` is a plain uuid (no FK to auth.users) on purpose: the optional
--    hard account purge (migration 0006) must not cascade-delete consent
--    evidence. Rows are pseudonymous. Retention for these records is to be
--    confirmed with a Ugandan advocate (docs/LEGAL_PENDING.md).
--  * All timestamps are timestamptz (stored in UTC).
--  * This migration was written without live database access. Review it, take
--    a backup, and apply it to staging first.

begin;

-- ── Published document versions (immutable once published) ───────────────────
create table if not exists public.legal_document_versions (
  doc_type text not null,
  version text not null,
  title text not null,
  content_sha256 text not null check (content_sha256 ~ '^[0-9a-f]{64}$'),
  effective_at timestamptz not null,
  is_material boolean not null default true,
  requires_acceptance text not null default 'accept'
    check (requires_acceptance in ('accept','acknowledge')),
  applies_to_roles text[] not null default array['all'],
  change_summary text,
  published_by uuid,
  published_at timestamptz not null default now(),
  primary key (doc_type, version),
  constraint legal_version_format check (version ~ '^[0-9]+\.[0-9]+\.[0-9]+$')
);

create or replace function public._legal_versions_immutable() returns trigger
language plpgsql as $$
begin
  if tg_op = 'DELETE' then
    raise exception 'legal document versions cannot be deleted' using errcode = '42501';
  end if;
  -- Allow a no-op re-publish of identical content (idempotent sync), nothing else.
  if new.content_sha256 is distinct from old.content_sha256
     or new.title is distinct from old.title
     or new.effective_at is distinct from old.effective_at
     or new.is_material is distinct from old.is_material
     or new.requires_acceptance is distinct from old.requires_acceptance
     or new.applies_to_roles is distinct from old.applies_to_roles then
    raise exception 'a published legal document version is immutable; publish a new version'
      using errcode = '42501';
  end if;
  return new;
end $$;
drop trigger if exists trg_legal_versions_immutable on public.legal_document_versions;
create trigger trg_legal_versions_immutable
  before update or delete on public.legal_document_versions
  for each row execute function public._legal_versions_immutable();

-- ── Who published what (admin accountability) ────────────────────────────────
create table if not exists public.legal_publication_log (
  id bigint generated always as identity primary key,
  doc_type text not null,
  version text not null,
  action text not null default 'published',
  actor uuid,
  note text,
  created_at timestamptz not null default now(),
  foreign key (doc_type, version) references public.legal_document_versions (doc_type, version)
);

-- ── Acceptance events (append-only) ──────────────────────────────────────────
create table if not exists public.legal_acceptances (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null,
  doc_type text not null,
  version text not null,
  content_sha256 text not null,
  account_role text,
  method text not null
    check (method in ('signup_checkbox','signup_checkbox_deferred','login_review','settings_review')),
  evidence jsonb not null default '{}'::jsonb,
  accepted_at timestamptz not null default now(),
  foreign key (doc_type, version) references public.legal_document_versions (doc_type, version),
  constraint legal_accept_once unique (user_id, doc_type, version)
);
create index if not exists idx_legal_accept_user on public.legal_acceptances (user_id, accepted_at desc);

-- ── Marketing consent events (append-only; latest event per channel wins) ────
create table if not exists public.marketing_consent_events (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null,
  channel text not null check (channel in ('sms','email','whatsapp')),
  granted boolean not null,
  method text not null check (method in ('signup_checkbox','settings','support_request')),
  evidence jsonb not null default '{}'::jsonb,
  recorded_at timestamptz not null default now()
);
create index if not exists idx_marketing_consent_user on public.marketing_consent_events (user_id, channel, recorded_at desc);

create or replace view public.marketing_consent_current
with (security_invoker = true) as
  select distinct on (user_id, channel) user_id, channel, granted, method, recorded_at
  from public.marketing_consent_events
  order by user_id, channel, recorded_at desc, id desc;

-- ── Privacy requests (access, correction, deletion, withdrawal, complaint) ───
create table if not exists public.privacy_requests (
  id uuid primary key default gen_random_uuid(),
  user_id uuid,                       -- null for a request made without signing in
  requester_email text,
  request_type text not null
    check (request_type in ('access','correction','deletion','marketing_withdrawal','complaint','other')),
  details text check (char_length(details) <= 2000),
  identity_verified boolean not null default false,
  status text not null default 'received'
    check (status in ('received','verifying','in_progress','completed','declined')),
  status_note text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  constraint privacy_request_contact check (user_id is not null or requester_email is not null)
);
create index if not exists idx_privacy_requests_status on public.privacy_requests (status, created_at);
create index if not exists idx_privacy_requests_user on public.privacy_requests (user_id, created_at desc);

-- ── Append-only guard shared by acceptance and consent tables ────────────────
create or replace function public._append_only() returns trigger
language plpgsql as $$
begin
  raise exception '% is append-only', tg_table_name using errcode = '42501';
end $$;

drop trigger if exists trg_legal_accept_append_only on public.legal_acceptances;
create trigger trg_legal_accept_append_only
  before update or delete on public.legal_acceptances
  for each row execute function public._append_only();

drop trigger if exists trg_marketing_consent_append_only on public.marketing_consent_events;
create trigger trg_marketing_consent_append_only
  before update or delete on public.marketing_consent_events
  for each row execute function public._append_only();

drop trigger if exists trg_publication_log_append_only on public.legal_publication_log;
create trigger trg_publication_log_append_only
  before update or delete on public.legal_publication_log
  for each row execute function public._append_only();

-- ── Row-level security ───────────────────────────────────────────────────────
alter table public.legal_document_versions enable row level security;
alter table public.legal_publication_log   enable row level security;
alter table public.legal_acceptances       enable row level security;
alter table public.marketing_consent_events enable row level security;
alter table public.privacy_requests        enable row level security;

-- Published versions are public information.
drop policy if exists legal_versions_public_read on public.legal_document_versions;
create policy legal_versions_public_read on public.legal_document_versions
  for select to anon, authenticated using (true);

drop policy if exists legal_versions_service on public.legal_document_versions;
create policy legal_versions_service on public.legal_document_versions
  for all to service_role using (true) with check (true);

drop policy if exists legal_pub_log_service on public.legal_publication_log;
create policy legal_pub_log_service on public.legal_publication_log
  for all to service_role using (true) with check (true);

-- Users read only their own evidence; nobody but the backend writes.
drop policy if exists legal_accept_self_read on public.legal_acceptances;
create policy legal_accept_self_read on public.legal_acceptances
  for select to authenticated using (user_id = auth.uid());
drop policy if exists legal_accept_service on public.legal_acceptances;
create policy legal_accept_service on public.legal_acceptances
  for all to service_role using (true) with check (true);

drop policy if exists marketing_consent_self_read on public.marketing_consent_events;
create policy marketing_consent_self_read on public.marketing_consent_events
  for select to authenticated using (user_id = auth.uid());
drop policy if exists marketing_consent_service on public.marketing_consent_events;
create policy marketing_consent_service on public.marketing_consent_events
  for all to service_role using (true) with check (true);

drop policy if exists privacy_requests_self_read on public.privacy_requests;
create policy privacy_requests_self_read on public.privacy_requests
  for select to authenticated using (user_id = auth.uid());
drop policy if exists privacy_requests_service on public.privacy_requests;
create policy privacy_requests_service on public.privacy_requests
  for all to service_role using (true) with check (true);

-- Defence in depth: remove write privileges from client roles entirely.
revoke insert, update, delete, truncate on
  public.legal_document_versions, public.legal_publication_log,
  public.legal_acceptances, public.marketing_consent_events, public.privacy_requests
  from anon, authenticated;
revoke all on public.privacy_requests from anon;
revoke all on public.legal_acceptances, public.marketing_consent_events, public.legal_publication_log from anon;

-- ── Recording acceptance (backend-only RPC; idempotent) ──────────────────────
-- Checks that the version exists and that the hash the client was shown matches
-- the published hash, so a stale or forged acceptance cannot be recorded.
create or replace function public.record_legal_acceptance(
  p_user_id uuid, p_doc_type text, p_version text, p_content_sha256 text,
  p_account_role text, p_method text, p_evidence jsonb default '{}'::jsonb
) returns jsonb language plpgsql security definer set search_path = public as $$
declare
  v_hash text;
  v_id uuid;
begin
  select content_sha256 into v_hash from public.legal_document_versions
   where doc_type = p_doc_type and version = p_version;
  if v_hash is null then
    raise exception 'unknown document version' using errcode = 'P0002';
  end if;
  if v_hash <> p_content_sha256 then
    raise exception 'document hash mismatch' using errcode = '22023';
  end if;
  insert into public.legal_acceptances
    (user_id, doc_type, version, content_sha256, account_role, method, evidence)
  values (p_user_id, p_doc_type, p_version, v_hash, p_account_role, p_method,
          coalesce(p_evidence, '{}'::jsonb))
  on conflict (user_id, doc_type, version) do nothing
  returning id into v_id;
  return jsonb_build_object('recorded', v_id is not null, 'already_accepted', v_id is null);
end $$;
revoke all on function public.record_legal_acceptance(uuid,text,text,text,text,text,jsonb) from public, anon, authenticated;
grant execute on function public.record_legal_acceptance(uuid,text,text,text,text,text,jsonb) to service_role;

commit;
