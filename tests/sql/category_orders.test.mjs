/**
 * Copyright (C) 2026 Mugumya Zeal. All rights reserved.
 */

// Verifies migration 0015 (category-aware create_order_atomic) against PGlite.
// The animal/supply catalogue tables are stubbed minimally; auth.uid() returns
// null so the function's self-order check is skipped for the service role.
// Run:  cd tests/sql && npm test
import { PGlite } from '@electric-sql/pglite';
import { readFileSync } from 'node:fs';
import assert from 'node:assert/strict';

const dir = new URL('../../supabase/migrations/', import.meta.url);
const up = readFileSync(new URL('0015_category_order_pricing.up.sql', dir), 'utf8');
const down = readFileSync(new URL('0015_category_order_pricing.down.sql', dir), 'utf8');

const db = new PGlite();
await db.exec(`
  create role anon nologin;
  create role authenticated nologin;
  create role service_role nologin;
  create schema auth;
  create function auth.uid() returns uuid language sql stable as
    $$ select nullif(current_setting('request.jwt.claim.sub', true), '')::uuid $$;
  grant usage on schema public, auth to anon, authenticated, service_role;
  grant execute on function auth.uid() to anon, authenticated, service_role;

  create table public.farmers (id uuid primary key, phone text);
  create table public.buyers  (id uuid primary key);
  create table public.listings (
    id uuid primary key default gen_random_uuid(),
    farmer_id uuid references public.farmers(id),
    crop_name text, category text default 'staples',
    quantity_kg numeric not null, reserved_qty numeric not null default 0,
    price_per_kg numeric not null, is_available boolean default true,
    farmer_phone text, stock_status text default 'available',
    stock_updated_at timestamptz default now()
  );
  create table public.animal_listings (
    id uuid primary key default gen_random_uuid(),
    farmer_id uuid, farmer_name text, farmer_phone text, name text,
    category text, price numeric, unit text, qty integer,
    status text default 'active'
  );
  create table public.supplier_products (
    id uuid primary key default gen_random_uuid(),
    category text not null, name text not null, price numeric not null default 0,
    unit text, in_stock boolean not null default true, stock_qty numeric,
    supplier_id uuid
  );
  create table public.orders (
    id uuid primary key default gen_random_uuid(),
    listing_id uuid references public.listings(id),
    buyer_id uuid references public.buyers(id),
    farmer_id uuid references public.farmers(id),
    quantity_kg numeric not null, price_per_kg numeric not null,
    total_amount numeric generated always as (quantity_kg * price_per_kg) stored,
    status text default 'pending', delivery_address text, payment_method text,
    payment_status text default 'unpaid', payment_ref text, notes text,
    created_at timestamptz default now(), updated_at timestamptz default now(),
    buyer_phone text, total_price numeric, item_category text, tracking_code text,
    idempotency_key uuid, created_by uuid
  );
  create table public.order_events (
    id uuid primary key default gen_random_uuid(), idempotency_key uuid,
    order_ref text, event_type text not null, actor uuid,
    payload jsonb not null default '{}'::jsonb, created_at timestamptz default now()
  );
  create table public.stock_changes (
    id uuid primary key default gen_random_uuid(),
    listing_id uuid not null references public.listings(id),
    changed_by uuid, previous_qty numeric not null, new_qty numeric not null,
    delta numeric not null, reason text not null, order_ref text,
    created_at timestamptz default now()
  );
`);

let passed = 0;
const ok = (name) => { passed++; console.log('  ok -', name); };
const as = async (role, uid, sql, params) => {
  await db.exec(`set role ${role}; select set_config('request.jwt.claim.sub','${uid ?? ''}',false);`);
  try { return await db.query(sql, params); } finally { await db.exec('reset role;'); }
};

const BUYER = '10000000-0000-0000-0000-000000000001';
const FARMER = '20000000-0000-0000-0000-000000000001';
const SUPPLIER = '20000000-0000-0000-0000-000000000002';
const ANIMAL = '30000000-0000-0000-0000-000000000001';
const SUPPLY = '40000000-0000-0000-0000-000000000001';
const LISTING = '50000000-0000-0000-0000-000000000001';

await db.exec(`
  insert into public.farmers values
    ('${FARMER}','0770000001'), ('${SUPPLIER}','0770000002');
  insert into public.buyers values ('${BUYER}');
  insert into public.animal_listings
    (id, farmer_id, farmer_name, farmer_phone, name, category, price, unit, qty)
  values ('${ANIMAL}','${FARMER}','Goat Farmer','0770000001','Boer Goat','livestock',650000,'head',6);
  insert into public.supplier_products
    (id, category, name, price, unit, in_stock, stock_qty, supplier_id)
  values ('${SUPPLY}','seeds','Hybrid Maize Seed',12000,'kg',true,10,'${SUPPLIER}');
  insert into public.listings
    (id, farmer_id, crop_name, category, quantity_kg, price_per_kg, farmer_phone)
  values ('${LISTING}','${FARMER}','Maize','staples',10,2000,'0770000001');
`);

const create = (items) => as('service_role', null,
  `select public.create_order_atomic('${BUYER}', $1::jsonb, 'Kampala', 'cash', null) as r`,
  [JSON.stringify(items)]);
const order = (ref) => db.query('select * from public.orders where payment_ref=$1', [ref]);

await db.exec(up); await db.exec(up);
ok('migration applies and re-applies idempotently');

// ── Livestock ──────────────────────────────────────────────────────────────
let r = await create([{ listing_id: ANIMAL, item_category: 'animal',
                        quantity: 2, price_per_unit: 1 }]);
let animalRef = r.rows[0].r.order_ref;
assert.equal(r.rows[0].r.total_price, 1300000);
assert.equal(r.rows[0].r.items[0].product_name, 'Boer Goat');
assert.equal(r.rows[0].r.items[0].farmer_phone, '0770000001');
ok('animal price is taken from animal_listings, not the client');

let rows = await order(animalRef);
assert.equal(rows.rows.length, 1);
assert.equal(Number(rows.rows[0].price_per_kg), 650000);
assert.equal(Number(rows.rows[0].total_price), 1300000);
assert.equal(rows.rows[0].farmer_id, FARMER);
assert.equal(rows.rows[0].listing_id, null);
assert.equal(rows.rows[0].item_category, 'animal');
assert.equal(rows.rows[0].notes, 'Item: Boer Goat');
let qty = await db.query('select qty from public.animal_listings where id=$1', [ANIMAL]);
assert.equal(qty.rows[0].qty, 4);
ok('animal order records the farmer and decrements animal stock');

await as('service_role', null, 'select public.set_order_status($1,$2)',
  [rows.rows[0].id, 'cancelled']);
qty = await db.query('select qty from public.animal_listings where id=$1', [ANIMAL]);
assert.equal(qty.rows[0].qty, 6);
ok('cancelling an animal order restores animal stock');

// ── Supplier input ─────────────────────────────────────────────────────────
r = await create([{ listing_id: SUPPLY, item_category: 'supply',
                    quantity: 3, price_per_unit: 1 }]);
const supplyRef = r.rows[0].r.order_ref;
assert.equal(r.rows[0].r.total_price, 36000);
rows = await order(supplyRef);
assert.equal(Number(rows.rows[0].price_per_kg), 12000);
assert.equal(rows.rows[0].farmer_id, SUPPLIER);
assert.equal(rows.rows[0].item_category, 'supply');
const stock = await db.query('select stock_qty from public.supplier_products where id=$1', [SUPPLY]);
assert.equal(Number(stock.rows[0].stock_qty), 7);
ok('supply price and supplier come from supplier_products, stock decremented');

await as('service_role', null, 'select public.set_order_status($1,$2)',
  [rows.rows[0].id, 'cancelled']);
const restored = await db.query('select stock_qty from public.supplier_products where id=$1', [SUPPLY]);
assert.equal(Number(restored.rows[0].stock_qty), 10);
ok('cancelling a supply order restores supplier stock');

// ── Produce catalogue stays unchanged ──────────────────────────────────────
r = await create([{ listing_id: LISTING, item_category: 'produce',
                    quantity: 3, price_per_unit: 1 }]);
const produceRef = r.rows[0].r.order_ref;
assert.equal(r.rows[0].r.total_price, 6000);
rows = await order(produceRef);
assert.equal(rows.rows[0].listing_id, LISTING);
assert.equal(Number(rows.rows[0].price_per_kg), 2000);
const listing = await db.query('select reserved_qty from public.listings where id=$1', [LISTING]);
assert.equal(Number(listing.rows[0].reserved_qty), 3);
const audit = await db.query('select count(*)::int c from public.stock_changes where order_ref=$1', [produceRef]);
assert.equal(audit.rows[0].c, 1);
ok('produce still reserves stock at the catalogue price');

// ── Guardrails ─────────────────────────────────────────────────────────────
await assert.rejects(
  () => create([{ listing_id: ANIMAL, item_category: 'animal',
                  quantity: 99, price_per_unit: 1 }]),
  /insufficient stock for animal/);
await assert.rejects(
  () => create([{ listing_id: SUPPLY, item_category: 'supply',
                  quantity: 99, price_per_unit: 1 }]),
  /insufficient stock for supply/);
ok('animal and supply oversell attempts are rejected');

// ── Free-form (equipment/transport) still works ────────────────────────────
r = await create([{ listing_id: null, item_category: 'equipment',
                    product: 'Water Pump', quantity: 1, price_per_unit: 85000 }]);
rows = await order(r.rows[0].r.order_ref);
assert.equal(rows.rows[0].farmer_id, null);
assert.equal(Number(rows.rows[0].total_price), 85000);
assert.equal(rows.rows[0].item_category, 'equipment');
ok('free-form items still use their supplied price with no catalogue owner');

// ── Down migration restores the 0007a behaviour ────────────────────────────
await db.exec(down);
await assert.rejects(
  () => create([{ listing_id: ANIMAL, item_category: 'animal',
                  quantity: 1, price_per_unit: 1 }]),
  /listing not found/);
ok('down migration restores 0007a (animal UUID no longer resolves)');

console.log(`\n${passed} category-order assertions passed`);
