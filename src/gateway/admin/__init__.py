"""
Alert Manager - gateway.admin

`admin_bp` (routes.py): `GET /admin/users`, `GET/POST /admin/users/new`,
`GET/POST /admin/users/<username>/edit`, `POST .../disable`,
`POST .../enable`, `POST .../reset-password` (Task 23). Session-only,
master_admin-only (`gateway.principal.require_master_admin`) -- see
routes.py's own module docstring for the full design: the two self-lockout
guard rails and their exact identity/role-membership check mechanism, the
per-user "personal role" convention this task introduces to map
repeatable ad-hoc (client, permission) scope rows onto the strictly
role-based identity schema, and the client-datalist query.

No CSRF exemption here (unlike `gateway.api`, which exempts itself since it
never accepts a session cookie) -- this blueprint IS session-only and its
mutating routes are all POST, exactly Flask-WTF `CSRFProtect`'s default-
protected method set. `gateway/__init__.py`'s `create_app()` already calls
`csrf.init_app(app)` globally; every POST here requires a valid
`csrf_token` automatically, verified end-to-end in this task's own tests.
"""

from .routes import admin_bp

__all__ = ["admin_bp"]
