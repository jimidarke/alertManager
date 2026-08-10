"""Joke API feed handler"""

import requests
from typing import List, Dict, Any
from feeds.base_feed import BaseFeed
from utils.payload_builder import build_alert_payload


class JokeFeed(BaseFeed):
    """Fetch jokes from JokeAPI"""

    def fetch(self) -> List[Dict[str, Any]]:
        """Fetch jokes from JokeAPI"""

        api_url = self.config['api_url']
        num_items = self.config['num_items']

        # Request multiple jokes at once
        url_with_amount = f"{api_url}&amount={num_items}"

        response = requests.get(url_with_amount, timeout=10)
        response.raise_for_status()

        data = response.json()

        if 'jokes' not in data:
            raise ValueError("No jokes found in API response")

        items = []

        for idx, joke in enumerate(data['jokes'], start=1):
            # Handle both single-part and two-part jokes
            if 'joke' in joke:
                joke_text = joke['joke'].replace('\n', ' ')
            elif 'setup' in joke and 'delivery' in joke:
                joke_text = f"{joke['setup']} {joke['delivery']}"
            else:
                continue

            # Build alert payload for each joke
            payload = build_alert_payload(
                level=self.config['level'],
                category=self.config['category'],
                title="Joke",
                message=joke_text,
                source=self.config['source'],
                client=self.client_name,
                metadata={
                    "feed_type": "joke",
                    "joke_index": idx,
                    "joke_category": joke.get('category', 'unknown')
                }
            )

            items.append(payload)

        if not items:
            raise ValueError("No valid jokes found in API response")

        return items
