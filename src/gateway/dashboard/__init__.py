"""
Alert Manager - gateway.dashboard

`dashboard_bp` (routes.py): `GET /dashboard`, `GET /api/dashboard/summary`,
`GET /api/dashboard/groups` (Task 22). Session-only auth
(`gateway.principal.SESSION_ONLY`) -- this is a browser-only surface, no
node/script API client is ever expected to hit it (docs/OVERHAUL_PLAN.md
section 4.6). See routes.py's own module docstring for the full design:
why `require_auth(methods=SESSION_ONLY)` plus a hand-rolled
`visible_clients()` scoping call was chosen over `require_permission` for
these two inherently multi-client endpoints, the client-scoping predicate
(including the NULL/empty-client exclusion analysis -- the security-critical
core of this task), and the DB-pool-down 503 response shape.

No CSRF exemption needed here (same reasoning as `gateway.logs`): every
route in this blueprint is GET-only, and Flask-WTF's `CSRFProtect` only
enforces tokens on state-changing methods (POST/PUT/PATCH/DELETE) by
default, so there is nothing for it to reject on a GET request regardless
of whether a session cookie is present.
"""

from .routes import dashboard_bp

__all__ = ["dashboard_bp"]
