"""API Ninjas trivia feed handler"""

import os
import random
import requests
from typing import List, Dict, Any
from feeds.base_feed import BaseFeed
from utils.payload_builder import build_alert_payload


class TriviaFeed(BaseFeed):
    """Fetch trivia questions from API Ninjas"""

    def fetch(self) -> List[Dict[str, Any]]:
        """Fetch random trivia question from API Ninjas"""

        api_url = self.config['api_url']
        categories = self.config.get('categories', [])

        if not categories:
            raise ValueError("No trivia categories configured")

        # Get API key from environment
        api_key_env = self.config.get('api_key_env', 'API_NINJAS_KEY')
        api_key = os.environ.get(api_key_env)

        if not api_key:
            raise ValueError(f"API key not found in environment: {api_key_env}")

        # Select random category
        random_category = random.choice(categories)
        url_with_category = f"{api_url}?category={random_category}"

        headers = {'X-Api-Key': api_key}

        response = requests.get(url_with_category, headers=headers, timeout=10)
        response.raise_for_status()

        data = response.json()

        if not data:
            raise ValueError("No trivia found in API response")

        trivia_item = data[0]
        question = trivia_item.get('question', '')
        answer = trivia_item.get('answer', '')
        category = trivia_item.get('category', random_category)

        if not question or not answer:
            raise ValueError("Invalid trivia data: missing question or answer")

        # Format as "Q: ... A: ..." for LED sign display
        message = f"Q: {question} A: {answer}"

        # Build alert payload for trivia
        payload = build_alert_payload(
            level=self.config['level'],
            category=self.config['category'],
            title="Trivia Question",
            message=message,
            source=self.config['source'],
            client=self.client_name,
            metadata={
                "feed_type": "trivia",
                "trivia_category": category,
                "question": question,
                "answer": answer
            }
        )

        return [payload]
