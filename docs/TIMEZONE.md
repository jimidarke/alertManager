# Timezone Handling Specification

**Version:** 1.0.0
**Last Updated:** 2025-12-01

This document describes how time data is handled across the Alert Manager system to ensure consistency and correct timezone behavior.

## Core Principle: Unix Epoch Storage, Local Display

**All timestamps are stored as Unix epoch integers (seconds since 1970-01-01 00:00:00 UTC).**

This approach provides:
- Timezone-agnostic storage (no ambiguity)
- Simple arithmetic for time comparisons
- Consistent behavior across all system components
- Client-side flexibility for display formatting

## Data Flow

```
┌─────────────────┐     Unix Epoch      ┌─────────────────┐
│  Alert Source   │ ──────────────────► │   MQTT Broker   │
│  (Client Node)  │    (integer)        │                 │
└─────────────────┘                     └────────┬────────┘
                                                 │
                                        Unix Epoch (integer)
                                                 │
                    ┌────────────────────────────┼────────────────────────────┐
                    │                            │                            │
                    ▼                            ▼                            ▼
           ┌────────────────┐          ┌────────────────┐          ┌────────────────┐
           │  File Pathway  │          │    Database    │          │  Email/LED     │
           │  (log files)   │          │  (PostgreSQL)  │          │   Pathways     │
           └───────┬────────┘          └───────┬────────┘          └────────────────┘
                   │                           │
          Unix Epoch in logs          to_timestamp() → TIMESTAMPTZ
                   │                           │
                   ▼                           ▼
           ┌────────────────┐          ┌────────────────┐
           │  Log Viewer UI │          │   Analytics    │
           │  (JavaScript)  │          │    Engine      │
           └────────────────┘          └────────────────┘
                   │                           │
      Intl.DateTimeFormat()           pytz for display
      with user-selected TZ           in email reports
```

## Component Details

### 1. Alert Payloads

All alerts MUST include a Unix epoch timestamp:

```json
{
  "timestamp": 1733097600,
  "level": "info",
  "category": "system",
  "title": "Example Alert",
  "message": "This is an example",
  "source": "my-device"
}
```

**Validation Rules:**
- `timestamp` must be an integer (not a float, not an ISO 8601 string)
- Value represents seconds since Unix epoch (1970-01-01 00:00:00 UTC)
- No decimal places (one-second precision)

**Code Reference:** `src/alert_router.py:236-238`
```python
if not isinstance(alert['timestamp'], int):
    raise ValueError("timestamp must be Unix epoch integer")
```

### 2. Log Files (File Pathway)

Log lines store the raw Unix epoch timestamp:

```
1733097600 [info] [system] my-device [client]: Example Alert - This is an example
```

**Format:** `{timestamp} [{level}] [{category}] {source} [{client}]: {title} - {message}`

The timestamp remains a Unix epoch integer in the log file, enabling:
- Simple parsing with regex
- Client-side timezone conversion
- No timezone ambiguity in stored data

### 3. Log Viewer UI (HTTP Gateway)

The web-based log viewer converts Unix timestamps to local time client-side using JavaScript's `Intl.DateTimeFormat` API.

**Implementation:** `src/static/js/log-viewer.js:429-459`

```javascript
function formatLocalizedTime(unixTimestamp) {
    const date = new Date(unixTimestamp * 1000);
    const options = {
        timeZone: selectedTimezone,  // User-selected timezone
        month: 'short',
        day: 'numeric',
        hour: 'numeric',
        minute: '2-digit',
        hour12: true
    };
    const formatter = new Intl.DateTimeFormat('en-US', options);
    return formatter.format(date);
}
```

**Timezone Selection:**
- User can select from North American timezones via dropdown
- Default: `America/Edmonton`
- Preference persisted in `localStorage`
- Available timezones defined in `northAmericanTimezones` array

**Supported Timezones:**
| Region | Timezone | Label |
|--------|----------|-------|
| Canada | America/St_Johns | Newfoundland (NST/NDT) |
| Canada | America/Halifax | Atlantic (AST/ADT) |
| Canada | America/Toronto | Eastern (EST/EDT) |
| Canada | America/Winnipeg | Central (CST/CDT) |
| Canada | America/Edmonton | Mountain (MST/MDT) |
| Canada | America/Vancouver | Pacific (PST/PDT) |
| USA | America/New_York | US Eastern (EST/EDT) |
| USA | America/Chicago | US Central (CST/CDT) |
| USA | America/Denver | US Mountain (MST/MDT) |
| USA | America/Phoenix | US Arizona (MST) |
| USA | America/Los_Angeles | US Pacific (PST/PDT) |
| USA | America/Anchorage | Alaska (AKST/AKDT) |
| USA | Pacific/Honolulu | Hawaii (HST) |
| Mexico | America/Mexico_City | Mexico Central (CST/CDT) |
| Mexico | America/Cancun | Mexico Eastern (EST) |
| Mexico | America/Mazatlan | Mexico Mountain (MST/MDT) |
| Mexico | America/Tijuana | Mexico Pacific (PST/PDT) |

### 4. PostgreSQL Database

The database uses `TIMESTAMPTZ` (timestamp with time zone) for storage:

```sql
timestamp TIMESTAMPTZ NOT NULL
```

**Conversion on Insert:** `to_timestamp(unix_epoch)`

This converts the Unix epoch integer to a PostgreSQL timestamp. PostgreSQL stores all `TIMESTAMPTZ` values internally as UTC, then converts to the session timezone on retrieval.

### 5. Docker Container Timezone

All Docker containers are configured with:

```yaml
environment:
  - TZ=America/Edmonton
```

This ensures:
- Consistent log file date rotation (`{date}` placeholder uses local date)
- Correct scheduling behavior for analytics jobs
- Consistent `datetime.now()` behavior across all Python services

**Important:** The `schedule` library in Python uses system time. Since containers have `TZ=America/Edmonton`, scheduled jobs run at the specified local time.

### 6. Analytics Engine Scheduling

The analytics engine supports timezone-aware scheduling:

```json
{
  "schedule": {
    "frequency": "daily",
    "time": "18:00",
    "timezone": "America/Edmonton"
  }
}
```

**Implementation:** `src/analytics_engine.py:139-157`

Since Docker containers have `TZ=America/Edmonton` set, the schedule library automatically uses local time. The `timezone` config field is for documentation and future flexibility.

### 7. Email Reports (Analytics)

Email reports display timestamps in the configured timezone:

**Implementation:** `src/analyzers/barcode_not_found.py:133-146`

```python
def _get_local_time_str(self) -> str:
    timezone_str = self.config.get('schedule', {}).get('timezone', 'America/Edmonton')
    tz = pytz.timezone(timezone_str)
    local_time = datetime.now(tz)
    return local_time.strftime('%Y-%m-%d %H:%M:%S %Z')
```

**Output Example:** `2025-12-01 18:00:00 MST`

## Date-Based Log Rotation

Log files use the `{date}` placeholder which is replaced based on container local time:

| Rotation Period | Behavior | Example |
|-----------------|----------|---------|
| `daily` | Current date (YYYY-MM-DD) | `2025-12-01` |
| `weekly` | Monday of current week | `2025-11-25` (for week Nov 25-Dec 1) |

**Timezone Impact:** Since containers use `TZ=America/Edmonton`, log rotation occurs at midnight Mountain Time.

## API Response Timestamps

HTTP API responses use ISO 8601 format with UTC indicator:

```json
{
  "timestamp": "2025-12-01T18:00:00Z",
  "status": "success"
}
```

**Code Pattern:**
```python
datetime.now(pytz.UTC).isoformat() + 'Z'
```

This is used for API responses only (not for alert payloads which use Unix epoch).

## Best Practices

### For Client Nodes (Sending Alerts)

1. **Always use Unix epoch integers:**
   ```python
   import time
   timestamp = int(time.time())
   ```

2. **Never use ISO 8601 strings in alert payloads**

3. **Never include decimal places** (no milliseconds)

### For Display/UI Code

1. **Store timestamps as Unix epoch**
2. **Convert to local time only at display time**
3. **Use `Intl.DateTimeFormat` (JavaScript) or `pytz` (Python) for conversion**
4. **Let user select their preferred timezone**

### For Database Queries

1. **Use `TIMESTAMPTZ` columns**
2. **Convert Unix epoch on insert:** `to_timestamp(epoch_value)`
3. **PostgreSQL handles timezone conversion automatically**

### For Scheduled Jobs

1. **Set `TZ` environment variable in Docker containers**
2. **Use 24-hour time format in config** (e.g., `"18:00"` not `"6:00 PM"`)
3. **Document the timezone in config for clarity**

## Troubleshooting

### Logs showing wrong date

**Symptom:** Log files have yesterday's date after midnight
**Cause:** Container timezone not set
**Fix:** Ensure `TZ=America/Edmonton` in docker-compose.yml

### Scheduled job runs at wrong time

**Symptom:** Analytics job runs 6-7 hours early/late
**Cause:** Container using UTC instead of local timezone
**Fix:** Ensure `TZ=America/Edmonton` in docker-compose.yml

### Log viewer showing wrong time

**Symptom:** Timestamps in UI don't match expected local time
**Cause:** Wrong timezone selected in dropdown
**Fix:** Select correct timezone from dropdown (persists in localStorage)

### Email report shows wrong timezone

**Symptom:** Report generation time shows UTC instead of local
**Cause:** Missing `timezone` in schedule config
**Fix:** Add `"timezone": "America/Edmonton"` to analyzer schedule config

## Summary Table

| Component | Storage Format | Display Format | Timezone Handling |
|-----------|---------------|----------------|-------------------|
| Alert Payloads | Unix epoch (int) | N/A | UTC (implicit) |
| Log Files | Unix epoch (int) | N/A | UTC (implicit) |
| Log Viewer UI | Unix epoch (int) | Local time | User-selected via dropdown |
| PostgreSQL | TIMESTAMPTZ | Varies | Session timezone |
| Email Reports | N/A | Local time string | Configured in analyzer |
| API Responses | ISO 8601 + Z | UTC | Always UTC |
| Scheduled Jobs | N/A | N/A | Container TZ env var |
