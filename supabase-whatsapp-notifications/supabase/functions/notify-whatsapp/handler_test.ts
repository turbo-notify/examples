// Tests for the notify-whatsapp handler. No network, no Supabase project, no real phone number:
// the handler receives `fetch`, the environment and the logger as arguments.
import { assert, assertEquals, assertNotEquals, assertStringIncludes } from "@std/assert";
import { type Deps, handle, type LogLine } from "./handler.ts";

const SECRET = "test-webhook-secret";
const API_KEY = "tn_live_test_key_not_real";
// Reserved fictional range: never a real person's number.
const PHONE = "+12025550123";
const TEXT_MARKERS = ["Order 1042"];

interface Call {
  url: string;
  init: RequestInit;
}

interface Harness {
  deps: Deps;
  calls: Call[];
  logs: LogLine[];
}

function harness(
  respond: () => Response | Promise<Response> = () =>
    Response.json({ message_id: "msg_0123456789abcdef0123456789abcdef" }, {
      status: 202,
      headers: { "X-Request-ID": "req_test" },
    }),
  env: Deps["env"] = { WEBHOOK_SECRET: SECRET, TN_API_KEY: API_KEY },
): Harness {
  const calls: Call[] = [];
  const logs: LogLine[] = [];
  const deps: Deps = {
    env,
    fetch: (url, init) => {
      calls.push({ url, init });
      return Promise.resolve(respond());
    },
    log: (line) => logs.push(line),
  };
  return { deps, calls, logs };
}

function webhook(
  payload: unknown,
  headers: Record<string, string> = { "x-webhook-secret": SECRET },
  method = "POST",
): Request {
  return new Request("http://localhost/functions/v1/notify-whatsapp", {
    method,
    headers: { "content-type": "application/json", ...headers },
    body: method === "GET"
      ? undefined
      : typeof payload === "string"
      ? payload
      : JSON.stringify(payload),
  });
}

function insertOrder(overrides: Record<string, unknown> = {}) {
  return {
    type: "INSERT",
    table: "orders",
    schema: "public",
    record: { id: 1042, phone: PHONE, whatsapp_opt_in: true, status: "received", ...overrides },
    old_record: null,
  };
}

function shipOrder(newStatus = "shipped", oldStatus = "received") {
  return {
    type: "UPDATE",
    table: "orders",
    schema: "public",
    record: { id: 1042, phone: PHONE, whatsapp_opt_in: true, status: newStatus },
    old_record: { id: 1042, phone: PHONE, whatsapp_opt_in: true, status: oldStatus },
  };
}

async function body(res: Response) {
  return await res.json();
}

// A1
Deno.test("A1 sends one message with the documented request shape", async () => {
  const h = harness();
  const res = await handle(webhook(insertOrder()), h.deps);
  assertEquals(res.status, 200);
  const out = await body(res);
  assertEquals(out.sent, true);
  assertEquals(out.message_id, "msg_0123456789abcdef0123456789abcdef");

  assertEquals(h.calls.length, 1);
  const { url, init } = h.calls[0];
  assertEquals(url, "https://api.turbonotify.com/v1/numbers/main/messages");
  assertEquals(init.method, "POST");
  const headers = new Headers(init.headers);
  assertEquals(headers.get("authorization"), `Bearer ${API_KEY}`);
  assertEquals(headers.get("content-type"), "application/json");
  assert((headers.get("idempotency-key") ?? "").length > 0);
  const sent = JSON.parse(init.body as string);
  assertEquals(sent.to, { number: PHONE });
  assertStringIncludes(sent.content.text, TEXT_MARKERS[0]);
  assertEquals(Object.keys(sent).sort(), ["content", "to"]);
});

Deno.test("A1 honours TN_API_BASE_URL and TN_NUMBER", async () => {
  const h = harness(undefined, {
    WEBHOOK_SECRET: SECRET,
    TN_API_KEY: API_KEY,
    TN_API_BASE_URL: "http://localhost:9000/",
    TN_NUMBER: "num_0123456789abcdef0123456789abcdef",
  });
  await handle(webhook(insertOrder()), h.deps);
  assertEquals(
    h.calls[0].url,
    "http://localhost:9000/v1/numbers/num_0123456789abcdef0123456789abcdef/messages",
  );
});

// A2
Deno.test("A2 rejects a missing or wrong secret and calls nothing", async () => {
  const cases: Array<Record<string, string>> = [
    {},
    { "x-webhook-secret": "wrong" },
    { "x-webhook-secret": "" },
  ];
  for (const headers of cases) {
    const h = harness();
    const res = await handle(webhook(insertOrder(), headers), h.deps);
    assertEquals(res.status, 401);
    assertEquals(h.calls.length, 0);
  }
});

Deno.test("A2 answers 500 misconfigured when the secret or the key is not set", async () => {
  for (const env of [{ TN_API_KEY: API_KEY }, { WEBHOOK_SECRET: SECRET }, {}]) {
    const h = harness(undefined, env);
    const res = await handle(webhook(insertOrder()), h.deps);
    assertEquals(res.status, 500);
    assertEquals((await body(res)).error, "misconfigured");
    assertEquals(h.calls.length, 0);
  }
});

Deno.test("A2 answers 405 to anything but POST", async () => {
  const h = harness();
  const res = await handle(webhook(null, {}, "GET"), h.deps);
  assertEquals(res.status, 405);
  assertEquals(h.calls.length, 0);
});

Deno.test("invalid JSON answers 400", async () => {
  const h = harness();
  const res = await handle(webhook("{not json"), h.deps);
  assertEquals(res.status, 400);
  assertEquals(h.calls.length, 0);
});

// A3
Deno.test("A3 skips without opt-in", async () => {
  for (const optIn of [false, null, undefined, "true", 1]) {
    const h = harness();
    const payload = insertOrder({ whatsapp_opt_in: optIn });
    const res = await handle(webhook(payload), h.deps);
    assertEquals(res.status, 200);
    assertEquals((await body(res)).skipped, "no_opt_in");
    assertEquals(h.calls.length, 0);
  }
});

// A4
Deno.test("A4 skips a phone that is not E.164", async () => {
  const bad = [
    "",
    "12025550123",
    "+0123456789",
    "+1 202 555 0123",
    "(202) 555-0123",
    "+123",
    "+1234567890123456",
    null,
    42,
  ];
  for (const phone of bad) {
    const h = harness();
    const res = await handle(webhook(insertOrder({ phone })), h.deps);
    assertEquals(res.status, 200);
    assertEquals((await body(res)).skipped, "invalid_phone", `phone: ${String(phone)}`);
    assertEquals(h.calls.length, 0);
  }
});

// A5
Deno.test("A5 sends the shipped text when the status becomes shipped", async () => {
  const h = harness();
  const res = await handle(webhook(shipOrder()), h.deps);
  assertEquals(res.status, 200);
  assertEquals((await body(res)).sent, true);
  const sent = JSON.parse(h.calls[0].init.body as string);
  assertStringIncludes(sent.content.text.toLowerCase(), "shipped");
});

Deno.test("A5 skips every other event", async () => {
  const cases = [
    shipOrder("processing", "received"),
    shipOrder("shipped", "shipped"), // status did not change
    { ...insertOrder(), type: "DELETE", record: null, old_record: insertOrder().record },
    { ...insertOrder(), table: "customers" },
  ];
  for (const payload of cases) {
    const h = harness();
    const res = await handle(webhook(payload), h.deps);
    assertEquals(res.status, 200);
    assertEquals((await body(res)).skipped, "not_applicable");
    assertEquals(h.calls.length, 0);
  }
});

// A6
Deno.test("A6 the same event gets the same Idempotency-Key, different events differ", async () => {
  async function keyFor(payload: unknown) {
    const h = harness();
    await handle(webhook(payload), h.deps);
    return new Headers(h.calls[0].init.headers).get("idempotency-key");
  }
  const a = await keyFor(insertOrder());
  const again = await keyFor(insertOrder());
  const otherId = await keyFor(insertOrder({ id: 1043 }));
  const shipped = await keyFor(shipOrder());
  assertEquals(a, again);
  assertNotEquals(a, otherId);
  assertNotEquals(a, shipped);
  assertNotEquals(otherId, shipped);
  // A retry of the same row after an unrelated column changed is still the same business event.
  assertEquals(a, await keyFor(insertOrder({ note: "changed later" })));
});

// A7
Deno.test("A7 retryable answers from Turbo Notify become 502", async () => {
  for (const status of [429, 500, 502, 503, 409]) {
    const h = harness(() => Response.json({ error: { code: "x" } }, { status }));
    const res = await handle(webhook(insertOrder()), h.deps);
    assertEquals(res.status, 502, `status ${status}`);
    assertEquals((await body(res)).retryable, true);
  }
});

Deno.test("A7 a 429 for a rate limit is retryable, with or without a readable code", async () => {
  const bodies: Array<() => Response> = [
    () => Response.json({ error: { code: "rate_limit_exceeded" } }, { status: 429 }),
    () => Response.json({ error: { code: "messages_per_minute_limit_exceeded" } }, { status: 429 }),
    () => Response.json({ error: { code: "recipients_per_day_limit_exceeded" } }, { status: 429 }),
    () => new Response("not json", { status: 429 }),
  ];
  for (const respond of bodies) {
    const h = harness(respond);
    const res = await handle(webhook(insertOrder()), h.deps);
    assertEquals(res.status, 502);
    assertEquals((await body(res)).retryable, true);
  }
});

Deno.test("A7 account and quota problems become 500 tn_account, never a retry or a silent success", async () => {
  const cases: Array<[number, string]> = [
    [402, "subscription_inactive"],
    [402, "payment_required"],
    [402, "contact_quota_exceeded"],
    [402, "feature_not_available"],
    [429, "messages_quota_exceeded"],
  ];
  for (const [status, code] of cases) {
    const h = harness(() => Response.json({ error: { code } }, { status }));
    const res = await handle(webhook(insertOrder()), h.deps);
    assertEquals(res.status, 500, `${status} ${code}`);
    const out = await body(res);
    assertEquals(out.error, "tn_account");
    assertEquals(out.retryable, undefined);
    assertEquals(h.logs[0].outcome, "account");
    assertEquals(h.logs[0].reason, code);
  }
});

Deno.test("A7 a network failure becomes 502", async () => {
  const h = harness(() => {
    throw new TypeError("connection refused");
  });
  const res = await handle(webhook(insertOrder()), h.deps);
  assertEquals(res.status, 502);
  assertEquals((await body(res)).retryable, true);
});

Deno.test("A7 credential problems become 500 tn_credentials", async () => {
  for (const status of [401, 403]) {
    const h = harness(() => Response.json({ error: { code: "not_authenticated" } }, { status }));
    const res = await handle(webhook(insertOrder()), h.deps);
    assertEquals(res.status, 500);
    assertEquals((await body(res)).error, "tn_credentials");
  }
});

Deno.test("A7 permanent client errors become 200 failed with the code", async () => {
  for (
    const [status, code] of [[400, "invalid_json"], [404, "number_not_found"], [
      422,
      "validation_error",
    ]] as const
  ) {
    const h = harness(() => Response.json({ error: { code } }, { status }));
    const res = await handle(webhook(insertOrder()), h.deps);
    assertEquals(res.status, 200, `status ${status}`);
    assertEquals((await body(res)).failed, code);
  }
});

Deno.test("A7 a permanent error with no readable code still answers 200 failed", async () => {
  const h = harness(() => new Response("not json", { status: 422 }));
  const res = await handle(webhook(insertOrder()), h.deps);
  assertEquals(res.status, 200);
  assertEquals((await body(res)).failed, "http_422");
});

// A8
Deno.test("A8 no log line carries the phone, the text, the key or the secret", async () => {
  const scenarios: Array<[Request, () => Response]> = [
    [
      webhook(insertOrder()),
      () =>
        Response.json({ message_id: "msg_1" }, {
          status: 202,
          headers: { "X-Request-ID": "req_1" },
        }),
    ],
    [webhook(insertOrder({ whatsapp_opt_in: false })), () => new Response(null, { status: 202 })],
    [webhook(insertOrder({ phone: "bad" })), () => new Response(null, { status: 202 })],
    [
      webhook(insertOrder()),
      () => Response.json({ error: { code: "number_not_connected" } }, { status: 409 }),
    ],
    [
      webhook(insertOrder()),
      () => Response.json({ error: { code: "not_authenticated" } }, { status: 401 }),
    ],
    [
      webhook(insertOrder()),
      () => Response.json({ error: { code: "validation_error" } }, { status: 422 }),
    ],
    [
      webhook(insertOrder(), { "x-webhook-secret": "wrong-secret-value" }),
      () => new Response(null, { status: 202 }),
    ],
    [webhook(shipOrder()), () => Response.json({ message_id: "msg_2" }, { status: 202 })],
  ];
  const forbidden = [
    PHONE,
    "2025550123",
    API_KEY,
    SECRET,
    "wrong-secret-value",
    "Order 1042",
    "shipped.",
  ];
  for (const [req, respond] of scenarios) {
    const h = harness(respond);
    await handle(req, h.deps);
    const dump = JSON.stringify(h.logs);
    for (const needle of forbidden) {
      assert(!dump.includes(needle), `log leaked ${needle}: ${dump}`);
    }
  }
});

Deno.test("A8 a successful send logs the table, the record id, the outcome and the request id", async () => {
  const h = harness();
  await handle(webhook(insertOrder()), h.deps);
  assertEquals(h.logs.length, 1);
  const line = h.logs[0];
  assertEquals(line.table, "orders");
  assertEquals(line.record_id, "1042");
  assertEquals(line.outcome, "sent");
  assertEquals(line.request_id, "req_test");
});
