# 09 · n8n reliability kit

**The pain:** branches that fail silently, and retries that do more harm than the failure.
n8n's Retry On Fail re-runs the **whole node with every input item** (2–5 tries, a fixed
wait of at most 5 s, no `Retry-After`), so a 429 on lead 57 of 100 re-sends leads 1–56 to
the sequencer. The HTTP Request node never retries a 429 by itself, and the error workflow
carries no items, so the lead that failed is not named anywhere a person looks (research
[R9 2026-11-01](../../research/notes/2026-11-01-R9-n8n-retry-dedupe-error-workflow.md),
[R9 2026-11-12](../../research/notes/2026-11-12-R9-n8n-rate-limits-queue-mode.md)).

v0.0.1 (started 2026-11-18): **per-item retries** (`reliability.retry`).

## Per-item retries

```python
from reliability import HttpResult, RetryPolicy, TransportError, run_items


def push(lead):  # your transport: HttpResult for any status, TransportError for no answer
    ...


report = run_items(
    leads,
    push,
    idempotent=False,
    key=lambda lead: lead["email"],
    policy=RetryPolicy(max_attempts=4, max_wait_seconds=300),
)
report.counts()  # {"ok": 97, "failed": 1, "exhausted": 0, "deferred": 1, "unknown": 1}
report.dlq_rows()  # one flat row per item that did not land, with safe_to_resend
```

Only the item that failed is sent again; an item that succeeded never is. One rule table,
`decide(result, attempt=, idempotent=, keyed=)`, returns `done` / `retry` (with `wait_ms`) /
`stop` (with an outcome), so an n8n loop can use it item by item: HTTP Request with
continue-on-fail → `decide` → Wait `wait_ms` → back into the same call for that item only.

| answer | idempotent call (GET / PUT / DELETE, upsert on a unique key, POST + honoured `Idempotency-Key`) | plain create (add-lead, CRM create, call create) |
|---|---|---|
| 2xx | `ok` | `ok` |
| 429 | retry: `Retry-After`, else backoff | retry: `Retry-After`, else backoff |
| 408 / 425 / 5xx | retry with backoff | **`unknown`** — may have landed; read back, never re-send blind |
| timeout / reset after sending | retry with backoff | **`unknown`** |
| DNS / refused (never left) | retry | retry |
| 409 with `Idempotency-Key` | retry (first request still in flight) | — |
| 422 with `Idempotency-Key` | `failed` (key reused with another payload) | — |
| other 4xx | `failed` | `failed` |
| `Retry-After` > `max_wait_seconds` | `deferred` + `retry_at` | `deferred` + `retry_at` |
| attempts used up | `exhausted` | `exhausted` |

Backoff is `base * factor ** (attempt - 1)` capped at `max_backoff_seconds`, with optional
jitter; `Retry-After` is read as delta-seconds or HTTP-date. A `Retry-After` longer than
`max_wait_seconds` is not slept inside the execution: the item ends `deferred` with
`retry_at`, for a DLQ table or an n8n Wait node.

Why these rules:
- RFC 9110 §9.2.2 idempotent methods; the same set (plus TRACE) is urllib3's default
  `allowed_methods`, and urllib3 also honours `Retry-After` before its own backoff.
- A POST becomes replay-safe only with an `Idempotency-Key` the vendor documents. Per
  draft-ietf-httpapi-idempotency-key-header, a second request while the first is still
  processing gets 409 and a reused key with a different payload gets 422.
- A 429 on a create is retried because the vendor refused it, which is the R9 advice
  (route 429 items back into the same call); a 5xx or a timeout on a create is not, because
  the lead may already be in the campaign.

## Run it

```bash
python -m pip install -e ".[dev]"
python -m pytest -q
```

## Limits

- Library only: no FastAPI route, n8n workflow, DLQ store or shared rate budget yet (next
  steps in the root ROADMAP 09). The wait happens in-process; for long waits use `deferred`.
- Offline tests with a scripted fake vendor; never run against a live vendor or a live n8n.
- Which vendors honour `Idempotency-Key`, and whether a vendor's 429 can follow a processed
  request, is per vendor and not checked here; the caller decides `idempotent` / `keyed`.
- RFC 9110, the IETF draft page and the AWS backoff article did not resolve from the run
  environment (2026-11-18); the rules come from the draft's source on GitHub and urllib3's
  `retry.py`.
