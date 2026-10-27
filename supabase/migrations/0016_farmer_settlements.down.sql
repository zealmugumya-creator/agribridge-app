-- 0016_farmer_settlements.down.sql
-- Copyright (C) 2026 zealmugumya-creator. All rights reserved.
--
-- Reverses the settlement plumbing. Deliberately keeps:
--   * payouts rows (financial records; deleting them would erase obligations);
--   * admin_audit_log (audit history; 0002 owns the same table).
-- After this migration no new payout is created automatically and
-- mark_payout_paid() is gone, but an existing settlement journal remains the
-- accounting record of money that already moved.
begin;

drop trigger if exists orders_create_payout on public.orders;
drop function if exists public.create_payout_for_order();
drop function if exists public.mark_payout_paid(uuid, text, text);

alter table public.platform_config
  drop constraint if exists platform_config_commission_pct_range;

drop policy if exists admin_audit_service_0016 on public.admin_audit_log;

commit;
