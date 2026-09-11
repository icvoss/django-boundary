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


class RLSNotEnforcedError(BoundaryError):
    """assert_rls_enforced() found the connecting role or a tenant table is
    not actually enforcing Row Level Security (issue #55).

    Raised rather than returning a bool so a consumer who calls the
    assertion directly (outside the pytest fixture wrapper) cannot
    accidentally ignore a falsy return value. The message names which of
    the two conditions failed and the value observed, so the run stops
    loudly instead of the suite proceeding to pass every isolation test
    vacuously against a bypassing connection or an unprotected table
    (the same trap boundary.W003 and boundary.E006 diagnose at
    ``manage.py check`` time, but that check never runs under a plain
    ``pytest`` invocation).
    """
