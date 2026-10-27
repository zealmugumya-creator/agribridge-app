-- Rollback for 0013_ledger_core. Drops only what 0013 added.
-- WARNING: this deletes every ledger journal and line. Export them first if any were posted.
begin;

drop view if exists public.ledger_account_balances;
drop function if exists public.reverse_journal(uuid, text, uuid, text);
drop function if exists public.post_journal(text, text, text, jsonb, text, uuid, jsonb, text);

drop table if exists public.ledger_lines;
drop table if exists public.ledger_journals;
drop table if exists public.ledger_accounts;

drop function if exists public.ledger_check_balanced();
drop function if exists public.ledger_journal_guard();
drop function if exists public.ledger_block_mutation();

commit;
