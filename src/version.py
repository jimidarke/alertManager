"""
Alert Manager Version Module

Provides centralized version information for all services.
Version is read from VERSION file at project root.
"""

import os
from pathlib import Path


def get_version() -> str:
    """
    Read version from VERSION file at project root.

    Returns:
        str: Version string (e.g., "1.1.0")
    """
    try:
        version_file = Path(__file__).parent.parent / "VERSION"
        with open(version_file, 'r') as f:
            return f.read().strip()
    except Exception as e:
        # Fallback version if file cannot be read
        return "unknown"


__version__ = get_version()
