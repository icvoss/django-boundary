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
from boundary.exceptions import RegionNotConfiguredError
from boundary.models import is_tenant_model

logger = logging.getLogger("boundary.routing")

_region_override: ContextVar[str | None] = ContextVar("boundary_region_override", default=None)

# Warn-once cache for the "tenant has a region, but it is not in
# BOUNDARY_REGIONS" fallback (issue #59). _route() runs on every ORM query,
# so an unconditional warning would flood; this is usually configuration
# drift (a decommissioned region, a bad write), not an expected routing
# case, so it deserves one warning per (tenant, region) pair rather than
# silence. Bounded so a process that churns through many distinct
# (tenant, region) pairs cannot grow this without limit: past the cap,
# further distinct pairs stop producing warnings for the rest of the
# process's life (a broad reset rather than a precise LRU, judged
# acceptable because the condition is a one-off config fix, not an ongoing
# per-tenant signal).
_MAX_UNMATCHED_REGION_WARNINGS = 10_000
_warned_unmatched_regions: set[tuple[str, str | None]] = set()


def _warn_unmatched_region_once(tenant_pk: str, region: str | None) -> None:
    """Log a warning for an unmatched tenant region, at most once per pair."""
    key = (tenant_pk, region)
    if key in _warned_unmatched_regions:
        return
    if len(_warned_unmatched_regions) >= _MAX_UNMATCHED_REGION_WARNINGS:
        _warned_unmatched_regions.clear()
    _warned_unmatched_regions.add(key)
    logger.warning(
        "Tenant region not in BOUNDARY_REGIONS, falling back to default",
        extra={"tenant_id": tenant_pk, "region": region},
    )


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
        if not (isinstance(model, type) and is_tenant_model(model)):
            return "default"

        tenant = TenantContext.get()
        if tenant is None:
            return "default"

        region_field = boundary_settings.REGION_FIELD
        region = getattr(tenant, region_field, None)
        if not region or region not in regions:
            # Keep the debug line for full-detail tracing, plus a warning
            # (issue #59) so a misconfigured tenant is operator-visible even
            # when debug logging is off, without flooding on every query.
            logger.debug(
                "Region not in BOUNDARY_REGIONS, falling back to default",
                extra={"tenant_id": str(tenant.pk), "region": region},
            )
            _warn_unmatched_region_once(str(tenant.pk), region)
            return "default"

        logger.debug(
            "Routing to region",
            extra={"tenant_id": str(tenant.pk), "region": region},
        )
        return region


def require_region(tenant=None):
    """Return the database alias for ``tenant`` or raise if it is not routable.

    Unlike :class:`RegionalRouter`, which silently falls back to ``"default"``
    (Django routers must always return an alias), this helper fails loudly when
    a tenant's region is missing or not present in ``BOUNDARY_REGIONS``. Use it
    where silent fallback to ``"default"`` would be a correctness or data
    residency problem, for example when provisioning a tenant or before a
    cross-region batch job.

    :param tenant: the tenant to check. Defaults to the active tenant in context.
    :raises RegionNotConfiguredError: if regions are unconfigured, no tenant is
        active, or the tenant's region is not in ``BOUNDARY_REGIONS``.
    :returns: the region alias the tenant routes to.
    """
    regions = boundary_settings.REGIONS
    if not regions:
        raise RegionNotConfiguredError("BOUNDARY_REGIONS is not configured; no regional routing is active.")

    if tenant is None:
        tenant = TenantContext.get()
    if tenant is None:
        raise RegionNotConfiguredError("No tenant is active in context, so its region cannot be resolved.")

    region_field = boundary_settings.REGION_FIELD
    region = getattr(tenant, region_field, None)
    if not region or region not in regions:
        raise RegionNotConfiguredError(
            f"Tenant {tenant.pk!r} has region {region!r}, which is not in BOUNDARY_REGIONS ({sorted(regions)})."
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
