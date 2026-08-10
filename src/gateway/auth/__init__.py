"""
Alert Manager - gateway.auth

`auth_bp` (routes.py): GET/POST /login, POST /logout, GET /api/whoami.
`csrf`: a module-level `flask_wtf.csrf.CSRFProtect` instance, created here
(NOT bound to an app yet) so it can be imported from two places without
either one owning app construction:

  - `gateway/__init__.py`'s `create_app()` calls `csrf.init_app(app)` --
    this is what actually activates CSRF checking for the app (see that
    module for the rest of the Task 19 wiring: `credential_cache`,
    `ip_throttle`, `ProxyFix`, and `auth_bp` registration).
  - Task 20's `api` blueprint (Basic/Bearer only, never cookies -- so
    nothing to forge) exempts itself with the SAME instance, once it
    exists:

        from gateway.auth import csrf
        from .routes import api_bp
        csrf.exempt(api_bp)

    `CSRFProtect.exempt()` can be called before OR after the blueprint is
    registered on the app (it just marks the view/blueprint object itself),
    so Task 20 is free to call it from its own blueprint-construction
    module without any import-order coordination with this package.

Constructing `CSRFProtect()` here at import time (rather than inside
`create_app()`) mirrors the standard Flask-extension "create once, init_app
per application" pattern -- it is what lets a module built later (Task 20)
import a live, already-existing `csrf` object instead of reaching into
`create_app()`'s local scope.
"""

from flask_wtf.csrf import CSRFProtect

from .routes import auth_bp

csrf = CSRFProtect()

__all__ = ["auth_bp", "csrf"]
