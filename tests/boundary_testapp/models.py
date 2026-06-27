"""Concrete test models for boundary's own test suite."""

from django.db import models

from boundary.models import (
    AbstractTenant,
    TenantModel,
    make_tenant_mixin,
    make_tenant_path_mixin,
)


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


# ── Indirect / traversal-scoped models (make_tenant_path_mixin) ──


class Brand(MerchantMixin):
    """Direct-FK parent that path-scoped models reach the tenant through."""

    name = models.CharField(max_length=100)

    class Meta:
        app_label = "boundary_testapp"


BrandAssetMixin = make_tenant_path_mixin("brand__merchant")


class BrandAsset(BrandAssetMixin):
    """Single-hop path-scoped model (brand__merchant). No own tenant column."""

    brand = models.ForeignKey(Brand, on_delete=models.CASCADE)
    label = models.CharField(max_length=100)

    class Meta:
        app_label = "boundary_testapp"


AssetVariantMixin = make_tenant_path_mixin("asset__brand__merchant")


class AssetVariant(AssetVariantMixin):
    """Multi-hop path-scoped model (asset__brand__merchant)."""

    asset = models.ForeignKey(BrandAsset, on_delete=models.CASCADE)
    fmt = models.CharField(max_length=20)

    class Meta:
        app_label = "boundary_testapp"
