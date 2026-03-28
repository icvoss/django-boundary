"""Regional database routing for multi-region deployments.

Routes queries for TenantModel subclasses to the database alias
corresponding to the active tenant's region. Non-tenant models
always route to 'default'.

Activate by configuring BOUNDARY_REGIONS and adding
'boundary.routing.RegionalRouter' to DATABASE_ROUTERS.
"""

import logging
from contextlib import contextmanager
from contextvars import ContextVar

from boundary.conf import boundary_settings
from boundary.context import TenantContext
from boundary.models import TenantMixin

logger = logging.getLogger("boundary.routing")

_region_override: ContextVar[str | None] = ContextVar("boundary_region_override", default=None)


class RegionalRouter:
    """Django database router that routes tenant-scoped queries by region.

    Falls back to 'default' when:
    - BOUNDARY_REGIONS is not configured
    - No tenant is active in context
    - Tenant's region is not in BOUNDARY_REGIONS
    - Model is not a TenantModel subclass
    """

    def db_for_read(self, model, **hints):
        return self._route(model)

    def db_for_write(self, model, **hints):
        return self._route(model)

    def allow_relation(self, obj1, obj2, **hints):
        # Allow relations between objects in the same database
        return None

    def allow_migrate(self, db, app_label, model_name=None, **hints):
        # Allow migrations on all databases
        return None

    def _route(self, model):
        regions = boundary_settings.REGIONS
        if not regions:
            return "default"

        # Check for explicit region override (specific_region context manager)
        override = _region_override.get()
        if override:
            if override in regions:
                return override
            return "default"

        # Non-tenant models always go to default
        if not (isinstance(model, type) and issubclass(model, TenantMixin)):
            return "default"

        tenant = TenantContext.get()
        if tenant is None:
            return "default"

        region_field = boundary_settings.REGION_FIELD
        region = getattr(tenant, region_field, None)
        if not region or region not in regions:
            logger.debug(
                "Region not in BOUNDARY_REGIONS, falling back to default",
                extra={"tenant_id": str(tenant.pk), "region": region},
            )
            return "default"

        logger.debug(
            "Routing to region",
            extra={"tenant_id": str(tenant.pk), "region": region},
        )
        return region


@contextmanager
def all_regions():
    """Context manager that yields all configured region aliases.

    Usage::

        with all_regions() as aliases:
            for alias in aliases:
                count = Booking.objects.using(alias).count()
    """
    regions = boundary_settings.REGIONS
    if not regions:
        yield ["default"]
    else:
        yield list(regions.keys())


@contextmanager
def specific_region(region_key):
    """Pin all queries to a specific region, ignoring the active tenant's region.

    Usage::

        with specific_region('eu-west'):
            bookings = Booking.objects.all()  # hits eu-west DB
    """
    token = _region_override.set(region_key)
    try:
        yield region_key
    finally:
        _region_override.reset(token)
