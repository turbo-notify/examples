// The Edge Function. All it does is wire the real environment, `fetch` and logger into the handler.
import { handle } from "./handler.ts";

Deno.serve((req) =>
  handle(req, {
    env: {
      WEBHOOK_SECRET: Deno.env.get("WEBHOOK_SECRET"),
      TN_API_KEY: Deno.env.get("TN_API_KEY"),
      TN_API_BASE_URL: Deno.env.get("TN_API_BASE_URL"),
      TN_NUMBER: Deno.env.get("TN_NUMBER"),
    },
    fetch: (url, init) => fetch(url, init),
    log: (line) => console.log(JSON.stringify(line)),
  })
);
