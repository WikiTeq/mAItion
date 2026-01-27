#!/usr/bin/env python3
import os
from http.server import HTTPServer, BaseHTTPRequestHandler

class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path != "/healthz":
            self.send_response(404)
            self.end_headers()
            return

        # Check readiness file
        if os.path.exists(os.environ.get("HEALTHZ_READY_FILE", "/tmp/healthz_ready")):
            self.send_response(200)
            body = b"OK"
        else:
            self.send_response(503)
            body = b"NOT READY"

        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        return  # silence stdout logging

if __name__ == "__main__":
    port = int(os.environ.get("HEALTHZ_PORT", "8081"))
    server = HTTPServer(("", port), HealthHandler)
    server.serve_forever()
