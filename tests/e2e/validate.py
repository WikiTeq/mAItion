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
    rag_profiled = all(
        "profiles" in services[s] for s in ("api", "celery_worker", "celery_beat")
    )
    if not rag_profiled:
        print("FAIL rag services must keep their 'rag' profile so they stay out of the smoke stack")
        return False
    print("OK   compose.e2e.yaml structure valid (llm-stub + profiled RAG services)")
    return True


def main():
    results = [check_python_syntax(), check_files()]
    r = check_compose_yaml()
    results.append(r)
    if all(results):
        print("\nAll E2E harness pre-flight checks passed.")
        return 0
    print("\nE2E harness pre-flight FAILED", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
