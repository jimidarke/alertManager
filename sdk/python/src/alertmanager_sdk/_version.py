"""
Alert Manager SDK version.

Independent of the server's own root VERSION file / tools/bump-version.sh --
those track the gateway service's release history, which has no bearing on
this package's own API-stability story. This SDK versions itself starting
at 0.1.0 (SemVer's own convention for "public API may still change") and
bumps independently as its own surface stabilizes.
"""

__version__ = "0.1.0"
