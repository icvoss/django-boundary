"""Tests for boundary.models — ORM layer."""

import pytest

from boundary.exceptions import TenantNotSetError
from boundary.testing import set_tenant


@pytest.mark.django_db
class TestAutomaticFiltering:
    """AC-ORM-001: Automatic filtering by active tenant."""

    def test_only_active_tenant_bookings_returned(self, tenant_a, tenant_b):
        from boundary_testapp.models import Booking

        with set_tenant(tenant_a):
            Booking.objects.create(court=1)
            Booking.objects.create(court=2)

        with set_tenant(tenant_b):
            Booking.objects.create(court=3)

        with set_tenant(tenant_a):
            assert Booking.objects.count() == 2

        with set_tenant(tenant_b):
            assert Booking.objects.count() == 1


@pytest.mark.django_db
class TestStrictMode:
    """AC-ORM-002/003: Strict mode raises; non-strict returns unfiltered."""

    def test_strict_mode_raises(self, tenant_a, settings):
        from boundary_testapp.models import Booking

        settings.BOUNDARY_STRICT_MODE = True
        with set_tenant(tenant_a):
            Booking.objects.create(court=1)

        with pytest.raises(TenantNotSetError):
            Booking.objects.count()

    def test_non_strict_returns_all(self, tenant_a, tenant_b, settings):
        from boundary_testapp.models import Booking

        settings.BOUNDARY_STRICT_MODE = False
        with set_tenant(tenant_a):
            Booking.objects.create(court=1)
        with set_tenant(tenant_b):
            Booking.objects.create(court=2)

        assert Booking.objects.count() == 2


@pytest.mark.django_db
class TestUnscopedManager:
    """AC-ORM-004: Unscoped manager bypasses filtering."""

    def test_unscoped_returns_all(self, tenant_a, tenant_b):
        from boundary_testapp.models import Booking

        with set_tenant(tenant_a):
            Booking.objects.create(court=1)
        with set_tenant(tenant_b):
            Booking.objects.create(court=2)

        with set_tenant(tenant_a):
            assert Booking.unscoped.count() == 2


@pytest.mark.django_db
class TestAutoPopulate:
    """AC-ORM-005/006/007/008: Auto-populate tenant on save."""

    def test_auto_populate_on_create(self, tenant_a):
        from boundary_testapp.models import Booking

        with set_tenant(tenant_a):
            booking = Booking.objects.create(court=1)
            assert booking.tenant == tenant_a

    def test_explicit_tenant_not_overridden(self, tenant_a, tenant_b):
        from boundary_testapp.models import Booking

        with set_tenant(tenant_a):
            booking = Booking.objects.create(court=1, tenant=tenant_b)
            assert booking.tenant == tenant_b

    def test_create_without_context_raises_strict(self, settings):
        from boundary_testapp.models import Booking

        settings.BOUNDARY_STRICT_MODE = True
        with pytest.raises(TenantNotSetError):
            Booking.objects.create(court=1)

    def test_unscoped_create_no_auto_populate(self, tenant_a, tenant_b):
        """AC-ORM-008: Unscoped create with explicit tenant works."""
        from boundary_testapp.models import Booking

        with set_tenant(tenant_a):
            booking = Booking.unscoped.create(court=1, tenant=tenant_b)
            assert booking.tenant == tenant_b

    def test_unscoped_create_without_tenant_raises(self, tenant_a):
        """AC-ORM-008: Unscoped create without tenant raises IntegrityError."""
        from boundary_testapp.models import Booking
        from django.db import IntegrityError

        with set_tenant(tenant_a), pytest.raises(IntegrityError):
            Booking.unscoped.create(court=1)


@pytest.mark.django_db
class TestBulkCreate:
    """AC-ORM-010/011: bulk_create auto-populates and respects explicit."""

    def test_bulk_create_auto_populates(self, tenant_a):
        from boundary_testapp.models import Booking

        with set_tenant(tenant_a):
            bookings = Booking.objects.bulk_create([Booking(court=1), Booking(court=2)])
            assert all(b.tenant == tenant_a for b in bookings)

    def test_bulk_create_respects_explicit(self, tenant_a, tenant_b):
        from boundary_testapp.models import Booking

        with set_tenant(tenant_a):
            bookings = Booking.objects.bulk_create([Booking(court=1, tenant=tenant_b)])
            assert bookings[0].tenant == tenant_b


@pytest.mark.django_db
class TestUpdateAndDelete:
    """AC-ORM-012/013: update/delete respect tenant filtering."""

    def test_update_only_active_tenant(self, tenant_a, tenant_b):
        from boundary_testapp.models import Booking

        with set_tenant(tenant_a):
            Booking.objects.create(court=1)
        with set_tenant(tenant_b):
            Booking.objects.create(court=2)

        with set_tenant(tenant_a):
            Booking.objects.update(is_paid=True)

        with set_tenant(tenant_b):
            booking = Booking.objects.first()
            assert not booking.is_paid

    def test_delete_only_active_tenant(self, tenant_a, tenant_b):
        from boundary_testapp.models import Booking

        with set_tenant(tenant_a):
            Booking.objects.create(court=1)
        with set_tenant(tenant_b):
            Booking.objects.create(court=2)

        with set_tenant(tenant_a):
            Booking.objects.all().delete()

        with set_tenant(tenant_b):
            assert Booking.objects.count() == 1


@pytest.mark.django_db
class TestBulkUpdate:
    """AC-ORM-014: bulk_update rejects cross-tenant objects."""

    def test_bulk_update_rejects_cross_tenant(self, tenant_a, tenant_b):
        from boundary_testapp.models import Booking

        with set_tenant(tenant_b):
            booking_b = Booking.objects.create(court=1)

        with set_tenant(tenant_a):
            booking_b.is_paid = True
            with pytest.raises(ValueError, match="Cross-tenant"):
                Booking.objects.bulk_update([booking_b], ["is_paid"])


@pytest.mark.django_db
class TestAbstractTenant:
    """Verify AbstractTenant provides expected fields."""

    def test_tenant_fields(self):
        from boundary_testapp.models import Tenant

        t = Tenant.objects.create(name="Test", slug="test")
        assert t.name == "Test"
        assert t.slug == "test"
        assert t.is_active is True
        assert t.region == ""
        assert t.created_at is not None
        assert t.updated_at is not None

    def test_str(self):
        from boundary_testapp.models import Tenant

        t = Tenant(name="My Club")
        assert str(t) == "My Club"
