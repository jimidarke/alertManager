"""
File Logging Pathway

Handles writing alerts to log files with configurable formats and rotation.
"""

import os
import logging
import pytz
from typing import Dict, Any
from datetime import datetime, timedelta
from .base_pathway import BasePathway


class FilePathway(BasePathway):
    """File logging pathway for writing alerts to files"""
    
    def __init__(self, name: str, config: Dict[str, Any]):
        """
        Initialize file pathway

        Args:
            name: Pathway instance name
            config: Configuration dictionary containing:
                - path: File path to write alerts to (can use {client} placeholder)
                - format: Message format template (optional)
                - create_directories: Create parent directories if they don't exist (default: True)
                - append_mode: Use append mode instead of overwrite (default: True)
                - encoding: File encoding (default: utf-8)
                - per_client_files: Create separate log files per client (default: False)
                - client_path_template: Path template for per-client files (default: uses path with {client} placeholder)
                - rotation_period: Log rotation period - 'daily' or 'weekly' (default: daily)
        """
        super().__init__(name, config)

        self.file_path = config.get('path', '/var/log/alerts.log')
        self.create_directories = config.get('create_directories', True)
        self.append_mode = config.get('append_mode', True)
        self.encoding = config.get('encoding', 'utf-8')
        self.per_client_files = config.get('per_client_files', False)
        self.client_path_template = config.get('client_path_template', self.file_path)
        self.rotation_period = config.get('rotation_period', 'daily')
        self.sanitize_messages = config.get('sanitize_messages', True)
        self.max_message_length = config.get('max_message_length', 500)
        self.newline_replacement = config.get('newline_replacement', ' ↵ ')

        # Ensure parent directories exist for base path
        if self.create_directories:
            self._ensure_directory_exists()

    def _get_week_start_date(self) -> str:
        """
        Get the Monday of the current week in YYYY-MM-DD format

        Uses ISO 8601 week date system where Monday is the first day of the week.

        Returns:
            str: Date string in YYYY-MM-DD format representing the Monday of current week
        """
        utc = pytz.UTC
        now = datetime.now(utc)
        # weekday() returns 0 for Monday, 6 for Sunday
        days_since_monday = now.weekday()
        monday = now - timedelta(days=days_since_monday)
        return monday.strftime('%Y-%m-%d')

    def validate_config(self) -> bool:
        """
        Validate file pathway configuration

        Returns:
            bool: True if configuration is valid
        """
        if not self.file_path:
            self.logger.error("File path not specified in configuration")
            return False

        # Check if we can write to the file location
        try:
            # Substitute {date} for validation purposes
            if self.rotation_period == 'weekly':
                date_string = self._get_week_start_date()
            else:
                utc = pytz.UTC
                date_string = datetime.now(utc).strftime('%Y-%m-%d')
            test_path = self.file_path.replace('{date}', date_string)

            parent_dir = os.path.dirname(test_path)
            if parent_dir and not os.path.exists(parent_dir):
                if self.create_directories:
                    os.makedirs(parent_dir, exist_ok=True)
                else:
                    self.logger.error(f"Parent directory does not exist: {parent_dir}")
                    return False

            # Test write access
            test_file = test_path + '.test'
            with open(test_file, 'w', encoding=self.encoding) as f:
                f.write('test')
            os.remove(test_file)

            return True

        except Exception as e:
            self.logger.error(f"Cannot write to file path {self.file_path}: {e}")
            return False
    
    def send_alert(self, alert: Dict[str, Any], pathway_config: Dict[str, Any] = None) -> bool:
        """
        Write alert to file

        Args:
            alert: Alert dictionary
            pathway_config: Pathway-specific configuration overrides

        Returns:
            bool: True if alert was written successfully
        """
        if pathway_config is None:
            pathway_config = {}

        if not self.should_send_alert(alert):
            return False

        try:
            # Determine filename based on per_client_files setting
            if self.per_client_files:
                client = alert.get('client', '').strip()
                if client:
                    # Generate client-specific filename with subdirectory
                    filename = self._get_client_filename(client)
                else:
                    # No client specified - use default path (no subdirectory)
                    filename = self.file_path
            else:
                # Use default filename (pathway config overrides default)
                filename = pathway_config.get('filename', self.file_path)

            # Substitute {date} with current date or week start date based on rotation_period
            if self.rotation_period == 'weekly':
                date_string = self._get_week_start_date()
            else:
                utc = pytz.UTC
                date_string = datetime.now(utc).strftime('%Y-%m-%d')
            filename = filename.replace('{date}', date_string)

            # Ensure directory exists for this specific file
            if self.create_directories:
                parent_dir = os.path.dirname(filename)
                if parent_dir and not os.path.exists(parent_dir):
                    os.makedirs(parent_dir, exist_ok=True)

            # Format the alert message (use pathway config format if provided)
            custom_format = pathway_config.get('format')

            # Create a sanitized copy of alert for formatting if sanitization is enabled
            alert_for_formatting = alert.copy() if self.sanitize_messages else alert

            if self.sanitize_messages:
                # Sanitize fields that go into the formatted message
                for field in ['title', 'message', 'source']:
                    if field in alert_for_formatting and isinstance(alert_for_formatting[field], str):
                        alert_for_formatting[field] = self.sanitize_for_single_line(
                            alert_for_formatting[field],
                            max_length=self.max_message_length if field == 'message' else None,
                            newline_replacement=self.newline_replacement
                        )

            message = self.format_message(alert_for_formatting, custom_format)

            # Add timestamp if not in format
            format_str = pathway_config.get('format', self.config.get('format', ''))
            if '{timestamp}' not in format_str:
                timestamp = datetime.now().isoformat()
                message = f"{timestamp} - {message}"

            # Write to file
            mode = 'a' if self.append_mode else 'w'
            with open(filename, mode, encoding=self.encoding) as f:
                f.write(message + '\n')
                f.flush()

            self.log_send_attempt(alert, True)
            return True

        except Exception as e:
            error_msg = f"Error writing to file: {e}"
            self.log_send_attempt(alert, False, error_msg)
            return False
    
    def _get_client_filename(self, client: str) -> str:
        """
        Generate filename for specific client

        Args:
            client: Client identifier

        Returns:
            str: Full path to client-specific log file
        """
        # If template contains {client}, replace it manually (preserves other placeholders like {date})
        if '{client}' in self.client_path_template:
            return self.client_path_template.replace('{client}', client)

        # Otherwise, insert client name before extension
        base_path = self.client_path_template
        path_parts = os.path.splitext(base_path)

        if path_parts[1]:
            # Has extension: /path/alerts.log -> /path/alerts-client1.log
            return f"{path_parts[0]}-{client}{path_parts[1]}"
        else:
            # No extension: /path/alerts -> /path/alerts-client1
            return f"{base_path}-{client}"

    def _ensure_directory_exists(self):
        """Ensure parent directory exists for file path"""
        try:
            parent_dir = os.path.dirname(self.file_path)
            if parent_dir and not os.path.exists(parent_dir):
                os.makedirs(parent_dir, exist_ok=True)
                self.logger.info(f"Created directory: {parent_dir}")
        except Exception as e:
            self.logger.warning(f"Could not create directory for {self.file_path}: {e}")
    
    def get_file_size(self) -> int:
        """
        Get current file size in bytes
        
        Returns:
            int: File size in bytes, 0 if file doesn't exist
        """
        try:
            return os.path.getsize(self.file_path)
        except OSError:
            return 0
    
    def rotate_file(self, backup_suffix: str = None) -> bool:
        """
        Rotate log file by renaming current file and creating new one
        
        Args:
            backup_suffix: Suffix for backup file (default: timestamp)
            
        Returns:
            bool: True if rotation was successful
        """
        try:
            if not os.path.exists(self.file_path):
                return True
            
            if backup_suffix is None:
                backup_suffix = datetime.now().strftime('%Y%m%d_%H%M%S')
            
            backup_path = f"{self.file_path}.{backup_suffix}"
            os.rename(self.file_path, backup_path)
            
            self.logger.info(f"Rotated log file: {self.file_path} -> {backup_path}")
            return True
            
        except Exception as e:
            self.logger.error(f"Error rotating log file: {e}")
            return False