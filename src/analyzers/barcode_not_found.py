"""
Barcode Not Found Analyzer

Discrete analyzer module for tracking failed barcode/UPC lookups with template-based reporting.
"""

from typing import Dict, Any, List
from datetime import datetime, timedelta
from collections import defaultdict
import pytz
from .base_analyzer import BaseAnalyzer


class BarcodeNotFoundAnalyzer(BaseAnalyzer):
    """Clean, template-driven analyzer for barcode lookup failures"""

    def analyze(self) -> Dict[str, Any]:
        """
        Analyze logs for barcode not found events and successful scans

        Returns:
            Dictionary containing analysis results ready for template rendering
        """
        self.logger.info(f"Starting barcode failure analysis")

        # Get log files
        log_files = self.get_log_files()

        if not log_files:
            return self._empty_results()

        # Calculate date range for filtering events
        analysis_period_days = self.config.get('analysis_period_days', 7)
        end_time = datetime.utcnow()
        start_time = end_time - timedelta(days=analysis_period_days)
        start_timestamp = int(start_time.timestamp())
        end_timestamp = int(end_time.timestamp())

        self.logger.info(f"Filtering events from {start_time.strftime('%Y-%m-%d %H:%M:%S')} to {end_time.strftime('%Y-%m-%d %H:%M:%S')} UTC")

        # Process logs
        barcode_events = defaultdict(list)
        successful_scans = 0

        for log_file in log_files:
            self.logger.debug(f"Processing: {log_file}")

            try:
                with open(log_file, 'r', encoding='utf-8', errors='ignore') as f:
                    for line in f:
                        # Parse log line
                        parsed = self.parse_log_line(line)
                        if not parsed:
                            continue

                        # Filter by timestamp - only include events within analysis period
                        event_timestamp = parsed.get('timestamp')
                        if not event_timestamp or event_timestamp < start_timestamp or event_timestamp > end_timestamp:
                            continue

                        # Check for successful scan (info level with "Price check:" in title)
                        if self._is_successful_scan(parsed):
                            successful_scans += 1
                            continue

                        # Check for failed scan using query pattern
                        if not self.matches_query_pattern(parsed):
                            continue

                        # Extract fields
                        extracted = self.extract_fields(parsed)
                        if not extracted:
                            continue

                        # Store event
                        upc = extracted.get('upc')
                        if upc:
                            event = {
                                'timestamp': extracted['timestamp'],
                                'device': extracted.get('device', 'unknown'),
                                'source': extracted.get('source', 'unknown')
                            }
                            barcode_events[upc].append(event)

            except Exception as e:
                self.logger.error(f"Error processing {log_file}: {e}")
                continue

        # Sort events within each barcode
        for upc in barcode_events:
            barcode_events[upc] = sorted(barcode_events[upc], key=lambda x: x['timestamp'])

        total_failures = sum(len(events) for events in barcode_events.values())
        unique_barcodes = len(barcode_events)

        self.logger.info(f"Analysis complete: {total_failures} failures, {unique_barcodes} unique barcodes, {successful_scans} successful scans")

        return {
            'total_failures': total_failures,
            'unique_barcodes': unique_barcodes,
            'successful_scans': successful_scans,
            'barcodes': dict(barcode_events),
            'analysis_period_days': self.config.get('analysis_period_days', 7),
            'log_files_analyzed': len(log_files)
        }

    def _is_successful_scan(self, parsed: Dict[str, Any]) -> bool:
        """
        Check if parsed log line represents a successful barcode scan

        Successful scan format:
        timestamp [info] [application] source [client]: get_price - Price check: UPC=...

        Args:
            parsed: Parsed log line dictionary

        Returns:
            True if line is a successful scan
        """
        # Successful scans are info level in application category
        if parsed.get('level') != 'info':
            return False
        if parsed.get('category') != 'application':
            return False
        # Message contains "Price check:" (not "Price check failed")
        message = parsed.get('message', '')
        return message.startswith('Price check:') and 'failed' not in message.lower()

    def _empty_results(self) -> Dict[str, Any]:
        """Return empty results structure"""
        return {
            'total_failures': 0,
            'unique_barcodes': 0,
            'successful_scans': 0,
            'barcodes': {},
            'analysis_period_days': self.config.get('analysis_period_days', 7),
            'log_files_analyzed': 0
        }

    def generate_email_report(self, results: Dict[str, Any]) -> str:
        """
        Generate HTML email report using Jinja2 template

        Args:
            results: Analysis results from analyze()

        Returns:
            HTML email content with inline CSS
        """
        # Build template context
        context = self._build_template_context(results)

        # Get template name from query definition or config
        template_name = 'emails/barcode_not_found.html.j2'
        if self.query_def:
            template_config = self.query_def.get('email_template', {})
            template_name = f"emails/{template_config.get('template_name', 'barcode_not_found.html.j2')}"

        # Render template
        return self.render_email_template(template_name, context)

    def _get_analysis_period_description(self, days: int) -> str:
        """
        Get human-readable description of analysis period with actual dates

        Args:
            days: Number of days in analysis period

        Returns:
            Human-readable period description with date range
        """
        # Get configured timezone (default to America/Edmonton if not specified)
        timezone_str = self.config.get('schedule', {}).get('timezone', 'America/Edmonton')
        tz = pytz.timezone(timezone_str)
        now = datetime.now(tz)

        if days == 1:
            # Single day - just show the date
            return now.strftime('%b %-d, %Y')
        else:
            # Date range - show start thru end
            # Subtract (days - 1) to get the start date, so that days=7 means "last 7 days including today"
            start_date = now - timedelta(days=days - 1)
            return f"{start_date.strftime('%b %-d')} thru {now.strftime('%b %-d, %Y')}"

    def _get_local_time_str(self) -> str:
        """
        Get current time in configured timezone for report generation timestamp

        Returns:
            Formatted timestamp string with timezone abbreviation
        """
        timezone_str = self.config.get('schedule', {}).get('timezone', 'America/Edmonton')
        tz = pytz.timezone(timezone_str)
        local_time = datetime.now(tz)
        return local_time.strftime('%Y-%m-%d %H:%M:%S %Z')

    def _build_template_context(self, results: Dict[str, Any]) -> Dict[str, Any]:
        """
        Build context dictionary for template rendering

        Args:
            results: Analysis results

        Returns:
            Context dictionary for Jinja2 template
        """
        total_failures = results['total_failures']
        unique_barcodes = results['unique_barcodes']
        successful_scans = results.get('successful_scans', 0)
        barcodes = results['barcodes']
        analysis_period_days = results['analysis_period_days']

        # Get theme colors
        theme = self.get_theme_colors()

        # Get period description
        period_description = self._get_analysis_period_description(analysis_period_days)

        # Calculate total scans and success rate
        total_scans = successful_scans + total_failures
        success_rate = (successful_scans / total_scans * 100) if total_scans > 0 else 0

        # Base context
        context = {
            'report_title': self.config.get('email_subject', 'Barcode Lookup Failure Report'),
            'report_subtitle': f"Analysis Period: {period_description}",
            'generation_time': self._get_local_time_str(),
            'client_name': self.config.get('client', 'Analytics').title(),
            'total_failures': total_failures,
            'unique_barcodes': unique_barcodes,
            'successful_scans': successful_scans,
            'total_scans': total_scans,
            'success_rate': success_rate,
            'analysis_period': period_description,
            'has_data': bool(barcodes),
            'logo_url': self.get_logo_base64(),
            'theme': theme,
        }

        # Summary metrics (light theme colors)
        context['metrics'] = [
            {
                'label': 'Successful Scans',
                'value': successful_scans,
                'color': '#27ae60'  # Green for success
            },
            {
                'label': 'Not Found',
                'value': total_failures,
                'color': '#c0392b'  # Red for failures
            },
            {
                'label': 'Success Rate',
                'value': f"{success_rate:.1f}%",
                'color': '#007acc'  # Accent blue
            }
        ]

        # Insight box (using theme colors)
        context['insight'] = {
            'title': 'Analysis Insight',
            'content': self._generate_insight(total_failures, unique_barcodes, successful_scans),
            'type': 'info'  # Uses preset from insight_box component
        }

        # Data table
        if barcodes:
            context.update(self._build_table_context(barcodes))

        # Recommendations
        context['recommendations'] = self._generate_recommendations(total_failures, unique_barcodes, barcodes)

        return context

    def _generate_insight(self, total_failures: int, unique_barcodes: int, successful_scans: int = 0) -> str:
        """Generate insight text based on statistics"""
        total_scans = successful_scans + total_failures
        success_rate = (successful_scans / total_scans * 100) if total_scans > 0 else 0

        if total_failures == 0:
            return f"Excellent barcode database coverage with {successful_scans:,} successful lookups and no failures."

        # Build insight based on success rate
        if success_rate >= 99:
            insight = f"Very high success rate ({success_rate:.1f}%) with {successful_scans:,} successful scans."
        elif success_rate >= 95:
            insight = f"Good database coverage ({success_rate:.1f}% success rate) across {total_scans:,} total scans."
        elif success_rate >= 90:
            insight = f"Moderate coverage gaps detected ({success_rate:.1f}% success rate)."
        else:
            insight = f"Significant coverage gaps ({success_rate:.1f}% success rate) requiring attention."

        # Add detail about repeat failures
        avg_failures = total_failures / unique_barcodes if unique_barcodes > 0 else 0
        if avg_failures > 5:
            insight += f" On average, each missing barcode was scanned {avg_failures:.1f} times."

        return insight

    def _build_table_context(self, barcodes: Dict[str, List[Dict[str, Any]]]) -> Dict[str, Any]:
        """Build table data for template"""
        # Sort barcodes by count descending (most scanned first)
        sorted_barcodes = sorted(barcodes.items(), key=lambda x: len(x[1]), reverse=True)

        # Build table headers (simplified 2-column)
        headers = [
            {'label': 'UPC Code', 'width': '60%'},
            {'label': 'Count', 'width': '40%'}
        ]

        # Build table rows (simplified)
        rows = []
        for upc, events in sorted_barcodes:
            attempt_count = len(events)

            row = {
                'cells': [
                    {
                        'value': upc,
                        'style': "font-family: 'Courier New', monospace; font-weight: bold;"
                    },
                    {
                        'value': attempt_count,
                        'style': "text-align: center; font-weight: bold;"
                    }
                ]
            }
            rows.append(row)

        return {
            'table_title': 'Barcode Lookup Failures',
            'table_description': 'UPC codes that could not be found using the price checker, sorted by scan count (most frequent first).',
            'headers': headers,
            'rows': rows,
            'table_footer': None
        }

    def _generate_recommendations(self, total_failures: int, unique_barcodes: int, barcodes: Dict) -> List[str]:
        """Generate recommendations based on analysis"""
        if total_failures == 0:
            return []

        recommendations = []

        # High failure count
        if total_failures > 50:
            recommendations.append("Consider implementing a barcode request system for customers to report missing products.")

        # High unique barcode count
        if unique_barcodes > 20:
            recommendations.append("Review product database coverage to identify systematic gaps in inventory data.")

        # High repeat attempts
        high_repeat_barcodes = [upc for upc, events in barcodes.items() if len(events) > 10]
        if high_repeat_barcodes:
            recommendations.append(f"Priority: {len(high_repeat_barcodes)} barcode(s) have 10+ failed attempts and should be added to the database immediately.")

        return recommendations
