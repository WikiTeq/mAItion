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
   - **Journey 1** — admin login → provider model list → create chat → LLM
     completion (served by the stub) → persist and re-read the chat.
   - **Journey 2** — regular-user (non-admin) login.
   - **Journey 3** — admin config endpoint sanity check.
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
| `MAITION_E2E_PROJECT` | `maition-e2e` | Docker Compose project name              |

## Adding journeys

Add a `journey_*()` function in `run.sh` and call it from `run_journeys`.
Journeys should only use public HTTP endpoints (`http://localhost:$E2E_HTTP_PORT`)
with the credentials from `tests/e2e/env.openwebui.e2e`, mirroring what real
users do.
