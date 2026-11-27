#!/usr/bin/env python3
"""Unit tests for the deterministic LLM stub (no Docker required)."""

import json
import os
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

# Import after setting port so the module binds correctly when started.
os.environ["LLM_STUB_PORT"] = "0"

import llm_stub  # noqa: E402


class LlmStubHandlerTests(unittest.TestCase):
    def setUp(self):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), llm_stub.Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def _get(self, path):
        with urllib.request.urlopen(f"{self.base}{path}", timeout=5) as resp:
            return resp.status, json.loads(resp.read().decode())

    def _post(self, path, payload):
        data = json.dumps(payload).encode()
        req = urllib.request.Request(
            f"{self.base}{path}",
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read().decode())

    def test_health(self):
        status, body = self._get("/health")
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "ok")

    def test_models_lists_stub_model(self):
        status, body = self._get("/v1/models")
        self.assertEqual(status, 200)
        ids = [m["id"] for m in body.get("data", [])]
        self.assertIn(llm_stub.MODEL_ID, ids)

    def test_chat_completion_echoes_user_text(self):
        status, body = self._post(
            "/v1/chat/completions",
            {"model": "stub-model", "messages": [{"role": "user", "content": "hello"}]},
        )
        self.assertEqual(status, 200)
        content = body["choices"][0]["message"]["content"]
        self.assertTrue(content.startswith(llm_stub.FIXED_REPLY))
        self.assertIn("hello", content)

    def test_unknown_route_returns_404(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._get("/v1/unknown")
        self.assertEqual(ctx.exception.code, 404)


if __name__ == "__main__":
    unittest.main()
