"""
Alert Manager - gateway.mqtt

`MqttPublisher`: ports src/http_gateway.py's `HTTPGateway.connect_mqtt()` /
`HTTPGateway.forward_to_mqtt()` methods (read verbatim before writing this
module) onto `GatewayConfig` instead of a JSON config dict. Same MQTT
client v2 callback API setup, same lazy reconnect-on-publish behavior,
same topic default (`alerts/input`) and QoS (1), same JSON payload shape
(`json.dumps(alert_data)`, no envelope). Nothing about the wire protocol
changes -- only where configuration comes from.

Deliberate small differences from the original, noted for the record:
- The client_id prefix changes from `"http-gateway-"` to `"gateway-"`
  (this package is not named http_gateway) -- cosmetic only, visible in
  broker connection logs, has no effect on routing/ACLs (the internal
  broker at localhost:1883 is unauthenticated, container-network-only
  per CLAUDE.md; client_id is not used for ACL matching there).
- `connect_mqtt()`/`forward_to_mqtt()`/`is_connected()` are instance
  methods on a standalone class rather than methods mixed into a Flask App
  wrapper class, so blueprints can reach the publisher via
  `current_app.extensions['mqtt_publisher']` without going through
  `self.mqtt_client` attribute access. `is_connected()` is new (the
  original inlined `self.mqtt_client.is_connected() if self.mqtt_client
  else False` directly in its /health route); it exists here so a future
  /health route (Task 20) can call `mqtt_publisher.is_connected()`
  instead of reaching into a private attribute.
"""

import json
import logging
import socket
import time
from typing import Any, Dict, Optional

import paho.mqtt.client as mqtt

from .config import GatewayConfig

logger = logging.getLogger("gateway.mqtt")


class MqttPublisher:
    """Owns one MQTT client connection and publishes alert payloads to `config.MQTT_INPUT_TOPIC`."""

    def __init__(self, config: GatewayConfig):
        self._config = config
        self._client: Optional[mqtt.Client] = None

    def is_connected(self) -> bool:
        return bool(self._client and self._client.is_connected())

    def connect_mqtt(self) -> bool:
        """
        Connect to the MQTT broker with the v2 callback API (this
        project's established convention -- see CLAUDE.md "Development
        Notes": "Use MQTT client v2 with callback_api_version set
        properly"). Returns True/False; never raises (matches the
        original's broad except -> log -> return False so a caller can
        decide what "failed to connect" means for it -- e.g.
        gateway/__init__.py's create_app() treats a failed *initial*
        connect as fatal, the same way src/http_gateway.py's run() did).
        """
        try:
            hostname = socket.gethostname()
            client_id = f"gateway-{hostname}-{int(time.time())}"

            self._client = mqtt.Client(
                client_id=client_id,
                callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
            )

            self._client.connect(self._config.MQTT_BROKER, self._config.MQTT_PORT, 60)
            self._client.loop_start()
            return True
        except Exception:
            logger.error("Failed to connect to MQTT broker", exc_info=True)
            return False

    def forward_to_mqtt(self, alert_data: Dict[str, Any]) -> bool:
        """
        Port of HTTPGateway.forward_to_mqtt(): publish `alert_data` as JSON
        to `config.MQTT_INPUT_TOPIC` at QoS 1, lazily (re)connecting first
        if the client is missing/disconnected. Returns True/False; never
        raises.
        """
        try:
            if not self.is_connected():
                logger.warning("MQTT client not connected, attempting reconnection...")
                self.connect_mqtt()

            result = self._client.publish(
                self._config.MQTT_INPUT_TOPIC,
                json.dumps(alert_data),
                qos=1,
            )

            if result.rc == mqtt.MQTT_ERR_SUCCESS:
                return True

            logger.error(f"MQTT publish failed with code: {result.rc}")
            return False
        except Exception:
            logger.error("Error forwarding to MQTT", exc_info=True)
            return False
