"""Tests for cross-tenant FK reference validation (BR-ORM-013, issue #39).

Neither the ORM filter nor RLS catches a tenant-scoped row whose own
tenant_id is correct but whose FK points at another tenant's row (both
layers only ever check the row's OWN tenant column). validate_cross_tenant_fks()
and the TenantMixin.clean() / make_tenant_mixin() clean() hooks that call it
are a full_clean()-only backstop for that gap; see
docs/explanation/isolation-layers.md for what full_clean() paths do and do
not cover.
"""

import pytest
from django.core.exceptions import ValidationError

from boundary.testing import set_tenant


@pytest.mark.django_db
class TestTenantMixinCrossTenantFK:
    """TenantMixin.clean() rejects a cross-tenant FK (Invoice.booking)."""

    def test_cross_tenant_fk_raises_on_full_clean(self, tenant_a, tenant_b):
        from boundary_testapp.models import Booking, Invoice

        with set_tenant(tenant_b):
            booking_b = Booking.objects.create(court=1)

        with set_tenant(tenant_a):
            invoice = Invoice(tenant=tenant_a, booking=booking_b, amount=100)
            with pytest.raises(ValidationError) as exc_info:
                invoice.full_clean()

        assert "booking" in exc_info.value.message_dict
        message = exc_info.value.message_dict["booking"][0]
        assert "booking" in message
        assert str(tenant_b.pk) in message

    def test_same_tenant_fk_does_not_raise(self, tenant_a):
        """Positive control: a correctly same-tenant FK passes full_clean()."""
        from boundary_testapp.models import Booking, Invoice

        with set_tenant(tenant_a):
            booking_a = Booking.objects.create(court=1)
            invoice = Invoice(tenant=tenant_a, booking=booking_a, amount=100)
            invoice.full_clean()  # must not raise

    def test_null_fk_does_not_raise(self, tenant_a):
        from boundary_testapp.models import Invoice

        with set_tenant(tenant_a):
            invoice = Invoice(tenant=tenant_a, booking=None, amount=100)
            invoice.full_clean()  # must not raise

    def test_fk_to_non_tenant_model_does_not_raise(self, tenant_a, django_user_model):
        """A FK to a non-tenant model (auth.User) has no tenant to compare
        and must be skipped without error."""
        from boundary_testapp.models import Invoice

        user = django_user_model.objects.create(username="agent")

        with set_tenant(tenant_a):
            invoice = Invoice(tenant=tenant_a, issued_by=user, amount=100)
            invoice.full_clean()  # must not raise

    def test_self_referential_cross_tenant_fk_raises(self, tenant_a, tenant_b):
        """A self-referential FK (Invoice.corrected_by -> Invoice) is
        validated the same way as any other cross-tenant FK."""
        from boundary_testapp.models import Invoice

        with set_tenant(tenant_b):
            original_b = Invoice.objects.create(tenant=tenant_b, amount=50)

        with set_tenant(tenant_a):
            correction = Invoice(tenant=tenant_a, corrected_by=original_b, amount=-50)
            with pytest.raises(ValidationError) as exc_info:
                correction.full_clean()

        assert "corrected_by" in exc_info.value.message_dict

    def test_self_referential_same_tenant_fk_does_not_raise(self, tenant_a):
        from boundary_testapp.models import Invoice

        with set_tenant(tenant_a):
            original_a = Invoice.objects.create(tenant=tenant_a, amount=50)
            correction = Invoice(tenant=tenant_a, corrected_by=original_a, amount=-50)
            correction.full_clean()  # must not raise

    def test_save_alone_does_not_catch_cross_tenant_fk(self, tenant_a, tenant_b):
        """Documents the residual gap (issue #39 ask 3): clean() only fires
        on full_clean(). save() alone never calls it, so a cross-tenant FK
        assigned via save() is not caught here. This is an intentional
        record of the boundary's limit, not a bug: see
        docs/explanation/isolation-layers.md.
        """
        from boundary_testapp.models import Booking, Invoice

        with set_tenant(tenant_b):
            booking_b = Booking.objects.create(court=1)

        with set_tenant(tenant_a):
            invoice = Invoice.objects.create(tenant=tenant_a, booking=booking_b, amount=100)

        # The row was written successfully, with a cross-tenant reference
        # intact, because save() never calls clean().
        invoice.refresh_from_db()
        assert invoice.booking_id == booking_b.pk


@pytest.mark.django_db
class TestMakeTenantMixinCrossTenantFK:
    """The make_tenant_mixin() clean() gets the same protection (Product.brand)."""

    def test_cross_tenant_fk_raises_on_full_clean(self, tenant_a, tenant_b):
        from boundary_testapp.models import Brand, Product

        with set_tenant(tenant_b):
            brand_b = Brand.objects.create(name="Brand B")

        with set_tenant(tenant_a):
            product = Product(merchant=tenant_a, sku="SKU-1", brand=brand_b)
            with pytest.raises(ValidationError) as exc_info:
                product.full_clean()

        assert "brand" in exc_info.value.message_dict

    def test_same_tenant_fk_does_not_raise(self, tenant_a):
        """Positive control for the make_tenant_mixin() path."""
        from boundary_testapp.models import Brand, Product

        with set_tenant(tenant_a):
            brand_a = Brand.objects.create(name="Brand A")
            product = Product(merchant=tenant_a, sku="SKU-1", brand=brand_a)
            product.full_clean()  # must not raise

    def test_null_fk_does_not_raise(self, tenant_a):
        from boundary_testapp.models import Product

        with set_tenant(tenant_a):
            product = Product(merchant=tenant_a, sku="SKU-1", brand=None)
            product.full_clean()  # must not raise
