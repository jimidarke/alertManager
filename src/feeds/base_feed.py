"""Abstract base class for feed handlers"""

import logging
import time
from abc import ABC, abstractmethod
from typing import List, Dict, Any
from utils.payload_builder import build_error_alert

logger = logging.getLogger(__name__)


class BaseFeed(ABC):
    """Abstract base class for all feed handlers"""

    def __init__(self, config: dict, client_name: str, mqtt_publisher):
        self.config = config
        self.client_name = client_name
        self.mqtt_publisher = mqtt_publisher
        self.feed_type = self.__class__.__name__.replace('Feed', '').lower()

    @abstractmethod
    def fetch(self) -> List[Dict[str, Any]]:
        """
        Fetch data from feed source
        Returns list of alert payloads
        Must be implemented by subclasses
        """
        pass

    def fetch_and_publish(self):
        """
        Fetch data and publish to MQTT with error handling
        Retry once on failure, send warning alert if both attempts fail
        """
        logger.info(f"Fetching {self.feed_type} feed")

        try:
            # First attempt
            payloads = self.fetch()
            self._publish_all(payloads)
            return

        except Exception as e:
            logger.warning(f"First attempt failed for {self.feed_type}: {e}")

            # Wait 5 seconds before retry
            time.sleep(5)

            try:
                # Retry attempt
                logger.info(f"Retrying {self.feed_type} fetch")
                payloads = self.fetch()
                self._publish_all(payloads)
                return

            except Exception as retry_error:
                # Both attempts failed - send error alert
                logger.error(f"Retry failed for {self.feed_type}: {retry_error}")

                error_alert = build_error_alert(
                    feed_type=self.feed_type,
                    error_message=str(retry_error),
                    client=self.client_name,
                    error_details={
                        "retry_attempted": True,
                        "original_error": str(e)
                    }
                )

                self.mqtt_publisher.publish_alert(error_alert)
                logger.info(f"Sent error alert for {self.feed_type} failure")

    def _publish_all(self, payloads: List[Dict[str, Any]]):
        """Publish all payloads to MQTT"""
        success_count = 0

        for payload in payloads:
            if self.mqtt_publisher.publish_alert(payload):
                success_count += 1

        logger.info(f"Published {success_count}/{len(payloads)} {self.feed_type} alerts")
