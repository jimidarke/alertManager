# Changelog

Versioned independently of the main Alert Manager server/gateway (its own `VERSION` file, `tools/bump-version.sh`) — this package's version tracks its own API-stability story, not the gateway's release history.

## [0.1.0] - 2026-07-22

Initial release.

### Added
- `AlertClient`: `send()`/`critical()`/`warning()`/`notice()`/`info()`/`debug()`, `test()`, `health()`, context-manager support.
- Client-side payload validation (`validate_alert()`/`is_valid_alert()`) mirroring the gateway's own `validate_alert()` field-for-field — catches invalid `level`/`category`/`timestamp`/`source` before the network call, closing the gap where the gateway's HTTP endpoint only validates field presence, not values.
- Typed exception hierarchy (`AlertManagerError` and subclasses) mapping every documented gateway response shape (400/401/403/500/503) to a specific, documented exception class.
- Retry/backoff for `POST /alert`/`POST /test`, mirroring the gateway project's own `pathways/email_pathway.py` retry precedent (retry only 429/5xx, capped at 2 retries, honor `Retry-After`, never retry connection-level failures).
- Opt-in automatic exception reporting via `install_exception_hooks()`/`exception_hook_scope()`/`report_exceptions()`, hooking both `sys.excepthook` and `threading.excepthook`.
