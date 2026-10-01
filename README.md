# Portfolio Support Copilot

Portfolio Support Copilot is an async customer-support copilot for a physical-media store selling Blu-ray, DVD, 4K UHD, box sets, and collector editions. It turns a customer message into structured data, routes the request, retrieves store policy or queries fake business data, and pauses a simulated refund until a human approves it.

## Architecture

```text
React console
          |
          v
POST /runs -> FastAPI -> Redis/arq queue -> worker -> LangGraph
GET /runs <----------------------------------|       extract -> route -> [rag | sql | refund] -> respond
POST /runs/{id}/decision -> Redis/arq queue -> worker -> Command(resume=approve|reject)
                                                       |
                                                       v
                              Postgres + pgvector <- checkpointer, business tables, help-doc embeddings
```

FastAPI serves the built console and accepts and reports runs. The arq worker owns graph execution. Redis holds the queue, run status, the daily run counter, and cached SQL tool results. A shared async OpenRouter client supplies structured JSON output and embeddings. Postgres stores fake business data, runtime settings, thread owners, pgvector help-document chunks, durable run/decision records, an order-level simulated action ledger, and LangGraph checkpoint state keyed by `thread_id`. Redis uses AOF and a persistent volume; workers also recover its run cache and incomplete jobs from Postgres.

## API contract

- `POST /runs` accepts support messages until the global daily demo budget is exhausted. See [`web/API.md`](web/API.md) for request and response payloads, including the `429` response.
- `GET /runs/{run_id}` returns queued, running, awaiting_approval, completed, or failed state. Runs use `answer`, `extraction.media_format`, a `{ lane, handler, rationale }` route object, and integer refund `amount_cents`. Paused runs include `proposed_refund`.
- `GET /runs?status=awaiting_approval` lists paused runs for the approval inbox. `GET /runs?limit=25&offset=0` lists runs newest first for employee monitoring.
- `POST /runs/{run_id}/decision` accepts `{ "decision": "approve" | "reject" }`, durably claims the first decision and enqueues a resume job. Identical replays return `202`, including after completion; conflicting decisions return `409`. A different conversation about an already approved order cannot approve another simulated action or overwrite it with rejection.
- `GET` and `PUT /settings/daily-run-limit` read and update the employee-controlled global daily run limit. See [`web/API.md`](web/API.md) for the payload.
- Customer endpoints use the supplied demo customer record to restrict run details and follow-ups. This lookup is not authentication. See [`web/API.md`](web/API.md) for the checks and follow-up behavior.
- Employee data endpoints provide create, read, update, and delete operations for `/customers`, `/products`, and `/orders`. See [`web/API.md`](web/API.md) for payloads and conflict responses.

## Run locally

1. Copy `.env.example` to `.env` and set `OPENROUTER_API_KEY`. The key is required for model calls and help-document ingestion.
2. Run `docker compose up --build`. If port 8000 is already in use, run `API_PORT=8001 docker compose up --build` and use port 8001 in the URLs below.
3. Open `http://localhost:8000` for the built React console. The customer portal accepts support messages without identification. The optional **Check your orders** lookup uses a demo customer's name and email to list orders, attach an order to a message, and show refund status. The employee console at `/employees` monitors runs, handles approvals, edits demo business data, and sets the daily run limit. The API documentation remains at `http://localhost:8000/docs`; direct API submissions also work with `curl -X POST http://localhost:8000/runs -H 'content-type: application/json' -d '{"message":"My damaged 4K order ORD-1001 needs a refund."}'`.
4. Poll `GET /runs/<run_id>`. When it is `awaiting_approval`, approve or reject it from the employee Approval inbox, or post `{"decision":"approve"}` to `/runs/<run_id>/decision`.

For console development, run `cd web && npm install && npm run dev`. Leave `VITE_API_BASE` blank to send `/api` requests through the Vite proxy to `http://localhost:8000`, or set `VITE_API_BASE=http://localhost:8000 npm run dev`; the API permits local Vite origins. Use `VITE_API_BASE=http://localhost:8001` when the Compose fallback port is in use.

The `init` Compose service applies the schema, seeds fake business data only when the business tables are empty, creates LangGraph checkpoint tables, and embeds documents in `docs/help/`. It exits successfully without a key so the API and worker can still boot, but RAG runs require embeddings and therefore an OpenRouter key. On later starts, unchanged documents are skipped; changed, added, or removed documents are synchronized automatically. Re-ingest documents manually with `docker compose run --rm init python scripts/ingest_help.py`.

`EMBEDDING_DIM` defaults to 1536, which matches `openai/text-embedding-3-small`. When changing the embedding model or its output size, set both `OPENROUTER_EMBEDDING_MODEL` and `EMBEDDING_DIM`; the changed fingerprint automatically re-ingests every help document. Every init run with `RESET_DEMO_DATA=1` truncates and reseeds the business tables. This does not reset help-document embeddings.

`DAILY_RUN_LIMIT` defaults to 50 and seeds the global UTC-day run cap only when no employee setting exists yet. Postgres keeps the employee setting across restarts. The employee console can change the active limit without a restart, and `0` disables it for local development and tests. Each submission that reaches enqueueing claims one run in Redis before the job is added to the queue. Redis expires that UTC-day key at midnight. This global cap is the deliberate exception while per-user tracking, API-key authentication, token accounting, and general rate limiting remain future work.

## Development and tests

Install with `pip install . --group dev`, then run `ruff check .` and `pytest -m "not integration and not eval"`. Unit tests fake the model through dependency injection and cover extraction, routing, retrieval, SQL caching and safety, and the LangGraph pause/resume behavior.

Secret-free real-stack regressions use a deterministic HTTP model transport with the production graph and prompts, pgvector, Postgres checkpoints, Redis/arq, and separate API/worker processes. The transport is not evidence of model answer quality. Use a task-owned Compose project, never the demo database:

```bash
OPENROUTER_API_KEY= docker compose -p support-gap-local -f docker-compose.yml -f tests/compose.integration.yml up -d --build
RUN_ISOLATED_INTEGRATION=1 pytest tests/test_refund_durability.py tests/test_refund_proposal_binding.py
```

The override binds only loopback ports 18082, 18083, 15482 and 16382. Do not share these ports with another project. Regressions cover cross-conversation approval/rejection, concurrent actions, missing orders, decision replays/conflicts, checkpoint-bound amounts, Redis status loss, and a crash between action commit and checkpoint persistence. The worker repairs incomplete durable runs on startup and every 15 seconds. It does not compete with an active thread lock; after a hard process crash with Redis intact, recovery waits for that lock's bounded 530-second expiry. A rejection may later be approved in a new conversation; an approved order is terminal. Each request's first decision and outcome remain immutable. No payment provider is called.

The versioned 50-conversation suite is `tests/evals/conversations-v1.json`. On a fresh isolated project with exactly the six seed orders and no runs, run `python scripts/evaluate_conversations.py --project support-gap-local --output evaluation-offline.json`. It records actual per-case answers, evidence, SQL rows, before/after orders and ledgers, source/fixture hashes, restart/replay checks and measured pass counts. Its offline mode measures integration only. Scoring is a strict mechanical rubric: policy source/key facts, exact SQL rows, numeral answer values in gold row/column order, and database invariants. Spelled-out numbers or different row aliases can fail that contract despite equivalent prose; pass counts are not a general answer-quality estimate. Paid live mode uses `tests/compose.evaluation.yml` and a bounded proxy; obtain a spending allowance first, as described below. Existing 20-message `scripts/evaluate.py` and `pytest -m eval` are legacy extraction/routing checks, not 50 end-to-end conversations or historical benchmark evidence. The older `RUN_INTEGRATION=1` tests call the configured model and must not run without spending authorization.

CI runs backend lint/unit tests, frontend unit/contract tests and build, real-stack secret-free regressions, and a Docker build on pushes and pull requests. Production promotion is not automatic on main. The manual, approval-gated immutable-image workflow, health checks, rollback and missing operator inputs are documented in [deploy/ec2/README.md](deploy/ec2/README.md).

## Paid synthetic evaluation

Do not run live mode until firstmate supplies a spending allowance and an isolated evaluation key. `scripts/evaluation_proxy.py` limits chat calls to 200, each input to 64,000 UTF-8 bytes including a 1,024-byte protocol reserve, each output to 1,024 tokens, and total embedding input to one million bytes. It reserves worst-case costs before every provider call and refuses extra request options or current prices above the approved ceilings. Failed calls remain reserved. The proxy fsyncs reservations to the task-owned `evaluation_budget` volume before calling the provider and restores them on restart. Keep that volume with the approved run; never destroy it to reset an exhausted allowance.

A nonsecret allowance receipt must contain `approval_reference`, `model`, `max_usd`, `input_usd_per_million`, `output_usd_per_million`, and `embedding_usd_per_million`. The supported model is `openai/gpt-4.1-mini`; the maximum allowed receipt is $8. At $0.40/$1.60 per million chat input/output tokens and $0.02 per million embedding tokens, the conservative run ceiling is $5.47. These prices are checked again at proxy startup. Supply `EVALUATION_ALLOWANCE_FILE` as an absolute receipt path and `OPENROUTER_API_KEY` through the authorized environment, then use all three Compose files in a fresh task-owned project. Never commit either runtime key or allowance credential material. Invoke the evaluator with `--mode live --allowance-receipt <receipt>` and retain its actual outcomes even when cases fail. No historical success percentage is assumed.

## Repository layout

- `src/support_copilot/graph.py` - the checkpointed `extract -> route -> [rag | sql | refund] -> respond` graph.
- `src/support_copilot/api.py` and `src/support_copilot/worker.py` - asynchronous HTTP and arq process boundaries.
- `src/support_copilot/schema.sql`, `seed.py`, and `ingest.py` - Postgres, fake data, and pgvector ingestion.
- `docs/help/` - ten store help documents used by RAG.
- `tests/evals/support_cases.jsonl` - labeled extract and route evaluation set.

## Next

- Authentication and authorization for the employee console. The `/employees` route is intentionally unauthenticated in this demo.
- API-key authentication and per-key rate limiting. The global daily demo cap is the deliberate exception documented above.
- Optimistic locking or another transaction policy for concurrent employee data edits.
- Structured logging with request tracing and a metrics endpoint.
- Live streaming with server-sent events for agent progress.
- Any real payment integration. Refunds remain simulated against fake business data.

## Frontend

The React and TypeScript support console lives in [web/](web/). It can run against the API or its canned mock mode and is documented in [web/README.md](web/README.md).
