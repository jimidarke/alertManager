"""
Alert Manager - gateway.config

`GatewayConfig`: the one place src/gateway/* reads process configuration
from. Everything is environment-variable driven (no JSON config file, in
contrast to src/http_gateway.py's `load_config(config_file)` /
src/alert_router.py's `load_config()`), for two reasons: (1) it mirrors
the convention every other Phase 4 identity-schema consumer already uses
-- src/db_migrate.py, src/database_logger.py's `_create_db_pool()`, and
src/userstore/store.py's `UserStore.from_env()` all read POSTGRES_HOST/
PORT/DB/USER/PASSWORD purely from the environment, and this package's own
db.py needs to build a pool from those same names; (2) it keeps this
module fully testable without a filesystem fixture or a running service
(see docs/OVERHAUL_PLAN.md's Task 18 environment note) -- `from_env()` is
pure `os.environ` reads plus one fail-fast check.

MQTT_BROKER/MQTT_PORT are already injected as environment variables for
every service in docker-compose.yml (including the current http-gateway
container) even though src/http_gateway.py itself only reads them from its
JSON config file today -- this module simply starts honoring the env vars
that were already there.

Fail-fast, not fail-open (CLAUDE.md "Development Notes"): `GATEWAY_SECRET_KEY`
signs/encrypts Flask's session cookie (docs/OVERHAUL_PLAN.md section 4.4 --
hand-rolled signed-cookie sessions, not Flask-Login) and there is no safe
default for it; unlike `POSTGRES_PASSWORD` (defaults to `''`, matching
every other consumer's own default -- a bad password fails loudly the
first time a query actually runs), a missing/weak secret key would
silently produce forgeable or trivially-brute-forced session cookies if
this module invented a fallback. So `from_env()` calls `sys.exit(1)` with
a clear operator-facing message instead, exactly as
src/http_gateway.py's own `load_config()` and
src/database_logger.py's `_create_db_pool()` already do for their own
fatal startup failures.
"""

import os
import sys
from dataclasses import dataclass

# Signing/encrypting Flask's session cookie needs real entropy; 32 chars
# comfortably exceeds the 256-bit (32-byte) minimum itsdangerous/Flask
# documentation recommends for SECRET_KEY, and matches the brief's own
# "openssl rand -hex 32" (64 hex chars) recommended generation command.
MIN_SECRET_KEY_LENGTH = 32

# 12h sliding session lifetime (docs/OVERHAUL_PLAN.md section 4.4: "12h
# sliding lifetime"). Task 19 owns the actual session-refresh mechanics;
# this is just the config knob it will read
# (`app.config['PERMANENT_SESSION_LIFETIME']`, which create_app() sets
# from this value -- see gateway/__init__.py).
DEFAULT_SESSION_LIFETIME_SECONDS = 12 * 60 * 60

# Matches src/database_logger.py's _create_db_pool() defaults/semantics
# (minconn=POSTGRES_POOL_SIZE, maxconn=minconn+POSTGRES_MAX_OVERFLOW) and
# reuses the identical env var names -- each service's own docker-compose
# environment block can still set different values per container. The
# gateway's own pool (src/gateway/db.py) is lower-traffic than
# database-logger's batch-insert pool, so the *defaults* here are smaller
# (5/10 vs. database_logger's 10/20); this is a judgment call, not a
# hard requirement from the plan.
DEFAULT_POSTGRES_POOL_SIZE = 5
DEFAULT_POSTGRES_MAX_OVERFLOW = 10

# Matches src/http_gateway.py's existing LOG_POLL_INTERVAL_MS env var
# (already set in docker-compose.yml for the current http-gateway
# container) and its own default of 10000ms (10s), used by the log viewer
# page's injected `SERVER_POLL_INTERVAL_MS` JS constant.
DEFAULT_LOG_POLL_INTERVAL_MS = 10000


@dataclass(frozen=True)
class GatewayConfig:
    """Immutable snapshot of this process's configuration. Construct via `GatewayConfig.from_env()`."""

    SECRET_KEY: str

    POSTGRES_HOST: str
    POSTGRES_PORT: int
    POSTGRES_DB: str
    POSTGRES_USER: str
    POSTGRES_PASSWORD: str
    POSTGRES_POOL_MIN: int
    POSTGRES_POOL_MAX_OVERFLOW: int

    MQTT_BROKER: str
    MQTT_PORT: int
    MQTT_INPUT_TOPIC: str

    LOG_POLL_INTERVAL_MS: int
    SESSION_LIFETIME_SECONDS: int

    @classmethod
    def from_env(cls) -> "GatewayConfig":
        """
        Build a GatewayConfig from environment variables (`.env` is loaded
        by the process entrypoint -- see gateway/wsgi.py -- exactly as
        src/http_gateway.py and src/alert_router.py already do via
        python-dotenv's `load_dotenv()`).

        Exits the process (`sys.exit(1)`) if GATEWAY_SECRET_KEY is unset or
        shorter than MIN_SECRET_KEY_LENGTH characters. Every other field
        has a safe, non-fatal default (matching the equivalent default in
        src/database_logger.py / src/db_migrate.py / src/userstore/store.py
        wherever one of those modules already reads the same env var).
        """
        secret_key = os.environ.get("GATEWAY_SECRET_KEY", "")
        if len(secret_key) < MIN_SECRET_KEY_LENGTH:
            print(
                "FATAL: GATEWAY_SECRET_KEY environment variable is "
                f"{'unset' if not secret_key else 'too short'} -- it must be set to a "
                f"random string of at least {MIN_SECRET_KEY_LENGTH} characters. "
                "Generate one with: openssl rand -hex 32",
                file=sys.stderr,
            )
            sys.exit(1)

        return cls(
            SECRET_KEY=secret_key,
            POSTGRES_HOST=os.environ.get("POSTGRES_HOST", "localhost"),
            POSTGRES_PORT=int(os.environ.get("POSTGRES_PORT", 5432)),
            POSTGRES_DB=os.environ.get("POSTGRES_DB", "alertmanager"),
            POSTGRES_USER=os.environ.get("POSTGRES_USER", "alertmanager"),
            POSTGRES_PASSWORD=os.environ.get("POSTGRES_PASSWORD", ""),
            POSTGRES_POOL_MIN=int(os.environ.get("POSTGRES_POOL_SIZE", DEFAULT_POSTGRES_POOL_SIZE)),
            POSTGRES_POOL_MAX_OVERFLOW=int(os.environ.get("POSTGRES_MAX_OVERFLOW", DEFAULT_POSTGRES_MAX_OVERFLOW)),
            MQTT_BROKER=os.environ.get("MQTT_BROKER", "mqtt-broker"),
            MQTT_PORT=int(os.environ.get("MQTT_PORT", 1883)),
            MQTT_INPUT_TOPIC=os.environ.get("MQTT_INPUT_TOPIC", "alerts/input"),
            LOG_POLL_INTERVAL_MS=int(os.environ.get("LOG_POLL_INTERVAL_MS", DEFAULT_LOG_POLL_INTERVAL_MS)),
            SESSION_LIFETIME_SECONDS=int(
                os.environ.get("GATEWAY_SESSION_LIFETIME_SECONDS", DEFAULT_SESSION_LIFETIME_SECONDS)
            ),
        )
