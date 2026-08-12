# Payload reference

Full wire-level contract for `POST /alert`. This is the detailed companion
to `SKILL.md`'s quick reference — read this when you need the exact field
list, an edge case, or the full response-body shapes.

## Table of contents

- [Required fields](#required-fields)
- [Optional fields](#optional-fields)
- [Level aliases and normalization](#level-aliases-and-normalization)
- [The timestamp field, in detail](#the-timestamp-field-in-detail)
- [Validation happens in two stages](#validation-happens-in-two-stages)
- [The client field](#the-client-field)
- [Routing overrides](#routing-overrides)
- [LED display config](#led-display-config)
- [Message sanitization (file logs only)](#message-sanitization-file-logs-only)
- [Full HTTP response table](#full-http-response-table)
- [Where alerts end up](#where-alerts-end-up)

## Required fields

All six must be **present** in the JSON body or the gateway returns `400`.
Presence is checked synchronously; the *values* are not (see
[Validation happens in two stages](#validation-happens-in-two-stages)).

| Field | Type | Constraints |
|-------|------|-------------|
| `timestamp` | integer | Unix epoch **seconds**. No decimals, not ISO-8601, not milliseconds |
| `level` | string | `critical`, `warning`, `notice`, `info`, `debug` (aliases below) |
| `category` | string | `security`, `system`, `weather`, `automation`, `personal`, `network`, `application`, `news`, `business`, `hardware`, `environmental` |
| `title` | string | Short title |
| `message` | string | Detail. Multiline is fine — see message sanitization below |
| `source` | string | Source identifier. Must be a string, not an object |

## Optional fields

| Field | Type | Description |
|-------|------|--------------|
| `client` | string | Log-routing key **and** authorization subject — see [The client field](#the-client-field) |
| `metadata` | object | Arbitrary key-value data. `metadata.led_zones` (array) targets LED sign zones |
| `routing` | object | `{"override": true, "pathways": [...]}` — bypass configured routing rules entirely |
| `display_config` | object | LED sign rendering — see [LED display config](#led-display-config) |

## Level aliases and normalization

`level`, `category`, and `client` are whitespace-trimmed and lowercased
before use. `level` additionally resolves aliases after that. `notice` has
no alias.

| Canonical | Accepted aliases |
|-----------|-------------------|
| `critical` | `error`, `err`, `crit` |
| `warning` | `warn` |
| `info` | `inf` |
| `debug` | `dbg` |

Examples: `" ERROR "` → `critical`. `"  PROD-Web  "` (a `client` value) →
`prod-web`.

## The timestamp field, in detail

There is exactly one check anywhere in this system for `timestamp`: is it a
Python `int` (and not `bool`, which is technically an `int` subclass)?
There is **no range/bounds check, client or server side, at any layer.**
That single fact produces two very different failure experiences:

- **Wrong type** (string, float): caught by the async validation stage,
  silently dropped. Annoying, but at least a known, documented failure mode.
- **Wrong unit** (milliseconds instead of seconds): not caught by *anything*.
  A millisecond epoch value is a valid `int`. It sails through every
  validation stage at every layer and produces a real, delivered alert with
  a timestamp ~1000x too large — landing somewhere in the year 57000-ish
  instead of now. Nothing errors. Nothing logs a warning. It just displays
  wrong wherever the timestamp is rendered.

Concrete numbers: `1732982400` (seconds, correct) vs. `1732982400123`
(milliseconds, silently wrong). This is the single easiest mistake to make
when porting alerting logic from JavaScript (`Date.now()`), Java
(`System.currentTimeMillis()`), or any other millisecond-epoch convention —
always divide by 1000 and truncate to an integer first.

## Validation happens in two stages

The highest-value thing to understand about this API, because the two
stages disagree and only the first one talks back to you.

**Stage 1 — the gateway (`POST /alert`), synchronous.** Checks
`Content-Type: application/json`, that the body parses as JSON, that it's
non-empty, and that the six required field *names* are present. It does
**not** check types or enum values. It then authorizes the client, publishes
internally, and returns `200`.

**Stage 2 — the routing layer, asynchronous, after you already got your
`200`.** Enforces the real schema: types, `level`/`category` enums, aliases,
normalization. A failure here is logged server-side and the alert is
**dropped**. No retry, no callback, nothing in your original HTTP response.

Verified behavior:

| Payload | Gateway (`POST /alert`) | Routing layer | Net result |
|---------|--------------------------|----------------|------------|
| fully valid | `200 success` | accept | delivered |
| `"level": " ERROR "` | `200 success` | accept → normalized to `critical` | delivered |
| `"level": "bogus"` | `200 success` | **reject** | **silently dropped** |
| `"category": "test"` | `200 success` | **reject** | **silently dropped** |
| `"timestamp": "2024-01-15T10:30:00Z"` | `200 success` | **reject** | **silently dropped** |
| `"timestamp": 1732982400.123` | `200 success` | **reject** | **silently dropped** |
| `"timestamp": 1732982400123` (milliseconds) | `200 success` | **accept** | **delivered, with a garbage far-future date** |
| `"source": {"name": "x"}` | `200 success` | **reject** | **silently dropped** |

**Implication:** validate `level`, `category`, and `timestamp` client-side
before you send. The Python SDK does this automatically; `scripts/send-alert.sh`
mirrors the same checks in bash for non-Python senders. If you're writing a
sender from scratch in a third language, hard-code the enum lists above and
check them before the network call — that's the entire value of this
section.

## The client field

`client` determines **where the alert is logged** and **whether you're
allowed to send it at all**. It is not a free-text tag.

**Resolution order** (server-side, in this order):

1. The payload's `client`, after stripping whitespace — a whitespace-only
   value like `"   "` counts as absent and falls through
2. else the authenticated account's configured default client
3. else clientless (`""`)

The value that gets authorized is the same value that gets published — the
gateway does not recompute it after checking.

**Authorization:**

- Your credentials need an `alerts:write` scope covering the effective
  client (or a wildcard scope). Otherwise: `403`.
- The clientless case (`""`) additionally requires the account to hold the
  top administrative role. A regular scoped account cannot send a fully
  clientless alert, even with a wildcard `alerts:write` scope.
- A non-empty effective client must **also** be a registered, active client
  name (registered through a top-admin-only web UI — there is no CLI
  equivalent). Otherwise: `400`, with an explicit "Unknown client" body —
  not a silent drop, and not the generic `403` a missing-scope failure
  returns. This check runs **after** the scope check, so an unauthorized
  caller never learns whether a client name is even registered.

Practically: omit `client` and let the account's configured default apply,
unless you're a multi-tenant sender that legitimately writes for several
clients — in which case set it explicitly and have an operator register the
client name and grant a scope for it (see
`references/auth-and-provisioning.md`).

## Routing overrides

```json
{
  "routing": {"override": true, "pathways": ["email_critical", "led_sign"]}
}
```

`routing.override: true` **replaces** the configured routing rules outright
— the listed `pathways` are used verbatim and no rule is consulted. An
override with an empty or missing `pathways` list routes the alert
**nowhere**. Without an override, rules are evaluated in order and the
first match wins; if nothing matches, the alert is delivered nowhere either.

**`routing.exclude` is not implemented.** You may find it mentioned in
older examples (including a stale doc still tracked in this repo). Only
`override` and `pathways` are read server-side — sending `exclude` has no
effect at all.

## LED display config

Only relevant if the alert routes to an LED sign. Target specific zones
with `metadata.led_zones` (array of zone names).

| Field | Values |
|-------|--------|
| `mode` | `rotate`, `hold`, `flash`, `scroll`, `rollup`, `rolldown`, `rollright`, `rollleft`, `explode`, `newsflash`, `welcome`, `compressed` |
| `color` | `red`, `green`, `amber`, `rainbow1`, `rainbow2`, `autocolor`, `color_mix` |
| `special_effect` | `twinkle`, `sparkle`, `snow`, `starburst`, `bomb`, `welcome`, `cyclecolors` |
| `character_set` | `5high`, `7high`, `10high`, `7shadow`, `7highfancy`, `7widestroke` |
| `speed` | `1`–`5` (slowest to fastest) |
| `priority` | boolean — interrupt normal display rotation |
| `duration` | seconds, for priority messages |

## Message sanitization (file logs only)

File-pathway output collapses each alert to a single line: `\r\n`/`\r`/`\n`
become a visible ` ↵ ` marker, tabs become a space, runs of spaces collapse,
and `message` is truncated (typically 500 chars) with `...`. Email,
database, and console pathways preserve the full multiline text — this only
affects how an alert *renders* in file logs, so send multiline messages
freely; only that one destination reformats them.

## Full HTTP response table

| Code | Condition | Body shape |
|------|-----------|------------|
| `200` | Accepted for routing (**not** delivery-confirmed) | `{"status":"success","message":"Alert forwarded to MQTT broker","alert_id":"<source>-<epoch>","timestamp":"<ISO>"}` |
| `400` | Wrong `Content-Type` | `{"error":"Content-Type must be application/json",...}` |
| `400` | Body isn't valid JSON | `{"error":"Invalid JSON format",...}` |
| `400` | Empty body, **including `{}`** | `{"error":"Empty JSON payload",...}` |
| `400` | Missing required field(s) | `{"error":"Missing required fields: ...","required_fields":[...]}` |
| `400` | Effective `client` isn't a registered, active client | `{"error":"Unknown client","detail":"Client '<name>' is not a registered client.","client":"<name>"}` |
| `401` | Bad credentials, or account locked out | `{"status":"error","error":"authentication required"}` |
| `403` | No `alerts:write` scope for the effective client, or clientless without top-admin role | `{"status":"error","error":"forbidden"}` |
| `500` | Broker publish failed | `{"status":"error","message":"Failed to forward alert to MQTT broker"}` |
| `503` | Credential verification couldn't complete (transient DB issue) | `{"status":"error",...}` |

`{}` returns "Empty JSON payload" rather than "missing required fields" — an
empty dict is falsy, so that check fires first. Retry `500`/`503` (transient);
don't retry `400`/`401`/`403` — see gotcha 5 in `SKILL.md` about not
retrying on connection timeouts either.

## Where alerts end up

| Pathway | Destination |
|---------|-------------|
| file | Per-client, date-rotated log files |
| database | A relational store, written by a separate consumer service |
| email | Outbound email, with optional storm-prevention aggregation for bursts |
| led_sign | Physical LED signs, per zone |
| console | stdout of whatever service has that pathway enabled |

Which pathways a given alert reaches is config-driven (routing rules) unless
overridden per-alert — see [Routing overrides](#routing-overrides).
