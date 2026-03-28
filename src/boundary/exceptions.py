"""Boundary exception hierarchy.

All exceptions inherit from BoundaryError so consuming code can catch
the entire family with a single except clause.
"""


class BoundaryError(Exception):
    """Base exception for all boundary errors."""


class TenantNotSetError(BoundaryError):
    """No tenant is active in context and STRICT_MODE is True."""


class TenantResolutionError(BoundaryError):
    """A resolver raised an unexpected exception during resolution."""


class TenantInactiveError(BoundaryError):
    """The resolved tenant has is_active=False."""


class TenantNotFoundError(BoundaryError):
    """A Celery task header references a tenant UUID that no longer exists."""


class RegionNotConfiguredError(BoundaryError):
    """The active tenant's region is not present in BOUNDARY_REGIONS."""
