"""
Base Pathway Class

Defines the interface and common functionality for all alert pathway implementations.
"""

import logging
import threading
from abc import ABC, abstractmethod
from typing import Dict, Any, Optional
from datetime import datetime


class BasePathway(ABC):
    """Abstract base class for all alert pathways"""

    def __init__(self, name: str, config: Dict[str, Any]):
        """
        Initialize pathway with configuration

        Args:
            name: Unique identifier for this pathway instance
            config: Configuration dictionary for this pathway
        """
        self.name = name
        self.config = config
        self.enabled = config.get('enabled', True)
        self.logger = logging.getLogger(f"pathway.{name}")

        # Counts failures from work this pathway does outside the
        # synchronous send_alert() call (e.g. a background-thread flush).
        # See increment_async_errors() for why this exists.
        self.async_errors = 0
        self._async_errors_lock = threading.Lock()

    @abstractmethod
    def send_alert(self, alert: Dict[str, Any]) -> bool:
        """
        Send alert via this pathway
        
        Args:
            alert: Alert dictionary containing all alert data
            
        Returns:
            bool: True if alert was sent successfully, False otherwise
        """
        pass
    
    @abstractmethod
    def validate_config(self) -> bool:
        """
        Validate pathway configuration
        
        Returns:
            bool: True if configuration is valid, False otherwise
        """
        pass
    
    def is_enabled(self) -> bool:
        """Check if pathway is enabled"""
        return self.enabled

    def increment_async_errors(self, count: int = 1) -> None:
        """
        Increment this pathway's async-error counter.

        Some pathways do work outside the main synchronous send_alert()
        call path -- e.g. EmailPathway's storm-prevention aggregation
        flush, which runs on a background threading.Timer. A failure
        there has no return value flowing back to a caller the way a
        send_alert() failure does, so without this counter it would never
        surface anywhere. Call this from that kind of code path.
        PathwayManager.get_async_errors() exposes it per pathway, and
        AlertRouter.publish_service_status() merges it into the same
        pathway_errors count used for ordinary synchronous failures.

        Args:
            count: Number of async errors to add (default 1).
        """
        with self._async_errors_lock:
            self.async_errors += count

    def format_message(self, alert: Dict[str, Any], template: Optional[str] = None) -> str:
        """
        Format alert message using template

        Args:
            alert: Alert dictionary
            template: Optional format template, uses default if None

        Returns:
            str: Formatted message
        """
        if template is None:
            template = self.config.get('format', '{timestamp} [{level}] {title}: {message}')

        # Ensure optional fields have default values for formatting
        format_data = {
            'client': alert.get('client', ''),
            **alert
        }

        try:
            return template.format(**format_data)
        except KeyError:
            return f"{alert.get('title', 'Unknown')}: {alert.get('message', 'No message')}"

    def sanitize_for_single_line(
        self,
        text: str,
        max_length: Optional[int] = None,
        newline_replacement: str = " ↵ "
    ) -> str:
        """
        Sanitize text for single-line output formats.
        Replaces newlines/carriage returns/tabs, truncates long text.

        Args:
            text: Text to sanitize
            max_length: Maximum length (None = no truncation)
            newline_replacement: String to replace newlines with

        Returns:
            str: Sanitized text suitable for single-line output
        """
        # Replace various newline formats
        sanitized = text.replace('\r\n', newline_replacement)
        sanitized = sanitized.replace('\r', newline_replacement)
        sanitized = sanitized.replace('\n', newline_replacement)

        # Replace tabs with single space
        sanitized = sanitized.replace('\t', ' ')

        # Collapse multiple spaces
        sanitized = ' '.join(sanitized.split())

        # Truncate if needed
        if max_length and len(sanitized) > max_length:
            sanitized = sanitized[:max_length - 3] + '...'

        return sanitized

    def log_send_attempt(self, alert: Dict[str, Any], success: bool, error: Optional[str] = None):
        """
        Log pathway send attempt

        Args:
            alert: Alert that was sent
            success: Whether send was successful
            error: Error message if send failed
        """
        if not success:
            alert_id = f"{alert.get('level', 'unknown')}/{alert.get('title', 'unknown')}"
            error_msg = f" - {error}" if error else ""
            self.logger.error(f"Failed to send alert {alert_id} via {self.name}{error_msg}")
    
    def should_send_alert(self, alert: Dict[str, Any]) -> bool:
        """
        Check if alert should be sent based on pathway-specific filters
        
        Args:
            alert: Alert dictionary
            
        Returns:
            bool: True if alert should be sent, False otherwise
        """
        if not self.is_enabled():
            return False
        
        # Check level filters
        level_filter = self.config.get('level_filter')
        if level_filter and alert.get('level') not in level_filter:
            return False
        
        # Check category filters
        category_filter = self.config.get('category_filter')
        if category_filter and alert.get('category') not in category_filter:
            return False
        
        # Check source filters
        source_filter = self.config.get('source_filter')
        if source_filter and alert.get('source') not in source_filter:
            return False

        # Check client filters
        client_filter = self.config.get('client_filter')
        if client_filter and alert.get('client', '') not in client_filter:
            return False

        return True