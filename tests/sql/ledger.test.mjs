/**
 * Copyright (C) 2026 Mugumya Zeal. All rights reserved.
 */

// Verifies migration 0013 (double-entry ledger core) against a real PostgreSQL engine (PGlite).
// Supabase pieces (roles, auth.uid, is_admin, admin_audit_log) are stubbed minimally.
// Run:  cd tests/sql && npm install && npm test
import { PGlite } from '@electric-sql/pglite';
import { readFileSync } from 'node:fs';
import assert from 'node:assert/strict';

const dir = new URL('../../supabase/migrations/', import.meta.url);
const up = readFileSync(new URL('0013_ledger_core.up.sql', dir), 'utf8');
const down = readFileSync(new URL('0013_ledger_core.down.sql', dir), 'utf8');

const db = new PGlite();
await db.exec(`
  create role anon nologin; create role authenticated nologin; create role service_role nologin;
  create schema auth;
  create table auth.users (id uuid primary key);
  create function auth.uid() returns uuid language sql stable as
    $$ select nullif(current_setting('request.jwt.claim.sub', true), '')::uuid $$;
  grant usage on schema public, auth to anon, authenticated, service_role;
  grant execute on function auth.uid() to anon, authenticated, service_role;
  create table public.user_roles (user_id uuid primary key, role text not null);
  create function public.is_admin(p_uid uuid) returns boolean language sql stable security definer as
    $$ select exists (select 1 from public.user_roles where user_id = p_uid and role in ('admin','superadmin')) $$;
  grant execute on function public.is_admin(uuid) to anon, authenticated, service_role;
  create table public.admin_audit_log (
    id bigint generated always as identity primary key, actor uuid, action text not null,
    target text, metadata jsonb, ip text, created_at timestamptz default now());
`);

let passed = 0;
const ok = (name) => { passed++; console.log('  ok -', name); };
const as = async (role, uid, sql, params) => {
  await db.exec(`set role ${role}; select set_config('request.jwt.claim.sub','${uid ?? ''}',false);`);
  try { return await db.query(sql, params); } finally { await db.exec('reset role;'); }
};
const rejects = async (fn, re, name) => { await assert.rejects(fn, re); ok(name); };

const ADMIN = '11111111-1111-1111-1111-111111111111';
const USER = '22222222-2222-2222-2222-222222222222';
await db.exec(`insert into public.user_roles values ('${ADMIN}','admin'), ('${USER}','user')`);

await db.exec(up); await db.exec(up); ok('migration applies, and re-applies idempotently');
let r = await db.query(`select count(*)::int c from public.ledger_accounts`);
assert.equal(r.rows[0].c, 9); ok('chart of accounts seeded once (9 accounts)');

// UGX 100,000 produce + UGX 10,000 delivery paid; 10% commission; transporter gets 7,000.
const payment = JSON.stringify([
  { account: 'PROVIDER_CLEARING',   side: 'debit',  amount: 110000 },
  { account: 'FARMER_PAYABLE',      side: 'credit', amount: 90000 },
  { account: 'COMMISSION_REVENUE',  side: 'credit', amount: 10000 },
  { account: 'TRANSPORTER_PAYABLE', side: 'credit', amount: 7000 },
  { account: 'DELIVERY_REVENUE',    side: 'credit', amount: 3000 },
]);
const post = (ref, lines = payment, actor = null) => as('service_role', null,
  `select public.post_journal($1,'customer_payment','test',$2::jsonb,'PAY-1',$3) as id`, [ref, lines, actor]);

r = await post('payment:flw-1');
const J1 = r.rows[0].id; assert.ok(J1); ok('balanced journal posts');
r = await post('payment:flw-1');
assert.equal(r.rows[0].id, J1);
r = await db.query(`select (select count(*)::int from public.ledger_journals) j, (select count(*)::int from public.ledger_lines) l`);
assert.deepEqual(r.rows[0], { j: 1, l: 5 }); ok('replaying a reference posts nothing (idempotent)');

const bad = (lines, re, name) => rejects(() => post('bad:' + name, JSON.stringify(lines)), re, name);
await bad([{ account: 'PROVIDER_CLEARING', side: 'debit', amount: 100 },
           { account: 'FARMER_PAYABLE', side: 'credit', amount: 90 }], /not balanced/, 'unbalanced journal rejected');
await bad([{ account: 'PROVIDER_CLEARING', side: 'debit', amount: 100 }], /at least two lines/, 'single-line journal rejected');
await bad([{ account: 'PROVIDER_CLEARING', side: 'debit', amount: -5 },
           { account: 'FARMER_PAYABLE', side: 'credit', amount: -5 }], /must be positive/, 'negative amount rejected');
await bad([{ account: 'NOPE', side: 'debit', amount: 5 },
           { account: 'FARMER_PAYABLE', side: 'credit', amount: 5 }], /unknown or inactive account/, 'unknown account rejected');
await bad([{ account: 'PROVIDER_CLEARING', side: 'left', amount: 5 },
           { account: 'FARMER_PAYABLE', side: 'credit', amount: 5 }], /side must be/, 'bad side rejected');
await bad([{ account: 'PROVIDER_CLEARING', side: 'debit', amount: 1.234 },
           { account: 'FARMER_PAYABLE', side: 'credit', amount: 1.234 }], /decimal places/, 'sub-cent amounts rejected');
await rejects(() => post('', payment), /reference is required/, 'empty reference rejected');

// Raw inserts that bypass post_journal still cannot commit an unbalanced journal.
await rejects(() => db.transaction(async (tx) => {
  await tx.query(`insert into public.ledger_journals (id, reference, kind) values ('99999999-9999-9999-9999-999999999999','raw:1','x')`);
  await tx.query(`insert into public.ledger_lines (journal_id, account_code, side, amount) values ('99999999-9999-9999-9999-999999999999','FARMER_PAYABLE','credit',50)`);
}), /not balanced/, 'raw unbalanced insert fails at commit');

// Append-only, even for the table owner / superuser.
await rejects(() => db.query(`update public.ledger_lines set amount = 1`), /append-only/, 'lines cannot be updated');
await rejects(() => db.query(`delete from public.ledger_lines`), /append-only/, 'lines cannot be deleted');
await rejects(() => db.query(`truncate public.ledger_lines`), /append-only/, 'lines cannot be truncated');
await rejects(() => db.query(`update public.ledger_journals set description = 'edited'`), /append-only/, 'journals cannot be edited');
await rejects(() => db.query(`delete from public.ledger_journals`), /append-only/, 'journals cannot be deleted');
await rejects(() => db.query(`update public.ledger_journals set status = 'reversed'`), /append-only/,
  'journal cannot be marked reversed without a reversing journal');

// Reversal.
const rev = (id, ref, actor, reason) => as('service_role', null,
  `select public.reverse_journal($1,$2,$3,$4) as id`, [id, ref, actor, reason]);
await rejects(() => rev(J1, 'rev:1', ADMIN, ' '), /reason is required/, 'reversal needs a reason');
await rejects(() => rev(J1, 'rev:1', null, 'dup'), /actor is required/, 'reversal needs an actor');
r = await rev(J1, 'rev:1', ADMIN, 'duplicate payment'); const R1 = r.rows[0].id; assert.ok(R1);
r = await db.query(`select status from public.ledger_journals where id = $1`, [J1]);
assert.equal(r.rows[0].status, 'reversed'); ok('reversal marks original reversed');
r = await db.query(`select count(*)::int c from public.ledger_account_balances where balance <> 0`);
assert.equal(r.rows[0].c, 0); ok('original + reversal net every account to zero');
r = await rev(J1, 'rev:1', ADMIN, 'duplicate payment');
assert.equal(r.rows[0].id, R1); ok('replaying a reversal reference is idempotent');
await rejects(() => rev(J1, 'rev:2', ADMIN, 'again'), /already reversed/, 'cannot reverse twice');
await rejects(() => rev(R1, 'rev:3', ADMIN, 'undo undo'), /cannot itself be reversed/, 'cannot reverse a reversal');
r = await db.query(`select action, actor from public.admin_audit_log where action = 'ledger.reversed'`);
assert.equal(r.rows.length, 1); assert.equal(r.rows[0].actor, ADMIN); ok('reversal is written to admin_audit_log');
await post('payment:flw-2', payment, ADMIN);
r = await db.query(`select count(*)::int c from public.admin_audit_log where action = 'ledger.posted'`);
assert.equal(r.rows[0].c, 1); ok('posting with an actor is audited');

// The backend serialises amounts as 2-decimal strings and posts as the system (null actor).
const appShape = JSON.stringify([
  { account: 'PROVIDER_CLEARING', side: 'debit', amount: '50000.00', memo: 'customer payment received' },
  { account: 'FARMER_PAYABLE', side: 'credit', amount: '50000.00', memo: 'product value owed to farmers' },
]);
r = await post('payment:app-shape', appShape);
const JAPP = r.rows[0].id; assert.ok(JAPP);
r = await db.query(`select amount::text a from public.ledger_lines where journal_id = $1`, [JAPP]);
assert.deepEqual(r.rows.map((x) => x.a), ['50000.00', '50000.00']);
ok('backend string amounts post exactly');

// Access control.
r = await as('authenticated', USER, `select count(*)::int c from public.ledger_journals`);
assert.equal(r.rows[0].c, 0); ok('non-admin sees no journals');
r = await as('authenticated', ADMIN, `select count(*)::int c from public.ledger_journals`);
assert.ok(r.rows[0].c >= 2); ok('admin can read journals');
r = await as('authenticated', ADMIN, `select count(*)::int c from public.ledger_account_balances`);
assert.equal(r.rows[0].c, 9); ok('admin can read balances view');
await rejects(() => as('authenticated', ADMIN, `select public.post_journal('x','y','z','[]'::jsonb)`),
  /permission denied/, 'even an admin session cannot call post_journal directly');
await rejects(() => as('authenticated', ADMIN, `insert into public.ledger_lines (journal_id, account_code, side, amount) values (gen_random_uuid(),'FARMER_PAYABLE','credit',1)`),
  /permission denied/, 'authenticated cannot write lines');
await rejects(() => as('anon', null, `select * from public.ledger_journals`), /permission denied/, 'anon cannot read the ledger');

// Rollback.
await db.exec(down);
r = await db.query(`select count(*)::int c from information_schema.tables where table_schema='public' and table_name like 'ledger%'`);
assert.equal(r.rows[0].c, 0);
r = await db.query(`select count(*)::int c from pg_proc where proname in ('post_journal','reverse_journal','ledger_check_balanced','ledger_journal_guard','ledger_block_mutation')`);
assert.equal(r.rows[0].c, 0); ok('down migration removes all 0013 objects');
await db.exec(up); ok('up works again after rollback');

console.log(`\n${passed} checks passed`);
