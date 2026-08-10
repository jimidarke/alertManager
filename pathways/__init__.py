"""
Alert Manager Pathway System

This module provides modular pathway implementations for routing alerts
to different output channels (file, email, LED, audio, etc.).
"""

from .base_pathway import BasePathway
from .file_pathway import FilePathway
from .console_pathway import ConsolePathway
from .mqtt_pathway import MQTTPathway
from .led_sign_pathway import LEDSignPathway
from .pathway_manager import PathwayManager

# Import email pathway optionally
try:
    from .email_pathway import EmailPathway
    EMAIL_AVAILABLE = True
except ImportError as e:
    print(f"Warning: Email pathway not available: {e}")
    EmailPathway = None
    EMAIL_AVAILABLE = False

__all__ = [
    'BasePathway',
    'FilePathway',
    'ConsolePathway',
    'MQTTPathway',
    'LEDSignPathway',
    'PathwayManager'
]

if EMAIL_AVAILABLE:
    __all__.append('EmailPathway')