/**
 * Copyright (C) 2026 Mugumya Zeal. All rights reserved.
 */

// Verifies migration 0019 against a real PostgreSQL engine (PGlite).
// orders_farmer_update is row-scoped, not column-scoped, so a farmer could PATCH
// any writable column of their own orders — and 0016 attaches a trigger that
// turns a delivered+paid row into a pending payout. This file reproduces that
// hole with the production policies and a stand-in trigger, closes it, and proves
// the read path and the service-role API path both still work.
// Run:  cd tests/sql && npm install && npm test
import { PGlite } from '@electric-sql/pglite';
import { readFileSync } from 'node:fs';
import assert from 'node:assert/strict';

const dir = new URL('../../supabase/migrations/', import.meta.url);
const up = readFileSync(new URL('0019_lock_farmer_order_writes.up.sql', dir), 'utf8');
const down = readFileSync(new URL('0019_lock_farmer_order_writes.down.sql', dir), 'utf8');

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

  create table public.orders (
    id uuid primary key default gen_random_uuid(),
    buyer_id uuid, farmer_id uuid not null,
    status text default 'pending',
    payment_status text default 'unpaid',
    payment_ref text, notes text,
    quantity_kg numeric not null default 1,
    price_per_kg numeric not null default 1000,
    total_amount numeric generated always as (quantity_kg * price_per_kg) stored
  );

  create table public.payouts (
    id uuid primary key default gen_random_uuid(),
    order_id uuid unique references public.orders(id),
    farmer_id uuid, gross_amount numeric,
    status text not null default 'pending'
  );

  -- Stand-in for 0016's create_payout_for_order(): same trigger event, same
  -- delivered+paid guard, and the same "security definer" context — which is what
  -- lets the payout insert bypass payouts' own RLS, so this measures the write
  -- path rather than the math.
  create function public.create_payout_for_order() returns trigger
  language plpgsql security definer set search_path = public, pg_temp as $$ begin
    if new.farmer_id is null
       or coalesce(new.status, '') <> 'delivered'
       or coalesce(new.payment_status, '') <> 'paid' then
      return new;
    end if;
    insert into public.payouts (order_id, farmer_id, gross_amount)
      values (new.id, new.farmer_id, new.total_amount)
      on conflict (order_id) do nothing;
    return new;
  end $$;
  create trigger orders_create_payout
    after insert or update of status, payment_status on public.orders
    for each row execute function public.create_payout_for_order();

  alter table public.orders enable row level security;
  alter table public.payouts enable row level security;
  -- Supabase grants ALL on public tables to these roles by default.
  grant all on public.orders to anon, authenticated, service_role;
  grant all on public.payouts to anon, authenticated, service_role;

  -- Both policies copied verbatim from supabase/base-schema.sql:264-265.
  create policy orders_owner_select on public.orders
    for select to authenticated using (auth.uid() = buyer_id or auth.uid() = farmer_id);
  create policy orders_farmer_update on public.orders
    for update to authenticated using (auth.uid() = farmer_id)
    with check (auth.uid() = farmer_id);
`);

let passed = 0;
const ok = (name) => { passed++; console.log('  ok -', name); };
const as = async (role, uid, sql) => {
  await db.exec(`set role ${role}; select set_config('request.jwt.claim.sub','${uid ?? ''}',false);`);
  try { return await db.query(sql); } finally { await db.exec('reset role;'); }
};

const FARMER = '22222222-2222-2222-2222-222222222222';
const OTHER_FARMER = '33333333-3333-3333-3333-333333333333';
const BUYER = '44444444-4444-4444-4444-444444444444';

const mkOrder = async (farmer) => {
  const r = await as('service_role', null,
    `insert into public.orders (buyer_id, farmer_id, quantity_kg, price_per_kg)
     values ('${BUYER}','${farmer}',10,5000) returning id`);
  return r.rows[0].id;
};

// ── Reproduce the pre-migration hole, exactly as production is configured ───
const hole = await mkOrder(FARMER);
let r = await as('authenticated', FARMER,
  `update public.orders set status='delivered', payment_status='paid'
     where id='${hole}' returning id`);
assert.equal(r.rows.length, 1);
r = await as('service_role', null, 'select count(*)::int c from public.payouts');
assert.equal(r.rows[0].c, 1);
ok('before 0019 a farmer can mint a pending payout with one PATCH');

r = await as('authenticated', FARMER,
  `update public.orders set payment_ref='forged-ref', notes='rewritten'
     where id='${hole}' returning payment_ref, notes`);
assert.equal(r.rows[0].payment_ref, 'forged-ref');
ok('the same policy lets any column be rewritten, not just status');

// ── Apply the fix ───────────────────────────────────────────────────────────
await db.exec(up);
await db.exec(up);
ok('0019 is safe to re-run');

const stillPaid = await as('service_role', null,
  `select status, payment_status from public.orders where id='${hole}'`);
assert.equal(stillPaid.rows[0].status, 'delivered');
ok('0019 rewrites nothing: rows already in place are left alone');

r = await as('authenticated', FARMER,
  `update public.orders set notes='try again' where id='${hole}' returning id`);
assert.equal(r.rows.length, 0);
r = await as('service_role', null,
  `select notes from public.orders where id='${hole}'`);
assert.equal(r.rows[0].notes, 'rewritten');
ok('after 0019 a farmer cannot write any column of their own order');

const fresh = await mkOrder(FARMER);
r = await as('authenticated', FARMER,
  `update public.orders set status='delivered', payment_status='paid'
     where id='${fresh}' returning id`);
assert.equal(r.rows.length, 0);
r = await as('service_role', null, 'select count(*)::int c from public.payouts');
assert.equal(r.rows[0].c, 1);
ok('the payout-minting PATCH is now refused, so no new obligation appears');

// ── Reads and the legitimate API path must survive ──────────────────────────
const mine = await mkOrder(FARMER);
r = await as('authenticated', FARMER, `select id from public.orders where id='${mine}'`);
assert.equal(r.rows.length, 1);
ok('the farmer still sees their own orders');

r = await as('authenticated', OTHER_FARMER, `select id from public.orders where id='${mine}'`);
assert.equal(r.rows.length, 0);
ok('another farmer still sees nothing');

r = await as('anon', null, `select id from public.orders where id='${mine}'`);
assert.equal(r.rows.length, 0);
ok('anon still cannot read orders');

r = await as('service_role', null,
  `update public.orders set status='delivered', payment_status='paid'
     where id='${mine}' returning id`);
assert.equal(r.rows.length, 1);
r = await as('service_role', null,
  `select count(*)::int c from public.payouts where order_id='${mine}'`);
assert.equal(r.rows[0].c, 1);
ok('the service-role status API still completes an order and pays it out');

// ── Down restores the declared policy, so the rollback is honest ────────────
await db.exec(down);
r = await as('authenticated', FARMER,
  `update public.orders set notes='rolled back' where id='${mine}' returning id`);
assert.equal(r.rows.length, 1);
ok('the down migration restores orders_farmer_update as base-schema declares it');

await db.exec(up);
r = await as('authenticated', FARMER,
  `update public.orders set notes='closed again' where id='${mine}' returning id`);
assert.equal(r.rows.length, 0);
ok('re-applying 0019 after a rollback closes it again');

console.log(`\n${passed} order-write lockdown assertions passed`);
