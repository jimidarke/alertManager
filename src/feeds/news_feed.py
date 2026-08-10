"""RSS news feed handler"""

import re
import html
import requests
import xml.etree.ElementTree as ET
from typing import List, Dict, Any
from feeds.base_feed import BaseFeed
from utils.payload_builder import build_alert_payload


class NewsFeed(BaseFeed):
    """Fetch news stories from RSS feeds"""

    def fetch(self) -> List[Dict[str, Any]]:
        """Fetch news stories from RSS feed"""

        rss_url = self.config['rss_url']
        num_items = self.config['num_items']

        # Add User-Agent header to avoid being blocked by news sites
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
        }
        response = requests.get(rss_url, headers=headers, timeout=10)
        response.raise_for_status()

        # Get content as bytes and decode properly
        content = response.content

        # Try to parse with error recovery
        try:
            # Use XMLParser for proper encoding handling
            parser = ET.XMLParser(encoding='utf-8')
            root = ET.fromstring(content, parser=parser)
        except ET.ParseError:
            # If parsing fails, clean the content and retry
            content_str = content.decode('utf-8', errors='ignore')
            # Remove control characters except tab, newline, carriage return
            content_str = re.sub(r'[\x00-\x08\x0B\x0C\x0E-\x1F\x7F]', '', content_str)
            root = ET.fromstring(content_str.encode('utf-8'))

        items = []

        for idx, item in enumerate(root.findall('.//item'), start=1):
            if idx > num_items:
                break

            title_elem = item.find('title')
            if title_elem is None or not title_elem.text:
                continue

            title = title_elem.text.strip()

            # Get description (may contain HTML)
            description_elem = item.find('description')
            description = ""
            if description_elem is not None and description_elem.text:
                # Strip HTML tags from description
                description_text = description_elem.text
                # Remove HTML tags
                description_text = re.sub(r'<[^>]+>', '', description_text)
                # Decode HTML entities
                description_text = html.unescape(description_text)
                # Clean up whitespace
                description = ' '.join(description_text.split()).strip()

            # Get link to article
            link_elem = item.find('link')
            link = link_elem.text.strip() if link_elem is not None and link_elem.text else ""

            # Build message with description (if different from title) + read more link
            message_parts = []

            # Add description if it exists and is different from title
            if description and description != title:
                # Truncate to 150 characters
                if len(description) > 150:
                    message_parts.append(description[:150] + "...")
                else:
                    message_parts.append(description)

            # Add "read more" link
            if link:
                message_parts.append(f"<a href='{link}'>read more</a>")

            message = " ".join(message_parts) if message_parts else title

            # Build alert payload for each news story
            payload = build_alert_payload(
                level=self.config['level'],
                category=self.config['category'],
                title=title,
                message=message,
                source=self.config['source'],
                client=self.client_name,
                metadata={
                    "feed_type": "news",
                    "feed_source": rss_url,
                    "story_index": idx,
                    "total_stories": num_items,
                    "link": link
                }
            )

            items.append(payload)

        if not items:
            raise ValueError("No news items found in RSS feed")

        return items
