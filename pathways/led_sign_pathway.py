"""
LED Sign Pathway

Handles routing alerts to alphanumeric LED signs (like BetaBrite displays) with intelligent 
visual effects, zone management, and context-aware display configurations using the Alpha Protocol.

Note: This pathway is specifically for text-based LED signs, NOT for RGB LED strips or light 
controllers. For colored lighting alerts, use a separate LED strip/light pathway.
"""

import json
import logging
import time
from typing import Dict, Any, List, Optional
from datetime import datetime, timedelta
from .base_pathway import BasePathway

try:
    import paho.mqtt.client as mqtt
except ImportError:
    mqtt = None


class LEDSignPathway(BasePathway):
    """BetaBrite LED sign pathway for text-based alphanumeric displays with rich visual effects and zone management"""
    
    # BetaBrite Alpha Protocol Code Mappings
    DISPLAY_MODES = {
        'rotate': 'a',           # Cycles through text files
        'hold': 'b',             # Static display
        'flash': 'c',            # Text blinks on/off
        'scroll': 'm',           # Text scrolls horizontally
        'auto': 'o',             # Sign determines best method
        'rollup': 'e',           # Text rolls up from bottom
        'rolldown': 'f',         # Text rolls down from top
        'rollleft': 'g',         # Text rolls in from right
        'rollright': 'h',        # Text rolls in from left
        'rollin': 'p',           # Text rolls in from edges
        'rollout': 'q',          # Text rolls out to edges
        'wipeup': 'i',           # Text wipes upward
        'wipedown': 'j',         # Text wipes downward
        'wipeleft': 'k',         # Text wipes leftward
        'wiperight': 'l',        # Text wipes rightward
        'wipein': 'r',           # Text wipes in from edges
        'wipeout': 's',          # Text wipes out to edges
        'compressedrotate': 't', # Compact rotation
        'explode': 'u',          # Text explodes outward
        'clock': 'v',            # Time/date display
        'special': 'n',          # Enables special effects
        'newsflash': 'A'         # Breaking news style (special effect)
    }
    
    SPECIAL_EFFECTS = {
        'twinkle': '0',          # Stars twinkling effect
        'sparkle': '1',          # Sparkling lights effect
        'snow': '2',             # Falling snow effect
        'interlock': '3',        # Interlocking pattern
        'switch': '4',           # Switching pattern
        'slide': '5',            # Sliding motion
        'spray': '6',            # Water spray effect
        'starburst': '7',        # Explosive star effect
        'welcome': '8',          # Welcome message effect
        'slots': '9',            # Slot machine effect
        'newsflash': 'A',        # Breaking news style
        'trumpet': 'B',          # Trumpet fanfare
        'cyclecolors': 'C',      # Rotates through colors
        'thankyou': 'S',         # Thank you message
        'nosmoke': 'U',          # No smoking symbol
        'nodrive': 'V',          # Don't drink & drive
        'fishimal': 'W',         # Fish animation
        'fireworks': 'X',        # Fireworks explosion
        'turballoon': 'Y',       # Turbulent balloon
        'bomb': 'Z'              # Explosion effect
    }
    
    COLORS = {
        'red': '1',              # Bright red
        'green': '2',            # Bright green
        'amber': '3',            # Amber/orange
        'dimred': '4',           # Darker red
        'dimgreen': '5',         # Darker green
        'brown': '6',            # Brown/dark amber
        'orange': '7',           # Orange
        'yellow': '8',           # Bright yellow
        'rainbow1': '9',         # First rainbow pattern
        'rainbow2': 'A',         # Second rainbow pattern
        'colormix': 'B',         # Mixed color pattern
        'autocolor': 'C'         # Automatic selection
    }
    
    CHARACTER_SETS = {
        '5high': '1',            # Basic 5-pixel height
        '5stroke': '2',          # 5-pixel stroke font
        '7high': '3',            # Standard 7-pixel height
        '7stroke': '4',          # 7-pixel stroke font
        '7highfancy': '5',       # Decorative 7-pixel
        '10high': '6',           # Large 10-pixel height
        '7shadow': '7',          # Shadow effect
        'fhighfancy': '8',       # Full height decorative
        'fhigh': '9',            # Full height standard
        '7shadowfancy': ':',     # Shadow with decoration
        '5wide': ';',            # 5-pixel wide
        '7wide': '<',            # 7-pixel wide
        '7widefancy': '=',       # Decorative wide
        '5widestroke': '>'       # 5-pixel wide outline
    }
    
    POSITIONS = {
        'topline': '"',          # Upper portion of display
        'midline': ' ',          # Center of display (space)
        'botline': '&',          # Lower portion of display
        'fill': '0',             # Full display area
        'left': '1',             # Left-aligned
        'right': '2'             # Right-aligned
    }
    
    SPEEDS = {
        1: '\\025',              # Slowest
        2: '\\026',              # Slow
        3: '\\027',              # Medium
        4: '\\030',              # Fast
        5: '\\031'               # Fastest
    }
    
    def __init__(self, name: str, config: Dict[str, Any]):
        """
        Initialize LED sign pathway
        
        Args:
            name: Pathway instance name
            config: Configuration dictionary containing:
                - zones: Dictionary of LED sign zone configurations
                - default_zone: Default LED sign zone name (default: 'main')
                - broker: MQTT broker address
                - port: MQTT broker port (default: 1883)
                - username: MQTT username (optional)
                - password: MQTT password (optional)
                - base_topic: Base MQTT topic pattern (default: 'ledSign/{zone}/')
                - effect_mappings: Custom text display effect mappings (optional)
                - category_styles: Category-specific text styling (optional)
                - persistence_rules: Display duration and priority rules (optional)
        """
        super().__init__(name, config)
        
        self.zones = config.get('zones', {'main': {}})
        self.default_zone = config.get('default_zone', 'main')
        self.broker = config.get('broker', 'localhost')
        self.port = config.get('port', 1883)
        self.username = config.get('username')
        self.password = config.get('password')
        self.base_topic = config.get('base_topic', 'ledSign/{zone}/')
        
        # Visual effect mappings
        self.effect_mappings = self._build_effect_mappings(config.get('effect_mappings', {}))
        self.category_styles = self._build_category_styles(config.get('category_styles', {}))
        self.persistence_rules = config.get('persistence_rules', {})
        
        # MQTT client
        self._client = None
        self._connected = False
    
    def validate_config(self) -> bool:
        """Validate LED sign pathway configuration"""
        if mqtt is None:
            self.logger.error("paho-mqtt library not available")
            return False
        
        if not self.zones:
            self.logger.error("No LED sign zones configured")
            return False
        
        if self.default_zone not in self.zones:
            self.logger.warning(f"Default LED sign zone '{self.default_zone}' not in configured zones")
        
        return True
    
    def send_alert(self, alert: Dict[str, Any], pathway_config: Dict[str, Any] = None) -> bool:
        """Send alert to LED signs"""
        if not self.should_send_alert(alert):
            return False

        if pathway_config is None:
            pathway_config = {}

        try:
            # Ensure MQTT connection
            if not self._ensure_connection():
                return False

            # Determine target zones from pathway config first, fallback to alert metadata
            target_zones = self._get_target_zones(alert, pathway_config)
            
            success_count = 0
            for zone_name in target_zones:
                if self._send_to_zone(zone_name, alert, pathway_config):
                    success_count += 1
            
            success = success_count > 0
            self.log_send_attempt(alert, success, 
                f"Sent to {success_count}/{len(target_zones)} zones" if not success else None)
            
            return success
            
        except Exception as e:
            error_msg = f"Error sending to LED signs: {e}"
            self.log_send_attempt(alert, False, error_msg)
            return False
    
    def _get_target_zones(self, alert: Dict[str, Any], pathway_config: Dict[str, Any] = None) -> List[str]:
        """Determine which zones should display this alert"""
        if pathway_config is None:
            pathway_config = {}

        # Check for zone override in pathway config first
        if 'zones' in pathway_config:
            zones = pathway_config['zones']
            if isinstance(zones, str):
                return [zones]
            elif isinstance(zones, list):
                return zones

        # Use zone filters from configuration
        target_zones = []
        for zone_name, zone_config in self.zones.items():
            if self._should_send_to_zone(zone_name, zone_config, alert):
                target_zones.append(zone_name)

        # Fallback to default zone if no zones matched
        if not target_zones and self.default_zone in self.zones:
            target_zones = [self.default_zone]
        
        return target_zones
    
    def _should_send_to_zone(self, zone_name: str, zone_config: Dict[str, Any], alert: Dict[str, Any]) -> bool:
        """Check if alert should be sent to specific zone"""
        # Check zone-specific filters
        level_filter = zone_config.get('level_filter')
        if level_filter and alert.get('level') not in level_filter:
            return False
        
        category_filter = zone_config.get('category_filter')
        if category_filter and alert.get('category') not in category_filter:
            return False
        
        source_filter = zone_config.get('source_filter')
        if source_filter and alert.get('source') not in source_filter:
            return False
        
        return True
    
    def _send_to_zone(self, zone_name: str, alert: Dict[str, Any], pathway_config: Dict[str, Any] = None) -> bool:
        """Send alert to specific LED zone"""
        if pathway_config is None:
            pathway_config = {}

        try:
            zone_config = self.zones.get(zone_name, {})

            # Build display configuration using pathway config
            display_config = self._build_display_config(alert, zone_config, pathway_config)
            
            # Create LED sign message with BetaBrite display configuration
            led_sign_message = {
                'timestamp': alert.get('timestamp'),
                'level': alert.get('level'),
                'category': alert.get('category'),
                'title': alert.get('title'),
                'message': self._format_message_for_led_sign(alert, zone_config),
                'display_config': display_config,
                'source': alert.get('source'),
                'zone': zone_name
            }
            
            # Publish to LED sign zone topic
            topic = self.base_topic.format(zone=zone_name) + 'message'
            payload = json.dumps(led_sign_message)
            
            result = self._client.publish(topic, payload, qos=1)

            if result.rc == mqtt.MQTT_ERR_SUCCESS:
                return True
            else:
                self.logger.error(f"Failed to publish LED sign alert to zone '{zone_name}', MQTT error: {result.rc}")
                return False
                
        except Exception as e:
            self.logger.error(f"Error sending to LED sign zone '{zone_name}': {e}")
            return False
    
    def _build_display_config(self, alert: Dict[str, Any], zone_config: Dict[str, Any], pathway_config: Dict[str, Any] = None) -> Dict[str, Any]:
        """Build BetaBrite display configuration based on alert"""
        if pathway_config is None:
            pathway_config = {}

        level = alert.get('level', 'info')
        category = alert.get('category', 'unknown')

        # Start with base effect mapping for level
        config = self.effect_mappings.get(level, self.effect_mappings['info']).copy()

        # Apply category-specific overrides
        category_style = self.category_styles.get(category, {})
        config.update(category_style)

        # Apply zone-specific overrides
        zone_overrides = zone_config.get('display_overrides', {})
        config.update(zone_overrides)

        # Apply pathway-specific display config (new priority structure)
        if 'display_config' in pathway_config:
            config.update(pathway_config['display_config'])
        
        # Add persistence and priority logic
        self._apply_persistence_rules(config, alert)
        
        # Convert config names to BetaBrite codes
        betabrite_config = self._convert_to_betabrite_codes(config)
        
        return betabrite_config
    
    def _convert_to_betabrite_codes(self, config: Dict[str, Any]) -> Dict[str, Any]:
        """Convert human-readable config to BetaBrite protocol codes"""
        betabrite_config = config.copy()
        
        # Convert mode
        if 'mode' in config:
            mode_name = config['mode'].lower()
            betabrite_config['mode_code'] = self.DISPLAY_MODES.get(mode_name, 'a')
        
        # Convert special effect
        if 'special_effect' in config and config['special_effect']:
            effect_name = config['special_effect'].lower()
            betabrite_config['effect_code'] = self.SPECIAL_EFFECTS.get(effect_name, '0')
        
        # Convert color
        if 'color' in config:
            color_name = config['color'].lower()
            betabrite_config['color_code'] = self.COLORS.get(color_name, '2')
        
        # Convert character set
        if 'character_set' in config:
            charset_name = config['character_set'].lower()
            betabrite_config['charset_code'] = self.CHARACTER_SETS.get(charset_name, '3')
        
        # Convert position
        if 'position' in config:
            position_name = config['position'].lower()
            betabrite_config['position_code'] = self.POSITIONS.get(position_name, ' ')
        
        # Convert speed
        if 'speed' in config:
            speed_num = config['speed']
            betabrite_config['speed_code'] = self.SPEEDS.get(speed_num, '\\027')
        
        return betabrite_config
    
    def _apply_persistence_rules(self, config: Dict[str, Any], alert: Dict[str, Any]):
        """Apply persistence and priority rules to display config"""
        level = alert.get('level', 'info')
        category = alert.get('category', 'unknown')
        
        # Base duration mapping (seconds)
        duration_map = {
            'critical': 60,  # 1 minute
            'warning': 30,   # 30 seconds
            'info': 15,      # 15 seconds
            'debug': 10      # 10 seconds
        }
        
        # Priority mapping (interrupts normal rotation)
        priority_map = {
            'critical': True,
            'warning': False,
            'info': False,
            'debug': False
        }
        
        # Category-specific adjustments
        category_adjustments = {
            'security': {'duration_multiplier': 2.0, 'force_priority': True},
            'news': {'duration_multiplier': 1.5},
            'weather': {'duration_multiplier': 1.2},
            'automation': {'duration_multiplier': 0.8},
            'system': {'duration_multiplier': 1.0},
            'personal': {'duration_multiplier': 0.7},
            'network': {'duration_multiplier': 1.1},
            'application': {'duration_multiplier': 0.9}
        }
        
        # Apply base rules
        base_duration = duration_map.get(level, 15)
        config.setdefault('duration', base_duration)
        config.setdefault('priority', priority_map.get(level, False))
        
        # Apply category adjustments
        if category in category_adjustments:
            adj = category_adjustments[category]
            
            if 'duration_multiplier' in adj:
                config['duration'] = int(config['duration'] * adj['duration_multiplier'])
            
            if adj.get('force_priority'):
                config['priority'] = True
        
        # Apply custom persistence rules from config
        custom_rules = self.persistence_rules.get(f"{level}_{category}") or self.persistence_rules.get(level)
        if custom_rules:
            config.update(custom_rules)
        
        # Handle special cases for news categories with context awareness
        if category == 'news':
            if level == 'info':
                # Light-hearted content (jokes, fun news) gets playful effects
                config.update({
                    'mode': 'rollright',
                    'special_effect': 'twinkle',
                    'color': 'rainbow1',
                    'character_set': '7highfancy',
                    'speed': 2,
                    'duration': 20
                })
            elif level == 'critical':
                # End-of-world type news gets maximum dramatic impact
                config.update({
                    'mode': 'explode',
                    'special_effect': 'bomb',
                    'color': 'red',
                    'character_set': '10high',
                    'position': 'fill',
                    'speed': 5,
                    'priority': True,
                    'duration': 90
                })
            elif level == 'warning':
                # Important news gets newsflash treatment
                config.update({
                    'mode': 'special',
                    'special_effect': 'newsflash',
                    'color': 'amber',
                    'character_set': '7high',
                    'speed': 4,
                    'duration': 45
                })
    
    def _format_message_for_led_sign(self, alert: Dict[str, Any], zone_config: Dict[str, Any]) -> str:
        """Format message text for LED sign display"""
        # Use zone-specific format if available
        led_sign_format = zone_config.get('format', '{title}: {message}')

        # Create sanitized copy for LED display (LED signs don't support newlines)
        alert_sanitized = alert.copy()
        for field in ['title', 'message']:
            if field in alert_sanitized and isinstance(alert_sanitized[field], str):
                # Use space instead of ↵ symbol for LED compatibility
                alert_sanitized[field] = self.sanitize_for_single_line(
                    alert_sanitized[field],
                    max_length=None,  # Length limiting happens below
                    newline_replacement=' '
                )

        # Apply format
        formatted = self.format_message(alert_sanitized, led_sign_format)

        # Apply length limits for LED sign displays
        max_length = zone_config.get('max_message_length', 200)
        if len(formatted) > max_length:
            formatted = formatted[:max_length-3] + '...'

        return formatted
    
    def _build_effect_mappings(self, custom_mappings: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
        """Build comprehensive effect mappings for alert levels using BetaBrite documentation"""
        default_mappings = {
            'critical': {
                'position': 'fill',
                'mode': 'flash',
                'special_effect': 'starburst',
                'color': 'red',
                'character_set': '10high',
                'speed': 5,
                'priority': True,
                'duration': 60
            },
            'warning': {
                'position': 'topline',
                'mode': 'special',
                'special_effect': 'sparkle',
                'color': 'amber',
                'character_set': '7high',
                'speed': 3,
                'priority': False,
                'duration': 30
            },
            'info': {
                'position': 'midline',
                'mode': 'rotate',
                'special_effect': 'twinkle',
                'color': 'green',
                'character_set': '7high',
                'speed': 2,
                'priority': False,
                'duration': 15
            },
            'debug': {
                'position': 'botline',
                'mode': 'hold',
                'special_effect': None,
                'color': 'dimgreen',
                'character_set': '5high',
                'speed': 1,
                'priority': False,
                'duration': 10
            }
        }
        
        # Merge with custom mappings
        for level, mapping in custom_mappings.items():
            if level in default_mappings:
                default_mappings[level].update(mapping)
            else:
                default_mappings[level] = mapping
        
        return default_mappings
    
    def _build_category_styles(self, custom_styles: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
        """Build category-specific visual styling using BetaBrite capabilities"""
        default_styles = {
            'security': {
                'mode': 'special',
                'special_effect': 'starburst',
                'color': 'red',
                'character_set': '7high',
                'speed': 5
            },
            'system': {
                'mode': 'compressedrotate',
                'special_effect': 'interlock',
                'color': 'amber',
                'character_set': '7high',
                'speed': 3
            },
            'network': {
                'mode': 'scroll',
                'special_effect': 'slide',
                'color': 'yellow',
                'character_set': '7high',
                'speed': 3
            },
            'weather': {
                'mode': 'scroll',
                'special_effect': 'snow',  # Default - can be overridden based on weather type
                'color': 'autocolor',
                'character_set': '7high',
                'speed': 2
            },
            'automation': {
                'mode': 'wipein',
                'special_effect': 'welcome',
                'color': 'green',
                'character_set': '5high',
                'speed': 2
            },
            'personal': {
                'mode': 'rollin',
                'special_effect': 'twinkle',
                'color': 'rainbow1',
                'character_set': '7highfancy',
                'speed': 2
            },
            'news': {
                'mode': 'scroll',
                'special_effect': 'sparkle',
                'color': 'autocolor',
                'character_set': '7high',
                'speed': 3
            },
            'application': {
                'mode': 'hold',
                'special_effect': None,
                'color': 'green',
                'character_set': '5high',
                'speed': 2
            }
        }
        
        # Merge with custom styles
        for category, style in custom_styles.items():
            if category in default_styles:
                default_styles[category].update(style)
            else:
                default_styles[category] = style
        
        return default_styles
    
    def _ensure_connection(self) -> bool:
        """Ensure MQTT client is connected"""
        if self._connected and self._client:
            return True
        
        try:
            if not self._client:
                import socket
                hostname = socket.gethostname()
                client_id = f"led-sign-{self.name}-{hostname}-{int(time.time())}"
                self._client = mqtt.Client(
                    client_id=client_id,
                    callback_api_version=mqtt.CallbackAPIVersion.VERSION2
                )
                
                if self.username:
                    self._client.username_pw_set(self.username, self.password)
                
                self._client.on_connect = self._on_connect
                self._client.on_disconnect = self._on_disconnect
            
            self._client.connect(self.broker, self.port, 60)
            self._client.loop_start()

            # Wait briefly for connection
            time.sleep(0.1)

            return self._connected
            
        except Exception as e:
            self.logger.error(f"Error connecting to MQTT broker {self.broker}:{self.port}: {e}")
            return False
    
    def _on_connect(self, client, userdata, flags, rc, properties=None):
        """MQTT connection callback (API v2 compatible)"""
        if rc == 0:
            self._connected = True
        else:
            self._connected = False
            self.logger.error(f"LED sign pathway failed to connect to MQTT broker, return code {rc}")

    def _on_disconnect(self, client, userdata, flags, rc, properties=None):
        """MQTT disconnection callback (API v2 compatible)"""
        self._connected = False
        if rc != 0:
            self.logger.warning(f"LED sign pathway unexpected disconnection from MQTT broker")
    
    def disconnect(self):
        """Disconnect from MQTT broker"""
        if self._client:
            try:
                self._client.loop_stop()
                self._client.disconnect()
                self._connected = False
            except Exception as e:
                self.logger.warning(f"Error disconnecting LED sign pathway from MQTT: {e}")
    
    def get_zone_status(self) -> Dict[str, Dict[str, Any]]:
        """Get status of all configured zones"""
        status = {}
        for zone_name, zone_config in self.zones.items():
            status[zone_name] = {
                'enabled': zone_config.get('enabled', True),
                'level_filter': zone_config.get('level_filter'),
                'category_filter': zone_config.get('category_filter'),
                'topic': self.base_topic.format(zone=zone_name) + 'message'
            }
        return status
    
    def get_betabrite_capabilities(self) -> Dict[str, List[str]]:
        """Get available BetaBrite capabilities for configuration reference"""
        return {
            'display_modes': list(self.DISPLAY_MODES.keys()),
            'special_effects': list(self.SPECIAL_EFFECTS.keys()),
            'colors': list(self.COLORS.keys()),
            'character_sets': list(self.CHARACTER_SETS.keys()),
            'positions': list(self.POSITIONS.keys()),
            'speeds': list(self.SPEEDS.keys())
        }
    
    def __del__(self):
        """Cleanup when pathway is destroyed"""
        self.disconnect()