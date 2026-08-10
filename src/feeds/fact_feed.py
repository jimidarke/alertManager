"""API Ninjas facts feed handler"""

import os
import time
import requests
from typing import List, Dict, Any
from feeds.base_feed import BaseFeed
from utils.payload_builder import build_alert_payload


class FactFeed(BaseFeed):
    """Fetch random facts from API Ninjas"""

    def fetch(self) -> List[Dict[str, Any]]:
        """Fetch facts from API Ninjas (one at a time with delays)"""

        api_url = self.config['api_url']
        num_items = self.config['num_items']
        delay_seconds = self.config.get('request_delay_seconds', 2)

        # Get API key from environment
        api_key_env = self.config.get('api_key_env', 'API_NINJAS_KEY')
        api_key = os.environ.get(api_key_env)

        if not api_key:
            raise ValueError(f"API key not found in environment: {api_key_env}")

        headers = {'X-Api-Key': api_key}
        items = []

        # Fetch facts one at a time with delay
        for idx in range(1, num_items + 1):
            response = requests.get(api_url, headers=headers, timeout=10)
            response.raise_for_status()

            data = response.json()

            if not data or 'fact' not in data[0]:
                continue

            fact_text = data[0]['fact']

            # Build alert payload for each fact
            payload = build_alert_payload(
                level=self.config['level'],
                category=self.config['category'],
                title="Fact",
                message=fact_text,
                source=self.config['source'],
                client=self.client_name,
                metadata={
                    "feed_type": "fact",
                    "fact_index": idx,
                    "total_facts": num_items
                }
            )

            items.append(payload)

            # Delay between requests (except after last one)
            if idx < num_items:
                time.sleep(delay_seconds)

        if not items:
            raise ValueError("No facts found in API responses")

        return items
