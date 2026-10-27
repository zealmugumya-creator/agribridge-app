/**
 * Copyright (C) 2026 Mugumya Zeal. All rights reserved.
 */

// Verifies migration 0016 (farmer settlements) against PGlite:
// payout creation on delivered+paid, commission snapshot, backfill,
// the posted-journal guard in mark_payout_paid, grants, and the down path.
// Run:  cd tests/sql && npm test
import { PGlite } from '@electric-sql/pglite';
import { readFileSync } from 'node:fs';
import assert from 'node:assert/strict';

const dir = new URL('../../supabase/migrations/', import.meta.url);
const up = readFileSync(new URL('0016_farmer_settlements.up.sql', dir), 'utf8');
const down = readFileSync(new URL('0016_farmer_settlements.down.sql', dir), 'utf8');

const db = new PGlite();
await db.exec(`
  create role anon nologin;
  create role authenticated nologin;
  create role service_role nologin;
  grant usage on schema public to anon, authenticated, service_role;

  create table public.platform_config (
    id integer primary key default 1 check (id = 1),
    commission_pct numeric not null default 0
  );
  insert into public.platform_config (id, commission_pct) values (1, 5);

  create table public.orders (
    id uuid primary key default gen_random_uuid(),
    farmer_id uuid,
    status text default 'pending',
    payment_status text default 'unpaid',
    total_amount numeric,
    total_price numeric
  );

  create table public.payouts (
    id uuid primary key default gen_random_uuid(),
    order_id uuid unique references public.orders(id),
    farmer_id uuid,
    gross_amount numeric not null default 0,
    commission_pct numeric not null default 0,
    commission_amount numeric not null default 0,
    net_amount numeric not null default 0,
    status text not null default 'pending',
    provider text, provider_ref text,
    created_at timestamptz default now(), paid_at timestamptz
  );

  create table public.ledger_journals (
    id uuid primary key default gen_random_uuid(),
    reference text not null unique,
    kind text not null,
    status text not null default 'posted'
  );
`);

let passed = 0;
const ok = (name) => { passed++; console.log('  ok -', name); };
const as = async (role, sql, params) => {
  await db.exec(`set role ${role};`);
  try { return await db.query(sql, params); } finally { await db.exec('reset role;'); }
};

const FARMER = '20000000-0000-0000-0000-000000000001';
const O1 = '50000000-0000-0000-0000-000000000001';
const O2 = '50000000-0000-0000-0000-000000000002';
const O3 = '50000000-0000-0000-0000-000000000003';
const O4 = '50000000-0000-0000-0000-000000000004';
const O5 = '50000000-0000-0000-0000-000000000005';
const O6 = '50000000-0000-0000-0000-000000000006';

// O1: already delivered + paid -> backfilled by the migration.
// O2: delivered but unpaid -> no payout.  O3: no farmer -> no payout.
// O6: generated total_amount is 0 but a legacy total_price exists; the payout
// must use the same fallback as finance.order_total.
await db.exec(`
  insert into public.orders (id, farmer_id, status, payment_status, total_amount, total_price) values
    ('${O1}', '${FARMER}', 'delivered', 'paid',   100000, 100000),
    ('${O2}', '${FARMER}', 'delivered', 'unpaid',  50000,  50000),
    ('${O3}', null,        'delivered', 'paid',    30000,  30000),
    ('${O6}', '${FARMER}', 'delivered', 'paid',        0,   7000);
`);

await db.exec(up); await db.exec(up);
ok('migration applies and re-applies idempotently');

let r = await db.query('select * from public.payouts order by order_id');
assert.equal(r.rows.length, 2);
const p1 = r.rows.find((row) => row.order_id === O1);
assert.equal(Number(p1.gross_amount), 100000);
assert.equal(Number(p1.commission_pct), 5);
assert.equal(Number(p1.commission_amount), 5000);
assert.equal(Number(p1.net_amount), 95000);
assert.equal(p1.status, 'pending');
const p6 = r.rows.find((row) => row.order_id === O6);
assert.equal(Number(p6.gross_amount), 7000);
assert.equal(Number(p6.commission_amount), 350);
assert.equal(Number(p6.net_amount), 6650);
ok('existing delivered+paid orders are backfilled once with a commission snapshot');

const audit = await db.query(`select relrowsecurity from pg_class where oid='public.admin_audit_log'::regclass`);
assert.equal(audit.rows[0].relrowsecurity, true);
ok('ledger audit dependency exists and has RLS enabled');

// ── Trigger lifecycle ──────────────────────────────────────────────────────
await db.exec(`
  insert into public.orders (id, farmer_id, status, payment_status, total_amount, total_price)
  values ('${O4}', '${FARMER}', 'pending', 'unpaid', 20000, 20000);
`);
r = await db.query('select count(*)::int as n from public.payouts where order_id=$1', [O4]);
assert.equal(r.rows[0].n, 0, 'pending order has no payout');

await db.exec(`update public.orders set status='delivered' where id='${O4}';`);
r = await db.query('select count(*)::int as n from public.payouts where order_id=$1', [O4]);
assert.equal(r.rows[0].n, 0, 'delivered but unpaid has no payout');

await db.exec(`update public.orders set payment_status='paid' where id='${O4}';`);
r = await db.query('select * from public.payouts where order_id=$1', [O4]);
assert.equal(r.rows.length, 1);
assert.equal(Number(r.rows[0].commission_amount), 1000);
assert.equal(Number(r.rows[0].net_amount), 19000);
ok('delivery and payment together create one pending payout');

await db.exec(`update public.orders set status='delivered' where id='${O4}';`);
r = await db.query('select count(*)::int as n from public.payouts where order_id=$1', [O4]);
assert.equal(r.rows[0].n, 1);
ok('re-updating the order never duplicates the payout');

await db.exec(`
  update public.platform_config set commission_pct=10 where id=1;
  insert into public.orders (id, farmer_id, status, payment_status, total_amount, total_price)
  values ('${O5}', '${FARMER}', 'delivered', 'paid', 40000, 40000);
`);
r = await db.query('select * from public.payouts where order_id=$1', [O5]);
assert.equal(Number(r.rows[0].commission_pct), 10);
assert.equal(Number(r.rows[0].commission_amount), 4000);
const old = await db.query('select * from public.payouts where order_id=$1', [O4]);
assert.equal(Number(old.rows[0].commission_pct), 5);
ok('commission is snapshotted per payout, not recomputed later');

// ── mark_payout_paid is guarded by a posted settlement journal ─────────────
const payout = await db.query('select id from public.payouts where order_id=$1', [O1]);
const payoutId = payout.rows[0].id;

r = await as('service_role',
  'select public.mark_payout_paid($1, $2, $3) as b', [payoutId, 'MTN MoMo', 'REF-0']);
assert.equal(r.rows[0].b, false, 'no journal -> not paid');

await db.exec(`
  insert into public.ledger_journals (reference, kind, status)
  values ('settlement:${payoutId}', 'farmer_settlement', 'posted');
`);
r = await as('service_role',
  'select public.mark_payout_paid($1, $2, $3) as b', [payoutId, 'MTN MoMo', 'MP-1']);
assert.equal(r.rows[0].b, true);
r = await db.query('select * from public.payouts where id=$1', [payoutId]);
assert.equal(r.rows[0].status, 'paid');
assert.equal(r.rows[0].provider, 'MTN MoMo');
assert.equal(r.rows[0].provider_ref, 'MP-1');
assert.ok(r.rows[0].paid_at);
ok('mark_payout_paid requires a posted journal and records the evidence');

r = await as('service_role',
  'select public.mark_payout_paid($1, $2, $3) as b', [payoutId, 'MTN MoMo', 'MP-1']);
assert.equal(r.rows[0].b, false, 'already paid -> false, no double settlement');
ok('a second settlement attempt is refused');

let denied = false;
try {
  await as('anon', 'select public.mark_payout_paid($1,$2,$3)', [payoutId, 'x', 'y']);
} catch (e) {
  denied = /permission denied/i.test(e.message);
}
assert.ok(denied, 'anon cannot mark payouts paid');
ok('only the backend can mark a payout paid');

// ── Down path keeps financial records ──────────────────────────────────────
await db.exec(down);
r = await db.query(`select count(*)::int as n from pg_trigger where tgname='orders_create_payout'`);
assert.equal(r.rows[0].n, 0);
r = await db.query(`select count(*)::int as n from pg_proc where proname in ('mark_payout_paid','create_payout_for_order')`);
assert.equal(r.rows[0].n, 0);
r = await db.query('select count(*)::int as n from public.payouts');
assert.equal(r.rows[0].n, 4, 'paid and pending payouts are retained');
r = await db.query(`select count(*)::int as n from pg_class where oid='public.admin_audit_log'::regclass`);
assert.equal(r.rows[0].n, 1, 'audit log is retained');
ok('down removes settlement plumbing but keeps payouts and audit history');

await db.exec(up);
r = await db.query('select count(*)::int as n from public.payouts');
assert.equal(r.rows[0].n, 4, 're-applying does not duplicate retained payouts');
ok('migration can be re-applied after a down');

console.log(`\n${passed} assertions passed (farmer settlements)`);
