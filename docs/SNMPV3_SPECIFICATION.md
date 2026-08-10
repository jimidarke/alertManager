# SNMP v3 Technical Specification

**Version 1.0.0**

Technical reference for SNMP v3 protocol architecture, security model, and data structures used in the Alert Manager SNMP Gateway.

## Official Reference

**Primary Source**: [SNMPv3 White Paper](https://www.snmp.com/snmpv3/v3white.shtml) - Comprehensive overview of SNMPv3 architecture and security features.

## RFC Standards

SNMP v3 is defined across multiple RFCs:

| RFC | Title | Purpose |
|-----|-------|---------|
| **RFC 3410** | Introduction and Applicability Statements for SNMP v3 | Framework overview |
| **RFC 3411** | An Architecture for Describing SNMP Management Frameworks | Core architecture |
| **RFC 3412** | Message Processing and Dispatching for SNMP | Message handling |
| **RFC 3413** | SNMP Applications | Trap/inform, get/set operations |
| **RFC 3414** | User-based Security Model (USM) for SNMPv3 | Authentication and encryption |
| **RFC 3415** | View-based Access Control Model (VACM) for SNMP | Access control |
| **RFC 3416** | Protocol Operations for SNMP | PDU formats |
| **RFC 3417** | Transport Mappings for SNMP | UDP/TCP transport |
| **RFC 3418** | Management Information Base (MIB) for SNMP | SNMP MIB objects |

## SNMP Protocol Evolution

### Version Comparison

| Feature | SNMPv1 | SNMPv2c | SNMPv3 |
|---------|--------|---------|--------|
| **Security** | Community string (plaintext) | Community string (plaintext) | USM with authentication & encryption |
| **Authentication** | None | None | MD5, SHA, SHA-224/256/384/512 |
| **Encryption** | None | None | DES, AES-128/192/256 |
| **Message Integrity** | None | None | Yes (HMAC) |
| **Inform Support** | No | Yes | Yes (with security) |
| **Error Reporting** | Limited | Enhanced | Enhanced |
| **Bulk Operations** | No | Yes (GetBulk) | Yes (GetBulk) |
| **Standard Port** | 161/162 (UDP) | 161/162 (UDP) | 161/162 (UDP) |

### Why SNMPv3?

**Security Improvements:**
- **Authentication**: Verify message sender identity
- **Privacy**: Encrypt message contents
- **Message Integrity**: Prevent tampering
- **Timeliness**: Prevent replay attacks

**Backwards Compatibility**: SNMPv3 can coexist with v1/v2c on the same devices.

## SNMPv3 Architecture

### Message Processing Model

```
┌─────────────────────────────────────────────────────────┐
│                   SNMP Application                       │
│  (Command Generator, Notification Receiver, Proxy)      │
└───────────────────────────┬─────────────────────────────┘
                            │
┌───────────────────────────▼─────────────────────────────┐
│              Message Processing Subsystem                │
│  ┌─────────────┐  ┌──────────────┐  ┌───────────────┐  │
│  │ SNMPv1 MP   │  │ SNMPv2c MP   │  │ SNMPv3 MP     │  │
│  └─────────────┘  └──────────────┘  └───────────────┘  │
└───────────────────────────┬─────────────────────────────┘
                            │
┌───────────────────────────▼─────────────────────────────┐
│              Security Subsystem                          │
│  ┌──────────────────────────────────────────────────┐   │
│  │  User-based Security Model (USM)                 │   │
│  │  - Authentication (HMAC-MD5/SHA/SHA2)            │   │
│  │  - Privacy/Encryption (DES/AES)                  │   │
│  │  - Timeliness (prevents replay attacks)          │   │
│  └──────────────────────────────────────────────────┘   │
└───────────────────────────┬─────────────────────────────┘
                            │
┌───────────────────────────▼─────────────────────────────┐
│              Access Control Subsystem                    │
│  ┌──────────────────────────────────────────────────┐   │
│  │  View-based Access Control Model (VACM)          │   │
│  │  - Define who can access what objects            │   │
│  │  - Read/write/notify permissions                 │   │
│  └──────────────────────────────────────────────────┘   │
└───────────────────────────┬─────────────────────────────┘
                            │
┌───────────────────────────▼─────────────────────────────┐
│              Dispatcher                                  │
│  - Route messages between subsystems                     │
│  - Handle transport addressing                           │
└───────────────────────────┬─────────────────────────────┘
                            │
┌───────────────────────────▼─────────────────────────────┐
│              Transport Layer (UDP/TCP)                   │
└─────────────────────────────────────────────────────────┘
```

### SNMPv3 Message Structure

```
┌──────────────────────────────────────────────────────────┐
│                    SNMPv3 Message                         │
├──────────────────────────────────────────────────────────┤
│  Message Header                                           │
│  ├── msgVersion (3)                                       │
│  ├── msgID (unique request identifier)                    │
│  ├── msgMaxSize (max message size supported)              │
│  ├── msgFlags (auth/priv/reportable)                      │
│  └── msgSecurityModel (USM = 3)                           │
├──────────────────────────────────────────────────────────┤
│  Security Parameters (USM)                                │
│  ├── msgAuthoritativeEngineID                             │
│  ├── msgAuthoritativeEngineBoots                          │
│  ├── msgAuthoritativeEngineTime                           │
│  ├── msgUserName                                          │
│  ├── msgAuthenticationParameters (HMAC digest)            │
│  └── msgPrivacyParameters (IV for encryption)             │
├──────────────────────────────────────────────────────────┤
│  Scoped PDU (encrypted if priv enabled)                   │
│  ├── contextEngineID                                      │
│  ├── contextName                                          │
│  └── PDU (Protocol Data Unit)                             │
│      ├── PDU Type (Trap = 7, Inform = 6, Get = 0, etc.)  │
│      ├── Request ID                                       │
│      ├── Error Status                                     │
│      ├── Error Index                                      │
│      └── Variable Bindings (OID-value pairs)              │
└──────────────────────────────────────────────────────────┘
```

## User-based Security Model (USM)

### Security Levels

SNMPv3 supports three security levels:

| Level | Name | Authentication | Privacy | Use Case |
|-------|------|----------------|---------|----------|
| **1** | noAuthNoPriv | No | No | **Maximum compatibility** - similar to SNMPv2c community strings |
| **2** | authNoPriv | Yes | No | Authentication without encryption - good balance |
| **3** | authPriv | Yes | Yes | Full security - authentication + encryption |

**Alert Manager SNMP Gateway**: Supports **all three security levels** for maximum vendor compatibility.

**Recommended**:
- **noAuthNoPriv** - Default, works with most vendor implementations
- **authNoPriv** - Use if you need authentication but device doesn't support encryption
- **authPriv** - Use for high-security environments where both auth and encryption are required

**Note**: The SNMP content being monitored is typically read-only and non-critical, so reduced security levels are often acceptable for usability.

### Authentication Protocols

Authentication ensures message sender identity and message integrity using HMAC.

| Protocol | Hash Function | Digest Length | Security Level | RFC |
|----------|---------------|---------------|----------------|-----|
| **HMAC-MD5-96** | MD5 | 96 bits | Deprecated | RFC 3414 |
| **HMAC-SHA-96** | SHA-1 | 96 bits | Acceptable | RFC 3414 |
| **HMAC-SHA-224** | SHA-224 | 128 bits | Good | RFC 7860 |
| **HMAC-SHA-256** | SHA-256 | 192 bits | Good | RFC 7860 |
| **HMAC-SHA-384** | SHA-384 | 256 bits | Strong | RFC 7860 |
| **HMAC-SHA-512** | SHA-512 | 384 bits | Strong | RFC 7860 |

**Recommendation**: Use SHA-256 or stronger for production deployments.

### Privacy (Encryption) Protocols

Privacy provides message confidentiality by encrypting the scoped PDU.

| Protocol | Key Size | Block Size | Security Level | RFC |
|----------|----------|------------|----------------|-----|
| **DES** | 56-bit | 64-bit | Deprecated | RFC 3414 |
| **3DES** | 168-bit | 64-bit | Acceptable | RFC 3826 |
| **AES-128** | 128-bit | 128-bit | Good | RFC 3826 |
| **AES-192** | 192-bit | 128-bit | Strong | RFC 3826 |
| **AES-256** | 256-bit | 128-bit | Strong | RFC 3826 |

**Recommendation**: Use AES-128 or stronger for production deployments.

### USM User Configuration

Each SNMPv3 user requires:
- **Username**: Unique identifier (no spaces)
- **Authentication Protocol**: SHA/SHA-256/etc.
- **Authentication Key**: Passphrase (minimum 8 characters)
- **Privacy Protocol**: AES/AES-256/etc.
- **Privacy Key**: Passphrase (minimum 8 characters)

**Key Derivation**: Passphrases are hashed with the engine ID to derive keys, so the same passphrase produces different keys on different devices.

## Object Identifiers (OIDs)

### OID Hierarchy

OIDs form a hierarchical tree structure managed by different organizations.

```
                         root
                           │
        ┌──────────────────┼──────────────────┐
        │                  │                  │
      ccitt(0)           iso(1)            joint(2)
                           │
        ┌──────────────────┼──────────────────┐
        │                  │                  │
    standard(0)          org(3)          member-body(2)
                           │
                         dod(6)
                           │
                       internet(1)
                           │
        ┌──────────────────┼──────────────────┬──────────────┐
        │                  │                  │              │
   directory(1)         mgmt(2)          experimental(3)  private(4)
                           │                                 │
                        mib-2(1)                       enterprises(1)
                           │                                 │
     ┌─────────────────────┼──────────────┬─────────       ├─ 9 (Cisco)
     │                     │              │                 ├─ 2636 (Juniper)
  system(1)          interfaces(2)    ip(4)                ├─ 311 (Microsoft)
                                                            ├─ 2021 (Net-SNMP)
                                                            └─ ... (thousands more)
```

### Standard OID Branches

| OID Prefix | Branch Name | Description |
|------------|-------------|-------------|
| **1.3.6.1.1** | directory | X.500 directory (rarely used) |
| **1.3.6.1.2.1** | mib-2 | Standard MIB objects (RFC 1213) |
| **1.3.6.1.3** | experimental | Experimental protocols |
| **1.3.6.1.4.1** | enterprises | Vendor-specific (private enterprise numbers) |
| **1.3.6.1.6.3** | snmpModules | SNMP framework MIBs |

### MIB-II Standard Objects (1.3.6.1.2.1.*)

Core managed objects defined in RFC 1213:

| OID | Group | Description |
|-----|-------|-------------|
| **1.3.6.1.2.1.1** | system | System description, uptime, contact, name, location |
| **1.3.6.1.2.1.2** | interfaces | Network interface table (ifTable) |
| **1.3.6.1.2.1.3** | at | Address translation (deprecated) |
| **1.3.6.1.2.1.4** | ip | IP statistics and routing table |
| **1.3.6.1.2.1.5** | icmp | ICMP statistics |
| **1.3.6.1.2.1.6** | tcp | TCP connection table and statistics |
| **1.3.6.1.2.1.7** | udp | UDP statistics |
| **1.3.6.1.2.1.11** | snmp | SNMP statistics |
| **1.3.6.1.2.1.25** | host | Host resources (CPU, memory, storage, processes) |
| **1.3.6.1.2.1.31** | ifMIB | Extended interface information |
| **1.3.6.1.2.1.33** | ups | UPS monitoring |

### Enterprise OIDs (1.3.6.1.4.1.*)

Vendors are assigned Private Enterprise Numbers (PENs) by IANA:

| PEN | Vendor | Example Use Cases |
|-----|--------|-------------------|
| **9** | Cisco Systems | Router/switch alerts, environmental monitoring |
| **2636** | Juniper Networks | JunOS device alerts |
| **311** | Microsoft | Windows SNMP agent |
| **2021** | Net-SNMP | Unix/Linux system monitoring |
| **43** | 3Com | Network equipment |
| **2001** | Oki Electric | Printers |
| **367** | Hewlett-Packard | Servers, printers, storage |
| **6876** | VMware | ESXi host monitoring |

**Full PEN Registry**: https://www.iana.org/assignments/enterprise-numbers/

### OID Representation

**Numeric Form** (always unambiguous):
```
1.3.6.1.2.1.2.2.1.8.2
```

**Symbolic Form** (requires MIB):
```
IF-MIB::ifOperStatus.2
```

**Mixed Form**:
```
iso.org.dod.internet.mgmt.mib-2.interfaces.ifTable.ifEntry.ifOperStatus.2
```

### OID Components

Using example: `1.3.6.1.2.1.2.2.1.8.2`

| Position | Value | Meaning |
|----------|-------|---------|
| 1 | 1 | ISO |
| 2 | 3 | org (organizations) |
| 3 | 6 | dod (Department of Defense) |
| 4 | 1 | internet |
| 5 | 2 | mgmt (management) |
| 6 | 1 | mib-2 |
| 7 | 2 | interfaces |
| 8 | 2 | ifTable |
| 9 | 1 | ifEntry |
| 10 | 8 | ifOperStatus (column) |
| 11 | 2 | Interface index (row) |

**Result**: Operational status of interface #2

## SNMP Traps and Informs

### Trap vs Inform

| Feature | Trap (SNMPv2-Trap) | Inform |
|---------|---------------------|--------|
| **Reliability** | Unconfirmed (fire-and-forget) | Confirmed (requires ACK) |
| **Overhead** | Low | Higher (waits for response) |
| **PDU Type** | 7 (SNMPv2-Trap-PDU) | 6 (InformRequest-PDU) |
| **Use Case** | High-volume alerts | Critical alerts requiring delivery confirmation |

**Alert Manager**: Supports both, but traps are more common for device monitoring.

### Standard Trap Types

Defined in SNMPv2-MIB (1.3.6.1.6.3.1.1.5.*):

| Trap OID | Name | Description | Typical Use |
|----------|------|-------------|-------------|
| **1.3.6.1.6.3.1.1.5.1** | coldStart | Device restarted (power cycle) | Server/network device boot |
| **1.3.6.1.6.3.1.1.5.2** | warmStart | SNMP agent restarted (no power cycle) | Service restart |
| **1.3.6.1.6.3.1.1.5.3** | linkDown | Network interface went down | Network outage detection |
| **1.3.6.1.6.3.1.1.5.4** | linkUp | Network interface came up | Recovery notification |
| **1.3.6.1.6.3.1.1.5.5** | authenticationFailure | SNMP authentication failed | Security monitoring |

### Trap PDU Structure

Per RFC 3416 Section 4.2.6, trap varbinds follow this structure:

```
┌────────────────────────────────────────────────────────┐
│  SNMPv2-Trap PDU                                        │
├────────────────────────────────────────────────────────┤
│  VarBind 1: sysUpTime.0 (1.3.6.1.2.1.1.3.0)             │
│    └─ Value: TimeTicks (e.g., 123456789)               │
├────────────────────────────────────────────────────────┤
│  VarBind 2: snmpTrapOID.0 (1.3.6.1.6.3.1.1.4.1.0)       │
│    └─ Value: ObjectIdentifier (trap type OID)          │
├────────────────────────────────────────────────────────┤
│  VarBind 3+: Trap-specific data                         │
│    └─ Application-defined OID-value pairs              │
└────────────────────────────────────────────────────────┘
```

**First Two Varbinds Are Mandatory**:
1. **sysUpTime.0**: System uptime in hundredths of seconds (TimeTicks)
2. **snmpTrapOID.0**: OID identifying the trap type

**Remaining Varbinds**: Trap-specific information (interface name, error codes, sensor values, etc.)

### Example Trap: linkDown

```json
{
  "trap_oid": "1.3.6.1.6.3.1.1.5.3",
  "source_ip": "192.168.1.1",
  "uptime": 987654321,
  "varbinds": [
    {
      "oid": "1.3.6.1.2.1.2.2.1.1.2",
      "symbolic": "IF-MIB::ifIndex.2",
      "value": "2",
      "type": "Integer"
    },
    {
      "oid": "1.3.6.1.2.1.2.2.1.2.2",
      "symbolic": "IF-MIB::ifDescr.2",
      "value": "eth0",
      "type": "OctetString"
    },
    {
      "oid": "1.3.6.1.2.1.2.2.1.3.2",
      "symbolic": "IF-MIB::ifType.2",
      "value": "6",
      "type": "Integer"
    }
  ]
}
```

**Translation**: Interface #2 (eth0, Ethernet) went down on device at 192.168.1.1.

## Management Information Base (MIB)

### What is a MIB?

A MIB is a formal definition of managed objects using ASN.1 (Abstract Syntax Notation One) syntax. It defines:
- Object names and OIDs
- Data types
- Access permissions (read-only, read-write)
- Object relationships (tables, scalars)
- Descriptions and semantics

**MIBs are like schemas for SNMP data** - they tell you what each OID means and how to interpret its value.

### MIB File Structure

```asn1
-- Example MIB module structure
MY-EXAMPLE-MIB DEFINITIONS ::= BEGIN

IMPORTS
    MODULE-IDENTITY, OBJECT-TYPE, Integer32
        FROM SNMPv2-SMI
    TEXTUAL-CONVENTION, DisplayString
        FROM SNMPv2-TC;

myExampleMIB MODULE-IDENTITY
    LAST-UPDATED "202501150000Z"
    ORGANIZATION "My Organization"
    CONTACT-INFO "admin@example.com"
    DESCRIPTION "Example MIB for documentation"
    ::= { enterprises 99999 }

-- Define OID tree
myExampleObjects OBJECT IDENTIFIER ::= { myExampleMIB 1 }

-- Scalar object example
exampleTemperature OBJECT-TYPE
    SYNTAX      Integer32 (-100..200)
    UNITS       "degrees Celsius"
    MAX-ACCESS  read-only
    STATUS      current
    DESCRIPTION "Current temperature reading"
    ::= { myExampleObjects 1 }

-- Table example
exampleInterfaceTable OBJECT-TYPE
    SYNTAX      SEQUENCE OF ExampleInterfaceEntry
    MAX-ACCESS  not-accessible
    STATUS      current
    DESCRIPTION "Table of network interfaces"
    ::= { myExampleObjects 2 }

exampleInterfaceEntry OBJECT-TYPE
    SYNTAX      ExampleInterfaceEntry
    MAX-ACCESS  not-accessible
    STATUS      current
    DESCRIPTION "An entry in the interface table"
    INDEX       { exampleInterfaceIndex }
    ::= { exampleInterfaceTable 1 }

ExampleInterfaceEntry ::= SEQUENCE {
    exampleInterfaceIndex       Integer32,
    exampleInterfaceName        DisplayString,
    exampleInterfaceSpeed       Integer32
}

exampleInterfaceIndex OBJECT-TYPE
    SYNTAX      Integer32 (1..2147483647)
    MAX-ACCESS  not-accessible
    STATUS      current
    DESCRIPTION "Unique interface index"
    ::= { exampleInterfaceEntry 1 }

-- Notification (trap) definition
exampleLinkDownTrap NOTIFICATION-TYPE
    OBJECTS     { exampleInterfaceIndex, exampleInterfaceName }
    STATUS      current
    DESCRIPTION "Sent when interface goes down"
    ::= { myExampleMIB 0 1 }

END
```

### Common MIB Modules

| MIB Module | OID Prefix | Purpose |
|------------|------------|---------|
| **SNMPv2-MIB** | 1.3.6.1.6.3.1 | SNMPv2 framework objects |
| **IF-MIB** | 1.3.6.1.2.1.31 | Network interface monitoring |
| **HOST-RESOURCES-MIB** | 1.3.6.1.2.1.25 | CPU, memory, storage, processes |
| **UCD-SNMP-MIB** | 1.3.6.1.4.1.2021 | Unix/Linux system monitoring |
| **NET-SNMP-EXTEND-MIB** | 1.3.6.1.4.1.8072.1.3 | Custom script execution |
| **UPS-MIB** | 1.3.6.1.2.1.33 | Uninterruptible Power Supply |
| **PRINTER-MIB** | 1.3.6.1.2.1.43 | Printer status and supplies |

### MIB Repositories

**Standard MIBs**:
- Pre-installed with net-snmp: `/usr/share/snmp/mibs/`
- RFCs containing MIB definitions

**Vendor MIBs**:
- Download from device manufacturer's website
- Usually in support/downloads section
- File extension: `.mib`, `.my`, or `.txt`

**Online MIB Browsers**:
- https://oidref.com/
- https://mibs.observium.org/
- https://mibbrowser.online/
- http://www.circitor.fr/Mibs/Html/

## Data Types

### SMI Data Types

SNMP uses Structure of Management Information (SMI) defined types:

| Type | Description | Example |
|------|-------------|---------|
| **INTEGER** | 32-bit signed integer | -2147483648 to 2147483647 |
| **Integer32** | Same as INTEGER | CPU percentage: 85 |
| **Unsigned32** | 32-bit unsigned integer | 0 to 4294967295 |
| **Counter32** | Monotonically increasing (wraps at 2^32) | Bytes transmitted |
| **Counter64** | 64-bit counter (for high-speed interfaces) | Bytes transmitted |
| **Gauge32** | Non-negative integer (can increase/decrease) | Current bandwidth usage |
| **TimeTicks** | Time in hundredths of seconds since epoch | System uptime: 123456789 |
| **OCTET STRING** | Byte sequence | "eth0", MAC address, arbitrary data |
| **DisplayString** | Printable ASCII string | "Router-01" |
| **OBJECT IDENTIFIER** | OID value | 1.3.6.1.2.1.1.1.0 |
| **IpAddress** | IPv4 address | 192.168.1.1 |
| **Opaque** | Arbitrary ASN.1 encoding | Float values (non-standard) |

## Security Best Practices

### For Device Configuration (Trap Senders)

1. **Use SNMPv3 Only**: Disable SNMPv1/v2c if possible
2. **Require authPriv**: Force authentication + encryption
3. **Strong Passphrases**: Minimum 16 characters, mixed case, numbers, symbols
4. **Use SHA-256+ and AES**: Avoid MD5 and DES
5. **Limit Access**: Firewall SNMP to specific IP addresses
6. **Unique Credentials**: Different usernames/passwords per device class
7. **Regular Rotation**: Change credentials periodically

### For Trap Receiver (Alert Manager)

1. **Isolated Network**: Place SNMP gateway on management VLAN
2. **Rate Limiting**: Prevent trap floods from misconfigured devices
3. **Validation**: Reject traps with invalid authentication
4. **Logging**: Log all authentication failures for security monitoring
5. **Discovery Mode**: Use temporarily to identify devices, then disable

## Port Numbers

| Port | Protocol | Direction | Purpose |
|------|----------|-----------|---------|
| **161/UDP** | SNMP | Client → Agent | Get/Set requests |
| **162/UDP** | SNMP | Agent → Manager | Traps/Informs |

**Alert Manager SNMP Gateway**: Listens on UDP port 162 for incoming traps.

## Troubleshooting

### Common Issues

**Authentication Failures**:
- Mismatched username, auth protocol, or auth key
- Check engine ID synchronization
- Verify clock skew (SNMPv3 uses timestamps)

**Privacy Failures**:
- Mismatched priv protocol or priv key
- Ensure both sides support same encryption algorithm

**No Traps Received**:
- Firewall blocking UDP port 162
- Incorrect trap destination IP/port on device
- SNMP agent not configured for traps
- Wrong community string (v1/v2c) or credentials (v3)

**Unknown OIDs**:
- Missing MIB files
- Vendor MIBs not loaded
- Use numeric OID as fallback

### Diagnostic Commands

```bash
# Test trap sending (from net-snmp tools)
snmptrap -v 3 -l authPriv -u monitor \
  -a SHA -A authkey123 -x AES -X privkey123 \
  alert.d-t.pw:162 '' 1.3.6.1.6.3.1.1.5.3

# Verify SNMP gateway is listening
netstat -uln | grep 162

# View SNMP gateway logs
tail -f /data/logs/ops/alert-snmp-gateway.*.log

# Analyze discovery logs
jq -r '.trap_oid' /data/logs/snmp-discovery/*.jsonl | sort | uniq -c
```

## Further Reading

- **Official SNMPv3 White Paper**: https://www.snmp.com/snmpv3/v3white.shtml
- **RFC 3410-3415**: SNMPv3 specification suite
- **Net-SNMP Project**: http://www.net-snmp.org/
- **IANA Enterprise Numbers**: https://www.iana.org/assignments/enterprise-numbers/
- **OID Reference Database**: https://oidref.com/
- **MIB Repository**: https://mibs.observium.org/

---

*This specification is part of the Alert Manager SNMP Gateway implementation. For integration guide and configuration examples, see SNMP_INTEGRATION.md.*
