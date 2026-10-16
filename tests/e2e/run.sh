#!/usr/bin/env bash
# E2E smoke tests for mAItion.
#
# Boots a trimmed compose stack (openwebui + postgres + redis + llm-stub),
# waits for health, runs deterministic API journeys against OpenWebUI's HTTP
# API, then tears everything down. Requires Docker; exits with a clear
# skip-with-warning when Docker is unavailable so CI can treat it as skipped.
#
# Usage: ./run.sh [--keep-up]     (env: MAITION_E2E_PROJECT to name the compose project)

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

PROJECT="${MAITION_E2E_PROJECT:-maition-e2e}"
HTTP_PORT="${E2E_HTTP_PORT:-3100}"
LLM_STUB_PORT="${LLM_STUB_HOST_PORT:-8090}"
export HTTP_WEB_PORT="$HTTP_PORT"
export LLM_STUB_HOST_PORT="$LLM_STUB_PORT"

# Must stay in sync with tests/e2e/env.openwebui.e2e (validated by validate.py).
E2E_ADMIN_EMAIL="admin@example123.com"
E2E_ADMIN_PASS="q1w2e3r4!"
E2E_USER_EMAIL="user@example123.com"
E2E_USER_PASS="q1w2e3r4!"
# compose.e2e.yaml uses this for its bind mounts (see comment there).
export E2E_DIR="$SCRIPT_DIR"

PASS=0
FAIL=0
KEEP_UP=0
# Per-file state: "provisioned" (we created it; delete on teardown unless
# KEEP_UP) or "backed_up" (we overwrote a pre-existing file; restore original).
ENV_FILE_STATE_OPENWEBUI=""
ENV_FILE_STATE_RAG=""
ENV_BACKUP_DIR=""

restore_env() {
  local f file state_var state
  for f in OPENWEBUI RAG; do
    file="$REPO_ROOT/.env"
    [[ "$f" == "RAG" ]] && file="$REPO_ROOT/.env.rag"
    state_var="ENV_FILE_STATE_$f"
    state="${!state_var}"
    if [[ "$state" == "provisioned" ]]; then
      if [[ "$KEEP_UP" == 1 ]]; then
        log "--keep-up set; leaving provisioned $(basename "$file") in place for stack reuse"
      else
        rm -f "$file"
      fi
    elif [[ "$state" == "backed_up" ]]; then
      if [[ "$KEEP_UP" == 1 ]]; then
        log "--keep-up set; $(basename "$file") stays overwritten by the E2E copy; your original is kept at $ENV_BACKUP_DIR/$(basename "$file").bak"
      elif mv "$ENV_BACKUP_DIR/$(basename "$file").bak" "$file"; then
        log "Restored pre-existing $(basename "$file") from backup"
      else
        log "WARN: could not restore $(basename "$file"); original preserved at $ENV_BACKUP_DIR/$(basename "$file").bak"
      fi
    fi
  done
}

cleanup() {
  if [[ "${_CLEANED_UP:-0}" == 1 ]]; then
    return
  fi
  _CLEANED_UP=1
  if [[ "$KEEP_UP" == 1 ]]; then
    log "--keep-up set; leaving stack running (project: $PROJECT). Tear down with:"
    log "  docker compose -p $PROJECT -f $REPO_ROOT/compose.yaml -f $REPO_ROOT/compose.dev.yaml -f $SCRIPT_DIR/compose.e2e.yaml down -v --remove-orphans"
    restore_env
    return
  fi
  log "Tearing down stack..."
  dc down --remove-orphans --volumes >/dev/null 2>&1 || true
  restore_env
}

if [[ "${1:-}" == "--keep-up" ]]; then
  KEEP_UP=1
fi

log()  { printf '\033[1;34m[e2e]\033[0m %s\n' "$*"; }
pass() { printf '\033[1;32m[PASS]\033[0m %s\n' "$*"; PASS=$((PASS+1)); }
fail() { printf '\033[1;31m[FAIL]\033[0m %s\n' "$*"; FAIL=$((FAIL+1)); }

dc() {
  docker compose -p "$PROJECT" \
    -f "$REPO_ROOT/compose.yaml" \
    -f "$REPO_ROOT/compose.dev.yaml" \
    -f "$SCRIPT_DIR/compose.e2e.yaml" \
    --project-directory "$REPO_ROOT" \
    "$@"
}

require_docker() {
  if ! command -v docker >/dev/null 2>&1; then
    log "SKIP: docker not found — mAItion E2E suite requires Docker. Skipping."
    exit 2
  fi
  if ! docker info >/dev/null 2>&1; then
    log "SKIP: Docker daemon unreachable — mAItion E2E suite requires Docker. Skipping."
    exit 2
  fi
}

wait_for() {
  local url="$1" label="$2" tries="${3:-90}"
  i=0
  while [ "$i" -lt "$tries" ]; do
    if curl -fsS --max-time 5 -o /dev/null "$url" 2>/dev/null; then
      return 0
    fi
    sleep 3
    i=$((i + 1))
  done
  fail "timeout waiting for $label ($url) after $tries attempts"
  return 1
}

api() {
  # api METHOD PATH [JSON_BODY] [TOKEN] -> sets API_CODE and API_BODY
  local method="$1" path="$2" body="${3:-}" token="${4:-}" tmp code
  tmp="$(mktemp)"
  local args=(-sS --max-time 30 -o "$tmp" -w '%{http_code}' -X "$method" "http://localhost:${HTTP_PORT}${path}")
  [[ -n "$body" ]] && args+=(-H "Content-Type: application/json" -d "$body")
  [[ -n "$token" ]] && args+=(-H "Authorization: Bearer $token")
  code=$(curl "${args[@]}") || code="000"
  API_BODY="$(cat "$tmp")"
  API_CODE="$code"
  rm -f "$tmp"
}

api_expect() {
  # api_expect EXPECTED_STATUS METHOD PATH [JSON_BODY] [TOKEN]
  local expected="$1"
  shift
  api "$@"
  if [[ "$API_CODE" != "$expected" ]]; then
    fail "expected HTTP $expected for $2, got $API_CODE: $(printf '%s' "$API_BODY" | head -c 300)"
    return 1
  fi
  return 0
}

jqget() { python3 -c "import json,sys;d=json.load(sys.stdin);v=d.get(sys.argv[1],'');print('' if v is None else v)" "$1"; }

check_port_free() {
  local port="$1" label="$2"
  if command -v ss >/dev/null 2>&1; then
    if ss -tlnH "sport = :$port" 2>/dev/null | grep -q .; then
      fail "port $port ($label) is already in use — set E2E_HTTP_PORT or LLM_STUB_HOST_PORT for parallel runs"
      return 1
    fi
    return 0
  fi
  if command -v nc >/dev/null 2>&1; then
    if nc -z 127.0.0.1 "$port" 2>/dev/null; then
      fail "port $port ($label) is already in use — set E2E_HTTP_PORT or LLM_STUB_HOST_PORT for parallel runs"
      return 1
    fi
    return 0
  fi
  if (echo >/dev/tcp/127.0.0.1/"$port") 2>/dev/null; then
    fail "port $port ($label) is already in use — set E2E_HTTP_PORT or LLM_STUB_HOST_PORT for parallel runs"
    return 1
  fi
  return 0
}

signin() {
  # signin EMAIL PASSWORD -> sets SIGNIN_CODE and SIGNIN_BODY
  local email="$1" password="$2" tmp code
  tmp="$(mktemp)"
  code=$(curl -sS --max-time 30 -o "$tmp" -w '%{http_code}' -X POST \
    "http://localhost:${HTTP_PORT}/api/v1/auths/signin" \
    -H "Content-Type: application/json" \
    -d "{\"email\":\"$email\",\"password\":\"$password\"}") || code="000"
  SIGNIN_BODY="$(cat "$tmp")"
  SIGNIN_CODE="$code"
  rm -f "$tmp"
}

# ---------------------------------------------------------------------------
# journeys
# ---------------------------------------------------------------------------

journey_admin_login_and_chat() {
  local token model_id chat_id content title

  log "Journey 1: admin login -> models -> chat completion -> chat persistence"

  signin "$E2E_ADMIN_EMAIL" "$E2E_ADMIN_PASS"
  if [[ "$SIGNIN_CODE" != "200" ]]; then
    fail "admin signin failed (HTTP $SIGNIN_CODE): $(printf '%s' "$SIGNIN_BODY" | head -c 300)"
    return 1
  fi
  token=$(printf '%s' "$SIGNIN_BODY" | jqget token)
  if [[ -z "$token" ]]; then
    fail "admin signin returned no token: $(printf '%s' "$SIGNIN_BODY" | head -c 300)"
    return 1
  fi
  pass "admin signin returns JWT"

  api_expect 200 GET /openai/models "" "$token" || return 1
  model_id=$(printf '%s' "$API_BODY" | python3 -c '
import json,sys
d=json.load(sys.stdin)
items=d if isinstance(d, list) else d.get("data", [])
ids=[m["id"] for m in items if isinstance(m, dict) and "id" in m]
pref=[i for i in ids if i.startswith("stub-model")]
print(pref[0] if pref else (ids[0] if ids else ""))')
  if [[ -z "$model_id" ]]; then
    fail "no usable model in /openai/models: $(printf '%s' "$API_BODY" | head -c 300)"
    return 1
  fi
  pass "model list contains stubbed LLM (id: $model_id)"

  api_expect 200 POST /api/v1/chats/new "{\"chat\":{\"title\":\"e2e-journey-1\",\"messages\":[],\"models\":[\"$model_id\"],\"params\":{}},\"model_id\":\"$model_id\",\"messages\":[]}" "$token" || return 1
  chat_id=$(printf '%s' "$API_BODY" | jqget id)
  if [[ -z "$chat_id" ]]; then
    fail "chat creation failed: $(printf '%s' "$API_BODY" | head -c 300)"
    return 1
  fi
  pass "created chat $chat_id"

  api_expect 200 POST /openai/chat/completions "{\"model\":\"$model_id\",\"stream\":false,\"messages\":[{\"role\":\"user\",\"content\":\"What is the capital of France?\"}]}" "$token" || return 1
  content=$(printf '%s' "$API_BODY" | python3 -c 'import json,sys;d=json.load(sys.stdin);print(d.get("choices",[{}])[0].get("message",{}).get("content",""))')
  if [[ "$content" != E2E-STUB-REPLY* ]]; then
    fail "completion did not come from stub: $(printf '%s' "$API_BODY" | head -c 300)"
    return 1
  fi
  pass "LLM completion served by llm-stub"

  api POST "/api/v1/chats/$chat_id" "{\"chat\":{\"title\":\"e2e-journey-1\",\"models\":[\"$model_id\"],\"messages\":[{\"role\":\"user\",\"content\":\"What is the capital of France?\"},{\"role\":\"assistant\",\"content\":\"stubbed answer\"}],\"params\":{}}}" "$token"
  if [[ "$API_CODE" != "200" ]]; then
    api POST "/api/v1/chats/$chat_id" "{\"title\":\"e2e-journey-1\",\"models\":[\"$model_id\"],\"messages\":[{\"role\":\"user\",\"content\":\"What is the capital of France?\"},{\"role\":\"assistant\",\"content\":\"stubbed answer\"}],\"params\":{}}" "$token"
  fi
  if [[ "$API_CODE" != "200" ]]; then
    fail "chat update/persistence failed (HTTP $API_CODE): $(printf '%s' "$API_BODY" | head -c 300)"
    return 1
  fi
  title=$(printf '%s' "$API_BODY" | jqget title)
  if [[ "$title" != "e2e-journey-1" ]]; then
    fail "chat update/persistence failed: $(printf '%s' "$API_BODY" | head -c 300)"
    return 1
  fi
  api_expect 200 GET "/api/v1/chats/$chat_id" "" "$token" || return 1
  if ! printf '%s' "$API_BODY" | python3 -c '
import json,sys
d=json.load(sys.stdin)
msgs=d.get("chat",{}).get("messages") or d.get("messages") or []
assert any(m.get("content")=="stubbed answer" for m in msgs), "assistant msg missing"' 2>/dev/null; then
    fail "persisted chat does not contain the saved messages: $(printf '%s' "$API_BODY" | head -c 300)"
    return 1
  fi
  pass "chat persisted and re-readable with full message history"
}

journey_regular_user_login() {
  log "Journey 2: regular user login"
  local token
  signin "$E2E_USER_EMAIL" "$E2E_USER_PASS"
  if [[ "$SIGNIN_CODE" != "200" ]]; then
    fail "regular user signin failed (HTTP $SIGNIN_CODE): $(printf '%s' "$SIGNIN_BODY" | head -c 300)"
    return 1
  fi
  token=$(printf '%s' "$SIGNIN_BODY" | jqget token)
  if [[ -z "$token" ]]; then
    fail "regular user signin returned no token: $(printf '%s' "$SIGNIN_BODY" | head -c 300)"
    return 1
  fi
  pass "regular user signin returns JWT"
}

journey_admin_config_endpoints() {
  log "Journey 3: admin config endpoints reachable"
  local token
  signin "$E2E_ADMIN_EMAIL" "$E2E_ADMIN_PASS"
  if [[ "$SIGNIN_CODE" != "200" ]]; then
    fail "signin for config journey failed (HTTP $SIGNIN_CODE)"
    return 1
  fi
  token=$(printf '%s' "$SIGNIN_BODY" | jqget token)
  if [[ -z "$token" ]]; then
    fail "signin for config journey returned no token"
    return 1
  fi
  api_expect 200 GET /api/config "" "$token" || return 1
  if ! printf '%s' "$API_BODY" | python3 -c 'import json,sys;json.load(sys.stdin)' 2>/dev/null; then
    fail "GET /api/config returned invalid JSON: $(printf '%s' "$API_BODY" | head -c 200)"
    return 1
  fi
  pass "public config endpoint responds with JSON"
}

run_journeys() {
  journey_admin_login_and_chat
  journey_regular_user_login
  journey_admin_config_endpoints
}

# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

main() {
  require_docker

  # Provision throwaway env files from the committed E2E templates. The repo
  # ships only examples (.env.openwebui.example etc.) and compose requires
  # real ones; both are gitignored. Refuse to clobber existing env files
  # unless E2E_FORCE_ENV=1 (backs up and restores on exit).
  if [[ ( -f "$REPO_ROOT/.env" || -f "$REPO_ROOT/.env.rag" ) && "${E2E_FORCE_ENV:-0}" != "1" ]]; then
    log "refusing to run: existing env file(s) detected (.env and/or .env.rag)."
    log "E2E journeys require the committed stub templates (see tests/e2e/env.openwebui.e2e)."
    log "Re-run with E2E_FORCE_ENV=1 to back up and overwrite .env/.env.rag for this run."
    exit 1
  fi

  if [[ "${E2E_FORCE_ENV:-0}" == "1" ]]; then
    ENV_BACKUP_DIR="$(mktemp -d "${TMPDIR:-/tmp}/maition-e2e-env-backup.XXXXXX")"
    chmod 700 "$ENV_BACKUP_DIR"
    if [[ -f "$REPO_ROOT/.env" ]]; then
      cp "$REPO_ROOT/.env" "$ENV_BACKUP_DIR/.env.bak"
      ENV_FILE_STATE_OPENWEBUI=backed_up
      log "Backed up existing .env to $ENV_BACKUP_DIR before overwrite"
    fi
    if [[ -f "$REPO_ROOT/.env.rag" ]]; then
      cp "$REPO_ROOT/.env.rag" "$ENV_BACKUP_DIR/.env.rag.bak"
      ENV_FILE_STATE_RAG=backed_up
      log "Backed up existing .env.rag to $ENV_BACKUP_DIR before overwrite"
    fi
  fi

  log "Provisioning .env / .env.rag from tests/e2e templates"
  cp "$SCRIPT_DIR/env.openwebui.e2e" "$REPO_ROOT/.env"
  cp "$SCRIPT_DIR/env.rag.e2e" "$REPO_ROOT/.env.rag"
  [[ -z "$ENV_FILE_STATE_OPENWEBUI" ]] && ENV_FILE_STATE_OPENWEBUI=provisioned
  [[ -z "$ENV_FILE_STATE_RAG" ]] && ENV_FILE_STATE_RAG=provisioned
  trap cleanup EXIT

  log "Using project '$PROJECT'; web UI http://localhost:${HTTP_PORT}; llm-stub 127.0.0.1:${LLM_STUB_PORT}"
  check_port_free "$HTTP_PORT" "OpenWebUI" || exit 1
  check_port_free "$LLM_STUB_PORT" "llm-stub" || exit 1
  log "Booting core stack (postgres, redis, llm-stub, openwebui)..."

  dc down --remove-orphans --volumes >/dev/null 2>&1 || true
  dc up -d --build --quiet-pull || { fail "docker compose up failed"; dc logs openwebui 2>/dev/null | tail -40; cleanup; exit 1; }

  log "Waiting for health endpoints..."
  wait_for "http://localhost:${HTTP_PORT}/health" "openwebui /health" 60 || { dc logs openwebui 2>/dev/null | tail -40; cleanup; exit 1; }

  # The custom entrypoint provisions the admin account, then continues with
  # provider wiring and the optional regular user; wait until BOTH accounts
  # can sign in so journeys never race first-boot provisioning.
  ok=0
  i=0
  while [ "$i" -lt 40 ]; do
    admin_code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 -X POST "http://localhost:${HTTP_PORT}/api/v1/auths/signin" \
      -H "Content-Type: application/json" -d "{\"email\":\"$E2E_ADMIN_EMAIL\",\"password\":\"$E2E_ADMIN_PASS\"}")
    user_code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 -X POST "http://localhost:${HTTP_PORT}/api/v1/auths/signin" \
      -H "Content-Type: application/json" -d "{\"email\":\"$E2E_USER_EMAIL\",\"password\":\"$E2E_USER_PASS\"}")
    [[ "$admin_code" == "200" && "$user_code" == "200" ]] && { ok=1; break; }
    sleep 3
    i=$((i + 1))
  done
  if [[ "$ok" != 1 ]]; then
    fail "provisioned accounts not ready before timeout (admin=$admin_code user=$user_code)"
    dc logs openwebui 2>/dev/null | tail -40
    cleanup
    exit 1
  fi
  pass "stack healthy; admin and regular user provisioned by entrypoint"

  run_journeys

  log "-------------------------------------------"
  log "Results: $PASS passed, $FAIL failed"
  cleanup
  if [[ "$FAIL" -gt 0 ]]; then
    exit 1
  fi
  log "All E2E checks passed."
  exit 0
}

main
