# Monitoring: Supabase -> Discord + Admin

How new activity reaches you. Two paths:

1. **Backend events** (orders, payments, cold storage / transport / loan requests, signups):
   the Flask app sends these to Discord itself.
2. **Rows the web app writes straight to Supabase** (contact messages, privacy and deletion
   requests, disease reports, vet bookings, reviews, fraud flags, ...): a Supabase Database
   Webhook posts each new row to `POST /api/hooks/supabase`, which forwards a safe summary
   (no phones, emails or tokens) to Discord and the admin **Activity** feed.

The admin **📥 Inbox** tab shows the same tables read-only. **🗺 Logistics** lives behind the
same admin password.

## Render environment variables

Render dashboard -> your service (agribridge-1) -> **Environment** -> **Add Environment Variable**.
Save, then Render redeploys.

| Variable | Where to get it |
|---|---|
| `DISCORD_WEBHOOK_URL` | Discord channel -> gear icon (Edit Channel) -> Integrations -> Webhooks -> New Webhook -> **Copy Webhook URL**. This is the catch-all channel. |
| `DISCORD_WEBHOOK_ORDERS` | Optional. Same steps in an #orders channel (orders + payments). |
| `DISCORD_WEBHOOK_SIGNUPS` | Optional. #signups channel. |
| `DISCORD_WEBHOOK_LISTINGS` | Optional. #listings channel (listings, scans, reviews, community). |
| `DISCORD_WEBHOOK_MATCHING` | Optional. #matching channel. |
| `DISCORD_WEBHOOK_TRANSPORT` | Optional. #transport channel. |
| `DISCORD_WEBHOOK_REQUESTS` | Optional. #requests channel (cold storage, cold transport, loans, vet, purchase). |
| `DISCORD_WEBHOOK_SUPPORT` | Optional. #support channel (contact, privacy, deletion, disease, fraud flags). |
| `DISCORD_WEBHOOK_ERRORS` | Optional. #errors channel (server errors, AI). |
| `SUPABASE_WEBHOOK_SECRET` | You make it up: any long random string. Generate one with `python -c "import secrets;print(secrets.token_urlsafe(32))"`. Use the same value in the SQL below. |
| `SUPABASE_URL`, `SUPABASE_KEY` | Supabase -> Project Settings -> API: Project URL and the **service_role** key (keep secret). |
| `ADMIN_PASSWORD`, `JWT_SECRET` | You choose them. Long and random. |
| `PUBLIC_BASE_URL` | Your site, e.g. `https://agribrige.com` (no trailing slash). |
| `FLW_SECRET_KEY` | Flutterwave dashboard -> Settings -> API Keys -> Secret key. |
| `FLW_WEBHOOK_HASH` | Flutterwave dashboard -> Settings -> Webhooks -> "Secret hash" (you pick it). Set the webhook URL to `https://agribridge-1-og7a.onrender.com/api/pay/webhook/flutterwave`. |
| `SENTRY_DSN` | Optional. sentry.io -> Project -> Settings -> Client Keys (DSN). |

Channels you leave unset fall back to `DISCORD_WEBHOOK_URL`.

After saving, log in to admin and open **📥 Inbox -> Send Discord test**. Each configured channel
should get a test message.

## Supabase: send new rows to the backend

Supabase dashboard -> **SQL Editor** -> New query. Replace `PASTE_SAME_SECRET_AS_RENDER`, then Run.
(Do not commit this file with the real secret in it.)

```sql
do $$
declare
  t text;
  hook_url text := 'https://agribridge-1-og7a.onrender.com/api/hooks/supabase';
  hook_headers text := '{"Content-Type":"application/json","X-Webhook-Secret":"PASTE_SAME_SECRET_AS_RENDER"}';
begin
  foreach t in array array[
    'account_deletion_requests','privacy_requests','contact_messages','disease_reports',
    'vet_bookings','purchase_requests','supply_orders','payouts','fraud_flags',
    'admin_devices','user_roles','reviews','community_posts','transporters',
    'farmers','buyers','order_offers'
  ] loop
    if to_regclass('public.' || t) is not null then
      execute format('drop trigger if exists ab_discord_hook on public.%I', t);
      execute format(
        'create trigger ab_discord_hook after insert on public.%I for each row ' ||
        'execute function supabase_functions.http_request(%L, %L, %L, %L, %L)',
        t, hook_url, 'POST', hook_headers, '{}', '5000');
    end if;
  end loop;
end $$;
```

If it errors with `schema "supabase_functions" does not exist`, enable it first:
Database -> Extensions -> turn on **pg_net**, then run again.

To remove the hooks later:
`drop trigger if exists ab_discord_hook on public.<table>;`
