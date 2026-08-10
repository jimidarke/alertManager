"""
SNMP v3 Gateway Service

Receives SNMP v3 traps and forwards them to the Alert Manager via MQTT.

Features:
- SNMPv3 trap receiver with USM authentication and encryption
- Discovery mode to capture and analyze all incoming traps
- Configuration-based OID mapping to alert levels/categories
- Threshold rules for numeric monitoring
- Template-based message generation
- Optional MIB translation for symbolic OID names
"""

import json
import logging
import os
import sys
import time
import socket
import re
import ipaddress
from datetime import datetime
from typing import Dict, Any, Optional, List, Tuple

import pytz
import paho.mqtt.client as mqtt
from pysnmp.entity import engine, config
from pysnmp.carrier.asyncio.dgram import udp
from pysnmp.entity.rfc3413 import ntfrcv
from pysnmp import debug as pysnmp_debug
from pysnmp.proto.api import v2c

from oid_translator import OIDTranslator


class SNMPGateway:
    """
    SNMP v3 trap receiver gateway.

    Listens for SNMP traps, maps them to alert payloads,
    and publishes to internal MQTT broker.
    """

    def __init__(self, config_file: str = "/data/config/docker.json"):
        """Initialize SNMP gateway."""
        self.config = self.load_config(config_file)
        self.mqtt_client = None
        self.oid_translator = None
        self.discovery_log_file = None
        self.snmp_users = {}  # Will be loaded from external file

        # Statistics
        self.stats = {
            'traps_received': 0,
            'traps_forwarded': 0,
            'traps_discovery_logged': 0,
            'parse_errors': 0,
            'mqtt_errors': 0,
            'mapping_matches': 0,
            'threshold_matches': 0,
            'start_time': int(time.time())
        }

        # Setup logging
        self.setup_logging()

        # Initialize components
        self.setup_oid_translator()
        self.setup_discovery_mode()
        self.load_snmp_users()

        # Log configuration
        snmp_config = self.config.get('snmp', {})
        self.logger.info(f"SNMP Gateway initialized")
        self.logger.info(f"Port: {snmp_config.get('port', 162)}")
        self.logger.info(f"SNMPv3 users loaded: {len(self.snmp_users)}")
        self.logger.info(f"Discovery mode: {snmp_config.get('discovery_mode', {}).get('enabled', False)}")
        self.logger.info(f"MIB translation: {snmp_config.get('mib_translation', {}).get('enabled', False)}")

    def load_config(self, config_file: str) -> Dict[str, Any]:
        """Load configuration from JSON file."""
        try:
            with open(config_file, 'r') as f:
                return json.load(f)
        except FileNotFoundError:
            print(f"Configuration file not found: {config_file}")
            sys.exit(1)
        except json.JSONDecodeError as e:
            print(f"Invalid JSON in configuration file: {e}")
            sys.exit(1)

    def setup_logging(self):
        """Configure logging with date-based rotation."""
        # Try SNMP-specific logging config first, fall back to top-level
        snmp_config = self.config.get('snmp', {})
        logging_config = snmp_config.get('logging', self.config.get('logging', {}))

        # Allow LOG_LEVEL environment variable to override config
        log_level_str = os.getenv('LOG_LEVEL', logging_config.get('level', 'INFO'))
        log_level = getattr(logging, log_level_str.upper())

        log_file = logging_config.get('file', '/data/logs/ops/alert-snmp-gateway.{date}.log')

        # Replace {date} with current UTC date
        utc = pytz.UTC
        current_date = datetime.now(utc).strftime('%Y-%m-%d')
        log_file = log_file.replace('{date}', current_date)

        # Create log directory if it doesn't exist
        log_dir = os.path.dirname(log_file)
        if log_dir and not os.path.exists(log_dir):
            os.makedirs(log_dir, exist_ok=True)

        # Configure logging
        logging.basicConfig(
            level=log_level,
            format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
            handlers=[
                logging.FileHandler(log_file),
                logging.StreamHandler()
            ]
        )

        self.logger = logging.getLogger(self.__class__.__name__)
        self.logger.info(f"Logging initialized: {log_file}")

    def setup_oid_translator(self):
        """Initialize OID translator if MIB translation is enabled."""
        snmp_config = self.config.get('snmp', {})
        mib_config = snmp_config.get('mib_translation', {})

        if not mib_config.get('enabled', False):
            self.logger.info("MIB translation disabled")
            return

        try:
            mib_paths = mib_config.get('mib_paths', [])
            load_modules = mib_config.get('load_modules', [])

            self.oid_translator = OIDTranslator(
                mib_paths=mib_paths,
                load_modules=load_modules,
                cache_enabled=True
            )
            self.logger.info("OID translator initialized")
        except Exception as e:
            self.logger.error(f"Failed to initialize OID translator: {e}")
            self.oid_translator = None

    def setup_discovery_mode(self):
        """Setup discovery mode logging if enabled."""
        snmp_config = self.config.get('snmp', {})
        discovery_config = snmp_config.get('discovery_mode', {})

        if not discovery_config.get('enabled', False):
            return

        # Setup discovery log file
        log_path = discovery_config.get('log_path', '/data/logs/snmp-discovery/discovery.{date}.jsonl')

        # Replace {date} with current UTC date
        utc = pytz.UTC
        current_date = datetime.now(utc).strftime('%Y-%m-%d')
        log_path = log_path.replace('{date}', current_date)

        # Create directory
        log_dir = os.path.dirname(log_path)
        if log_dir and not os.path.exists(log_dir):
            os.makedirs(log_dir, exist_ok=True)

        try:
            self.discovery_log_file = open(log_path, 'a')
            self.logger.info(f"Discovery mode enabled: {log_path}")
        except Exception as e:
            self.logger.error(f"Failed to open discovery log file: {e}")
            self.discovery_log_file = None

    def load_snmp_users(self):
        """Load SNMPv3 users from external JSON file."""
        snmp_config = self.config.get('snmp', {})
        users_file = snmp_config.get('users_file', '/data/config/snmp-gateway/users.json')

        if not os.path.exists(users_file):
            self.logger.warning(f"SNMPv3 users file not found: {users_file}")
            self.logger.warning("No users will be configured. Use snmp-auth-manager.sh to create users.")
            self.snmp_users = {}
            return

        try:
            with open(users_file, 'r') as f:
                users_data = json.load(f)
                self.snmp_users = users_data.get('users', {})
                self.logger.info(f"Loaded {len(self.snmp_users)} SNMPv3 users from {users_file}")

                # Log active users
                active_users = [u for u, d in self.snmp_users.items() if d.get('status') == 'active']
                self.logger.info(f"Active users: {', '.join(active_users) if active_users else 'none'}")

        except json.JSONDecodeError as e:
            self.logger.error(f"Invalid JSON in users file: {e}")
            self.snmp_users = {}
        except Exception as e:
            self.logger.error(f"Failed to load users file: {e}")
            self.snmp_users = {}

    def connect_mqtt(self):
        """Connect to internal MQTT broker."""
        mqtt_config = self.config.get('mqtt', {})

        # Generate unique client ID
        hostname = socket.gethostname()
        client_id = f"snmp-gateway-{hostname}-{int(time.time())}"

        # Initialize MQTT client with callback API v2
        self.mqtt_client = mqtt.Client(
            client_id=client_id,
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2
        )

        # Connect to internal broker (no TLS)
        broker = mqtt_config.get('broker', 'mqtt-broker')
        port = mqtt_config.get('port', 1883)

        try:
            self.mqtt_client.connect(broker, port, 60)
            self.mqtt_client.loop_start()
            self.logger.info(f"Connected to MQTT broker: {broker}:{port}")
        except Exception as e:
            self.logger.error(f"Failed to connect to MQTT broker: {e}")
            sys.exit(1)

    def forward_to_mqtt(self, alert_data: Dict[str, Any]) -> bool:
        """
        Publish alert to MQTT alerts/input topic.

        Args:
            alert_data: Alert payload dictionary

        Returns:
            True if published successfully, False otherwise
        """
        try:
            if not self.mqtt_client or not self.mqtt_client.is_connected():
                self.logger.warning("MQTT client not connected, attempting reconnection...")
                self.connect_mqtt()

            mqtt_config = self.config.get('mqtt', {})
            input_topic = mqtt_config.get('input_topic', 'alerts/input')

            # Publish to MQTT with QoS 1
            result = self.mqtt_client.publish(
                input_topic,
                json.dumps(alert_data),
                qos=1
            )

            if result.rc == mqtt.MQTT_ERR_SUCCESS:
                self.stats['traps_forwarded'] += 1
                return True
            else:
                self.logger.error(f"MQTT publish failed with code: {result.rc}")
                self.stats['mqtt_errors'] += 1
                return False
        except Exception as e:
            self.logger.error(f"Error forwarding to MQTT: {e}", exc_info=True)
            self.stats['mqtt_errors'] += 1
            return False

    def log_discovery_trap(self, trap_oid: str, varbinds: List[Dict], source_ip: str, snmp_username: Optional[str] = None):
        """
        Log trap to discovery file for analysis.

        Includes authenticated username for troubleshooting.

        Args:
            trap_oid: Trap OID string
            varbinds: List of varbind dictionaries
            source_ip: Source IP address
            snmp_username: Authenticated SNMPv3 username (if available)
        """
        if not self.discovery_log_file:
            return

        try:
            trap_oid_symbolic = self.oid_translator.oid_to_symbolic(trap_oid) if self.oid_translator else trap_oid

            # Client name = username (direct mapping)
            client_name = snmp_username if snmp_username else "unauthenticated"

            # Extract enterprise OID (for vendor identification)
            # Enterprise OIDs are typically 1.3.6.1.4.1.{vendor_number}
            enterprise_oid = None
            if trap_oid.startswith('1.3.6.1.4.1.'):
                parts = trap_oid.split('.')
                if len(parts) >= 7:
                    enterprise_oid = '.'.join(parts[:7])  # 1.3.6.1.4.1.{vendor}

            # Identify numeric varbinds (useful for threshold rules)
            numeric_varbinds = []
            for vb in varbinds:
                if vb['type'] in ['Integer', 'Integer32', 'Gauge32', 'Counter32', 'Counter64']:
                    numeric_varbinds.append({
                        'oid': vb['oid'],
                        'symbolic': vb.get('symbolic', vb['oid']),
                        'value': vb['value'],
                        'type': vb['type']
                    })

            discovery_entry = {
                'timestamp': int(time.time()),
                'source_ip': source_ip,
                'snmp_username': snmp_username,
                'assigned_client': client_name,
                'trap_oid': trap_oid,
                'trap_oid_symbolic': trap_oid_symbolic,
                'enterprise_oid': enterprise_oid,
                'varbinds': varbinds,
                'numeric_varbinds': numeric_varbinds,
                'varbind_count': len(varbinds)
            }

            self.discovery_log_file.write(json.dumps(discovery_entry) + '\n')
            self.discovery_log_file.flush()
            self.stats['traps_discovery_logged'] += 1
        except Exception as e:
            self.logger.error(f"Failed to write discovery log: {e}")

    def apply_template(self, template: str, variables: Dict[str, Any]) -> str:
        """
        Apply template string with variable substitution.

        Args:
            template: Template string with {variable} placeholders
            variables: Dictionary of variable values

        Returns:
            Rendered template string
        """
        result = template
        for key, value in variables.items():
            placeholder = f"{{{key}}}"
            result = result.replace(placeholder, str(value))
        return result

    def find_varbind_value(self, varbinds: List[Dict], oid: str) -> Optional[Any]:
        """
        Find varbind value by OID (supports numeric or symbolic).

        Args:
            varbinds: List of varbind dictionaries
            oid: OID to search for (numeric or symbolic)

        Returns:
            Varbind value or None if not found
        """
        for varbind in varbinds:
            if varbind['oid'] == oid or varbind.get('symbolic') == oid:
                return varbind['value']
        return None

    def apply_threshold_rules(self, varbinds: List[Dict], source_ip: str) -> Optional[Dict[str, Any]]:
        """
        Check varbinds against threshold rules.

        Args:
            varbinds: List of varbind dictionaries
            source_ip: Source IP address

        Returns:
            Alert dictionary if threshold triggered, None otherwise
        """
        snmp_config = self.config.get('snmp', {})
        threshold_rules = snmp_config.get('threshold_rules', [])

        for rule in threshold_rules:
            varbind_oid = rule.get('varbind_oid')
            varbind_name = rule.get('varbind_name')

            # Find matching varbind
            value = self.find_varbind_value(varbinds, varbind_oid)
            if value is None and varbind_name:
                value = self.find_varbind_value(varbinds, varbind_name)

            if value is None:
                continue

            # Check if value is numeric
            try:
                numeric_value = float(value)
            except (ValueError, TypeError):
                continue

            # Check thresholds
            thresholds = rule.get('thresholds', {})
            level = None

            for threshold_level in ['critical', 'warning']:
                threshold_config = thresholds.get(threshold_level)
                if not threshold_config:
                    continue

                operator = threshold_config.get('operator', '>')
                threshold_value = threshold_config.get('value', 0)

                triggered = False
                if operator == '>':
                    triggered = numeric_value > threshold_value
                elif operator == '<':
                    triggered = numeric_value < threshold_value
                elif operator == '>=':
                    triggered = numeric_value >= threshold_value
                elif operator == '<=':
                    triggered = numeric_value <= threshold_value
                elif operator == '==':
                    triggered = numeric_value == threshold_value
                elif operator == '!=':
                    triggered = numeric_value != threshold_value

                if triggered:
                    level = threshold_level
                    break

            if level:
                # Build template variables
                variables = {
                    'value': numeric_value,
                    'hostname': source_ip,
                    'metric_name': rule.get('metric_name', 'unknown')
                }

                # Add varbinds to variables
                for vb in varbinds:
                    oid_key = vb.get('symbolic', vb['oid']).replace('::', '_').replace('.', '_')
                    variables[oid_key] = vb['value']

                self.stats['threshold_matches'] += 1

                return {
                    'level': level,
                    'category': rule.get('category', 'system'),
                    'title': self.apply_template(rule.get('title_template', 'Threshold Alert'), variables),
                    'message': self.apply_template(rule.get('message_template', 'Threshold triggered'), variables)
                }

        return None

    def apply_oid_mappings(self, trap_oid: str, varbinds: List[Dict], source_ip: str) -> Optional[Dict[str, Any]]:
        """
        Apply OID mapping rules to trap.

        Args:
            trap_oid: Trap OID string
            varbinds: List of varbind dictionaries
            source_ip: Source IP address

        Returns:
            Alert dictionary if mapping matched, None otherwise
        """
        snmp_config = self.config.get('snmp', {})
        oid_mappings = snmp_config.get('oid_mappings', [])

        for mapping in oid_mappings:
            if mapping.get('trap_oid') == trap_oid:
                # Build template variables
                variables = {
                    'hostname': source_ip,
                    'trap_oid': trap_oid
                }

                # Add varbinds to variables
                for varbind in varbinds:
                    oid_key = varbind.get('symbolic', varbind['oid']).replace('::', '_').replace('.', '_')
                    variables[oid_key] = varbind['value']

                self.stats['mapping_matches'] += 1

                return {
                    'level': mapping.get('level', 'notice'),
                    'category': mapping.get('category', 'system'),
                    'title': self.apply_template(mapping.get('title_template', 'SNMP Trap'), variables),
                    'message': self.apply_template(mapping.get('message_template', 'Trap received'), variables)
                }

        return None

    def apply_defaults(self, trap_oid: str, source_ip: str) -> Dict[str, Any]:
        """
        Apply default alert values for unmapped traps.

        Args:
            trap_oid: Trap OID string
            source_ip: Source IP address

        Returns:
            Alert dictionary with defaults
        """
        snmp_config = self.config.get('snmp', {})
        defaults = snmp_config.get('defaults', {})

        trap_oid_symbolic = self.oid_translator.oid_to_symbolic(trap_oid) if self.oid_translator else trap_oid

        variables = {
            'hostname': source_ip,
            'trap_oid': trap_oid,
            'trap_oid_symbolic': trap_oid_symbolic
        }

        return {
            'level': defaults.get('level', 'notice'),
            'category': defaults.get('category', 'system'),
            'title': self.apply_template(
                defaults.get('title_template', 'SNMP Trap from {hostname}'),
                variables
            ),
            'message': self.apply_template(
                defaults.get('message_template', 'Trap OID: {trap_oid} ({trap_oid_symbolic})'),
                variables
            )
        }

    def process_trap(self, trap_oid: str, varbinds_raw: List[Tuple], source_ip: str, uptime: int, snmp_username: Optional[str] = None):
        """
        Process received SNMP trap.

        Args:
            trap_oid: Trap OID tuple
            varbinds_raw: List of (OID, value) tuples
            source_ip: Source IP address
            uptime: System uptime in timeticks
            snmp_username: Authenticated SNMPv3 username (if available)
        """
        self.stats['traps_received'] += 1

        # Convert trap OID to string
        trap_oid_str = '.'.join(map(str, trap_oid))

        # Convert varbinds to dictionaries
        varbinds = []
        for oid_tuple, value in varbinds_raw:
            oid_str = '.'.join(map(str, oid_tuple))

            # Determine value type
            value_type = type(value).__name__
            if hasattr(value, 'prettyPrint'):
                value_str = value.prettyPrint()
            else:
                value_str = str(value)

            varbind = {
                'oid': oid_str,
                'value': value_str,
                'type': value_type
            }

            # Add symbolic name if translator available
            if self.oid_translator:
                varbind['symbolic'] = self.oid_translator.oid_to_symbolic(oid_str)

            varbinds.append(varbind)

        # Log to discovery file if enabled
        snmp_config = self.config.get('snmp', {})
        discovery_config = snmp_config.get('discovery_mode', {})
        if discovery_config.get('enabled', False):
            self.log_discovery_trap(trap_oid_str, varbinds, source_ip, snmp_username)

            # Skip MQTT forwarding in discovery mode if configured
            if not discovery_config.get('forward_to_mqtt', False):
                return

        # Apply threshold rules
        alert_data = self.apply_threshold_rules(varbinds, source_ip)

        # If no threshold match, apply OID mappings
        if not alert_data:
            alert_data = self.apply_oid_mappings(trap_oid_str, varbinds, source_ip)

        # If no mapping match, apply defaults
        if not alert_data:
            alert_data = self.apply_defaults(trap_oid_str, source_ip)

        # Direct mapping: username = client name
        client_name = snmp_username if snmp_username else "unauthenticated"

        if snmp_username:
            self.logger.debug(f"Trap from {source_ip} authenticated as '{snmp_username}' → client='{client_name}'")

        # Build complete alert payload
        alert_payload = {
            'timestamp': int(time.time()),
            'level': alert_data['level'],
            'category': alert_data['category'],
            'title': alert_data['title'],
            'message': alert_data['message'],
            'source': f"snmp/{source_ip}",
            'client': client_name,
            'metadata': {
                'snmp_trap_oid': trap_oid_str,
                'snmp_agent_addr': source_ip,
                'snmp_uptime': uptime,
                'snmp_username': snmp_username,
                'varbinds': varbinds
            },
            '_gateway': {
                'source': 'snmp-gateway',
                'received_at': int(time.time()),
                'client_ip': source_ip,
                'protocol': 'snmpv3',
                'authenticated_user': snmp_username
            }
        }

        # Add symbolic trap OID if available
        if self.oid_translator:
            alert_payload['metadata']['snmp_trap_oid_symbolic'] = self.oid_translator.oid_to_symbolic(trap_oid_str)

        # Forward to MQTT
        self.forward_to_mqtt(alert_payload)

    def callback_trap_handler(self, snmpEngine, stateReference, contextEngineId, contextName, varBinds, cbCtx):
        """
        PySNMP callback for received traps.

        This is called by the SNMP engine when a trap is received.
        """
        # Log that callback was invoked
        self.logger.info("=== TRAP CALLBACK INVOKED ===")

        # Extract transport information
        transportDomain, transportAddress = snmpEngine.msgAndPduDsp.getTransportInfo(stateReference)
        source_ip = transportAddress[0]
        self.logger.info(f"Trap received from {source_ip}")

        # Extract authenticated SNMPv3 username
        snmp_username = None
        try:
            # Get security information from SNMP engine state
            execContext = snmpEngine.observer.getExecutionContext('rfc3412.receiveMessage:request')
            if execContext:
                snmp_username = str(execContext.get('securityName', ''))
                if snmp_username:
                    self.logger.info(f"Authenticated user: {snmp_username} from {source_ip}")
                else:
                    snmp_username = None
                    self.logger.warning(f"No username in execution context from {source_ip}")
        except Exception as e:
            self.logger.warning(f"Could not extract security name from {source_ip}: {e}")
            snmp_username = None

        try:
            # Parse varbinds
            # First two varbinds are sysUpTime and snmpTrapOID (RFC 3416)
            trap_oid = None
            uptime = 0
            varbinds_list = []

            for idx, varBind in enumerate(varBinds):
                oid, value = varBind

                if idx == 0:
                    # First varbind is sysUpTime
                    uptime = int(value.prettyPrint())
                elif idx == 1:
                    # Second varbind is snmpTrapOID
                    trap_oid = tuple(oid)
                else:
                    # Remaining varbinds are trap-specific data
                    varbinds_list.append((tuple(oid), value))

            if trap_oid:
                self.process_trap(trap_oid, varbinds_list, source_ip, uptime, snmp_username)
            else:
                self.logger.warning(f"Received trap without trap OID from {source_ip}")
                self.stats['parse_errors'] += 1

        except Exception as e:
            self.logger.error(f"Error processing trap from {source_ip}: {e}", exc_info=True)
            self.stats['parse_errors'] += 1

    def log_stats(self):
        """Log current statistics."""
        uptime = int(time.time()) - self.stats['start_time']
        self.logger.info(
            f"Stats: "
            f"received={self.stats['traps_received']}, "
            f"forwarded={self.stats['traps_forwarded']}, "
            f"discovery_logged={self.stats['traps_discovery_logged']}, "
            f"mapping_matches={self.stats['mapping_matches']}, "
            f"threshold_matches={self.stats['threshold_matches']}, "
            f"parse_errors={self.stats['parse_errors']}, "
            f"mqtt_errors={self.stats['mqtt_errors']}, "
            f"uptime={uptime}s"
        )

    def run(self):
        """Start SNMP trap receiver."""
        snmp_config = self.config.get('snmp', {})

        if not snmp_config.get('enabled', True):
            self.logger.error("SNMP gateway is disabled in configuration")
            sys.exit(1)

        # Enable PySNMP internal debug logging (disabled for normal operation)
        # Uncomment below for deep debugging:
        # pysnmp_debug.set_logger(pysnmp_debug.Debug('all'))

        # Connect to MQTT broker
        self.connect_mqtt()

        # Create SNMP engine
        snmpEngine = engine.SnmpEngine()

        # Configure UDP transport
        bind_address = snmp_config.get('bind_address', '0.0.0.0')
        port = snmp_config.get('port', 162)

        try:
            transport = udp.UdpTransport().open_server_mode((bind_address, port))
            config.add_transport(
                snmpEngine,
                udp.DOMAIN_NAME,
                transport
            )
            self.logger.info(f"UDP transport bound to {bind_address}:{port}")
        except Exception as e:
            self.logger.error(f"Failed to bind UDP transport to {bind_address}:{port}: {e}")
            sys.exit(1)

        # Configure SNMPv3 users from external file
        if not self.snmp_users:
            self.logger.error("No SNMPv3 users configured. Use tools/snmp-auth-manager.sh to create users.")
            sys.exit(1)

        # Map protocol names to pysnmp 7.x constants
        auth_protocol_map = {
            'MD5': config.USM_AUTH_HMAC96_MD5,
            'SHA': config.USM_AUTH_HMAC96_SHA,
            'SHA224': config.USM_AUTH_HMAC128_SHA224,
            'SHA256': config.USM_AUTH_HMAC192_SHA256,
            'SHA384': config.USM_AUTH_HMAC256_SHA384,
            'SHA512': config.USM_AUTH_HMAC384_SHA512
        }

        priv_protocol_map = {
            'DES': config.USM_PRIV_CBC56_DES,
            'AES': config.USM_PRIV_CFB128_AES,
            'AES128': config.USM_PRIV_CFB128_AES,
            'AES192': config.USM_PRIV_CFB192_AES,
            'AES256': config.USM_PRIV_CFB256_AES
        }

        # Configure SNMPv3 users and VACM permissions
        for username, user_data in self.snmp_users.items():
            # Skip revoked users
            if user_data.get('status') != 'active':
                self.logger.warning(f"Skipping revoked user: {username}")
                continue

            # Detect security level from user configuration
            auth_key = user_data.get('auth_key')
            priv_key = user_data.get('priv_key')
            security_level = user_data.get('security_level', 'noAuthNoPriv')

            # Auto-detect security level if not explicitly set
            if security_level == 'auto':
                if auth_key and priv_key:
                    security_level = 'authPriv'
                elif auth_key:
                    security_level = 'authNoPriv'
                else:
                    security_level = 'noAuthNoPriv'

            client_name = user_data.get('client', username)

            # Configure user based on security level
            if security_level == 'noAuthNoPriv':
                # Username-only authentication (like SNMPv2c community string)
                # No authentication or encryption - maximum compatibility
                config.add_v3_user(
                    snmpEngine,
                    username
                )

                # Add VACM permissions for noAuthNoPriv
                config.add_vacm_user(
                    snmpEngine,
                    3,  # SNMPv3
                    username,  # securityName
                    'noAuthNoPriv',  # securityLevel
                    (1, 3, 6, 1, 6, 3, 10, 2, 1),  # readSubTree (trap reception)
                    (1, 3, 6, 1, 6, 3, 10, 2, 1),  # writeSubTree
                    (1, 3, 6, 1, 6, 3, 10, 2, 1),  # notifySubTree (trap/inform)
                    contextName=''  # default context
                )

                self.logger.info(f"Configured SNMPv3 user: {username} (security=noAuthNoPriv, client={client_name}, VACM=enabled)")

            elif security_level == 'authNoPriv':
                # Authentication without encryption - good balance
                if not auth_key:
                    self.logger.error(f"User {username} configured for authNoPriv but missing auth_key")
                    continue

                auth_protocol = user_data.get('auth_protocol', 'SHA')
                auth_proto = auth_protocol_map.get(auth_protocol.upper(), config.USM_AUTH_HMAC96_SHA)

                config.add_v3_user(
                    snmpEngine,
                    username,
                    auth_proto,
                    auth_key
                )

                # Add VACM permissions for authNoPriv
                config.add_vacm_user(
                    snmpEngine,
                    3,  # SNMPv3
                    username,  # securityName
                    'authNoPriv',  # securityLevel
                    (1, 3, 6, 1, 6, 3, 10, 2, 1),  # readSubTree (trap reception)
                    (1, 3, 6, 1, 6, 3, 10, 2, 1),  # writeSubTree
                    (1, 3, 6, 1, 6, 3, 10, 2, 1),  # notifySubTree (trap/inform)
                    contextName=''  # default context
                )

                self.logger.info(f"Configured SNMPv3 user: {username} (security=authNoPriv, auth={auth_protocol}, client={client_name}, VACM=enabled)")

            elif security_level == 'authPriv':
                # Full authentication and encryption - highest security
                if not auth_key or not priv_key:
                    self.logger.error(f"User {username} configured for authPriv but missing auth_key or priv_key")
                    continue

                auth_protocol = user_data.get('auth_protocol', 'SHA')
                priv_protocol = user_data.get('priv_protocol', 'AES')
                auth_proto = auth_protocol_map.get(auth_protocol.upper(), config.USM_AUTH_HMAC96_SHA)
                priv_proto = priv_protocol_map.get(priv_protocol.upper(), config.USM_PRIV_CFB128_AES)

                config.add_v3_user(
                    snmpEngine,
                    username,
                    auth_proto,
                    auth_key,
                    priv_proto,
                    priv_key
                )

                # Add VACM permissions for authPriv
                config.add_vacm_user(
                    snmpEngine,
                    3,  # SNMPv3
                    username,  # securityName
                    'authPriv',  # securityLevel
                    (1, 3, 6, 1, 6, 3, 10, 2, 1),  # readSubTree (trap reception)
                    (1, 3, 6, 1, 6, 3, 10, 2, 1),  # writeSubTree
                    (1, 3, 6, 1, 6, 3, 10, 2, 1),  # notifySubTree (trap/inform)
                    contextName=''  # default context
                )

                self.logger.info(f"Configured SNMPv3 user: {username} (security=authPriv, auth={auth_protocol}, priv={priv_protocol}, client={client_name}, VACM=enabled)")

            else:
                self.logger.error(f"User {username} has invalid security_level: {security_level}")

        # Add default context for trap processing
        self.logger.info("Configuring SNMP context for trap reception...")
        config.add_context(snmpEngine, '')

        self.logger.info("Registering trap receiver callback...")
        notification_receiver = ntfrcv.NotificationReceiver(snmpEngine, self.callback_trap_handler)
        self.logger.info(f"Trap receiver registered: {notification_receiver}")

        self.logger.info(f"SNMP Gateway started on {bind_address}:{port}")
        self.logger.info("Starting asyncio dispatcher - waiting for traps...")

        # Log stats periodically
        last_stats_time = time.time()
        stats_interval = 300  # 5 minutes

        try:
            # PySNMP 7.x with asyncio - use open_dispatcher() then keep alive
            # open_dispatcher() is non-blocking, so we need to keep the process alive
            snmpEngine.open_dispatcher()

            # Keep process alive and log stats periodically
            while True:
                time.sleep(1)
                current_time = time.time()
                if current_time - last_stats_time >= stats_interval:
                    self.log_stats()
                    last_stats_time = current_time

        except KeyboardInterrupt:
            self.logger.info("Received shutdown signal")
        finally:
            self.log_stats()
            snmpEngine.close_dispatcher()

            if self.discovery_log_file:
                self.discovery_log_file.close()

            if self.mqtt_client:
                self.mqtt_client.loop_stop()
                self.mqtt_client.disconnect()

            self.logger.info("SNMP Gateway stopped")


if __name__ == '__main__':
    # Allow config file override via command line
    config_file = sys.argv[1] if len(sys.argv) > 1 else "/data/config/docker.json"

    gateway = SNMPGateway(config_file)
    gateway.run()
