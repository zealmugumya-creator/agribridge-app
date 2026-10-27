/**
 * Copyright (C) 2026 Mugumya Zeal. All rights reserved.
 */

// Verifies migration 0018 against a real PostgreSQL engine (PGlite).
// The shipped SPA reads public.farmer_ratings, which does not exist in the live
// project (404), so the top-rated-farmer strip hides itself. 0018 adds the
// relation as an aggregate over the reviews the app already stores — it must
// return exactly the columns the SPA selects, invent nothing, and stay filtered
// by reviews' own RLS.
// Run:  cd tests/sql && npm install && npm test
import { PGlite } from '@electric-sql/pglite';
import { readFileSync } from 'node:fs';
import assert from 'node:assert/strict';

const dir = new URL('../../supabase/migrations/', import.meta.url);
const up = readFileSync(new URL('0018_farmer_ratings_view.up.sql', dir), 'utf8');
const down = readFileSync(new URL('0018_farmer_ratings_view.down.sql', dir), 'utf8');

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

  -- reviews copied from supabase/base-schema.sql:98
  create table public.reviews (
    id uuid primary key default gen_random_uuid(),
    order_id uuid, listing_id uuid,
    farmer_id uuid not null, buyer_id uuid not null,
    rating integer not null check (rating between 1 and 5),
    comment text,
    created_at timestamptz default now()
  );
  alter table public.reviews enable row level security;
  grant all on public.reviews to anon, authenticated, service_role;
  -- base-schema.sql:271 — reviews are public, so a guest can see the aggregate.
  create policy reviews_public_select on public.reviews for select to public using (true);
`);

let passed = 0;
const ok = (name) => { passed++; console.log('  ok -', name); };
const as = async (role, uid, sql) => {
  await db.exec(`set role ${role}; select set_config('request.jwt.claim.sub','${uid ?? ''}',false);`);
  try { return await db.query(sql); } finally { await db.exec('reset role;'); }
};
const SPA_READ =
  'select farmer_id, avg_rating, review_count from public.farmer_ratings';

const FARMER_A = '22222222-2222-2222-2222-222222222222';
const FARMER_B = '33333333-3333-3333-3333-333333333333';
const BUYER_1 = '44444444-4444-4444-4444-444444444444';
const BUYER_2 = '55555555-5555-5555-5555-555555555555';

// ── Reproduce the live failure first ────────────────────────────────────────
await assert.rejects(() => as('anon', null, SPA_READ), /does not exist|undefined_table/);
ok('before 0018 the shipped read fails, which is the production 404');

await db.exec(`
  insert into public.reviews (farmer_id, buyer_id, rating) values
    ('${FARMER_A}','${BUYER_1}',5),
    ('${FARMER_A}','${BUYER_2}',4),
    ('${FARMER_B}','${BUYER_1}',3);
`);

// ── Apply the fix ───────────────────────────────────────────────────────────
await db.exec(up);
await db.exec(up);
ok('0018 is safe to re-run');

const rows = (await as('anon', null, SPA_READ + ' order by avg_rating desc')).rows;
assert.equal(rows.length, 2);
assert.equal(String(rows[0].farmer_id).toLowerCase(), FARMER_A);
assert.equal(Number(rows[0].avg_rating), 4.5);
assert.equal(Number(rows[0].review_count), 2);
assert.equal(Number(rows[1].avg_rating), 3);
ok('the exact column list the SPA selects now resolves with real numbers');

await assert.rejects(() => as('authenticated', BUYER_1,
  `insert into public.farmer_ratings (farmer_id, avg_rating, review_count)
   values ('${FARMER_A}', 5, 1)`), /cannot insert into view/);
ok('the relation cannot be written to, so no rating can be forged through it');

// ── security_invoker: the view reports the rows its reader may see ──────────
await db.exec(`
  drop policy reviews_public_select on public.reviews;
  create policy reviews_buyer_select on public.reviews
    for select to authenticated using (buyer_id = auth.uid());
`);
assert.equal((await as('anon', null, SPA_READ)).rows.length, 0);
ok('anon gets nothing once reviews stop being public — the view is no bypass');
const mine = (await as('authenticated', BUYER_1, SPA_READ)).rows;
assert.equal(mine.length, 2);
assert.equal(mine.every(f => Number(f.review_count) === 1), true);
const aForBuyer1 = mine.find(f => String(f.farmer_id).toLowerCase() === FARMER_A);
assert.equal(Number(aForBuyer1.avg_rating), 5);
ok('a buyer aggregates only the reviews they may read');

await db.exec(`
  drop policy reviews_buyer_select on public.reviews;
  create policy reviews_public_select on public.reviews for select to public using (true);
`);

// ── The aggregate tracks the rows, with nothing cached ──────────────────────
await db.exec(`delete from public.reviews where farmer_id='${FARMER_A}' and rating=4`);
const after = (await as('anon', null,
  SPA_READ + ` where farmer_id='${FARMER_A}'`)).rows;
assert.equal(Number(after[0].avg_rating), 5);
assert.equal(Number(after[0].review_count), 1);
ok('removing a review moves the average, so no stale rating can linger');

await db.exec(`delete from public.reviews where farmer_id='${FARMER_A}'`);
const gone = (await as('anon', null,
  SPA_READ + ` where farmer_id='${FARMER_A}'`)).rows;
assert.equal(gone.length, 0);
ok('a farmer with no reviews left is absent, not shown as zero stars');

// ── Down restores the pre-migration state ──────────────────────────────────
await db.exec(down);
await assert.rejects(() => as('anon', null, SPA_READ), /does not exist|undefined_table/);
const kept = await as('service_role', null, 'select count(*)::int c from public.reviews');
assert.equal(kept.rows[0].c, 1);
ok('the down path removes only the view; every review row survives');

console.log(`\n${passed} farmer-ratings assertions passed`);
