"""The smoke consumer's models: a tenant and one tenant-scoped model.

Deliberately small, and deliberately built the way the documentation tells a
consumer to build: a concrete tenant inheriting ``AbstractTenant`` and named
by ``BOUNDARY_TENANT_MODEL``, and a scoped model inheriting ``TenantMixin``
so it picks up the tenant FK, ``TenantManager`` and ``unscoped``. A fixture
that used boundary any other way would prove nothing about the way consumers
actually use it (ADR-027: "It must use the package the way real consumers
do, or it tests nothing").
"""

from django.db import models

from boundary.models import AbstractTenant, TenantMixin


class Organisation(AbstractTenant):
    """The consumer's own tenant model, pointed at by BOUNDARY_TENANT_MODEL."""


class Booking(TenantMixin):
    """One tenant-scoped model, carrying the RLS migration in 0002."""

    reference = models.CharField(max_length=50)
    seats = models.PositiveIntegerField(default=1)

    def __str__(self):
        return self.reference
