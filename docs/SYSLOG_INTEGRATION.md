# Syslog Integration Guide

---
## ⚠️ DEPRECATED (v1.2.0) - DO NOT USE FOR NEW INTEGRATIONS

**This integration method has been deprecated as of version 1.2.0.**

**Use these alternatives instead:**
- **SNMPv3** (Network devices): Port 162/UDP, USM authentication, per-device client assignment
- **HTTP** (Scripts/services): Port 8080, Basic Auth, full payload support
- **MQTT** (IoT/high-volume): Port 42690, TLS 1.2, persistent connections

**Why deprecated:**
- No `client` field support (cannot route to per-client logs)
- Limited category mapping (only `security` and `system`)
- No authentication or encryption
- No metadata, routing, or display configuration support

**This documentation is preserved for reference only. The syslog gateway service is disabled by default.**

---

## Connection Details (Deprecated)

- **Host**: `alert.d-t.pw`
- **Port**: `51514`
- **Protocol**: `UDP` (unencrypted)
- **Format**: RFC5424

## Message Format

**RFC5424 Syslog Format:**
```
<PRIORITY>VERSION TIMESTAMP HOSTNAME APP-NAME PROCID MSGID - MESSAGE
```

**Example:**
```
<134>1 2025-01-15T10:30:00Z webserver01 nginx 1234 ID47 - User login successful
```

## Internal Alert Format

Syslog messages are converted to the following JSON format before routing:

```json
{
  "timestamp": 1736945400,
  "level": "info",
  "category": "system",
  "title": "Syslog from webserver01",
  "message": "User login successful",
  "source": {
    "id": "webserver01/nginx",
    "type": "syslog",
    "metadata": {
      "facility": 16,
      "severity": 6,
      "client_ip": "192.168.1.100"
    }
  },
  "metadata": {
    "syslog_facility": 16,
    "syslog_severity": 6
  }
}
```

**Key Format Details:**
- **timestamp**: Unix epoch integer (seconds since 1970-01-01)
- **source**: Object with `id`, `type`, and `metadata` fields
- All timestamps are Unix epoch integers, not ISO strings

## Priority Mapping

Priority = (Facility × 8) + Severity

**Common priorities using facility 16 (local0):**

| Priority | Syslog Severity | Alert Level | Description |
|----------|----------------|-------------|-------------|
| 130 | 2 (Critical) | critical | Emergency/Alert/Critical messages |
| 131 | 3 (Error) | warning | Error conditions |
| 132 | 4 (Warning) | warning | Warning conditions |
| 133 | 5 (Notice) | notice | Normal but significant operational events |
| 134 | 6 (Info) | info | Informational messages |
| 135 | 7 (Debug) | debug | Debug messages |

**Severity to Alert Level Mapping:**
- Severities 0-2 (Emergency, Alert, Critical) → `critical`
- Severities 3-4 (Error, Warning) → `warning`
- Severity 5 (Notice) → `notice`
- Severity 6 (Informational) → `info`
- Severity 7 (Debug) → `debug`

**Facility to Category Mapping:**
- Facilities 4, 10, 13, 14 (auth, security, audit) → `security`
- All other facilities → `system`

## Configuration Examples

### Linux rsyslog
```bash
# /etc/rsyslog.d/50-alert-manager.conf
*.* @alert.d-t.pw:51514

# Restart
sudo systemctl restart rsyslog
```

### Cisco IOS
```
logging host alert.d-t.pw transport udp port 51514
logging trap informational
```

### Shell Script (nc/netcat)
```bash
# Send RFC5424 formatted message
send_syslog() {
    local priority=$1
    local app_name=$2
    local message=$3
    local timestamp=$(date -u '+%Y-%m-%dT%H:%M:%SZ')
    local hostname=$(hostname)
    local pid=$$

    echo "<${priority}>1 ${timestamp} ${hostname} ${app_name} ${pid} - - ${message}" | nc -u alert.d-t.pw 51514
}

# Examples
send_syslog 130 security "CRITICAL: Unauthorized access detected"
send_syslog 134 app-monitor "INFO: Application status normal"
```

## Quick Test

```bash
# Using netcat (RFC5424 format)
echo "<134>1 $(date -u '+%Y-%m-%dT%H:%M:%SZ') $(hostname) test-app $$ MSG01 - Test message" | nc -u alert.d-t.pw 51514

# Critical alert
echo "<130>1 $(date -u '+%Y-%m-%dT%H:%M:%SZ') $(hostname) security $$ CRIT01 - CRITICAL: Security breach detected" | nc -u alert.d-t.pw 51514
```

## Firewall Requirements

- **Direction**: Outbound
- **Protocol**: UDP
- **Destination**: alert.d-t.pw:51514
