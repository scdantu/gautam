"""Errors raised by Project Gautam.

Every error names the bad value and the fix.
"""


class GautamError(Exception):
    """Base class for all Project Gautam errors."""


class SettingsError(GautamError):
    """A setting is unknown or has an invalid value."""


class GraphError(GautamError):
    """The citation graph cannot do what was asked (e.g. not built yet)."""
