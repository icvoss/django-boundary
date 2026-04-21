"""Tests for configurable FK field name (make_tenant_mixin) and model registry."""

import pytest

from boundary.exceptions import TenantNotSetError
from boundary.models import get_tenant_fk_field, is_tenant_model
from boundary.testing import set_tenant


@pytest.mark.django_db
class TestMakeTenantMixinFiltering:
    """Auto-filtering works with a custom FK field name."""

    def test_only_active_tenant_products_returned(self, tenant_a, tenant_b):
        from boundary_testapp.models import Product

        with set_tenant(tenant_a):
            Product.objects.create(sku="A1")
            Product.objects.create(sku="A2")

        with set_tenant(tenant_b):
            Product.objects.create(sku="B1")

        with set_tenant(tenant_a):
            assert Product.objects.count() == 2

        with set_tenant(tenant_b):
            assert Product.objects.count() == 1

    def test_strict_mode_raises(self, tenant_a, settings):
        from boundary_testapp.models import Product

        settings.BOUNDARY_STRICT_MODE = True
        with set_tenant(tenant_a):
            Product.objects.create(sku="A1")

        with pytest.raises(TenantNotSetError):
            Product.objects.count()

    def test_unscoped_returns_all(self, tenant_a, tenant_b):
        from boundary_testapp.models import Product

        with set_tenant(tenant_a):
            Product.objects.create(sku="A1")
        with set_tenant(tenant_b):
            Product.objects.create(sku="B1")

        with set_tenant(tenant_a):
            assert Product.unscoped.count() == 2


@pytest.mark.django_db
class TestMakeTenantMixinAutoPopulate:
    """Auto-populate works with custom FK field name."""

    def test_auto_populate_on_create(self, tenant_a):
        from boundary_testapp.models import Product

        with set_tenant(tenant_a):
            product = Product.objects.create(sku="A1")
            assert product.merchant == tenant_a
            assert product.merchant_id == tenant_a.pk

    def test_explicit_tenant_not_overridden(self, tenant_a, tenant_b):
        from boundary_testapp.models import Product

        with set_tenant(tenant_a):
            product = Product.objects.create(sku="A1", merchant=tenant_b)
            assert product.merchant == tenant_b

    def test_bulk_create_auto_populates(self, tenant_a):
        from boundary_testapp.models import Product

        with set_tenant(tenant_a):
            products = Product.objects.bulk_create([Product(sku="A1"), Product(sku="A2")])
            assert all(p.merchant == tenant_a for p in products)

    def test_bulk_update_rejects_cross_tenant(self, tenant_a, tenant_b):
        from boundary_testapp.models import Product

        with set_tenant(tenant_b):
            product_b = Product.objects.create(sku="B1")

        with set_tenant(tenant_a):
            product_b.sku = "B1-updated"
            with pytest.raises(ValueError, match="Cross-tenant"):
                Product.objects.bulk_update([product_b], ["sku"])


@pytest.mark.django_db
class TestModelRegistry:
    """is_tenant_model() and get_tenant_fk_field() work for both patterns."""

    def test_standard_mixin_registered(self):
        from boundary_testapp.models import Booking

        assert is_tenant_model(Booking)
        assert get_tenant_fk_field(Booking) == "tenant"

    def test_custom_fk_registered(self):
        from boundary_testapp.models import Product

        assert is_tenant_model(Product)
        assert get_tenant_fk_field(Product) == "merchant"

    def test_non_tenant_model_not_registered(self):
        from boundary_testapp.models import Tenant

        assert not is_tenant_model(Tenant)
        assert get_tenant_fk_field(Tenant) is None


@pytest.mark.django_db
class TestCustomFKFieldName:
    """The FK field on the model has the correct name."""

    def test_field_name_is_merchant(self):
        from boundary_testapp.models import Product

        field_names = [f.name for f in Product._meta.get_fields()]
        assert "merchant" in field_names
        assert "tenant" not in field_names

    def test_merchant_id_column(self):
        from boundary_testapp.models import Product

        fk = Product._meta.get_field("merchant")
        assert fk.column == "merchant_id"
