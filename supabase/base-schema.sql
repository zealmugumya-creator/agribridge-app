-- AgriBridge base schema (tables that predate migrations 0001-0007).
-- Recreated from the live column definitions of the old shared project.
-- Run this FIRST in the new project's SQL Editor, then migrations 0001..0007.
-- RLS is enabled everywhere; the Flask backend uses the service-role key.

create table if not exists public.farmers (
  id uuid primary key default gen_random_uuid(),
  full_name text not null,
  phone text not null,
  email text unique,
  district text not null,
  sub_county text, village text,
  farm_size_acres numeric,
  crops_grown text[],
  is_verified boolean default false,
  is_premium boolean default false,
  profile_photo text,
  otp_code text, otp_expires_at timestamptz,
  created_at timestamptz default now(),
  updated_at timestamptz default now()
);

create table if not exists public.buyers (
  id uuid primary key default gen_random_uuid(),
  full_name text not null,
  phone text not null unique,
  email text unique,
  buyer_type text default 'individual'
    check (buyer_type in ('individual','vendor','hotel','restaurant','exporter','ngo')),
  district text, business_name text,
  is_verified boolean default false,
  otp_code text, otp_expires_at timestamptz,
  created_at timestamptz default now()
);

create table if not exists public.listings (
  id uuid primary key default gen_random_uuid(),
  farmer_id uuid references public.farmers(id),
  crop_name text not null,
  category text default 'other'
    check (category in ('staples','vegetables','fruits','cash crops','other')),
  quantity_kg numeric not null,
  price_per_kg numeric not null,
  unit text default 'kg',
  description text,
  district text not null,
  sub_county text, village text,
  is_organic boolean default false,
  is_verified boolean default false,
  is_available boolean default true,
  harvest_date date, expiry_date date,
  photo_url text,
  views integer default 0,
  created_at timestamptz default now(),
  updated_at timestamptz default now(),
  farmer_phone text,
  discount_pct integer not null default 0 check (discount_pct between 0 and 100),
  sale_unit text default 'per kg',
  min_order_kg integer not null default 0,
  delivery_available boolean not null default false,
  payment_terms text, image_url text, video_url text
);

create table if not exists public.orders (
  id uuid primary key default gen_random_uuid(),
  listing_id uuid references public.listings(id),
  buyer_id uuid references public.buyers(id),
  farmer_id uuid references public.farmers(id),
  quantity_kg numeric not null,
  price_per_kg numeric not null,
  total_amount numeric generated always as (quantity_kg * price_per_kg) stored,
  status text default 'pending'
    check (status in ('pending','confirmed','packed','in_transit','delivered','cancelled','disputed')),
  delivery_type text default 'delivery' check (delivery_type in ('pickup','delivery')),
  delivery_address text,
  payment_method text default 'mtn_momo'
    check (payment_method in ('mtn_momo','airtel_money','cash','bank')),
  payment_status text default 'unpaid' check (payment_status in ('unpaid','paid','refunded')),
  payment_ref text, notes text,
  created_at timestamptz default now(),
  updated_at timestamptz default now(),
  buyer_phone text, total_price numeric, item_category text, tracking_code text
);

create table if not exists public.deliveries (
  id uuid primary key default gen_random_uuid(),
  order_id uuid references public.orders(id),
  driver_name text, driver_phone text, vehicle_plate text,
  status text default 'scheduled'
    check (status in ('scheduled','picked_up','in_transit','at_checkpoint','delivered','failed')),
  pickup_location text, delivery_location text,
  estimated_arrival timestamptz, actual_arrival timestamptz,
  tracking_code text unique default upper(substr(md5(random()::text),1,8)),
  gps_lat numeric, gps_lng numeric, notes text,
  created_at timestamptz default now(), updated_at timestamptz default now()
);

create table if not exists public.reviews (
  id uuid primary key default gen_random_uuid(),
  order_id uuid references public.orders(id),
  listing_id uuid,
  farmer_id uuid not null, buyer_id uuid not null,
  rating integer not null check (rating between 1 and 5),
  comment text,
  created_at timestamptz default now()
);

create table if not exists public.payouts (
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

create table if not exists public.platform_config (
  id integer primary key default 1 check (id = 1),
  commission_pct numeric not null default 0,
  payout_note text default 'Set commission_pct here (e.g. 5 = 5%). Applies to future payouts only.',
  updated_at timestamptz default now()
);
insert into public.platform_config (id) values (1) on conflict do nothing;

create table if not exists public.market_prices (
  id uuid primary key default gen_random_uuid(),
  crop_name text not null, market text not null, district text not null,
  price_per_kg numeric not null, unit text default 'kg',
  price_min numeric, price_max numeric, change_pct numeric default 0,
  recorded_by text default 'AgriBridge Agent',
  price_date date default current_date,
  created_at timestamptz default now()
);

create table if not exists public.training_videos (
  id uuid primary key default gen_random_uuid(),
  title text not null, description text, youtube_url text not null, thumbnail_url text,
  crop_tags text[],
  language text default 'english' check (language in ('english','luganda','swahili','runyankole')),
  level text default 'beginner' check (level in ('beginner','intermediate','advanced')),
  duration_mins integer, views integer default 0, is_featured boolean default false,
  created_at timestamptz default now()
);

create table if not exists public.contact_messages (
  id uuid primary key default gen_random_uuid(),
  full_name text not null, contact text not null, role text, message text not null,
  is_read boolean default false, replied_at timestamptz,
  created_at timestamptz default now()
);

create table if not exists public.supply_orders (
  id uuid primary key default gen_random_uuid(),
  buyer_phone text not null, buyer_name text,
  product_name text not null,
  quantity integer default 1,
  unit_price numeric not null,
  total_price numeric generated always as (quantity::numeric * unit_price) stored,
  district text, status text default 'pending',
  created_at timestamptz default now()
);

create table if not exists public.disease_reports (
  id uuid primary key default gen_random_uuid(),
  farmer_phone text, crop_name text not null, affected_part text,
  symptom text not null, diagnosis_name text, district text, weather_cond text,
  description text, treatment_given text,
  reported_to_extension boolean default false,
  created_at timestamptz default now()
);

create table if not exists public.price_alerts (
  id uuid primary key default gen_random_uuid(),
  phone text not null, crop_name text not null, target_price numeric not null,
  direction text default 'above' check (direction in ('above','below')),
  is_active boolean default true, triggered_at timestamptz,
  created_at timestamptz default now()
);

create table if not exists public.ussd_sessions (
  id uuid primary key default gen_random_uuid(),
  session_id text not null unique, phone_number text not null, network_code text,
  current_menu text default 'main', session_data jsonb default '{}'::jsonb,
  is_active boolean default true, last_input text,
  created_at timestamptz default now(), updated_at timestamptz default now()
);

create table if not exists public.animal_listings (
  id uuid primary key default gen_random_uuid(),
  farmer_id uuid, farmer_name text, farmer_phone text,
  name text, species text, category text,
  price numeric, unit text, qty integer, district text, description text,
  health_cert boolean, maaif_certified boolean,
  status text default 'active', created_at timestamptz,
  image_url text, video_url text
);

create table if not exists public.cart_sessions (
  user_phone text primary key, cart_data jsonb, updated_at timestamptz
);

create table if not exists public.fraud_flags (
  id bigint generated by default as identity primary key,
  type text not null, details jsonb, ip text, ts timestamptz default now()
);

create table if not exists public.supplier_products (
  id uuid primary key default gen_random_uuid(),
  category text not null, name text not null, brand text,
  price numeric not null default 0, unit text, badge text, description text, image_id text,
  in_stock boolean not null default true,
  created_at timestamptz default now(), updated_at timestamptz default now(),
  supplier_id uuid, supplier_name text, stock_qty numeric, image_url text
);

create table if not exists public.vet_bookings (
  id uuid primary key default gen_random_uuid(),
  service_type text, farmer_phone text, status text default 'pending', notes text,
  created_at timestamptz default now()
);

create table if not exists public.community_posts (
  id uuid primary key default gen_random_uuid(),
  author_name text, content text not null, category text default 'general',
  created_at timestamptz default now()
);

do $$ declare t text; begin
  foreach t in array array['farmers','buyers','listings','orders','deliveries','reviews','payouts',
    'platform_config','market_prices','training_videos','contact_messages','supply_orders',
    'disease_reports','price_alerts','ussd_sessions','animal_listings','cart_sessions',
    'fraud_flags','supplier_products','vet_bookings','community_posts']
  loop execute format('alter table public.%I enable row level security', t); end loop;
end $$;

-- ---------------------------------------------------------------
-- Row-level security policies (copied from the old project, hardened)
-- Deliberately NOT copied:
--   orders_owner_insert        -> orders are created only via create_order_atomic (migration 0001)
--   ussd_sessions_public_select-> would expose every caller's phone number
--   cart_sessions_auth_*       -> any signed-in user could read everyone's cart
-- The Flask backend uses the service-role key, which bypasses RLS.
-- ---------------------------------------------------------------
create policy animal_listings_owner_insert on public.animal_listings for insert to authenticated with check (auth.uid() = farmer_id);
create policy animal_listings_owner_update on public.animal_listings for update to authenticated using (auth.uid() = farmer_id) with check (auth.uid() = farmer_id);
create policy animal_listings_public_select on public.animal_listings for select to public using (true);
create policy buyers_owner_select on public.buyers for select to authenticated using (auth.uid() = id);
create policy community_posts_auth_insert on public.community_posts for insert to authenticated with check (true);
create policy community_posts_public_select on public.community_posts for select to public using (true);
create policy contact_messages_public_insert on public.contact_messages for insert to public with check (true);
create policy deliveries_public_select on public.deliveries for select to public using (true);
create policy disease_reports_public_insert on public.disease_reports for insert to public with check (true);
create policy farmers_owner_update on public.farmers for update to authenticated using (auth.uid() = id) with check (auth.uid() = id);
create policy farmers_public_select on public.farmers for select to public using (true);
create policy fraud_admin_only on public.fraud_flags for all to public using (false);
create policy listings_owner_insert on public.listings for insert to authenticated with check (auth.uid() = farmer_id);
create policy listings_owner_update on public.listings for update to authenticated using (auth.uid() = farmer_id) with check (auth.uid() = farmer_id);
create policy listings_public_select on public.listings for select to public using (true);
create policy market_prices_public_select on public.market_prices for select to public using (true);
create policy orders_farmer_update on public.orders for update to authenticated using (auth.uid() = farmer_id) with check (auth.uid() = farmer_id);
create policy orders_owner_select on public.orders for select to authenticated using (auth.uid() = buyer_id or auth.uid() = farmer_id);
create policy payouts_owner_select on public.payouts for select to authenticated using (auth.uid() = farmer_id);
create policy platform_config_public_select on public.platform_config for select to public using (true);
create policy price_alerts_public_insert on public.price_alerts for insert to public with check (true);
create policy reviews_owner_delete on public.reviews for delete to authenticated using (auth.uid() = buyer_id);
create policy reviews_owner_update on public.reviews for update to authenticated using (auth.uid() = buyer_id) with check (auth.uid() = buyer_id);
create policy reviews_public_select on public.reviews for select to public using (true);
create policy reviews_verified_insert on public.reviews for insert to authenticated with check (
  auth.uid() = buyer_id and exists (select 1 from public.orders o
    where o.id = reviews.order_id and o.buyer_id = auth.uid()
      and o.farmer_id = reviews.farmer_id and o.status = 'delivered'));
create policy supplier_products_owner_delete on public.supplier_products for delete to authenticated using (auth.uid() = supplier_id);
create policy supplier_products_owner_insert on public.supplier_products for insert to authenticated with check (auth.uid() = supplier_id);
create policy supplier_products_owner_update on public.supplier_products for update to authenticated using (auth.uid() = supplier_id) with check (auth.uid() = supplier_id);
create policy supplier_products_public_select on public.supplier_products for select to public using (true);
create policy supply_orders_public_insert on public.supply_orders for insert to public with check (true);
create policy training_videos_public_select on public.training_videos for select to public using (true);
create policy vet_bookings_public_insert on public.vet_bookings for insert to public with check (true);

-- farmers is publicly readable (profiles), but one-time codes must never be.
revoke select (otp_code, otp_expires_at) on public.farmers from anon, authenticated;
revoke select (otp_code, otp_expires_at) on public.buyers from anon, authenticated;
