"""
Alert Manager - gateway package

`create_app()`: the Flask app-factory entry point for this package, which
will eventually replace src/http_gateway.py (docs/OVERHAUL_PLAN.md section
4.3 -- deletion is Task 28's job, once every blueprint below actually
exists and covers the same URLs). Task 18 built the non-blueprint plumbing:

    config.py     GatewayConfig (env-driven, fail-fast SECRET_KEY)
    db.py         Postgres connection pool + UserStore, onto app.extensions
    mqtt.py       MqttPublisher (ported forward_to_mqtt/connect_mqtt)
    principal.py  Principal, resolve_principal, require_auth/require_permission/require_master_admin

Task 19 adds the first blueprint (`auth`: `/login`, `/logout`,
`/api/whoami`) plus the session-cookie/CSRF/throttle/credential-cache
machinery it depends on:

    auth/routes.py       auth_bp (registered below)
    auth/cache.py        CredentialCache -- app.extensions['credential_cache']
    auth/ip_throttle.py  IPThrottle -- app.extensions['ip_throttle']
    auth/__init__.py     csrf (Flask-WTF CSRFProtect) -- csrf.init_app(app) below

Task 20 adds the second blueprint (`api`: `/health`, `POST /alert`,
`POST /test`, `/docs`, `/docs/<f>`), CSRF-exempted at its own package's
`__init__.py` (see `gateway/api/__init__.py`) since it never accepts a
session cookie -- see `gateway/api/routes.py`'s module docstring for the
breaking `POST /alert` authorization change and how it composes Task 19's
`CredentialCache` without touching the shared `_resolve_basic`/
`_resolve_bearer` resolvers `logs` (Task 21) will also use.

Task 21 adds the third blueprint (`logs`: `GET /logs`, `GET /api/logs/list`,
`GET /api/logs/tail`, `GET /api/logs/chunk`) -- session-OR-Basic/Bearer
(`gateway.principal.SESSION_OR_API`/`DEFAULT_AUTH_METHODS`), no CSRF
exemption needed (GET-only). See `gateway/logs/routes.py`'s module
docstring for the binary/newline-aligned read rework and
`gateway/logs/files.py`'s for the ported traversal defense + redesigned
has_permission()/is_master_admin() authorization tiers.

Task 22 adds the fourth blueprint (`dashboard`: `GET /dashboard`,
`GET /api/dashboard/summary`, `GET /api/dashboard/groups`) --
session-only (`gateway.principal.SESSION_ONLY`), no CSRF exemption needed
(GET-only). See `gateway/dashboard/routes.py`'s module docstring for why
these two inherently multi-client endpoints use `require_auth` plus a
hand-rolled `visible_clients()` scoping call instead of
`require_permission`, and the client-scoping predicate's NULL/empty-client
exclusion analysis.

Task 23 adds the fifth and final blueprint (`admin`: `GET /admin/users`,
`GET/POST /admin/users/new`, `GET/POST /admin/users/<username>/edit`,
`POST .../disable|enable|reset-password`) -- session-only,
master_admin-only (`gateway.principal.require_master_admin`), NOT exempted
from CSRF (unlike `api`): every mutating route here is a POST and this
blueprint IS session-based, exactly what Flask-WTF's `CSRFProtect`
protects by default. See `gateway/admin/routes.py`'s module docstring for
the two self-lockout guard rails (can't disable/de-admin your own session)
and their exact identity/role-membership check mechanism, and the
per-user "personal role" convention used to map ad-hoc (client,
permission) scope rows onto the strictly role-based identity schema.

Task 24 adds the shared template/static foundation every HTML-rendering
route above builds on: `templates/layout.html` + `templates/login.html`
(Flask's default template folder, relative to this package's root_path --
`src/gateway/templates/`, already holding Task 23's `admin/*.html` stubs)
and `src/static/{css/base.css,js/common.js}`, served via an explicitly
configured `static_folder`/`static_url_path` below since `src/static/`
lives one level up from this package (a sibling of `gateway/`, not
`gateway/static/`) -- see docs/OVERHAUL_PLAN.md section 4.3 ("`src/static/`
stays where it is ... URLs preserved exactly: ... `/static/...`").
Two small additions land here too:

  - A `version` context processor (`inject_version` below), reading
    `src/version.py`'s `__version__` once at import time -- this is what
    `layout.html`'s footer (`Alert Manager v{{ version }}`) and any future
    template use instead of the old inline-HTML `src/http_gateway.py`'s
    literal, never-substituted `{__version__}` f-string placeholder bug.
  - `GET /` -- redirects to `/dashboard` if a valid session already
    resolves, `/login` otherwise. Registered directly on `app` (not its
    own blueprint) since it is one trivial redirect, not a resource with
    multiple routes.

`ProxyFix(x_for=1, x_proto=1)` is added here (not deferred) per
docs/OVERHAUL_PLAN.md section 4.4: it's what makes `request.remote_addr`
(used by the IP throttle and `UserStore`'s own audit `source_ip`) and
`SESSION_COOKIE_SECURE` trustworthy when this app runs behind the
nginx-proxy reverse proxy in production, rather than trusting a
client-spoofable `X-Forwarded-*` header directly. What IS deferred to Task
27 is the docker-compose side of that: removing the direct `8080:8080` host
port publish (a spoofable-XFF path that bypasses the proxy entirely) is a
compose-file change, out of scope for this package.
"""

import logging
import os

from flask import Flask, redirect, request, url_for
from werkzeug.middleware.proxy_fix import ProxyFix

from version import __version__ as APP_VERSION

from .auth import auth_bp, csrf
from .api import api_bp  # after .auth: api/__init__.py exempts itself via gateway.auth's csrf instance
from .logs import logs_bp
from .dashboard import dashboard_bp
from .admin import admin_bp
from .auth.cache import CredentialCache
from .auth.ip_throttle import IPThrottle
from .config import GatewayConfig
from .db import init_db
from .mqtt import MqttPublisher
from .principal import SESSION_ONLY, resolve_principal

logger = logging.getLogger("gateway")

# `src/static/` is a SIBLING of this package's directory (`src/gateway/`),
# not `src/gateway/static/` -- Flask's static_folder is resolved relative
# to `root_path` (this package's own directory) when given as a relative
# path, so `../static` lands on `src/static`. `static_url_path` is pinned
# explicitly to `/static` (Flask would derive the same value by default
# here, since it takes the final path segment of `static_folder` -- but
# spelling it out keeps the plan's "URLs preserved exactly: ... /static/..."
# requirement visible at the definition site rather than implicit).
_STATIC_FOLDER = os.path.join(os.path.dirname(__file__), "..", "static")


def create_app(config: GatewayConfig = None) -> Flask:
    """
    Build and return a fully-configured Flask app: `GatewayConfig` applied
    to `app.config`, the Postgres pool + `UserStore` wired onto
    `app.extensions` (db.py), an `MqttPublisher` connected and stored at
    `app.extensions['mqtt_publisher']`, and (Task 19) the `auth` blueprint
    registered with its supporting `CredentialCache`/`IPThrottle`/CSRF
    machinery wired onto `app.extensions`/the app itself.

    `config` defaults to `GatewayConfig.from_env()` (the real,
    fail-fast-on-bad-SECRET_KEY path used in production/under gunicorn --
    see wsgi.py); passing an explicit `config` is a testing seam so tests
    can construct an app without relying on process environment variables.

    The initial MQTT connect attempt is fail-fast, exactly like
    src/http_gateway.py's `run()`: `MqttPublisher.connect_mqtt()` itself
    never raises (it logs and returns False on failure -- see mqtt.py), so
    this function is what turns "could not connect" into a hard startup
    failure, mirroring the original's "log CRITICAL, sys.exit(1)" instead
    of silently serving traffic with a permanently-dead MQTT client. This
    matters because under gunicorn `create_app()` runs once at worker-boot
    time (see wsgi.py) -- there is no separate `run()` step for a fail-fast
    check to live in instead.
    """
    app = Flask(__name__, static_folder=_STATIC_FOLDER, static_url_path="/static")
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1)

    config = config or GatewayConfig.from_env()
    app.config["SECRET_KEY"] = config.SECRET_KEY
    app.config["PERMANENT_SESSION_LIFETIME"] = config.SESSION_LIFETIME_SECONDS

    # Hand-rolled session cookie security properties (docs/OVERHAUL_PLAN.md
    # 4.4): HttpOnly (no JS access), Secure (HTTPS-only -- safe to require
    # unconditionally now that ProxyFix, above, makes `request.is_secure`
    # reflect the real client<->proxy scheme rather than this container's
    # own plain-HTTP listener), SameSite=Lax (sent on top-level navigations
    # like following a link/redirect, not on cross-site subresource/form
    # requests -- CSRF-protection-in-depth alongside Flask-WTF below).
    app.config["SESSION_COOKIE_HTTPONLY"] = True
    app.config["SESSION_COOKIE_SECURE"] = True
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"

    app.extensions["gateway_config"] = config

    init_db(app, config)

    mqtt_publisher = MqttPublisher(config)
    if not mqtt_publisher.connect_mqtt():
        logger.critical("Failed to connect to MQTT broker at startup")
        import sys

        sys.exit(1)
    app.extensions["mqtt_publisher"] = mqtt_publisher

    # Task 19: credential cache + IP throttle (both process-lifetime,
    # single-instance-per-app state -- see their own modules for why an
    # in-memory implementation is correct given the single-gunicorn-worker
    # deployment, docs/OVERHAUL_PLAN.md section 4.9) and CSRF protection
    # (Flask-WTF). `auth/__init__.py`'s own docstring documents how Task
    # 20's `api` blueprint exempts itself from `csrf` (it never accepts
    # session cookies, so nothing to forge).
    app.extensions["credential_cache"] = CredentialCache()
    app.extensions["ip_throttle"] = IPThrottle()
    csrf.init_app(app)

    app.register_blueprint(auth_bp)
    app.register_blueprint(api_bp)
    app.register_blueprint(logs_bp)
    app.register_blueprint(dashboard_bp)
    app.register_blueprint(admin_bp)

    # Task 24: `version` is injected into every template's context here
    # (rather than each render_template() call passing it individually) so
    # `layout.html`'s footer -- and any other template -- can simply write
    # `{{ version }}`. This is the fix for the old inline-HTML log viewer's
    # documented `{__version__}` bug (CLAUDE.md's verified findings): that
    # code built its HTML via an f-string but the `{__version__}` token sat
    # inside a *plain* (non-f) string literal, so it was never substituted
    # and shipped to users verbatim. A Jinja context processor can't
    # reproduce that class of bug -- `{{ version }}` is always evaluated by
    # the template engine, never emitted as a literal.
    @app.context_processor
    def inject_version():
        return {"version": APP_VERSION}

    # Task 24: `GET /` -- redirect to `/dashboard` if a session is already
    # valid, `/login` otherwise. Uses `resolve_principal(SESSION_ONLY)`
    # directly (the same primitive `gateway/auth/routes.py`'s `GET /login`
    # uses for its own "already logged in?" check) rather than
    # `require_auth`, since an anonymous request here is the EXPECTED case
    # to redirect to `/login`, not a failure to reject.
    @app.route("/")
    def index():
        if resolve_principal(SESSION_ONLY) is not None:
            return redirect(url_for("dashboard.view_dashboard"))
        return redirect(url_for("auth.login"))

    # v3.0.0: enforce `users.must_change_password`. The column and its
    # setters (admin user-create, admin password-reset, user-manager
    # init-admin/create-user/set-password) all predate this hook -- until
    # now the flag was written in six places, surfaced by `GET /api/whoami`,
    # and enforced nowhere, so an operator could hand out a password the
    # recipient had no way to rotate.
    #
    # SESSION principals only. A Basic/Bearer caller is never redirected:
    # machine accounts created through the admin UI carry this flag too, and
    # bouncing `POST /alert` to an HTML form would break every device for a
    # reason that does not apply to them (they cannot use the form, and
    # their credential is rotated by an operator via tools/user-manager.py).
    #
    # `_resolve_session` memoizes on `g` for the request, so this hook and
    # the `require_auth` decorator that runs moments later share one DB
    # lookup rather than issuing two identical joins.
    EXEMPT_ENDPOINTS = frozenset({
        "auth.change_password",
        "auth.change_password_form",
        "auth.logout",
        "auth.login",
        "auth.login_submit",
        "auth.whoami",
        "static",
    })

    @app.before_request
    def enforce_password_change():
        if request.endpoint in EXEMPT_ENDPOINTS:
            return None
        # `api_bp` routes are reachable by session cookie for nothing --
        # they are Basic/Bearer only -- but `logs` accepts either, so gate
        # on what actually resolved rather than on the blueprint.
        principal = resolve_principal(SESSION_ONLY)
        if principal is None or not principal.must_change_password:
            return None
        return redirect(url_for("auth.change_password_form"))

    return app
