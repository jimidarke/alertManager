"""
Alert Manager - gateway.api

`api_bp` (routes.py): `GET /health`, `POST /alert`, `POST /test`,
`GET /docs`, `GET /docs/<filename>` (Task 20). See routes.py's own module
docstring for the full design: the breaking `POST /alert` authorization
change, the CredentialCache composition decision, and the 401/403
JSON-ification fix.

CSRF-exempted here, exactly as `gateway/auth/__init__.py`'s own docstring
anticipated ("Task 20's `api` blueprint ... exempts itself with the SAME
instance, once it exists"): this blueprint is Basic/Bearer only
(`gateway.principal.API_ONLY`) and never accepts a session cookie, so
there is nothing here for Flask-WTF's CSRFProtect to protect against --
without this, a curl script or node device's plain `POST /alert` (no CSRF
token, because it never had a session to get one from) would be rejected
with a 400 by Flask-WTF before this blueprint's own routes ever ran.
`csrf.exempt()` only marks the blueprint object; it works whether called
before or after `csrf.init_app(app)` (gateway/__init__.py's create_app()).
"""

from gateway.auth import csrf

from .routes import api_bp

csrf.exempt(api_bp)

__all__ = ["api_bp"]
