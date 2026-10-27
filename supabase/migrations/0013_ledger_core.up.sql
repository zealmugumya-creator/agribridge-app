-- AgriBridge migration 0013 — Double-entry ledger core
-- Foundation of the Financial Control Center. Nothing posts to it yet: wiring
-- verified payment / delivery events comes in later migrations and backend work.
--
-- Guarantees enforced IN THE DATABASE (not just in app code):
--   * every journal balances (debits = credits), checked at COMMIT even for raw inserts
--   * posted entries are append-only: no UPDATE / DELETE / TRUNCATE
--   * a journal reference is unique, so a retried webhook cannot post twice
--   * corrections happen only through reverse_journal() (mirror entry + audit row)
--   * only admins can read; only the backend (service role) can post
--
-- Amounts are numeric(18,2). UGX has no minor unit, but 2 decimals leaves room for
-- other currencies later. Single-currency for now: all accounts are UGX.
--
-- SAFETY: additive only (3 tables, 1 view, helper functions). Does not touch orders
-- or payouts. Rollback: 0013_ledger_core.down.sql

begin;

-- ── Chart of accounts ────────────────────────────────────────────────────────
create table if not exists public.ledger_accounts (
  code text primary key,
  name text not null,
  account_type text not null,
  normal_side text not null,
  is_active boolean not null default true,
  created_at timestamptz not null default now(),
  constraint ledger_accounts_type_chk
    check (account_type in ('asset','liability','revenue','expense')),
  constraint ledger_accounts_side_chk
    check (normal_side in ('debit','credit'))
);

insert into public.ledger_accounts (code, name, account_type, normal_side) values
  ('PROVIDER_CLEARING',    'Payment provider clearing (money received, not yet settled)', 'asset',     'debit'),
  ('COD_CASH_HELD',        'Cash collected on delivery, not yet banked',                  'asset',     'debit'),
  ('FARMER_PAYABLE',       'Owed to farmers',                                             'liability', 'credit'),
  ('TRANSPORTER_PAYABLE',  'Owed to transporters',                                        'liability', 'credit'),
  ('CUSTOMER_REFUND_PAYABLE','Owed back to customers',                                    'liability', 'credit'),
  ('SUSPENSE_OVERPAYMENT', 'Unallocated receipts / overpayments',                         'liability', 'credit'),
  ('COMMISSION_REVENUE',   'AgriBridge commission revenue',                               'revenue',   'credit'),
  ('DELIVERY_REVENUE',     'AgriBridge delivery revenue',                                 'revenue',   'credit'),
  ('LOGISTICS_EXPENSE',    'Logistics expense',                                           'expense',   'debit')
on conflict (code) do nothing;

-- ── Journals (one per business event) and lines ─────────────────────────────
create table if not exists public.ledger_journals (
  id uuid primary key default gen_random_uuid(),
  reference text not null unique,          -- idempotency key, e.g. 'payment:<flw_id>'
  kind text not null,                      -- customer_payment | settlement | reversal | ...
  currency text not null default 'UGX',
  payment_ref text,                        -- checkout reference shared by order rows
  description text,
  status text not null default 'posted',
  reverses_journal_id uuid unique references public.ledger_journals(id),
  posted_by uuid,
  metadata jsonb not null default '{}'::jsonb,
  posted_at timestamptz not null default now(),
  constraint ledger_journals_status_chk check (status in ('posted','reversed'))
);

create index if not exists idx_ledger_journals_payment_ref
  on public.ledger_journals (payment_ref) where payment_ref is not null;
create index if not exists idx_ledger_journals_posted_at
  on public.ledger_journals (posted_at desc);

create table if not exists public.ledger_lines (
  id bigint generated always as identity primary key,
  journal_id uuid not null references public.ledger_journals(id),
  account_code text not null references public.ledger_accounts(code),
  side text not null,
  amount numeric(18,2) not null,
  memo text,
  constraint ledger_lines_side_chk check (side in ('debit','credit')),
  constraint ledger_lines_amount_chk check (amount > 0)
);

create index if not exists idx_ledger_lines_journal on public.ledger_lines (journal_id);
create index if not exists idx_ledger_lines_account on public.ledger_lines (account_code);

-- ── Append-only guards ───────────────────────────────────────────────────────
create or replace function public.ledger_block_mutation()
returns trigger language plpgsql as $$
begin
  raise exception 'ledger is append-only: % on % is not allowed; post a reversal instead',
    tg_op, tg_table_name using errcode = '55000';
end $$;

-- A journal may change in exactly one way: posted -> reversed, and only once a
-- reversing journal for it exists. Every other column is frozen.
create or replace function public.ledger_journal_guard()
returns trigger language plpgsql as $$
begin
  if tg_op = 'UPDATE'
     and old.status = 'posted' and new.status = 'reversed'
     and (new.id, new.reference, new.kind, new.currency, new.payment_ref,
          new.description, new.reverses_journal_id, new.posted_by,
          new.metadata, new.posted_at)
         is not distinct from
         (old.id, old.reference, old.kind, old.currency, old.payment_ref,
          old.description, old.reverses_journal_id, old.posted_by,
          old.metadata, old.posted_at)
     and exists (select 1 from public.ledger_journals r where r.reverses_journal_id = old.id)
  then
    return new;
  end if;
  raise exception 'ledger is append-only: % on ledger_journals is not allowed; post a reversal instead',
    tg_op using errcode = '55000';
end $$;

drop trigger if exists ledger_lines_no_update_delete on public.ledger_lines;
create trigger ledger_lines_no_update_delete
  before update or delete on public.ledger_lines
  for each row execute function public.ledger_block_mutation();

drop trigger if exists ledger_lines_no_truncate on public.ledger_lines;
create trigger ledger_lines_no_truncate
  before truncate on public.ledger_lines
  for each statement execute function public.ledger_block_mutation();

drop trigger if exists ledger_journals_guard on public.ledger_journals;
create trigger ledger_journals_guard
  before update or delete on public.ledger_journals
  for each row execute function public.ledger_journal_guard();

drop trigger if exists ledger_journals_no_truncate on public.ledger_journals;
create trigger ledger_journals_no_truncate
  before truncate on public.ledger_journals
  for each statement execute function public.ledger_block_mutation();

-- ── Balance check at COMMIT (catches raw inserts that bypass post_journal) ──
create or replace function public.ledger_check_balanced()
returns trigger language plpgsql as $$
declare
  v_debit numeric; v_credit numeric; v_n int;
begin
  select coalesce(sum(amount) filter (where side = 'debit'), 0),
         coalesce(sum(amount) filter (where side = 'credit'), 0),
         count(*)
    into v_debit, v_credit, v_n
    from public.ledger_lines where journal_id = new.journal_id;
  if v_n < 2 or v_debit <> v_credit then
    raise exception 'journal % is not balanced (debits %, credits %, lines %)',
      new.journal_id, v_debit, v_credit, v_n using errcode = '23514';
  end if;
  return null;
end $$;

drop trigger if exists ledger_lines_balanced on public.ledger_lines;
create constraint trigger ledger_lines_balanced
  after insert on public.ledger_lines
  deferrable initially deferred
  for each row execute function public.ledger_check_balanced();

-- ── post_journal: the only supported way to write ───────────────────────────
-- p_lines: [{"account":"FARMER_PAYABLE","side":"credit","amount":90000,"memo":"..."}, ...]
-- Replaying an existing p_reference returns the original journal id and posts nothing.
create or replace function public.post_journal(
  p_reference   text,
  p_kind        text,
  p_description text,
  p_lines       jsonb,
  p_payment_ref text  default null,
  p_actor       uuid  default null,
  p_metadata    jsonb default '{}'::jsonb,
  p_currency    text  default 'UGX'
) returns uuid
language plpgsql security definer set search_path = public as $$
declare
  v_id uuid; v_line jsonb; v_side text; v_amt numeric;
  v_debit numeric := 0; v_credit numeric := 0; v_n int := 0;
begin
  if p_reference is null or btrim(p_reference) = '' then
    raise exception 'reference is required' using errcode = '22023';
  end if;
  if p_kind is null or btrim(p_kind) = '' then
    raise exception 'kind is required' using errcode = '22023';
  end if;

  select id into v_id from public.ledger_journals where reference = p_reference;
  if found then return v_id; end if;

  if jsonb_typeof(p_lines) is distinct from 'array' then
    raise exception 'lines must be a JSON array' using errcode = '22023';
  end if;

  for v_line in select value from jsonb_array_elements(p_lines) loop
    v_n := v_n + 1;
    v_side := v_line->>'side';
    v_amt := (v_line->>'amount')::numeric;
    if v_side is null or v_side not in ('debit','credit') then
      raise exception 'line %: side must be debit or credit', v_n using errcode = '22023';
    end if;
    if v_amt is null or v_amt <= 0 then
      raise exception 'line %: amount must be positive', v_n using errcode = '22023';
    end if;
    if round(v_amt, 2) <> v_amt then
      raise exception 'line %: amount has more than 2 decimal places', v_n using errcode = '22023';
    end if;
    perform 1 from public.ledger_accounts
      where code = v_line->>'account' and is_active;
    if not found then
      raise exception 'line %: unknown or inactive account %', v_n, v_line->>'account'
        using errcode = '22023';
    end if;
    if v_side = 'debit' then v_debit := v_debit + v_amt; else v_credit := v_credit + v_amt; end if;
  end loop;

  if v_n < 2 then
    raise exception 'a journal needs at least two lines' using errcode = '23514';
  end if;
  if v_debit <> v_credit then
    raise exception 'journal is not balanced (debits %, credits %)', v_debit, v_credit
      using errcode = '23514';
  end if;

  insert into public.ledger_journals
    (reference, kind, currency, payment_ref, description, posted_by, metadata)
  values
    (p_reference, p_kind, p_currency, p_payment_ref, p_description, p_actor,
     coalesce(p_metadata, '{}'::jsonb))
  on conflict (reference) do nothing
  returning id into v_id;

  if v_id is null then  -- lost a race with a concurrent identical post
    select id into v_id from public.ledger_journals where reference = p_reference;
    return v_id;
  end if;

  insert into public.ledger_lines (journal_id, account_code, side, amount, memo)
  select v_id, l->>'account', l->>'side', (l->>'amount')::numeric, l->>'memo'
    from jsonb_array_elements(p_lines) l;

  if p_actor is not null then
    insert into public.admin_audit_log (actor, action, target, metadata)
    values (p_actor, 'ledger.posted', v_id::text,
            jsonb_build_object('reference', p_reference, 'kind', p_kind, 'total', v_debit));
  end if;
  return v_id;
end $$;

-- ── reverse_journal: the only supported way to correct a mistake ────────────
create or replace function public.reverse_journal(
  p_journal_id uuid,
  p_reference  text,
  p_actor      uuid,
  p_reason     text
) returns uuid
language plpgsql security definer set search_path = public as $$
declare
  v_orig public.ledger_journals;
  v_id uuid;
begin
  if p_actor is null then
    raise exception 'actor is required to reverse a journal' using errcode = '22023';
  end if;
  if p_reason is null or btrim(p_reason) = '' then
    raise exception 'a reason is required to reverse a journal' using errcode = '22023';
  end if;
  if p_reference is null or btrim(p_reference) = '' then
    raise exception 'reference is required' using errcode = '22023';
  end if;

  select id into v_id from public.ledger_journals where reference = p_reference;
  if found then return v_id; end if;

  select * into v_orig from public.ledger_journals where id = p_journal_id for update;
  if not found then
    raise exception 'journal % not found', p_journal_id using errcode = 'P0002';
  end if;
  if v_orig.reverses_journal_id is not null then
    raise exception 'a reversal cannot itself be reversed' using errcode = '55000';
  end if;
  if v_orig.status <> 'posted' then
    raise exception 'journal % is already reversed', p_journal_id using errcode = '55000';
  end if;

  insert into public.ledger_journals
    (reference, kind, currency, payment_ref, description, reverses_journal_id, posted_by, metadata)
  values
    (p_reference, 'reversal', v_orig.currency, v_orig.payment_ref,
     'Reversal of ' || v_orig.reference || ': ' || p_reason, p_journal_id, p_actor,
     jsonb_build_object('reason', p_reason, 'reverses', v_orig.reference))
  returning id into v_id;

  insert into public.ledger_lines (journal_id, account_code, side, amount, memo)
  select v_id, account_code,
         case side when 'debit' then 'credit' else 'debit' end,
         amount, 'reversal'
    from public.ledger_lines where journal_id = p_journal_id;

  update public.ledger_journals set status = 'reversed' where id = p_journal_id;

  insert into public.admin_audit_log (actor, action, target, metadata)
  values (p_actor, 'ledger.reversed', p_journal_id::text,
          jsonb_build_object('reason', p_reason, 'reversal_id', v_id, 'reference', p_reference));
  return v_id;
end $$;

-- ── Balances view (RLS of the underlying tables applies to the caller) ──────
create or replace view public.ledger_account_balances
with (security_invoker = true) as
select code, name, account_type, normal_side, total_debits, total_credits,
       case when normal_side = 'debit' then total_debits - total_credits
            else total_credits - total_debits end as balance
  from (
    select a.code, a.name, a.account_type, a.normal_side,
           coalesce(sum(l.amount) filter (where l.side = 'debit'), 0)  as total_debits,
           coalesce(sum(l.amount) filter (where l.side = 'credit'), 0) as total_credits
      from public.ledger_accounts a
      left join public.ledger_lines l on l.account_code = a.code
     group by a.code, a.name, a.account_type, a.normal_side
  ) t;

-- ── Access control ───────────────────────────────────────────────────────────
alter table public.ledger_accounts enable row level security;
alter table public.ledger_journals enable row level security;
alter table public.ledger_lines    enable row level security;

revoke all on public.ledger_accounts, public.ledger_journals, public.ledger_lines,
  public.ledger_account_balances from anon, authenticated;
grant select on public.ledger_accounts, public.ledger_journals, public.ledger_lines,
  public.ledger_account_balances to authenticated, service_role;

drop policy if exists ledger_accounts_admin_read on public.ledger_accounts;
create policy ledger_accounts_admin_read on public.ledger_accounts
  for select to authenticated using (public.is_admin(auth.uid()));
drop policy if exists ledger_journals_admin_read on public.ledger_journals;
create policy ledger_journals_admin_read on public.ledger_journals
  for select to authenticated using (public.is_admin(auth.uid()));
drop policy if exists ledger_lines_admin_read on public.ledger_lines;
create policy ledger_lines_admin_read on public.ledger_lines
  for select to authenticated using (public.is_admin(auth.uid()));

drop policy if exists ledger_accounts_service on public.ledger_accounts;
create policy ledger_accounts_service on public.ledger_accounts
  for select to service_role using (true);
drop policy if exists ledger_journals_service on public.ledger_journals;
create policy ledger_journals_service on public.ledger_journals
  for select to service_role using (true);
drop policy if exists ledger_lines_service on public.ledger_lines;
create policy ledger_lines_service on public.ledger_lines
  for select to service_role using (true);

revoke all on function public.post_journal(text, text, text, jsonb, text, uuid, jsonb, text)
  from public, anon, authenticated;
revoke all on function public.reverse_journal(uuid, text, uuid, text)
  from public, anon, authenticated;
grant execute on function public.post_journal(text, text, text, jsonb, text, uuid, jsonb, text)
  to service_role;
grant execute on function public.reverse_journal(uuid, text, uuid, text) to service_role;

commit;
