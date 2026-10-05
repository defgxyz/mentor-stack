"""
Tiny internal-only sidecar running alongside `caddy run` in the same
container: a single narrow endpoint that does exactly one thing - re-run
`caddy reload` against the current Caddyfile on disk. No config-rewrite
capability, no broader admin API surface - just "pick up whatever's already
in the file", which is all another container should ever need to trigger.

Not referenced by any Caddyfile site block, so nothing public routes to it -
only reachable on the internal docker network as http://caddy:9999/reload,
and even then only with the shared bearer token.
"""
import os
import subprocess
from http.server import BaseHTTPRequestHandler, HTTPServer

TOKEN = os.environ["CADDY_RELOAD_TOKEN"]


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        if self.path != "/reload":
            self.send_response(404)
            self.end_headers()
            return
        auth = self.headers.get("Authorization", "")
        if auth != f"Bearer {TOKEN}":
            self.send_response(401)
            self.end_headers()
            return
        result = subprocess.run(
            ["caddy", "reload", "--config", "/etc/caddy/Caddyfile", "--adapter", "caddyfile"],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode == 0:
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"reloaded")
        else:
            self.send_response(500)
            self.end_headers()
            self.wfile.write(result.stderr.encode()[:2000])

    def log_message(self, format, *args):
        pass  # keep container logs focused on Caddy's own output


if __name__ == "__main__":
    HTTPServer(("0.0.0.0", 9999), Handler).serve_forever()
