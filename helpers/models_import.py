#!/usr/bin/env python3
"""Discover and import Open WebUI workspace models on first boot.

Commands (all read their settings from the environment):

  preflight  Validate every enabled model. Print the enabled names, one per
             line. Exit 1 with errors on stderr if anything is wrong. No API
             calls are made.
  auth       Sign up the admin user, or sign in when signup is closed. Print the
             API token. Exit 1 if no admin token can be obtained.
  import     Create each enabled workspace model. Needs API_KEY.
  core       Create the core (OPENAI_DEFAULT_MODEL) model record. Needs
             API_KEY. Pass --public or --private.

Layout: <MODELS_DIR>/<name>/model.json and <MODELS_DIR>/<name>/system.md.
A model is enabled by MODEL_IMPORT_ENABLE_<NAME> = true or 1, where <NAME> is
the directory name in upper case with "." and "-" changed to "_".

This module only creates models. It never updates or removes existing ones.
"""

import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

MODEL_FILE = "model.json"
PROMPT_FILE = "system.md"
FLAG_PREFIX = "MODEL_IMPORT_ENABLE_"
LEGACY_VAR = "CREATE_CUSTOM_WORKSPACE_MODEL"
LEGACY_MODEL = "wikiteqcenturion"
DIR_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
TRUE_VALUES = ("true", "1")
FALSE_VALUES = ("false", "0")
REQUEST_TIMEOUT = 30
# The API is local. Never send it through a proxy from the environment.
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class ModelsImportError(Exception):
    """A fatal, user-facing error. The message never contains secrets."""


def log(message):
    print(f"[models_import] {message}", file=sys.stderr)


def models_dir():
    return os.environ.get("MODELS_DIR", "/etc/owui-models")


def base_url():
    return os.environ.get("OWUI_BASE_URL", "http://localhost:8080").rstrip("/")


def flag_name(dir_name):
    return FLAG_PREFIX + dir_name.upper().replace(".", "_").replace("-", "_")


def parse_flag(var, value):
    """Return True, False or None (unset or empty)."""
    if value is None or value.strip() == "":
        return None
    lowered = value.strip().lower()
    if lowered in TRUE_VALUES:
        return True
    if lowered in FALSE_VALUES:
        return False
    raise ModelsImportError(f"{var} must be true, 1, false or 0 (got '{value}')")


def discover(root):
    """Return sorted [(dir_name, flag_name)]. Fails on bad names or collisions."""
    if not os.path.isdir(root):
        return []
    try:
        entries = sorted(os.listdir(root))
    except OSError as exc:
        raise ModelsImportError(f"{root} cannot be read ({type(exc).__name__})")
    found = {}
    for entry in entries:
        if not os.path.isdir(os.path.join(root, entry)):
            continue
        if not DIR_NAME_RE.match(entry):
            raise ModelsImportError(
                f"model directory '{entry}' has an invalid name "
                "(use ASCII letters, digits, '.', '_' and '-')"
            )
        flag = flag_name(entry)
        if flag in found:
            raise ModelsImportError(
                f"model directories '{found[flag]}' and '{entry}' map to the same flag {flag}"
            )
        found[flag] = entry
    return [(name, flag) for flag, name in sorted(found.items(), key=lambda i: i[1])]


def enabled_names(root, env):
    """Return the sorted names of enabled models, after the legacy alias."""
    models = discover(root)
    flags = {name: parse_flag(flag, env.get(flag)) for name, flag in models}

    legacy = env.get(LEGACY_VAR, "")
    legacy_state = parse_legacy(legacy)
    if legacy_state:
        log(
            f"{LEGACY_VAR} is deprecated. Use "
            f"{flag_name(LEGACY_MODEL)}=true instead."
        )
        if LEGACY_MODEL not in flags:
            raise ModelsImportError(
                f"{LEGACY_VAR}=True needs the bundled models/{LEGACY_MODEL}/ directory"
            )
        if flags[LEGACY_MODEL] is None:
            flags[LEGACY_MODEL] = True

    return [name for name, _ in models if flags[name]]


def parse_legacy(value):
    value = (value or "").strip()
    if value == "" or value.lower() in FALSE_VALUES:
        return False
    if value.lower() in TRUE_VALUES:
        return True
    raise ModelsImportError(
        f"{LEGACY_VAR}='{value}' is no longer supported. Put the model in "
        f"models/<name>/model.json with a system.md file and set "
        f"{FLAG_PREFIX}<NAME>=true"
    )


def read_prompt(path):
    try:
        with open(path, "r", encoding="utf-8", newline="") as handle:
            text = handle.read()
    except FileNotFoundError:
        raise ModelsImportError(f"{path} is missing")
    except (OSError, UnicodeDecodeError) as exc:
        raise ModelsImportError(f"{path} cannot be read ({type(exc).__name__})")
    # An editor adds one final newline. It is not part of the prompt.
    if text.endswith("\r\n"):
        text = text[:-2]
    elif text.endswith("\n"):
        text = text[:-1]
    if text.strip() == "":
        raise ModelsImportError(f"{path} is empty")
    return text


def read_manifest(path):
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except FileNotFoundError:
        raise ModelsImportError(f"{path} is missing")
    except (OSError, ValueError) as exc:
        raise ModelsImportError(f"{path} cannot be parsed ({type(exc).__name__})")
    validate_manifest(data, path)
    return data


def validate_manifest(data, path):
    if not isinstance(data, dict):
        raise ModelsImportError(f"{path} must contain one JSON object")
    for key in ("id", "name"):
        if not isinstance(data.get(key), str) or not data[key].strip():
            raise ModelsImportError(f"{path}: '{key}' must be a non-empty string")
    for key in ("params", "meta"):
        if not isinstance(data.get(key), dict):
            raise ModelsImportError(f"{path}: '{key}' must be an object")
    if "is_active" in data and not isinstance(data["is_active"], bool):
        raise ModelsImportError(f"{path}: 'is_active' must be true or false")
    if data.get("access_control") is not None and not isinstance(
        data["access_control"], dict
    ):
        raise ModelsImportError(f"{path}: 'access_control' must be null or an object")
    tool_ids = data["meta"].get("toolIds")
    if tool_ids is not None and (
        not isinstance(tool_ids, list) or not all(isinstance(t, str) for t in tool_ids)
    ):
        raise ModelsImportError(f"{path}: 'meta.toolIds' must be a list of strings")


def build_payload(manifest, system_prompt, env):
    """Return the API payload for one model. Does not change its arguments.

    Kept separate from the HTTP code so later reconciliation can reuse it.
    """
    default_model = env.get("OPENAI_DEFAULT_MODEL", "")
    payload = json.loads(json.dumps(manifest))
    payload["base_model_id"] = default_model

    prompt = system_prompt
    override = env.get("OWUI_MODEL_PROMPT", "")
    append = env.get("OWUI_MODEL_PROMPT_APPEND", "")
    if override:
        prompt = override
    if append:
        prompt = prompt + "\n\n" + append
    payload["params"]["system"] = prompt

    if env.get("MEDIAWIKI_API_URL"):
        for kind in ("READ", "WRITE"):
            if env.get(f"TOOL_MEDIAWIKI_{kind}_ENABLED") == "True":
                tool_id = f"mediawiki_{kind.lower()}"
                tool_ids = payload["meta"].setdefault("toolIds", [])
                if tool_id not in tool_ids:
                    tool_ids.append(tool_id)
    return payload


def core_payload(default_model, public):
    access = (
        None
        if public
        else {
            "read": {"group_ids": [], "user_ids": []},
            "write": {"group_ids": [], "user_ids": []},
        }
    )
    return {
        "id": default_model,
        "name": default_model,
        "base_model_id": None,
        "params": {"function_calling": "native"},
        "meta": {
            "profile_image_url": "/static/favicon.png",
            "description": None,
            "suggestion_prompts": None,
            "tags": [],
            "capabilities": {"vision": False, "citations": True},
        },
        "access_control": access,
        "is_active": True,
    }


def prepare(env):
    """Validate everything and return [(name, payload)] for enabled models."""
    root = models_dir()
    names = enabled_names(root, env)
    if not names:
        return []
    default_model = env.get("OPENAI_DEFAULT_MODEL", "")
    if env.get("ENABLE_OPENAI_API") != "True" or not default_model:
        raise ModelsImportError(
            "models are enabled but ENABLE_OPENAI_API is not True or "
            "OPENAI_DEFAULT_MODEL is empty"
        )
    prepared = []
    seen = {}
    for name in names:
        folder = os.path.join(root, name)
        manifest = read_manifest(os.path.join(folder, MODEL_FILE))
        prompt = read_prompt(os.path.join(folder, PROMPT_FILE))
        model_id = manifest["id"]
        if model_id == default_model:
            raise ModelsImportError(
                f"model '{name}' uses the id of OPENAI_DEFAULT_MODEL ('{model_id}')"
            )
        if model_id in seen:
            raise ModelsImportError(
                f"models '{seen[model_id]}' and '{name}' use the same id '{model_id}'"
            )
        seen[model_id] = name
        prepared.append((name, build_payload(manifest, prompt, env)))
    return prepared


class ApiError(Exception):
    def __init__(self, kind, message, status=None):
        super().__init__(message)
        self.kind = kind  # "http" or "transport"
        self.status = status


def request(method, path, token=None, body=None):
    """Send one bounded request. Return the decoded JSON body."""
    data = None
    headers = {"Accept": "application/json"}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(
        base_url() + path, data=data, headers=headers, method=method
    )
    try:
        with OPENER.open(req, timeout=REQUEST_TIMEOUT) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        raise ApiError(
            "http", f"{method} {path.split('?')[0]} returned HTTP {exc.code}", exc.code
        )
    except (urllib.error.URLError, OSError) as exc:
        raise ApiError(
            "transport", f"{method} {path.split('?')[0]} failed ({type(exc).__name__})"
        )
    try:
        return json.loads(raw)
    except ValueError:
        raise ApiError(
            "http", f"{method} {path.split('?')[0]} returned a body that is not JSON"
        )


def token_from(body):
    token = body.get("token") if isinstance(body, dict) else None
    if isinstance(token, str) and token.strip() and token != "null":
        return token
    return None


def authenticate(env):
    name = env.get("X_WEBUI_ADMIN_USER", "")
    email = env.get("X_WEBUI_ADMIN_EMAIL", "")
    password = env.get("X_WEBUI_ADMIN_PASS", "")
    if not email or not password:
        raise ModelsImportError(
            "X_WEBUI_ADMIN_EMAIL and X_WEBUI_ADMIN_PASS must be set"
        )

    token = None
    try:
        token = token_from(
            request(
                "POST",
                "/api/v1/auths/signup",
                body={"name": name, "email": email, "password": password},
            )
        )
    except ApiError as exc:
        if exc.kind == "transport":
            raise ModelsImportError(str(exc))
        log("signup was refused, trying sign in")
    if token is None:
        try:
            token = token_from(
                request(
                    "POST",
                    "/api/v1/auths/signin",
                    body={"email": email, "password": password},
                )
            )
        except ApiError as exc:
            raise ModelsImportError(f"admin sign in failed: {exc}")
    if token is None:
        raise ModelsImportError("no usable token in the sign in response")

    try:
        user = request("GET", "/api/v1/auths/", token=token)
    except ApiError as exc:
        raise ModelsImportError(f"token check failed: {exc}")
    if not isinstance(user, dict) or user.get("role") != "admin":
        raise ModelsImportError("the authenticated user is not an admin")
    return token


def create_model(token, payload):
    """Create one model. A model that already exists counts as imported."""
    model_id = payload["id"]
    try:
        body = request("POST", "/api/v1/models/create", token=token, body=payload)
    except ApiError as exc:
        if exc.kind == "http":
            if model_exists(token, model_id):
                log(f"model '{model_id}' already exists, left unchanged")
                return
        raise ModelsImportError(f"creating model '{model_id}' failed: {exc}")
    if not isinstance(body, dict) or body.get("id") != model_id:
        raise ModelsImportError(
            f"creating model '{model_id}' returned an unexpected response"
        )
    log(f"model '{model_id}' created")


def model_exists(token, model_id):
    query = urllib.parse.urlencode({"id": model_id})
    try:
        body = request("GET", f"/api/v1/models/model?{query}", token=token)
    except ApiError:
        return False
    return isinstance(body, dict) and body.get("id") == model_id


def require_token(env):
    token = env.get("API_KEY", "")
    if not token or token == "null":
        raise ModelsImportError("API_KEY is not set")
    return token


def main(argv, env):
    command = argv[0] if argv else ""
    if command == "preflight":
        for name, _ in prepare(env):
            print(name)
    elif command == "auth":
        print(authenticate(env))
    elif command == "import":
        token = require_token(env)
        for name, payload in prepare(env):
            log(f"importing '{name}'")
            create_model(token, payload)
    elif command == "core":
        if len(argv) != 2 or argv[1] not in ("--public", "--private"):
            raise ModelsImportError("usage: core --public|--private")
        token = require_token(env)
        default_model = env.get("OPENAI_DEFAULT_MODEL", "")
        if not default_model:
            raise ModelsImportError("OPENAI_DEFAULT_MODEL is empty")
        create_model(token, core_payload(default_model, argv[1] == "--public"))
    else:
        raise ModelsImportError("usage: models_import.py preflight|auth|import|core")


if __name__ == "__main__":
    try:
        main(sys.argv[1:], os.environ)
    except ModelsImportError as exc:
        print(f"[models_import] ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
