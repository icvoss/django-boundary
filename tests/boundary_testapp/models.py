"""Concrete test models for boundary's own test suite."""

from django.conf import settings
from django.db import models

from boundary.models import (
    AbstractTenant,
    TenantManager,
    TenantModel,
    TenantQuerySet,
    make_tenant_mixin,
    make_tenant_path_mixin,
    tenant_unique,
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


class PaidCourtQuerySet(TenantQuerySet):
    """Distinctive custom queryset method for the from_queryset() coverage
    (issue #29): TenantManager.get_queryset() must return an instance of
    the queryset class TenantManager.from_queryset() was built from, not
    a hardcoded TenantQuerySet, or a generated manager method calling this
    method on the get_queryset() result raises AttributeError.
    """

    def paid(self):
        return self.filter(is_paid=True)


class BookingWithCustomQuerySet(TenantModel):
    """Tenant-scoped model whose manager is built via
    TenantManager.from_queryset(PaidCourtQuerySet), covering issue #29.
    """

    court = models.IntegerField()
    is_paid = models.BooleanField(default=False)

    objects = TenantManager.from_queryset(PaidCourtQuerySet)()

    class Meta:
        app_label = "boundary_testapp"


class Invoice(TenantModel):
    """Tenant-scoped model with a direct FK to another tenant-scoped model,
    a self-referential FK, and a FK to a non-tenant model (issue #39,
    cross-tenant FK validation fixtures).
    """

    booking = models.ForeignKey(Booking, on_delete=models.CASCADE, null=True, blank=True)
    corrected_by = models.ForeignKey(
        "self",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="corrections",
    )
    issued_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
    )
    amount = models.IntegerField(default=0)

    class Meta:
        app_label = "boundary_testapp"


# ── Custom FK field name models (for make_tenant_mixin tests) ──

MerchantMixin = make_tenant_mixin("merchant")


# ── Indirect / traversal-scoped models (make_tenant_path_mixin) ──


class Brand(MerchantMixin):
    """Direct-FK parent that path-scoped models reach the tenant through."""

    name = models.CharField(max_length=100)

    class Meta:
        app_label = "boundary_testapp"


class Product(MerchantMixin):
    """Model using a custom FK field name via make_tenant_mixin.

    Carries a direct FK to another make_tenant_mixin()-based model
    (Brand) so the cross-tenant FK validation (issue #39) has a fixture
    proving the make_tenant_mixin() path gets the same clean() coverage
    as TenantMixin.
    """

    sku = models.CharField(max_length=50)
    brand = models.ForeignKey(Brand, on_delete=models.CASCADE, null=True, blank=True)

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


# -- Per-tenant uniqueness fixtures (BR-ORM-015, AC-ORM-016) --


class UniqueDoc(TenantModel):
    """TenantMixin-flavoured model carrying tenant_unique("slug").

    The auto-derived name is exercised here; UniqueLabel below exercises an
    explicit one, so both halves of the naming rule have a concrete model.
    """

    slug = models.CharField(max_length=50)

    class Meta:
        app_label = "boundary_testapp"
        constraints = [tenant_unique("slug")]


class UniqueLabel(TenantModel):
    """Two tenant_unique() calls on one model, one explicitly named.

    Proves the two auto-derived names on different field lists do not
    collide, and that an explicit name is used verbatim.
    """

    slug = models.CharField(max_length=50)
    year = models.IntegerField(default=2026)

    class Meta:
        app_label = "boundary_testapp"
        constraints = [
            tenant_unique("slug"),
            tenant_unique("slug", "year", name="boundary_testapp_uniquelabel_slug_year"),
        ]


class UniqueProduct(MerchantMixin):
    """make_tenant_mixin("merchant") model carrying tenant_unique("slug").

    The point of the fixture: the helper must resolve to ("merchant", "slug")
    without the author naming the field, which is what would break if the
    tenant field were hardcoded to "tenant" or read at call time.
    """

    slug = models.CharField(max_length=50)

    class Meta:
        app_label = "boundary_testapp"
        constraints = [tenant_unique("slug")]


# -- boundary.W010 fixtures (BR-ORM-015, AC-ORM-017) --


class GlobalUniqueDoc(TenantModel):
    """TenantMixin model with field-level unique=True: W010 must warn."""

    slug = models.CharField(max_length=50, unique=True)

    class Meta:
        app_label = "boundary_testapp"


class OptedOutUniqueDoc(TenantModel):
    """Genuinely global uniqueness, opted out of W010 via class attribute."""

    boundary_global_unique = ["token"]

    token = models.CharField(max_length=64, unique=True)

    class Meta:
        app_label = "boundary_testapp"


class GlobalUniqueProduct(MerchantMixin):
    """make_tenant_mixin("merchant") model with unique=True: W010 must warn
    and the hint must name merchant, not tenant.
    """

    sku = models.CharField(max_length=50, unique=True)

    class Meta:
        app_label = "boundary_testapp"


class GlobalTogetherDoc(TenantModel):
    """unique_together without the tenant FK: W010 must warn."""

    code = models.CharField(max_length=20)
    year = models.IntegerField()

    class Meta:
        app_label = "boundary_testapp"
        unique_together = [("code", "year")]


class TenantTogetherDoc(TenantModel):
    """unique_together that already includes the tenant FK: no W010."""

    code = models.CharField(max_length=20)

    class Meta:
        app_label = "boundary_testapp"
        unique_together = [("tenant", "code")]


class GlobalConstraintDoc(TenantModel):
    """Hand-written UniqueConstraint omitting the tenant FK: W010 must warn."""

    reference = models.CharField(max_length=32)

    class Meta:
        app_label = "boundary_testapp"
        constraints = [
            models.UniqueConstraint(
                fields=["reference"],
                name="boundary_testapp_globalconstraintdoc_reference",
            )
        ]


class OptedOutTogetherDoc(TenantModel):
    """unique_together opted out field-by-field via boundary_global_unique."""

    boundary_global_unique = ["code", "year"]

    code = models.CharField(max_length=20)
    year = models.IntegerField()

    class Meta:
        app_label = "boundary_testapp"
        unique_together = [("code", "year")]
