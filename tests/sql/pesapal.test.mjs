/**
 * Copyright (C) 2026 Mugumya Zeal. All rights reserved.
 */

// Verifies migration 0017 (Pesapal gateway state, subscriptions, recurring
// charges and the widened orders.payment_method check) against PGlite:
// idempotent apply, service-role-only access, uniqueness/idempotency guards,
// the recurring-charge FK, and the reversible down path.
// Run:  cd tests/sql && npm test
import { PGlite } from '@electric-sql/pglite';
import { readFileSync } from 'node:fs';
import assert from 'node:assert/strict';

const dir = new URL('../../supabase/migrations/', import.meta.url);
const up = readFileSync(new URL('0017_pesapal_payments.up.sql', dir), 'utf8');
const down = readFileSync(new URL('0017_pesapal_payments.down.sql', dir), 'utf8');

const db = new PGlite();
await db.exec(`
  create role anon nologin;
  create role authenticated nologin;
  create role service_role nologin bypassrls;
  grant usage on schema public to anon, authenticated, service_role;

  -- Production orders table carried a narrow legacy check; reproduce it so the
  -- migration has to find and replace it.
  create table public.orders (
    id uuid primary key default gen_random_uuid(),
    payment_method text
      check (payment_method in ('mtn_momo','airtel_money','cash')) -- orders_payment_method_check
  );
  insert into public.orders (payment_method) values ('cash');
`);

let passed = 0;
const ok = (name) => { passed++; console.log('  ok -', name); };
const as = async (role, sql, params) => {
  await db.exec(`set role ${role};`);
  try { return await db.query(sql, params); } finally { await db.exec('reset role;'); }
};
const rejects = async (fn, re, name) => { await assert.rejects(fn, re); ok(name); };

// ── Apply ──────────────────────────────────────────────────────────────────
await db.exec(up);
await db.exec(up);
ok('migration applies and re-applies idempotently');

// ── Widened payment_method (NOT VALID) ─────────────────────────────────────
const legacy = await db.query(`select count(*)::int n from public.orders where payment_method='cash'`);
assert.equal(legacy.rows[0].n, 1, 'pre-existing row survives constraint replacement');
for (const method of ['mtn_momo', 'airtel_money', 'cash', 'bank', 'card', 'momo', 'airtel', 'pesapal', 'direct', 'mobile_money']) {
  await db.query('insert into public.orders (payment_method) values ($1)', [method]);
}
ok('every payment method the checkout offers is accepted');

await db.query('insert into public.orders (payment_method) values (null)');
ok('a null payment method is still accepted');

await assert.rejects(
  () => db.query("insert into public.orders (payment_method) values ('crypto')"),
  /check constraint/i,
);
ok('unknown payment methods are still rejected');

const def = await db.query(`
  select convalidated from pg_constraint
   where conname='orders_payment_method_allowed' and conrelid='public.orders'::regclass
`);
assert.equal(def.rows.length, 1);
assert.equal(def.rows[0].convalidated, false, 'added NOT VALID so legacy rows are not rescanned');
ok('the new constraint is added without rescanning legacy orders');

// ── RLS and grants ─────────────────────────────────────────────────────────
for (const table of ['payment_gateway_state', 'payment_subscriptions', 'subscription_charges']) {
  const r = await db.query(
    `select relrowsecurity from pg_class where oid = $1::regclass`,
    [`public.${table}`],
  );
  assert.equal(r.rows[0].relrowsecurity, true, `${table} has RLS`);
  await rejects(() => as('anon', `select * from public.${table}`), /permission denied/i,
    `${table}: anon cannot read`);
  await rejects(() => as('authenticated', `select * from public.${table}`), /permission denied/i,
    `${table}: authenticated cannot read`);
}
ok('gateway state, subscriptions and charges are service-role only');

// ── Gateway state ──────────────────────────────────────────────────────────
await as('service_role',
  `insert into public.payment_gateway_state (provider, config)
   values ('pesapal', '{"ipn_id":"ipn-1"}'::jsonb)`);
let r = await as('service_role',
  `select config->>'ipn_id' ipn from public.payment_gateway_state where provider='pesapal'`);
assert.equal(r.rows[0].ipn, 'ipn-1');
await rejects(() => as('service_role',
  `insert into public.payment_gateway_state (provider) values ('pesapal')`),
  /duplicate key/i, 'gateway state is one row per provider');
ok('the registered IPN id is stored once per provider');

// ── Subscriptions and charges ──────────────────────────────────────────────
await as('service_role',
  `insert into public.payment_subscriptions
     (provider, provider_correlation_id, account_ref, buyer_id, amount, status)
   values ('Pesapal','corr-1','AB-100','22222222-2222-2222-2222-222222222222',25000,'active')`);
const sub = await db.query(
  `select id from public.payment_subscriptions where provider='Pesapal' and provider_correlation_id='corr-1'`);
const subId = sub.rows[0].id;

await rejects(() => as('service_role',
  `insert into public.payment_subscriptions
     (provider, provider_correlation_id, account_ref) values ('Pesapal','corr-1','AB-101')`),
  /duplicate key/i, 'a Pesapal correlation id is recorded once');
await rejects(() => as('service_role',
  `insert into public.payment_subscriptions
     (provider, provider_correlation_id, account_ref, status) values ('Pesapal','corr-2','AB-102','paused')`),
  /check constraint/i, 'unknown subscription statuses are refused');
ok('subscription correlation ids are unique and statuses are constrained');

await as('service_role',
  `insert into public.subscription_charges
     (subscription_id, provider, provider_tracking_id, confirmation_code, account_ref,
      amount, status, ledger_reference)
   values ($1,'Pesapal','track-1','conf-1','AB-100',25000,'paid','recurring:track-1')`, [subId]);
await rejects(() => as('service_role',
  `insert into public.subscription_charges
     (subscription_id, provider, provider_tracking_id, account_ref, amount, status)
   values ($1,'Pesapal','track-1','AB-100',25000,'paid')`, [subId]),
  /duplicate key/i, 'a replayed IPN cycle cannot be charged twice');
ok('each recurring tracking id is recorded exactly once');

await db.query('delete from public.payment_subscriptions where id=$1', [subId]);
r = await db.query('select subscription_id from public.subscription_charges where provider_tracking_id=$1', ['track-1']);
assert.equal(r.rows[0].subscription_id, null, 'charge history outlives the subscription row');
ok('deleting a subscription keeps its charge history');

// ── Down path ──────────────────────────────────────────────────────────────
await db.exec(down);
r = await db.query(`select count(*)::int n from pg_class
  where relname in ('payment_gateway_state','payment_subscriptions','subscription_charges')`);
assert.equal(r.rows[0].n, 0);
ok('down removes the gateway tables');

r = await db.query(`select count(*)::int n from pg_constraint
  where conname='orders_payment_method_allowed' and conrelid='public.orders'::regclass`);
assert.equal(r.rows[0].n, 0);
const restored = await db.query(`select convalidated from pg_constraint
  where conname='orders_payment_method_check' and conrelid='public.orders'::regclass`);
assert.equal(restored.rows.length, 1);
assert.equal(restored.rows[0].convalidated, false);
ok('down restores the legacy check NOT VALID');

r = await db.query(`select count(*)::int n from public.orders where payment_method='pesapal'`);
assert.equal(r.rows[0].n, 1, 'existing Pesapal orders are kept');
await assert.rejects(
  () => db.query("insert into public.orders (payment_method) values ('pesapal')"),
  /check constraint/i,
);
ok('after rollback existing rows are kept but new Pesapal orders are refused');

await db.exec(up);
await db.query("insert into public.orders (payment_method) values ('pesapal')");
ok('migration can be re-applied after a down');

console.log(`\n${passed} assertions passed (Pesapal payments)`);
