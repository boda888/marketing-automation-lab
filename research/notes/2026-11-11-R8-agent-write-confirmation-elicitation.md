# R8 · Confirming agent writes: elicitation forms vs a flag the model can set (2026-11-11)

## Question
When an AI agent drives a GTM tool over MCP (deploy, CRM write, sequencer push), how does a
server actually make a human confirm the write — and does that confirmation survive when the
agent runs inside n8n instead of Claude? Side question from the open R8 items: which
"Clay MCP" packages are actually Clay (clay.com), and what is known about HubSpot's remote
CRM MCP server while its docs stay unreachable from the run environment?

## Findings

**1. The protocol mechanism: elicitation (MCP spec `2025-11-25`, `client/elicitation`).**
- A server can pause a tool call and ask the *client* to show the user a form
  (`elicitation/create`, `mode: "form"`, flat JSON schema) or send them to a URL
  (`mode: "url"`, new in `2025-11-25`, for credentials / OAuth / payments that must not pass
  through the client).
- Answers are three-way: `accept` (user submitted), `decline` (user said no), `cancel`
  (dismissed, closed, failed to load). Clients MUST offer decline and cancel; servers MUST NOT
  send a mode the client did not declare.
- It only works if the client **declares the capability** at initialise
  (`capabilities.elicitation`, `{}` inside it = form only). A client that does not declare it
  never sees the form.
- Servers MUST NOT use form mode for secrets; elicitation state MUST be tied to the user's
  identity (e.g. OAuth `sub`), not to a session id alone.

**2. A vendor implementation: HubSpot's developer MCP server (`@hubspot/cli` 8.18.0,
published 2026-10-27, `mcp-server/` read, not run).** This is the `hs mcp` server for building
HubSpot apps and CMS assets — not the CRM data server — but it is the only HubSpot MCP
source reachable from this environment, and it shows how HubSpot gates writes:
- `deploy-project` and `upload-project` call `confirmProductionTargets`: if the target
  account is classed `production-with-care` (not a test account / sandbox) and the client can
  elicit, the server sends a form "*Deploy … to production account …?*" and runs only on
  `accept`. A failed or timed-out elicitation (5 min, `MCP_ELICITATION_TIMEOUT`) **counts as a
  decline**; on decline the tool returns "do not ask again, do not call again unless the user
  asks".
- If the client **cannot** elicit, the server falls back to text: it refuses once and tells
  the model to ask the user and call again with `confirmProductionAccount: true`. The
  parameter description says "NEVER set this on your own" — but that is an instruction to the
  model, not a control. The server ignores the flag only when a form is possible.
- `deploy-project` without `buildNumber` deploys nothing and lists recent builds — the
  "missing argument = show the plan" pattern.
- Write tools are annotated `readOnlyHint: false, idempotentHint: false`; reads `true`.
  Annotations stay hints (R8 note 2026-10-31).

**3. n8n does not show elicitation forms to anyone (n8n `master`, read 2026-11-11).**
- *MCP Client Tool* / *MCP Client*: `connectMcpClient` builds the SDK client with
  `{ capabilities: {} }` — no `elicitation` declared. So a server like HubSpot's above takes
  the text fallback, and in an unattended n8n AI Agent there is no user in the loop: the model
  can simply call again with the confirm flag set. **A confirmation that is safe in Claude
  becomes a flag the model sets in n8n.**
- *MCP Server Trigger* (`McpServer.ts`) uses URL-mode elicitation for one thing only: when a
  caller has not connected a required credential, it sends the connection URL (300 s timeout)
  if the client declared `elicitation.url`, else returns the URLs as text. It does **not**
  ask callers to confirm writes; the workflow's tools run as soon as they are called.

**4. "Clay MCP" on npm is not Clay.com.** `@clayhq/clay-mcp` 1.0.4 (npm, 2025-09-10) is the
MCP server of **clay.earth**, a personal-network CRM (contact search, notes, groups, "add new
contact"; `CLAY_API_KEY` from `web.clay.earth`). It has nothing to do with Clay.com tables or
enrichment credits. The MCP registry (queried again 2026-11-11, `search=clay`) still has no
Clay.com entry; the closest is a third-party "clay-to-instantly-smartlead-push" server.
Clay.com's own connector: per search summaries only (university.clay.com, reply.io,
scalekit.com all ENOTFOUND from this run) it is the hosted connector in Claude's directory,
its OAuth flow rejects `claude mcp add` CLI connections, it exposes Clay search, providers
and the workspace's admin-enabled Functions rather than raw tables, and a Function run from
the agent costs the same credits as in a table. Unconfirmed — no per-call credit cap found.

**5. HubSpot remote CRM MCP server — still search summaries only.** developers.hubspot.com,
mcp.hubspot.com and api.hubapi.com are blocked from this environment (ENOTFOUND /
proxy refusal). Summaries of HubSpot's "integrate with the remote HubSpot MCP server" page
say: `https://mcp.hubspot.com`, OAuth 2.1 + PKCE via an MCP auth app created in the
portal, GA 2026-04-13, scopes derived from the server's tools at install time (new tools need
a re-install), activity / conversation data blocked when Sensitive Data is on. Third-party
pages disagree on whether it writes at all. `@hubspot/mcp-server` on npm is still 0.4.0
(2025-06-18), and the registry still has no HubSpot-namespace entry (2026-11-11).

## Apply
- **n8n AI Agent with MCP tools that write** (CRM, sequencer, deploy): do not rely on the
  server's "confirm" step — n8n never shows it, and the fallback is a boolean the model can
  set. Keep write tools out of "Tools to Include" (Selected = reads only), or put a human
  step in n8n itself: the agent writes a *plan* row (Sheet / Airtable / Slack message with
  Approve button), and a separate workflow executes approved rows through your own upsert node.
- **Claude Desktop / Claude Code with vendor MCP servers**: a server that uses elicitation
  (HubSpot's dev server) shows a real form there — read it, it names the account. Test once
  with a sandbox / test portal so you know which accounts are classed as production.
- **Before installing a "Clay MCP"**, check the publisher: `@clayhq/clay-mcp` and Smithery's
  `@clay-inc/clay-mcp` are clay.earth. For Clay.com use the hosted connector from Claude's
  directory, and enable only the Functions you want the agent to spend credits on.
- Measure: writes executed by agents without a human approval row (should be 0), and agent
  sessions that hit the confirm fallback text (grep tool results for "confirm" in n8n
  executions).

## Lab impact
Project 13 (GTM MCP server): non-read tools confirm via **form elicitation when the client
declares it**, and treat decline / cancel / timeout / error as "not run"; when the client
cannot elicit, `outbound` and `spends_credits` tools return a plan with a plan id and refuse
to execute on a model-settable flag — execution needs an approval recorded outside the MCP
session (e.g. a signed approval from a review step). Missing target arguments return the plan,
never a default target. ROADMAP 13 item edited.

## Sources
- https://raw.githubusercontent.com/modelcontextprotocol/modelcontextprotocol/main/docs/specification/2025-11-25/client/elicitation.mdx — opened 2026-11-11
- https://raw.githubusercontent.com/modelcontextprotocol/modelcontextprotocol/main/docs/specification/2025-06-18/client/elicitation.mdx — opened 2026-11-11 (earlier, form-only version)
- https://registry.npmjs.org/@hubspot/cli/-/cli-8.18.0.tgz — downloaded and read 2026-11-11 (`mcp-server/tools/project/productionConfirmation.js`, `DeployProjectTool.js`, `constants.js`, `mcp-server/utils/elicitation.js`, `lib/constants.js`), not executed
- https://registry.npmjs.org/@hubspot/mcp-server — opened 2026-11-11 (latest still 0.4.0)
- https://registry.npmjs.org/@clayhq/clay-mcp — opened 2026-11-11 (README: clay.earth)
- https://registry.npmjs.org/-/v1/search?text=clay%20mcp — opened 2026-11-11
- https://registry.modelcontextprotocol.io/v0/servers?search=hubspot and `?search=clay` — opened 2026-11-11
- https://raw.githubusercontent.com/n8n-io/n8n/master/packages/@n8n/nodes-langchain/nodes/mcp/shared/utils.ts — opened 2026-11-11 (`connectMcpClient`, `capabilities: {}`)
- https://raw.githubusercontent.com/n8n-io/n8n/master/packages/@n8n/nodes-langchain/nodes/mcp/shared/runtime.ts — opened 2026-11-11
- https://raw.githubusercontent.com/n8n-io/n8n/master/packages/@n8n/nodes-langchain/nodes/mcp/McpTrigger/McpServer.ts — opened 2026-11-11 (credential-gate URL elicitation)
- https://developers.hubspot.com/docs/apps/developer-platform/build-apps/integrate-with-the-remote-hubspot-mcp-server — search summary, not opened (ENOTFOUND)
- https://university.clay.com/docs/mcp-troubleshooting-and-faqs — search summary, not opened (ENOTFOUND)
- https://reply.io/blog/clay-mcp/ and https://www.scalekit.com/blog/clay-mcp-vs-api — search summaries, not opened (ENOTFOUND)
