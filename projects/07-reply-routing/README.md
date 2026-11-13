# 07 · Reply intent & routing

**The pain:** replies are triaged by hand, so hot leads go cold; an out-of-office reply stops
a sequence for good (or is ignored and the follow-ups land while the person is away); a
"remove me" on LinkedIn never reaches the email campaign; a hard bounce keeps being mailed;
and "Mara has left, please contact Jon" gets read as an OOO. The sequencer's own label is
not proof either — Instantly's interest code for Out of Office is `0`, so a truthy check in
n8n reads it as "no status" (research R4, 2026-10-25) — and native HeyReach ↔ Smartlead /
Instantly sync does not stop the other channel on a reply (R5, 2026-11-06).

v0.0.1 (started 2026-11-12): the **deterministic** stage and the routing plan. The classes
with suppression or legal consequences are decided by rules in code, never by a model;
everything else is handed to an LLM stage (next step) with `needs_llm=True`.
v0.0.2 (2026-11-13): the **webhook intake** in front of it — Smartlead, Instantly and
lemlist events are authenticated, keyed, age-checked and deduped before anything is
classified, and every event leaves with an outcome and a reason.

## Intake (`intake()` → `handle()`)

Sequencers replay failed deliveries (Smartlead `retrigger-failed-events`, Instantly after
`/resume`), lemlist fires per occurrence, Smartlead and Instantly sign nothing, and a wrong
event-name string in an n8n Switch sends every reply to the fallback branch without a sound
(research R4, 2026-10-25 and 2026-11-05). So, in this order:

| step | rule | outcome when it fails |
|---|---|---|
| auth | lemlist: `secret` echoed in the body, `hmac.compare_digest`, then **stripped** from the payload; Smartlead / Instantly: per-vendor random path token. Nothing configured = refuse | `rejected` |
| event type | both Smartlead spellings (`EMAIL_REPLY` / `EMAIL_REPLIED`), all five lemlist `*Unsubscribed` types; only names seen in an opened package are mapped | unknown → `alert` |
| key | lemlist `_id`; Smartlead `campaign + sl_email_lead_id + kind + timestamp`; Instantly `campaign + lead e-mail (lower) + kind + timestamp` — the kind, not the raw name, so a replay under the other spelling still matches; timestamps normalised to UTC | `invalid` |
| age | older than 24 h (configurable) or > 5 min in the future | `stale` → review, **not dropped**: a replay after an outage is legitimately old |
| dedupe | `SeenEvents.add(key)` first sighting wins: `MemorySeenEvents` (bounded) or `SqliteSeenEvents` (unique key, `INSERT OR IGNORE`, shared by every process on the file); `release(key)` when downstream failed so the replay is processed | `duplicate` |
| result | reply with text → `accepted`; reply without text → `needs_body` (fetch from the inbox API, never classify an empty body); vendor unsubscribe → class `unsubscribe`; vendor bounce → `bounce` **unknown** (the vendor does not say permanent); interest / category labels → `label_only`; lemlist mailbox-health (`sendLimitReached`, `connectionIssue`, …) → `alert`; sends / opens / clicks → `ignored`; WhatsApp / SMS replies → `alert` (no channel here) | |

**LinkedIn (`linkedin_intake()`, v0.0.3).** HeyReach and Aimfox payload fields are not in
any package opened (R5 2026-10-26 / 2026-11-06, rechecked 2026-11-13), so the n8n flow maps
each delivery with a Set node into an envelope — `event_type`, `profile_url`, `message_text`,
`message_at` (the message's time, not the delivery's), `campaign_id` — and the lab works on
that. Neither vendor signs deliveries: a token is required in the URL path or, for HeyReach,
in a custom header set on the webhook (`customHeaders` on `CreateWebhook`, documented only
in a third-party plugin's schema — read the webhook back with `includeCustomHeaders` to
confirm). The reply key is **content-based** — vendor + normalised `/in/` profile URL +
whitespace/case-folded text hash + message time, without the event type — so HeyReach's
`MESSAGE_REPLY_RECEIVED` and `EVERY_MESSAGE_REPLY_RECEIVED` (or Aimfox's `reply` /
`new_reply` / `campaign_reply` / `inmail_reply`) for one message are processed once. Aimfox
`account_logged_out` → `alert` (once per sender and minute): a logged-out sender stops the
sequence silently. Sales Navigator / company URLs are never turned into a profile key.

`handle(result)` runs classify → route for `accepted` events (and `needs_body` once the text
is passed in) and returns None for everything else, so the caller branches on `outcome`.

## Architecture

```
vendor webhook ─► intake()  auth · event type · key · age · dedupe   (see Intake)
   │
InboundReply(channel, body_text, subject, headers, from, person_email, person_linkedin_url,
             vendor_label)          one deduped event
   │
own_text()            quoted thread ("On … wrote:", "-----Original Message-----", "From:/Sent:"),
                      '>' lines and "-- " signature removed: our own "reply unsubscribe"
                      footer in the quote never opts anyone out
   │
classify()            fixed order, first match wins
   │ 1 bounce          DSN: multipart/report; report-type=delivery-status, mailer-daemon /
   │                   postmaster sender or DSN subject; Status 5.x.x hard, 4.x.x soft,
   │                   none readable → unknown (never promoted to hard)
   │ 2 left company    "is no longer with", "this mailbox is no longer monitored" → referral
   │                   (another address in the text) or wrong_person — before OOO, because
   │                   these arrive as auto-replies
   │ 3 out_of_office   auto-reply signal (Auto-Submitted ≠ no, X-Autoreply / X-Autorespond,
   │                   Precedence: auto_reply, Exchange X-Auto-Response-Suppress *with*
   │                   X-MS-Exchange-Inbox-Rules-Loop, OOO subject) + wording, or strong
   │                   wording alone ("out of office", "on parental leave") when the message
   │                   shows no engagement (no "?", "interested", "call me" …)
   │                   → return date after a return cue, else a note why not
   │   (other auto-generated mail → no_meaningful_reply, auto_generated: the sequence goes on)
   │ 4 unsubscribe     opt-out wording in the person's own text ("remove me", "STOP" alone …)
   │ 5 wrong_person / referral
   │ 6 no_meaningful_reply   nothing left after quote, signature, "thanks", "sent from my …"
   │ 7 unclassified + needs_llm   intent (interested / question / objection / not interested)
   ▼
Classification(reply_class, rule, evidence, bounce_kind/status, return_date(+note),
               referral_email, vendor_label, vendor_label_disagrees)
   │
route(reply, classification, RoutingPolicy)     plans, executed by 06 / 08
   ▼
RoutingDecision(actions, warnings)
```

| class | actions (per channel key: email address, LinkedIn profile URL) |
|---|---|
| bounce hard | stop + suppress + mark_email_invalid — **email only**, LinkedIn untouched |
| bounce soft | record_soft_bounce (for 02's bounce guard) |
| bounce unknown | pause email for the default window + notify a human |
| unsubscribe | stop + suppress on **both** channels, CRM opted_out |
| out_of_office | pause **both** channels until return + 1 day; no date / past date → 7 days; capped at 60 days + notify |
| wrong_person / referral | stop both, CRM update; referral → `referral_task` (enrich + decide — the new person is never enrolled automatically) |
| no_meaningful_reply | auto-generated → nothing; a human "thanks" → stop both + notify |
| unclassified | stop both + classify_with_llm |
| interested / question (LLM stage) | stop both + notify (high) + CRM |
| objection / not_interested (LLM stage) | stop both + CRM |

A missing key for a channel is a **warning** in the decision ("stop_sequences on linkedin
not planned: no linkedin key for the person (other channel)"), never a silent skip.

## Return dates

`extract_return_date(text, received)` reads ISO (`2026-12-04`), `24.11.2026`, `24/11`,
`23 November`, `November 30th, 2026`, `the 3rd of December`, optional weekday. Rules:
the date must follow a return cue within 40 characters in the same sentence; "back /
returning / available" outranks "until / till / through"; "from" is not a cue ("away from
10 to 24 Nov" names the start); a date without a year is the next occurrence on or after the
day the reply arrived; dotted numbers need a year (`9.30` is a time); `03/04` is reported as
`ambiguous`, never guessed.

## Run it

```bash
cd projects/07-reply-routing
python -m pip install -e ".[dev]"
python -m pytest -q
```

```python
from datetime import UTC, datetime
from replyrouting import Channel, InboundReply, classify, route

r = InboundReply(
    channel=Channel.LINKEDIN,
    received_at=datetime.now(UTC),
    body_text="Please stop messaging me.",
    person_email="mara.olsen@northwind-freight.example",
    person_linkedin_url="https://www.linkedin.com/in/mara-olsen-example",
)
decision = route(r, classify(r))  # stop + suppress on LinkedIn *and* email
```

## Limits (honest)

- English wording only (plus a few common non-English OOO subject prefixes); rules are
  heuristics written from knowledge plus Discourse's and Zammad's open-source auto-reply
  checks — not tuned on a labelled reply set yet (eval set is a next step).
- RFC 3834 (Auto-Submitted) and RFC 3464 (DSN) pages were unreachable from the run
  environment (rfc-editor.org DNS-blocked, 2026-11-12); the header handling follows those
  two codebases, not the RFC text.
- Most sequencer webhooks carry the reply body without mail headers, so header rules only
  help when the intake has the raw message (IMAP / Gmail API / unified inbox).
- Intake payload fields come from integration packages (Activepieces Smartlead 0.2.0 / 0.2.1,
  lemlist 0.2.2, Instantly 0.2.3; `instantly-cli`), not vendor docs — vendor doc sites were
  unreachable from the run environment (proxy 403, 2026-11-05 and 2026-11-13). No opened
  sample shows the **reply text** field for any vendor or Instantly's lead e-mail field; they
  are looked up from candidate lists in `intake.FIELDS`. Send one real event per vendor and
  check them; until then expect `needs_body`.
- LinkedIn intake trusts the n8n mapping into the envelope; HeyReach's "first" vs "every"
  reply semantics come from event labels, not docs. Not yet built: the LLM stage, FastAPI
  and the n8n flow.
- Nothing here talks to a live sequencer, CRM or LLM.
