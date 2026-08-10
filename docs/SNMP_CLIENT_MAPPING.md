# SNMPv3 Authentication & Client Assignment

**Version 2.0.0**

Complete guide to SNMPv3 user management and automatic client assignment in Alert Manager.

## Table of Contents

- [Overview](#overview)
- [The Username = Client Approach](#the-username--client-approach)
- [User Management Tool](#user-management-tool)
- [Access Control Model](#access-control-model)
- [Complete Workflow](#complete-workflow)
- [Discovery Mode](#discovery-mode)
- [Troubleshooting](#troubleshooting)

## Overview

### The Simplified Approach

In Alert Manager's SNMP Gateway, **the authenticated SNMPv3 username directly becomes the client name**. This eliminates complex IP-based or OID-based mapping rules.

**Key Principle**:
```
SNMPv3 Username → Client Name → Log Segregation + Access Control
```

### Why This Works

1. **SNMPv3 provides strong authentication** - Each device authenticates with unique credentials
2. **No ambiguity** - Username directly maps to client, no rule evaluation needed
3. **Simple management** - Add/remove clients by creating/revoking users
4. **Secure** - Strong cryptographic authentication (SHA + AES)
5. **Audit trail** - Every trap is tied to an authenticated identity

### Example Flow

```
Device Configuration:
  username:  acme-co
  password:  a7k9m3p2

↓

SNMP Trap Sent:
  Authenticated as: acme-co

↓

Alert Manager Processing:
  client = "acme-co"

↓

Log File:
  /data/logs/file/acme-co/2025-11-18.log

↓

HTTP Access Control:
  User with clientname="acme-co" can view this log
```

---

## The Username = Client Approach

### Direct Mapping

When you create an SNMPv3 user, the username **IS** the client name:

| Username | Client Name | Log Path |
|----------|-------------|----------|
| `acme-co` | `acme-co` | `/data/logs/file/acme-co/2025-11-18.log` |
| `office-network` | `office-network` | `/data/logs/file/office-network/2025-11-18.log` |
| `prod-servers` | `prod-servers` | `/data/logs/file/prod-servers/2025-11-18.log` |

### Username Rules

Usernames must follow these requirements:

- **Length**: 3-32 characters
- **Format**: Lowercase letters, numbers, hyphens only (`a-z`, `0-9`, `-`)
- **Restrictions**: Cannot start or end with hyphen
- **Examples**:
  - ✅ `acme-co`
  - ✅ `office-network`
  - ✅ `prod-web-01`
  - ❌ `Acme Co` (uppercase, space)
  - ❌ `office_network` (underscore)
  - ❌ `-production` (starts with hyphen)

### Security Levels

Alert Manager supports **three SNMPv3 security levels** for maximum vendor compatibility:

| Security Level | Authentication | Encryption | Compatibility | Use Case |
|----------------|----------------|------------|---------------|----------|
| **noAuthNoPriv** | Username only | None | **Maximum** | Default - similar to SNMPv2c, works with most vendors |
| **authNoPriv** | SHA/SHA-256 | None | High | Authenticated but not encrypted |
| **authPriv** | SHA/SHA-256 | AES-128/256 | Moderate | Full security - authentication + encryption |

**Default**: `noAuthNoPriv` for maximum compatibility with vendor implementations

---

## User Management Tool

### Using `snmp-auth-manager.sh`

The `snmp-auth-manager.sh` tool provides complete SNMPv3 user lifecycle management.

#### Initialize System

```bash
./tools/snmp-auth-manager.sh init
```

Creates directory structure and empty users database.

#### Create User

```bash
# Create with noAuthNoPriv (default - maximum compatibility)
./tools/snmp-auth-manager.sh create-user acme-co

# Create with noAuthNoPriv (explicit)
./tools/snmp-auth-manager.sh create-user acme-co noAuthNoPriv

# Create with authNoPriv (authenticated, no encryption)
./tools/snmp-auth-manager.sh create-user acme-co authNoPriv [password]

# Create with authPriv (full security)
./tools/snmp-auth-manager.sh create-user acme-co authPriv [password]
```

**Output (noAuthNoPriv)**:
```
Username:       acme-co
Security Level: noAuthNoPriv (username-only)
Client:         acme-co
Status:         active

Device Configuration (SNMPv3):
--------------------
username:         acme-co
security level:   noAuthNoPriv
trap destination: alert.d-t.pw:162

Compatible with most vendor SNMP implementations
```

**Output (authPriv)**:
```
Username:       acme-co
Password:       a7k9m3p2
Security Level: authPriv (authenticated + encrypted)
Auth Protocol:  SHA
Priv Protocol:  AES
Client:         acme-co
Status:         active

Device Configuration (SNMPv3):
--------------------
username:         acme-co
password:         a7k9m3p2
security level:   authPriv
auth protocol:    SHA/SHA-1
priv protocol:    AES/AES-128
trap destination: alert.d-t.pw:162
```

#### List Users

```bash
./tools/snmp-auth-manager.sh list
```

**Output**:
```
SNMPv3 Users:
=============

USERNAME             CLIENT          STATUS          CREATED              LAST USED
--------             ------          ------          -------              ---------
acme-co             acme-co        active          2025-11-18T10:30:00  2025-11-18T14:22:15
office-network       office-network  active          2025-11-18T11:00:00  never
prod-servers         prod-servers    revoked         2025-11-15T09:15:00  2025-11-17T23:45:10
```

#### Show User Details

```bash
./tools/snmp-auth-manager.sh show acme-co
```

**Output**:
```
SNMPv3 User Details: acme-co
==============================

Username:       acme-co
Client:         acme-co
Status:         active
Auth Protocol:  SHA
Priv Protocol:  AES
Created:        2025-11-18T10:30:00Z
Last Used:      2025-11-18T14:22:15Z

Password (unified auth/priv):
  a7k9m3p2
```

#### Reset Password

```bash
# Auto-generate new password
./tools/snmp-auth-manager.sh reset-password acme-co

# Specify new password
./tools/snmp-auth-manager.sh reset-password acme-co newpass99
```

#### Revoke User Access

```bash
./tools/snmp-auth-manager.sh revoke acme-co
```

User can no longer authenticate. Traps from this device will be rejected.

#### Restore Revoked User

```bash
./tools/snmp-auth-manager.sh restore acme-co
```

Re-enable a previously revoked user.

#### Delete User Permanently

```bash
./tools/snmp-auth-manager.sh delete acme-co
```

Permanently removes user from database (requires confirmation).

---

## Access Control Model

### User Types

Alert Manager supports three HTTP user types for log viewing:

| User Type | Clientname | Send Alerts | View Logs | Client Behavior |
|-----------|------------|-------------|-----------|-----------------|
| **admin** | `null` | Yes | **ALL** (ops/, file/) | Can set any client |
| **node** | Required | Yes | file/{clientname}*.log | HTTP: auto-inherits; SNMP: N/A |
| **client** | Required | No (view-only) | file/{clientname}*.log | View only |

### SNMP Users vs HTTP Users

**SNMP users** (managed by `snmp-auth-manager.sh`):
- Used by devices to send traps
- Username determines client name automatically
- Configured in `/data/config/snmp-gateway/users.json`

**HTTP users** (managed by `http-auth-manager.sh`):
- Used by humans/systems to view logs via web interface
- Must have matching `clientname` to view corresponding logs
- Configured in `/data/config/http-gateway/users.json`

### Creating HTTP Users for Log Access

After creating SNMP user, create corresponding HTTP user to view logs:

```bash
# SNMP user (device authentication)
./tools/snmp-auth-manager.sh create-user acme-co

# HTTP user (log viewing)
./tools/http-auth-manager.sh create-user acme-co-viewer SecurePass123! client acme-co
```

Now:
- Device sends traps as `acme-co` → Logs to `file/acme-co/2025-11-18.log`
- User `acme-co-viewer` can view `file/acme-co/*.log` files

### Log Directory Structure

```
data/logs/
├── ops/                                  # Admin-only operational logs
│   ├── alert-router.2025-11-18.log
│   ├── alert-http-gateway.2025-11-18.log
│   └── alert-snmp-gateway.2025-11-18.log
└── file/                                 # Client-specific alert logs
    ├── acme-co/
    │   └── 2025-11-18.log                # client="acme-co"
    ├── office-network/
    │   └── 2025-11-18.log                # client="office-network"
    ├── prod-servers/
    │   └── 2025-11-18.log                # client="prod-servers"
    └── unauthenticated/
        └── 2025-11-18.log                # No valid authentication
```

### Access Examples

**Example 1: Office Network Viewer**

```bash
# Create SNMP user for office devices
./tools/snmp-auth-manager.sh create-user office-network

# Create HTTP viewer
./tools/http-auth-manager.sh create-user office-viewer password123 client office-network
```

**Can view:**
- ✅ `file/office-network/2025-11-18.log`

**Cannot view:**
- ❌ `file/acme-co/2025-11-18.log`
- ❌ `file/prod-servers/2025-11-18.log`
- ❌ `ops/alert-snmp-gateway.2025-11-18.log`

**Example 2: IT Admin**

```bash
./tools/http-auth-manager.sh create-user it-admin password456 admin
```

**Can view:**
- ✅ ALL logs in ops/
- ✅ ALL logs in file/
- ✅ Can send alerts to any client

---

## Complete Workflow

### Scenario: Adding New Device Client

Let's add monitoring for a new network segment called "warehouse".

#### Step 1: Create SNMPv3 User

```bash
cd /mnt/c/Users/Jimi/Documents/Code/alertManager

# Create with noAuthNoPriv (recommended for maximum compatibility)
./tools/snmp-auth-manager.sh create-user warehouse

# Or create with authPriv if your device requires full security
./tools/snmp-auth-manager.sh create-user warehouse authPriv
```

**Output (noAuthNoPriv)**:
```
Username:       warehouse
Security Level: noAuthNoPriv (username-only)
Client:         warehouse
Status:         active
```

**Output (authPriv)**:
```
Username:       warehouse
Password:       x3m8k2p5
Security Level: authPriv (authenticated + encrypted)
Auth Protocol:  SHA
Priv Protocol:  AES
Client:         warehouse
```

#### Step 2: Configure Device

**For noAuthNoPriv (most compatible)**:
```
SNMPv3 Configuration:
--------------------
Username:         warehouse
Security Level:   noAuthNoPriv
Trap Destination: alert.d-t.pw
Trap Port:        162
```

**For authPriv (high security)**:
```
SNMPv3 Configuration:
--------------------
Username:         warehouse
Password:         x3m8k2p5
Auth Hash:        SHA / SHA-1
Privacy:          AES / AES-128
Security Level:   authPriv
Trap Destination: alert.d-t.pw
Trap Port:        162
```

#### Step 3: Restart SNMP Gateway

```bash
docker-compose restart snmp-gateway
```

Watch logs:
```bash
docker logs -f alert-snmp-gateway
```

You should see (noAuthNoPriv):
```
Configured SNMPv3 user: warehouse (security=noAuthNoPriv, client=warehouse, VACM=enabled)
```

Or (authPriv):
```
Configured SNMPv3 user: warehouse (security=authPriv, auth=SHA, priv=AES, client=warehouse, VACM=enabled)
```

#### Step 4: Test Device

Trigger a test trap from your device. Check discovery logs:

```bash
tail -f data/logs/snmp-discovery/snmpFULL.*.jsonl | jq '.snmp_username, .assigned_client'
```

**Output**:
```
"warehouse"
"warehouse"
```

#### Step 5: Verify Log File

```bash
ls -la data/logs/file/warehouse/
```

**Output**:
```
-rw-r--r-- 1 user user 1234 Nov 18 14:30 2025-11-18.log
```

#### Step 6: Create HTTP Viewer (Optional)

```bash
./tools/http-auth-manager.sh create-user warehouse-viewer SecurePass123! client warehouse
```

Now `warehouse-viewer` can log into `http://alert.d-t.pw:8080/logs` and see only warehouse logs.

---

## Discovery Mode

### Purpose

Discovery mode captures ALL incoming traps with detailed metadata, including authenticated username. This helps you:

1. Verify devices are authenticating correctly
2. See what client names are being assigned
3. Troubleshoot authentication issues
4. Identify unauthenticated devices

### Enable Discovery Mode

Edit `data/config/docker.json`:

```json
{
  "snmp": {
    "discovery_mode": {
      "enabled": true,
      "log_path": "/data/logs/snmp-discovery/snmpFULL.{date}.jsonl",
      "accept_all_traps": true,
      "log_all_varbinds": true,
      "forward_to_mqtt": false
    }
  }
}
```

Restart:
```bash
docker-compose restart snmp-gateway
```

### Analyze Discovery Logs

**View recent traps with usernames**:
```bash
tail -20 data/logs/snmp-discovery/snmpFULL.*.jsonl | jq '.source_ip, .snmp_username, .assigned_client'
```

**Output**:
```
"192.168.1.10"
"office-network"
"office-network"

"10.5.20.15"
"warehouse"
"warehouse"

"192.168.50.99"
null
"unauthenticated"
```

**Count traps by username**:
```bash
jq -r '.snmp_username // "none"' data/logs/snmp-discovery/snmpFULL.*.jsonl | sort | uniq -c
```

**Output**:
```
   245 office-network
    89 warehouse
    12 acme-co
     5 none
```

**Find unauthenticated devices**:
```bash
jq 'select(.snmp_username == null)' data/logs/snmp-discovery/snmpFULL.*.jsonl
```

---

## Troubleshooting

### All Traps Showing "unauthenticated"

**Symptom**: Discovery logs show `"snmp_username": null` for all traps.

**Possible Causes**:
1. Device is using SNMPv1 or SNMPv2c (not SNMPv3)
2. Wrong username configured on device
3. Wrong password configured on device (for authNoPriv/authPriv)
4. Device security level doesn't match server configuration

**Solutions**:
1. Verify device is configured for SNMPv3
2. Check username matches exactly (case-sensitive)
3. Verify security levels match:
   - Server: noAuthNoPriv → Device: noAuthNoPriv
   - Server: authNoPriv → Device: authNoPriv
   - Server: authPriv → Device: authPriv
4. For password-based levels (authNoPriv/authPriv), reset password:
   ```bash
   ./tools/snmp-auth-manager.sh reset-password warehouse
   ```
5. Consider using noAuthNoPriv for maximum compatibility:
   ```bash
   ./tools/snmp-auth-manager.sh delete warehouse
   ./tools/snmp-auth-manager.sh create-user warehouse noAuthNoPriv
   ```

### User Cannot View Logs

**Symptom**: HTTP user logs in but sees no log files.

**Check**:
1. Verify HTTP user `clientname` matches SNMP username:
   ```bash
   ./tools/http-auth-manager.sh show warehouse-viewer
   ```
   Should show: `Client: warehouse`

2. Verify log file exists:
   ```bash
   ls -la data/logs/file/warehouse/
   ```

3. Verify SNMP user is active:
   ```bash
   ./tools/snmp-auth-manager.sh show warehouse
   ```
   Should show: `Status: active`

### Authentication Failures in Gateway Logs

**Symptom**: Gateway logs show authentication errors.

```bash
docker logs alert-snmp-gateway | grep -i auth
```

**Common Issues**:
- **Wrong password**: Reset password and update device
- **Wrong protocol**: Ensure device uses SHA (not MD5) and AES (not DES)
- **Revoked user**: Restore user if accidentally revoked:
  ```bash
  ./tools/snmp-auth-manager.sh restore warehouse
  ```

### Password Requirements

**If you see**: "Password must be at least 8 characters long"

SNMPv3 requires minimum 8 character passwords. Auto-generated passwords are always valid:

```bash
./tools/snmp-auth-manager.sh create-user warehouse
```

Or specify your own (8+ chars, any characters):
```bash
./tools/snmp-auth-manager.sh create-user warehouse MySecure123!
```

### Device Configuration Help

**Device Interface Shows Different Terms**:

| Your Device Says | Alert Manager Equivalent |
|------------------|--------------------------|
| Read-Only Username | Username (same) |
| Read-Only Password | Password (same) |
| Auth Protocol / Auth Hash | SHA / SHA-1 |
| Priv Protocol / Privacy Algorithm | AES / AES-128 |
| Security Level | authPriv or "auth, priv" |
| Community String (v1/v2c) | **Not used** (SNMPv3 only) |

---

## Best Practices

1. **Meaningful usernames** - Use descriptive client names like `warehouse-network` instead of `client1`
2. **One user per logical group** - Separate clients by department, location, or function
3. **Revoke promptly** - Revoke users when devices are decommissioned
4. **Secure passwords** - Use auto-generated passwords (8 chars, random)
5. **Regular audits** - Review user list quarterly:
   ```bash
   ./tools/snmp-auth-manager.sh list
   ```
6. **Discovery mode** - Run discovery periodically to find misconfigured devices
7. **Separate HTTP users** - Don't reuse SNMP passwords for HTTP access

---

## Migration from Old System

If you previously used IP-based or OID-based client mapping rules:

### Old Approach (Deprecated)
```json
{
  "client_mapping_rules": [
    {
      "name": "Warehouse Network",
      "conditions": {"source_ip_ranges": ["10.50.0.0/24"]},
      "client": "warehouse"
    }
  ]
}
```

### New Approach
```bash
# Create SNMPv3 user with username = client name
./tools/snmp-auth-manager.sh create-user warehouse

# Configure devices in 10.50.0.0/24 to use username "warehouse"
```

**Benefits**:
- ✅ No mapping rules to maintain
- ✅ No IP subnet tracking required
- ✅ Strong cryptographic authentication
- ✅ Clear audit trail (username in every log entry)

---

## Related Documentation

- **SNMP_INTEGRATION.md** - Device configuration and testing guide
- **SNMPV3_SPECIFICATION.md** - Technical protocol details
- **CLIENTNODE.md** - Alert payload structure and user types
- **AUTH.md** - HTTP user management and authentication

---

*This document is part of the Alert Manager SNMP Gateway implementation.*
