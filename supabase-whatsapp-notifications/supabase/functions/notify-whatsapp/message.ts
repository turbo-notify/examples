// The one file you edit to change what is sent and when.
//
// `messageFor` looks at a Database Webhook payload and answers with the message to send, or `null`
// when the event is not one you want a WhatsApp message for. The sample handles an `orders` table:
// a new row says "order received", and a row whose `status` changes to `shipped` says "order
// shipped". Replace the table, the columns and the texts with your own.

/** The shape Supabase sends for a Database Webhook (INSERT, UPDATE and DELETE). */
export interface WebhookPayload {
  type: "INSERT" | "UPDATE" | "DELETE";
  table: string;
  schema: string;
  record: Record<string, unknown> | null;
  old_record: Record<string, unknown> | null;
}

export interface Outgoing {
  /** Names the business event. Together with the table and the row id it makes the idempotency key. */
  kind: "order_received" | "order_shipped";
  text: string;
}

export function messageFor(payload: WebhookPayload): Outgoing | null {
  if (payload.table !== "orders" || payload.record === null) return null;
  const id = String(payload.record.id);

  if (payload.type === "INSERT") {
    return {
      kind: "order_received",
      text: `Order ${id} received. We will tell you when it ships.`,
    };
  }

  if (
    payload.type === "UPDATE" &&
    payload.record.status === "shipped" &&
    payload.old_record?.status !== "shipped"
  ) {
    return { kind: "order_shipped", text: `Order ${id} shipped.` };
  }

  return null;
}
