#!/usr/bin/env python3
"""
Alert Router Service

Clean implementation using modular pathway system for routing alerts
to different output channels based on configurable rules.
"""

import json
import logging
import os
import signal
import sys
import time
import pytz
from datetime import datetime
from typing import Dict, List, Any, Optional

try:
    import paho.mqtt.client as mqtt
except ImportError:
    print("Error: paho-mqtt library not found. Install with: pip install paho-mqtt")
    sys.exit(1)

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    print("Warning: python-dotenv not found. Install with: pip install python-dotenv")

# Add pathways to Python path
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__))))

from pathways import PathwayManager
from utils import heartbeat


class AlertRouter:
    """Main alert routing service"""

    def __init__(self, config_file: str = "config.json"):
        self.config_file = config_file
        self.config = self.load_config()
        self.mqtt_client = None
        self.running = True
        self.pathway_manager = PathwayManager()

        # Heartbeat file, polled by the container healthcheck to detect a
        # hung/dead process (see src/utils/heartbeat.py). file-logger reuses
        # this same script with a different HEARTBEAT_FILE so its heartbeat
        # doesn't collide with alert-router's on the shared ops log volume.
        self.heartbeat_path = os.getenv('HEARTBEAT_FILE', '/data/logs/ops/alert-router.heartbeat')

        # Statistics
        utc = pytz.UTC
        self.stats = {
            'alerts_processed': 0,
            'alerts_routed': 0,
            'start_time': datetime.now(utc),
            'last_alert': None,
            'errors': 0,
            'pathway_errors': {}
        }
        
        # Setup components
        self.setup_logging()
        self.load_pathways()
        
        # Setup signal handlers
        signal.signal(signal.SIGINT, self.signal_handler)
        signal.signal(signal.SIGTERM, self.signal_handler)
    
    def load_config(self) -> Dict[str, Any]:
        """Load configuration from JSON file"""
        try:
            with open(self.config_file, 'r') as f:
                config = json.load(f)
                self.logger = logging.getLogger("alert_router")
                return config
        except FileNotFoundError:
            print(f"Error: Configuration file {self.config_file} not found")
            sys.exit(1)
        except json.JSONDecodeError as e:
            print(f"Error: Invalid JSON in configuration file: {e}")
            sys.exit(1)
    
    def setup_logging(self):
        """Setup logging configuration"""
        logging_config = self.config.get('logging', {})
        log_level = getattr(logging, logging_config.get('level', 'INFO').upper(), logging.INFO)
        
        # Create formatter
        formatter = logging.Formatter(
            '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
        )
        
        # Setup root logger
        logger = logging.getLogger()
        logger.setLevel(log_level)
        
        # Clear any existing handlers
        logger.handlers.clear()
        
        # Console handler
        if logging_config.get('console', True):
            console_handler = logging.StreamHandler()
            console_handler.setFormatter(formatter)
            logger.addHandler(console_handler)
        
        # File handler
        log_file = logging_config.get('file')
        if log_file:
            try:
                # Substitute {date} with current date in YYYY-MM-DD format
                utc = pytz.UTC
                current_date = datetime.now(utc).strftime('%Y-%m-%d')
                log_file = log_file.replace('{date}', current_date)

                # Ensure directory exists
                log_dir = os.path.dirname(log_file)
                if log_dir and not os.path.exists(log_dir):
                    os.makedirs(log_dir, exist_ok=True)

                file_handler = logging.FileHandler(log_file)
                file_handler.setFormatter(formatter)
                logger.addHandler(file_handler)
            except Exception as e:
                print(f"Warning: Could not setup file logging to {log_file}: {e}")
        
        self.logger = logging.getLogger("alert_router")
    
    def load_pathways(self):
        """Load pathway configurations"""
        pathway_configs = self.config.get('pathways', {})

        if not pathway_configs:
            self.logger.error("No pathways configured")
            sys.exit(1)

        if not self.pathway_manager.load_pathways(pathway_configs):
            self.logger.error("Failed to load some pathways")
            # Continue running with successfully loaded pathways
    
    def setup_mqtt(self) -> bool:
        """Setup MQTT client and connection"""
        mqtt_config = self.config.get('mqtt', {})
        broker = mqtt_config.get('broker', 'localhost')
        port = mqtt_config.get('port', 1883)

        # Generate unique client ID using hostname (container name in Docker)
        import socket
        hostname = socket.gethostname()
        client_id = f"alert-router-{hostname}-{int(time.time())}"

        # Initialize MQTT client with latest API
        self.mqtt_client = mqtt.Client(
            client_id=client_id,
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2
        )

        # Set credentials if provided
        username = mqtt_config.get('username')
        password = mqtt_config.get('password')
        if username:
            self.mqtt_client.username_pw_set(username, password)

        # Setup callbacks
        self.mqtt_client.on_connect = self.on_connect
        self.mqtt_client.on_message = self.on_message
        self.mqtt_client.on_disconnect = self.on_disconnect

        # Connect to broker
        try:
            self.mqtt_client.connect(broker, port, 60)
            return True
        except Exception as e:
            self.logger.error(f"Failed to connect to MQTT broker: {e}", exc_info=True)
            return False
    
    def on_connect(self, client, userdata, flags, rc, properties=None):
        """MQTT connection callback"""
        mqtt_config = self.config.get('mqtt', {})

        if rc == 0:
            # Subscribe to input topic
            input_topic = mqtt_config.get('input_topic', 'alerts/input')
            try:
                result = client.subscribe(input_topic)
                if result[0] != mqtt.MQTT_ERR_SUCCESS:
                    self.logger.error(f"Failed to subscribe to {input_topic}")
            except Exception as e:
                self.logger.error(f"Error subscribing to {input_topic}: {e}", exc_info=True)

            # Publish online status
            self.publish_service_status('online')
        else:
            error_messages = {
                1: "Connection refused - incorrect protocol version",
                2: "Connection refused - invalid client identifier",
                3: "Connection refused - server unavailable",
                4: "Connection refused - bad username or password",
                5: "Connection refused - not authorised"
            }
            error_msg = error_messages.get(rc, f"Unknown error code {rc}")
            self.logger.error(f"MQTT connection failed: {error_msg}")
    
    def on_disconnect(self, client, userdata, disconnect_flags, rc, properties=None):
        """MQTT disconnection callback"""
        if rc != 0:
            self.logger.warning(f"Unexpected MQTT disconnection: {rc}")
    
    def on_message(self, client, userdata, msg, properties=None):
        """MQTT message callback"""
        try:
            # Parse JSON alert
            alert = json.loads(msg.payload.decode())

            # Validate and process alert
            if self.validate_alert(alert):
                self.process_alert(alert)
                self.stats['alerts_processed'] += 1
                self.stats['last_alert'] = datetime.now()
            else:
                self.stats['errors'] += 1

        except json.JSONDecodeError as e:
            self.logger.error(f"Error parsing alert JSON: {e}", exc_info=True)
            self.stats['errors'] += 1
        except Exception as e:
            self.logger.error(f"Error processing alert: {e}", exc_info=True)
            self.stats['errors'] += 1
    
    def validate_alert(self, alert: Dict[str, Any]) -> bool:
        """Validate alert message format"""
        required_fields = ['timestamp', 'level', 'category', 'title', 'message', 'source']

        for field in required_fields:
            if field not in alert:
                self.logger.critical(f"Missing required field: {field}")
                raise ValueError(f"Missing required field: {field}")

        # Timestamp must be Unix epoch integer
        if not isinstance(alert['timestamp'], int):
            self.logger.critical(f"timestamp must be Unix epoch integer, got: {type(alert['timestamp'])}")
            raise ValueError("timestamp must be Unix epoch integer")

        # Source must be a string
        source = alert['source']
        if not isinstance(source, str):
            self.logger.critical(f"source must be string, got: {type(source)}")
            raise ValueError("source must be string")

        # Normalize and validate level
        if not isinstance(alert['level'], str):
            self.logger.critical(f"level must be string, got: {type(alert['level'])}")
            raise ValueError("level must be string")

        # Normalize: strip whitespace and convert to lowercase
        normalized_level = alert['level'].strip().lower()

        # Level aliases (for compatibility with syslog and common usage)
        level_aliases = {
            'error': 'critical',
            'err': 'critical',
            'crit': 'critical',
            'warn': 'warning',
            'inf': 'info',
            'dbg': 'debug'
        }

        # Apply alias mapping
        if normalized_level in level_aliases:
            normalized_level = level_aliases[normalized_level]

        # Validate against valid levels
        valid_levels = ['critical', 'warning', 'notice', 'info', 'debug']
        if normalized_level not in valid_levels:
            self.logger.critical(f"Invalid alert level: '{alert['level']}' (normalized: '{normalized_level}')")
            raise ValueError(f"Invalid alert level: '{alert['level']}'. Valid levels: {', '.join(valid_levels)}")

        # Update alert with normalized level
        alert['level'] = normalized_level

        # Normalize and validate category
        if not isinstance(alert['category'], str):
            self.logger.critical(f"category must be string, got: {type(alert['category'])}")
            raise ValueError("category must be string")

        # Normalize: strip whitespace and convert to lowercase
        normalized_category = alert['category'].strip().lower()

        valid_categories = ['security', 'system', 'weather', 'automation', 'personal', 'network', 'application', 'news', 'business', 'hardware', 'environmental']
        if normalized_category not in valid_categories:
            self.logger.critical(f"Invalid alert category: '{alert['category']}' (normalized: '{normalized_category}')")
            raise ValueError(f"Invalid alert category: '{alert['category']}'. Valid categories: {', '.join(valid_categories)}")

        # Update alert with normalized category
        alert['category'] = normalized_category

        # Validate and normalize client if present (optional field)
        if 'client' in alert and alert['client'] is not None:
            if not isinstance(alert['client'], str):
                self.logger.critical(f"client must be string, got: {type(alert['client'])}")
                raise ValueError("client must be string")
            # Normalize client: strip whitespace and convert to lowercase
            alert['client'] = alert['client'].strip().lower()

        return True
    
    def process_alert(self, alert: Dict[str, Any]):
        """Process alert and route to appropriate pathways"""
        # Apply routing rules to determine target pathways
        target_pathways = self.apply_routing_rules(alert)

        if not target_pathways:
            return

        # Extract pathway-specific configurations
        pathway_config = alert.get('pathway_config', {})

        # Send to each pathway with its specific config
        results = self.pathway_manager.send_to_multiple(target_pathways, alert, pathway_config)

        # Update statistics
        for pathway_name, success in results.items():
            if success:
                self.stats['alerts_routed'] += 1
            else:
                self.stats['errors'] += 1
                if pathway_name not in self.stats['pathway_errors']:
                    self.stats['pathway_errors'][pathway_name] = 0
                self.stats['pathway_errors'][pathway_name] += 1
    
    def apply_routing_rules(self, alert: Dict[str, Any]) -> List[str]:
        """Apply routing rules to determine target pathways"""
        routing_rules = self.config.get('routing_rules', [])

        # Check for routing override in alert
        if alert.get('routing', {}).get('override', False):
            overrides = alert.get('routing', {}).get('pathways', [])
            return overrides

        # Apply rules in order (first match wins)
        for rule in routing_rules:
            if self.matches_rule(alert, rule):
                pathways = rule.get('pathways', [])
                return pathways

        # No rules matched
        return []
    
    def matches_rule(self, alert: Dict[str, Any], rule: Dict[str, Any]) -> bool:
        """Check if alert matches routing rule conditions"""
        conditions = rule.get('conditions', {})
        
        # Check level condition
        if 'level' in conditions:
            if alert['level'] not in conditions['level']:
                return False
        
        # Check category condition
        if 'category' in conditions:
            if alert['category'] not in conditions['category']:
                return False
        
        # Check source condition
        if 'source' in conditions:
            if alert['source'] not in conditions['source']:
                return False

        # Check client condition
        if 'client' in conditions:
            alert_client = alert.get('client', '')
            if alert_client not in conditions['client']:
                return False

        # Check metadata conditions
        if 'metadata' in conditions and 'metadata' in alert:
            metadata_conditions = conditions['metadata']
            alert_metadata = alert['metadata']
            
            for key, expected_values in metadata_conditions.items():
                if key not in alert_metadata:
                    return False
                if alert_metadata[key] not in expected_values:
                    return False
        
        return True
    
    def publish_service_status(self, status: str):
        """Publish service status to MQTT"""
        if not self.mqtt_client:
            return

        mqtt_config = self.config.get('mqtt', {})
        status_topic = mqtt_config.get('status_topic', 'alerts/service/status')

        utc = pytz.UTC
        now = datetime.now(utc)

        # Merge each pathway's async-error count (e.g. EmailPathway's
        # storm-prevention timer-thread flush failures, which never touch
        # self.stats['pathway_errors'] since they don't happen inside
        # process_alert()) into that same per-pathway count. Computed
        # fresh on every publish rather than accumulated into self.stats,
        # since pathway.async_errors is itself already a cumulative
        # counter -- accumulating it here too would double-count.
        merged_pathway_errors = dict(self.stats['pathway_errors'])
        for pathway_name, async_count in self.pathway_manager.get_async_errors().items():
            if async_count:
                merged_pathway_errors[pathway_name] = merged_pathway_errors.get(pathway_name, 0) + async_count

        status_data = {
            'status': status,
            'timestamp': now.isoformat(),
            'stats': {
                'alerts_processed': self.stats['alerts_processed'],
                'alerts_routed': self.stats['alerts_routed'],
                'errors': self.stats['errors'],
                'uptime_seconds': (now - self.stats['start_time']).total_seconds(),
                'last_alert': self.stats['last_alert'].isoformat() if self.stats['last_alert'] else None,
                'loaded_pathways': self.pathway_manager.get_pathway_names(),
                'enabled_pathways': self.pathway_manager.get_enabled_pathways(),
                'pathway_errors': merged_pathway_errors
            }
        }

        try:
            self.mqtt_client.publish(status_topic, json.dumps(status_data))
        except Exception as e:
            self.logger.error(f"Error publishing status: {e}", exc_info=True)
    
    def signal_handler(self, signum, frame):
        """Handle shutdown signals"""
        self.logger.info(f"Received signal {signum}, shutting down...")
        self.running = False
    
    def run(self):
        """Main service loop"""
        self.logger.info("Starting Alert Router Service")

        # Setup MQTT connection
        if not self.setup_mqtt():
            self.logger.error("Failed to setup MQTT, exiting")
            return

        # Start MQTT loop
        self.mqtt_client.loop_start()

        try:
            # Main service loop
            while self.running:
                self.publish_service_status('running')
                heartbeat.beat(self.heartbeat_path)
                time.sleep(60)

        except KeyboardInterrupt:
            pass
        finally:
            try:
                # Shutdown pathways (flushes pending storm prevention alerts)
                self.pathway_manager.shutdown_all()

                self.publish_service_status('offline')
                self.mqtt_client.loop_stop()
                self.mqtt_client.disconnect()
            except Exception as e:
                self.logger.error(f"Error during shutdown: {e}", exc_info=True)


def main():
    """Main entry point"""
    import argparse
    
    parser = argparse.ArgumentParser(description='Alert Router Service')
    parser.add_argument('--config', default='data/config/docker.json',
                       help='Configuration file path')
    parser.add_argument('--test', action='store_true',
                       help='Run in test mode with sample alerts')
    
    args = parser.parse_args()
    
    # Create and run alert router
    router = AlertRouter(args.config)
    
    if args.test:
        # Test mode - process sample alerts without MQTT
        test_alerts = [
            {
                "timestamp": datetime.now().isoformat(),
                "level": "critical",
                "category": "security",
                "title": "Motion Detected",
                "message": "Front door motion sensor triggered",
                "source": "test_script"
            },
            {
                "timestamp": datetime.now().isoformat(),
                "level": "warning", 
                "category": "system",
                "title": "High CPU Usage",
                "message": "CPU usage is at 85%",
                "source": "test_script"
            },
            {
                "timestamp": datetime.now().isoformat(),
                "level": "info",
                "category": "weather",
                "title": "Weather Update",
                "message": "Temperature is 72°F",
                "source": "test_script"
            }
        ]
        
        print("Test mode - processing sample alerts...")
        for alert in test_alerts:
            router.process_alert(alert)
            time.sleep(1)
    else:
        router.run()


if __name__ == "__main__":
    main()