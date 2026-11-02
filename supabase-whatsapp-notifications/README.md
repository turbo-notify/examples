# Supabase to WhatsApp notifications

When a row changes in your [Supabase](https://supabase.com) project, your customer gets a WhatsApp
message. A **Database Webhook** calls an **Edge Function**, and the function sends the message through
the [Turbo Notify API](https://docs.turbonotify.com/en/messages/send/).

The function is one small file of logic with **no dependencies**, tested without a network. The sample
reacts to an `orders` table: a new order says "received", and an order whose `status` becomes
`shipped` says "shipped". You change the table, the columns and the texts in one file, `message.ts`.

This is an independent example. It is not made, endorsed or reviewed by Supabase.

## What it does, in order

1. The database calls the function with the changed row (the Database Webhook payload).
2. The function checks the `x-webhook-secret` header against its own secret. No match, no work.
3. `message.ts` decides whether this event should send a message, and what it says.
4. The function sends only if the row has `whatsapp_opt_in = true` and a phone in international format.
5. It calls `POST /v1/numbers/{number}/messages` with an `Idempotency-Key` built from the table, the row
   id and the kind of message, so the same event delivered twice is sent once.

| Turbo Notify answers | The function answers | Why |
|---|---|---|
| `202` | `200 {"sent": true, "message_id": "..."}` | Accepted. Delivery is reported later (webhook or polling). |
| `429` for a rate limit, `5xx`, `409`, or the call fails | `502 {"retryable": true}` | Trying again can work. |
| `402`, or `429 messages_quota_exceeded` | `500 {"error": "tn_account"}` | The account is the problem (subscription not active, or an allowance used up until the cycle turns). Repeating fails the same way, so it is shown loudly instead of reported as a success. |
| `401`, `403` | `500 {"error": "tn_credentials"}` | The key is wrong. Shows up in the function logs. |
| other `4xx` | `200 {"failed": "<code>"}` | Repeating the call cannot fix it. |

Skipped events answer `200` with the reason: `not_applicable`, `no_opt_in` or `invalid_phone`.

The logs carry the table, the row id and the outcome. They never carry the phone number, the text, the
key or the secret.

## Consent

Send WhatsApp messages only to people who agreed to receive them from you. WhatsApp decides on restrictions, and a number can be restricted whatever the pace, so sending only to people who want the message is also what keeps it healthy. The sample table has a
`whatsapp_opt_in` column that defaults to `false`, and the function refuses any row where it is not
exactly `true`. Collect the consent first, then set the column.

## What you need

- A Turbo Notify account with a connected number, and an API key with only the `messages:send`
  permission (dashboard, API Keys). See the [quickstart](https://docs.turbonotify.com/en/guides/quickstart/).
- A Supabase project and the [Supabase CLI](https://supabase.com/docs/guides/cli), signed in and linked.
- [Deno](https://deno.com), only to run the tests.

## Set it up

```text
supabase/
  config.toml                                the verify_jwt setting for the function
  migrations/20261102000000_orders.sql       the sample table
  webhook.sql                                the webhook as SQL, with placeholders
  functions/notify-whatsapp/
    index.ts                                 the Edge Function entry point
    handler.ts                               the logic (receives fetch, env and logger)
    message.ts                               the file you edit: which events, what text
    handler_test.ts                          the tests
```

1. Copy `supabase/functions/notify-whatsapp` into your project, and add the `[functions.notify-whatsapp]`
   block from `supabase/config.toml` to yours. `verify_jwt` is off because the database calls the
   function without a user token; the secret header is the check.
2. Apply the sample table (`supabase db push`), or use your own table with a `phone` column in
   international format and a `whatsapp_opt_in` column.
3. Set the secrets. `WEBHOOK_SECRET` is any long random string:

   ```text
   openssl rand -hex 32
   supabase secrets set TN_API_KEY=YOUR_KEY WEBHOOK_SECRET=THE_VALUE_FROM_ABOVE
   ```

   `.env.example` lists every setting (`TN_NUMBER` and `TN_API_BASE_URL` are optional).
4. Deploy: `supabase functions deploy notify-whatsapp`.
5. Create the webhook in the Supabase dashboard (Integrations, Database Webhooks): your table, the Insert
   and Update events, type Supabase Edge Functions, function `notify-whatsapp`, method `POST`, and the
   HTTP header `x-webhook-secret` with the value you generated. Creating it in the dashboard keeps the
   secret out of your repository. `webhook.sql` is the same thing as SQL, for when you prefer that: replace
   the two placeholders first and do not commit the result. Webhooks rely on the `pg_net` extension: the
   dashboard turns it on for you, and the first statement of `webhook.sql` does the same. Without it, every
   insert and update on the table fails with `schema "net" does not exist`.

## Try it

Without sending anything, call the deployed function with a row that did not opt in:

```text
curl -X POST https://YOUR_PROJECT.supabase.co/functions/v1/notify-whatsapp \
  -H "x-webhook-secret: THE_VALUE_FROM_ABOVE" \
  -d '{"type":"INSERT","table":"orders","schema":"public","record":{"id":1,"phone":"+12025550123","whatsapp_opt_in":false},"old_record":null}'
```

The answer is `{"skipped":"no_opt_in"}`. Then insert a row with **your own number** and the opt-in true:
the message arrives on your phone.

To run the function on your machine, without Supabase, with the settings in your environment:

```text
TN_API_KEY=YOUR_KEY WEBHOOK_SECRET=local-secret \
  deno run --allow-net --allow-env supabase/functions/notify-whatsapp/index.ts
```

and post the same JSON to `http://localhost:8000/`.

## Run it on your machine with the Supabase CLI

This is the whole path (database trigger, Edge Function, Turbo Notify API) without a hosted project. It needs
Docker and the Supabase CLI, and it sends **one real WhatsApp message**, so use your own number.

```text
supabase init
cp -R <this example>/supabase/functions/notify-whatsapp supabase/functions/
cp <this example>/supabase/migrations/*.sql supabase/migrations/
# add the [functions.notify-whatsapp] block from this example's supabase/config.toml to yours

supabase start -x gotrue,realtime,storage-api,imgproxy,mailpit,postgrest,postgres-meta,studio,logflare,vector,supavisor
supabase functions serve --env-file supabase/functions/.env
```

`supabase start` applies the migration. Put your key and a secret in `supabase/functions/.env` (see
`.env.example`), then apply `webhook.sql` with the placeholders replaced. On the local stack the database runs in
a container, so the function address is `http://host.docker.internal:54321/functions/v1/notify-whatsapp`:

```text
psql "postgresql://postgres:postgres@127.0.0.1:54322/postgres" -f webhook.sql
psql "postgresql://postgres:postgres@127.0.0.1:54322/postgres" \
  -c "insert into public.orders (phone, whatsapp_opt_in) values ('+YOUR_NUMBER', true);"
psql "postgresql://postgres:postgres@127.0.0.1:54322/postgres" \
  -c "select status_code, content from net._http_response order by id desc limit 1;"
```

The last query shows what the function answered (`{"sent":true,"message_id":"msg_..."}`). Stop everything with
`supabase stop --no-backup`.

## Tests

```text
deno task test     # unit tests, no network
deno task check    # type check
deno task lint
deno task fmt      # formatting check
```

The tests cover the request the function sends, the secret check, consent, phone validation, the event
rules, the idempotency key, the mapping of every answer from Turbo Notify, and that nothing sensitive
reaches the logs.

## How it was tested

- The unit tests, `deno check`, `deno lint` and `deno fmt --check`.
- The whole path on the local Supabase stack, with the real Edge Function runtime and the real Turbo Notify API: a
  database trigger created by `webhook.sql` fired the function; a row with consent produced a WhatsApp message on
  the phone; the same event sent twice produced one message; a status change to `shipped` produced the second
  message; a row without consent sent nothing. Running that path is how the `pg_net` requirement above was found.
- A hosted Supabase project has not been tried yet. The deploy commands and the dashboard webhook settings follow
  Supabase's documentation. The function does not depend on Supabase calling it again after a failed answer: the
  same event is sent once either way.

## Licence

MIT, like the rest of this repository.
