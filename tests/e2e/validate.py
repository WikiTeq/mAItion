#!/usr/bin/env python3
"""Syntax and wiring validation for the E2E harness.

Runs without Docker: checks Python syntax of the stub server, parses compose
files, verifies required files exist and are executable, and confirms the
runner's journeys reference real OpenWebUI endpoints. Intended as a fast
pre-flight for environments without a Docker daemon.
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
]


def check_python_syntax():
    path = os.path.join(HERE, "llm_stub.py")
    py_compile.compile(path, doraise=True)
    print(f"OK   python syntax: {path}")
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
        print(f"FAIL run.sh is not executable")
        ok = False
    return ok


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
    results = [check_python_syntax(), check_files(), check_runner_endpoints()]
    r = check_compose_yaml()
    results.append(r)
    if all(results):
        print("\nAll E2E harness pre-flight checks passed.")
        return 0
    print("\nE2E harness pre-flight FAILED", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
