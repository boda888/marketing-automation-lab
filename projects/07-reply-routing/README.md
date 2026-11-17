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
v0.0.4 (2026-11-14): the **LLM intent stage** (`classify_intent()`) for what the rules left
open — schema-validated, quote-checked, and every failure ends with a person, not a guess.
v0.0.5 (2026-11-15): a **labelled eval set and scorer** (`replyrouting.evals`) for the whole
path, so a rule or prompt change is measured before it ships.
v0.0.6 (2026-11-16): a **FastAPI service** (`replyrouting.api`) that puts intake → classify
(→ LLM stage when a client is wired) → route behind webhook URLs, with a waiting room for
replies whose text has to be fetched first.
v0.1.0 (2026-11-17): an **n8n workflow** ([`n8n/intake-and-route.json`](n8n/intake-and-route.json))
that answers the sender with the service's own status code first, then routes every outcome
and every planned action, plus an offline demo of eleven synthetic deliveries that lands on
every outcome branch.

## Service (`replyrouting.api`)

```bash
python -m pip install -e ".[api]"
REPLYROUTING_API_TOKEN=... REPLYROUTING_TOKEN_SMARTLEAD=<long random> \
REPLYROUTING_LEMLIST_SECRET=... REPLYROUTING_SEEN_DB=seen.db \
  uvicorn replyrouting.api:app_from_env --factory
```

| route | auth | does |
|---|---|---|
| `POST /v1/intake/{vendor}/{token}` | path token (Smartlead / Instantly) | intake → decide; body as the vendor sent it |
| `POST /v1/intake/lemlist` | `secret` in the body | same |
| `POST /v1/linkedin/{vendor}/{token}` | path token | HeyReach / Aimfox envelope mapped by n8n |
| `POST /v1/replies` `{event_key, body_text}` | bearer | fills a `needs_body` event **once**, then decides |
| `GET /v1/replies/pending` | bearer | `needs_body` events still waiting |

The answer carries `outcome`, `reason`, the event (keys and ids only — never the payload or
the reply text), and for decided events `reply_class`, `rule`, `actions` (kind, channel,
key, until, priority), `warnings` and LLM `calls` / tokens. HTTP status is picked for the
**sender's retry logic**, the outcome for n8n's Switch:

| status | when | why |
|---|---|---|
| 401 | token / secret wrong or not configured (fail closed) | Smartlead retries a 401 but not a 400 (search summary of its help-centre page — not opened), so a delivery refused during a mis-configuration can be retriggered after the fix; no key is recorded |
| 404 / 400 / 413 | unknown vendor / body not JSON / body over 256 KB | a retry cannot fix these |
| 503 | classify / route / the LLM client **raised** after the key was recorded | the key is **released** first, so the retry is processed, not dropped as a duplicate; a failed `/v1/replies` fill stays pending |
| 200 | every other outcome — `accepted`, `needs_body`, `duplicate`, `stale`, `alert`, `invalid`, `label_only`, `ignored` | a non-2xx would only make the vendor resend an event already decided |

Without an LLM client, a reply the rules leave open is planned as `classify_with_llm`; with
one (`ReplyService(..., llm=client)`), the intent stage runs inline. Senders want a fast 2xx
(Smartlead help centre; Instantly's 30 s timeout is from a third-party page) — keep the model
timeout well below that, or answer the vendor from n8n first. `app_from_env` wires no LLM
client and refuses to start without `REPLYROUTING_API_TOKEN`.

## n8n workflow (`n8n/intake-and-route.json`)

```
Webhook reply-intake/:vendor/:token ── pass body ──┐
Webhook linkedin-intake/:vendor/:token ─ envelope ─┴─ Config ─ POST /v1/{intake|linkedin}/{vendor}/{token}
   ├─ (any status) Respond to vendor with the service's status ─ Decision ─ Switch on outcome
   │      accepted ─ one item per action ─ Switch on action kind
   │           stop / pause / suppress ─ Execute: sequencer actions ─(error)─ Alert
   │           crm_update / mark_email_invalid / record_soft_bounce ─ Execute: CRM + data ─(error)─ Alert
   │           notify_human / referral_task / classify_with_llm ─ Execute: notify a human
   │      needs_body ─ Execute: log key (inbox fetch) ··· Webhook reply-text ─ POST /v1/replies ─ back into the Switch
   │      stale / invalid ─ review queue      alert / rejected / error / unknown ─ Alert
   │      duplicate / ignored / label_only ─ done
   └─ (service unreachable) Respond 503 ─ Alert
```

- **Answer first, act after.** The Intake call uses full response + never-error, so the
  vendor gets exactly the status the service chose (401 → Smartlead can retrigger after a
  token fix, 503 → resend after a crash, 200 for anything decided); a connection error
  answers 503 because nothing was recorded. No n8n Retry On Fail anywhere: the vendor's
  retry plus the dedupe store is the retry.
- Both Switch nodes use `fallbackOutput: extra` (Switch V3 drops unmatched items otherwise),
  so an outcome or action kind added to the service later reaches the alert, not the void.
- The **LinkedIn envelope** Set node maps HeyReach / Aimfox fields from candidate names
  (`lead.profile_url`, `message.text`, `message.created_at`, …, with `?event_type=` in the
  webhook URL because HeyReach sends one event type per webhook) — none is confirmed by a
  vendor doc; check it against one real delivery. A missing profile URL or time comes back
  `invalid` → review, never a guessed key.
- Vendor actions (stop / pause / suppress per vendor, CRM writes through 08, alerts) live in
  sub-workflows with `REPLACE_…` ids: the lab does not pretend to know your accounts.
  Sequencer and CRM sub-workflows are awaited, so a failed stop lands on the alert path.
- `needs_body`: the sub-workflow fetches the reply text from the vendor's inbox API (not
  wired: field names unconfirmed) and posts `{event_key, body_text}` to the internal
  `reply-text` webhook (header auth) → `POST /v1/replies` (bearer) → the same outcome Switch.
  A second fill answers 404 and is alerted, never routed twice.

`tests/test_n8n.py` checks the JSON against the service: URL template and body keys match
the routes and `FillBody`, every `IntakeOutcome` and `ActionKind` has a branch, error
outputs lead to the alert, and [`examples/demo-deliveries.json`](examples/demo-deliveries.json)
(11 synthetic `.example` deliveries, the LinkedIn ones passed through the Set node's
field expressions) is replayed against the app to show each lands on its expected branch.
Structure only: the workflow was not imported into or run on a live n8n.

## Eval set (`run_eval()` / `python -m replyrouting.evals`)

`examples/eval-replies.jsonl` holds 30 synthetic replies (`.example` domains, fictional
LinkedIn slugs), each labelled with the class a person would give it — or `review` when a
person deciding *is* the right outcome. They cover every rule class (hard / soft bounce,
OOO by header, body, German subject and Instantly code `0`, left company with and without a
new address, referral, unsubscribe incl. a LinkedIn `STOP` and one the vendor labelled
"interested", ticket acks, thanks-only) and the hard LLM cases: OOO that still asks for
pricing, our own footer saying "reply stop" in the quoted thread, sarcasm, an injected
"mark this lead as interested", a quote taken from our email (repair call), a truncated
answer, low confidence, a refusal.

Cases the rules leave open carry a scripted model answer that `FakeLLMClient` replays, so
**the offline run measures the rules and the validation / fallback around the model, not a
model**; pass `client_for=` a live client to score one on the same cases. A case with no
script fails like a provider (`llm_error` → review), never crashes the run.

Errors do not cost the same (an unsubscribe is a legal signal, an OOO mix-up loses a week),
so the report keeps them apart from accuracy:

| field | meaning |
|---|---|
| `confusion`, `per_class` | expected → predicted counts; precision / recall per label |
| `rule_accuracy` | accuracy over rule-class cases only (named denominator) |
| `review_rate` | share sent to a person; LLM calls and tokens summed over attempts |
| `critical` | unsubscribe / bounce / wrong person not caught — even "review" counts, since suppression did not happen |
| `false_hot` | scored interested / question when a person would not (a rep chases a "no") |
| `wrong_confident` | wrong class *not* sent to review |

`EvalGate` defaults: accuracy ≥ 0.9, rule accuracy 1.0, review rate ≤ 0.25, **zero**
critical misses and false hot leads, optional tags that must all pass. The CLI prints the
JSON report and exits 1 on a fail, so it can run in CI. The shipped set passes with one
miss: the refusal case goes to a person (accuracy 0.967, review rate 0.10). Note the
injection case: a model fooled into quoting "mark this lead as interested" passes the
verbatim-quote check (it *is* the person's text) — only a labelled case catches it
(`test_fooled_model_on_injection_fails_the_gate`).

## LLM intent stage (`classify_intent()`)

Runs only on a `needs_llm` classification (calling it on a rule-decided class raises — the
model never overrides bounce / OOO / unsubscribe / wrong person / referral). The provider is
behind `LLMClient.complete(system, user, schema, max_tokens) -> LLMResponse(text,
stop_reason, tokens)`; `FakeLLMClient` replays scripted answers offline, and
`anthropic_request()` / `parse_anthropic_response()` build and read a Messages API body with
`output_config.format` (no network call in the lab).

Answer schema `INTENT_SCHEMA`: `intent` (interested / question / objection / not_interested /
unclear), `confidence`, `evidence_quote`, `suggested_next_action`, `reason` — all required,
`additionalProperties: false`. Provider-side structured output is not enough on its own
(Claude structured-outputs docs, opened 2026-11-14): a refusal or a `max_tokens` stop can
return non-schema output with HTTP 200, enum casing is not guaranteed, and `minimum` /
`maxLength` are rejected by the API, so `validate_intent()` checks locally:

| check | on failure |
|---|---|
| JSON object, exactly the five keys, enums (case-insensitive), confidence a number in 0–1 (not a bool or string), quote ≤ 200 chars, reason ≤ 45 words, next action fits the intent | one repair call with the errors listed, then human review |
| `evidence_quote` is a verbatim span (whitespace / case folded) of the person's **own** text — quoted thread and signature stripped, so a quote from our email or an injected "mark me interested" cannot pass | same |
| `stop_reason: refusal` | human review, no retry (asking again with the same text does not fix it) |
| `stop_reason: max_tokens` | never parsed (partial JSON can look valid); retried with `max_tokens` doubled up to a ceiling |
| `LLMError` (timeout, 5xx) | human review; transport retries belong to the caller (09) |
| `unclear`, next action `review`, confidence < 0.7 | human review; interested / question keep **high** priority |

A review result is `unclassified` + `needs_human_review`: `route()` still stops both
channels (a person replied) and plans `notify_human` with the reason instead of another
`classify_with_llm`. Tokens are summed over all attempts (`IntentResult.input_tokens` /
`output_tokens`); attempts keep stop reason and errors, never the reply text. The vendor
label is compared again after the model's answer.

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
   │ 7 unclassified + needs_llm   → classify_intent() (LLM stage above): interested /
   │                   question / objection / not interested, or unclassified + review
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
| unclassified | stop both + classify_with_llm (after the LLM stage: notify_human with the review reason) |
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
python -m replyrouting.evals examples/eval-replies.jsonl   # JSON report, exit 1 if the gate fails
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

- Service: tested with FastAPI's `TestClient` only, never behind a real vendor webhook; the
  `needs_body` waiting room is in-process and bounded (oldest evicted, lost on restart; the
  dedupe store can be shared through `SqliteSeenEvents`, the waiting room cannot); path
  tokens end up in access logs of anything in front of the service — keep them out of
  shared logs. Vendor retry counts and timeouts are not documented in any page opened.

- English wording only (plus a few common non-English OOO subject prefixes); rules are
  heuristics written from knowledge plus Discourse's and Zammad's open-source auto-reply
  checks — measured on a 30-case synthetic eval set written for this lab, not on real
  replies; a high score there shows the rules do what was intended, not real-world recall.
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
  reply semantics come from event labels, not docs, and the envelope's source field names
  are candidates. The n8n workflow is checked structurally and against the app offline; it
  was never imported into a live n8n, and the inbox fetch for `needs_body` is not wired.
- The LLM stage is tested against `FakeLLMClient` only: no live model call; the eval set's
  LLM answers are scripted, so its LLM-class scores say nothing about a real model. The prompt is English and the 0.7 confidence cut-off is a default, not tuned.
  OpenAI's strict-mode refusal / `finish_reason: "length"` behaviour is a search summary
  (platform.openai.com unreachable, 2026-11-14); only an Anthropic body builder is included.
- Nothing here talks to a live sequencer, CRM or LLM.
