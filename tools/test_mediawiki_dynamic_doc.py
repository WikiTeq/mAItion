"""
Standalone check for the dynamic, valve-dependent docstrings in mediawiki_tool.py
(MAIT-322): search_wiki/save_to_wiki's descriptions should include the configured
wiki_url, refreshed whenever valves are (re)assigned, without leaking across
Tools() instances or breaking OWUI's parameter-schema introspection.

Run inside the api container (which has pydantic/requests/pyyaml installed):
    docker exec -it <api_container> python /app/tools/test_mediawiki_dynamic_doc.py

Or on a bare host, stub the missing third-party deps first since this repo has
no test framework/dependencies installed outside the Docker image:
    pip install --user pydantic
    python3 tools/test_mediawiki_dynamic_doc.py
"""

import asyncio
import importlib.util
import inspect
import sys
from pathlib import Path
from typing import get_type_hints

MODULE_PATH = Path(__file__).parent / "mediawiki_tool.py"


def load_mediawiki_tool():
    spec = importlib.util.spec_from_file_location("mediawiki_tool", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main() -> int:
    mod = load_mediawiki_tool()
    Tools = mod.Tools
    total = 0
    failures = []

    def check(label: str, condition: bool):
        nonlocal total
        total += 1
        status = "PASS" if condition else "FAIL"
        print(f"{status}: {label}")
        if not condition:
            failures.append(label)

    # --- OWUI's get_functions_from_tool only filters names starting with "__"
    #     and excludes classes; anything else callable on the instance is
    #     treated as a separate, spurious tool. Replicate that filter exactly
    #     to guard against regressing to the private-"_impl"-method approach,
    #     which broke in live testing: dir() exposed _search_wiki_impl,
    #     _save_to_wiki_impl, etc. as extra tools with empty descriptions. ---
    t_unset = Tools()
    public_methods = sorted(
        n
        for n in dir(t_unset)
        if callable(getattr(t_unset, n)) and not n.startswith("__") and not inspect.isclass(getattr(t_unset, n))
    )
    check(
        "exactly search_wiki + save_to_wiki are visible to OWUI's tool introspection (no leaked helpers)",
        public_methods == ["save_to_wiki", "search_wiki"],
    )

    # --- unset wiki_url: no hint text is present, and re-fetching the
    #     docstring twice with the same (unset) valves is stable/idempotent.
    #     (Tools.search_wiki.__doc__ is not a meaningful baseline here: class-
    #     level access hits the descriptor's `instance is None` branch, which
    #     returns the raw function whose literal __doc__ was never set --  the
    #     doc text lives in the _dynamic_doc(...) decorator argument instead.) ---
    check(
        "unset wiki_url produces no dynamic hint text",
        "belongs to this wiki" not in t_unset.search_wiki.__doc__,
    )
    check(
        "docstring is stable across repeated access with unchanged valves",
        t_unset.search_wiki.__doc__ == t_unset.search_wiki.__doc__,
    )

    # --- whitespace-only wiki_url must be treated as unset, not as a
    #     configured (but broken) wiki -- regression test for a bug where
    #     _wiki_hint checked truthiness without stripping, so a value like
    #     "   " produced a misleading "connected" hint while every real
    #     call failed _parse_wiki_url's http://https:// validation. ---
    t_whitespace = Tools()
    t_whitespace.valves = Tools.Valves(wiki_url="   ")
    check(
        "whitespace-only wiki_url produces no dynamic hint text",
        "belongs to this wiki" not in t_whitespace.search_wiki.__doc__,
    )

    # --- setting wiki_url updates the docstring ---
    wiki_url = "https://wiki.example.com/w/api.php"
    t = Tools()
    t.valves = Tools.Valves(wiki_url=wiki_url)
    check("search_wiki docstring mentions configured wiki_url", wiki_url in t.search_wiki.__doc__)
    check("save_to_wiki docstring mentions configured wiki_url", wiki_url in t.save_to_wiki.__doc__)
    check(
        "hint lands before Args: section (not after Returns:)",
        t.search_wiki.__doc__.index(wiki_url) < t.search_wiki.__doc__.index("Args:"),
    )

    # --- reassigning valves updates the docstring again (simulates OWUI's
    #     per-request module.valves = module.Valves(**stored_valves)) ---
    other_url = "https://othersite.com/w/api.php"
    t.valves = Tools.Valves(wiki_url=other_url)
    check("docstring reflects the newly reassigned wiki_url", other_url in t.search_wiki.__doc__)
    check("docstring no longer mentions the previous wiki_url", wiki_url not in t.search_wiki.__doc__)

    # --- no cross-instance leakage: mutating t must not affect t2 ---
    t2 = Tools()
    check(
        "a second, freshly-constructed instance is unaffected by t's valves",
        other_url not in t2.search_wiki.__doc__,
    )

    # --- inspect.signature must match a normal bound method: no leaked `self`,
    #     __event_emitter__ preserved with its annotation/default (what OWUI's
    #     convert_function_to_pydantic_model relies on to build parameters) ---
    sig = inspect.signature(t.search_wiki)
    check("search_wiki signature has no leaked 'self' parameter", "self" not in sig.parameters)
    check("search_wiki signature retains 'query' parameter", "query" in sig.parameters)
    check(
        "search_wiki signature retains '__event_emitter__' parameter",
        "__event_emitter__" in sig.parameters,
    )

    hints = get_type_hints(t.search_wiki)
    check("get_type_hints resolves 'query' as str", hints.get("query") is str)
    check("get_type_hints resolves 'return' as str", hints.get("return") is str)

    # --- the wrapped method still forwards calls correctly to the real impl ---
    async def run_call():
        # wiki_url IS configured on `t`, so this exercises the real connection
        # path rather than the "not configured" short-circuit; expect it to
        # fail at connection (no real wiki at othersite.com) rather than at
        # argument handling, proving args/kwargs are forwarded correctly.
        return await t.search_wiki(query="test query")

    result = asyncio.run(run_call())
    check(
        "wrapped search_wiki forwards the call through to the real implementation",
        isinstance(result, str) and result.startswith("Error:") and "not configured" not in result,
    )

    if failures:
        print(f"\n{len(failures)}/{total} check(s) failed")
        return 1

    print(f"\nAll {total} checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
