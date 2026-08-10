#!/usr/bin/env python3
"""
Pathway Configuration Generator
Generates JSON configuration from simplified template input
"""

import json
import argparse
from typing import Dict, List

class PathwayConfigGenerator:
    
    def __init__(self):
        self.default_outputs = {
            'critical': ['led_sign', 'audio_alerts', 'light_indicators', 'mobile_push', 'console', 'file'],
            'warning': ['audio_alerts', 'console', 'file'],
            'info': ['console', 'file'],
            'debug': ['file']
        }
        
        self.alert_categories = {
            'security': ['door-open', 'window-open', 'motion-detected', 'security-alarm'],
            'environmental': ['weather-severe', 'temperature-extreme', 'humidity-high', 'air-quality'],
            'system': ['power-outage', 'network-down', 'device-offline', 'backup-failed'],
            'automation': ['schedule-reminder', 'task-complete', 'maintenance-due']
        }
    
    def generate_from_template(self, template_config: Dict) -> Dict:
        """Generate full configuration from simplified template"""
        
        config = {
            "mqtt": template_config.get('mqtt', {
                "broker": "mqtt-broker",
                "port": 1883,
                "use_tls": False,
                "input_topic": "alerts/input",
                "status_topic": "alerts/service/status"
            }),
            "pathway_matrix": {},
            "outputs": {},
            "time_restrictions": template_config.get('time_restrictions', {
                "quiet_hours": {
                    "enabled": True,
                    "start": "22:00",
                    "end": "07:00",
                    "volume_limit": 30,
                    "restricted_outputs": ["audio_alerts"],
                    "exceptions": ["critical"]
                }
            })
        }
        
        # Generate pathway matrix
        config['pathway_matrix'] = self.generate_pathway_matrix(template_config)
        
        # Generate output configurations
        config['outputs'] = self.generate_output_configs(template_config)
        
        return config
    
    def generate_pathway_matrix(self, template_config: Dict) -> Dict:
        """Generate pathway matrix from template rules"""
        
        pathway_matrix = {}
        
        # Get template rules or use defaults
        template_rules = template_config.get('pathway_rules', {})
        
        for category, alert_types in self.alert_categories.items():
            pathway_matrix[category] = {}
            
            for alert_type in alert_types:
                pathway_matrix[category][alert_type] = {}
                
                # Check if there are custom rules for this alert type
                custom_rules = template_rules.get(alert_type, {})
                
                for level in ['critical', 'warning', 'info', 'debug']:
                    if level in custom_rules:
                        # Use custom rule
                        pathway_matrix[category][alert_type][level] = custom_rules[level]
                    else:
                        # Use category default or global default
                        category_defaults = template_rules.get(f'{category}_defaults', {})
                        if level in category_defaults:
                            pathway_matrix[category][alert_type][level] = category_defaults[level]
                        else:
                            pathway_matrix[category][alert_type][level] = self.default_outputs[level].copy()
        
        # Add fallback rules
        pathway_matrix['fallback'] = template_rules.get('fallback', self.default_outputs)
        
        return pathway_matrix
    
    def generate_output_configs(self, template_config: Dict) -> Dict:
        """Generate output configurations from template"""
        
        outputs = {}
        
        # Console output
        outputs['console'] = {
            "enabled": True,
            "format": "{timestamp} [{level}] {title} - {message}",
            "color_coding": True
        }
        
        # File output
        outputs['file'] = {
            "enabled": True,
            "path": "/var/log/alerts.log",
            "format": "{timestamp} [{level}] [{type}] {source}: {title} - {message}",
            "rotation": {
                "enabled": True,
                "max_size": "10MB",
                "backup_count": 5
            }
        }
        
        # LED sign output
        led_config = template_config.get('led_sign', {})
        outputs['led_sign'] = {
            "enabled": led_config.get('enabled', True),
            "topic": led_config.get('topic', 'ledSign/main/message'),
            "format": "[{level}] {title}: {message}",
            "qos": 0,
            "retain": False
        }
        
        # Audio alerts
        audio_config = template_config.get('audio_alerts', {})
        if audio_config.get('enabled', False):
            outputs['audio_alerts'] = {
                "enabled": True,
                "zones": audio_config.get('zones', {
                    "living_room": {
                        "topic": "alerts/audio/living_room/command",
                        "type": "tts",
                        "volume_mapping": {"critical": 80, "warning": 60, "info": 40, "debug": 20}
                    }
                }),
                "tts_settings": audio_config.get('tts_settings', {
                    "voice": "en+f3",
                    "speed": 150,
                    "language": "en-US"
                })
            }
        
        # Light indicators
        light_config = template_config.get('light_indicators', {})
        if light_config.get('enabled', False):
            outputs['light_indicators'] = {
                "enabled": True,
                "zones": light_config.get('zones', {
                    "main_strip": {
                        "topic": "alerts/lights/main/command",
                        "type": "ws2812",
                        "color_mapping": {
                            "critical": "#FF0000",
                            "warning": "#FFA500", 
                            "info": "#0000FF",
                            "debug": "#808080"
                        },
                        "effects": {
                            "critical": "flash",
                            "warning": "pulse",
                            "info": "fade",
                            "debug": "solid"
                        }
                    }
                })
            }
        
        # Mobile push
        mobile_config = template_config.get('mobile_push', {})
        if mobile_config.get('enabled', False):
            outputs['mobile_push'] = {
                "enabled": True,
                "service": mobile_config.get('service', 'pushover'),
                "api_key": mobile_config.get('api_key', 'YOUR_API_KEY'),
                "user_key": mobile_config.get('user_key', 'YOUR_USER_KEY'),
                "priority_mapping": {
                    "critical": 2,
                    "warning": 1,
                    "info": 0,
                    "debug": -1
                }
            }
        
        # Email
        email_config = template_config.get('email', {})
        if email_config.get('enabled', False):
            outputs['email'] = {
                "enabled": True,
                "smtp_server": email_config.get('smtp_server', 'smtp.gmail.com'),
                "smtp_port": email_config.get('smtp_port', 587),
                "username": email_config.get('username', 'your_email@gmail.com'),
                "password": email_config.get('password', 'your_password'),
                "from_address": email_config.get('from_address', 'your_email@gmail.com'),
                "to_addresses": email_config.get('to_addresses', ['recipient@example.com']),
                "subject_template": "Home Alert: {title}"
            }
        
        # SMS
        sms_config = template_config.get('sms', {})
        if sms_config.get('enabled', False):
            outputs['sms'] = {
                "enabled": True,
                "service": sms_config.get('service', 'twilio'),
                "account_sid": sms_config.get('account_sid', 'YOUR_ACCOUNT_SID'),
                "auth_token": sms_config.get('auth_token', 'YOUR_AUTH_TOKEN'),
                "from_number": sms_config.get('from_number', '+1234567890'),
                "to_numbers": sms_config.get('to_numbers', ['+1987654321'])
            }
        
        return outputs

def create_basic_template() -> Dict:
    """Create a basic template for customization"""
    
    return {
        "mqtt": {
            "broker": "192.168.1.40",
            "port": 1883,
            "input_topic": "alerts/input",
            "status_topic": "alerts/service/status"
        },
        "pathway_rules": {
            # Security category defaults - more aggressive alerting
            "security_defaults": {
                "critical": ["led_sign", "audio_alerts", "light_indicators", "mobile_push", "sms", "console", "file"],
                "warning": ["led_sign", "audio_alerts", "light_indicators", "console", "file"],
                "info": ["console", "file"],
                "debug": ["file"]
            },
            # Environmental category defaults - weather alerts need immediate attention
            "environmental_defaults": {
                "critical": ["led_sign", "audio_alerts", "mobile_push", "console", "file"],
                "warning": ["audio_alerts", "console", "file"],
                "info": ["console", "file"],
                "debug": ["file"]
            },
            # System category defaults - technical issues
            "system_defaults": {
                "critical": ["led_sign", "mobile_push", "email", "console", "file"],
                "warning": ["mobile_push", "console", "file"],
                "info": ["console", "file"],
                "debug": ["file"]
            },
            # Automation category defaults - scheduled events and reminders
            "automation_defaults": {
                "critical": ["audio_alerts", "mobile_push", "console", "file"],
                "warning": ["audio_alerts", "console", "file"],
                "info": ["audio_alerts", "console", "file"],
                "debug": ["console", "file"]
            },
            # Custom rules for specific alert types
            "door-open": {
                "critical": ["led_sign", "audio_alerts", "light_indicators", "mobile_push", "sms", "console", "file"],
                "warning": ["led_sign", "audio_alerts", "console", "file"]
            },
            "power-outage": {
                "critical": ["mobile_push", "sms", "console", "file"],  # No local alerts when power is out
                "warning": ["mobile_push", "console", "file"]
            },
            "schedule-reminder": {
                "info": ["audio_alerts", "console", "file"],  # Always use audio for reminders
                "critical": ["audio_alerts", "mobile_push", "console", "file"]
            },
            # Fallback for unknown alert types
            "fallback": {
                "critical": ["led_sign", "audio_alerts", "mobile_push", "console", "file"],
                "warning": ["audio_alerts", "console", "file"],
                "info": ["console", "file"],
                "debug": ["file"]
            }
        },
        "led_sign": {
            "enabled": True,
            "topic": "ledSign/main/message"
        },
        "audio_alerts": {
            "enabled": True,
            "zones": {
                "kitchen": {
                    "topic": "alerts/audio/kitchen/command",
                    "type": "tts",
                    "volume_mapping": {"critical": 80, "warning": 60, "info": 40, "debug": 20}
                },
                "living_room": {
                    "topic": "alerts/audio/living_room/command",
                    "type": "tts",
                    "volume_mapping": {"critical": 80, "warning": 60, "info": 40, "debug": 20}
                },
                "bedroom": {
                    "topic": "alerts/audio/bedroom/command",
                    "type": "chime",  # Use chimes in bedroom to be less intrusive
                    "volume_mapping": {"critical": 60, "warning": 40, "info": 25, "debug": 10}
                }
            },
            "tts_settings": {
                "voice": "en+f3",
                "speed": 150,
                "language": "en-US"
            }
        },
        "light_indicators": {
            "enabled": True,
            "zones": {
                "main_strip": {
                    "topic": "alerts/lights/main/command",
                    "type": "ws2812",
                    "color_mapping": {
                        "critical": "#FF0000",
                        "warning": "#FFA500",
                        "info": "#0000FF", 
                        "debug": "#808080"
                    },
                    "effects": {
                        "critical": "flash",
                        "warning": "pulse",
                        "info": "fade",
                        "debug": "solid"
                    }
                }
            }
        },
        "mobile_push": {
            "enabled": False,  # Set to true and configure with your credentials
            "service": "pushover",
            "api_key": "YOUR_PUSHOVER_API_KEY",
            "user_key": "YOUR_PUSHOVER_USER_KEY"
        },
        "email": {
            "enabled": False,  # Set to true and configure with your credentials
            "smtp_server": "smtp.gmail.com",
            "smtp_port": 587,
            "username": "your_email@gmail.com",
            "password": "your_app_password",
            "from_address": "your_email@gmail.com",
            "to_addresses": ["recipient@example.com"]
        },
        "sms": {
            "enabled": False,  # Set to true and configure with your credentials
            "service": "twilio",
            "account_sid": "YOUR_TWILIO_ACCOUNT_SID",
            "auth_token": "YOUR_TWILIO_AUTH_TOKEN",
            "from_number": "+1234567890",
            "to_numbers": ["+1987654321"]
        },
        "time_restrictions": {
            "quiet_hours": {
                "enabled": True,
                "start": "22:00",
                "end": "07:00",
                "volume_limit": 30,
                "restricted_outputs": ["audio_alerts"],
                "exceptions": ["critical"]
            }
        }
    }

def main():
    parser = argparse.ArgumentParser(description='Generate pathway configuration from template')
    parser.add_argument('--input', '-i', help='Input template file (JSON)')
    parser.add_argument('--output', '-o', help='Output configuration file', default='generated_config.json')
    parser.add_argument('--template', '-t', action='store_true', help='Generate basic template file')
    
    args = parser.parse_args()
    
    generator = PathwayConfigGenerator()
    
    if args.template:
        # Generate basic template
        template = create_basic_template()
        template_file = args.output.replace('.json', '_template.json')
        
        with open(template_file, 'w') as f:
            json.dump(template, f, indent=2)
        
        print(f"Basic template generated: {template_file}")
        print("Edit this template file and then run:")
        print(f"python {__file__} -i {template_file} -o {args.output}")
        return
    
    if not args.input:
        print("Error: Input template file required. Use --template to generate a basic template first.")
        return
    
    try:
        # Load template
        with open(args.input, 'r') as f:
            template_config = json.load(f)
        
        # Generate full configuration
        full_config = generator.generate_from_template(template_config)
        
        # Write output
        with open(args.output, 'w') as f:
            json.dump(full_config, f, indent=2)
        
        print(f"Configuration generated: {args.output}")
        print(f"Generated {len(full_config['pathway_matrix'])} pathway categories")
        print(f"Generated {len(full_config['outputs'])} output configurations")
        
    except FileNotFoundError:
        print(f"Error: Template file '{args.input}' not found")
    except json.JSONDecodeError as e:
        print(f"Error: Invalid JSON in template file: {e}")
    except Exception as e:
        print(f"Error: {e}")

if __name__ == "__main__":
    main()