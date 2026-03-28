"""Shared test fixtures for boundary test suite."""

import pytest
from django.conf import settings


def pytest_configure(config):
    """Ensure boundary_testapp is in INSTALLED_APPS."""
    if "boundary_testapp" not in settings.INSTALLED_APPS:
        settings.INSTALLED_APPS.append("boundary_testapp")
    settings.MIGRATION_MODULES.setdefault("boundary_testapp", None)

    # Patch Django's PostgreSQL flush to use CASCADE, avoiding FK errors
    # in TransactionTestCase with the large sandbox schema.
    try:
        from django.db.backends.postgresql import operations

        _orig_sql_flush = operations.DatabaseOperations.sql_flush

        def _patched_sql_flush(self, style, tables, *, reset_sequences=False, allow_cascade=False):
            result = _orig_sql_flush(
                self,
                style,
                tables,
                reset_sequences=reset_sequences,
                allow_cascade=True,
            )
            return result

        operations.DatabaseOperations.sql_flush = _patched_sql_flush
    except Exception:
        pass


@pytest.fixture
def tenant_a(db):
    """Create tenant A."""
    from boundary_testapp.models import Tenant

    return Tenant.objects.create(name="Club A", slug="club-a")


@pytest.fixture
def tenant_b(db):
    """Create tenant B."""
    from boundary_testapp.models import Tenant

    return Tenant.objects.create(name="Club B", slug="club-b")


@pytest.fixture
def inactive_tenant(db):
    """Create an inactive tenant."""
    from boundary_testapp.models import Tenant

    return Tenant.objects.create(name="Closed Club", slug="closed", is_active=False)
