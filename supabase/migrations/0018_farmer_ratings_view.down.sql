-- Copyright (C) 2026 zealmugumya-creator. All rights reserved.
--
-- 0018 rollback. The view is derived state, so dropping it restores the exact
-- pre-migration shape: readers fall back to the current behavior (no ratings,
-- strip hidden) rather than to stale or fabricated ones. No rows are touched.

begin;

drop view if exists public.farmer_ratings;

commit;
