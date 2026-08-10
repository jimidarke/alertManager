"""
Alert Manager - gateway.wsgi

Gunicorn entry point: `gunicorn gateway.wsgi:app` (docs/OVERHAUL_PLAN.md
section 4.9 -- 1 worker x 8 gthread threads, `--chdir /app/src`; wiring
that command into docker-compose.yml/docker/Dockerfile is Task 28's job,
not this one).

Calling `create_app()` at import time is deliberate: gunicorn imports this
module once per worker process at boot, so this is where
`GatewayConfig.from_env()`'s fail-fast `GATEWAY_SECRET_KEY` check, the DB
pool's eager `minconn` connections, and the initial MQTT connect attempt
all happen -- a bad deploy fails loudly when `gunicorn` starts, not on the
first inbound request.
"""

from . import create_app

app = create_app()
