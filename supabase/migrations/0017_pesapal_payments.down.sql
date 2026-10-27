-- 0017_pesapal_payments.down.sql
-- Copyright (C) 2026 zealmugumya-creator. All rights reserved.
--
-- Rolling back removes the gateway tables, including subscription and charge
-- history. Export those rows before running this in production. The original
-- narrow payment_method check is restored NOT VALID so it never rescans
-- existing card/Pesapal orders.
begin;

drop table if exists public.subscription_charges;
drop table if exists public.payment_subscriptions;
drop table if exists public.payment_gateway_state;

do $$
begin
  alter table public.orders
    drop constraint if exists orders_payment_method_allowed;

  if not exists (
    select 1 from pg_constraint
     where conname = 'orders_payment_method_check'
       and conrelid = 'public.orders'::regclass
  ) then
    alter table public.orders
      add constraint orders_payment_method_check
      check (payment_method in ('mtn_momo','airtel_money','cash','bank'))
      not valid;
  end if;
end $$;

commit;
