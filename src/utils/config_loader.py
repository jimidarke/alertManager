"""Configuration loader for feed aggregator"""

import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)


def load_config(config_path: str) -> dict:
    """Load and validate configuration file"""

    path = Path(config_path)
    if not path.exists():
        raise FileNotFoundError(f"Configuration file not found: {config_path}")

    with open(path, 'r') as f:
        config = json.load(f)

    # Validate required sections
    if 'feed_aggregator' not in config:
        raise ValueError("Configuration missing 'feed_aggregator' section")

    feed_config = config['feed_aggregator']

    required_keys = ['enabled', 'mqtt', 'client_name', 'feeds']
    for key in required_keys:
        if key not in feed_config:
            raise ValueError(f"feed_aggregator config missing required key: {key}")

    if not feed_config['enabled']:
        logger.warning("Feed aggregator is disabled in configuration")

    logger.info(f"Configuration loaded from {config_path}")
    return config
