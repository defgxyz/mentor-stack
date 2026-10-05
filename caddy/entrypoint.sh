#!/bin/sh
set -e
python3 /reload_server.py &
exec caddy run --config /etc/caddy/Caddyfile --adapter caddyfile
