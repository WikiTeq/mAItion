#!/usr/bin/env python3
"""Discover Open WebUI tools under a directory and import the enabled ones.

Layout: <tools_dir>/<tool-id>/tool.json (manifest) and tool.py (source).
A tool is imported when TOOL_IMPORT_<ID> is "true" or "1". Valves are read
from TOOL_VALVE_<ID>_<VALVE>. Empty or unset valves are left to the tool
defaults. Only stdlib modules are used, so the script runs in the stock
Open WebUI image.

Environment:
  API_KEY               admin bearer token (required)
  OWUI_URL              Open WebUI base URL (default http://localhost:8080)
  TOOLS_DIR             tools directory (default /etc/owui-tools)
  ENSURED_TOOLS_FILE    output file with one ensured tool ID per line
"""

import json
import os
import sys
import urllib.error
import urllib.request

TIMEOUT = 30
TRUE_VALUES = ("true", "1")
BOOL_TRUE = ("true", "1", "yes")
BOOL_FALSE = ("false", "0", "no")

# Pre-MAIT-257 variable names. Remove this block once deployments use the
# TOOL_IMPORT_* and TOOL_VALVE_* names.
LEGACY_FLAGS = {
    "mediawiki_read": "TOOL_MEDIAWIKI_READ_ENABLED",
    "mediawiki_write": "TOOL_MEDIAWIKI_WRITE_ENABLED",
    "web_search": "TOOL_WEB_SEARCH_ENABLED",
    "get_sources": "TOOL_GET_SOURCES_ENABLED",
}
_MEDIAWIKI_LEGACY = {
    "wiki_url": ("MEDIAWIKI_API_URL", None),
    "username": ("MEDIAWIKI_USERNAME", None),
    "password": ("MEDIAWIKI_PASSWORD", None),
}
LEGACY_VALVES = {
    "roat_retrieval": {
        "rag_service_url": ("ROAT_API_URL", lambda v: v + "/api/v1/query" if v else v),
        "rag_service_api_key": ("ROAT_API_KEY", None),
    },
    "mediawiki_read": _MEDIAWIKI_LEGACY,
    "mediawiki_write": _MEDIAWIKI_LEGACY,
    "web_search": {"tavily_api_key": ("TOOL_WEB_SEARCH_API_KEY", None)},
}


class ToolImportError(Exception):
    """A failure that must stop the bootstrap."""


def warn(message):
    print(f"[tools-import] WARNING: {message}", file=sys.stderr)


def log(message):
    print(f"[tools-import] {message}")


def discover(tools_dir):
    """Return sorted directory names that hold both tool.json and tool.py."""
    found = []
    for name in sorted(os.listdir(tools_dir)):
        path = os.path.join(tools_dir, name)
        if os.path.isfile(os.path.join(path, "tool.json")) and os.path.isfile(
            os.path.join(path, "tool.py")
        ):
            found.append(name)
    return found


def validate_schema(tool_id, schema):
    if not isinstance(schema, dict):
        raise ToolImportError(f"{tool_id}: 'valves' must be an object")
    for name, spec in schema.items():
        if not isinstance(spec, dict):
            raise ToolImportError(f"{tool_id}: valve '{name}' must be an object")
        vtype = spec.get("type")
        if vtype in ("str", "int", "bool"):
            continue
        if vtype == "literal":
            values = spec.get("values")
            if not isinstance(values, list) or not values:
                raise ToolImportError(f"{tool_id}: valve '{name}' needs 'values'")
            continue
        raise ToolImportError(f"{tool_id}: valve '{name}' has unknown type {vtype!r}")


def validate_manifest(path, dirname):
    """Load tool.json and check it. Return the manifest dict."""
    try:
        with open(path, encoding="utf-8") as handle:
            manifest = json.load(handle)
    except (OSError, ValueError) as exc:
        raise ToolImportError(f"{dirname}: cannot read manifest: {exc}")
    if not isinstance(manifest, dict):
        raise ToolImportError(f"{dirname}: manifest must be an object")
    for key in ("id", "name"):
        if not isinstance(manifest.get(key), str) or not manifest[key]:
            raise ToolImportError(f"{dirname}: manifest needs a string '{key}'")
    if manifest["id"] != dirname:
        raise ToolImportError(
            f"{dirname}: manifest id '{manifest['id']}' differs from directory name"
        )
    validate_schema(dirname, manifest.get("valves", {}))
    return manifest


def is_enabled(tool_id, env):
    """New flag first. Fall back to the legacy flag when the new one is unset."""
    flag = f"TOOL_IMPORT_{tool_id.upper()}"
    if flag in env:
        return env[flag].strip().lower() in TRUE_VALUES
    legacy = LEGACY_FLAGS.get(tool_id)
    if legacy and legacy in env:
        warn(f"{legacy} is deprecated. Use {flag}.")
        return env[legacy] == "True"
    return False


def convert(tool_id, name, spec, raw):
    vtype = spec["type"]
    if vtype == "str":
        return raw
    text = raw.strip()
    lowered = text.lower()
    if vtype == "int":
        try:
            return int(text)
        except ValueError:
            raise ToolImportError(f"{tool_id}: valve '{name}' must be an integer")
    if vtype == "bool":
        if lowered in BOOL_TRUE:
            return True
        if lowered in BOOL_FALSE:
            return False
        raise ToolImportError(f"{tool_id}: valve '{name}' must be true or false")
    if text in spec["values"]:
        return text
    raise ToolImportError(
        f"{tool_id}: valve '{name}' must be one of {', '.join(spec['values'])}"
    )


def collect_valves(tool_id, schema, env):
    """Return typed valve values. Unset and empty values are omitted."""
    valves = {}
    legacy_map = LEGACY_VALVES.get(tool_id, {})
    for name, spec in schema.items():
        var = f"TOOL_VALVE_{tool_id.upper()}_{name.upper()}"
        if var in env:
            raw = env[var]
        elif name in legacy_map and legacy_map[name][0] in env:
            legacy_var, transform = legacy_map[name]
            warn(f"{legacy_var} is deprecated. Use {var}.")
            raw = env[legacy_var]
            if transform:
                raw = transform(raw)
        else:
            continue
        if raw == "":
            continue
        valves[name] = convert(tool_id, name, spec, raw)
    return valves


def build_payload(manifest, source_code):
    payload = {k: v for k, v in manifest.items() if k != "valves"}
    payload["content"] = source_code
    return payload


class OwuiClient:
    def __init__(self, base_url, token):
        self.base_url = base_url.rstrip("/")
        self.token = token

    def request(self, method, path, body=None):
        """Return (status, parsed JSON). Never put the body or token in errors."""
        data = None if body is None else json.dumps(body).encode("utf-8")
        req = urllib.request.Request(self.base_url + path, data=data, method=method)
        req.add_header("Authorization", f"Bearer {self.token}")
        req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                status, raw = resp.status, resp.read()
        except urllib.error.HTTPError as exc:
            status, raw = exc.code, exc.read()
        except (urllib.error.URLError, OSError) as exc:
            raise ToolImportError(f"{method} {path} failed: {type(exc).__name__}")
        try:
            return status, json.loads(raw)
        except ValueError:
            if status >= 400:
                return status, None
            raise ToolImportError(f"{method} {path} returned a non-JSON response")


def ensure_tool(client, tool_id, payload, valves):
    status, body = client.request("POST", "/api/v1/tools/create", payload)
    if status == 200:
        if not isinstance(body, dict) or body.get("id") != tool_id:
            raise ToolImportError(f"{tool_id}: create returned an unexpected response")
        log(f"{tool_id}: created")
    elif status == 400:
        # Open WebUI answers 400 when the ID exists. Confirm with a GET.
        gstatus, gbody = client.request("GET", f"/api/v1/tools/id/{tool_id}")
        if gstatus != 200 or not isinstance(gbody, dict) or gbody.get("id") != tool_id:
            raise ToolImportError(f"{tool_id}: create failed with HTTP 400")
        log(f"{tool_id}: already exists, reusing")
    else:
        raise ToolImportError(f"{tool_id}: create failed with HTTP {status}")

    if not valves:
        return
    status, _ = client.request(
        "POST", f"/api/v1/tools/id/{tool_id}/valves/update", valves
    )
    if status != 200:
        raise ToolImportError(f"{tool_id}: valve update failed with HTTP {status}")
    status, current = client.request("GET", f"/api/v1/tools/id/{tool_id}/valves")
    if status != 200 or not isinstance(current, dict):
        raise ToolImportError(f"{tool_id}: valve read-back failed with HTTP {status}")
    bad = sorted(k for k, v in valves.items() if current.get(k) != v)
    if bad:
        raise ToolImportError(f"{tool_id}: valves not saved: {', '.join(bad)}")
    log(f"{tool_id}: valves set ({', '.join(sorted(valves))})")


def main(env=None):
    env = os.environ if env is None else env
    tools_dir = env.get("TOOLS_DIR", "/etc/owui-tools")
    out_file = env.get("ENSURED_TOOLS_FILE", "/tmp/ensured_tools.txt")
    token = env.get("API_KEY", "")
    if not token or token == "null":
        print("[tools-import] ERROR: API_KEY is missing", file=sys.stderr)
        return 1

    # Preflight: validate everything before the first API call.
    plan = []
    try:
        for name in discover(tools_dir):
            manifest = validate_manifest(
                os.path.join(tools_dir, name, "tool.json"), name
            )
            if not is_enabled(name, env):
                log(f"{name}: not enabled, skipping")
                continue
            valves = collect_valves(name, manifest.get("valves", {}), env)
            with open(os.path.join(tools_dir, name, "tool.py"), encoding="utf-8") as f:
                source = f.read()
            plan.append((name, build_payload(manifest, source), valves))

        client = OwuiClient(env.get("OWUI_URL", "http://localhost:8080"), token)
        ensured = []
        for name, payload, valves in plan:
            ensure_tool(client, name, payload, valves)
            ensured.append(name)
    except ToolImportError as exc:
        print(f"[tools-import] ERROR: {exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"[tools-import] ERROR: {type(exc).__name__}", file=sys.stderr)
        return 1

    with open(out_file, "w", encoding="utf-8") as handle:
        handle.write("".join(f"{t}\n" for t in ensured))
    log(f"done, {len(ensured)} tool(s) ensured")
    return 0


if __name__ == "__main__":
    sys.exit(main())
