#!/usr/bin/env python3
"""Minimal deterministic OpenAI-compatible LLM stub used by the E2E suite.

Serves /v1/models and /v1/chat/completions so OpenWebUI can be pointed at it
instead of a real provider. Responses are fixed, which keeps the suite free,
fast and reproducible. Runs on the host or inside the compose network.
"""

import json
import os
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.environ.get("LLM_STUB_PORT", "8090"))

MODEL_ID = os.environ.get("LLM_STUB_MODEL", "stub-model")

# Fixed reply so assertions can rely on an exact string.
FIXED_REPLY = os.environ.get(
    "LLM_STUB_REPLY",
    "E2E-STUB-REPLY: The llm-stub service answered this message deterministically.",
)

_started_at = int(time.time())
_request_log = []
_log_lock = threading.Lock()


def _record(kind):
    with _log_lock:
        _request_log.append((kind, time.time()))


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        return

    def _send_json(self, payload, status=200):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # --- helpers -----------------------------------------------------------

    def _chat_completion(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            req = json.loads(raw.decode() or "{}")
        except ValueError:
            req = {}

        messages = req.get("messages") or []
        user_text = ""
        for msg in reversed(messages):
            content = msg.get("content")
            if isinstance(content, str) and content.strip():
                user_text = content
                break
            if isinstance(content, list):
                parts = [
                    p.get("text", "")
                    for p in content
                    if isinstance(p, dict) and p.get("type") == "text"
                ]
                joined = " ".join(x for x in parts if x)
                if joined:
                    user_text = joined
                    break

        echo = ""
        if user_text:
            compact = re.sub(r"\s+", " ", user_text.strip())
            echo = compact[:200]
        if echo:
            content = f"{FIXED_REPLY} You said: {echo}"
        else:
            content = FIXED_REPLY

        return {
            "id": f"chatcmpl-e2e-stub-{int(time.time()*1000)}",
            "object": "chat.completion",
            "created": _started_at,
            "model": req.get("model", MODEL_ID),
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": content},
                    "finish_reason": "stop",
                }
            ],
            "usage": {
                "prompt_tokens": len(user_text.split()) if user_text else 1,
                "completion_tokens": 8,
                "total_tokens": 8,
            },
        }

    # --- routes ------------------------------------------------------------

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/health":
            self._send_json({"status": "ok"})
            return
        if path == "/v1/models":
            _record("models")
            self._send_json(
                {
                    "object": "list",
                    "data": [
                        {
                            "id": MODEL_ID,
                            "object": "model",
                            "created": _started_at,
                            "owned_by": "llm-stub",
                        }
                    ],
                }
            )
            return
        self._send_json({"error": {"message": f"not found: {path}", "type": "invalid_request_error"}}, 404)

    def do_POST(self):
        path = self.path.split("?")[0]
        if path in ("/v1/chat/completions", "/chat/completions"):
            _record("completions")
            self._send_json(self._chat_completion())
            return
        self._send_json({"error": {"message": f"not found: {path}", "type": "invalid_request_error"}}, 404)


def main():
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"llm-stub listening on :{PORT} model={MODEL_ID}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
