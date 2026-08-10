"""
Base Analyzer

Abstract base class for all log analyzers with Jinja2 template support and query abstraction.

This class provides file-based log analysis. For database-backed analysis with
improved performance, use DatabaseAnalyzer (src/analyzers/database_analyzer.py).

Dual-Mode Support:
- BaseAnalyzer: File-based parsing with regex (legacy, backward compatible)
- DatabaseAnalyzer: SQL queries on PostgreSQL (new, high-performance)

Both classes share the same template engine and query definition system.
"""

import os
import re
import json
import glob
import base64
from abc import ABC, abstractmethod
from typing import Dict, Any, List, Optional
from datetime import datetime, timedelta
import logging
from jinja2 import Environment, FileSystemLoader, select_autoescape
from premailer import Premailer


# Theme colors from THEME_SPECIFICATION.md v1.0.1
THEME_COLORS = {
    'bg_primary': '#1e1e1e',
    'bg_secondary': '#252526',
    'bg_tertiary': '#2d2d2d',
    'bg_surface': '#3c3c3c',
    'text_primary': '#d4d4d4',
    'text_secondary': '#858585',
    'text_value': '#4ec9b0',
    'accent_primary': '#007acc',
    'accent_focus': '#3794ff',
    'border_color': '#555555',
    'level_critical': '#f48771',
    'level_critical_bg': '#5a1a1a',
    'level_warning': '#dcdcaa',
    'level_warning_bg': '#4d4d1a',
    'level_notice': '#4ec9b0',
    'level_notice_bg': '#1a4d4d',
    'level_info': '#9cdcfe',
    'level_info_bg': '#1a3d4d',
    'level_debug': '#6a9955',
    'level_debug_bg': '#1a3d1a',
    'status_success': '#0e8a16',
    'status_error': '#d73a49',
}


class BaseAnalyzer(ABC):
    """Abstract base class for log analyzers with template support"""

    def __init__(self, name: str, config: Dict[str, Any], logger: logging.Logger):
        """
        Initialize base analyzer

        Args:
            name: Analyzer name
            config: Configuration dictionary
            logger: Logger instance
        """
        self.name = name
        self.config = config
        self.logger = logger.getChild(name)

        # Setup Jinja2 environment
        self.template_env = self._setup_template_environment()

        # Load query definition if specified
        self.query_def = self._load_query_definition()

    def _setup_template_environment(self) -> Environment:
        """Setup Jinja2 template environment"""
        template_dir = os.path.join(
            os.path.dirname(os.path.dirname(__file__)),
            'templates'
        )

        env = Environment(
            loader=FileSystemLoader(template_dir),
            autoescape=select_autoescape(['html', 'xml']),
            trim_blocks=True,
            lstrip_blocks=True
        )

        # Add custom filters
        env.filters['datetime_format'] = self._datetime_format_filter
        env.filters['number_format'] = self._number_format_filter

        return env

    def _datetime_format_filter(self, timestamp: int, format: str = '%Y-%m-%d %H:%M:%S') -> str:
        """Jinja2 filter to format Unix timestamp"""
        try:
            return datetime.fromtimestamp(timestamp).strftime(format)
        except:
            return str(timestamp)

    def _number_format_filter(self, value: float, decimals: int = 2) -> str:
        """Jinja2 filter to format numbers"""
        try:
            return f"{value:,.{decimals}f}"
        except:
            return str(value)

    def get_logo_base64(self) -> Optional[str]:
        """
        Get logo as base64 data URI for embedding in emails

        Returns:
            Base64 data URI string or None if logo not found
        """
        # Look for logo in assets directory
        logo_paths = [
            '/data/assets/logo/AlertManager-512x512.png',
            os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
                        'assets', 'logo', 'AlertManager-512x512.png'),
        ]

        for logo_path in logo_paths:
            if os.path.exists(logo_path):
                try:
                    with open(logo_path, 'rb') as f:
                        logo_data = base64.b64encode(f.read()).decode('utf-8')
                    return f"data:image/png;base64,{logo_data}"
                except Exception as e:
                    self.logger.warning(f"Failed to read logo from {logo_path}: {e}")

        self.logger.debug("Logo file not found for email embedding")
        return None

    def get_theme_colors(self) -> Dict[str, str]:
        """Get theme colors dictionary for template context"""
        return THEME_COLORS.copy()

    def _load_query_definition(self) -> Optional[Dict[str, Any]]:
        """Load query definition from file if specified"""
        query_def_file = self.config.get('query_definition')

        if not query_def_file:
            return None

        query_def_path = os.path.join(
            os.path.dirname(os.path.dirname(__file__)),
            'query_definitions',
            query_def_file
        )

        try:
            with open(query_def_path, 'r') as f:
                query_def = json.load(f)
            self.logger.info(f"Loaded query definition: {query_def_file}")
            return query_def
        except Exception as e:
            self.logger.error(f"Failed to load query definition {query_def_file}: {e}")
            return None

    def get_log_files(self, days_back: int = None) -> List[str]:
        """
        Get list of log files to analyze based on configuration

        Args:
            days_back: Number of days back to analyze (overrides config)

        Returns:
            List of log file paths
        """
        if days_back is None:
            days_back = self.config.get('analysis_period_days', 7)

        log_path_pattern = self.config.get('log_path_pattern')
        rotation_period = self.config.get('rotation_period', 'daily')

        if not log_path_pattern:
            self.logger.error("log_path_pattern not configured")
            return []

        log_files = []
        dates = []
        current_date = datetime.utcnow()

        # Generate date strings based on rotation period
        if rotation_period == 'daily':
            for i in range(days_back):
                date = current_date - timedelta(days=i)
                dates.append(date.strftime('%Y-%m-%d'))

        elif rotation_period == 'weekly':
            weeks_back = (days_back // 7) + 1
            for i in range(weeks_back):
                date = current_date - timedelta(weeks=i)
                monday = date - timedelta(days=date.weekday())
                dates.append(monday.strftime('%Y-%m-%d'))

        # Resolve log file paths
        for date_str in dates:
            path = log_path_pattern.replace('{date}', date_str)

            if '*' in path:
                matched_files = glob.glob(path)
                log_files.extend(matched_files)
            else:
                if os.path.exists(path):
                    log_files.append(path)

        self.logger.info(f"Found {len(log_files)} log file(s) to analyze")
        return log_files

    def parse_log_line(self, line: str) -> Optional[Dict[str, Any]]:
        """
        Parse a log line into structured data

        Expected format: timestamp [level] [category] source [client]: title - message

        Args:
            line: Raw log line

        Returns:
            Dictionary with parsed fields or None if parse fails
        """
        pattern = r'^(\d+)\s+\[(\w+)\]\s+\[(\w+)\]\s+([^\[]+)\s+\[([^\]]*)\]:\s+(.+?)(?:\s+-\s+(.+))?$'
        match = re.match(pattern, line.strip())

        if not match:
            return None

        timestamp, level, category, source, client, title, message = match.groups()

        return {
            'timestamp': int(timestamp),
            'level': level.lower(),
            'category': category.lower(),
            'source': source.strip(),
            'client': client.strip(),
            'title': title.strip(),
            'message': message.strip() if message else '',
            'raw_line': line
        }

    def matches_query_pattern(self, parsed_line: Dict[str, Any]) -> bool:
        """
        Check if parsed log line matches query definition patterns

        Args:
            parsed_line: Parsed log line dictionary

        Returns:
            True if line matches query patterns
        """
        if not self.query_def:
            return True

        pattern_matching = self.query_def.get('log_pattern_matching', {})

        # Check title pattern
        title_pattern = pattern_matching.get('title_pattern')
        if title_pattern:
            flags = re.IGNORECASE if 'IGNORECASE' in pattern_matching.get('title_pattern_flags', []) else 0
            if not re.search(title_pattern, parsed_line.get('title', ''), flags):
                return False

        # Check message pattern
        message_pattern = pattern_matching.get('message_pattern')
        if message_pattern:
            flags = re.IGNORECASE if 'IGNORECASE' in pattern_matching.get('message_pattern_flags', []) else 0
            if not re.search(message_pattern, parsed_line.get('message', ''), flags):
                return False

        # Check level filter
        level_filter = pattern_matching.get('level_filter')
        if level_filter and parsed_line.get('level') not in level_filter:
            return False

        # Check category filter
        category_filter = pattern_matching.get('category_filter')
        if category_filter and parsed_line.get('category') not in category_filter:
            return False

        return True

    def extract_fields(self, parsed_line: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """
        Extract data fields from parsed log line using query definition

        Args:
            parsed_line: Parsed log line dictionary

        Returns:
            Dictionary of extracted fields or None if required fields missing
        """
        if not self.query_def:
            return parsed_line

        data_extraction = self.query_def.get('data_extraction', {})
        fields_config = data_extraction.get('fields', [])

        extracted = {
            'timestamp': parsed_line['timestamp'],
            'source': parsed_line['source'],
            'client': parsed_line['client']
        }

        message = parsed_line.get('message', '')

        for field_config in fields_config:
            field_name = field_config['name']
            pattern = field_config['pattern']
            required = field_config.get('required', False)
            default = field_config.get('default')

            match = re.search(pattern, message)

            if match:
                value = match.group(1) if match.groups() else match.group(0)
                extracted[field_name] = value
            elif required:
                return None
            elif default is not None:
                extracted[field_name] = default

        return extracted

    def render_email_template(self, template_name: str, context: Dict[str, Any]) -> str:
        """
        Render email template with context and convert CSS to inline styles

        Args:
            template_name: Template filename (e.g., 'emails/barcode_not_found.html.j2')
            context: Template context dictionary

        Returns:
            HTML email string with inline CSS
        """
        try:
            # Render template
            template = self.template_env.get_template(template_name)
            html = template.render(**context)

            # Convert CSS to inline styles using premailer
            premailer = Premailer(
                html,
                strip_important=False,
                keep_style_tags=False,
                remove_classes=False,
                align_floating_images=False,
                base_url=None
            )

            inline_html = premailer.transform()

            self.logger.debug(f"Rendered template: {template_name}")
            return inline_html

        except Exception as e:
            self.logger.error(f"Error rendering template {template_name}: {e}", exc_info=True)
            return self._generate_error_email(str(e))

    def _generate_error_email(self, error_message: str) -> str:
        """Generate simple error email when template rendering fails"""
        return f"""
        <html>
        <body style="font-family: Arial, sans-serif; padding: 20px;">
            <h2 style="color: #c0392b;">Analytics Report Error</h2>
            <p>An error occurred while generating the analytics report:</p>
            <pre style="background-color: #f4f4f4; padding: 10px; border-radius: 5px;">{error_message}</pre>
            <p>Please contact the system administrator.</p>
        </body>
        </html>
        """

    @abstractmethod
    def analyze(self) -> Dict[str, Any]:
        """
        Perform analysis on log files

        Returns:
            Dictionary containing analysis results
        """
        pass

    @abstractmethod
    def generate_email_report(self, results: Dict[str, Any]) -> str:
        """
        Generate HTML email report from analysis results

        Args:
            results: Analysis results dictionary

        Returns:
            HTML email content
        """
        pass
