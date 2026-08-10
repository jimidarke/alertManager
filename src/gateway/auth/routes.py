"""
Alert Manager - gateway.auth.routes

`auth` blueprint: `GET/POST /login`, `POST /logout`, `GET /api/whoami`. This
is the one route (`POST /login`) that can actually create a session, so it
is where the per-IP login throttle (auth/ip_throttle.py) and the session
cookie itself (payload `{uid, u, iat}`, see gateway/principal.py's
`_resolve_session` -- this task also implements that function's body, but
its docstring lives there, not repeated here) both get wired in.

Task 24 landed `templates/login.html` + `templates/layout.html`;
`_render_login_page()` below now calls `render_template("login.html", ...)`
with the already-validated (or blanked) `next` value and the optional
generic failure message, exactly replacing the hand-rolled HTML string this
task originally shipped (see that template's own header comment for why it
is standalone rather than `{% extends "layout.html" %}`, and
`tests/test_gateway_auth_routes.py` -- unchanged by Task 24 -- for the
CSRF-token-scraping/round-trip tests that keep passing against the real
template).

--------------------------------------------------------------------------
Never revealing whether a username exists
--------------------------------------------------------------------------
`UserStore.verify_login()` already returns `None` uniformly for "no such
user", "wrong password", "disabled", "expired", AND "locked out" (see that
method's own docstring) -- the *why* only ever goes to the audit trail,
never the return value. `POST /login` below deliberately preserves that:
every failure path (bad IP-throttle state aside, which is unrelated to any
particular username) renders the exact same
"Invalid username or password" message with the exact same response shape.

This is a deliberate choice for the store-level lockout case specifically:
a distinct "your account is locked" message would itself confirm the
username exists and is presently locked -- exactly the enumeration channel
"never reveal whether the username existed" rules out. A generic message
covering lockout too is "sensible" (per the task brief) precisely because
it does not leak more than the plan asks for; an operator can still see the
real reason (`lockout` vs `login_failed` vs `bad_password`) via
`UserStore.recent_audit()` / the `user-manager audit` CLI subcommand.

--------------------------------------------------------------------------
`next=` validation
--------------------------------------------------------------------------
See `_is_safe_next()` below: only a same-origin relative path is ever
honored, everything else (absolute URLs, protocol-relative `//host/...`,
anything carrying a scheme/netloc) falls back to `_default_next()`.
"""

import time
from typing import Optional
from urllib.parse import urlsplit

from flask import (
    Blueprint, Response, current_app, g, jsonify, redirect, render_template,
    request, session, url_for,
)

from gateway.principal import SESSION_ONLY, SESSION_OR_API, resolve_principal

auth_bp = Blueprint("auth", __name__)


def _user_store():
    return current_app.extensions["user_store"]


def _ip_throttle():
    return current_app.extensions["ip_throttle"]


# ---------------------------------------------------------------------------
# next= validation -- reject anything that isn't a same-origin relative path.
# ---------------------------------------------------------------------------

def _is_safe_next(value: Optional[str]) -> bool:
    """
    True only for a same-origin relative path: must start with exactly one
    `/`, must carry no scheme/netloc (blocks `https://evil.com`), and must
    not start with a second `/` or a backslash (blocks the
    protocol-relative `//evil.com` and the `/\\evil.com` variant some
    browsers still normalize to `//evil.com`). No embedded CR/LF (header
    injection into the eventual `Location`/attribute value).
    """
    if not value:
        return False
    if "\r" in value or "\n" in value:
        return False
    if not value.startswith("/") or value.startswith("//") or value.startswith("/\\"):
        return False
    parsed = urlsplit(value)
    return not parsed.scheme and not parsed.netloc


def _default_next() -> str:
    try:
        return url_for("dashboard.view_dashboard")
    except Exception:
        # Defensive fallback only, not the expected path: `dashboard.view_dashboard`
        # (Task 22, gateway/dashboard/routes.py) is a real, always-registered
        # endpoint in this app, so `url_for` above should always succeed --
        # this except branch exists purely so a misconfigured/partial app
        # (e.g. the dashboard blueprint unregistered in some deployment
        # variant) degrades to the hardcoded path instead of a 500, same
        # last-resort pattern gateway/principal.py's `_login_redirect()` uses.
        return "/dashboard"


def _safe_next_or_default(value: Optional[str]) -> str:
    return value if _is_safe_next(value) else _default_next()


# ---------------------------------------------------------------------------
# Login page rendering -- templates/login.html (Task 24)
# ---------------------------------------------------------------------------

def _render_login_page(*, next_value: str, error: Optional[str] = None) -> str:
    """
    Renders `templates/login.html` with the `next` redirect target
    (blanked, not passed through raw, if it fails `_is_safe_next` -- same
    defense-in-depth this function has always applied, now enforced before
    the value ever reaches Jinja rather than via manual HTML-escaping) and
    the optional generic failure message (`error`, e.g. "Invalid username
    or password" -- see module docstring's "Never revealing whether a
    username exists" section for why this string is deliberately identical
    across every failure cause).
    """
    safe_next = next_value if _is_safe_next(next_value) else ""
    return render_template("login.html", next_value=safe_next, error=error)


# ---------------------------------------------------------------------------
# Change password -- templates/change_password.html (v3.0.0)
# ---------------------------------------------------------------------------

# Minimum length for a self-service password change. Deliberately the ONLY
# composition rule: length is the one requirement with real evidence behind
# it, and character-class rules mostly push people toward predictable
# substitutions. Generated passwords (tools/user-manager.py's
# GENERATED_PASSWORD_BYTES, gateway/admin/routes.py's reset) are far longer
# than this already -- the floor exists for human-chosen replacements.
MIN_PASSWORD_LENGTH = 12


def _render_change_password_page(*, forced: bool, error: Optional[str] = None) -> str:
    return render_template(
        "change_password.html",
        forced=forced,
        error=error,
        min_length=MIN_PASSWORD_LENGTH,
    )


def _validate_new_password(current: str, new: str, confirm: str) -> Optional[str]:
    """
    Return an error message, or None if the change is acceptable.

    Order matters: "current password is incorrect" is checked by the caller
    against the real hash BEFORE this runs, so by the time we get here the
    visitor has proven they know `current` and specific feedback about the
    new value leaks nothing.
    """
    if new != confirm:
        return "New passwords do not match."
    if len(new) < MIN_PASSWORD_LENGTH:
        return f"New password must be at least {MIN_PASSWORD_LENGTH} characters."
    if new == current:
        return "New password must be different from the current password."
    return None


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@auth_bp.route("/login", methods=["GET"])
def login():
    next_param = request.args.get("next", "")
    # Already logged in (valid session cookie)? Don't show the form again.
    if resolve_principal(SESSION_ONLY) is not None:
        return redirect(_safe_next_or_default(next_param))
    return _render_login_page(next_value=next_param)


@auth_bp.route("/login", methods=["POST"])
def login_submit():
    ip = request.remote_addr or "unknown"
    next_value = request.form.get("next", "")

    # IP throttle checked BEFORE any credential verification is attempted
    # (task brief: "apply IP throttle before attempting verification") --
    # a throttled IP never even reaches UserStore.verify_login().
    if _ip_throttle().is_blocked(ip):
        return Response("Too many login attempts. Please try again later.\n", status=429)

    username = request.form.get("username", "")
    password = request.form.get("password", "")

    data = _user_store().verify_login(username, password, source_ip=ip)
    if data is None:
        _ip_throttle().record_failure(ip)
        return _render_login_page(next_value=next_value, error="Invalid username or password")

    # New, authenticated session. `session.clear()` first: defense in depth
    # against session fixation (an old, unrelated session dict -- there
    # shouldn't be one given this cookie is httponly/signed, but a fresh
    # dict for a fresh login is the safer default regardless).
    session.clear()
    session["uid"] = data.user_id
    session["u"] = data.username
    session["iat"] = int(time.time())
    session.permanent = True  # 12h sliding lifetime -- see gateway/principal.py's _resolve_session docstring

    return redirect(_safe_next_or_default(next_value))


@auth_bp.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect(url_for("auth.login"))


@auth_bp.route("/change-password", methods=["GET"])
def change_password_form():
    principal = resolve_principal(SESSION_ONLY)
    if principal is None:
        return redirect(url_for("auth.login", next=url_for("auth.change_password")))
    return _render_change_password_page(forced=principal.must_change_password)


@auth_bp.route("/change-password", methods=["POST"])
def change_password():
    """
    Self-service password change. SESSION_ONLY on purpose: this is the
    browser's credential-rotation path, and it is also the release valve for
    the `must_change_password` gate enforced in gateway/__init__.py's
    `before_request`. Basic/Bearer principals are never gated and have no
    business here -- a machine account rotates its credential through
    tools/user-manager.py, where an operator is accountable for the new
    value.

    CSRF is covered by the app-wide CSRFProtect (`auth_bp` is not exempt,
    unlike `api_bp`), so a missing/blank token is rejected before this view
    ever runs.
    """
    principal = resolve_principal(SESSION_ONLY)
    if principal is None:
        return redirect(url_for("auth.login", next=url_for("auth.change_password")))

    forced = principal.must_change_password
    current_password = request.form.get("current_password", "")
    new_password = request.form.get("new_password", "")
    confirm_password = request.form.get("confirm_password", "")

    store = _user_store()
    ip = request.remote_addr or "unknown"

    # Re-verify the current password against the real hash. verify_login()
    # is the only path that does this, and reusing it means a wrong current
    # password counts toward lockout exactly like any other failed
    # credential check -- an authenticated session is not a licence to
    # brute-force the account's own password.
    if store.verify_login(principal.username, current_password, source_ip=ip) is None:
        return _render_change_password_page(forced=forced, error="Current password is incorrect.")

    error = _validate_new_password(current_password, new_password, confirm_password)
    if error is not None:
        return _render_change_password_page(forced=forced, error=error)

    store.set_password(
        principal.username,
        new_password,
        must_change_password=False,
        actor=principal.username,
        source_ip=ip,
    )

    # The memoized session principal (gateway/principal.py `_resolve_session`)
    # still says must_change_password=True for this request; drop it so the
    # redirect below is evaluated against fresh state rather than bouncing
    # straight back into the gate.
    g.pop("_session_principal", None)

    return redirect(_default_next())


@auth_bp.route("/api/whoami", methods=["GET"])
def whoami():
    """
    JSON API endpoint -- deliberately does NOT use the `require_auth`
    decorator, even though it accepts the same `SESSION_OR_API` methods
    `require_auth(methods=SESSION_OR_API)` would. `require_auth`'s shared
    failure behavior (`gateway/principal.py`'s `_auth_failure_response`)
    redirects (302 to `/login`) whenever "session" is an allowed method and
    no credentials were presented at all -- correct for an HTML page
    (`/logs`-style), but wrong for a JSON endpoint: the task brief is
    explicit that an unauthenticated `GET /api/whoami` returns 401, not an
    HTML redirect a JSON client can't do anything useful with. Calling
    `resolve_principal()` directly here reuses the exact same session ->
    basic -> bearer precedence/logic without inheriting the HTML-oriented
    redirect behavior built for browser page routes.

    Reconsidered in Task 21: `gateway/principal.py`'s `_auth_failure_response`
    now carries a centralized fix for exactly this redirect-vs-401 gap
    (`_prefers_json()`, keyed on the `/api/` path prefix -- see that
    module's docstring), which would make a bare
    `@require_auth(methods=SESSION_OR_API)` behave correctly here too. This
    route is deliberately NOT switched over to it, though: `require_auth`'s
    failure response is a plain-text 401 body
    (`"Authentication required\n"` + a `WWW-Authenticate` challenge
    header), not JSON -- switching would trade the redirect bug (now fixed
    centrally either way) for a regression in THIS route's own "always
    JSON" contract, with no `api`-blueprint-style `_jsonify_auth_errors`
    hook on `auth_bp` to restore it. No existing test pins the exact 401
    body shape (only the status code), so this would be a silent behavior
    change for no benefit -- the bypass stays.
    """
    principal = resolve_principal(SESSION_OR_API)
    if principal is None:
        return jsonify({"error": "authentication required"}), 401

    return jsonify(
        {
            "user_id": principal.user_id,
            "username": principal.username,
            "auth_method": principal.auth_method,
            "default_client": principal.default_client,
            "is_master_admin": principal.is_master_admin(),
            "must_change_password": principal.must_change_password,
        }
    )
