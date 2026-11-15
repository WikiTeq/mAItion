import contextlib
import io
import json
import os
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "helpers"))
import tools_import as ti  # noqa: E402

SECRET = "s3cret-value"


class FakeOwui:
    """Minimal Open WebUI tool API."""

    def __init__(self):
        self.tools = {}
        self.valves = {}
        self.calls = []
        self.fail_create = {}  # tool id -> HTTP status
        self.drop_valves = False
        self.raw_create = None  # (status, body bytes) override

        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _send(self, status, body):
                data = body if isinstance(body, bytes) else json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(data)

            def _body(self):
                length = int(self.headers.get("Content-Length", 0))
                return json.loads(self.rfile.read(length) or b"null")

            def do_POST(self):
                body = self._body()
                owner.calls.append(("POST", self.path))
                if self.path == "/api/v1/tools/create":
                    if owner.raw_create:
                        return self._send(*owner.raw_create)
                    status = owner.fail_create.get(body["id"])
                    if status:
                        return self._send(status, {"detail": SECRET})
                    if body["id"] in owner.tools:
                        return self._send(400, {"detail": "ID taken"})
                    owner.tools[body["id"]] = body
                    return self._send(200, {"id": body["id"]})
                if self.path.endswith("/valves/update"):
                    tool_id = self.path.split("/")[5]
                    if not owner.drop_valves:
                        owner.valves[tool_id] = body
                    return self._send(200, body)
                self._send(404, {})

            def do_GET(self):
                owner.calls.append(("GET", self.path))
                parts = self.path.split("/")
                if self.path.endswith("/valves"):
                    return self._send(200, owner.valves.get(parts[5], {}))
                tool_id = parts[5]
                if tool_id in owner.tools:
                    return self._send(200, {"id": tool_id})
                self._send(404, {})

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def write_tool(root, tool_id, valves=None, manifest_id=None, source="print(1)\n"):
    path = os.path.join(root, tool_id)
    os.makedirs(path)
    manifest = {"id": manifest_id or tool_id, "name": tool_id.title(), "content": ""}
    if valves is not None:
        manifest["valves"] = valves
    with open(os.path.join(path, "tool.json"), "w") as f:
        json.dump(manifest, f)
    with open(os.path.join(path, "tool.py"), "w") as f:
        f.write(source)


SCHEMA = {
    "name": {"type": "str"},
    "count": {"type": "int"},
    "flag": {"type": "bool"},
    "mode": {"type": "literal", "values": ["a", "b"]},
}


class HelperCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = os.path.join(self.tmp.name, "tools")
        os.makedirs(self.root)
        self.out = os.path.join(self.tmp.name, "ensured.txt")
        self.owui = FakeOwui()
        self.addCleanup(self.owui.close)
        self.addCleanup(self.tmp.cleanup)

    def env(self, **extra):
        env = {
            "API_KEY": "tok-" + SECRET,
            "OWUI_URL": self.owui.url,
            "TOOLS_DIR": self.root,
            "ENSURED_TOOLS_FILE": self.out,
        }
        env.update(extra)
        return env

    def run_main(self, **extra):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = ti.main(self.env(**extra))
        return code, out.getvalue(), err.getvalue()

    def ensured(self):
        with open(self.out) as f:
            return f.read().split()


class TestDiscoveryAndFlags(HelperCase):
    def test_flags_select_tools(self):
        for name in ("one", "two", "three", "four"):
            write_tool(self.root, name)
        code, _, _ = self.run_main(
            TOOL_IMPORT_ONE="true",
            TOOL_IMPORT_TWO="1",
            TOOL_IMPORT_THREE="TRUE",
            TOOL_IMPORT_FOUR="false",
        )
        self.assertEqual(code, 0)
        self.assertEqual(self.ensured(), ["one", "three", "two"])
        self.assertNotIn("four", self.owui.tools)

    def test_unset_and_zero_not_imported(self):
        write_tool(self.root, "one")
        write_tool(self.root, "two")
        code, _, _ = self.run_main(TOOL_IMPORT_TWO="0")
        self.assertEqual(code, 0)
        self.assertEqual(self.ensured(), [])
        self.assertEqual(self.owui.calls, [])

    def test_underscore_id(self):
        write_tool(self.root, "media_read")
        self.run_main(TOOL_IMPORT_MEDIA_READ="true")
        self.assertEqual(self.ensured(), ["media_read"])

    def test_dir_without_files_ignored(self):
        os.makedirs(os.path.join(self.root, "empty"))
        write_tool(self.root, "one")
        self.assertEqual(ti.discover(self.root), ["one"])

    def test_payload_has_source_and_no_schema(self):
        write_tool(self.root, "one", valves=SCHEMA, source="x = 'é'\n")
        self.run_main(TOOL_IMPORT_ONE="true")
        sent = self.owui.tools["one"]
        self.assertEqual(sent["content"], "x = 'é'\n")
        self.assertNotIn("valves", sent)


class TestManifest(HelperCase):
    def test_id_mismatch(self):
        write_tool(self.root, "one", manifest_id="other")
        code, _, err = self.run_main(TOOL_IMPORT_ONE="true")
        self.assertEqual(code, 1)
        self.assertIn("differs", err)
        self.assertEqual(self.owui.calls, [])

    def test_malformed_json(self):
        write_tool(self.root, "one")
        with open(os.path.join(self.root, "one", "tool.json"), "w") as f:
            f.write("{bad")
        code, _, _ = self.run_main(TOOL_IMPORT_ONE="true")
        self.assertEqual(code, 1)

    def test_missing_name_or_id(self):
        write_tool(self.root, "one")
        with open(os.path.join(self.root, "one", "tool.json"), "w") as f:
            json.dump({"id": "one"}, f)
        self.assertEqual(self.run_main(TOOL_IMPORT_ONE="true")[0], 1)

    def test_bad_manifest_of_disabled_tool_still_fails_before_api(self):
        write_tool(self.root, "good")
        write_tool(self.root, "bad", manifest_id="x")
        code, _, _ = self.run_main(TOOL_IMPORT_GOOD="true")
        self.assertEqual(code, 1)
        self.assertEqual(self.owui.calls, [])

    def test_unknown_valve_type(self):
        write_tool(self.root, "one", valves={"a": {"type": "float"}})
        self.assertEqual(self.run_main(TOOL_IMPORT_ONE="true")[0], 1)


class TestValves(HelperCase):
    def test_typed_values(self):
        write_tool(self.root, "one", valves=SCHEMA)
        code, _, _ = self.run_main(
            TOOL_IMPORT_ONE="true",
            TOOL_VALVE_ONE_NAME="x",
            TOOL_VALVE_ONE_COUNT="0",
            TOOL_VALVE_ONE_FLAG="No",
            TOOL_VALVE_ONE_MODE="b",
        )
        self.assertEqual(code, 0)
        self.assertEqual(
            self.owui.valves["one"],
            {"name": "x", "count": 0, "flag": False, "mode": "b"},
        )

    def test_literal_value_is_stripped(self):
        write_tool(self.root, "one", valves=SCHEMA)
        self.run_main(TOOL_IMPORT_ONE="true", TOOL_VALVE_ONE_MODE="a \r")
        self.assertEqual(self.owui.valves["one"], {"mode": "a"})

    def test_empty_and_unset_omitted(self):
        write_tool(self.root, "one", valves=SCHEMA)
        self.run_main(
            TOOL_IMPORT_ONE="true", TOOL_VALVE_ONE_NAME="", TOOL_VALVE_ONE_COUNT=""
        )
        self.assertNotIn("one", self.owui.valves)
        self.assertEqual(self.ensured(), ["one"])

    def test_bad_values_stop_before_api(self):
        write_tool(self.root, "one", valves=SCHEMA)
        for var, value in (
            ("COUNT", "1.5"),
            ("FLAG", "maybe"),
            ("MODE", "c"),
        ):
            code, _, _ = self.run_main(
                TOOL_IMPORT_ONE="true", **{f"TOOL_VALVE_ONE_{var}": value}
            )
            self.assertEqual(code, 1, var)
        self.assertEqual(self.owui.calls, [])

    def test_valve_not_saved_detected(self):
        write_tool(self.root, "one", valves=SCHEMA)
        self.owui.drop_valves = True
        code, _, err = self.run_main(TOOL_IMPORT_ONE="true", TOOL_VALVE_ONE_NAME="x")
        self.assertEqual(code, 1)
        self.assertIn("not saved", err)


class TestLegacy(HelperCase):
    def setUp(self):
        super().setUp()
        write_tool(
            self.root,
            "roat_retrieval",
            valves={
                "rag_service_url": {"type": "str"},
                "rag_service_api_key": {"type": "str"},
            },
        )
        write_tool(self.root, "mediawiki_read", valves={"wiki_url": {"type": "str"}})

    def test_legacy_flag_and_valves(self):
        code, _, err = self.run_main(
            TOOL_MEDIAWIKI_READ_ENABLED="True",
            MEDIAWIKI_API_URL="https://w/api.php",
            TOOL_IMPORT_ROAT_RETRIEVAL="true",
            ROAT_API_URL="http://api:8000",
            ROAT_API_KEY=SECRET,
        )
        self.assertEqual(code, 0)
        self.assertEqual(
            self.owui.valves["mediawiki_read"], {"wiki_url": "https://w/api.php"}
        )
        self.assertEqual(
            self.owui.valves["roat_retrieval"],
            {
                "rag_service_url": "http://api:8000/api/v1/query",
                "rag_service_api_key": SECRET,
            },
        )
        self.assertIn("TOOL_MEDIAWIKI_READ_ENABLED", err)
        self.assertNotIn(SECRET, err)

    def test_new_name_wins_even_when_empty(self):
        code, _, _ = self.run_main(
            TOOL_IMPORT_MEDIAWIKI_READ="true",
            TOOL_MEDIAWIKI_READ_ENABLED="False",
            TOOL_VALVE_MEDIAWIKI_READ_WIKI_URL="",
            MEDIAWIKI_API_URL="https://old/api.php",
        )
        self.assertEqual(code, 0)
        self.assertEqual(self.ensured(), ["mediawiki_read"])
        self.assertNotIn("mediawiki_read", self.owui.valves)

    def test_new_flag_false_beats_legacy_true(self):
        self.run_main(
            TOOL_IMPORT_MEDIAWIKI_READ="false", TOOL_MEDIAWIKI_READ_ENABLED="True"
        )
        self.assertEqual(self.ensured(), [])

    def test_roat_has_no_legacy_flag(self):
        self.run_main(ROAT_API_URL="http://api:8000")
        self.assertEqual(self.ensured(), [])

    def test_new_roat_url_has_no_suffix_added(self):
        self.run_main(
            TOOL_IMPORT_ROAT_RETRIEVAL="true",
            TOOL_VALVE_ROAT_RETRIEVAL_RAG_SERVICE_URL="http://x/q",
        )
        self.assertEqual(
            self.owui.valves["roat_retrieval"], {"rag_service_url": "http://x/q"}
        )


class TestFailures(HelperCase):
    def setUp(self):
        super().setUp()
        write_tool(self.root, "one", valves=SCHEMA)
        write_tool(self.root, "two", valves=SCHEMA)

    def flags(self):
        return dict(TOOL_IMPORT_ONE="true", TOOL_IMPORT_TWO="true")

    def test_missing_token(self):
        code, _, _ = self.run_main(API_KEY="null", **self.flags())
        self.assertEqual(code, 1)
        self.assertEqual(self.owui.calls, [])

    def test_http_errors(self):
        for status in (401, 403, 500):
            self.owui.fail_create["one"] = status
            code, _, err = self.run_main(**self.flags())
            self.assertEqual(code, 1)
            self.assertIn(str(status), err)
            self.assertNotIn(SECRET, err)

    def test_non_json_and_wrong_shape(self):
        self.owui.raw_create = (200, b"<html>")
        self.assertEqual(self.run_main(**self.flags())[0], 1)
        self.owui.raw_create = (200, b'{"id": "other"}')
        self.assertEqual(self.run_main(**self.flags())[0], 1)
        self.owui.raw_create = (200, b'["x"]')
        self.assertEqual(self.run_main(**self.flags())[0], 1)

    def test_connection_refused(self):
        code, _, err = self.run_main(OWUI_URL="http://127.0.0.1:1", **self.flags())
        self.assertEqual(code, 1)
        self.assertNotIn(SECRET, err)

    def test_400_without_existing_tool_fails(self):
        self.owui.raw_create = (400, b'{"detail": "bad"}')
        self.assertEqual(self.run_main(**self.flags())[0], 1)

    def test_retry_after_partial_failure(self):
        self.owui.fail_create["two"] = 500
        code, _, _ = self.run_main(TOOL_VALVE_ONE_NAME="x", **self.flags())
        self.assertEqual(code, 1)
        self.assertFalse(os.path.exists(self.out))
        self.assertIn("one", self.owui.tools)

        self.owui.fail_create.clear()
        code, out, _ = self.run_main(TOOL_VALVE_ONE_NAME="x", **self.flags())
        self.assertEqual(code, 0)
        self.assertIn("already exists", out)
        self.assertEqual(self.ensured(), ["one", "two"])

    def test_secrets_not_in_output(self):
        code, out, err = self.run_main(TOOL_VALVE_ONE_NAME=SECRET, **self.flags())
        self.assertEqual(code, 0)
        self.assertNotIn(SECRET, out + err)


if __name__ == "__main__":
    unittest.main()
