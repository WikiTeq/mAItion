#!/usr/bin/env python3
"""Syntax and wiring validation for the E2E harness.

Runs without Docker: checks Python syntax of the stub server, parses compose
files, verifies required files exist and are executable, confirms the
runner's journeys reference documented OpenWebUI endpoints, and checks that
run.sh credentials stay in sync with env.openwebui.e2e.
"""

import os
import py_compile
import sys

HERE = os.path.dirname(os.path.abspath(__file__))

REQUIRED = [
    "llm_stub.py",
    "compose.e2e.yaml",
    "run.sh",
    "env.openwebui.e2e",
    "env.rag.e2e",
    "__init__.py",
    "test_llm_stub.py",
]

# Journey endpoints documented in tests/e2e/README.md; run.sh must reference each.
REQUIRED_RUNNER_ENDPOINTS = (
    "/api/v1/auths/signin",
    "/openai/models",
    "/api/v1/chats/new",
    "/openai/chat/completions",
    "/api/v1/chats/",
    "/api/config",
    "/health",
)

CREDENTIAL_MAP = (
    ("E2E_ADMIN_EMAIL", "X_WEBUI_ADMIN_EMAIL"),
    ("E2E_ADMIN_PASS", "X_WEBUI_ADMIN_PASS"),
    ("E2E_USER_EMAIL", "X_WEBUI_USER_EMAIL"),
    ("E2E_USER_PASS", "X_WEBUI_USER_PASS"),
)


def check_python_syntax():
    for name in ("llm_stub.py", "validate.py", "test_llm_stub.py"):
        path = os.path.join(HERE, name)
        py_compile.compile(path, doraise=True)
        print(f"OK   python syntax: {name}")
    return True


def check_files():
    ok = True
    for name in REQUIRED:
        path = os.path.join(HERE, name)
        if not os.path.exists(path):
            print(f"FAIL missing file: {path}")
            ok = False
        else:
            print(f"OK   present: {name}")
    runner = os.path.join(HERE, "run.sh")
    if os.path.exists(runner) and not os.access(runner, os.X_OK):
        print("FAIL run.sh is not executable")
        ok = False
    return ok


def _parse_env_file(path):
    values = {}
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                key, _, value = line.partition("=")
                values[key.strip()] = value.strip()
    return values


def check_credential_sync():
    env_path = os.path.join(HERE, "env.openwebui.e2e")
    runner_path = os.path.join(HERE, "run.sh")
    env_vals = _parse_env_file(env_path)
    with open(runner_path) as fh:
        runner = fh.read()
    ok = True
    for runner_var, env_key in CREDENTIAL_MAP:
        expected = env_vals.get(env_key, "")
        needle = f'{runner_var}="{expected}"'
        if needle not in runner:
            print(f"FAIL run.sh {runner_var} does not match {env_key} in env.openwebui.e2e")
            ok = False
    if ok:
        print("OK   run.sh E2E credentials match env.openwebui.e2e")
    return ok


def check_runner_endpoints():
    runner = os.path.join(HERE, "run.sh")
    with open(runner) as fh:
        contents = fh.read()
    missing = [ep for ep in REQUIRED_RUNNER_ENDPOINTS if ep not in contents]
    if missing:
        print(f"FAIL run.sh missing documented journey endpoints: {missing}")
        return False
    print("OK   run.sh references documented journey endpoints")
    return True


def check_compose_yaml():
    try:
        import yaml
    except ImportError:
        print("SKIP yaml module unavailable; skipping compose parse check")
        return True

    with open(os.path.join(HERE, "compose.e2e.yaml")) as fh:
        doc = yaml.safe_load(fh)
    services = doc.get("services", {})
    expected = {"llm-stub", "openwebui", "api", "celery_worker", "celery_beat"}
    missing = expected - set(services)
    if missing:
        print(f"FAIL compose.e2e.yaml missing service keys: {sorted(missing)}")
        return False

    rag_services = ("api", "celery_worker", "celery_beat")
    for svc in rag_services:
        profiles = services[svc].get("profiles", [])
        if "rag" not in profiles:
            print(f"FAIL {svc} must include the 'rag' profile so it stays out of the smoke stack")
            return False

    stub = services.get("llm-stub", {})
    if not stub.get("healthcheck"):
        print("FAIL llm-stub must define a healthcheck")
        return False

    openwebui = services.get("openwebui", {})
    depends = openwebui.get("depends_on", {})
    llm_dep = depends.get("llm-stub") if isinstance(depends, dict) else None
    if not isinstance(llm_dep, dict) or llm_dep.get("condition") != "service_healthy":
        print("FAIL openwebui must depend on llm-stub with condition: service_healthy")
        return False

    print("OK   compose.e2e.yaml structure valid (llm-stub + profiled RAG services)")
    return True


def main():
    results = [
        check_python_syntax(),
        check_files(),
        check_credential_sync(),
        check_runner_endpoints(),
        check_compose_yaml(),
    ]
    if all(results):
        print("\nAll E2E harness pre-flight checks passed.")
        return 0
    print("\nE2E harness pre-flight FAILED", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
