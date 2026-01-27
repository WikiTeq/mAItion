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

    	JSON_TEMPLATE_PATH="/etc/function.json"
    	PYTHON_FILE_PATH="/etc/function.py"

    	# Read the Python file and escape special characters for JSON
    	PYTHON_CODE=$(jq -Rs . < "/etc/function.py")

    	# Read the template and replace placeholders
    	DATA_RAW=$(jq --argjson content "${PYTHON_CODE}" \
    				  '.content=$content' \
    				  "${JSON_TEMPLATE_PATH}")

    	echo ""
    	echo "[Custom entrypoint] Adding Pipe function to Open WebUI"
    	curl -s -X POST "http://localhost:8080/api/v1/functions/create" \
    	  -H "Authorization: Bearer ${API_KEY}" \
    	  -H "Content-Type: application/json" \
    	  --data-raw "${DATA_RAW}"

    	echo ""
    	echo "[Custom entrypoint] Configuring the function valves"
    	curl -s -X POST "http://localhost:8080/api/v1/functions/id/ragofalltrades/valves/update" \
    	  -H "Authorization: Bearer ${API_KEY}" \
    	  -H "Content-Type: application/json" \
    	  --data-raw '{"pipelines":["*"],"priority":null,"enabled":true,"rag_service_url":"http://api:8000/api/v1/query/","rag_service_api_key":"12345","rag_service_timeout":null,"top_k":null,"inject_context":null,"context_template":null}'

    	echo ""
    	echo "[Custom entrypoint] Enabling the function"
    	curl -s -X POST "http://localhost:8080/api/v1/functions/id/ragofalltrades/toggle" \
    	  -H "Authorization: Bearer ${API_KEY}" \
    	  -H "Content-Type: application/json"

    	echo ""
    	echo "[Custom entrypoint] Enabling the function globally"
    	curl -s -X POST "http://localhost:8080/api/v1/functions/id/ragofalltrades/toggle/global" \
    	  -H "Authorization: Bearer ${API_KEY}" \
    	  -H "Content-Type: application/json"

    	# extra
    	if [ "$ENABLE_OPENAI_API" == "True" ]; then
    		if [ ! -z "$OPENAI_DEFAULT_MODEL" ]; then
    			echo ""
            	echo "[Custom entrypoint] Setting default OpenAI model.."

    			curl -s -X POST "http://localhost:8080/openai/config/update" \
    		      -H "Authorization: Bearer ${API_KEY}" \
    		      -H "Content-Type: application/json" \
                  --data-raw "{\"ENABLE_OPENAI_API\":true,\"OPENAI_API_BASE_URLS\":[\"$OPENAI_API_BASE_URL\"],\"OPENAI_API_KEYS\":[\"$OPENAI_API_KEY\"],\"OPENAI_API_CONFIGS\":{\"0\":{\"enable\":true,\"tags\":[],\"prefix_id\":\"\",\"model_ids\":[\"$OPENAI_DEFAULT_MODEL\"]}}}"

                curl -s -X POST "http://localhost:8080/api/v1/users/user/settings/update" \
                  -H "Authorization: Bearer ${API_KEY}" \
                  -H "Content-Type: application/json" \
                  --data-raw "{\"ui\":{\"version\":\"0.6.5\",\"models\":[\"$OPENAI_DEFAULT_MODEL\"]}}"
    		fi
    	fi

    	touch /app/backend/data/.first_start
}

start_healthz_server
apply_patches
start_app
wait_for_app
#copy_statics

if [ ! -f "/app/backend/data/.first_start" ]; then
    do_first_start
fi

touch "$HEALTHZ_READY_FILE"

# Keep the container running
wait
