#!/bin/bash

# Inspired by https://github.com/open-webui/open-webui/discussions/8955#discussioncomment-12548747
# this custom entrypoint script does the following:
# - creates pre-defined admin user account as specified in ENVs
# - creates a pre-defined Function

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

do_first_start() {
    echo ""
    echo "[Custom entrypoint] First start detected.."

    echo ""
    echo "[Custom entrypoint] Sign up default admin user ..."
    SIGNUP_RESPONSE=$(curl -s -X POST "http://localhost:8080/api/v1/auths/signup" \
      -H "Content-Type: application/json" \
      --data-raw "{\"name\":\"$X_WEBUI_ADMIN_USER\", \"email\":\"$X_WEBUI_ADMIN_EMAIL\", \"password\":\"$X_WEBUI_ADMIN_PASS\"}")

    API_KEY=$(echo "${SIGNUP_RESPONSE}" | jq -r '.token')

    echo ""
    echo "[Custom entrypoint] Received API_KEY.."

    # Filter function replaced by ROAT search Tool — kept for reference
    # JSON_TEMPLATE_PATH="/etc/function.json"
    # PYTHON_FILE_PATH="/etc/function.py"
    #
    # PYTHON_CODE=$(jq -Rs . < "/etc/function.py")
    # DATA_RAW=$(jq --argjson content "${PYTHON_CODE}" \
    #   '.content=$content' \
    #   "${JSON_TEMPLATE_PATH}")
    #
    # echo ""
    # echo "[Custom entrypoint] Adding Pipe function to Open WebUI"
    # curl -s -X POST "http://localhost:8080/api/v1/functions/create" \
    #   -H "Authorization: Bearer ${API_KEY}" \
    #   -H "Content-Type: application/json" \
    #   --data-raw "${DATA_RAW}"
    #
    # echo ""
    # echo "[Custom entrypoint] Configuring the function valves"
    # curl -s -X POST "http://localhost:8080/api/v1/functions/id/ragofalltrades/valves/update" \
    #   -H "Authorization: Bearer ${API_KEY}" \
    #   -H "Content-Type: application/json" \
    #   --data-raw "{\"pipelines\":[\"*\"],\"priority\":null,\"enabled\":true,\"rag_service_url\":\"$ROAT_API_URL/api/v1/query/\",\"rag_service_api_key\":\"$ROAT_API_KEY\",\"rag_service_timeout\":null,\"top_k\":null,\"inject_context\":null,\"context_template\":null}"
    #
    # echo ""
    # echo "[Custom entrypoint] Enabling the function"
    # curl -s -X POST "http://localhost:8080/api/v1/functions/id/ragofalltrades/toggle" \
    #   -H "Authorization: Bearer ${API_KEY}" \
    #   -H "Content-Type: application/json"
    #
    # echo ""
    # echo "[Custom entrypoint] Enabling the function globally"
    # curl -s -X POST "http://localhost:8080/api/v1/functions/id/ragofalltrades/toggle/global" \
    #   -H "Authorization: Bearer ${API_KEY}" \
    #   -H "Content-Type: application/json"

    TOOL_PYTHON_CODE=$(jq -Rs . < "/etc/roat_retrieval.py")
    TOOL_DATA_RAW=$(jq --argjson content "${TOOL_PYTHON_CODE}" \
      '.content=$content' \
      "/etc/roat_retrieval.json")

    echo ""
    echo "[Custom entrypoint] Installing ROAT search Tool"
    curl -s -X POST "http://localhost:8080/api/v1/tools/create" \
      -H "Authorization: Bearer ${API_KEY}" \
      -H "Content-Type: application/json" \
      --data-raw "${TOOL_DATA_RAW}"

    echo ""
    echo "[Custom entrypoint] Configuring the tool valves"
    curl -s -X POST "http://localhost:8080/api/v1/tools/id/roat_retrieval/valves/update" \
      -H "Authorization: Bearer ${API_KEY}" \
      -H "Content-Type: application/json" \
      --data-raw "{\"rag_service_url\":\"$ROAT_API_URL/api/v1/query/\",\"rag_service_api_key\":\"$ROAT_API_KEY\"}"

    echo ""
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

            # rename the model and enable native functions calling
            echo ""
            echo "[Custom entrypoint] Renaming the model"
            curl -s -X POST "http://localhost:8080/api/v1/models/create" \
              -H "Authorization: Bearer ${API_KEY}" \
              -H "Content-Type: application/json" \
              --data-raw "{\"meta\":{\"profile_image_url\":\"/static/favicon.png\",\"description\":null,\"suggestion_prompts\":null,\"tags\":[],\"capabilities\":{\"vision\":false,\"citations\":true}},\"id\":\"$OPENAI_DEFAULT_MODEL\",\"name\":\"wikiteq/centurion\",\"base_model_id\":null,\"params\":{ \"function_calling\": \"native\" },\"access_control\":null,\"owned_by\":\"openai\",\"openai\":{\"id\":\"$OPENAI_DEFAULT_MODEL\",\"name\":\"wikiteq/centurion\",\"owned_by\":\"openai\",\"openai\":{\"id\":\"$OPENAI_DEFAULT_MODEL\"},\"urlIdx\":0},\"urlIdx\":0,\"is_active\":true}"

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

    touch /app/backend/data/.first_start
}

start_healthz_server
apply_patches

# this is required for speedy HF models download
pip install hf_xet

start_app
wait_for_app
#copy_statics

if [ ! -f "/app/backend/data/.first_start" ]; then
    do_first_start
fi

touch "$HEALTHZ_READY_FILE"

# Keep the container running
wait
