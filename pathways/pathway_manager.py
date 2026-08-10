"""
Pathway Manager

Manages loading, configuration, and execution of alert pathways.
"""

import logging
import importlib
from typing import Dict, Any, List, Optional
from .base_pathway import BasePathway


class PathwayManager:
    """Manages alert pathway instances and routing"""
    
    def __init__(self):
        self.pathways: Dict[str, BasePathway] = {}
        self.logger = logging.getLogger("pathway_manager")
    
    def load_pathways(self, pathway_configs: Dict[str, Any]) -> bool:
        """
        Load pathway instances from configuration
        
        Args:
            pathway_configs: Dictionary of pathway configurations
                Format: {pathway_name: {type: 'file|email|...', ...config...}}
        
        Returns:
            bool: True if all pathways loaded successfully
        """
        success = True
        
        for pathway_name, config in pathway_configs.items():
            try:
                if self.load_pathway(pathway_name, config):
                    self.logger.info(f"Loaded pathway: {pathway_name}")
                else:
                    self.logger.error(f"Failed to load pathway: {pathway_name}")
                    success = False
            except Exception as e:
                self.logger.error(f"Error loading pathway {pathway_name}: {e}")
                success = False
        
        return success
    
    def load_pathway(self, name: str, config: Dict[str, Any]) -> bool:
        """
        Load a single pathway instance
        
        Args:
            name: Pathway instance name
            config: Pathway configuration including 'type' field
        
        Returns:
            bool: True if pathway loaded successfully
        """
        pathway_type = config.get('type')
        if not pathway_type:
            self.logger.error(f"No pathway type specified for {name}")
            return False
        
        try:
            # Get pathway class
            pathway_class = self._get_pathway_class(pathway_type)
            if not pathway_class:
                return False
            
            # Create pathway instance
            pathway = pathway_class(name, config)
            
            # Validate configuration
            if not pathway.validate_config():
                self.logger.error(f"Invalid configuration for pathway {name}")
                return False
            
            # Store pathway
            self.pathways[name] = pathway
            return True
            
        except Exception as e:
            self.logger.error(f"Error creating pathway {name}: {e}")
            return False
    
    def _get_pathway_class(self, pathway_type: str) -> Optional[type]:
        """Get pathway class by type name"""
        pathway_classes = {
            'file': 'FilePathway',
            'email': 'EmailPathway',
            'console': 'ConsolePathway',
            'mqtt': 'MQTTPathway',
            'led_sign': 'LEDSignPathway',
        }

        class_name = pathway_classes.get(pathway_type.lower())
        if not class_name:
            self.logger.error(f"Unknown pathway type: {pathway_type}")
            return None

        try:
            # Import the module - handle special naming cases
            if pathway_type.lower() == 'led_sign':
                module_name = "pathways.led_sign_pathway"
            else:
                module_name = f"pathways.{pathway_type.lower()}_pathway"

            module = importlib.import_module(module_name)

            # Get the class
            pathway_class = getattr(module, class_name)
            return pathway_class

        except (ImportError, AttributeError) as e:
            if pathway_type.lower() == 'email':
                self.logger.warning(f"Email pathway not available - email dependencies may be missing: {e}")
            else:
                self.logger.error(f"Cannot import pathway class {class_name}: {e}")
            return None
    
    def send_alert(self, pathway_name: str, alert: Dict[str, Any], pathway_specific_config: Dict[str, Any] = None) -> bool:
        """
        Send alert via specific pathway

        Args:
            pathway_name: Name of pathway to use
            alert: Alert data
            pathway_specific_config: Pathway-specific configuration overrides

        Returns:
            bool: True if alert sent successfully
        """
        if pathway_name not in self.pathways:
            self.logger.error(f"Unknown pathway: {pathway_name}")
            return False

        pathway = self.pathways[pathway_name]
        return pathway.send_alert(alert, pathway_specific_config or {})
    
    def send_to_multiple(self, pathway_names: List[str], alert: Dict[str, Any], pathway_config: Dict[str, Any] = None) -> Dict[str, bool]:
        """
        Send alert to multiple pathways

        Args:
            pathway_names: List of pathway names
            alert: Alert data
            pathway_config: Pathway-specific configurations

        Returns:
            Dict[str, bool]: Results for each pathway {pathway_name: success}
        """
        results = {}
        if pathway_config is None:
            pathway_config = {}

        for pathway_name in pathway_names:
            # Pass pathway-specific config to the individual pathway
            specific_config = pathway_config.get(pathway_name, {})
            results[pathway_name] = self.send_alert(pathway_name, alert, specific_config)
        
        return results
    
    def get_pathway_names(self) -> List[str]:
        """Get list of loaded pathway names"""
        return list(self.pathways.keys())
    
    def get_enabled_pathways(self) -> List[str]:
        """Get list of enabled pathway names"""
        return [name for name, pathway in self.pathways.items() if pathway.is_enabled()]

    def get_async_errors(self) -> Dict[str, int]:
        """
        Get async-error counts for all loaded pathways.

        See BasePathway.async_errors / increment_async_errors() -- these
        are errors from background/async work that doesn't flow back
        through a normal send_alert() return value (e.g. EmailPathway's
        storm-prevention timer-thread flush).

        Returns:
            Dict[str, int]: {pathway_name: async_error_count}, one entry
            per loaded pathway (0 for pathways that have never
            incremented it).
        """
        return {name: pathway.async_errors for name, pathway in self.pathways.items()}
    
    def pathway_exists(self, name: str) -> bool:
        """Check if pathway exists"""
        return name in self.pathways
    
    def is_pathway_enabled(self, name: str) -> bool:
        """Check if pathway is enabled"""
        if name not in self.pathways:
            return False
        return self.pathways[name].is_enabled()
    
    def reload_pathway(self, name: str, config: Dict[str, Any]) -> bool:
        """
        Reload a pathway with new configuration
        
        Args:
            name: Pathway name
            config: New configuration
        
        Returns:
            bool: True if reloaded successfully
        """
        # Remove existing pathway
        if name in self.pathways:
            del self.pathways[name]
        
        # Load new pathway
        return self.load_pathway(name, config)
    
    def get_pathway_status(self) -> Dict[str, Dict[str, Any]]:
        """
        Get status information for all pathways

        Returns:
            Dict with pathway status information
        """
        status = {}

        for name, pathway in self.pathways.items():
            status[name] = {
                'type': pathway.config.get('type'),
                'enabled': pathway.is_enabled(),
                'class': pathway.__class__.__name__
            }

        return status

    def shutdown_all(self):
        """
        Gracefully shutdown all pathways.
        Calls shutdown() on pathways that support it (e.g., EmailPathway for storm prevention flush).
        """
        self.logger.info("Shutting down all pathways")

        for name, pathway in self.pathways.items():
            try:
                if hasattr(pathway, 'shutdown') and callable(pathway.shutdown):
                    self.logger.debug(f"Calling shutdown on pathway: {name}")
                    pathway.shutdown()
            except Exception as e:
                self.logger.error(f"Error shutting down pathway {name}: {e}")