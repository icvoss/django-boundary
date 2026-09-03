"""Shared test fixtures for boundary test suite."""

import pytest
from django.conf import settings
from django.db import connection


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


@pytest.fixture
def app_conn():
    """Raw psycopg connection as non-superuser icv_app role for RLS testing.

    Superusers bypass RLS even with FORCE ROW LEVEL SECURITY. Tests that
    verify RLS enforcement (test_rls.py, test_context.py's admin_bypass()
    coverage) MUST run as a non-superuser, since the default `icv_test` role
    used elsewhere in the suite is itself a bypassing superuser and would
    make an isolation assertion pass on both a correct and a broken policy.

    Grants icv_app SELECT/INSERT/UPDATE/DELETE on the test tables for the
    duration of the fixture, then revokes on teardown. Shared here (moved
    from test_rls.py, issue #37) rather than duplicated per test module.
    """
    import psycopg

    db = connection.settings_dict
    try:
        conn = psycopg.connect(
            host=db.get("HOST", "localhost"),
            port=db.get("PORT", 5432),
            dbname=db["NAME"],
            user="icv_app",
            password="icv_dev",
            autocommit=False,
        )
    except Exception as e:
        pytest.skip(f"icv_app role not available: {e}")

    # Grant table access to the non-superuser role (run as superuser via Django conn).
    tables = (
        "boundary_testapp_booking",
        "boundary_testapp_tenant",
        "boundary_testapp_brand",
        "boundary_testapp_brandasset",
    )
    with connection.cursor() as cur:
        for table in tables:
            cur.execute(f'GRANT SELECT, INSERT, UPDATE, DELETE ON "{table}" TO icv_app')

    yield conn
    conn.close()

    # Revoke grants on teardown.
    with connection.cursor() as cur:
        for table in tables:
            cur.execute(f'REVOKE ALL ON "{table}" FROM icv_app')


@pytest.fixture(autouse=True)
def _cleanup_tenant_context():
    """Clear any lingering tenant context after each test.

    Ensures test isolation: a monkeypatch in one test or a failed context
    manager exit doesn't leave a stale tenant in _current_tenant for the next
    test to see.
    """
    yield
    from boundary.context import TenantContext, _current_tenant

    # Only clear if something is set; this is best-effort cleanup.
    if TenantContext.get() is not None:
        _current_tenant.set(None)
