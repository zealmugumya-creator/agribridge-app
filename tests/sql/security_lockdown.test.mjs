/**
 * Copyright (C) 2026 Mugumya Zeal. All rights reserved.
 */

// Verifies migration 0014 against a real PostgreSQL engine (PGlite).
// It first reproduces the production state (legacy 0008 + Supabase-style
// default grants) so the anon read leak is proven, then applies the fix.
// Run:  cd tests/sql && npm install && npm test
import { PGlite } from '@electric-sql/pglite';
import { readFileSync } from 'node:fs';
import assert from 'node:assert/strict';

const migrationDir = new URL('../../supabase/migrations/', import.meta.url);
const legacy = readFileSync(new URL('../../migrations/0008_transporters_and_system_events.sql', import.meta.url), 'utf8');
const up = readFileSync(new URL('0014_lockdown_internal_tables.up.sql', migrationDir), 'utf8');
const down = readFileSync(new URL('0014_lockdown_internal_tables.down.sql', migrationDir), 'utf8');

const db = new PGlite();
await db.exec(`
  create role anon nologin;
  create role authenticated nologin;
  create role service_role nologin bypassrls;
  create schema auth;
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
const rejects = async (fn, re, name) => { await assert.rejects(fn, re); ok(name); };

const USER = '22222222-2222-2222-2222-222222222222';
const OTHER = '33333333-3333-3333-3333-333333333333';

// ── Reproduce the live pre-fix state ────────────────────────────────────────
// The repository copy of 0008 is patched, so apply it first and prove it is
// safe on its own. Then recreate the original unscoped policy (which applied
// to PUBLIC) to reproduce what production actually ran before the fix.
await db.exec(legacy);
// Supabase grants ALL on tables in public to these roles by default.
await db.exec(`
  grant all on public.system_events to anon, authenticated, service_role;
  grant all on public.transporters to anon, authenticated, service_role;
  insert into public.system_events (event_type, severity, details)
  values ('order_failed','error','{"buyer_id":"secret"}'::jsonb);
  insert into public.transporters (supabase_uid, full_name, phone)
  values ('44444444-4444-4444-4444-444444444444','Seed','0700000009');
`);

let r = await as('anon', null, 'select count(*)::int c from public.system_events');
assert.equal(r.rows[0].c, 0);
ok('patched 0008 alone no longer exposes system_events');

await db.exec(`
  drop policy "service role full access" on public.system_events;
  create policy "service role full access" on public.system_events using (true) with check (true);
  drop policy "service role full access" on public.transporters;
  create policy "service role full access" on public.transporters using (true) with check (true);
`);

r = await as('anon', null, 'select count(*)::int c from public.system_events');
assert.equal(r.rows[0].c, 1);
r = await as('anon', null, 'select count(*)::int c from public.transporters');
assert.equal(r.rows[0].c, 1);
ok('pre-fix: anon can read internal events and transporters (vulnerability reproduced)');

// ── Apply the fix ───────────────────────────────────────────────────────────
await db.exec(up);
await db.exec(up);
ok('migration applies and re-applies idempotently');

await rejects(() => as('anon', null, 'select * from public.system_events'),
  /permission denied/, 'anon cannot read system_events');
await rejects(() => as('authenticated', USER, 'select * from public.system_events'),
  /permission denied/, 'authenticated cannot read system_events');
await rejects(() => as('anon', null, `insert into public.system_events (event_type) values ('forged')`),
  /permission denied/, 'anon cannot insert system_events');
await rejects(() => as('anon', null, `update public.system_events set severity='info'`),
  /permission denied/, 'anon cannot update system_events');
await rejects(() => as('anon', null, 'delete from public.system_events'),
  /permission denied/, 'anon cannot delete system_events');

r = await as('service_role', null, 'select count(*)::int c from public.system_events');
assert.equal(r.rows[0].c, 1);
ok('service_role still reads system_events');

// ── Transporters: owner-only client access ─────────────────────────────────
r = await as('service_role', null,
  `insert into public.transporters (supabase_uid, full_name, phone)
   values ($1,'Owner','0700000001'), ($2,'Other','0700000002') returning id`, [USER, OTHER]);
const [ownId, otherId] = r.rows.map((x) => x.id);

await rejects(() => as('anon', null, 'select * from public.transporters'),
  /permission denied/, 'anon cannot read transporters');

r = await as('authenticated', USER, 'select supabase_uid from public.transporters');
assert.equal(r.rows.length, 1);
assert.equal(r.rows[0].supabase_uid, USER);
ok('transporter sees only own row');

await as('authenticated', USER, 'update public.transporters set is_available=true where id=$1', [ownId]);
r = await as('authenticated', USER, 'select is_available from public.transporters where id=$1', [ownId]);
assert.equal(r.rows[0].is_available, true);
ok('transporter can update own availability');

await as('authenticated', USER, 'update public.transporters set is_available=true where id=$1', [otherId]);
r = await as('service_role', null, 'select is_available from public.transporters where id=$1', [otherId]);
assert.equal(r.rows[0].is_available, false);
ok('transporter cannot update another transporter');

await rejects(() => as('authenticated', USER,
  `insert into public.transporters (full_name, phone) values ('X','0700000000')`),
  /permission denied/, 'authenticated cannot insert transporters');

r = await as('service_role', null, 'select count(*)::int c from public.transporters');
assert.equal(r.rows[0].c, 3);
ok('service_role still manages transporters');

// ── Down migration must not reopen the hole ─────────────────────────────────
await db.exec(down);
await rejects(() => as('anon', null, 'select * from public.system_events'),
  /permission denied/, 'down migration does not reopen anon access');
r = await as('service_role', null, 'select count(*)::int c from public.system_events');
assert.equal(r.rows[0].c, 1);
ok('data preserved through down migration');

console.log(`\n${passed} security assertions passed`);
