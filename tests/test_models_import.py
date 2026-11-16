"""Tests for helpers/models_import.py. Run: python3 -m unittest discover -s tests"""

import io
import json
import os
import sys
import tempfile
import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "helpers"))
import models_import as mi  # noqa: E402

BASE_ENV = {
    "ENABLE_OPENAI_API": "True",
    "OPENAI_DEFAULT_MODEL": "openai/gpt-4.1-nano",
}


def manifest(model_id, **extra):
    data = {
        "id": model_id,
        "name": model_id,
        "base_model_id": "ignored",
        "params": {"function_calling": "native", "system": "embedded"},
        "meta": {"toolIds": ["roat_retrieval"], "capabilities": {"citations": True}},
        "access_control": None,
        "is_active": True,
    }
    data.update(extra)
    return data


class ModelsDirCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = self._tmp.name
        self.addCleanup(self._tmp.cleanup)
        self._old_dir = os.environ.get("MODELS_DIR")
        os.environ["MODELS_DIR"] = self.root
        self.addCleanup(self._restore_dir)

    def _restore_dir(self):
        if self._old_dir is None:
            os.environ.pop("MODELS_DIR", None)
        else:
            os.environ["MODELS_DIR"] = self._old_dir

    def add_model(self, name, model_id=None, prompt="Prompt\n", data=None):
        folder = os.path.join(self.root, name)
        os.makedirs(folder, exist_ok=True)
        payload = data if data is not None else manifest(model_id or name)
        with open(os.path.join(folder, "model.json"), "w", encoding="utf-8") as f:
            json.dump(payload, f)
        if prompt is not None:
            with open(
                os.path.join(folder, "system.md"), "w", encoding="utf-8", newline=""
            ) as f:
                f.write(prompt)
        return folder

    def env(self, **extra):
        env = dict(BASE_ENV)
        env.update(extra)
        return env

    def run_main(self, args, env):
        out, err = io.StringIO(), io.StringIO()
        code = 0
        with redirect_stdout(out), redirect_stderr(err):
            try:
                mi.main(args, env)
            except mi.ModelsImportError as exc:
                code = 1
                err.write(str(exc))
        return code, out.getvalue(), err.getvalue()


class DiscoveryTests(ModelsDirCase):
    def test_flag_values(self):
        self.add_model("alpha")
        for value, expected in [
            ("true", ["alpha"]),
            ("TRUE", ["alpha"]),
            ("1", ["alpha"]),
            ("false", []),
            ("0", []),
            ("", []),
        ]:
            with self.subTest(value=value):
                env = self.env(MODEL_IMPORT_ENABLE_ALPHA=value)
                self.assertEqual(mi.enabled_names(self.root, env), expected)

    def test_unset_flag_skips(self):
        self.add_model("alpha")
        self.assertEqual(mi.enabled_names(self.root, self.env()), [])

    def test_invalid_flag_value_fails(self):
        self.add_model("alpha")
        with self.assertRaisesRegex(mi.ModelsImportError, "MODEL_IMPORT_ENABLE_ALPHA"):
            mi.enabled_names(self.root, self.env(MODEL_IMPORT_ENABLE_ALPHA="yes"))

    def test_multiple_models_sorted(self):
        for name in ("zeta", "alpha", "mid"):
            self.add_model(name)
        env = self.env(
            MODEL_IMPORT_ENABLE_ZETA="1",
            MODEL_IMPORT_ENABLE_ALPHA="true",
            MODEL_IMPORT_ENABLE_MID="0",
        )
        self.assertEqual(mi.enabled_names(self.root, env), ["alpha", "zeta"])

    def test_name_mapping(self):
        self.assertEqual(mi.flag_name("my-model.v2"), "MODEL_IMPORT_ENABLE_MY_MODEL_V2")

    def test_collision_fails(self):
        self.add_model("a-b")
        self.add_model("a_b")
        with self.assertRaisesRegex(mi.ModelsImportError, "same flag"):
            mi.discover(self.root)

    def test_case_collision_fails(self):
        self.add_model("Alpha")
        self.add_model("alpha")
        with self.assertRaisesRegex(mi.ModelsImportError, "same flag"):
            mi.discover(self.root)

    def test_invalid_dir_name_fails(self):
        os.makedirs(os.path.join(self.root, "bad name"))
        with self.assertRaisesRegex(mi.ModelsImportError, "invalid name"):
            mi.discover(self.root)

    def test_plain_files_ignored(self):
        with open(os.path.join(self.root, "old.json"), "w") as f:
            f.write("[]")
        self.assertEqual(mi.discover(self.root), [])

    def test_unreadable_directory_fails_cleanly(self):
        self.add_model("alpha")
        os.chmod(self.root, 0)
        self.addCleanup(os.chmod, self.root, 0o755)
        if os.access(self.root, os.R_OK):
            self.skipTest("running as a user that ignores file modes")
        with self.assertRaisesRegex(mi.ModelsImportError, "cannot be read"):
            mi.discover(self.root)

    def test_missing_directory_is_empty(self):
        self.assertEqual(mi.discover(os.path.join(self.root, "nope")), [])


class LegacyTests(ModelsDirCase):
    def setUp(self):
        super().setUp()
        self.add_model("wikiteqcenturion")

    def test_true_enables_bundled_model_when_flag_unset(self):
        env = self.env(CREATE_CUSTOM_WORKSPACE_MODEL="True")
        err = io.StringIO()
        with redirect_stderr(err):
            names = mi.enabled_names(self.root, env)
        self.assertEqual(names, ["wikiteqcenturion"])
        self.assertIn("deprecated", err.getvalue())

    def test_new_flag_wins(self):
        env = self.env(
            CREATE_CUSTOM_WORKSPACE_MODEL="True",
            MODEL_IMPORT_ENABLE_WIKITEQCENTURION="false",
        )
        with redirect_stderr(io.StringIO()):
            self.assertEqual(mi.enabled_names(self.root, env), [])

    def test_false_and_empty_do_nothing(self):
        for value in ("False", ""):
            env = self.env(CREATE_CUSTOM_WORKSPACE_MODEL=value)
            self.assertEqual(mi.enabled_names(self.root, env), [])

    def test_filename_value_fails(self):
        env = self.env(CREATE_CUSTOM_WORKSPACE_MODEL="wikiteqcenturion.json")
        with self.assertRaisesRegex(mi.ModelsImportError, "no longer supported"):
            mi.enabled_names(self.root, env)

    def test_alias_without_bundled_model_fails(self):
        os.rename(
            os.path.join(self.root, "wikiteqcenturion"),
            os.path.join(self.root, "other"),
        )
        env = self.env(CREATE_CUSTOM_WORKSPACE_MODEL="True")
        with redirect_stderr(io.StringIO()):
            with self.assertRaisesRegex(mi.ModelsImportError, "directory"):
                mi.enabled_names(self.root, env)


class PromptTests(ModelsDirCase):
    def prompt_for(self, text, **extra):
        self.add_model("alpha", prompt=text)
        env = self.env(MODEL_IMPORT_ENABLE_ALPHA="1", **extra)
        ((name, payload),) = mi.prepare(env)
        return payload["params"]["system"]

    def test_multiline_unicode_quotes(self):
        text = 'Line one "quoted"\n\n  indented → ünïcode 日本語\n'
        self.assertEqual(self.prompt_for(text + "\n"), text)

    def test_one_final_newline_removed(self):
        self.assertEqual(self.prompt_for("abc\n"), "abc")
        self.assertEqual(self.prompt_for("abc"), "abc")
        self.assertEqual(self.prompt_for("abc\n\n"), "abc\n")

    def test_crlf_final_newline_removed(self):
        self.assertEqual(self.prompt_for("abc\r\n"), "abc")

    def test_system_md_wins_over_embedded_prompt(self):
        self.assertEqual(self.prompt_for("from file\n"), "from file")

    def test_override_and_append(self):
        self.assertEqual(self.prompt_for("base\n", OWUI_MODEL_PROMPT="new"), "new")
        self.assertEqual(
            self.prompt_for("base\n", OWUI_MODEL_PROMPT_APPEND="more"), "base\n\nmore"
        )
        self.assertEqual(
            self.prompt_for(
                "base\n", OWUI_MODEL_PROMPT="new", OWUI_MODEL_PROMPT_APPEND="more"
            ),
            "new\n\nmore",
        )

    def test_overrides_apply_to_every_enabled_model(self):
        self.add_model("alpha", prompt="a\n")
        self.add_model("beta", prompt="b\n")
        env = self.env(
            MODEL_IMPORT_ENABLE_ALPHA="1",
            MODEL_IMPORT_ENABLE_BETA="1",
            OWUI_MODEL_PROMPT="shared",
        )
        prompts = [p["params"]["system"] for _, p in mi.prepare(env)]
        self.assertEqual(prompts, ["shared", "shared"])

    def test_missing_empty_unreadable_prompt_fails(self):
        env = self.env(MODEL_IMPORT_ENABLE_ALPHA="1")
        self.add_model("alpha", prompt=None)
        with self.assertRaisesRegex(mi.ModelsImportError, "missing"):
            mi.prepare(env)
        for empty in ("", "\n", "  \n\n"):
            self.add_model("alpha", prompt=empty)
            with self.assertRaisesRegex(mi.ModelsImportError, "empty"):
                mi.prepare(env)
        folder = self.add_model("alpha", prompt=None)
        with open(os.path.join(folder, "system.md"), "wb") as f:
            f.write(b"\xff\xfe\x00bad")
        with self.assertRaisesRegex(mi.ModelsImportError, "cannot be read"):
            mi.prepare(env)

    def test_prompt_text_not_in_errors(self):
        self.add_model("alpha", prompt="SECRET PROMPT")
        self.add_model("beta", model_id="alpha", prompt="SECRET PROMPT")
        env = self.env(MODEL_IMPORT_ENABLE_ALPHA="1", MODEL_IMPORT_ENABLE_BETA="1")
        with self.assertRaises(mi.ModelsImportError) as ctx:
            mi.prepare(env)
        self.assertNotIn("SECRET", str(ctx.exception))


class PayloadTests(ModelsDirCase):
    def test_preserves_settings_and_substitutes_base_model(self):
        self.add_model("alpha")
        env = self.env(MODEL_IMPORT_ENABLE_ALPHA="1")
        ((_, payload),) = mi.prepare(env)
        self.assertEqual(payload["base_model_id"], "openai/gpt-4.1-nano")
        self.assertEqual(payload["id"], "alpha")
        self.assertEqual(payload["params"]["function_calling"], "native")
        self.assertEqual(payload["meta"]["capabilities"], {"citations": True})
        self.assertIsNone(payload["access_control"])
        self.assertTrue(payload["is_active"])

    def test_access_control_and_inactive_are_kept(self):
        acl = {
            "read": {"group_ids": ["g"], "user_ids": []},
            "write": {"group_ids": [], "user_ids": []},
        }
        self.add_model(
            "alpha", data=manifest("alpha", access_control=acl, is_active=False)
        )
        env = self.env(MODEL_IMPORT_ENABLE_ALPHA="1")
        ((_, payload),) = mi.prepare(env)
        self.assertEqual(payload["access_control"], acl)
        self.assertFalse(payload["is_active"])

    def test_mediawiki_tool_ids(self):
        self.add_model("alpha")
        env = self.env(
            MODEL_IMPORT_ENABLE_ALPHA="1",
            MEDIAWIKI_API_URL="https://w/api.php",
            TOOL_MEDIAWIKI_READ_ENABLED="True",
            TOOL_MEDIAWIKI_WRITE_ENABLED="True",
        )
        ((_, payload),) = mi.prepare(env)
        self.assertEqual(
            payload["meta"]["toolIds"],
            ["roat_retrieval", "mediawiki_read", "mediawiki_write"],
        )

    def test_mediawiki_tool_ids_need_url_and_flag_and_no_duplicates(self):
        data = manifest("alpha")
        data["meta"]["toolIds"].append("mediawiki_read")
        self.add_model("alpha", data=data)
        env = self.env(
            MODEL_IMPORT_ENABLE_ALPHA="1", TOOL_MEDIAWIKI_READ_ENABLED="True"
        )
        ((_, payload),) = mi.prepare(env)
        self.assertEqual(
            payload["meta"]["toolIds"], ["roat_retrieval", "mediawiki_read"]
        )
        env["MEDIAWIKI_API_URL"] = "https://w/api.php"
        ((_, payload),) = mi.prepare(env)
        self.assertEqual(payload["meta"]["toolIds"].count("mediawiki_read"), 1)
        self.assertNotIn("mediawiki_write", payload["meta"]["toolIds"])

    def test_manifest_without_tool_ids_gets_mediawiki_ids(self):
        data = manifest("alpha")
        del data["meta"]["toolIds"]
        self.add_model("alpha", data=data)
        env = self.env(
            MODEL_IMPORT_ENABLE_ALPHA="1",
            MEDIAWIKI_API_URL="https://w/api.php",
            TOOL_MEDIAWIKI_WRITE_ENABLED="True",
        )
        ((_, payload),) = mi.prepare(env)
        self.assertEqual(payload["meta"]["toolIds"], ["mediawiki_write"])

    def test_build_payload_does_not_change_manifest(self):
        data = manifest("alpha")
        before = json.dumps(data, sort_keys=True)
        mi.build_payload(data, "p", self.env(OWUI_MODEL_PROMPT_APPEND="x"))
        self.assertEqual(json.dumps(data, sort_keys=True), before)

    def test_core_payload_visibility(self):
        self.assertIsNone(mi.core_payload("m", True)["access_control"])
        private = mi.core_payload("m", False)["access_control"]
        self.assertEqual(private["read"], {"group_ids": [], "user_ids": []})


class ManifestValidationTests(ModelsDirCase):
    def check(self, data, pattern):
        self.add_model("alpha", data=data)
        env = self.env(MODEL_IMPORT_ENABLE_ALPHA="1")
        with self.assertRaisesRegex(mi.ModelsImportError, pattern):
            mi.prepare(env)

    def test_array_is_rejected(self):
        self.check([manifest("alpha")], "one JSON object")

    def test_bad_fields(self):
        self.check(manifest("alpha", id=""), "'id'")
        self.check(manifest("alpha", name=5), "'name'")
        self.check(manifest("alpha", params=[]), "'params'")
        self.check(manifest("alpha", meta="x"), "'meta'")
        self.check(manifest("alpha", is_active="yes"), "is_active")
        self.check(manifest("alpha", access_control=[]), "access_control")
        bad_tools = manifest("alpha")
        bad_tools["meta"]["toolIds"] = "roat_retrieval"
        self.check(bad_tools, "toolIds")

    def test_invalid_json_and_missing_manifest(self):
        folder = self.add_model("alpha")
        with open(os.path.join(folder, "model.json"), "w") as f:
            f.write("{not json")
        env = self.env(MODEL_IMPORT_ENABLE_ALPHA="1")
        with self.assertRaisesRegex(mi.ModelsImportError, "cannot be parsed"):
            mi.prepare(env)
        os.remove(os.path.join(folder, "model.json"))
        with self.assertRaisesRegex(mi.ModelsImportError, "missing"):
            mi.prepare(env)

    def test_disabled_invalid_manifest_is_not_checked(self):
        folder = self.add_model("alpha")
        with open(os.path.join(folder, "model.json"), "w") as f:
            f.write("{not json")
        os.remove(os.path.join(folder, "system.md"))
        self.assertEqual(mi.prepare(self.env(MODEL_IMPORT_ENABLE_ALPHA="0")), [])
        self.assertEqual(mi.prepare(self.env()), [])

    def test_duplicate_ids_fail(self):
        self.add_model("alpha", model_id="same")
        self.add_model("beta", model_id="same")
        env = self.env(MODEL_IMPORT_ENABLE_ALPHA="1", MODEL_IMPORT_ENABLE_BETA="1")
        with self.assertRaisesRegex(mi.ModelsImportError, "same id"):
            mi.prepare(env)

    def test_id_equal_to_default_model_fails(self):
        self.add_model("alpha", model_id="openai/gpt-4.1-nano")
        env = self.env(MODEL_IMPORT_ENABLE_ALPHA="1")
        with self.assertRaisesRegex(mi.ModelsImportError, "OPENAI_DEFAULT_MODEL"):
            mi.prepare(env)

    def test_enabled_model_needs_openai_settings(self):
        self.add_model("alpha")
        for env in (
            {"MODEL_IMPORT_ENABLE_ALPHA": "1"},
            {"MODEL_IMPORT_ENABLE_ALPHA": "1", "ENABLE_OPENAI_API": "True"},
            {
                "MODEL_IMPORT_ENABLE_ALPHA": "1",
                "ENABLE_OPENAI_API": "False",
                "OPENAI_DEFAULT_MODEL": "m",
            },
        ):
            with self.assertRaisesRegex(mi.ModelsImportError, "ENABLE_OPENAI_API"):
                mi.prepare(env)

    def test_no_openai_settings_needed_when_nothing_enabled(self):
        self.add_model("alpha")
        self.assertEqual(mi.prepare({}), [])

    def test_preflight_prints_enabled_names(self):
        self.add_model("alpha")
        self.add_model("beta")
        env = self.env(MODEL_IMPORT_ENABLE_BETA="true")
        code, out, _ = self.run_main(["preflight"], env)
        self.assertEqual((code, out), (0, "beta\n"))

    def test_preflight_empty_for_empty_or_all_disabled(self):
        code, out, _ = self.run_main(["preflight"], self.env())
        self.assertEqual((code, out), (0, ""))
        self.add_model("alpha")
        code, out, _ = self.run_main(
            ["preflight"], self.env(MODEL_IMPORT_ENABLE_ALPHA="false")
        )
        self.assertEqual((code, out), (0, ""))


class FakeOwui:
    """A small Open WebUI stand-in. Records every request."""

    def __init__(self):
        self.requests = []
        self.models = {}
        self.signup = ("json", {"token": "tok-signup"})
        self.signin = ("json", {"token": "tok-signin"})
        self.role = "admin"
        self.create_status = {}  # model id -> forced HTTP status
        self.create_body = {}  # model id -> forced body
        self.lookup_status = None
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _send(self, status, body):
                raw = body if isinstance(body, bytes) else json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def _body(self):
                length = int(self.headers.get("Content-Length") or 0)
                return json.loads(self.rfile.read(length) or b"null")

            def _record(self, body=None):
                outer.requests.append(
                    (self.command, self.path, self.headers.get("Authorization"), body)
                )

            def do_POST(self):
                body = self._body()
                self._record(body)
                path = urlparse(self.path).path
                if path == "/api/v1/auths/signup":
                    return self._reply(outer.signup)
                if path == "/api/v1/auths/signin":
                    return self._reply(outer.signin)
                if path == "/api/v1/models/create":
                    model_id = body["id"]
                    if model_id in outer.create_status:
                        return self._send(
                            outer.create_status[model_id], {"detail": "x"}
                        )
                    if model_id in outer.create_body:
                        return self._send(200, outer.create_body[model_id])
                    if model_id in outer.models:
                        return self._send(
                            401, {"detail": "This model id is already registered"}
                        )
                    outer.models[model_id] = body
                    return self._send(200, body)
                self._send(404, {})

            def do_GET(self):
                self._record()
                parsed = urlparse(self.path)
                if parsed.path == "/api/v1/auths/":
                    return self._send(200, {"role": outer.role})
                if parsed.path == "/api/v1/models/model":
                    if outer.lookup_status:
                        return self._send(outer.lookup_status, {"detail": "x"})
                    model_id = parse_qs(parsed.query)["id"][0]
                    if model_id in outer.models:
                        return self._send(200, outer.models[model_id])
                    return self._send(401, {"detail": "not found"})
                self._send(404, {})

            def _reply(self, spec):
                kind, value = spec
                if kind == "json":
                    return self._send(200, value)
                if kind == "raw":
                    return self._send(200, value)
                return self._send(value, {"detail": "x"})

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def url(self):
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def start(self):
        self.thread.start()

    def stop(self):
        self.server.shutdown()
        self.server.server_close()

    def paths(self, method=None):
        return [p for m, p, _, _ in self.requests if method in (None, m)]


class ApiTests(ModelsDirCase):
    def setUp(self):
        super().setUp()
        self.owui = FakeOwui()
        self.owui.start()
        self.addCleanup(self.owui.stop)
        self.set_base(self.owui.url)
        self.addCleanup(os.environ.pop, "OWUI_BASE_URL", None)
        self.add_model("alpha")
        self.add_model("beta")

    def set_base(self, url):
        os.environ["OWUI_BASE_URL"] = url

    def env(self, **extra):
        env = super().env(
            X_WEBUI_ADMIN_USER="Admin",
            X_WEBUI_ADMIN_EMAIL="a@example.com",
            X_WEBUI_ADMIN_PASS="pw",
            API_KEY="tok",
        )
        env.update(extra)
        return env

    # --- import
    def test_import_creates_only_enabled_models(self):
        env = self.env(MODEL_IMPORT_ENABLE_ALPHA="1", MODEL_IMPORT_ENABLE_BETA="0")
        code, _, _ = self.run_main(["import"], env)
        self.assertEqual(code, 0)
        self.assertEqual(list(self.owui.models), ["alpha"])
        self.assertEqual(
            self.owui.models["alpha"]["base_model_id"], "openai/gpt-4.1-nano"
        )
        for _, _, auth, _ in self.owui.requests:
            self.assertEqual(auth, "Bearer tok")

    def test_import_multiple_models_in_order(self):
        env = self.env(MODEL_IMPORT_ENABLE_ALPHA="1", MODEL_IMPORT_ENABLE_BETA="1")
        self.run_main(["import"], env)
        self.assertEqual(list(self.owui.models), ["alpha", "beta"])

    def test_disabled_models_send_no_requests(self):
        code, _, _ = self.run_main(["import"], self.env())
        self.assertEqual(code, 0)
        self.assertEqual(self.owui.requests, [])

    def test_invalid_manifest_sends_no_requests(self):
        folder = self.add_model("alpha")
        os.remove(os.path.join(folder, "system.md"))
        env = self.env(MODEL_IMPORT_ENABLE_ALPHA="1", MODEL_IMPORT_ENABLE_BETA="1")
        code, _, err = self.run_main(["import"], env)
        self.assertEqual(code, 1)
        self.assertEqual(self.owui.requests, [])
        self.assertIn("system.md", err)

    def test_import_needs_api_key(self):
        for key in ("", "null"):
            env = self.env(MODEL_IMPORT_ENABLE_ALPHA="1", API_KEY=key)
            code, _, _ = self.run_main(["import"], env)
            self.assertEqual(code, 1)
        self.assertEqual(self.owui.requests, [])

    def test_http_errors_fail(self):
        for status in (400, 401, 403, 500):
            with self.subTest(status=status):
                self.owui.create_status["alpha"] = status
                self.owui.requests.clear()
                env = self.env(MODEL_IMPORT_ENABLE_ALPHA="1")
                code, _, err = self.run_main(["import"], env)
                self.assertEqual(code, 1)
                self.assertIn("alpha", err)

    def test_unexpected_response_shapes_fail(self):
        for body in (b"not json", [], {"id": "other"}, {}, None):
            with self.subTest(body=body):
                self.owui.create_body["alpha"] = body
                env = self.env(MODEL_IMPORT_ENABLE_ALPHA="1")
                code, _, _ = self.run_main(["import"], env)
                self.assertEqual(code, 1)

    def test_unreachable_server_fails(self):
        self.set_base("http://127.0.0.1:9")
        env = self.env(MODEL_IMPORT_ENABLE_ALPHA="1")
        code, _, err = self.run_main(["import"], env)
        self.assertEqual(code, 1)
        self.assertNotIn("tok", err)

    def test_partial_failure_then_retry(self):
        env = self.env(MODEL_IMPORT_ENABLE_ALPHA="1", MODEL_IMPORT_ENABLE_BETA="1")
        self.owui.create_status["beta"] = 500
        code, _, _ = self.run_main(["import"], env)
        self.assertEqual(code, 1)
        self.assertEqual(list(self.owui.models), ["alpha"])
        # Retry: alpha already exists, beta now works.
        del self.owui.create_status["beta"]
        before = json.dumps(self.owui.models["alpha"], sort_keys=True)
        code, _, _ = self.run_main(["import"], env)
        self.assertEqual(code, 0)
        self.assertEqual(list(self.owui.models), ["alpha", "beta"])
        self.assertEqual(json.dumps(self.owui.models["alpha"], sort_keys=True), before)

    def test_existing_model_is_left_unchanged_and_never_updated(self):
        self.owui.models["alpha"] = {"id": "alpha", "name": "kept"}
        env = self.env(MODEL_IMPORT_ENABLE_ALPHA="1")
        code, _, _ = self.run_main(["import"], env)
        self.assertEqual(code, 0)
        self.assertEqual(self.owui.models["alpha"]["name"], "kept")
        self.assertEqual(
            {m for m in (r[0] for r in self.owui.requests)}, {"POST", "GET"}
        )
        self.assertNotIn("update", " ".join(self.owui.paths()))

    def test_real_401_is_not_treated_as_existing(self):
        self.owui.create_status["alpha"] = 401  # permission failure, model absent
        env = self.env(MODEL_IMPORT_ENABLE_ALPHA="1")
        code, _, _ = self.run_main(["import"], env)
        self.assertEqual(code, 1)
        self.assertNotIn("alpha", self.owui.models)

    def test_lookup_failure_is_not_treated_as_existing(self):
        self.owui.models["alpha"] = {"id": "alpha"}
        self.owui.lookup_status = 500
        env = self.env(MODEL_IMPORT_ENABLE_ALPHA="1")
        code, _, _ = self.run_main(["import"], env)
        self.assertEqual(code, 1)

    # --- core
    def test_core_public_and_private(self):
        env = self.env()
        self.run_main(["core", "--public"], env)
        self.assertIsNone(self.owui.models["openai/gpt-4.1-nano"]["access_control"])
        self.owui.models.clear()
        self.run_main(["core", "--private"], env)
        self.assertIsNotNone(self.owui.models["openai/gpt-4.1-nano"]["access_control"])

    def test_core_existing_record_keeps_visibility(self):
        self.owui.models["openai/gpt-4.1-nano"] = {
            "id": "openai/gpt-4.1-nano",
            "access_control": None,
        }
        code, _, _ = self.run_main(["core", "--private"], self.env())
        self.assertEqual(code, 0)
        self.assertIsNone(self.owui.models["openai/gpt-4.1-nano"]["access_control"])

    def test_core_usage_errors(self):
        self.assertEqual(self.run_main(["core"], self.env())[0], 1)
        self.assertEqual(self.run_main(["core", "--x"], self.env())[0], 1)
        self.assertEqual(
            self.run_main(["core", "--public"], self.env(OPENAI_DEFAULT_MODEL=""))[0], 1
        )

    # --- auth
    def test_signup_token_used(self):
        code, out, _ = self.run_main(["auth"], self.env())
        self.assertEqual((code, out), (0, "tok-signup\n"))
        self.assertNotIn("/api/v1/auths/signin", self.owui.paths())

    def test_signup_refused_falls_back_to_signin(self):
        self.owui.signup = ("status", 403)
        code, out, _ = self.run_main(["auth"], self.env())
        self.assertEqual((code, out), (0, "tok-signin\n"))

    def test_bad_tokens_are_not_accepted(self):
        for bad in (
            {"token": None},
            {"token": "null"},
            {"token": ""},
            {"token": 5},
            {},
            [],
        ):
            with self.subTest(bad=bad):
                self.owui.signup = ("json", bad)
                self.owui.signin = ("json", bad)
                code, out, _ = self.run_main(["auth"], self.env())
                self.assertEqual(code, 1)
                self.assertEqual(out, "")

    def test_signin_rejected(self):
        self.owui.signup = ("status", 403)
        self.owui.signin = ("status", 400)
        code, _, err = self.run_main(["auth"], self.env())
        self.assertEqual(code, 1)
        self.assertIn("sign in failed", err)

    def test_non_admin_token_rejected(self):
        self.owui.role = "user"
        code, _, err = self.run_main(["auth"], self.env())
        self.assertEqual(code, 1)
        self.assertIn("not an admin", err)

    def test_signup_transport_error_is_not_retried_as_signin(self):
        self.set_base("http://127.0.0.1:9")
        code, _, err = self.run_main(["auth"], self.env())
        self.assertEqual(code, 1)
        self.assertIn("failed", err)

    def test_password_not_in_output(self):
        self.owui.signup = ("status", 403)
        self.owui.signin = ("status", 400)
        _, out, err = self.run_main(["auth"], self.env(X_WEBUI_ADMIN_PASS="s3cret-pw"))
        self.assertNotIn("s3cret-pw", out + err)

    def test_missing_credentials(self):
        code, _, _ = self.run_main(["auth"], self.env(X_WEBUI_ADMIN_PASS=""))
        self.assertEqual(code, 1)
        self.assertEqual(self.owui.requests, [])

    def test_unknown_command(self):
        self.assertEqual(self.run_main(["nope"], self.env())[0], 1)


if __name__ == "__main__":
    unittest.main()
