-- Copyright (C) 2026 zealmugumya-creator. All rights reserved.
--
-- Reverse the policy names introduced by 0014, but deliberately keep the
-- privilege revokes. Restoring anon/authenticated grants would reopen the
-- vulnerability this migration exists to close. No rows are touched.

begin;

drop policy if exists system_events_service_role on public.system_events;
drop policy if exists transporters_service_role on public.transporters;

create policy "service role full access" on public.system_events
  for all to service_role using (true) with check (true);

create policy "service role full access" on public.transporters
  for all to service_role using (true) with check (true);

commit;
