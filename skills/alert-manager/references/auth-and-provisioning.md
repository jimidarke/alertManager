# Auth and provisioning

The identity/authorization model, how to provision new sender credentials,
and a troubleshooting table for auth-flavored failures. Read this when
something is `401`/`403`/`400`-unknown-client, or when you need to create
new credentials for a sender.

## Table of contents

- [Auth model summary](#auth-model-summary)
- [The client field's authorization rules](#the-client-fields-authorization-rules)
- [Provisioning a new sender account](#provisioning-a-new-sender-account)
- [Verifying credentials](#verifying-credentials)
- [Env-var naming conventions](#env-var-naming-conventions)
- [Troubleshooting table](#troubleshooting-table)
- [Lockout and throttling, briefly](#lockout-and-throttling-briefly)

## Auth model summary

The service has two independent access surfaces:

- **The alert-submission API** (`/alert`, `/test`, `/health`, `/api/whoami`,
  `/api/logs/*`) — **HTTP Basic or Bearer token only, never a session
  cookie.** This is what anything scripted or automated should use.
- **The browser dashboard/admin UI** (`/login`, `/dashboard`, `/admin/*`) —
  session cookies, CSRF-protected forms. Not relevant to a sender — if
  you're writing code or a script, ignore this surface entirely. Watch out
  for docs that show a `curl -c cookies.txt .../login` flow right next to
  the real API auth docs; that flow is for the browser surface only and
  will not work against `/alert`.

Access is role-based: an account holds roles, each role carries scopes, each
scope is a `(client, permission)` pair where either side may be a wildcard.
The two permissions relevant to sending/reading alerts are `alerts:write`
and `logs:read`; a top-level `master_admin`-equivalent role holds a
wildcard-everything scope and is the only role that can register new client
names or send/see fully clientless (`client=""`) data.

## The client field's authorization rules

(Full field-level detail in `references/payload-reference.md` — this is the
authorization-specific view.)

1. Effective client = payload `client` (stripped) → else the account's
   configured default client → else `""` (clientless).
2. Your credentials need an `alerts:write` scope covering that value (a
   scope on the exact client, or a wildcard-client scope). No match → `403`.
3. The clientless case additionally requires the account's top
   administrative role — a wildcard `alerts:write` scope alone is not
   sufficient for `client=""`.
4. **Separately from authorization**, a non-empty effective client must
   already exist as a registered, active entry in a client registry,
   managed through a master_admin-only web page. **There is no CLI for
   this** — only that web UI can register a new client name. This check
   runs *after* step 2, so a `403` always means "fix the scope," and a
   `400 Unknown client` always means "register (or correct) the client
   name" — they're never the same problem.

**If you hit `400 Unknown client` and you (the agent) don't have
master_admin web access**, don't try to work around it (don't guess at a
different client name, don't strip the client field to bypass it if the
task actually needs per-client routing). Stop and tell the user: either
register the client name via the admin UI, or use a client name that's
already registered.

## Provisioning a new sender account

Requires shell/operator access to wherever the Alert Manager is deployed
(commonly via `docker exec` into the gateway container). The canonical tool
is `tools/user-manager.py` in the main repo. Worked example — a scoped,
write-only account for a new sender:

```bash
docker exec -it <gateway-container> python3 tools/user-manager.py create-user my-sender --default-client my-client
docker exec -it <gateway-container> python3 tools/user-manager.py create-role my-sender-role --description 'Alert submission for my-client'
docker exec -it <gateway-container> python3 tools/user-manager.py add-scope my-sender-role my-client alerts:write
docker exec -it <gateway-container> python3 tools/user-manager.py grant-role my-sender my-sender-role
docker exec -it <gateway-container> python3 tools/user-manager.py create-token my-sender   # prints amt_... exactly once — save it now
```

Before any of this works for a non-empty `client`, an operator with
master_admin web access also needs to register `my-client` on the admin
Clients page — the CLI above does not do this, and there's no CLI that does.

If you don't have operator access to the host, you can't run any of this —
ask the user for existing credentials, or ask them (or someone who does have
access) to run the provisioning steps and hand you the resulting
username/password or token.

## Verifying credentials

After creating or rotating a credential, always confirm it works before
considering the job done:

```bash
curl -s -u my-sender:'...' https://alert.d-t.pw/api/whoami
# or: curl -s -H "Authorization: Bearer amt_..." https://alert.d-t.pw/api/whoami
```

Response includes the account's username, its resolved `default_client`,
whether it holds the top administrative role, and its scopes — everything
you need to confirm the account can actually do what you provisioned it for
before moving on to a real send.

## Env-var naming conventions

This codebase's own tooling is **not** consistent about env-var names — if
you're editing an existing file, match whatever it already uses rather than
"fixing" it. If you're writing something new, prefer the `ALERT_MANAGER_*`
form (what this skill and the SDK's own quickstart example use):

| Convention | Used by |
|---|---|
| `ALERT_MANAGER_URL` / `ALERT_MANAGER_USERNAME` / `ALERT_MANAGER_PASSWORD` / `ALERT_MANAGER_TOKEN` | This skill's `scripts/send-alert.sh`, the Python SDK's own quickstart example — **prefer this for new code** |
| `ALERT_URL` / `ALERT_CLIENT` / `ALERT_TOKEN` | The turnkey Linux host-health agent's dotenv config |
| `ALERT_USER` / `ALERT_PASSWORD` | Various shell test/send scripts in the main repo's `tools/` directory |

## Troubleshooting table

| Symptom | Cause | Fix |
|---|---|---|
| `401` on `/alert`/`/test`/`/health`-adjacent endpoints | Missing/invalid Basic or Bearer credentials, or account locked | Verify the account exists and is active; check credentials are being sent correctly |
| `403` on `POST /alert` | No `alerts:write` scope for the effective client, or a clientless send without the top admin role | Grant/verify the scope for that exact client (see provisioning above) |
| `400 "Unknown client"` on `POST /alert` | Scope check passed, but the client name isn't registered in the client registry | A master_admin needs to register it via the web UI — no CLI path exists |
| `429` on `/login` | Too many failed logins from one source IP in a short window | Only affects the browser login endpoint, not the API — wait it out or investigate the source |
| Same generic "invalid credentials" error every time, even with the right password | Account is locked out after repeated failed attempts | An operator needs to reset/unlock the account server-side |
| `503` on `POST /alert` | Credential verification couldn't get a fresh or cached answer (a database hiccup) | Transient — retry shortly; check `GET /health`'s detail fields |
| `http://<host>:8080` connection refused | Older configs assumed a direct port; the gateway container may not publish one | Use the HTTPS base URL, not a direct host:port |
| 302 redirect to a login page when calling an API-style endpoint | You accidentally hit a session-only (browser) route, or sent no credentials to a route that also serves an HTML login page | Confirm you're calling the API path with Basic/Bearer auth, not the browser path |

## Lockout and throttling, briefly

Two independent, stacking protections apply to login attempts: repeated
failed logins against **one account** lock that account out temporarily
(an operator can clear this), and repeated failed logins from **one source
IP** (regardless of which username) get rate-limited on the login endpoint
specifically. Neither mechanism is specific to the alert-submission API path
beyond the shared credential-verification logic — a burst of failed
`POST /alert` attempts with bad credentials can still trip the
per-account lockout the same way a failed browser login would.
