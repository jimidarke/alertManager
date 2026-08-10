"""
Alert Manager - gateway.logs

`logs_bp` (routes.py): `GET /logs`, `GET /api/logs/list`,
`GET /api/logs/tail`, `GET /api/logs/chunk` (Task 21). Auth policy is
`gateway.principal.DEFAULT_AUTH_METHODS` (session OR Basic/Bearer) -- see
routes.py's own module docstring.

No CSRF exemption needed here (unlike `gateway.api`): every route in this
blueprint is GET-only, and Flask-WTF's `CSRFProtect` only enforces tokens
on state-changing methods (POST/PUT/PATCH/DELETE) by default, so there is
nothing for it to reject on a GET request regardless of whether a session
cookie is present.
"""

from .routes import logs_bp

__all__ = ["logs_bp"]
