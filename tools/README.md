# Alert Manager Tools

Operational scripts for identity/auth administration, alert testing, and one-off migrations/backfills. Most `*-manager.sh`/`*.py` tools auto-detect whether they're running inside a container (`/data/config` present) or on the host.

## Identity & Authentication

| Tool | Purpose |
|------|---------|
| **`user-manager.py`** | Canonical CLI for the HTTP gateway's identity/RBAC store (Postgres-backed, `src/userstore/`): accounts, roles, `(client, permission)` scopes, Bearer tokens, audit trail. Run via `docker exec -it alert-http-gateway python3 tools/user-manager.py <command> --help`. See `docs/AUTH.md` for the full subcommand list and the RBAC model, and `docs/CLIENTNODE.md` for credential-rotation guidance. **Replaces the removed `tools/http-auth-manager.sh`** (deleted in v2.0.0 -- the HTTP gateway no longer uses `data/config/http-gateway/users.json`/`tokens.json` at all). |
| **`migrate-users-json.py`** | One-off migration script: reads the legacy `data/config/http-gateway/users.json` and creates equivalent accounts/roles/scopes in the new Postgres identity schema via `UserStore`, printing a one-time table of freshly generated passwords. Run exactly once, after `src/db_migrate.py` has applied `sql/migrations/0002_identity.sql` and before the v2.0.0 gateway goes live. See `docs/AUTH.md` section 2.10 and `docs/OVERHAUL_PLAN.md` section 4.10 for the exact rollout order. |
| `mqtt-auth-manager.sh` | Username/password account management for the external MQTT listener (port 42690, ESP32 LED signs only). |
| `snmp-auth-manager.sh` | SNMPv3 user management (`create-user <username> [security_level] [password]`) -- see `docs/SNMP_CLIENT_MAPPING.md`. |
| `cert-manager.sh` | TLS certificate authority / server / client certificate management for MQTT. Client-certificate MQTT auth is deprecated (username/password is now the standard); this script still manages the CA/server certificate the TLS listener itself requires. |

## Database

| Tool | Purpose |
|------|---------|
| **`src/db_migrate.py`** (not under `tools/` -- see path) | Versioned SQL migration runner (`sql/migrations/NNNN_*.sql` against Postgres, `schema_migrations` bookkeeping table, advisory-lock serialized, fail-fast). Run once per new migration file: `python3 src/db_migrate.py` (or `--dry-run` / `--dsn`). See `docs/DATABASE_INTEGRATION.md`. |
| **`backfill_file_logs.py`** | One-off tool that parses `data/logs/file/**` (the file-pathway's alert-content logs) and inserts them into the partitioned `alerts` table, so historical alert data survives a partition-automation gap instead of being queryable only from raw log files. `--dry-run` first; guards against double-insertion and requires `--force` to re-run once manual rows already exist. See the tool's own module docstring for the full guard/idempotency design. |

## Sending Test Alerts

| Tool | Purpose |
|------|---------|
| `send-http-alert.sh` | Send a test alert through the HTTP gateway (`POST /alert`) with Basic Auth. |
| `send-mqtt-alert.sh` | Publish a test alert directly to the `alerts/input` MQTT topic. |
| `send-syslog-alert.sh` | Send an RFC3164/RFC5424 syslog test message (deprecated gateway -- reference only, see `docs/SYSLOG_INTEGRATION.md`). |
| `test-snmp-trap.sh` | Send a test SNMPv3 trap to the SNMP gateway. |
| `watch-mqtt.sh` | Subscribe to and print alert payloads from the MQTT broker. |
| `mqtt-test-connection.sh` | Verify MQTT broker connectivity under each supported authentication method. |
| `validate-https-flow.sh` | End-to-end check: HTTPS alert submission → MQTT routing → file logging → HTTP gateway log viewer. |

## Other

| Tool | Purpose |
|------|---------|
| `bump-version.sh` | `./tools/bump-version.sh [major\|minor\|patch]` -- updates `VERSION`, `README.md`'s badge, and `CLAUDE.md`'s version/date. Update `CHANGELOG.md` and tag manually afterward -- see `CLAUDE.md`'s "Version Management" section. |
| `analytics-manager.sh` | Analytics-engine scheduler/report management -- see `docs/ANALYTICS.md`. |
| `pathway_config_generator.py` | Generates a pathway-routing JSON config from a simplified template input. |

## Integration Tests (`tools/tests/`)

Shell scripts that exercise a **running HTTP gateway**:

```bash
bash tools/tests/test_normalization.sh      # level/category alias + trim/lowercase
bash tools/tests/test_invalid_requests.sh   # validation rejection paths
bash tools/tests/test_log_viewer.sh         # /api/logs/* endpoints
```

These hit `localhost:8080` directly (or wherever the gateway container is reachable within the network) and use Basic Auth (or Bearer, for `logs`) against real accounts -- see `docs/AUTH.md` for how to provision one with `tools/user-manager.py`.
