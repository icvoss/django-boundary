"""Test-only middleware subclass.

CustomTenantMiddleware exists solely to prove boundary.E004 matches by
issubclass rather than by dotted-path suffix (issue #52): a consumer
subclass of TenantMiddleware (Magmify's host-scoped
SiteTenantBoundaryMiddleware is the documented-in-the-wild case) still
performs tenant resolution on every request and must satisfy the check even
though its import path is not boundary.middleware.TenantMiddleware.
"""

from boundary.middleware import TenantMiddleware


class CustomTenantMiddleware(TenantMiddleware):
    """A no-op subclass; behaviour is irrelevant, only the base class is."""
