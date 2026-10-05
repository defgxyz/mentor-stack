FROM caddy:2

RUN apk add --no-cache python3

COPY reload_server.py /reload_server.py
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]
