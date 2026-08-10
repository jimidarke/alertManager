"""MQTT publisher wrapper for feed aggregator"""

import json
import logging
import paho.mqtt.client as mqtt

logger = logging.getLogger(__name__)


class MQTTPublisher:
    """MQTT client wrapper for publishing alerts"""

    def __init__(self, mqtt_config: dict):
        self.broker = mqtt_config['broker']
        self.port = mqtt_config['port']
        self.topic = mqtt_config['topic']
        self.client = mqtt.Client(callback_api_version=mqtt.CallbackAPIVersion.VERSION2)

        # Set up callbacks
        self.client.on_connect = self._on_connect
        self.client.on_disconnect = self._on_disconnect
        self.client.on_publish = self._on_publish

        self.connected = False

    def _on_connect(self, client, userdata, flags, reason_code, properties):
        """Callback for successful connection"""
        if reason_code == 0:
            self.connected = True
            logger.info(f"Connected to MQTT broker {self.broker}:{self.port}")
        else:
            logger.error(f"Failed to connect to MQTT broker: {reason_code}")

    def _on_disconnect(self, client, userdata, flags, reason_code, properties):
        """Callback for disconnection"""
        self.connected = False
        logger.warning(f"Disconnected from MQTT broker: {reason_code}")

    def _on_publish(self, client, userdata, mid, reason_code, properties):
        """Callback for successful publish"""
        logger.debug(f"Message {mid} published successfully")

    def connect(self):
        """Connect to MQTT broker"""
        try:
            self.client.connect(self.broker, self.port, 60)
            self.client.loop_start()
            logger.info(f"Connecting to MQTT broker {self.broker}:{self.port}")
        except Exception as e:
            logger.critical(f"Failed to connect to MQTT broker: {e}")
            raise

    def disconnect(self):
        """Disconnect from MQTT broker"""
        self.client.loop_stop()
        self.client.disconnect()
        logger.info("MQTT client disconnected")

    def publish_alert(self, payload: dict) -> bool:
        """
        Publish alert payload to alerts/input topic
        Returns True on success, False on failure
        """
        if not self.connected:
            logger.error("Cannot publish: not connected to MQTT broker")
            return False

        try:
            message = json.dumps(payload)
            result = self.client.publish(self.topic, message, qos=1)

            if result.rc == mqtt.MQTT_ERR_SUCCESS:
                logger.info(f"Published alert: {payload['title']}")
                return True
            else:
                logger.error(f"Failed to publish alert: {result.rc}")
                return False

        except Exception as e:
            logger.error(f"Exception during publish: {e}")
            return False
