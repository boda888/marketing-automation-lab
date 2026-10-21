# Research: how real teams automate outbound end to end

The lab's code answers "how would I build it"; this folder answers "what do practitioners
actually run, with which tools, at what cost, and what breaks". The daily agent researches
one track per morning run ([`ROADMAP.md`](ROADMAP.md)) and turns findings into:

- a dated note in [`notes/`](notes/) (template: [`notes/_TEMPLATE.md`](notes/_TEMPLATE.md));
- rows in the tool catalogue [`STACK.md`](STACK.md) (price, API / webhooks / MCP / n8n node);
- short, actionable entries in [`INSIGHTS.md`](INSIGHTS.md) — things the owner can apply in
  a Clay table, an n8n flow or a sequencer setting this week;
- roadmap items for the lab when a finding changes what the code should do.

Channels covered: data & signals (Clay, Apollo, Apify, Firecrawl, Airtable), email
(Smartlead, Instantly, Lemlist, mailbox infra), LinkedIn (HeyReach, Aimfox), calls (dialers,
AI voice agents), CRM & reporting (HubSpot, Attio, Twenty), and AI agents / MCP over the stack.

## Honesty rules
- Cite only pages that were actually opened. Web-search summaries may be used but are
  labelled `search summary, not opened` and never presented as verified.
- Prices and limits always carry the date and where they came from; they go stale fast.
- Screenshots only of public pages the agent opened itself, saved under `assets/` as
  `<date>-<slug>.png` (≤ 300 KB) with the URL and date in the note. No client data, no
  logged-in screens, no screenshots of paid course material.
- Vendor marketing claims (reply rates, "10x") are quoted as claims, not facts.
