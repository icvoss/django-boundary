"""Tests for boundary.testing — test utilities."""

import pytest
from django.test import TestCase

from boundary.context import TenantContext
from boundary.testing import TenantTestMixin, set_tenant, tenant_factory


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
