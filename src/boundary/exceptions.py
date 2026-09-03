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


class AdminBypassNotActiveError(BoundaryError):
    """admin_bypass() set the admin flag but a read-back found it not active.

    Raised instead of silently proceeding (issue #37). The transaction-local
    form of set_config() only has scope for the lifetime of the surrounding
    transaction; with no active transaction and BOUNDARY_WRAP_ATOMIC=False,
    the setting vanishes before the read-back statement runs, so a caller
    that assumed the bypass was active would silently keep operating under
    ordinary tenant isolation instead. That failure is not self-announcing:
    a maintenance script would see filtered or zero rows and either produce
    wrong output or raise TenantNotSetError somewhere downstream, with no
    indication the cause was a bypass that never took effect. Verifying the
    postcondition and raising here surfaces the misconfiguration at the
    point of entry instead.
    """
