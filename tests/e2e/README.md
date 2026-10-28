# End-to-end tests for mAItion

Deterministic, Docker-based smoke tests that exercise the real compose stack
and OpenWebUI's HTTP API. No LLM API keys, no S3, no external services.

## What it does

1. Boots a trimmed stack via `docker compose`:
   `postgres` + `redis` + `openwebui`, plus an in-network **`llm-stub`**
   service — a tiny OpenAI-compatible server (`llm_stub.py`) that returns
   fixed chat completions.
2. The stock `helpers/entrypoint.sh` runs unmodified, so first-boot
   provisioning (admin signup, tool install, provider wiring) is covered too.
3. Waits for health, waits until first-boot provisioning has created both the
   admin and the regular user, then runs user journeys through public endpoints:
   - **Journey 1** — admin login → model list (`/openai/models`) → create chat
     → LLM completion (`/openai/chat/completions`, served by the stub) →
     persist and re-read the chat (`/api/v1/chats`).
   - **Journey 2** — regular-user (non-admin) login.
   - **Journey 3** — liveness smoke check of the public config endpoint
     (`/api/config`).
4. Tears the stack down (volumes included) unless `--keep-up` is passed.

The RAG services (`api`, `celery_worker`, `celery_beat`) are profiled out by
`compose.dev.yaml`/`compose.e2e.yaml`, keeping the smoke stack small and fast.
Because `OPENAI_API_BASE_URL` points at the stub, no outbound LLM calls happen
and results are fully deterministic.

## Requirements

- Docker with the Compose plugin (the suite exits with code 2 — skip, not
  fail — when Docker is unavailable).
- `curl` and `python3` on the host.

## Running

```bash
./tests/e2e/run.sh              # full run: boot, test, teardown (<5 min)
./tests/e2e/run.sh --keep-up    # keep stack running afterwards for debugging
```

Or via make:

```bash
make test-e2e                   # same as ./tests/e2e/run.sh
make test-e2e-validate          # syntax/wiring pre-flight, no Docker needed
```

Environment knobs:

| Variable              | Default       | Purpose                                  |
| --------------------- | ------------- | ---------------------------------------- |
| `E2E_HTTP_PORT`       | `3100`        | Host port for the OpenWebUI web UI       |
| `LLM_STUB_HOST_PORT`  | `8090`        | Host port for the llm-stub service       |
| `MAITION_E2E_PROJECT` | `maition-e2e` | Docker Compose project name              |
| `E2E_FORCE_ENV`       | unset         | Required when `.env` already exists: backs up and overwrites `.env`/`.env.rag` with the stub templates for this run (restored on exit) |

If `.env` or `.env.rag` already exists in the repo root, the runner **refuses to start**
unless `E2E_FORCE_ENV=1` — journeys always use the fixed E2E accounts from
`env.openwebui.e2e`, not whatever is in a developer checkout. With
`E2E_FORCE_ENV=1`, each existing file is backed up independently and restored
on exit.

For parallel runs on one host, set distinct `E2E_HTTP_PORT`, `LLM_STUB_HOST_PORT`,
and `MAITION_E2E_PROJECT` values per run.

## Adding journeys

Add a `journey_*()` function in `run.sh` and call it from `run_journeys`.
Journeys should only use public HTTP endpoints (`http://localhost:$E2E_HTTP_PORT`)
with the credentials from `tests/e2e/env.openwebui.e2e`, mirroring what real
users do.
