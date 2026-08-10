#!/usr/bin/env python3
"""
Feed Aggregator Service
Fetches data from APIs and RSS feeds, converts to Alert Manager payloads,
and publishes to alerts/input MQTT topic for routing.
"""

import signal
import sys
import time
import logging
from datetime import datetime
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger

from feeds.news_feed import NewsFeed
from feeds.joke_feed import JokeFeed
from feeds.fact_feed import FactFeed
from feeds.trivia_feed import TriviaFeed
from utils.config_loader import load_config
from utils.mqtt_publisher import MQTTPublisher

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)


class FeedAggregator:
    """Main feed aggregator service with scheduler"""

    def __init__(self, config_path: str):
        self.config = load_config(config_path)
        self.mqtt_publisher = MQTTPublisher(self.config['feed_aggregator']['mqtt'])
        self.scheduler = BackgroundScheduler()
        self.feeds = {}
        self.running = False

        # Initialize feed handlers
        feed_config = self.config['feed_aggregator']
        self.client_name = feed_config['client_name']

        if feed_config['feeds']['news']['enabled']:
            self.feeds['news'] = NewsFeed(
                feed_config['feeds']['news'],
                self.client_name,
                self.mqtt_publisher
            )

        if feed_config['feeds']['jokes']['enabled']:
            self.feeds['jokes'] = JokeFeed(
                feed_config['feeds']['jokes'],
                self.client_name,
                self.mqtt_publisher
            )

        if feed_config['feeds']['facts']['enabled']:
            self.feeds['facts'] = FactFeed(
                feed_config['feeds']['facts'],
                self.client_name,
                self.mqtt_publisher
            )

        if feed_config['feeds']['trivia']['enabled']:
            self.feeds['trivia'] = TriviaFeed(
                feed_config['feeds']['trivia'],
                self.client_name,
                self.mqtt_publisher
            )

        logger.info(f"Initialized {len(self.feeds)} feed handlers")

    def start(self):
        """Start the scheduler and run initial fetch"""
        logger.info("Starting Feed Aggregator Service")
        self.running = True

        # Connect to MQTT broker
        self.mqtt_publisher.connect()

        # Fetch all feeds immediately on startup
        logger.info("Running initial feed fetch")
        for feed_name, feed_handler in self.feeds.items():
            logger.info(f"Fetching {feed_name} feed...")
            try:
                feed_handler.fetch_and_publish()
            except Exception as e:
                logger.error(f"Initial fetch failed for {feed_name}: {e}")

        # Schedule recurring fetches
        for feed_name, feed_handler in self.feeds.items():
            interval_minutes = feed_handler.config['interval_minutes']
            self.scheduler.add_job(
                func=feed_handler.fetch_and_publish,
                trigger=IntervalTrigger(minutes=interval_minutes),
                id=f'{feed_name}_fetch',
                name=f'Fetch {feed_name}',
                replace_existing=True
            )
            logger.info(f"Scheduled {feed_name} to run every {interval_minutes} minutes")

        self.scheduler.start()
        logger.info("Scheduler started successfully")

        # Keep the main thread alive
        try:
            while self.running:
                time.sleep(1)
        except KeyboardInterrupt:
            logger.info("Received interrupt signal")
            self.stop()

    def stop(self):
        """Stop the scheduler and disconnect MQTT"""
        logger.info("Stopping Feed Aggregator Service")
        self.running = False

        if self.scheduler.running:
            self.scheduler.shutdown(wait=False)
            logger.info("Scheduler stopped")

        self.mqtt_publisher.disconnect()
        logger.info("MQTT disconnected")


def signal_handler(sig, frame):
    """Handle SIGTERM and SIGINT"""
    logger.info(f"Received signal {sig}")
    sys.exit(0)


def main():
    import argparse

    parser = argparse.ArgumentParser(description='Feed Aggregator Service')
    parser.add_argument('--config', required=True, help='Path to configuration file')
    args = parser.parse_args()

    # Register signal handlers
    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)

    try:
        aggregator = FeedAggregator(args.config)
        aggregator.start()
    except Exception as e:
        logger.critical(f"Fatal error: {e}", exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
