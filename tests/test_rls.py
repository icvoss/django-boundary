"""Tests for RLS migration operations and database-level enforcement.

Uses a module-scoped fixture to apply RLS once, then run all enforcement
tests within that scope. RLS is removed at module teardown.
"""

import pytest
from django.db import connection

from boundary.migrations_ops import CreateTenantPolicy, DropTenantPolicy, EnableRLS
from boundary.testing import set_tenant


def _get_fake_state():
    from django.apps import apps

    return type("FakeState", (), {"apps": apps})()


def _apply_rls():
    state = _get_fake_state()
    with connection.schema_editor() as editor:
        EnableRLS("Booking").database_forwards("boundary_testapp", editor, state, state)
        CreateTenantPolicy("Booking").database_forwards("boundary_testapp", editor, state, state)


def _remove_rls():
    state = _get_fake_state()
    with connection.schema_editor() as editor:
        EnableRLS("Booking").database_backwards("boundary_testapp", editor, state, state)


def _has_rls(table_name):
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT relrowsecurity, relforcerowsecurity FROM pg_class WHERE relname = %s",
            [table_name],
        )
        row = cursor.fetchone()
        if row is None:
            return False, False
        return row[0], row[1]


# ── Migration operation unit tests (no RLS needed) ───────────


class TestEnableRLSUnit:
    """Unit tests for EnableRLS operation (describe, deconstruct)."""

    def test_describe(self):
        assert "Booking" in EnableRLS("Booking").describe()

    def test_deconstruct(self):
        _, _, kwargs = EnableRLS("Booking").deconstruct()
        assert kwargs["model_name"] == "Booking"


class TestCreateTenantPolicyUnit:
    """Unit tests for CreateTenantPolicy."""

    def test_describe(self):
        assert "Booking" in CreateTenantPolicy("Booking").describe()

    def test_deconstruct_default_column(self):
        _, _, kwargs = CreateTenantPolicy("Booking").deconstruct()
        assert "tenant_column" not in kwargs

    def test_deconstruct_custom_column(self):
        _, _, kwargs = CreateTenantPolicy("Booking", tenant_column="org_id").deconstruct()
        assert kwargs["tenant_column"] == "org_id"


class TestDropTenantPolicyUnit:
    """Unit tests for DropTenantPolicy."""

    def test_describe(self):
        assert "Booking" in DropTenantPolicy("Booking").describe()


@pytest.mark.django_db
class TestCustomSessionVariables:
    """Issue #5: RLS SQL must honour BOUNDARY_DB_SESSION_VAR / ADMIN_FLAG_VAR.

    The generated policies and helper function must reference the configured
    session-variable names, not the hardcoded defaults, otherwise customising
    the settings silently breaks isolation.
    """

    def _function_body(self):
        with connection.cursor() as cursor:
            cursor.execute("SELECT prosrc FROM pg_proc WHERE proname = 'boundary_current_tenant_id'")
            row = cursor.fetchone()
            return row[0] if row else ""

    def _admin_policy_qual(self):
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT pg_get_expr(polqual, polrelid) FROM pg_policy "
                "WHERE polname = 'boundary_admin_bypass' "
                "AND polrelid = 'boundary_testapp_booking'::regclass"
            )
            row = cursor.fetchone()
            return row[0] if row else ""

    def test_custom_session_var_in_function(self, settings):
        settings.BOUNDARY_DB_SESSION_VAR = "myapp.tenant"
        settings.BOUNDARY_ADMIN_FLAG_VAR = "myapp.is_admin"
        _apply_rls()
        try:
            body = self._function_body()
            assert "myapp.tenant" in body
            assert "app.current_tenant_id" not in body

            admin_qual = self._admin_policy_qual()
            assert "myapp.is_admin" in admin_qual
            assert "app.boundary_admin" not in admin_qual
        finally:
            _remove_rls()
            state = _get_fake_state()
            with connection.schema_editor() as editor:
                DropTenantPolicy("Booking").database_forwards("boundary_testapp", editor, state, state)


# ── Database integration tests ────────────────────────────────


@pytest.mark.django_db
class TestRLSOperations:
    """Test that RLS operations modify pg_class correctly."""

    def test_enable_and_disable_rls(self):
        _apply_rls()
        try:
            enabled, forced = _has_rls("boundary_testapp_booking")
            assert enabled is True
            assert forced is True
        finally:
            _remove_rls()

        enabled, forced = _has_rls("boundary_testapp_booking")
        assert enabled is False

    def test_creates_helper_function_not_leakproof_by_default(self):
        # LEAKPROOF requires a superuser (unavailable on managed Postgres), so
        # the helper is created without it unless BOUNDARY_FUNCTION_LEAKPROOF is
        # set. See conf.FUNCTION_LEAKPROOF and BR-RLS-009.
        _apply_rls()
        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT proleakproof FROM pg_proc WHERE proname = 'boundary_current_tenant_id'")
                row = cursor.fetchone()
                assert row is not None, "Function not created"
                assert row[0] is False, "Function should not be LEAKPROOF by default"
        finally:
            _remove_rls()

    def test_creates_leakproof_function_when_opted_in(self, settings):
        # Opt in via BOUNDARY_FUNCTION_LEAKPROOF. The test database role is a
        # superuser, so the LEAKPROOF declaration is permitted here.
        settings.BOUNDARY_FUNCTION_LEAKPROOF = True
        _apply_rls()
        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT proleakproof FROM pg_proc WHERE proname = 'boundary_current_tenant_id'")
                row = cursor.fetchone()
                assert row is not None, "Function not created"
                assert row[0] is True, "Function not LEAKPROOF when opted in"
        finally:
            _remove_rls()

    def test_creates_both_policies(self):
        _apply_rls()
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT polname FROM pg_policy "
                    "WHERE polrelid = 'boundary_testapp_booking'::regclass "
                    "ORDER BY polname"
                )
                policies = [row[0] for row in cursor.fetchall()]
                assert "boundary_admin_bypass" in policies
                assert "boundary_tenant_isolation" in policies
        finally:
            _remove_rls()

    def test_drop_removes_policies(self):
        _apply_rls()
        state = _get_fake_state()
        with connection.schema_editor() as editor:
            DropTenantPolicy("Booking").database_forwards("boundary_testapp", editor, state, state)
        with connection.cursor() as cursor:
            cursor.execute("SELECT count(*) FROM pg_policy WHERE polrelid = 'boundary_testapp_booking'::regclass")
            assert cursor.fetchone()[0] == 0
        _remove_rls()

    def test_drop_reverse_recreates(self):
        _apply_rls()
        state = _get_fake_state()
        with connection.schema_editor() as editor:
            drop = DropTenantPolicy("Booking")
            drop.database_forwards("boundary_testapp", editor, state, state)
            drop.database_backwards("boundary_testapp", editor, state, state)
        with connection.cursor() as cursor:
            cursor.execute("SELECT count(*) FROM pg_policy WHERE polrelid = 'boundary_testapp_booking'::regclass")
            assert cursor.fetchone()[0] == 2
        _remove_rls()


@pytest.fixture
def app_conn():
    """Raw psycopg connection as non-superuser icv_app role for RLS testing.

    Superusers bypass RLS even with FORCE ROW LEVEL SECURITY.
    These tests MUST run as a non-superuser to verify enforcement.

    Grants icv_app SELECT/INSERT/UPDATE/DELETE on the test tables for the
    duration of the fixture, then revokes on teardown.
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
    tables = ("boundary_testapp_booking", "boundary_testapp_tenant")
    with connection.cursor() as cur:
        for table in tables:
            cur.execute(f'GRANT SELECT, INSERT, UPDATE, DELETE ON "{table}" TO icv_app')

    yield conn
    conn.close()

    # Revoke grants on teardown.
    with connection.cursor() as cur:
        for table in tables:
            cur.execute(f'REVOKE ALL ON "{table}" FROM icv_app')


@pytest.mark.django_db(transaction=True)
class TestRLSEnforcement:
    """AC-RLS-001/002/003/006/007: Database-level enforcement tests.

    Uses a raw psycopg connection as non-superuser icv_app role, because
    superusers bypass RLS even with FORCE ROW LEVEL SECURITY.
    """

    def test_rls_filters_raw_sql_by_tenant(self, tenant_a, tenant_b, app_conn):
        """AC-RLS-001: Only active tenant's rows visible via raw SQL."""
        from boundary_testapp.models import Booking

        _apply_rls()
        try:
            with set_tenant(tenant_a):
                Booking.objects.create(court=1)
            with set_tenant(tenant_b):
                Booking.objects.create(court=2)

            with app_conn.cursor() as cur:
                cur.execute("BEGIN")
                cur.execute(
                    "SELECT set_config('app.current_tenant_id', %s, true)",
                    [str(tenant_a.pk)],
                )
                cur.execute("SELECT count(*) FROM boundary_testapp_booking")
                count = cur.fetchone()[0]
                cur.execute("COMMIT")
            assert count == 1, f"Expected 1, got {count}"
        finally:
            _remove_rls()

    def test_rls_empty_context_returns_zero(self, tenant_a, app_conn):
        """AC-RLS-002: No tenant context = zero rows."""
        from boundary_testapp.models import Booking

        _apply_rls()
        try:
            with set_tenant(tenant_a):
                Booking.objects.create(court=1)

            with app_conn.cursor() as cur:
                cur.execute("BEGIN")
                cur.execute("SELECT set_config('app.current_tenant_id', '', true)")
                cur.execute("SELECT count(*) FROM boundary_testapp_booking")
                count = cur.fetchone()[0]
                cur.execute("COMMIT")
            assert count == 0, f"Expected 0, got {count}"
        finally:
            _remove_rls()

    def test_rls_admin_bypass(self, tenant_a, tenant_b, app_conn):
        """AC-RLS-003: Admin flag bypasses RLS."""
        from boundary_testapp.models import Booking

        _apply_rls()
        try:
            with set_tenant(tenant_a):
                Booking.objects.create(court=1)
            with set_tenant(tenant_b):
                Booking.objects.create(court=2)

            with app_conn.cursor() as cur:
                cur.execute("BEGIN")
                cur.execute("SELECT set_config('app.boundary_admin', 'true', true)")
                cur.execute("SELECT count(*) FROM boundary_testapp_booking")
                count = cur.fetchone()[0]
                cur.execute("COMMIT")
            assert count == 2, f"Expected 2, got {count}"
        finally:
            _remove_rls()

    def test_rls_blocks_cross_tenant_insert(self, tenant_a, tenant_b, app_conn):
        """AC-RLS-007: WITH CHECK prevents INSERT for wrong tenant."""
        _apply_rls()
        try:
            with app_conn.cursor() as cur:
                cur.execute("BEGIN")
                cur.execute(
                    "SELECT set_config('app.current_tenant_id', %s, true)",
                    [str(tenant_a.pk)],
                )
                with pytest.raises(Exception, match=r"."):
                    cur.execute(
                        "INSERT INTO boundary_testapp_booking (tenant_id, court, is_paid) VALUES (%s, %s, false)",
                        [str(tenant_b.pk), 99],
                    )
                cur.execute("ROLLBACK")
        finally:
            _remove_rls()

    def test_rls_allows_insert_for_active_tenant(self, tenant_a, app_conn):
        """INSERT succeeds when tenant_id matches active context."""
        _apply_rls()
        try:
            with app_conn.cursor() as cur:
                cur.execute("BEGIN")
                cur.execute(
                    "SELECT set_config('app.current_tenant_id', %s, true)",
                    [str(tenant_a.pk)],
                )
                cur.execute(
                    "INSERT INTO boundary_testapp_booking (tenant_id, court, is_paid) VALUES (%s, %s, false)",
                    [str(tenant_a.pk), 5],
                )
                cur.execute("SELECT count(*) FROM boundary_testapp_booking")
                count = cur.fetchone()[0]
                cur.execute("ROLLBACK")  # Don't persist test data
            assert count == 1
        finally:
            _remove_rls()

    def test_orm_and_raw_sql_in_sync(self, tenant_a, tenant_b, app_conn):
        """AC-RLS-006: ORM and raw SQL return identical results."""
        from boundary_testapp.models import Booking

        _apply_rls()
        try:
            with set_tenant(tenant_a):
                Booking.objects.create(court=1)
                Booking.objects.create(court=2)
            with set_tenant(tenant_b):
                Booking.objects.create(court=3)

            # ORM count (as superuser — filtered by TenantManager)
            with set_tenant(tenant_a):
                orm_count = Booking.objects.count()

            # Raw SQL count (as non-superuser — filtered by RLS)
            with app_conn.cursor() as cur:
                cur.execute("BEGIN")
                cur.execute(
                    "SELECT set_config('app.current_tenant_id', %s, true)",
                    [str(tenant_a.pk)],
                )
                cur.execute("SELECT count(*) FROM boundary_testapp_booking")
                raw_count = cur.fetchone()[0]
                cur.execute("COMMIT")

            assert orm_count == raw_count == 2
        finally:
            _remove_rls()
