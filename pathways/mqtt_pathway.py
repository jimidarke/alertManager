"""
MQTT Pathway

Handles publishing alerts to MQTT topics (LED signs, IoT devices, etc.).
"""

import json
import logging
import time
from typing import Dict, Any, Optional
from .base_pathway import BasePathway

try:
    import paho.mqtt.client as mqtt
except ImportError:
    mqtt = None


class MQTTPathway(BasePathway):
    """MQTT pathway for publishing alerts to MQTT topics"""
    
    def __init__(self, name: str, config: Dict[str, Any]):
        """
        Initialize MQTT pathway
        
        Args:
            name: Pathway instance name
            config: Configuration dictionary containing:
                - broker: MQTT broker address
                - port: MQTT broker port (default: 1883)
                - username: MQTT username (optional)
                - password: MQTT password (optional)
                - topic: MQTT topic to publish to
                - format: Message format template (optional)
                - qos: MQTT QoS level (default: 0)
                - retain: Retain MQTT message (default: False)
                - json_payload: Send as JSON instead of formatted string (default: False)
        """
        super().__init__(name, config)
        
        self.broker = config.get('broker', 'localhost')
        self.port = config.get('port', 1883)
        self.username = config.get('username')
        self.password = config.get('password')
        self.topic = config.get('topic')
        self.qos = config.get('qos', 0)
        self.retain = config.get('retain', False)
        self.json_payload = config.get('json_payload', False)
        
        self._client = None
        self._connected = False
    
    def validate_config(self) -> bool:
        """
        Validate MQTT pathway configuration
        
        Returns:
            bool: True if configuration is valid
        """
        if mqtt is None:
            self.logger.error("paho-mqtt library not available")
            return False
        
        if not self.broker:
            self.logger.error("MQTT broker not specified")
            return False
        
        if not self.topic:
            self.logger.error("MQTT topic not specified")
            return False
        
        return True
    
    def send_alert(self, alert: Dict[str, Any], pathway_config: Dict[str, Any] = None) -> bool:
        """
        Publish alert to MQTT topic
        
        Args:
            alert: Alert dictionary
            
        Returns:
            bool: True if alert was published successfully
        """
        if not self.should_send_alert(alert):
            return False
        
        try:
            # Ensure we have a connection
            if not self._ensure_connection():
                return False
            
            # Prepare message
            if self.json_payload:
                # Send full alert as JSON
                payload = json.dumps(alert)
            else:
                # Send formatted message
                payload = self.format_message(alert)
            
            # Publish message
            result = self._client.publish(
                self.topic, 
                payload, 
                qos=self.qos, 
                retain=self.retain
            )
            
            if result.rc == mqtt.MQTT_ERR_SUCCESS:
                self.logger.debug(f"Published to {self.topic}: {payload}")
                self.log_send_attempt(alert, True)
                return True
            else:
                error_msg = f"MQTT publish failed with code {result.rc}"
                self.log_send_attempt(alert, False, error_msg)
                return False
                
        except Exception as e:
            error_msg = f"Error publishing to MQTT: {e}"
            self.log_send_attempt(alert, False, error_msg)
            return False
    
    def _ensure_connection(self) -> bool:
        """Ensure MQTT client is connected"""
        if self._connected and self._client:
            return True
        
        try:
            # Create client if needed
            if not self._client:
                client_id = f"mqtt-pathway-{self.name}-{int(time.time())}"
                self._client = mqtt.Client(
                    client_id=client_id,
                    callback_api_version=mqtt.CallbackAPIVersion.VERSION2
                )
                
                # Set credentials if provided
                if self.username:
                    self._client.username_pw_set(self.username, self.password)
                
                # Set callbacks
                self._client.on_connect = self._on_connect
                self._client.on_disconnect = self._on_disconnect
            
            # Connect to broker
            self._client.connect(self.broker, self.port, 60)
            self._client.loop_start()
            
            # Wait briefly for connection
            import time
            time.sleep(0.1)
            
            return self._connected
            
        except Exception as e:
            self.logger.error(f"Error connecting to MQTT broker {self.broker}:{self.port}: {e}")
            return False
    
    def _on_connect(self, client, userdata, flags, rc):
        """Callback for MQTT connection"""
        if rc == 0:
            self._connected = True
            self.logger.debug(f"Connected to MQTT broker {self.broker}:{self.port}")
        else:
            self._connected = False
            self.logger.error(f"Failed to connect to MQTT broker, return code {rc}")
    
    def _on_disconnect(self, client, userdata, rc):
        """Callback for MQTT disconnection"""
        self._connected = False
        if rc != 0:
            self.logger.warning(f"Unexpected disconnection from MQTT broker")
    
    def disconnect(self):
        """Disconnect from MQTT broker"""
        if self._client:
            try:
                self._client.loop_stop()
                self._client.disconnect()
                self._connected = False
            except Exception as e:
                self.logger.warning(f"Error disconnecting from MQTT: {e}")
    
    def __del__(self):
        """Cleanup when pathway is destroyed"""
        self.disconnect()