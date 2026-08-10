#!/usr/bin/env python3
"""
Syslog to MQTT Gateway Service

⚠️  DEPRECATED: This service has been deprecated in favor of MQTT and HTTP gateways.
⚠️  Use MQTT (port 42690) or HTTP (port 8080) for new integrations.
⚠️  This code is preserved for reference but is no longer actively maintained.

UDP syslog receiver that accepts syslog messages (RFC3164/RFC5424) and forwards
them to the MQTT broker as standardized alert messages. Provides a simple way
for network devices and systems to send alerts via the ubiquitous syslog protocol.

DEPRECATION RATIONALE:
- Syslog lacks support for critical Alert Manager features (client field, metadata, routing)
- Limited category mapping (only security and system, missing 6 other categories)
- No authentication or encryption
- MQTT and HTTP provide superior feature coverage and flexibility
"""

import json
import logging
import os
import re
import socket
import sys
import time
from datetime import datetime
from typing import Dict, Any, Optional

try:
    import paho.mqtt.client as mqtt
except ImportError:
    print("Error: paho-mqtt not found. Install with: pip install paho-mqtt")
    sys.exit(1)

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    print("Warning: python-dotenv not found. Install with: pip install python-dotenv")


class SyslogGateway:
    """Syslog to MQTT Gateway Service"""

    # Syslog severity levels (RFC 5424) mapped to Alert Manager levels
    # Alert Manager levels: debug, info, notice, warning, error, critical
    SEVERITY_MAP = {
        0: 'critical',  # Emergency -> critical
        1: 'critical',  # Alert -> critical
        2: 'critical',  # Critical -> critical
        3: 'error',     # Error -> error
        4: 'warning',   # Warning -> warning
        5: 'notice',    # Notice -> notice
        6: 'info',      # Informational -> info
        7: 'debug'      # Debug -> debug
    }

    # Syslog facility map
    FACILITY_MAP = {
        0: 'system',      # kernel messages
        1: 'system',      # user-level messages
        2: 'system',      # mail system
        3: 'system',      # system daemons
        4: 'security',    # security/authorization messages
        10: 'security',   # security/authorization messages
        13: 'security',   # log audit
        14: 'security',   # log alert
        16: 'system',     # local use 0
        17: 'system',     # local use 1
        18: 'system',     # local use 2
        19: 'system',     # local use 3
        20: 'system',     # local use 4
        21: 'system',     # local use 5
        22: 'system',     # local use 6
        23: 'system',     # local use 7
    }

    def __init__(self, config_file: str = "/data/config/docker.json"):
        self.config = self.load_config(config_file)
        self.mqtt_client = None
        self.socket = None
        self.stats = {
            'messages_received': 0,
            'messages_forwarded': 0,
            'parse_errors': 0,
            'mqtt_errors': 0,
            'start_time': int(time.time())
        }
        self.setup_logging()

    def load_config(self, config_file: str) -> Dict[str, Any]:
        """Load configuration from JSON file"""
        try:
            with open(config_file, 'r') as f:
                return json.load(f)
        except FileNotFoundError:
            # Use defaults if config not found
            logging.warning(f"Config file not found: {config_file}, using defaults")
            return {}
        except json.JSONDecodeError as e:
            logging.error(f"Invalid JSON in config file: {e}")
            sys.exit(1)

    def setup_logging(self):
        """Configure logging"""
        log_level = self.config.get('logging', {}).get('level', 'INFO')

        # Create formatter
        formatter = logging.Formatter(
            '%(asctime)s - %(name)s - %(levelname)s - %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S'
        )

        # Configure root logger
        root_logger = logging.getLogger()
        root_logger.setLevel(getattr(logging, log_level))

        # Console handler
        console_handler = logging.StreamHandler()
        console_handler.setLevel(getattr(logging, log_level))
        console_handler.setFormatter(formatter)
        root_logger.addHandler(console_handler)

        self.logger = logging.getLogger('syslog-gateway')

    def parse_syslog_message(self, data: bytes, source_ip: str) -> Optional[Dict[str, Any]]:
        """
        Parse syslog message (RFC3164 or RFC5424 format)

        RFC3164: <PRI>TIMESTAMP HOSTNAME MESSAGE
        RFC5424: <PRI>VERSION TIMESTAMP HOSTNAME APP-NAME PROCID MSGID STRUCTURED-DATA MSG
        """
        try:
            message = data.decode('utf-8', errors='ignore').strip()

            if not message:
                return None

            # Extract priority
            priority_match = re.match(r'^<(\d+)>', message)
            if priority_match:
                priority = int(priority_match.group(1))
                message = message[priority_match.end():]
                severity = priority & 0x07
                facility = priority >> 3
            else:
                # No priority found, default to info/user
                severity = 6
                facility = 1
                priority = None

            # Map severity and facility to alert format
            level = self.SEVERITY_MAP.get(severity, 'info')
            category = self.FACILITY_MAP.get(facility, 'system')

            # Try to parse RFC5424 format first
            rfc5424_match = re.match(
                r'^(\d+)\s+(\S+)\s+(\S+)\s+(\S+)\s+(\S+)\s+(\S+)\s+(\S+|\-)\s+(.*)',
                message
            )

            if rfc5424_match:
                # RFC5424 format
                version, timestamp, hostname, app_name, proc_id, msg_id, structured_data, msg = rfc5424_match.groups()

                # Clean up hostname
                hostname = hostname if hostname != '-' else source_ip

                # Build source identifier
                source = f"{hostname}"
                if app_name != '-':
                    source = f"{hostname}/{app_name}"
                if proc_id != '-':
                    source = f"{source}[{proc_id}]"

                # Use msg_id as title if available
                title = msg_id if msg_id != '-' else f"Syslog from {hostname}"

                # Parse structured data if present
                metadata = {'syslog_facility': facility, 'syslog_severity': severity}
                if structured_data != '-':
                    metadata['structured_data'] = structured_data

            else:
                # Try RFC3164 format: TIMESTAMP HOSTNAME MESSAGE
                # or simple format: HOSTNAME MESSAGE
                # or just MESSAGE
                parts = message.split(None, 2)

                if len(parts) >= 2:
                    # Assume first part might be timestamp or hostname
                    # If it looks like a timestamp (MMM DD HH:MM:SS), skip it
                    if re.match(r'^[A-Z][a-z]{2}\s+\d+\s+\d+:\d+:\d+', parts[0] + ' ' + parts[1]):
                        hostname = parts[2].split()[0] if len(parts) > 2 else source_ip
                        msg = ' '.join(parts[2].split()[1:]) if len(parts) > 2 else ''
                    else:
                        hostname = parts[0]
                        msg = ' '.join(parts[1:])
                else:
                    hostname = source_ip
                    msg = message

                source = hostname
                title = f"Syslog from {hostname}"
                metadata = {'syslog_facility': facility, 'syslog_severity': severity}

            # Build alert object with standardized structure
            alert = {
                'timestamp': int(time.time()),
                'level': level,
                'category': category,
                'title': title[:100],  # Limit title length
                'message': msg[:1000],  # Limit message length
                'source': source,  # Simple string identifier
                'metadata': {
                    'syslog_facility': facility,
                    'syslog_severity': severity,
                    'source_ip': source_ip,
                    'raw_priority': priority if priority is not None else None
                },
                '_gateway': {
                    'source': 'syslog-gateway',
                    'received_at': int(time.time()),
                    'client_ip': source_ip,
                    'protocol': 'syslog'
                }
            }

            return alert

        except Exception as e:
            self.logger.error(f"Error parsing syslog message from {source_ip}: {e}")
            self.stats['parse_errors'] += 1
            return None

    def forward_to_mqtt(self, alert_data: Dict[str, Any]) -> bool:
        """Forward alert to MQTT broker"""
        try:
            if not self.mqtt_client or not self.mqtt_client.is_connected():
                self.logger.warning("MQTT client not connected, attempting reconnection...")
                self.connect_mqtt()

            mqtt_config = self.config.get('mqtt', {})
            input_topic = mqtt_config.get('input_topic', 'alerts/input')

            payload = json.dumps(alert_data)

            # Publish to MQTT
            result = self.mqtt_client.publish(
                input_topic,
                payload,
                qos=1
            )

            if result.rc == mqtt.MQTT_ERR_SUCCESS:
                self.stats['messages_forwarded'] += 1
                return True
            else:
                self.logger.error(
                    f"MQTT publish failed with return code: {result.rc}"
                )
                self.stats['mqtt_errors'] += 1
                return False

        except Exception as e:
            self.logger.error(f"Error forwarding to MQTT: {str(e)}")
            self.stats['mqtt_errors'] += 1
            return False

    def connect_mqtt(self):
        """Connect to MQTT broker"""
        try:
            mqtt_config = self.config.get('mqtt', {})

            # Generate unique client ID using hostname (container name in Docker)
            import socket
            hostname = socket.gethostname()
            client_id = f"syslog-gateway-{hostname}-{int(time.time())}"

            # Initialize MQTT client with callback API v2
            self.mqtt_client = mqtt.Client(
                client_id=client_id,
                callback_api_version=mqtt.CallbackAPIVersion.VERSION2
            )

            # Connect to broker (default to internal Docker broker)
            broker = mqtt_config.get('broker', 'mqtt-broker')
            port = mqtt_config.get('port', 1883)

            self.mqtt_client.connect(broker, port, 60)
            self.mqtt_client.loop_start()

            return True

        except Exception as e:
            self.logger.error(f"Failed to connect to MQTT broker: {str(e)}")
            return False

    def run(self, host: str = '0.0.0.0', port: int = 51514):
        """Run the syslog gateway service"""
        # Connect to MQTT
        if not self.connect_mqtt():
            self.logger.error("Failed to connect to MQTT broker")
            sys.exit(1)

        # Create UDP socket
        try:
            self.socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self.socket.bind((host, port))
        except Exception as e:
            self.logger.error(f"Failed to create UDP socket: {e}")
            sys.exit(1)

        # Main loop
        last_stats_time = time.time()
        try:
            while True:
                # Receive syslog message
                data, addr = self.socket.recvfrom(65535)
                self.stats['messages_received'] += 1

                source_ip = addr[0]

                # Parse syslog message
                alert = self.parse_syslog_message(data, source_ip)

                if alert:
                    # Forward to MQTT
                    self.forward_to_mqtt(alert)

                # Log stats every 5 minutes
                if time.time() - last_stats_time > 300:
                    self.log_stats()
                    last_stats_time = time.time()

        except KeyboardInterrupt:
            pass
        except Exception as e:
            self.logger.error(f"Syslog Gateway error: {e}")
            raise
        finally:
            if self.socket:
                self.socket.close()
            if self.mqtt_client:
                self.mqtt_client.loop_stop()
                self.mqtt_client.disconnect()

    def log_stats(self):
        """Log statistics"""
        uptime = int(time.time()) - self.stats['start_time']
        self.logger.info(
            f"Stats: received={self.stats['messages_received']}, "
            f"forwarded={self.stats['messages_forwarded']}, "
            f"parse_errors={self.stats['parse_errors']}, "
            f"mqtt_errors={self.stats['mqtt_errors']}, "
            f"uptime={uptime}s"
        )


def main():
    """Main entry point"""
    # Print deprecation warning
    print("=" * 80)
    print("⚠️  WARNING: SYSLOG GATEWAY IS DEPRECATED")
    print("=" * 80)
    print("This service has been deprecated in favor of MQTT and HTTP gateways.")
    print("Use MQTT (port 42690) or HTTP (port 8080) for new integrations.")
    print("This code is preserved for reference but is no longer actively maintained.")
    print("=" * 80)
    print()

    config_file = os.getenv('CONFIG_FILE', '/data/config/docker.json')

    # For local testing
    if not os.path.exists(config_file):
        config_file = './data/config/docker.json'

    gateway = SyslogGateway(config_file)

    # Get port from environment or default
    port = int(os.getenv('SYSLOG_PORT', 51514))

    try:
        gateway.run(port=port)
    except KeyboardInterrupt:
        pass
    except Exception as e:
        gateway.logger.error(f"Syslog Gateway crashed: {str(e)}")
        sys.exit(1)


if __name__ == '__main__':
    main()
