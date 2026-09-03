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

    def test_rls_admin_bypass_allows_cross_tenant_insert(self, tenant_a, tenant_b, app_conn):
        """Issue #37: the admin flag does not just widen visibility, it also
        lifts the write check. boundary_admin_bypass has a USING clause and
        no WITH CHECK, so PostgreSQL falls back to USING for the write check
        too; since permissive policies are OR'd, satisfying admin_bypass's
        USING (the flag is 'true') is sufficient on its own, regardless of
        what boundary_tenant_isolation's WITH CHECK says. Verified by direct
        probe against a standalone table before this test was written; this
        pins that behaviour against the actual migrations_ops.py SQL. See
        test_rls_blocks_cross_tenant_insert for the positive control (same
        INSERT, same tables, no admin flag, blocked)."""
        _apply_rls()
        try:
            with app_conn.cursor() as cur:
                cur.execute("BEGIN")
                cur.execute("SELECT set_config('app.boundary_admin', 'true', true)")
                # No app.current_tenant_id set at all: the INSERT disagrees
                # with the active (absent) tenant context and is accepted
                # anyway, because the admin policy imposes no tenant check.
                cur.execute(
                    "INSERT INTO boundary_testapp_booking (tenant_id, court, is_paid) VALUES (%s, %s, false)",
                    [str(tenant_b.pk), 77],
                )
                cur.execute(
                    "SELECT count(*) FROM boundary_testapp_booking WHERE tenant_id = %s",
                    [str(tenant_b.pk)],
                )
                count = cur.fetchone()[0]
                cur.execute("ROLLBACK")  # Don't persist test data
            assert count == 1, f"Expected the cross-tenant INSERT to succeed under the admin flag, got count={count}"
        finally:
            _remove_rls()

    def test_rls_admin_bypass_allows_cross_tenant_update(self, tenant_a, tenant_b, app_conn):
        """Issue #37: the admin flag also lifts the write check for UPDATE,
        including an UPDATE that moves a row to a DIFFERENT tenant than the
        one it started with. Same OR'd-permissive-policy mechanism as the
        INSERT case above."""
        from boundary_testapp.models import Booking

        _apply_rls()
        try:
            with set_tenant(tenant_a):
                booking = Booking.objects.create(court=9)

            with app_conn.cursor() as cur:
                cur.execute("BEGIN")
                cur.execute("SELECT set_config('app.boundary_admin', 'true', true)")
                cur.execute(
                    "UPDATE boundary_testapp_booking SET tenant_id = %s WHERE id = %s",
                    [str(tenant_b.pk), booking.pk],
                )
                cur.execute(
                    "SELECT tenant_id FROM boundary_testapp_booking WHERE id = %s",
                    [booking.pk],
                )
                new_tenant_id = cur.fetchone()[0]
                cur.execute("ROLLBACK")  # Don't persist test data
            assert str(new_tenant_id) == str(tenant_b.pk), (
                "Expected the admin flag to allow moving a row to a different tenant via UPDATE"
            )
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


def _apply_rls_to_brand():
    """Apply RLS to Brand (the direct-FK parent BrandAsset paths through).

    Brand's tenant column is merchant_id (make_tenant_mixin("merchant")), so
    CreateTenantPolicy needs the non-default tenant_column kwarg.
    """
    state = _get_fake_state()
    with connection.schema_editor() as editor:
        EnableRLS("Brand").database_forwards("boundary_testapp", editor, state, state)
        CreateTenantPolicy("Brand", tenant_column="merchant_id").database_forwards(
            "boundary_testapp", editor, state, state
        )


def _remove_rls_from_brand():
    state = _get_fake_state()
    with connection.schema_editor() as editor:
        EnableRLS("Brand").database_backwards("boundary_testapp", editor, state, state)


@pytest.mark.django_db(transaction=True)
class TestPathScopedModelHasNoOwnRls:
    """Issue #14: path-scoped (relation-scoped) models are ORM-layer-only.

    This test PINS the documented contract: make_tenant_path_mixin() models
    carry no RLS policy of their own, so the ORM manager (auto-filtering on
    the declared path) constrains results while raw SQL against the child
    table does not, even though its parent (Brand) has RLS applied. If a
    future release adds a database-level policy for path-scoped models, this
    test must be updated deliberately, not left to fail as a surprise.
    """

    def test_raw_sql_bypasses_isolation_but_orm_does_not(self, tenant_a, tenant_b, app_conn):
        from boundary_testapp.models import Brand, BrandAsset

        _apply_rls_to_brand()
        try:
            with set_tenant(tenant_a):
                brand_a = Brand.objects.create(name="Brand A")
                BrandAsset.objects.create(brand=brand_a, label="a1")
                BrandAsset.objects.create(brand=brand_a, label="a2")
            with set_tenant(tenant_b):
                brand_b = Brand.objects.create(name="Brand B")
                BrandAsset.objects.create(brand=brand_b, label="b1")

            # ORM layer: auto-filtered on brand__merchant, sees only tenant A's rows.
            with set_tenant(tenant_a):
                orm_count = BrandAsset.objects.count()
            assert orm_count == 2

            # Raw SQL against the CHILD table directly: no RLS policy exists on
            # boundary_testapp_brandasset, so setting the tenant session variable
            # has no effect here and every tenant's rows come back.
            with app_conn.cursor() as cur:
                cur.execute("BEGIN")
                cur.execute(
                    "SELECT set_config('app.current_tenant_id', %s, true)",
                    [str(tenant_a.pk)],
                )
                cur.execute("SELECT count(*) FROM boundary_testapp_brandasset")
                raw_count = cur.fetchone()[0]
                cur.execute("COMMIT")

            assert raw_count == 3, (
                "Direct SQL against a path-scoped child table must see ALL tenants' rows: "
                "this pins the documented ORM-only contract for make_tenant_path_mixin (issue #14)"
            )
        finally:
            _remove_rls_from_brand()
