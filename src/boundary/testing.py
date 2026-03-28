"""Test utilities for consuming projects and boundary's own test suite.

Provides set_tenant(), TenantTestMixin, and tenant_factory() for
convenient multi-tenant testing.
"""

import uuid
from contextlib import contextmanager

from boundary.conf import get_tenant_model
from boundary.context import TenantContext


@contextmanager
def set_tenant(tenant):
    """Context manager for setting tenant in tests.

    Usage::

        with set_tenant(tenant_a):
            Booking.objects.create(court=1)
    """
    with TenantContext.using(tenant):
        yield tenant


def tenant_factory(**kwargs):
    """Create a tenant with sane defaults for tests.

    Generates a unique slug to avoid collisions in parallel tests.
    All kwargs are passed to the tenant model's create() method.
    """
    TenantModel = get_tenant_model()

    defaults = {
        "name": f"Test Tenant {uuid.uuid4().hex[:6]}",
        "slug": f"test-{uuid.uuid4().hex[:8]}",
    }
    defaults.update(kwargs)

    return TenantModel.objects.create(**defaults)


class TenantTestMixin:
    """Mixin for TestCase classes. Creates self.tenant before each test.

    Usage::

        class BookingTests(TenantTestMixin, TestCase):
            def test_booking_creation(self):
                booking = Booking.objects.create(court=1)
                assert booking.tenant == self.tenant
    """

    _boundary_context = None

    def get_tenant_factory_kwargs(self):
        """Override to customise the created tenant."""
        return {}

    def setUp(self):
        super().setUp()
        self.tenant = tenant_factory(**self.get_tenant_factory_kwargs())
        self._boundary_context = TenantContext.using(self.tenant)
        self._boundary_context.__enter__()

    def tearDown(self):
        if self._boundary_context is not None:
            self._boundary_context.__exit__(None, None, None)
            self._boundary_context = None
        super().tearDown()
