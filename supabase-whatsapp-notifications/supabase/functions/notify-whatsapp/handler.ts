// Receives a Supabase Database Webhook and sends a WhatsApp message through Turbo Notify.
//
// Everything that decides something lives here and takes its inputs as arguments (the request, the
// environment, `fetch` and the logger), so the tests run without a network and without a Supabase
// project. `index.ts` wires the real ones.
import { messageFor, type WebhookPayload } from "./message.ts";

export interface Env {
  /** Shared secret the Database Webhook sends in the `x-webhook-secret` header. */
  WEBHOOK_SECRET?: string;
  /** A Turbo Notify API key. Give it only the `messages:send` scope. */
  TN_API_KEY?: string;
  /** Defaults to https://api.turbonotify.com */
  TN_API_BASE_URL?: string;
  /** The number to send from: `main`, an alias or a `num_` id. Defaults to `main`. */
  TN_NUMBER?: string;
}

export type LogLine = Record<string, string | number | boolean | undefined>;

export interface Deps {
  env: Env;
  fetch: (url: string, init: RequestInit) => Promise<Response>;
  log: (line: LogLine) => void;
}

const E164 = /^\+[1-9]\d{6,14}$/;
const DEFAULT_BASE_URL = "https://api.turbonotify.com";

export async function handle(req: Request, deps: Deps): Promise<Response> {
  if (req.method !== "POST") return reply(405, { error: "method_not_allowed" });

  const { WEBHOOK_SECRET, TN_API_KEY } = deps.env;
  if (!WEBHOOK_SECRET || !TN_API_KEY) return reply(500, { error: "misconfigured" });

  // The database calls this function without a user token (verify_jwt is off), so this header is
  // the whole caller check.
  const given = req.headers.get("x-webhook-secret") ?? "";
  if (!(await sameSecret(given, WEBHOOK_SECRET))) return reply(401, { error: "unauthorized" });

  let payload: WebhookPayload;
  try {
    payload = await req.json();
  } catch {
    return reply(400, { error: "invalid_json" });
  }

  const outgoing = messageFor(payload);
  const recordId = String(payload.record?.id ?? payload.old_record?.id ?? "");
  const base = { table: payload.table, record_id: recordId };

  if (outgoing === null || payload.record === null) {
    deps.log({ ...base, outcome: "skipped", reason: "not_applicable" });
    return reply(200, { skipped: "not_applicable" });
  }

  // Consent is the sender's responsibility. The sample refuses to send to a row that did not opt in.
  if (payload.record.whatsapp_opt_in !== true) {
    deps.log({ ...base, outcome: "skipped", reason: "no_opt_in" });
    return reply(200, { skipped: "no_opt_in" });
  }

  const phone = payload.record.phone;
  if (typeof phone !== "string" || !E164.test(phone)) {
    deps.log({ ...base, outcome: "skipped", reason: "invalid_phone" });
    return reply(200, { skipped: "invalid_phone" });
  }

  const baseUrl = (deps.env.TN_API_BASE_URL ?? DEFAULT_BASE_URL).replace(/\/+$/, "");
  const number = encodeURIComponent(deps.env.TN_NUMBER ?? "main");
  const idempotencyKey = await sha256Hex(`${payload.table}|${recordId}|${outgoing.kind}`);

  let res: Response;
  try {
    res = await deps.fetch(`${baseUrl}/v1/numbers/${number}/messages`, {
      method: "POST",
      headers: {
        "authorization": `Bearer ${TN_API_KEY}`,
        "content-type": "application/json",
        "idempotency-key": idempotencyKey,
      },
      body: JSON.stringify({ to: { number: phone }, content: { text: outgoing.text } }),
    });
  } catch {
    deps.log({ ...base, outcome: "retryable", reason: "network" });
    return reply(502, { retryable: true });
  }

  const requestId = res.headers.get("x-request-id") ?? undefined;

  if (res.status === 202) {
    const data = await res.json().catch(() => ({}));
    deps.log({ ...base, outcome: "sent", request_id: requestId });
    return reply(200, { sent: true, message_id: data?.message_id });
  }

  // The two answers whose reason decides what to do carry it in the body, so read it once.
  const code = res.status === 429 || res.status === 402 ? await errorCode(res) : undefined;

  // The account, not the request, is the problem: an unpaid or inactive subscription, or an
  // allowance (messages, contacts) used up until the cycle turns. Repeating the call fails the same
  // way, and a quiet success would hide that every notification is being dropped. Shown loudly.
  if (res.status === 402 || (res.status === 429 && code === "messages_quota_exceeded")) {
    deps.log({
      ...base,
      outcome: "account",
      reason: code ?? `http_${res.status}`,
      request_id: requestId,
    });
    return reply(500, { error: "tn_account" });
  }

  // Worth trying again: a rate limit, a server problem, or the number is not connected yet.
  if (res.status === 429 || res.status >= 500 || res.status === 409) {
    deps.log({
      ...base,
      outcome: "retryable",
      reason: `http_${res.status}`,
      request_id: requestId,
    });
    return reply(502, { retryable: true });
  }

  // Wrong or missing key: an operator problem, shown loudly.
  if (res.status === 401 || res.status === 403) {
    deps.log({
      ...base,
      outcome: "credentials",
      reason: `http_${res.status}`,
      request_id: requestId,
    });
    return reply(500, { error: "tn_credentials" });
  }

  // Any other 4xx cannot be fixed by repeating the call.
  const failed = await errorCode(res) ?? `http_${res.status}`;
  deps.log({ ...base, outcome: "failed", reason: failed, request_id: requestId });
  return reply(200, { failed });
}

function reply(status: number, body: Record<string, unknown>): Response {
  return Response.json(body, { status });
}

async function errorCode(res: Response): Promise<string | undefined> {
  try {
    const data = await res.json();
    const code = data?.error?.code ?? data?.error;
    return typeof code === "string" && code.length > 0 ? code : undefined;
  } catch {
    return undefined;
  }
}

async function sha256(text: string): Promise<Uint8Array> {
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(text));
  return new Uint8Array(digest);
}

async function sha256Hex(text: string): Promise<string> {
  const bytes = await sha256(text);
  return Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("");
}

/** Compares two secrets in constant time by comparing their digests, which have equal length. */
async function sameSecret(given: string, expected: string): Promise<boolean> {
  const [a, b] = await Promise.all([sha256(given), sha256(expected)]);
  let diff = 0;
  for (let i = 0; i < a.length; i++) diff |= a[i] ^ b[i];
  return diff === 0;
}
