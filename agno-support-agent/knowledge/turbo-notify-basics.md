# Turbo Notify reference for the support attendant

> This is the material the agent answers from. It is a condensed version of the
> public documentation at <https://docs.turbonotify.com>, written to be read by a
> model rather than by a person. Keep it short: everything here costs context on
> every single answer.
>
> **Refreshing it:** the source of truth is the public documentation site. When
> the product changes, update this file from there. It is deliberately hand-
> curated rather than scraped: a scrape brings navigation chrome, duplicated
> boilerplate and stale pages along with the substance.

## What Turbo Notify is

Infrastructure for sending and receiving WhatsApp messages from software. A
customer connects one or more real WhatsApp numbers, then sends and receives
through a REST API, through webhooks, or through the Model Context Protocol
server (which is how this attendant itself works).

It is not the WhatsApp Business Cloud API. Numbers are connected as **linked
devices**, the way WhatsApp Web is, which is why a number is "connected" or
"disconnected" rather than simply configured.

## Numbers

Every organization has one **main** number. **Extra** numbers are a paid
capability: the two entry plans allow none, so adding one there is refused. Each
is addressed by an **alias**: the literal `main`, or a name the customer chose
for an extra one.

A number's status is one of: `pending`, `pairing`, `inactive`, `connecting`,
`connected`, `disconnected`, `reconnecting`, `failed`, `revoked`, and for an
extra number also `pending_deletion`, which overrides the rest once a removal is
scheduled. Only `connected` can send.

- `disconnected` means the link to the phone still exists and the connection
  dropped. Reconnecting needs no QR code and no human.
- `revoked` means the phone unlinked the device. The main number is re-paired
  from the dashboard by a person. An extra number can be re-paired through the
  API, and either way somebody has to act on the phone.

Adding an extra number is charged immediately, before it is provisioned.
Removing one is refunded if it has sent fewer than 10 messages and it is within
72 hours of that number FIRST CONNECTING, not of the purchase. A number that was
never connected is always refundable.

## Messages

Sending is asynchronous: the API accepts the message, returns a `msg_…` id
straight away, and delivery happens after. The outcome arrives later as a
webhook or by polling the events endpoint. A plain send that goes out is
`message.sent`; a reply is `message.replied`. Delivery and reading follow as
`message.delivered` and `message.read`. There is no `message.failed`: each
failure names its own operation, so a send that could not go out is
`message.send.failed` and a reply is `message.reply.failed`.

Content types: text, image, video, audio, document, sticker, location, contact
card, calendar event, and an interactive message with one link button. Reply
buttons and list pickers are **not** available on this kind of connection and
will not become available.

Audio is sent as an audio file, never as a voice note. There is no parameter
that changes this, and `mime_type` does not: it only tells the recipient's phone
how to present the file. If someone asks for a voice message, say it is not
available rather than suggesting a MIME type.

Media is sent by URL, and the URL has to be reachable when the send happens:
Turbo Notify fetches the bytes from it. A copy is kept in the customer's own
object storage when that is configured, which is what makes the media readable
back later; it is not required in order to send.

Writing `@<number>` in message text mentions that person, e.g.
`@5511999999999`. It is detected automatically.

## Contacts and groups

A contact belongs to a **number**, not to the organization: the same person
reached from the main number and from an extra number is two contacts with two
different `ct_…` ids. An id from one inbox does not resolve in another.

Contacts appear only through real traffic. Turbo Notify never reads the phone's
address book, so a person the organization has not messaged simply is not there.

Groups are read-only through the API: there is no endpoint that creates a group,
renames it, or manages its members. Sending *to* a group works, and so does the
typing indicator in one; both require a plan that includes group messaging.
Receiving, listing, editing and deleting in a group work on every plan.
Reacting needs a plan that includes reactions, in a group or anywhere else.

## Quotas and billing

Two quotas, both **per number** and both anchored to the organization's billing
cycle rather than a rolling window:

- **Messages**: consumed only by messages sent through Turbo Notify. Messages
  someone sends from the phone itself do not count.
- **Contacts**: a contact counts once per cycle the first time that number
  messages them in that cycle, whether they are new or returning.

An organization with three numbers has three separate allowances of each. Both
reset at the cycle end, which is a real date the customer can plan around.

Plans: Gratuito, Lobo Solitário, Empresarial, and a Personalizado tier arranged
directly. Prices and limits are on the pricing page; do not quote figures from
memory, because they change.

## Webhooks and events

Turbo Notify delivers events to a URL the customer registers. Every delivery is
signed: `X-Webhook-Signature` is `sha256=<hex>`, an HMAC-SHA256 of
`{X-Webhook-Timestamp}.{raw_body}` with the customer's secret. Deliveries older
than about five minutes should be rejected.

Events that belong to one of the customer's numbers carry a `number_alias`
saying which inbox it is. Consumers need it, because message and contact ids are
unique but not self-addressing. Three events are about the account as a whole and
carry no `number_alias` at all: `storage.degraded`, `storage.recovered` and
`media.unavailable`. Read the field as optional.

Customers who cannot host a public endpoint can poll the events endpoint
instead. Both channels carry byte-identical event data.

## MCP server, for AI agents

Yes, Turbo Notify has one. It is how this attendant itself works, so if someone
asks whether Turbo Notify supports MCP, the answer is yes and the honest extra
detail is that they are talking to it.

The server exposes the public API as 31 ready-made tools an agent can call:
sending (text, media, location, contact card, calendar event, CTA button),
acting on a message (reply, edit, delete, react, typing indicator), reading
messages, contacts and groups, listing and connecting numbers, and checking
usage and quotas. The agent picks between them; the customer writes no HTTP
client.

It is hosted by Turbo Notify, over Streamable HTTP at
`https://mcp.turbonotify.com/mcp`, with the API key in the `Authorization`
header of every call. One address serves everybody, and the key is what says
whose account it is, so there is nothing to install and nothing to run: any MCP
host that accepts a remote server points at that URL.

A key created without choosing scopes reaches all 31 tools. A scoped key needs
the scope of each family it uses, and two of those surprise people: replying,
editing, deleting and reacting all count as sending (`messages:send`), and
refreshing a contact spends a real WhatsApp lookup (`contacts:write`).

What the server deliberately does not offer: adding, renaming, removing or
linking a number. Those either change what the account is billed or need a
person holding the phone, so they stay in the dashboard.

## Where to send someone

- Documentation: <https://docs.turbonotify.com>
- Dashboard: <https://app.turbonotify.com>
- Webhook Inspector, for testing deliveries: <https://webhook.turbonotify.com>
- MCP server reference: <https://docs.turbonotify.com/general/mcp-server/>
