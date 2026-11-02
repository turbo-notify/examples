-- Database Webhook for the sample table, written as SQL.
--
-- A Database Webhook calls the function through the pg_net extension. The Supabase dashboard turns it on
-- when you create a webhook there; applying this file does not, and without it every INSERT and UPDATE on
-- the table fails ("schema net does not exist"). The first statement below turns it on.
--
-- You can create the same webhook in the dashboard (Integrations, Database Webhooks) instead, which
-- keeps the secret out of any file. If you apply this file, replace the two placeholders first and
-- do not commit the result:
--
--   <PROJECT_REF>     your project reference (the part before .supabase.co)
--   <WEBHOOK_SECRET>  the same value you set with `supabase secrets set WEBHOOK_SECRET=...`
--
-- Local development: the database runs in a container, so use
-- http://host.docker.internal:54321/functions/v1/notify-whatsapp as the URL.

create extension if not exists pg_net;

create trigger notify_whatsapp_on_insert
after insert on public.orders
for each row
execute function supabase_functions.http_request(
  'https://<PROJECT_REF>.supabase.co/functions/v1/notify-whatsapp',
  'POST',
  '{"Content-Type":"application/json","x-webhook-secret":"<WEBHOOK_SECRET>"}',
  '{}',
  '5000'
);

-- Only when the status column changes, so an unrelated edit does not fire the function.
create trigger notify_whatsapp_on_status_change
after update on public.orders
for each row
when (old.status is distinct from new.status)
execute function supabase_functions.http_request(
  'https://<PROJECT_REF>.supabase.co/functions/v1/notify-whatsapp',
  'POST',
  '{"Content-Type":"application/json","x-webhook-secret":"<WEBHOOK_SECRET>"}',
  '{}',
  '5000'
);
