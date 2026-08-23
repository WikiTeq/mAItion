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
export HTTP_WEB_PORT="$HTTP_PORT"
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
  # api METHOD PATH [JSON_BODY] [TOKEN]
  local method="$1" path="$2" body="${3:-}" token="${4:-}"
  local args=(-sS --max-time 30 -X "$method" "http://localhost:${HTTP_PORT}${path}")
  [[ -n "$body" ]] && args+=(-H "Content-Type: application/json" -d "$body")
  [[ -n "$token" ]] && args+=(-H "Authorization: Bearer $token")
  curl "${args[@]}"
}

jqget() { python3 -c "import json,sys;d=json.load(sys.stdin);print(d.get(sys.argv[1],''))" "$1"; }

check_provider_guard() {
  # In reuse-existing-.env mode, refuse to run against anything but the stub:
  # otherwise Journey 1 traffic would hit the dev's real paid LLM provider.
  local base_url
  base_url="$(grep -E '^OPENAI_API_BASE_URL=' "$REPO_ROOT/.env" | tail -n 1 | cut -d= -f2- || true)"
  base_url="${base_url//\"/}"
  base_url="${base_url%\'}"
  if [[ -z "$base_url" ]]; then
    fail "reuse mode: no OPENAI_API_BASE_URL found in $REPO_ROOT/.env"
    return 1
  fi
  case "$base_url" in
    *llm-stub*)
      return 0
      ;;
    *)
      fail "refusing to run E2E: OPENAI_API_BASE_URL ($base_url) does not point at llm-stub."
      fail "Journey traffic would hit your real LLM provider. Re-run with E2E_FORCE_ENV=1 to force the stub env, or point OPENAI_API_BASE_URL at http://llm-stub:8090/v1."
      return 1
      ;;
  esac
}

# ---------------------------------------------------------------------------
# journeys
# ---------------------------------------------------------------------------

journey_admin_login_and_chat() {
  local token resp model_id chat_id content title

  log "Journey 1: admin login -> models -> chat completion -> chat persistence"

  resp=$(api POST /api/v1/auths/signin '{"email":"admin@example123.com","password":"q1w2e3r4!"}')
  token=$(printf '%s' "$resp" | jqget token)
  if [[ -z "$token" || "$resp" == *"detail"* && "$resp" == *"$token"* ]]; then
    fail "admin signin failed: $(printf '%s' "$resp" | head -c 300)"
    return 1
  fi
  pass "admin signin returns JWT"

  resp=$(api GET /openai/models "" "$token")
  model_id=$(printf '%s' "$resp" | python3 -c '
import json,sys
d=json.load(sys.stdin)
items=d if isinstance(d, list) else d.get("data", [])
ids=[m["id"] for m in items if isinstance(m, dict) and "id" in m]
pref=[i for i in ids if i.startswith("stub-model")]
print(pref[0] if pref else (ids[0] if ids else ""))')
  if [[ -z "$model_id" ]]; then
    fail "no usable model in /openai/models: $(printf '%s' "$resp" | head -c 300)"
    return 1
  fi
  pass "model list contains stubbed LLM (id: $model_id)"

  resp=$(api POST /api/v1/chats/new "{\"chat\":{\"title\":\"e2e-journey-1\",\"messages\":[],\"models\":[\"$model_id\"],\"params\":{}},\"model_id\":\"$model_id\",\"messages\":[]}" "$token")
  chat_id=$(printf '%s' "$resp" | jqget id)
  if [[ -z "$chat_id" ]]; then
    fail "chat creation failed: $(printf '%s' "$resp" | head -c 300)"
    return 1
  fi
  pass "created chat $chat_id"

  resp=$(api POST /openai/chat/completions "{\"model\":\"$model_id\",\"stream\":false,\"messages\":[{\"role\":\"user\",\"content\":\"What is the capital of France?\"}]}" "$token")
  content=$(printf '%s' "$resp" | python3 -c 'import json,sys;d=json.load(sys.stdin);print(d.get("choices",[{}])[0].get("message",{}).get("content",""))')
  if [[ "$content" != E2E-STUB-REPLY* ]]; then
    fail "completion did not come from stub: $(printf '%s' "$resp" | head -c 300)"
    return 1
  fi
  pass "LLM completion served by llm-stub"

  # Persist a completed conversation, then read it back to verify storage.
  resp=$(api POST "/api/v1/chats/$chat_id" "{\"chat\":{\"title\":\"e2e-journey-1\",\"models\":[\"$model_id\"],\"messages\":[{\"role\":\"user\",\"content\":\"What is the capital of France?\"},{\"role\":\"assistant\",\"content\":\"stubbed answer\"}],\"params\":{}}}" "$token") ||
    resp=$(api POST "/api/v1/chats/$chat_id" "{\"title\":\"e2e-journey-1\",\"models\":[\"$model_id\"],\"messages\":[{\"role\":\"user\",\"content\":\"What is the capital of France?\"},{\"role\":\"assistant\",\"content\":\"stubbed answer\"}],\"params\":{}}" "$token")
  title=$(printf '%s' "$resp" | jqget title)
  if [[ "$title" != "e2e-journey-1" ]]; then
    fail "chat update/persistence failed: $(printf '%s' "$resp" | head -c 300)"
    return 1
  fi
  resp=$(api GET "/api/v1/chats/$chat_id" "" "$token")
  if ! printf '%s' "$resp" | python3 -c '
import json,sys
d=json.load(sys.stdin)
msgs=d.get("chat",{}).get("messages") or d.get("messages") or []
assert any(m.get("content")=="stubbed answer" for m in msgs), "assistant msg missing"' 2>/dev/null; then
    fail "persisted chat does not contain the saved messages: $(printf '%s' "$resp" | head -c 300)"
    return 1
  fi
  pass "chat persisted and re-readable with full message history"
}

journey_regular_user_login() {
  log "Journey 2: regular user login"
  local resp token
  resp=$(api POST /api/v1/auths/signin '{"email":"user@example123.com","password":"q1w2e3r4!"}')
  token=$(printf '%s' "$resp" | jqget token)
  if [[ -z "$token" ]]; then
    fail "regular user signin failed: $(printf '%s' "$resp" | head -c 300)"
    return 1
  fi
  pass "regular user signin returns JWT"
}

journey_admin_config_endpoints() {
  log "Journey 3: admin config endpoints reachable"
  local resp token
  resp=$(api POST /api/v1/auths/signin '{"email":"admin@example123.com","password":"q1w2e3r4!"}')
  token=$(printf '%s' "$resp" | jqget token)
  if [[ -z "$token" ]]; then
    fail "signin for config journey failed"
    return 1
  fi
  resp=$(api GET /api/config "" "$token")
  if ! printf '%s' "$resp" | python3 -c 'import json,sys;json.load(sys.stdin)' 2>/dev/null; then
    fail "GET /api/config returned invalid JSON: $(printf '%s' "$resp" | head -c 200)"
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
  # real ones; both are gitignored so this never touches a dev's own setup.
  if [[ ! -f "$REPO_ROOT/.env" || "${E2E_FORCE_ENV:-0}" == "1" ]] ; then
    if [[ -f "$REPO_ROOT/.env" && "${E2E_FORCE_ENV:-0}" == "1" ]]; then
      ENV_BACKUP_DIR="$(mktemp -d "${TMPDIR:-/tmp}/maition-e2e-env-backup.XXXXXX")"
      chmod 700 "$ENV_BACKUP_DIR"
      cp "$REPO_ROOT/.env" "$ENV_BACKUP_DIR/.env.bak"
      ENV_FILE_STATE_OPENWEBUI=backed_up
      log "Backed up existing .env to $ENV_BACKUP_DIR before overwrite"
    fi
    if [[ -f "$REPO_ROOT/.env.rag" ]]; then
      if [[ "${E2E_FORCE_ENV:-0}" == "1" ]]; then
        [[ -n "$ENV_BACKUP_DIR" ]] || { ENV_BACKUP_DIR="$(mktemp -d "${TMPDIR:-/tmp}/maition-e2e-env-backup.XXXXXX")"; chmod 700 "$ENV_BACKUP_DIR"; }
        cp "$REPO_ROOT/.env.rag" "$ENV_BACKUP_DIR/.env.rag.bak"
        ENV_FILE_STATE_RAG=backed_up
        log "Backed up existing .env.rag to $ENV_BACKUP_DIR before overwrite"
      fi
    elif [[ "${E2E_FORCE_ENV:-0}" == "1" ]]; then
      log "No existing .env.rag found; provisioning from tests/e2e template"
    fi
    log "Provisioning .env / .env.rag from tests/e2e templates"
    cp "$SCRIPT_DIR/env.openwebui.e2e" "$REPO_ROOT/.env"
    cp "$SCRIPT_DIR/env.rag.e2e" "$REPO_ROOT/.env.rag"
    [[ -z "$ENV_FILE_STATE_OPENWEBUI" ]] && ENV_FILE_STATE_OPENWEBUI=provisioned
    [[ -z "$ENV_FILE_STATE_RAG" ]] && ENV_FILE_STATE_RAG=provisioned
  else
    log "Using existing $REPO_ROOT/.env (E2E credentials may differ)"
    check_provider_guard || { cleanup; exit 1; }
  fi
  trap cleanup EXIT

  log "Using project '$PROJECT'; web UI will bind http://localhost:${HTTP_PORT}"
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
      -H "Content-Type: application/json" -d '{"email":"admin@example123.com","password":"q1w2e3r4!"}')
    user_code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 -X POST "http://localhost:${HTTP_PORT}/api/v1/auths/signin" \
      -H "Content-Type: application/json" -d '{"email":"user@example123.com","password":"q1w2e3r4!"}')
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
