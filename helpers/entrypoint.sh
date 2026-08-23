#!/bin/bash

# Inspired by https://github.com/open-webui/open-webui/discussions/8955#discussioncomment-12548747
# this custom entrypoint script does the following:
# - creates pre-defined admin user account as specified in ENVs
# - automatically installs & enables pre-defined Functions and Tools
#   (see install_tools_and_functions() below)

set -e
: "${HEALTHZ_PORT:?missing HEALTHZ_PORT}"
: "${HEALTHZ_READY_FILE:?missing HEALTHZ_READY_FILE}"

start_healthz_server() {
    # poor mans healthz server
    echo "[Custom entrypoint] Starting :$HEALTHZ_PORT/healthz endpoint.."
    python3 /etc/healthz.py &

    rm -f "$HEALTHZ_READY_FILE"
}

apply_patches() {
    # PATCHES
    PATCH_DIR="/etc/patches"
    TARGET_DIR="/app"
    if [[ -d "$PATCH_DIR" ]]; then

        if [[ ! -d "$TARGET_DIR" ]]; then
            echo "Target directory does not exist: $TARGET_DIR" >&2
            exit 1
        fi

        shopt -s nullglob
        PATCHES=("$PATCH_DIR"/*.patch)
        shopt -u nullglob

        if [[ ${#PATCHES[@]} -eq 0 ]]; then
            echo "No patches found in $PATCH_DIR"
        else
            echo "${#PATCHES[@]} patches found"

            echo "Applying patches to $TARGET_DIR"
            echo "--------------------------------"

            for patch_file in "${PATCHES[@]}"; do
                echo "Applying patch: $(basename "$patch_file")"
                patch --forward -p0 -d "$TARGET_DIR" < "$patch_file" || true
            done

            echo "--------------------------------"
            echo "All patches applied successfully"
        fi

    fi
}

#copy_statics() {
#    cp -vrf /etc/static/* /app/backend/open_webui/static/
#}

start_app() {
    echo ""
    echo "[Custom entrypoint] Starting server..."
    /app/backend/start.sh &
}

wait_for_app() {
    # Wait for API to become available
    echo ""
    echo "[Custom entrypoint] Waiting for start..." &&
      while ! curl -s -o /dev/null "http://localhost:8080/health"; do
          sleep 2;
      done &&
      echo ""
    echo "[Custom entrypoint] started"
}

# Install one Tool or Function from its .py/.json pair mounted under /etc.
#
# Arguments:
#   $1 - kind: "tool" or "function"
#   $2 - id, matching the .py/.json file basenames (e.g. web_search)
#   $3 - name of the "<...>_ENABLED" env var gating this item, or "-"
#        to always install (e.g. TOOL_WEB_SEARCH_ENABLED,
#        FUNCTION_VIDEO_INJECT_ENABLED)
#   $4 - optional valves JSON, applied to the item right after creation
#
# Optional environment:
#   OWUI_ITEMS_DIR - directory holding the .py/.json pairs (default /etc)
#
# A failed create or missing files are reported but never abort the
# remaining items.
install_item() {
    local kind="$1"
    local id="$2"
    local enable_var="$3"
    local valves_json="$4"
    local dir="${OWUI_ITEMS_DIR:-/etc}"
    local api_kind="${kind}s" # tools | functions
    local py_file="${dir}/${id}.py"
    local json_file="${dir}/${id}.json"

    if [ "$enable_var" != "-" ] && [ "${!enable_var}" != "True" ]; then
        return 0
    fi

    if [ ! -f "$py_file" ] || [ ! -f "$json_file" ]; then
        echo ""
        echo "[Custom entrypoint] WARNING: ${kind} '${id}' enabled via ${enable_var} but ${py_file} or ${json_file} is missing. Skipping." >&2
        return 0
    fi

    echo ""
    echo "[Custom entrypoint] Installing ${kind} '${id}'..."

    local code data_raw create_response
    code=$(jq -Rs . < "$py_file")
    data_raw=$(jq --argjson content "${code}" '.content=$content' "$json_file")

    create_response=$(curl -s --connect-timeout 10 --max-time 30 -X POST "http://localhost:8080/api/v1/${api_kind}/create" \
      -H "Authorization: Bearer ${API_KEY}" \
      -H "Content-Type: application/json" \
      --data-raw "${data_raw}") || {
        echo "[Custom entrypoint] WARNING: ${kind} '${id}' install request failed" >&2
        return 0
    }

    local item_id
    item_id=$(echo "${create_response}" | jq -r '.id // empty')
    if [ -z "$item_id" ]; then
        echo "[Custom entrypoint] WARNING: ${kind} '${id}' install failed" >&2
        echo "${create_response}" >&2
        return 0
    fi

    echo "[Custom entrypoint] ${kind} '${id}' installed with id: ${item_id}"

    if [ -n "$valves_json" ]; then
        echo ""
        echo "[Custom entrypoint] Configuring the ${kind} '${id}' valves"
        curl -s --connect-timeout 10 --max-time 30 -X POST "http://localhost:8080/api/v1/${api_kind}/id/${item_id}/valves/update" \
          -H "Authorization: Bearer ${API_KEY}" \
          -H "Content-Type: application/json" \
          --data-raw "${valves_json}"
    fi

    if [ "$kind" == "function" ]; then
        echo ""
        echo "[Custom entrypoint] Enabling the ${kind} '${id}'"
        curl -s --connect-timeout 10 --max-time 30 -X POST "http://localhost:8080/api/v1/functions/id/${item_id}/toggle" \
          -H "Authorization: Bearer ${API_KEY}" \
          -H "Content-Type: application/json"

        echo ""
        echo "[Custom entrypoint] Enabling the ${kind} '${id}' globally"
        curl -s --connect-timeout 10 --max-time 30 -X POST "http://localhost:8080/api/v1/functions/id/${item_id}/toggle/global" \
          -H "Authorization: Bearer ${API_KEY}" \
          -H "Content-Type: application/json"
    fi
}

# Build the valves JSON for a Tool/Function id. Prints the valves JSON on
# stdout; returns non-zero to skip the item's install entirely.
valves_for() {
    case "$1" in
        roat_retrieval)
            jq -n --arg url "$ROAT_API_URL/api/v1/query" --arg key "$ROAT_API_KEY" \
              '{rag_service_url:$url,rag_service_api_key:$key}'
            ;;
        mediawiki_tool)
            if [ -z "${MEDIAWIKI_API_URL:-}" ]; then
                echo "[Custom entrypoint] WARNING: TOOL_MEDIAWIKI_ENABLED=True but MEDIAWIKI_API_URL is not set. Skipping MediaWiki Tool install." >&2
                return 1
            fi
            jq -n \
              --arg wiki "${MEDIAWIKI_API_URL}" \
              --arg user "${MEDIAWIKI_USERNAME:-}" \
              --arg pass "${MEDIAWIKI_PASSWORD:-}" \
              '{wiki_url:$wiki,username:$user,password:$pass}'
            ;;
        web_search)
            if [ -n "${TOOL_WEB_SEARCH_API_KEY:-}" ]; then
                jq -n --arg key "${TOOL_WEB_SEARCH_API_KEY}" '{tavily_api_key:$key}'
            else
                echo "[Custom entrypoint] TOOL_WEB_SEARCH_API_KEY not set. Set the tavily_api_key valve from Workspace -> Tools in the UI." >&2
            fi
            ;;
    esac
}

# Every bundled Tool/Function is provisioned here. Adding a new one means
# dropping its .py/.json pair into tools/ (or functions/), mounting it in
# compose.yaml, and adding a single entry to the list below:
#   "<tool|function> <id> <ENABLED env var or ->"
install_tools_and_functions() {
    local item kind id enable_var
    for item in \
        "tool roat_retrieval -" \
        "function video_inject FUNCTION_VIDEO_INJECT_ENABLED" \
        "function image_resizer FUNCTION_IMAGE_RESIZER_ENABLED" \
        "tool mediawiki_tool TOOL_MEDIAWIKI_ENABLED" \
        "tool web_search TOOL_WEB_SEARCH_ENABLED" \
        "tool get_sources TOOL_GET_SOURCES_ENABLED"
    do
        kind="${item%% *}"
        enable_var="${item##* }"
        id="$(echo "$item" | awk '{print $2}')"
        if ! item_valves=$(valves_for "$id"); then
            continue
        fi
        install_item "$kind" "$id" "$enable_var" "$item_valves"
    done
}

do_first_start() {
    echo ""
    echo "[Custom entrypoint] First start detected.."

    # Resolve and validate the custom workspace model file up front, before any
    # API calls are made, so a bad CREATE_CUSTOM_WORKSPACE_MODEL value stops
    # initialization cleanly with no side effects (signup, tool/provider setup, etc).
    if [ "$CREATE_CUSTOM_WORKSPACE_MODEL" == "True" ]; then
        WORKSPACE_MODEL_FILE="wikiteqcenturion.json"
    elif [ "$CREATE_CUSTOM_WORKSPACE_MODEL" == "False" ] || [ -z "$CREATE_CUSTOM_WORKSPACE_MODEL" ]; then
        WORKSPACE_MODEL_FILE=""
    else
        WORKSPACE_MODEL_FILE="$CREATE_CUSTOM_WORKSPACE_MODEL"
    fi

    if [ -n "$WORKSPACE_MODEL_FILE" ] && [ ! -f "/etc/owui-models/${WORKSPACE_MODEL_FILE}" ]; then
        echo "[Custom entrypoint] ERROR: CREATE_CUSTOM_WORKSPACE_MODEL is set to '${CREATE_CUSTOM_WORKSPACE_MODEL}' but /etc/owui-models/${WORKSPACE_MODEL_FILE} was not found" >&2
        exit 1
    fi

    echo ""
    echo "[Custom entrypoint] Sign up default admin user ..."
    SIGNUP_RESPONSE=$(curl -s -X POST "http://localhost:8080/api/v1/auths/signup" \
      -H "Content-Type: application/json" \
      --data-raw "{\"name\":\"$X_WEBUI_ADMIN_USER\", \"email\":\"$X_WEBUI_ADMIN_EMAIL\", \"password\":\"$X_WEBUI_ADMIN_PASS\"}")

    API_KEY=$(echo "${SIGNUP_RESPONSE}" | jq -r '.token')

    echo ""
    echo "[Custom entrypoint] Received API_KEY.."

    # Filter function replaced by ROAT search Tool — kept for reference
    echo "[Custom entrypoint] Disabling Direct Connections for regular users"
    curl -s -X POST "http://localhost:8080/api/v1/configs/direct_connections" \
      -H "Authorization: Bearer ${API_KEY}" \
      -H "Content-Type: application/json" \
      --data-raw '{"ENABLE_DIRECT_CONNECTIONS":false}'

    # extra
    if [ "$ENABLE_OPENAI_API" == "True" ]; then
        if [ ! -z "$OPENAI_DEFAULT_MODEL" ]; then
            echo ""
            echo "[Custom entrypoint] Setting default OpenAI model.."

            # setup openai provider
            echo ""
            echo "[Custom entrypoint] Adding provider"
            curl -s -X POST "http://localhost:8080/openai/config/update" \
              -H "Authorization: Bearer ${API_KEY}" \
              -H "Content-Type: application/json" \
              --data-raw "{\"ENABLE_OPENAI_API\":true,\"OPENAI_API_BASE_URLS\":[\"$OPENAI_API_BASE_URL\"],\"OPENAI_API_KEYS\":[\"$OPENAI_API_KEY\"],\"OPENAI_API_CONFIGS\":{\"0\":{\"enable\":true,\"tags\":[],\"prefix_id\":\"\",\"model_ids\":[\"$OPENAI_DEFAULT_MODEL\"]}}}"

            # set the model as default
            echo ""
            echo "[Custom entrypoint] Adding default model"
            curl -s -X POST "http://localhost:8080/api/v1/users/user/settings/update" \
              -H "Authorization: Bearer ${API_KEY}" \
              -H "Content-Type: application/json" \
              --data-raw "{\"ui\":{\"version\":\"0.6.5\",\"models\":[\"$OPENAI_DEFAULT_MODEL\"]}}"

            if [ -n "$WORKSPACE_MODEL_FILE" ]; then
                echo ""
                echo "[Custom entrypoint] Making default model private"
                curl -s -X POST "http://localhost:8080/api/v1/models/create" \
                  -H "Authorization: Bearer ${API_KEY}" \
                  -H "Content-Type: application/json" \
                  --data-raw "{\"id\":\"$OPENAI_DEFAULT_MODEL\",\"name\":\"$OPENAI_DEFAULT_MODEL\",\"base_model_id\":null,\"params\":{\"function_calling\":\"native\"},\"meta\":{\"profile_image_url\":\"/static/favicon.png\",\"description\":null,\"suggestion_prompts\":null,\"tags\":[],\"capabilities\":{\"vision\":false,\"citations\":true}},\"access_control\":{\"read\":{\"group_ids\":[],\"user_ids\":[]},\"write\":{\"group_ids\":[],\"user_ids\":[]}},\"is_active\":true}"

                echo ""
                echo "[Custom entrypoint] Creating Workspace model"
                WORKSPACE_MODEL_DATA=$(jq \
                  --arg base_model "$OPENAI_DEFAULT_MODEL" \
                  '.[0].base_model_id = $base_model | .[0]' \
                  "/etc/owui-models/${WORKSPACE_MODEL_FILE}")

                if [ -n "$OWUI_MODEL_PROMPT" ]; then
                    WORKSPACE_MODEL_DATA=$(echo "${WORKSPACE_MODEL_DATA}" | jq \
                      --arg prompt "$OWUI_MODEL_PROMPT" \
                      '.params.system = $prompt')
                fi

                if [ -n "$OWUI_MODEL_PROMPT_APPEND" ]; then
                    WORKSPACE_MODEL_DATA=$(echo "${WORKSPACE_MODEL_DATA}" | jq \
                      --arg append "$OWUI_MODEL_PROMPT_APPEND" \
                      '.params.system = (.params.system + "\n\n" + $append)')
                fi

                if [ "$TOOL_MEDIAWIKI_ENABLED" == "True" ]; then
                    WORKSPACE_MODEL_DATA=$(echo "${WORKSPACE_MODEL_DATA}" | jq \
                      '.meta.toolIds += ["mediawiki"]')
                fi
                curl -s -X POST "http://localhost:8080/api/v1/models/create" \
                  -H "Authorization: Bearer ${API_KEY}" \
                  -H "Content-Type: application/json" \
                  --data-raw "${WORKSPACE_MODEL_DATA}"
            else
                echo ""
                echo "[Custom entrypoint] Making default model public"
                curl -s -X POST "http://localhost:8080/api/v1/models/create" \
                  -H "Authorization: Bearer ${API_KEY}" \
                  -H "Content-Type: application/json" \
                  --data-raw "{\"id\":\"$OPENAI_DEFAULT_MODEL\",\"name\":\"$OPENAI_DEFAULT_MODEL\",\"base_model_id\":null,\"params\":{\"function_calling\":\"native\"},\"meta\":{\"profile_image_url\":\"/static/favicon.png\",\"description\":null,\"suggestion_prompts\":null,\"tags\":[],\"capabilities\":{\"vision\":false,\"citations\":true}},\"access_control\":null,\"is_active\":true}"
            fi

        fi
    fi

    # user setup
    if [ -n "$X_WEBUI_USER_EMAIL" ]; then
        echo ""
        echo "[Custom entrypoint] Creating first non-admin user"
        curl -s -X POST "http://localhost:8080/api/v1/auths/add" \
          -H "Authorization: Bearer ${API_KEY}" \
          -H "Content-Type: application/json" \
          --data-raw "{\"name\":\"$X_WEBUI_USER_NAME\",\"email\":\"$X_WEBUI_USER_EMAIL\",\"password\":\"$X_WEBUI_USER_PASS\",\"role\":\"user\"}"
    fi

    #disable ollama API
    if [ "$ENABLE_OLLAMA_API" == "false" ] || [ "$ENABLE_OLLAMA_API" == "False" ]; then
        echo ""
        echo "[Custom entrypoint] Disabling Ollama"
        curl -s -X POST "http://localhost:8080/ollama/config/update" \
          -H "Authorization: Bearer ${API_KEY}" \
          -H "Content-Type: application/json" \
          --data-raw "{\"ENABLE_OLLAMA_API\":false,\"OLLAMA_BASE_URLS\":[\"/ollama\"],\"OLLAMA_API_CONFIGS\":{\"0\":{}}}"
    fi

    # remove default suggestions
    echo ""
    echo "[Custom entrypoint] Removing default suggestions"
    curl -s -X POST "http://localhost:8080/api/v1/configs/suggestions" \
      -H "Authorization: Bearer ${API_KEY}" \
      -H "Content-Type: application/json" \
      --data-raw "{\"suggestions\":[]}"

    install_tools_and_functions

    touch /app/backend/data/.first_start
}


start_healthz_server
apply_patches

# this is required for speedy HF models download
pip install hf_xet

# Tool Python requirements must be installed here because runtime pip install
# (ENABLE_PIP_INSTALL_FRONTMATTER_REQUIREMENTS) is disabled — it is incompatible
# with multi-worker deployments and unreliable across container restarts.
pip install "mwclient>=0.10.1"
pip install "pyyaml>=6.0"
pip install "tavily-python>=0.5.0"
pip install "markdownify>=0.13.1"

start_app
wait_for_app
#copy_statics

if [ ! -f "/app/backend/data/.first_start" ]; then
    do_first_start
fi

touch "$HEALTHZ_READY_FILE"

# Keep the container running
wait
