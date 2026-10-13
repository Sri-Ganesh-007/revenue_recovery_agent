# AI Revenue Recovery Agent

A small FastAPI service that decides what to do about a **failed subscription payment**. A local LLM
(Llama 3.2 through LangChain) proposes a recovery action, deterministic Python rules approve or block it,
and the approved action is **simulated** and recorded in PostgreSQL.

> **Everything is simulated.** Nothing is charged, no email or message is sent, and the `payments`
> table is never written. The project is a portfolio prototype, not a payment system.

## The problem

When a recurring payment fails, the right response depends on *why* it failed. Not enough money
suggests trying again later. An expired card means asking the customer for new details. A suspected
fraud case should go to a person and must never be retried automatically. This project lets an LLM
choose the strategy from the customer's context while hard rules keep it safe.

## How it works

```
 client ──► FastAPI ──► service ──► PostgreSQL            existing tables, read through the
              │                      │                    repository + RecoveryContextBuilder
              │                      ▼
              │             RecoveryContext (validated)
              │                      │
              │          ┌───────────▼────────────┐
              │          │  LangGraph workflow    │   at most 2 tries
              │          │  propose ─► check ─┐   │
              │          │     ▲              │   │
              │          │     └── refused ◄──┘   │
              │          └───────────┬────────────┘
              │      LLM (LangChain + Ollama)      deterministic policy.py
              │      proposes ONE action           approves or blocks it
              ▼                      │
          response ◄── PostgreSQL ◄──┘   one transaction: decision (+ simulated action if approved)
```

1. **Replay check.** A request whose `Idempotency-Key` was already answered returns the saved result.
2. **Context.** The existing `RecoveryRepository` and `RecoveryContextBuilder` assemble a validated
   `RecoveryContext`: customer, subscription, the failed payment and its failure reason, payment
   pattern, and this payment's recovery history. A payment that is not failed is rejected (409).
3. **Decide.** `DecisionMaker` sends the context to Llama 3.2 (LangChain `ChatOllama` with
   structured output) and gets back one decision in a fixed form. The customer's name and email are
   not sent to the model.
4. **Check.** `policy.py` returns *approved* or *blocked* with a reason. If blocked, the model is told
   why and asked **once more**. If that is blocked too, the run ends and **nothing is executed**.
5. **Save.** In one transaction the payment row is locked, the context is rebuilt and the policy
   re-run on fresh data, then the decision is saved, plus a simulated action only if approved.
6. **Respond** with what happened and why.

### The LLM chooses, the policy only approves or blocks

| | LLM (`decision_maker.py`) | Policy (`policy.py`) |
|---|---|---|
| Chooses the action and its details | yes | **never** |
| Can execute anything | no | no (only `actions.py` simulates, after approval) |
| Can be overruled | yes, always | n/a |

The five actions the LLM can propose: `schedule_retry` (1–168 hours), `request_payment_update`,
`send_reminder` (friendly or urgent), `escalate_to_human`, `no_action`.

### Safety rules (explicit, in `app/recovery/policy.py`)

| Rule | Meaning |
|---|---|
| Payment still failed | otherwise only the safe fallbacks are allowed |
| Subscription `active` | |
| Recovery window | 14 days from the payment's `created_at` |
| Failure reason | **only exactly `insufficient_funds` may be retried.** Unknown or missing reasons never allow a retry. Fraud (`suspected_fraud`, or any unrecognised reason containing "fraud"/"stolen") and `gateway_timeout` allow *only* escalation or no action. Everything else may get a reminder or a payment-update request, never a retry |
| No open retry | a payment can have only one scheduled retry |
| Retry cap | at most 3 retries per payment |
| 24-hour spacing | retries at least 24h apart; a retry must land inside the window |
| 24-hour cooldown | the same action is not repeated within 24h |
| Always allowed | `escalate_to_human` and `no_action` |

The ten failure labels (`insufficient_funds`, `expired_card`, `invalid_payment_details`,
`authentication_required`, `suspected_fraud`, `gateway_timeout`, `processor_decline`,
`transaction_not_allowed`, `processing_limit_exceeded`, unknown) are the policy's own vocabulary. Only
`insufficient_funds` appears in real data; the others are synthetic test labels, not verified processor
codes. The original failure reason is saved untouched in the audit record.

## Database

The API uses your **existing** PostgreSQL database and does not create tables, run migrations or
initialise a schema. It needs the original five tables: `customers`, `subscriptions`, `payments`,
`recovery_attempts`, `agent_decisions`.

| Table | How the API uses it |
|---|---|
| `customers`, `subscriptions`, `payments` | read only (the payment row is *locked*, never written) |
| `recovery_attempts` | read as recovery history; **one row is added for each approved simulated action** (`strategy` = action, `status` = `scheduled`/`executed_simulated`/`noop`, `outcome` = "SIMULATED: …") |
| `agent_decisions` | **one row per request**: the proposed action, the LLM's explanation, `policy_status` (`approved`/`blocked`) and `policy_reason`. `action_parameters` holds the audit payload: `{parameters, failure_reason, attempts}`, where `attempts` lists every proposal with the policy's verdict |

A blocked proposal is saved as a decision but **never** creates a `recovery_attempts` row.

### Idempotency and duplicates (and their limits)

* With an `Idempotency-Key`, the decision's primary key is `dec_<sha256 of the key>`. The database
  itself therefore rejects a second decision for the same key; the loser returns the saved result.
  The same key for a *different* payment returns 422. Without a header a key is generated and returned
  (`auto-…`) and can be used to replay.
* **Different keys (or no key) for the same payment** are protected by the payment-row lock plus the
  policy re-run on fresh data (no open retry, cap, spacing, cooldown). That is **application logic, not
  a database constraint**: a writer that skips the lock could still create a duplicate.
* Two simultaneous requests with the *same* key may both call the LLM; only one result is saved.
* If the LLM or database fails, nothing is saved, so retrying the same key just runs again.

## Run it

**Requirements:** Python 3.14, an existing PostgreSQL database with the five tables, and
[Ollama](https://ollama.com) with the model pulled (`ollama pull llama3.2:3b`).

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env            # then edit DATABASE_URL (keep the +psycopg2 part)
uvicorn app.main:app --port 8000
```

Configuration (environment variables or `.env`; `.env` is git-ignored):

| Variable | Meaning | Default |
|---|---|---|
| `DATABASE_URL` | `postgresql+psycopg2://user:password@host:5432/dbname` | required |
| `OLLAMA_URL` | where Ollama listens | `http://localhost:11434` |
| `OLLAMA_MODEL` | the model | `llama3.2:3b` |
| `OLLAMA_TIMEOUT` | seconds for one model call | `120` |

### Docker

`docker compose up --build` runs **only the API**; it connects to the PostgreSQL and Ollama you already
have. Inside a container `localhost` is the container itself, so add these to your git-ignored `.env`:

```
CONTAINER_DATABASE_URL=postgresql+psycopg2://USER:PASSWORD@host.docker.internal:5433/YOUR_DATABASE
CONTAINER_OLLAMA_URL=http://host.docker.internal:11434      # optional, this is the default
```

Ollama must be reachable from the container: Docker Desktop (macOS/Windows) works as is; on Linux start
Ollama with `OLLAMA_HOST=0.0.0.0`. `GET /health` reports `database` and `ollama` separately.

## Use the API

```bash
curl -X POST localhost:8000/recovery/simulate \
  -H 'Content-Type: application/json' -H 'Idempotency-Key: order-123-attempt-1' \
  -d '{"payment_id": "pay_004"}'
```

`POST /recovery/proposal` (same body) is a **preview**: it shows what the LLM would propose and saves nothing.
`GET /health` and `GET /` are for monitoring.

Example response when the policy approves (shortened; produced by the project's test fixtures with a fake LLM):

```json
{
  "simulated": true,
  "payment_id": "pay_1",
  "action_taken": "schedule_retry",
  "run_status": "completed",
  "stopped_reason": "completed",
  "idempotency_key": "demo-key-1",
  "idempotent_replay": false,
  "run_id": "dec_0b2c109e25ac7d47cc0c56f999832031c7391890",
  "decision": {"proposed_action": "schedule_retry", "parameters": {"retry_after_hours": 24},
               "policy_status": "approved",
               "policy_reason": "all_rules_passed: schedule_retry is permitted for category 'insufficient_funds'",
               "summary": "Funds were short; the next payday may fix it."},
  "simulated_action": {"id": "att_0b2c10…", "action": "schedule_retry", "status": "scheduled",
                       "scheduled_for": "2026-10-13T12:00:00"},
  "tool_calls": [{"name": "schedule_retry", "arguments": {"retry_after_hours": 24}, "status": "executed_simulated",
                  "policy": {"allowed": true, "rule": "all_rules_passed", "category": "insufficient_funds"}}],
  "message": "schedule_retry was approved by policy and recorded as a SIMULATED action. Nothing real was charged, sent or changed."
}
```

How to read it:

* `run_status`: `completed` (an action was approved and simulated) or `blocked` (every proposal was blocked;
  **nothing was executed** and a human should look). A blocked run is still HTTP 200.
* `tool_calls`: one entry per action the LLM proposed (at most two), in order: `executed_simulated` or
  `rejected`, with the policy's verdict (`allowed`, `rule`, the ten-label `category`, `rules_evaluated`,
  and `alternatives`, the actions that would be allowed).
* `final_message` is the LLM's explanation. It is narrative only; trust `decision`, `tool_calls` and
  `simulated_action`.
* `run_id` is the decision id. Errors: 404 payment not found, 409 payment not failed, 422 bad or reused
  `Idempotency-Key`, 502 the model gave no usable decision, 503 Ollama or the database is unavailable.

## Tests

```bash
pytest                      # no PostgreSQL is contacted unless TEST_DATABASE_URL is set (see below)
```

* The default suite uses fake LLMs and an in-memory SQLite copy of the original tables.
  `tests/conftest.py` forces `DATABASE_URL` to in-memory SQLite and **refuses to create an engine for the
  development database**, so tests cannot touch it.
* `tests/test_database.py` runs only if `TEST_DATABASE_URL` points at a *separate* database whose name ends
  in `_test` (anything else is refused). Every test runs inside a transaction that is **always rolled
  back**, and the teardown checks that no rows were left.
* `LIVE_OLLAMA=1 pytest tests/test_decision_maker.py -k live` calls the real model (no database).
* `scripts/smoke_test.sh` checks a *running* API (health, one simulation, one replay). It makes the API
  save rows, so point it only at a database you are happy to write to.

## Limitations

* **A 3B model is not a strong decision-maker.** It often picks a risky action first (for example a retry
  for an expired card). The policy blocks those and the single re-ask usually steers it to something
  allowed, but results vary. There is no evidence here that the LLM improves recovery.
* **Scheduled retries are never executed or closed** (there is no scheduler). So a payment can have at most
  one scheduled retry; the retry-cap and spacing rules work and are unit-tested but cannot be reached in a
  live run.
* **Concurrency:** the same-key guarantee comes from the primary key. Protection against duplicate retries
  from *different* keys relies on the row lock and the policy (application logic). Truly simultaneous
  requests were **not** tested with parallel database connections; only simulated interleavings were.
* `failed_at` is the payment's `created_at`; the schema has no separate failure time. The failure reason is
  free text, so only the exact value `insufficient_funds` is trusted for retries.
* The audit payload lives in the existing `agent_decisions.action_parameters` JSON column because no schema
  changes were allowed; a dedicated column would be cleaner.
* No authentication, no dashboard, no schema setup (the database must already exist with its tables).
