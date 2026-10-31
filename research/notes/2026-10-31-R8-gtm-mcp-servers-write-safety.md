# R8 · GTM MCP servers: who ships one, and what an agent can safely write (2026-10-31)

## Question
Which CRM / ops / orchestration tools in the outbound stack (HubSpot, Clay, Smartlead,
Instantly, Attio, Pipedrive, Airtable, n8n) ship a first-party MCP server, and what stops an
agent driving them from writing duplicates, losing failed rows or being reachable by anyone?
(Sourcing tools — Apollo, Apify, Firecrawl, Prospeo — were covered in the
[R1 note](2026-10-22-R1-sourcing-mcp-servers.md); sequencers and LinkedIn in R4 / R5.)

## Findings

**1. First-party servers in the official MCP registry (queried 2026-10-31,
`registry.modelcontextprotocol.io/v0/servers?search=…`).**

| Tool | First-party entry? | What the registry shows |
|---|---|---|
| Airtable | yes — `com.airtable/mcp` 0.1.0, remote `https://mcp.airtable.com/mcp` | plus community servers (`airtable-mcp-server`, gateways) |
| HubSpot | **no** registry entry under a HubSpot namespace | community / gateway servers only (`@mindstone/mcp-server-hubspot`, `hubspot-auditor`, `Servosity/hubspot-mcp`, hosted gateways) |
| Attio | no | community `kesslerio/attio-mcp-server` (npm `attio-mcp`) and Smithery hosts |
| Pipedrive | no | community (`@ckalima/pipedrive-mcp-server`, `mmedum/pipedrive-mcp`, `Servosity`) |
| Clay | no | only unrelated "clay*" names and a third-party `mcp-clay-to-instantly-smartlead-push` |
| Smartlead / Instantly | no | the same third-party push server; nothing from the vendors |
| Lemlist | nothing returned for `lemlist` | (R4: the official CLI config names `app.lemlist.com/mcp`) |
| n8n | no vendor entry | many community "manage n8n" servers; n8n's own MCP support is in the product (point 3) |

The registry is not the whole picture: HubSpot publishes `@hubspot/mcp-server` on npm and,
per third-party write-ups, a remote server used by Claude's connector (search summary, not
opened). A web search says Clay was added to Claude's connector directory in February 2026
(search summary of a directory blog, not opened, unconfirmed). So "not in the registry" ≠
"no official server" — but it does mean there is no machine-readable, namespace-verified entry
to pin a version against.

**2. HubSpot's npm server (`@hubspot/mcp-server` 0.4.0, published 2025-06-18, beta under the
Early Adopter terms; package downloaded and read, not run).**
- Auth: one private-app token in `PRIVATE_APP_ACCESS_TOKEN`; the README says "Consider
  starting with read-only scopes". Whatever scopes the token has, the agent has.
- 21 tools. Read: list / search / batch-read objects, schemas, properties, associations,
  engagements, workflows, owner details. Write: `hubspot-batch-create-objects`,
  `hubspot-batch-update-objects` (optional `idProperty`), `hubspot-batch-create-associations`,
  `hubspot-create-engagement` / `update-engagement`, `hubspot-create-property` /
  `update-property`. No delete and no merge tool.
- **No upsert.** Writes are `/crm/v3/objects/{type}/batch/create` (≤ 100 inputs), annotated
  `idempotentHint: false`. An agent that "adds these 40 leads" twice creates 40 duplicates;
  the tool's own guidance only says to *sample* existing objects first.
- **Failed rows are dropped from the answer.** The client throws only on `!response.ok`; a
  207 partial success is OK, and the tool returns `status` + `results[]` only — the API's
  `errors[]` array is not passed back to the agent. Combined with the R7 finding (207 +
  `errors[]` resolves as success in the SDK), the agent sees "done" for a batch where some
  rows failed.
- No retries in its HTTP client (plain `fetch`), so the server itself does not duplicate a
  create after a timeout — but the agent may simply call the tool again.
- The guardrail is text: each write tool's description starts "Only use when the user has
  explicitly requested to update their CRM". Nothing in the server enforces it.
- Remote server (search summaries only, not opened): third-party posts report a blocking
  confirmation per CRM write showing object, property, old and new value; 21 → 28 tools
  depending on granted scopes; create/update only, no delete / merge. Treat as unverified.

**3. n8n as an MCP server and client (node source on GitHub `master`, read 2026-10-31).**
- *MCP Server Trigger* (`mcpTrigger`, versions 1–2.1) turns a workflow into an MCP endpoint
  whose tools are the connected tool nodes / sub-workflows. **Authentication defaults to
  `none`**; options are Bearer, Header, and from v2 "n8n User Auth (OAuth2)" with an optional
  "Require Workflow Execute Permission". The node's builder hint even tells n8n's AI builder
  to keep `none` unless the user asks for auth, because "n8n exposes inbound trigger URLs
  publicly by design". An unauthenticated MCP trigger in front of a "push to sequencer" or
  "create CRM contact" tool is an open, unmetered outbound button.
- *MCP Client Tool* (`mcpClientTool`) gives an n8n AI Agent the tools of a remote MCP server
  (SSE or HTTP Streamable, Bearer / Header / OAuth2). **Tools to include defaults to `All`**;
  `Selected` (allowlist) and `All Except` exist. Pointed at the HubSpot server with defaults,
  the agent gets `batch-create-objects` and `create-property` alongside the reads.

**4. What the protocol promises (MCP spec 2025-06-18, `server/tools`).** Clients "SHOULD"
keep a human in the loop who can deny tool calls and show confirmation prompts for
sensitive operations, and clients "MUST consider tool annotations to be untrusted unless they
come from trusted servers". So `readOnlyHint` / `idempotentHint` are labels, not controls: the
safety has to sit in the token scope, the client's allowlist and the tool implementation.

## Apply
- **n8n MCP Server Trigger**: set Authentication to Bearer / Header (or n8n OAuth2 on v2+)
  before the workflow is ever activated, and keep outbound tools (sequencer push, CRM create)
  out of agent-facing MCP workflows or behind a review sheet. Audit existing workflows:
  search for `mcpTrigger` nodes with `authentication: none`.
- **n8n MCP Client Tool / Claude connectors**: switch Tools to Include to `Selected` and list
  read tools only (search, batch-read, list). Writes go through your normal n8n HTTP node with
  upsert, not through the agent.
- **HubSpot via MCP**: give the private app read-only CRM scopes for research / prep agents.
  If an agent must write, do it through your own upsert step (batch upsert on `email`, R7) and
  have the agent hand over rows — the MCP tool only creates, and does not report failed rows.
- **Before trusting "tool is read-only"**: open the server's tool list (or package source)
  and check what actually writes; annotations are hints by spec.
- Measure: duplicates created per agent session (HubSpot contacts with the same email
  created in the same hour) and the count of MCP endpoints without auth.

## Lab impact
Project 13 (GTM MCP server) gets concrete design rules: write tools are upserts keyed by a
person key and return per-row `created / updated / failed` (never drop failed rows); the
HTTP transport refuses to start without auth; effect class is enforced server-side (a
`read`-only mode that does not register write tools), not just declared as annotations;
an n8n MCP Server Trigger example ships with Bearer auth. ROADMAP 13 item edited.

## Sources
- https://registry.modelcontextprotocol.io/v0/servers?search=hubspot (and `clay`, `smartlead`, `instantly`, `attio`, `pipedrive`, `n8n`, `airtable`, `lemlist`, `salesforce`) — opened 2026-10-31
- https://registry.npmjs.org/@hubspot/mcp-server — opened 2026-10-31 (metadata + README)
- https://registry.npmjs.org/@hubspot/mcp-server/-/mcp-server-0.4.0.tgz — downloaded and read 2026-10-31, not executed
- https://raw.githubusercontent.com/n8n-io/n8n/master/packages/@n8n/nodes-langchain/nodes/mcp/McpTrigger/McpTrigger.node.ts — opened 2026-10-31
- https://raw.githubusercontent.com/n8n-io/n8n/master/packages/@n8n/nodes-langchain/nodes/mcp/McpClientTool/McpClientTool.node.ts — opened 2026-10-31
- https://raw.githubusercontent.com/modelcontextprotocol/modelcontextprotocol/main/docs/specification/2025-06-18/server/tools.mdx — opened 2026-10-31
- https://tldv.io/blog/hubspot-mcp/ — search summary, not opened (ENOTFOUND from the run environment)
- https://sanka.com/docs/resources/hubspot-mcp-capabilities/ — search summary, not opened
- https://www.dirjournal.com/blogs/claude-connectors-list — search summary, not opened (Clay in Claude's connector directory, Feb 2026, unconfirmed)
- developers.hubspot.com, docs.clay.com, clay.com, developer.airtable.com, modelcontextprotocol.io — unreachable from the run environment (curl 000 / ENOTFOUND)
