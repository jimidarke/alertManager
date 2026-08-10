"""
OID Translator Utility

Provides MIB-based translation of numeric OIDs to symbolic names.
Uses pysnmp's MIB compiler to load and resolve OID mappings.
"""

import logging
from typing import Optional, Tuple, List
from pysnmp.smi import builder, view, compiler
from pysnmp.smi.error import SmiError


class OIDTranslator:
    """
    Translates numeric OIDs to symbolic names using MIB files.

    Supports:
    - Loading standard MIBs (SNMPv2-MIB, IF-MIB, etc.)
    - Loading vendor-specific MIBs from custom paths
    - Caching translations for performance
    - Graceful fallback to numeric representation
    """

    def __init__(
        self,
        mib_paths: Optional[List[str]] = None,
        load_modules: Optional[List[str]] = None,
        cache_enabled: bool = True
    ):
        """
        Initialize OID translator with MIB sources.

        Args:
            mib_paths: List of directories containing MIB files
            load_modules: List of MIB module names to preload
            cache_enabled: Enable translation cache for performance
        """
        self.logger = logging.getLogger(self.__class__.__name__)
        self.cache_enabled = cache_enabled
        self.translation_cache = {}

        # Initialize MIB builder
        self.mibBuilder = builder.MibBuilder()

        # Add MIB sources if provided
        if mib_paths:
            try:
                compiler.addMibCompiler(
                    self.mibBuilder,
                    sources=mib_paths
                )
                self.logger.info(f"Added MIB compiler sources: {mib_paths}")
            except Exception as e:
                self.logger.warning(f"Failed to add MIB compiler sources: {e}")

        # Load specified MIB modules
        if load_modules:
            for module in load_modules:
                try:
                    self.mibBuilder.loadModules(module)
                    self.logger.info(f"Loaded MIB module: {module}")
                except Exception as e:
                    self.logger.warning(f"Failed to load MIB module {module}: {e}")

        # Initialize MIB view controller for OID lookups
        self.mibView = view.MibViewController(self.mibBuilder)

    def oid_to_symbolic(self, oid: str) -> str:
        """
        Convert numeric OID string to symbolic name.

        Args:
            oid: Numeric OID string (e.g., "1.3.6.1.2.1.1.1.0")

        Returns:
            Symbolic name (e.g., "SNMPv2-MIB::sysDescr.0") or numeric OID if translation fails
        """
        # Check cache
        if self.cache_enabled and oid in self.translation_cache:
            return self.translation_cache[oid]

        # Convert string OID to tuple
        try:
            oid_tuple = tuple(int(x) for x in oid.split('.') if x)
        except ValueError:
            self.logger.warning(f"Invalid OID format: {oid}")
            return oid

        # Translate
        symbolic = self._translate_oid_tuple(oid_tuple)

        # Cache result
        if self.cache_enabled:
            self.translation_cache[oid] = symbolic

        return symbolic

    def _translate_oid_tuple(self, oid_tuple: Tuple[int, ...]) -> str:
        """
        Internal method to translate OID tuple to symbolic name.

        Args:
            oid_tuple: OID as tuple of integers

        Returns:
            Symbolic name or numeric OID string
        """
        try:
            # Get MIB node location
            modName, symName, suffix = self.mibView.getNodeLocation(oid_tuple)

            # Build symbolic representation
            symbolic = f"{modName}::{symName}"

            # Append suffix if present (e.g., table indices)
            if suffix:
                symbolic += '.' + '.'.join(map(str, suffix))

            return symbolic

        except SmiError:
            # MIB not loaded or OID not found
            return '.'.join(map(str, oid_tuple))
        except Exception as e:
            self.logger.debug(f"Failed to translate OID {oid_tuple}: {e}")
            return '.'.join(map(str, oid_tuple))

    def varbind_to_dict(self, oid: str, value: any, value_type: str = 'unknown') -> dict:
        """
        Convert varbind to dictionary with both numeric and symbolic representations.

        Args:
            oid: Numeric OID string
            value: Varbind value
            value_type: Value type (integer, string, etc.)

        Returns:
            Dictionary with oid, symbolic, value, and type fields
        """
        return {
            'oid': oid,
            'symbolic': self.oid_to_symbolic(oid),
            'value': value,
            'type': value_type
        }

    def load_additional_mib(self, module_name: str) -> bool:
        """
        Load an additional MIB module at runtime.

        Args:
            module_name: Name of MIB module to load

        Returns:
            True if successful, False otherwise
        """
        try:
            self.mibBuilder.loadModules(module_name)
            self.logger.info(f"Dynamically loaded MIB module: {module_name}")
            return True
        except Exception as e:
            self.logger.error(f"Failed to load MIB module {module_name}: {e}")
            return False

    def clear_cache(self):
        """Clear the translation cache."""
        self.translation_cache.clear()
        self.logger.debug("Translation cache cleared")

    def get_cache_stats(self) -> dict:
        """
        Get cache statistics.

        Returns:
            Dictionary with cache size and status
        """
        return {
            'enabled': self.cache_enabled,
            'size': len(self.translation_cache),
            'entries': list(self.translation_cache.keys())[:10]  # First 10 entries
        }
