# Turbo Notify Examples

Working examples of building on [Turbo Notify](https://turbonotify.com), the WhatsApp
infrastructure for transactional notifications and AI assistants.

Each example is a **self-contained project**: clone it, set a few environment variables, and run
it. Nothing here depends on anything else in this repository, and nothing here needs access to
Turbo Notify's internals. Every example talks to the same public API and the same MCP server your
own code would.

---

## Examples

| Example | What it shows |
|---|---|
| [**agno-support-agent**](./agno-support-agent) | A WhatsApp support attendant that answers questions about Turbo Notify, built with [Agno](https://agno.com) and driven entirely through the [Turbo Notify MCP Server](https://docs.turbonotify.com/general/mcp-server/). Receives inbound messages by webhook, decides with an LLM, and replies through an MCP tool. |

More are coming. If you build something worth showing, open a pull request.

---

## What these examples assume

- A Turbo Notify account with at least one connected WhatsApp number.
- An API key from the dashboard, under **API Keys**.
- Whatever the individual example lists in its own README.

They do **not** assume you have read the whole API reference first. That is the point: each one is
meant to be readable start to finish in a few minutes.

## A note on the MCP server

Several examples reach Turbo Notify through the **Model Context Protocol** rather than by calling
REST endpoints directly. That is not a different API: it is the same public API, presented as
tools an LLM can choose between, with schemas it can read.

If you are building an agent, that is usually what you want: you describe the goal, and the model
picks `send_text` or `reply_to_message` or `get_number` on its own. If you are building a
deterministic integration, call the REST API directly instead; both are supported and both are
documented.

## Licence

MIT. Copy anything here into your own project.
