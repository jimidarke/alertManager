"""
Console Pathway

Handles outputting alerts to console/stdout.
"""

from typing import Dict, Any
from .base_pathway import BasePathway


class ConsolePathway(BasePathway):
    """Console pathway for printing alerts to stdout"""
    
    def __init__(self, name: str, config: Dict[str, Any]):
        """
        Initialize console pathway
        
        Args:
            name: Pathway instance name
            config: Configuration dictionary containing:
                - format: Message format template (optional)
                - prefix: Optional prefix for console output
        """
        super().__init__(name, config)
        self.prefix = config.get('prefix', 'ALERT:')
    
    def validate_config(self) -> bool:
        """
        Validate console pathway configuration
        
        Returns:
            bool: True (console pathway always valid)
        """
        return True
    
    def send_alert(self, alert: Dict[str, Any], pathway_config: Dict[str, Any] = None) -> bool:
        """
        Print alert to console
        
        Args:
            alert: Alert dictionary
            
        Returns:
            bool: True if alert was printed successfully
        """
        if not self.should_send_alert(alert):
            return False
        
        try:
            message = self.format_message(alert)
            output = f"{self.prefix} {message}"
            print(output)
            
            self.log_send_attempt(alert, True)
            return True
            
        except Exception as e:
            error_msg = f"Error printing to console: {e}"
            self.log_send_attempt(alert, False, error_msg)
            return False