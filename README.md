# Turbo Notify Examples

> **Em português:** exemplos prontos de uso do [Turbo Notify](https://turbonotify.com), o WhatsApp
> para o seu sistema, os seus agentes de IA e os seus clientes. Cada exemplo é um projeto
> independente: clone, preencha as variáveis de ambiente e rode. O primeiro,
> [`agno-support-agent`](./agno-support-agent), é um atendente de WhatsApp feito com Agno que
> responde pelo servidor MCP do Turbo Notify. Para montar o seu próprio agente passo a passo, veja
> o [guia de agentes de IA](https://docs.turbonotify.com/guides/ai-agent/). O restante deste repositório está em inglês.

Working examples of building on [Turbo Notify](https://turbonotify.com): WhatsApp for your
software, your AI agents and your customers.

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

- A Turbo Notify account with at least one connected WhatsApp number (in the dashboard, under
  **Numbers**, or **Números** when the dashboard is in Portuguese).
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

Building an agent of your own? Start with the [AI agent guide](https://docs.turbonotify.com/en/guides/ai-agent/) in the documentation: it
walks through the same pattern these examples use (webhook in, MCP out) and how to keep a message
from a stranger from steering your agent.

## Licence

MIT. Copy anything here into your own project.
