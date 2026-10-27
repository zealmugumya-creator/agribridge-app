/**
 * Copyright (C) 2026 Mugumya Zeal. All rights reserved.
 */

// Verifies migration 0007 against a real PostgreSQL engine (PGlite).
// Supabase-specific pieces (roles, auth.uid, auth.users) are stubbed minimally.
// Run:  cd tests/sql && npm install && npm test
import { PGlite } from '@electric-sql/pglite';
import { readFileSync } from 'node:fs';
import assert from 'node:assert/strict';

const dir = new URL('../../supabase/migrations/', import.meta.url);
const up = readFileSync(new URL('0007_legal_consent.up.sql', dir), 'utf8');
const down = readFileSync(new URL('0007_legal_consent.down.sql', dir), 'utf8');

const db = new PGlite();
await db.exec(`
  create role anon nologin; create role authenticated nologin; create role service_role nologin;
  create schema auth;
  create table auth.users (id uuid primary key);
  create function auth.uid() returns uuid language sql stable as
    $$ select nullif(current_setting('request.jwt.claim.sub', true), '')::uuid $$;
  grant usage on schema public, auth to anon, authenticated, service_role;
  grant execute on function auth.uid() to anon, authenticated, service_role;
`);

let passed = 0;
const ok = (name) => { passed++; console.log('  ok -', name); };
const as = async (role, uid, sql, params) => {
  await db.exec(`set role ${role}; select set_config('request.jwt.claim.sub','${uid ?? ''}',false);`);
  try { return await db.query(sql, params); } finally { await db.exec('reset role;'); }
};
const rejects = async (fn, re, name) => {
  await assert.rejects(fn, re); ok(name);
};

// Apply (and prove it is idempotent by applying twice).
await db.exec(up); await db.exec(up); ok('migration applies, and re-applies idempotently');
// Grants on new tables for service_role (Supabase default privileges).
await db.exec(`grant all on all tables in schema public to service_role;
               grant select on all tables in schema public to authenticated;
               grant select on public.legal_document_versions to anon;`);
await db.exec(up); // revokes must survive a re-run
await db.exec(`grant all on all tables in schema public to service_role;`);

const U1 = '11111111-1111-1111-1111-111111111111';
const U2 = '22222222-2222-2222-2222-222222222222';
const H = 'a'.repeat(64);

await db.exec(`insert into public.legal_document_versions
  (doc_type, version, title, content_sha256, effective_at)
  values ('terms-of-use','1.0.0','Terms','${H}', now()),
         ('privacy-policy','1.0.0','Privacy','${H}', now())`);

// RPC: records, is idempotent, checks hash and version.
let r = await as('service_role', null,
  `select public.record_legal_acceptance($1,'terms-of-use','1.0.0',$2,'farmer','signup_checkbox','{}') as r`, [U1, H]);
assert.equal(r.rows[0].r.recorded, true); ok('acceptance recorded with version and hash');
r = await as('service_role', null,
  `select public.record_legal_acceptance($1,'terms-of-use','1.0.0',$2,'farmer','login_review','{}') as r`, [U1, H]);
assert.equal(r.rows[0].r.already_accepted, true); ok('repeat acceptance is idempotent (no duplicate, no overwrite)');
await rejects(() => as('service_role', null,
  `select public.record_legal_acceptance($1,'terms-of-use','1.0.0',$2,'farmer','signup_checkbox','{}')`, [U1, 'b'.repeat(64)]),
  /hash mismatch/, 'wrong document hash rejected');
await rejects(() => as('service_role', null,
  `select public.record_legal_acceptance($1,'terms-of-use','9.9.9',$2,'farmer','signup_checkbox','{}')`, [U1, H]),
  /unknown document version/, 'unpublished version rejected');
await rejects(() => as('service_role', null,
  `select public.record_legal_acceptance($1,'terms-of-use','1.0.0',$2,'farmer','made_up','{}')`, [U2, H]),
  /check constraint|violates/, 'unknown acceptance method rejected');

// Append-only: even the service role cannot edit or delete evidence.
await rejects(() => as('service_role', null, `update public.legal_acceptances set version='1.0.0' where user_id=$1`, [U1]),
  /append-only/, 'acceptance rows cannot be updated');
await rejects(() => as('service_role', null, `delete from public.legal_acceptances where user_id=$1`, [U1]),
  /append-only/, 'acceptance rows cannot be deleted');

// Users cannot write, and cannot read someone else's rows.
await rejects(() => as('authenticated', U1,
  `insert into public.legal_acceptances (user_id,doc_type,version,content_sha256,method) values ($1,'terms-of-use','1.0.0',$2,'settings_review')`, [U2, H]),
  /permission denied/, 'authenticated users cannot insert acceptances (cannot forge)');
await rejects(() => as('authenticated', U1, `update public.legal_acceptances set account_role='admin'`),
  /permission denied/, 'authenticated users cannot update acceptances');
await rejects(() => as('authenticated', U1, `delete from public.legal_acceptances`),
  /permission denied/, 'authenticated users cannot delete acceptances');
await as('service_role', null,
  `select public.record_legal_acceptance($1,'privacy-policy','1.0.0',$2,'vendor','signup_checkbox','{}')`, [U2, H]);
r = await as('authenticated', U1, `select user_id from public.legal_acceptances`);
assert.ok(r.rows.length >= 1 && r.rows.every(x => x.user_id === U1)); ok('user sees only their own acceptances (RLS)');
r = await as('authenticated', null, `select user_id from public.legal_acceptances`);
assert.equal(r.rows.length, 0); ok('no session sees no acceptances');
await rejects(() => as('anon', null, `select * from public.legal_acceptances`), /permission denied/, 'anon cannot read acceptances');
await rejects(() => as('authenticated', U1,
  `select public.record_legal_acceptance($1,'terms-of-use','1.0.0',$2,'farmer','signup_checkbox','{}')`, [U1, H]),
  /permission denied/, 'authenticated users cannot call the recording RPC directly');

// Published versions: public read, immutable.
r = await as('anon', null, `select doc_type from public.legal_document_versions order by 1`);
assert.equal(r.rows.length, 2); ok('anon can read published versions');
await rejects(() => as('anon', null, `insert into public.legal_document_versions (doc_type,version,title,content_sha256,effective_at) values ('x','1.0.0','x','${H}',now())`),
  /permission denied/, 'anon cannot publish a version');
await rejects(() => as('service_role', null, `update public.legal_document_versions set content_sha256='${'c'.repeat(64)}' where doc_type='terms-of-use'`),
  /immutable/, 'published version content cannot be changed');
await rejects(() => as('service_role', null, `delete from public.legal_document_versions where doc_type='terms-of-use'`),
  /cannot be deleted/, 'published version cannot be deleted');
await as('service_role', null, `update public.legal_document_versions set change_summary='note' where doc_type='terms-of-use'`);
ok('non-content metadata (change_summary) may still be annotated');

// Marketing consent: append-only, latest event wins, user-readable only for self.
await as('service_role', null,
  `insert into public.marketing_consent_events (user_id,channel,granted,method) values ($1,'sms',true,'signup_checkbox')`, [U1]);
await as('service_role', null,
  `insert into public.marketing_consent_events (user_id,channel,granted,method) values ($1,'sms',false,'settings')`, [U1]);
r = await as('authenticated', U1, `select granted from public.marketing_consent_current where channel='sms'`);
assert.deepEqual(r.rows.map(x => x.granted), [false]); ok('withdrawal is the current state; history kept');
r = await as('authenticated', U1, `select count(*)::int c from public.marketing_consent_events`);
assert.equal(r.rows[0].c, 2); ok('consent history preserved (grant + withdrawal)');
r = await as('authenticated', U2, `select count(*)::int c from public.marketing_consent_events`);
assert.equal(r.rows[0].c, 0); ok('other users cannot see consent events');
await rejects(() => as('service_role', null, `delete from public.marketing_consent_events`), /append-only/, 'consent events cannot be deleted');
await rejects(() => as('authenticated', U1, `insert into public.marketing_consent_events (user_id,channel,granted,method) values ($1,'sms',true,'settings')`, [U1]),
  /permission denied/, 'users cannot write consent directly');

// Privacy requests.
await as('service_role', null, `insert into public.privacy_requests (requester_email, request_type) values ('a@b.co','access')`);
await rejects(() => as('service_role', null, `insert into public.privacy_requests (request_type) values ('access')`),
  /privacy_request_contact/, 'a request needs a user or an email');
await rejects(() => as('authenticated', U1, `insert into public.privacy_requests (user_id, request_type) values ($1,'access')`, [U1]),
  /permission denied/, 'users file requests through the backend only');
r = await as('authenticated', U1, `select count(*)::int c from public.privacy_requests`);
assert.equal(r.rows[0].c, 0); ok('users cannot read other people\'s requests');
await rejects(() => as('anon', null, `select * from public.privacy_requests`), /permission denied/, 'anon cannot read privacy requests');

// Rollback removes everything it added and nothing else.
await db.exec(down);
r = await db.query(`select count(*)::int c from information_schema.tables where table_schema='public'`);
assert.equal(r.rows[0].c, 0); ok('down migration removes all 0007 objects');
await db.exec(up); ok('up works again after rollback');

console.log(`\n${passed} checks passed`);
