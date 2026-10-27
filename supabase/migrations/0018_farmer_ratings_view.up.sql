-- Copyright (C) 2026 zealmugumya-creator. All rights reserved.
--
-- 0018 — Give the farmer-ratings read path the relation it already queries.
--
-- Verified against the live project on 2026-10-23 (read-only, publishable key):
--   GET /rest/v1/farmer_ratings?select=farmer_id,avg_rating,review_count  -> 404
--   GET /rest/v1/reviews                                                 -> 200
-- so the relation simply does not exist. Two readers already name it:
--   * static/index.html loadHomeStats() selects farmer_id,avg_rating,review_count
--     and feeds renderHomeFarmers();
--   * 0004a_farmer_matching.up.sql:108 reads public.farmer_ratings inside
--     match_eligible_farmers().
-- Both fail quietly today: the top-rated strip requires avg_rating > 0 and hides
-- itself, so no rating is invented and no user sees a fake star.
--
-- This view serves the reader that ships and is actually called — the SPA. Note
-- that it does NOT repair match_eligible_farmers(): that function's lateral does
-- `select avg(rating) from public.farmer_ratings`, so it expects one row per
-- review, not one row per farmer. Nothing anywhere calls it (no reference in
-- app.py, services/, static/index.html or static/admin.html), so it is left as
-- the dead code it is rather than silently reshaped for a caller that does not
-- exist.
--
-- reviews already holds the real facts, and base-schema.sql's
-- reviews_verified_insert policy lets only the buyer of a *delivered* order for
-- that farmer write one, with rating checked 1..5. This migration aggregates
-- those rows; it creates no new fact, backfills nothing, and touches no data.
-- reviews.farmer_id is NOT NULL, so every review belongs to a farmer and the
-- grouping needs no orphan guard.
--
-- security_invoker is deliberate: the view is then filtered by reviews' own RLS
-- rather than by the owner's bypass, so tightening that policy later cannot leak
-- through this view. No rows are touched. Safe to re-run.

begin;

create or replace view public.farmer_ratings
  with (security_invoker = true) as
  select r.farmer_id,
         avg(r.rating::numeric) as avg_rating,
         count(*)::int          as review_count
  from public.reviews r
  group by r.farmer_id;

grant select on public.farmer_ratings to anon, authenticated;
grant all on public.farmer_ratings to service_role;

commit;
