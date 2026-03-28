"""Concrete test models for boundary's own test suite."""

from django.db import models

from boundary.models import AbstractTenant, TenantModel


class Tenant(AbstractTenant):
    """Concrete tenant model for tests."""

    class Meta:
        app_label = "boundary_testapp"


class Booking(TenantModel):
    """Concrete tenant-scoped model for tests."""

    court = models.IntegerField()
    is_paid = models.BooleanField(default=False)

    class Meta:
        app_label = "boundary_testapp"
