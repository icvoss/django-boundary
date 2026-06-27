"""Tests for tenant injection into get_or_create / update_or_create.

The lookup half is scoped to the active tenant (so a get cannot match another
tenant's row) and the create half stamps the FK — without the caller having to
pass it. Path-scoped models are left untouched (no column to write).
"""

import pytest

from boundary.exceptions import TenantNotSetError
from boundary.testing import set_tenant


@pytest.mark.django_db
class TestGetOrCreateInjection:
    def test_create_branch_stamps_fk(self, tenant_a):
        from boundary_testapp.models import Product

        with set_tenant(tenant_a):
            product, created = Product.objects.get_or_create(sku="A1")
            assert created
            assert product.merchant == tenant_a

    def test_get_branch_is_scoped(self, tenant_a, tenant_b):
        """A row created for tenant_b with the same lookup must NOT be returned
        to tenant_a — get_or_create should create a fresh tenant_a row."""
        from boundary_testapp.models import Product

        with set_tenant(tenant_b):
            Product.objects.create(sku="SHARED")

        with set_tenant(tenant_a):
            product, created = Product.objects.get_or_create(sku="SHARED")
            assert created  # did not match tenant_b's row
            assert product.merchant == tenant_a

        # Two distinct rows now exist, one per tenant
        assert Product.unscoped.filter(sku="SHARED").count() == 2

    def test_existing_row_is_returned(self, tenant_a):
        from boundary_testapp.models import Product

        with set_tenant(tenant_a):
            first, c1 = Product.objects.get_or_create(sku="A1")
            second, c2 = Product.objects.get_or_create(sku="A1")
            assert c1 and not c2
            assert first.pk == second.pk

    def test_explicit_fk_respected(self, tenant_a, tenant_b):
        from boundary_testapp.models import Product

        with set_tenant(tenant_a):
            product, created = Product.objects.get_or_create(sku="A1", merchant=tenant_b)
            assert created
            assert product.merchant == tenant_b

    def test_no_context_raises_strict(self, settings):
        from boundary_testapp.models import Product

        settings.BOUNDARY_STRICT_MODE = True
        with pytest.raises(TenantNotSetError):
            Product.objects.get_or_create(sku="A1")


@pytest.mark.django_db
class TestUpdateOrCreateInjection:
    def test_create_branch_stamps_fk(self, tenant_a):
        from boundary_testapp.models import Product

        with set_tenant(tenant_a):
            product, created = Product.objects.update_or_create(sku="A1", defaults={})
            assert created
            assert product.merchant == tenant_a

    def test_update_branch_is_scoped(self, tenant_a, tenant_b):
        """update_or_create must not update another tenant's matching row."""
        from boundary_testapp.models import Product

        with set_tenant(tenant_b):
            b_row = Product.objects.create(sku="SHARED")

        with set_tenant(tenant_a):
            product, created = Product.objects.update_or_create(sku="SHARED")
            assert created
            assert product.merchant == tenant_a
            assert product.pk != b_row.pk

    def test_create_defaults_stamps_fk(self, tenant_a):
        """Django 5.0+ create_defaults path is also scoped."""
        from boundary_testapp.models import Product

        with set_tenant(tenant_a):
            product, created = Product.objects.update_or_create(
                sku="A1", create_defaults={"sku": "A1"}, defaults={"sku": "A1"}
            )
            assert created
            assert product.merchant == tenant_a


@pytest.mark.django_db
class TestPathModelInjectionNoop:
    """Path-scoped models have no column — injection is a no-op and relies on
    the auto-filtered queryset."""

    def test_get_or_create_path_model(self, tenant_a, tenant_b):
        from boundary_testapp.models import Brand, BrandAsset

        with set_tenant(tenant_a):
            brand_a = Brand.objects.create(name="A")
            asset, created = BrandAsset.objects.get_or_create(brand=brand_a, label="a1")
            assert created
            assert asset.pk is not None
            # No tenant/merchant column was injected
            assert not hasattr(asset, "merchant_id")
