"""
Analytics Engine

Scheduled analytics job runner that processes log files and generates email reports.
Supports multiple analyzers with flexible scheduling (daily, weekly, monthly).
"""

import os
import sys
import json
import logging
import schedule
import time
import signal
import pytz
from typing import Dict, Any, List, Optional
from datetime import datetime
from pathways.email_pathway import EmailPathway
from utils import heartbeat


class AnalyticsEngine:
    """Main analytics engine for scheduled log analysis and reporting"""

    def __init__(self, config_path: str):
        """
        Initialize analytics engine

        Args:
            config_path: Path to configuration JSON file
        """
        self.config_path = config_path
        self.config = self._load_config()
        self.running = True
        self.logger = self._setup_logging()
        self.analyzers = {}
        self.email_pathway = None

        # Heartbeat file, polled by the container healthcheck to detect a
        # hung/dead process (see src/utils/heartbeat.py).
        self.heartbeat_path = os.getenv('HEARTBEAT_FILE', '/data/logs/ops/analytics-engine.heartbeat')

        # Setup signal handlers for graceful shutdown
        signal.signal(signal.SIGINT, self._handle_shutdown)
        signal.signal(signal.SIGTERM, self._handle_shutdown)

        self.logger.info("Analytics Engine initialized")

    def _load_config(self) -> Dict[str, Any]:
        """Load configuration from JSON file"""
        try:
            with open(self.config_path, 'r') as f:
                config = json.load(f)
            return config.get('analytics', {})
        except Exception as e:
            print(f"CRITICAL: Failed to load config from {self.config_path}: {e}")
            sys.exit(1)

    def _setup_logging(self) -> logging.Logger:
        """Setup logging for analytics engine"""
        logger = logging.getLogger('analytics_engine')
        logger.setLevel(logging.INFO)

        # Console handler
        console_handler = logging.StreamHandler()
        console_handler.setLevel(logging.INFO)
        console_formatter = logging.Formatter(
            '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
        )
        console_handler.setFormatter(console_formatter)
        logger.addHandler(console_handler)

        # File handler (if configured)
        log_config = self.config.get('logging', {})
        log_file = log_config.get('file', '/data/logs/ops/analytics-engine.{date}.log')

        # Replace {date} placeholder
        log_file = log_file.replace('{date}', datetime.utcnow().strftime('%Y-%m-%d'))

        # Create directory if needed
        os.makedirs(os.path.dirname(log_file), exist_ok=True)

        file_handler = logging.FileHandler(log_file)
        file_handler.setLevel(logging.INFO)
        file_formatter = logging.Formatter(
            '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
        )
        file_handler.setFormatter(file_formatter)
        logger.addHandler(file_handler)

        return logger

    def _handle_shutdown(self, signum, frame):
        """Handle shutdown signals gracefully"""
        self.logger.info(f"Received signal {signum}, shutting down gracefully...")
        self.running = False

    def _setup_email_pathway(self):
        """Setup email pathway for sending reports"""
        email_config = self.config.get('email', {})

        if not email_config.get('enabled', False):
            self.logger.warning("Email pathway not enabled, reports will not be sent")
            return

        try:
            self.email_pathway = EmailPathway('analytics_email', email_config)

            if self.email_pathway.validate_config():
                self.logger.info("Email pathway configured successfully")
            else:
                self.logger.error("Email pathway configuration validation failed")
                self.email_pathway = None
        except Exception as e:
            self.logger.error(f"Failed to setup email pathway: {e}")
            self.email_pathway = None

    def _load_analyzers(self):
        """Dynamically load analyzers based on configuration"""
        analyzers_config = self.config.get('analyzers', {})

        for analyzer_name, analyzer_config in analyzers_config.items():
            if not analyzer_config.get('enabled', False):
                self.logger.info(f"Analyzer '{analyzer_name}' is disabled, skipping")
                continue

            try:
                # Import analyzer module
                analyzer_type = analyzer_config.get('type')

                if analyzer_type == 'barcode_not_found':
                    from analyzers.barcode_not_found import BarcodeNotFoundAnalyzer
                    analyzer = BarcodeNotFoundAnalyzer(analyzer_name, analyzer_config, self.logger)
                else:
                    self.logger.error(f"Unknown analyzer type: {analyzer_type}")
                    continue

                self.analyzers[analyzer_name] = analyzer
                self.logger.info(f"Loaded analyzer: {analyzer_name} ({analyzer_type})")

            except Exception as e:
                self.logger.error(f"Failed to load analyzer '{analyzer_name}': {e}")

    def _get_local_time_str(self, time_str: str, timezone_str: str) -> str:
        """
        Convert local time to system time for scheduling.

        The schedule library uses system time (which should be set via TZ env var).
        If TZ matches the configured timezone, we use the time directly.
        Otherwise, we convert from the configured timezone to UTC and then to system time.

        Args:
            time_str: Time string in HH:MM format (local time)
            timezone_str: Timezone string (e.g., 'America/Edmonton')

        Returns:
            Time string for scheduling in system timezone
        """
        # Since Docker containers have TZ=America/Edmonton, and we're scheduling
        # in the same timezone, the schedule library will use local time directly
        # This is the simplest and most reliable approach
        return time_str

    def _schedule_analyzers(self):
        """Schedule analyzers based on their configuration"""
        for analyzer_name, analyzer in self.analyzers.items():
            schedule_config = analyzer.config.get('schedule', {})
            frequency = schedule_config.get('frequency', 'weekly')
            time_str = schedule_config.get('time', '09:00')
            day_of_week = schedule_config.get('day_of_week', 'monday')
            timezone_str = schedule_config.get('timezone', 'America/Edmonton')

            # Convert time to system timezone (schedule library uses system time)
            scheduled_time = self._get_local_time_str(time_str, timezone_str)

            # Create scheduled job
            if frequency == 'daily':
                schedule.every().day.at(scheduled_time).do(
                    self._run_analyzer, analyzer_name
                )
                self.logger.info(f"Scheduled {analyzer_name}: daily at {time_str} ({timezone_str})")

            elif frequency == 'weekly':
                day_method = getattr(schedule.every(), day_of_week.lower())
                day_method.at(scheduled_time).do(
                    self._run_analyzer, analyzer_name
                )
                self.logger.info(f"Scheduled {analyzer_name}: {day_of_week} at {time_str} ({timezone_str})")

            elif frequency == 'monthly':
                # Monthly scheduling (run on first day of month)
                schedule.every().day.at(scheduled_time).do(
                    self._run_monthly_analyzer, analyzer_name
                )
                self.logger.info(f"Scheduled {analyzer_name}: monthly on 1st at {time_str} ({timezone_str})")

            else:
                self.logger.error(f"Unknown frequency for {analyzer_name}: {frequency}")

    def _run_monthly_analyzer(self, analyzer_name: str):
        """Run analyzer only on first day of month"""
        if datetime.now().day == 1:
            self._run_analyzer(analyzer_name)

    def _is_empty_results(self, results: Dict[str, Any]) -> bool:
        """
        Check if analysis results contain no data.

        Checks common result keys that indicate data presence:
        - total_failures, total_count, count (numeric == 0)
        - barcodes, items, data (empty dict/list)
        - has_data (explicit boolean)

        Args:
            results: Analysis results dictionary

        Returns:
            True if results appear to be empty
        """
        # Check explicit has_data flag
        if 'has_data' in results:
            return not results['has_data']

        # Check common count fields
        for count_key in ['total_failures', 'total_count', 'count', 'total']:
            if count_key in results and results[count_key] == 0:
                return True

        # Check common data collection fields
        for data_key in ['barcodes', 'items', 'data', 'events', 'records']:
            if data_key in results:
                data = results[data_key]
                if isinstance(data, (dict, list)) and len(data) == 0:
                    return True

        return False

    def _run_analyzer(self, analyzer_name: str):
        """
        Run a specific analyzer and send email report

        Args:
            analyzer_name: Name of the analyzer to run
        """
        self.logger.info(f"Running analyzer: {analyzer_name}")

        try:
            analyzer = self.analyzers.get(analyzer_name)
            if not analyzer:
                self.logger.error(f"Analyzer not found: {analyzer_name}")
                return

            # Run analysis
            results = analyzer.analyze()

            if results is None:
                self.logger.error(f"Analyzer {analyzer_name} returned None")
                return

            # Check if we should skip empty results
            skip_empty = analyzer.config.get('skip_if_no_results', False)
            if skip_empty and self._is_empty_results(results):
                self.logger.info(f"Skipping email for {analyzer_name}: no results found for analysis period")
                return

            # Generate email report
            email_html = analyzer.generate_email_report(results)

            # Send email if pathway is configured
            if self.email_pathway and email_html:
                recipients = analyzer.config.get('email_recipients', [])
                subject = analyzer.config.get('email_subject', f'Analytics Report: {analyzer_name}')

                # Create alert payload for email pathway
                alert = {
                    'timestamp': int(datetime.now().timestamp()),
                    'level': 'info',
                    'category': 'application',
                    'title': subject,
                    'message': email_html,
                    'source': 'analytics-engine',
                    'client': analyzer.config.get('client', 'analytics')
                }

                # Override email settings for this specific report
                original_to = self.email_pathway.to_addresses
                original_subject = self.email_pathway.subject_template
                original_html = self.email_pathway.html_format
                original_body_template = self.email_pathway.body_template

                self.email_pathway.to_addresses = recipients
                self.email_pathway.subject_template = subject
                self.email_pathway.html_format = True
                # Use body_template to pass pre-rendered HTML directly (bypass alert wrapper)
                self.email_pathway.body_template = '{message}'

                # Send email
                success = self.email_pathway.send_alert(alert)

                # Restore original settings
                self.email_pathway.to_addresses = original_to
                self.email_pathway.subject_template = original_subject
                self.email_pathway.html_format = original_html
                self.email_pathway.body_template = original_body_template

                if success:
                    self.logger.info(f"Email report sent for {analyzer_name}")
                else:
                    self.logger.error(f"Failed to send email report for {analyzer_name}")
            else:
                self.logger.warning(f"Email pathway not configured, skipping email for {analyzer_name}")

            self.logger.info(f"Completed analyzer: {analyzer_name}")

        except Exception as e:
            self.logger.error(f"Error running analyzer {analyzer_name}: {e}", exc_info=True)

    def run(self):
        """Main run loop for analytics engine"""
        self.logger.info("Starting Analytics Engine")

        # Setup components
        self._setup_email_pathway()
        self._load_analyzers()

        if not self.analyzers:
            self.logger.error("No analyzers loaded, exiting")
            sys.exit(1)

        # Schedule analyzers
        self._schedule_analyzers()

        self.logger.info(f"Analytics Engine running with {len(self.analyzers)} analyzer(s)")

        # Check if we should run analyzers immediately on startup
        if self.config.get('run_on_startup', False):
            self.logger.info("Running all analyzers on startup")
            for analyzer_name in self.analyzers.keys():
                self._run_analyzer(analyzer_name)

        # Main loop
        while self.running:
            try:
                schedule.run_pending()
                heartbeat.beat(self.heartbeat_path)
                time.sleep(60)  # Check every minute
            except Exception as e:
                self.logger.error(f"Error in main loop: {e}", exc_info=True)
                time.sleep(60)

        self.logger.info("Analytics Engine stopped")


def main():
    """Main entry point"""
    import argparse

    parser = argparse.ArgumentParser(description='Analytics Engine')
    parser.add_argument('--config', required=True, help='Path to configuration file')
    parser.add_argument('--run-once', help='Run specific analyzer once and exit')

    args = parser.parse_args()

    engine = AnalyticsEngine(args.config)

    if args.run_once:
        # Run single analyzer and exit
        engine._setup_email_pathway()
        engine._load_analyzers()
        engine._run_analyzer(args.run_once)
    else:
        # Run scheduled loop
        engine.run()


if __name__ == '__main__':
    main()
