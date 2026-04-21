"""Concrete test models for boundary's own test suite."""

from django.db import models

from boundary.models import AbstractTenant, TenantModel, make_tenant_mixin


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


# ── Custom FK field name models (for make_tenant_mixin tests) ──

MerchantMixin = make_tenant_mixin("merchant")


class Product(MerchantMixin):
    """Model using a custom FK field name via make_tenant_mixin."""

    sku = models.CharField(max_length=50)

    class Meta:
        app_label = "boundary_testapp"
