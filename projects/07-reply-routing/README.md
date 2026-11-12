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

## Architecture

```
InboundReply(channel, body_text, subject, headers, from, person_email, person_linkedin_url,
             vendor_label)          one deduped event (intake / dedupe: next steps)
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
- No intake yet: per-vendor event keys, secret / path-token checks, stale-event cut-off and
  dedupe (ROADMAP 07 third item) come next; so do the LLM stage, FastAPI and the n8n flow.
- Nothing here talks to a live sequencer, CRM or LLM.
