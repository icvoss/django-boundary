"""Tests for boundary.testing — test utilities."""

import pytest
from django.test import TestCase

from boundary.context import TenantContext
from boundary.exceptions import TenantNotSetError
from boundary.testing import TenantTestMixin, call_view, set_tenant, tenant_factory


@pytest.mark.django_db
class TestSetTenant:
    """AC-TEST-001: set_tenant context manager."""

    def test_sets_context(self, tenant_a):
        with set_tenant(tenant_a):
            assert TenantContext.get() == tenant_a

    def test_clears_on_exit(self, tenant_a):
        with set_tenant(tenant_a):
            pass
        assert TenantContext.get() is None


@pytest.mark.django_db
class TestTenantFactory:
    """AC-TEST-004: tenant_factory defaults."""

    def test_creates_with_defaults(self):
        tenant = tenant_factory()
        assert tenant.pk is not None
        assert tenant.slug.startswith("test-")
        assert tenant.name.startswith("Test Tenant")

    def test_accepts_kwargs(self):
        tenant = tenant_factory(name="Custom", slug="custom")
        assert tenant.name == "Custom"
        assert tenant.slug == "custom"

    def test_unique_slugs(self):
        t1 = tenant_factory()
        t2 = tenant_factory()
        assert t1.slug != t2.slug


@pytest.mark.django_db
class TestTenantTestMixin(TenantTestMixin, TestCase):
    """AC-TEST-002/003: TenantTestMixin setup and cleanup."""

    def test_tenant_available(self):
        """AC-TEST-002: self.tenant is pre-created with context active."""
        assert self.tenant is not None
        assert TenantContext.get() == self.tenant

    def test_can_create_scoped_objects(self):
        from boundary_testapp.models import Booking

        booking = Booking.objects.create(court=1)
        assert booking.tenant == self.tenant


@pytest.mark.django_db
class TestCallView:
    """call_view runs a CBV under an active tenant context."""

    def _view_cls(self):
        from boundary_testapp.models import Booking
        from django.http import JsonResponse
        from django.views import View

        class BookingCountView(View):
            def get(self, request, *args, **kwargs):
                return JsonResponse({"count": Booking.objects.count()})

        return BookingCountView

    def test_view_sees_active_tenant_rows(self, tenant_a, tenant_b):
        import json

        from boundary_testapp.models import Booking

        with set_tenant(tenant_a):
            Booking.objects.create(court=1)
            Booking.objects.create(court=2)
        with set_tenant(tenant_b):
            Booking.objects.create(court=3)

        response = call_view(self._view_cls(), tenant=tenant_a)
        assert json.loads(response.content)["count"] == 2

        response = call_view(self._view_cls(), tenant=tenant_b)
        assert json.loads(response.content)["count"] == 1

    def test_without_helper_raises_strict(self, tenant_a, settings):
        """Proves the helper is what fixes the missing-context problem: the
        same view called via a bare RequestFactory raises under strict mode."""
        from django.test import RequestFactory

        settings.BOUNDARY_STRICT_MODE = True
        with set_tenant(tenant_a):
            from boundary_testapp.models import Booking

            Booking.objects.create(court=1)

        request = RequestFactory().get("/")
        with pytest.raises(TenantNotSetError):
            self._view_cls().as_view()(request)
