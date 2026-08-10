# SNMP Integration Guide

**Version 1.0.0**

Practical guide for integrating network devices with the Alert Manager SNMP Gateway.

## Quick Start

### Prerequisites

- Alert Manager installed and running
- Network devices with SNMP v3 support
- Network connectivity on UDP port 162
- Basic understanding of SNMP concepts

### 5-Minute Setup

1. **Create SNMPv3 User** (username = client name):
```bash
cd /mnt/c/Users/Jimi/Documents/Code/alertManager
./tools/snmp-auth-manager.sh init

# Create with noAuthNoPriv (recommended - maximum compatibility)
./tools/snmp-auth-manager.sh create-user mydevice

# Or with authPriv (if device requires full security)
# ./tools/snmp-auth-manager.sh create-user mydevice authPriv
```

**Output (noAuthNoPriv)**:
```
Username:       mydevice
Security Level: noAuthNoPriv (username-only)
Client:         mydevice
Status:         active

Device Configuration (SNMPv3):
--------------------
username:         mydevice
security level:   noAuthNoPriv
trap destination: alert.d-t.pw:162

Compatible with most vendor SNMP implementations
```

2. **Start SNMP Gateway**:
```bash
docker-compose build --no-cache alert-router
docker-compose --profile snmp up -d snmp-gateway
```

3. **Configure Network Device** (send traps to `alert.d-t.pw:162`):

**For noAuthNoPriv (most compatible)**:
- SNMPv3 trap destination: `alert.d-t.pw`
- Port: `162`
- Username: `mydevice`
- Security Level: `noAuthNoPriv`

**For authPriv (high security)**:
- SNMPv3 trap destination: `alert.d-t.pw`
- Port: `162`
- Username: `mydevice`
- Password: `x5k8m2p9` (from create-user output)
- Auth Hash: SHA / SHA-1
- Privacy: AES / AES-128
- Security Level: authPriv

4. **Test** (from device or using snmptrap):

**noAuthNoPriv test**:
```bash
snmptrap -v 3 -l noAuthNoPriv -u mydevice \
  alert.d-t.pw:162 '' 1.3.6.1.6.3.1.1.5.3
```

**authPriv test**:
```bash
snmptrap -v 3 -l authPriv -u mydevice \
  -a SHA -A x5k8m2p9 \
  -x AES -X x5k8m2p9 \
  alert.d-t.pw:162 '' 1.3.6.1.6.3.1.1.5.3
```

5. **View Logs**:
```bash
docker logs -f alert-snmp-gateway
tail -f data/logs/file/mydevice/2025-11-18.log
```

**Note**: Username directly becomes the client name. Traps log to `/data/logs/file/{username}/`.

## Discovery Mode - Finding What's Available

**Discovery mode captures ALL incoming traps** for analysis, helping you understand what data your devices are sending.

### Step 1: Enable Discovery Mode

Edit `data/config/docker.json`:

```json
{
  "snmp": {
    "enabled": true,
    "port": 162,
    "discovery_mode": {
      "enabled": true,
      "log_path": "/data/logs/snmp-discovery/snmpFULL.{date}.jsonl",
      "accept_all_traps": true,
      "log_all_varbinds": true,
      "forward_to_mqtt": false,
      "duration_days": 7
    },
    "users_file": "/data/config/snmp-gateway/users.json"
  }
}
```

**Note**: SNMPv3 users are managed separately using `./tools/snmp-auth-manager.sh`.

**Key Settings**:
- `enabled: true` - Activates discovery mode
- `accept_all_traps: true` - No filtering, capture everything
- `log_all_varbinds: true` - Include full varbind data
- `forward_to_mqtt: false` - Don't trigger alerts during discovery
- `duration_days: 7` - Recommended capture period

### Step 2: Restart Gateway

```bash
docker-compose restart snmp-gateway
```

### Step 3: Wait and Collect Data

Let the gateway run for 7-14 days to capture a representative sample of traps.

**What to Monitor**:
- Regular events (daily, hourly)
- Error conditions (manually trigger if possible)
- Threshold alerts (CPU spikes, bandwidth peaks)
- Device state changes (reboots, interface up/down)

### Step 4: Analyze Captured Data

Discovery logs are in JSONL format (one JSON object per line):

```bash
cd data/logs/snmp-discovery/
```

**Count trap types by OID**:
```bash
jq -r '.trap_oid' discovery.*.jsonl | sort | uniq -c | sort -rn
```

Output example:
```
    245 1.3.6.1.6.3.1.1.5.3    # linkDown (frequent)
    240 1.3.6.1.6.3.1.1.5.4    # linkUp (recovery)
     12 1.3.6.1.4.1.9.9.41.2.0.1  # Cisco environmental alert
      5 1.3.6.1.6.3.1.1.5.1    # coldStart (reboots)
```

**List all unique varbind OIDs**:
```bash
jq -r '.varbinds[].oid' discovery.*.jsonl | sort -u > all_varbind_oids.txt
```

**Find numeric varbinds (for threshold monitoring)**:
```bash
jq 'select(.varbinds[].type == "Integer" or .varbinds[].type == "Gauge32")' discovery.*.jsonl
```

**Identify source devices**:
```bash
jq -r '.source_ip' discovery.*.jsonl | sort -u
```

**View full trap example**:
```bash
jq '.' discovery.*.jsonl | head -50
```

### Step 5: Build OID Mappings

Based on your analysis, create mappings in `docker.json`:

```json
{
  "snmp": {
    "oid_mappings": [
      {
        "trap_oid": "1.3.6.1.6.3.1.1.5.3",
        "trap_name": "linkDown",
        "level": "warning",
        "category": "network",
        "title_template": "Link Down: {IF-MIB::ifDescr}",
        "message_template": "Interface {IF-MIB::ifDescr} is down on {SNMPv2-MIB::sysName}"
      }
    ],
    "threshold_rules": [
      {
        "varbind_oid": "1.3.6.1.4.1.2021.11.9.0",
        "varbind_name": "UCD-SNMP-MIB::ssCpuIdle",
        "metric_name": "cpu_idle_percent",
        "thresholds": {
          "critical": {"operator": "<", "value": 10},
          "warning": {"operator": "<", "value": 20}
        },
        "category": "system",
        "title_template": "High CPU Usage",
        "message_template": "CPU idle at {value}% on {hostname}"
      }
    ]
  }
}
```

### Step 6: Disable Discovery Mode

Once you've built your mappings:

```json
{
  "snmp": {
    "discovery_mode": {
      "enabled": false
    }
  }
}
```

Restart gateway:
```bash
docker-compose restart snmp-gateway
```

## User Management & Access Control

### Username = Client Name

**In Alert Manager's SNMP Gateway, the authenticated SNMPv3 username directly becomes the client name.** This eliminates complex mapping rules.

**How It Works:**
1. Create SNMPv3 user with `./tools/snmp-auth-manager.sh create-user office-network`
2. Configure device to authenticate as `office-network`
3. Device sends trap → Authenticated as `office-network` → Logs to `file/office-network/`
4. HTTP users with `clientname=office-network` can view these logs

### Managing SNMPv3 Users

**Create User (username = client name)**:
```bash
# Create with noAuthNoPriv (default - maximum compatibility)
./tools/snmp-auth-manager.sh create-user office-network

# Or specify security level
./tools/snmp-auth-manager.sh create-user office-network noAuthNoPriv
./tools/snmp-auth-manager.sh create-user office-network authNoPriv [password]
./tools/snmp-auth-manager.sh create-user office-network authPriv [password]
```

**Output (noAuthNoPriv)**:
```
Username:       office-network
Security Level: noAuthNoPriv (username-only)
Client:         office-network
Status:         active
```

**Output (authPriv)**:
```
Username:       office-network
Password:       x5k8m2p9
Security Level: authPriv (authenticated + encrypted)
Auth Protocol:  SHA
Priv Protocol:  AES
Client:         office-network
```

**List All Users**:
```bash
./tools/snmp-auth-manager.sh list
```

**Show User Details**:
```bash
./tools/snmp-auth-manager.sh show office-network
```

**Reset Password**:
```bash
./tools/snmp-auth-manager.sh reset-password office-network
```

**Revoke Access**:
```bash
./tools/snmp-auth-manager.sh revoke office-network
```

### Creating HTTP Users for Log Viewing

After creating SNMP user, create corresponding HTTP user to view logs:

```bash
# SNMP user (device authentication)
./tools/snmp-auth-manager.sh create-user office-network

# HTTP user (log viewing)
./tools/http-auth-manager.sh create-user office-viewer SecurePass123! client office-network
```

**Result:**
- Devices authenticate as `office-network` → Logs to `file/office-network/`
- User `office-viewer` can view `file/office-network/*.log`
- User cannot see other clients' logs or ops/ logs

### Complete Example

**Scenario**: Separate monitoring for office network and warehouse:

```bash
# Create SNMP users (one per client/location)
./tools/snmp-auth-manager.sh create-user office-network
./tools/snmp-auth-manager.sh create-user warehouse

# Create HTTP viewers
./tools/http-auth-manager.sh create-user office-viewer pass123 client office-network
./tools/http-auth-manager.sh create-user warehouse-viewer pass456 client warehouse

# Create IT admin (sees everything)
./tools/http-auth-manager.sh create-user it-admin secure789 admin
```

**Configure Devices:**
- Office devices: Use username `office-network`
- Warehouse devices: Use username `warehouse`

**Log Structure:**
```
data/logs/file/
├── office-network/
│   └── 2025-11-18.log
├── warehouse/
│   └── 2025-11-18.log
└── unauthenticated/
    └── 2025-11-18.log   # Devices with invalid credentials
```

### Verification

**Check gateway logs:**
```bash
docker logs alert-snmp-gateway | grep "authenticated as"
```

**Output**:
```
Authenticated user: office-network from 192.168.1.10
Trap from 192.168.1.10 authenticated as 'office-network' → client='office-network'
```

**Check log files:**
```bash
ls -la data/logs/file/
```

**Output**:
```
drwxr-xr-x office-network/
drwxr-xr-x warehouse/
drwxr-xr-x unauthenticated/
```

**Using Discovery Mode to Find Unauthenticated Devices:**
```bash
jq 'select(.snmp_username == null)' data/logs/snmp-discovery/snmpFULL.*.jsonl
```

### For Complete Details

See **SNMP_CLIENT_MAPPING.md** for:
- Detailed username-based client assignment
- Complete snmp-auth-manager.sh reference
- Access control model
- Troubleshooting authentication issues
- Best practices

## OID Mapping Configuration

### Mapping Structure

```json
{
  "trap_oid": "1.3.6.1.6.3.1.1.5.3",
  "trap_name": "linkDown",
  "level": "warning",
  "category": "network",
  "title_template": "Link Down: {IF-MIB::ifDescr}",
  "message_template": "Interface {IF-MIB::ifDescr} (index {IF-MIB::ifIndex}) is down on {SNMPv2-MIB::sysName}"
}
```

**Field Descriptions**:
- `trap_oid`: Numeric trap OID (required, from varbind #2)
- `trap_name`: Human-readable name (optional, for documentation)
- `level`: Alert level - `critical`, `warning`, `notice`, `info`, `debug`
- `category`: Alert category - `security`, `system`, `weather`, `automation`, `personal`, `network`, `application`, `news`, `business`, `hardware`, `environmental`
- `title_template`: Short title with `{variable}` placeholders
- `message_template`: Detailed message with `{variable}` placeholders

### Template Variables

Templates support placeholder substitution:

**Built-in Variables**:
- `{hostname}` - Source IP address
- `{trap_oid}` - Numeric trap OID
- `{trap_oid_symbolic}` - Symbolic trap name (if MIB loaded)

**Varbind Variables**:
- Use symbolic OID name: `{IF-MIB::ifDescr}`
- Use numeric OID (replace dots/colons): `{1_3_6_1_2_1_2_2_1_2}`

**Example**:
```
Template: "Interface {IF-MIB::ifDescr} down on {hostname}"
Rendered: "Interface eth0 down on 192.168.1.100"
```

## Threshold Rules

Monitor numeric varbinds and trigger alerts when thresholds are exceeded.

### Rule Structure

```json
{
  "varbind_oid": "1.3.6.1.4.1.2021.10.1.3.1",
  "varbind_name": "UCD-SNMP-MIB::laLoad.1",
  "metric_name": "load_average_1min",
  "thresholds": {
    "critical": {"operator": ">", "value": 4.0},
    "warning": {"operator": ">", "value": 2.0}
  },
  "category": "system",
  "title_template": "High Load Average",
  "message_template": "1-min load average is {value} on {hostname}"
}
```

**Supported Operators**:
- `>` - Greater than
- `<` - Less than
- `>=` - Greater than or equal
- `<=` - Less than or equal
- `==` - Equal to
- `!=` - Not equal to

**Threshold Evaluation**:
- Checks `critical` first, then `warning`
- Uses first matching threshold
- Skips non-numeric varbinds automatically

## Common Device Examples

### Network Switch/Router (Standard Traps)

**linkDown Alert**:
```json
{
  "trap_oid": "1.3.6.1.6.3.1.1.5.3",
  "level": "critical",
  "category": "network",
  "title_template": "Network Link Down",
  "message_template": "Interface {IF-MIB::ifDescr} (index {IF-MIB::ifIndex}) on {hostname} is down. Type: {IF-MIB::ifType}"
}
```

**linkUp Alert**:
```json
{
  "trap_oid": "1.3.6.1.6.3.1.1.5.4",
  "level": "notice",
  "category": "network",
  "title_template": "Network Link Up",
  "message_template": "Interface {IF-MIB::ifDescr} on {hostname} is now up"
}
```

**Device Configuration** (Cisco IOS example):
```
snmp-server group v3group v3 priv
snmp-server user monitor v3group v3 auth sha AUTH-PASSWORD-HERE priv aes 128 PRIV-PASSWORD-HERE
snmp-server host alert.d-t.pw version 3 priv monitor
snmp-server enable traps snmp linkdown linkup
```

### Linux Server (Net-SNMP)

**High CPU Alert**:
```json
{
  "varbind_oid": "1.3.6.1.4.1.2021.11.9.0",
  "metric_name": "cpu_idle",
  "thresholds": {
    "critical": {"operator": "<", "value": 5},
    "warning": {"operator": "<", "value": 15}
  },
  "category": "system",
  "title_template": "Critical CPU Usage",
  "message_template": "CPU idle at {value}% on {hostname}"
}
```

**High Memory Alert**:
```json
{
  "varbind_oid": "1.3.6.1.4.1.2021.4.6.0",
  "metric_name": "mem_available_kb",
  "thresholds": {
    "critical": {"operator": "<", "value": 524288},
    "warning": {"operator": "<", "value": 1048576}
  },
  "category": "system",
  "title_template": "Low Memory",
  "message_template": "Available memory: {value} KB on {hostname}"
}
```

**Server Configuration** (`/etc/snmp/snmpd.conf`):
```bash
createUser monitor SHA AUTH-PASSWORD-HERE AES PRIV-PASSWORD-HERE
rouser monitor priv

trapsink alert.d-t.pw:162 monitor

# CPU monitoring
load 2.0 2.5 3.0
```

Restart snmpd:
```bash
sudo systemctl restart snmpd
```

### Network Printer

**Common Printer OIDs**:
- **1.3.6.1.2.1.43.11.1.1.9.1.1** - Toner level (percentage)
- **1.3.6.1.2.1.43.11.1.1.9.1.2** - Black toner level
- **1.3.6.1.2.1.43.8.2.1.9.1.1** - Paper tray status

**Low Toner Alert**:
```json
{
  "varbind_oid": "1.3.6.1.2.1.43.11.1.1.9.1.1",
  "metric_name": "toner_level",
  "thresholds": {
    "warning": {"operator": "<", "value": 20},
    "notice": {"operator": "<", "value": 40}
  },
  "category": "system",
  "title_template": "Low Printer Toner",
  "message_template": "Toner at {value}% on printer {hostname}"
}
```

**Printer Configuration** (varies by manufacturer - check web interface):
- Enable SNMP traps
- Set trap destination: `alert.d-t.pw`
- Configure SNMPv3 user credentials
- Enable alerts for: toner low, paper out, offline

### UPS (APC, CyberPower)

**On Battery Alert**:
```json
{
  "trap_oid": "1.3.6.1.4.1.318.0.5",
  "level": "critical",
  "category": "system",
  "title_template": "UPS On Battery Power",
  "message_template": "UPS {hostname} switched to battery. Estimated runtime: {value} minutes"
}
```

**Low Battery Alert**:
```json
{
  "varbind_oid": "1.3.6.1.2.1.33.1.2.4.0",
  "metric_name": "ups_battery_percent",
  "thresholds": {
    "critical": {"operator": "<", "value": 20},
    "warning": {"operator": "<", "value": 50}
  },
  "category": "system",
  "title_template": "UPS Low Battery",
  "message_template": "UPS battery at {value}% on {hostname}"
}
```

**UPS Configuration** (APC example - via web interface):
- Network > SNMP
- Enable SNMPv3
- Add user: monitor with SHA/AES
- Set trap receiver: alert.d-t.pw:162
- Enable traps: on battery, low battery, power restored

## Device Configuration Examples

### Cisco Switch/Router

```
configure terminal

! Create SNMPv3 user
snmp-server group v3group v3 priv
snmp-server user monitor v3group v3 auth sha AUTH-PASSWORD-HERE priv aes 128 PRIV-PASSWORD-HERE

! Set trap destination
snmp-server host alert.d-t.pw version 3 priv monitor udp-port 162

! Enable specific traps
snmp-server enable traps snmp authentication linkdown linkup coldstart
snmp-server enable traps config
snmp-server enable traps entity
snmp-server enable traps cpu threshold

exit
write memory
```

### Juniper Router

```
set snmp v3 usm local-engine user monitor authentication-sha authentication-password AUTH-PASSWORD-HERE
set snmp v3 usm local-engine user monitor privacy-aes128 privacy-password PRIV-PASSWORD-HERE
set snmp v3 target-address alert target-address-port 162
set snmp v3 target-address alert address alert.d-t.pw
set snmp v3 target-parameters v3-params user monitor security-model usm security-level privacy
set snmp v3 notify-filter all-traps oid 1.3.6 include

commit
```

### HP/Aruba Switch

```
snmpv3 user monitor auth sha AUTH-PASSWORD-HERE priv aes PRIV-PASSWORD-HERE
snmp-server host alert.d-t.pw version 3 auth monitor udp-port 162
snmp-server enable traps
```

### Dell iDRAC Server

Via web interface:
1. Navigate to: iDRAC Settings > Network > SNMP and SNMP Traps
2. Enable SNMP Agent
3. Add SNMPv3 User:
   - Username: monitor
   - Authentication: SHA with passphrase
   - Privacy: AES with passphrase
4. Add Trap Destination:
   - Destination: alert.d-t.pw
   - Port: 162
   - SNMPv3: Enabled
   - Username: monitor
5. Enable traps: hardware, storage, power, temperature

## MIB Management

### Loading Vendor MIBs

1. **Download MIB files** from vendor website
2. **Place in MIB directory**:
```bash
mkdir -p data/mibs/vendor
cp VENDOR-MIB.mib data/mibs/vendor/
```

3. **Configure in docker.json**:
```json
{
  "snmp": {
    "mib_translation": {
      "enabled": true,
      "mib_paths": [
        "/usr/share/snmp/mibs",
        "/data/mibs/vendor"
      ],
      "load_modules": [
        "SNMPv2-MIB",
        "IF-MIB",
        "HOST-RESOURCES-MIB",
        "UCD-SNMP-MIB",
        "VENDOR-MIB"
      ]
    }
  }
}
```

4. **Restart gateway**:
```bash
docker-compose restart snmp-gateway
```

### Finding Vendor MIBs

**Major Vendors**:
- **Cisco**: https://www.cisco.com/c/en/us/support/docs/ip/simple-network-management-protocol-snmp/13013-mibs.html
- **HP Enterprise**: https://www.hpe.com/info/mibs
- **Juniper**: https://www.juniper.net/documentation/en_US/junos/information-products/topic-collections/mibs/
- **Dell**: https://www.dell.com/support (search for "MIB")
- **APC**: https://www.apc.com/us/en/download/

**MIB Repositories**:
- https://mibs.observium.org/
- http://www.circitor.fr/Mibs/Html/

## Testing SNMP Traps

### Using snmptrap Command

**Install net-snmp tools**:
```bash
# Ubuntu/Debian
sudo apt-get install snmp snmp-mibs-downloader

# RHEL/CentOS
sudo yum install net-snmp-utils
```

**Send test trap**:
```bash
snmptrap -v 3 -l authPriv \
  -u monitor \
  -a SHA -A AUTH-PASSWORD-HERE \
  -x AES -X PRIV-PASSWORD-HERE \
  alert.d-t.pw:162 \
  '' \
  1.3.6.1.6.3.1.1.5.3 \
  ifIndex i 2 \
  ifDescr s "eth0"
```

**Parameters**:
- `-v 3`: SNMPv3
- `-l authPriv`: Security level (authentication + privacy)
- `-u monitor`: Username
- `-a SHA -A AUTH-PASSWORD-HERE`: Auth protocol and key
- `-x AES -X PRIV-PASSWORD-HERE`: Privacy protocol and key
- `alert.d-t.pw:162`: Destination host and port
- `''`: Uptime (empty = current time)
- `1.3.6.1.6.3.1.1.5.3`: Trap OID (linkDown)
- `ifIndex i 2`: Varbind (integer value)
- `ifDescr s "eth0"`: Varbind (string value)

### Verify Reception

**Check gateway logs**:
```bash
docker logs -f alert-snmp-gateway | grep "Trap received"
```

**Check Alert Manager logs**:
```bash
tail -f data/logs/file/*.log
```

**View in web interface**:
```
http://alert.d-t.pw:8080/logs
```

## Troubleshooting

### Gateway Not Receiving Traps

**1. Check gateway is running**:
```bash
docker ps | grep snmp-gateway
```

**2. Verify port binding**:
```bash
docker exec alert-snmp-gateway netstat -uln | grep 162
```

Should show:
```
udp  0  0.0.0.0:162  0.0.0.0:*
```

**3. Test network connectivity**:
```bash
# From device, test UDP reachability
nc -u alert.d-t.pw 162
```

**4. Check firewall rules**:
```bash
# On Alert Manager host
sudo iptables -L -n | grep 162
```

**5. Review gateway logs**:
```bash
docker logs alert-snmp-gateway
```

### Authentication Failures

**Symptoms**: Logs show "Authentication failed" or no traps received

**Solutions**:
1. **Verify credentials match exactly** (case-sensitive)
2. **Check passphrase length** (minimum 8 characters)
3. **Confirm auth/priv protocols** (SHA/AES vs MD5/DES)
4. **Test with snmptrap** from manager host to isolate device config issues

### Missing Symbolic OID Names

**Symptoms**: Logs show numeric OIDs instead of names like `IF-MIB::ifDescr`

**Solutions**:
1. **Enable MIB translation**:
```json
{
  "snmp": {
    "mib_translation": {
      "enabled": true
    }
  }
}
```

2. **Load required MIB modules**:
```json
{
  "mib_translation": {
    "load_modules": ["IF-MIB", "SNMPv2-MIB"]
  }
}
```

3. **Restart gateway after MIB changes**

### Threshold Rules Not Triggering

**Check**:
1. **Varbind OID matches exactly**
2. **Value is numeric** (check discovery logs)
3. **Operator and threshold value are correct**
4. **Threshold rule comes before OID mapping** (checked first)

## Security Recommendations

### Credential Management

1. **Strong passphrases**: 16+ characters, mixed case, numbers, symbols
2. **Unique per environment**: Different keys for prod/dev/test
3. **Regular rotation**: Change every 90-180 days
4. **Secure storage**: Use secrets management (Vault, etc.)

### Network Security

1. **Firewall rules**: Restrict source IPs to known device subnets
```bash
# Example iptables rule
iptables -A INPUT -p udp --dport 162 -s 192.168.1.0/24 -j ACCEPT
iptables -A INPUT -p udp --dport 162 -j DROP
```

2. **Management VLAN**: Isolate SNMP traffic on dedicated network
3. **Rate limiting**: Prevent trap floods

### SNMPv3 Best Practices

1. **Require authPriv**: Never use noAuthNoPriv in production
2. **Use SHA-256 or stronger**: Avoid MD5
3. **Use AES-128 or stronger**: Avoid DES
4. **Disable SNMPv1/v2c**: On all devices if possible
5. **Monitor authentication failures**: Alert on repeated failures

## Performance Tuning

### High-Volume Environments

**Adjust buffer sizes**:
```json
{
  "snmp": {
    "socket_buffer_size": 2097152,
    "max_trap_queue": 10000
  }
}
```

**Use multiple gateways**:
- Load balance across multiple gateway instances
- Different ports or IP addresses
- Partition devices by type or location

### Discovery Mode Impact

**Storage**: 1000 traps/day × 30 days × 2KB/trap = ~60 MB

**Disable when not needed** to reduce disk I/O and log volume.

## Additional Resources

- **SNMP_CLIENT_MAPPING.md** - Complete guide to client mapping and access control
- **SNMPV3_SPECIFICATION.md** - Technical specification for SNMPv3 protocol
- **CLIENTNODE.md** - Alert payload structure and user types
- **HTTP_GATEWAY.md** - HTTP API reference and log viewer
- **OID Reference**: https://oidref.com/
- **MIB Repository**: https://mibs.observium.org/
- **Net-SNMP Documentation**: http://www.net-snmp.org/docs/

---

*For technical details on SNMP v3 protocol, see SNMPV3_SPECIFICATION.md. For Alert Manager payload structure, see CLIENTNODE.md.*
