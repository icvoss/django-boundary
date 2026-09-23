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


class TestAcRls017AppLabelOverride:
    """AC-RLS-017 (BR-RLS-019): an RLS operation with app_label targets
    another app's table, and deconstruct()/describe() reflect the override.

    The applied-DDL half of the acceptance criterion (a migration in the
    consumer's app enabling RLS on ``thirdparty_widget``) lives in
    tests/test_adoption.py, where the third-party test app exists; this
    class pins the serialisation and description halves plus the resolution
    itself, which is what makes that migration target another app's table.
    """

    def test_ac_rls_017_enable_rls_deconstruct_is_byte_identical_without_the_override(self):
        """And EnableRLS("Booking").deconstruct() emits exactly
        {"model_name": "Booking"}, byte-identical to its output before the
        keyword existed.
        """
        name, args, kwargs = EnableRLS("Booking").deconstruct()
        assert name == "EnableRLS"
        assert args == []
        assert kwargs == {"model_name": "Booking"}

    def test_ac_rls_017_deconstruct_includes_app_label_when_set(self):
        """While EnableRLS("Widget", app_label="thirdparty").deconstruct()
        includes app_label.
        """
        _, _, kwargs = EnableRLS("Widget", app_label="thirdparty").deconstruct()
        assert kwargs == {"model_name": "Widget", "app_label": "thirdparty"}

    def test_ac_rls_017_policy_operations_deconstruct_the_override_the_same_way(self):
        """CreateTenantPolicy and DropTenantPolicy carry the same keyword on
        the same terms: absent when unset, present when set, alongside any
        tenant_column already in play.
        """
        for op_class in (CreateTenantPolicy, DropTenantPolicy):
            _, _, plain = op_class("Booking").deconstruct()
            assert plain == {"model_name": "Booking"}, op_class.__name__

            _, _, overridden = op_class("Widget", app_label="thirdparty").deconstruct()
            assert overridden == {"model_name": "Widget", "app_label": "thirdparty"}, op_class.__name__

            _, _, both = op_class("Widget", tenant_column="org_id", app_label="thirdparty").deconstruct()
            assert both == {
                "model_name": "Widget",
                "tenant_column": "org_id",
                "app_label": "thirdparty",
            }, op_class.__name__

    def test_ac_rls_017_describe_names_the_app_label_when_the_override_is_set(self):
        """And describe() returns a string naming thirdparty.Widget when the
        override is set and Widget when it is not.
        """
        for op_class in (EnableRLS, CreateTenantPolicy, DropTenantPolicy):
            assert "thirdparty.Widget" in op_class("Widget", app_label="thirdparty").describe(), op_class.__name__
            plain = op_class("Widget").describe()
            assert "Widget" in plain, op_class.__name__
            assert "thirdparty.Widget" not in plain, op_class.__name__

    @pytest.mark.django_db
    def test_ac_rls_017_the_override_resolves_the_model_from_the_named_app(self, tenant_a):
        """Given a migration in the consumer's app containing
        EnableRLS(..., app_label=...) followed by CreateTenantPolicy(...,
        app_label=...), when the migration is applied, then the target table
        has RLS enabled and forced and both policies present.

        The owning app label passed to database_forwards() is deliberately
        one that holds no such model, so the assertion fails with LookupError
        rather than passing vacuously if the override were ignored.
        """
        state = _get_fake_state()
        owning_app = "boundary"  # the consumer's own app: it has no Booking
        try:
            with connection.schema_editor() as editor:
                EnableRLS("Booking", app_label="boundary_testapp").database_forwards(owning_app, editor, state, state)
                CreateTenantPolicy("Booking", app_label="boundary_testapp").database_forwards(
                    owning_app, editor, state, state
                )

            enabled, forced = _has_rls("boundary_testapp_booking")
            assert enabled is True
            assert forced is True

            with connection.cursor() as cursor:
                cursor.execute("SELECT polname FROM pg_policy WHERE polrelid = 'boundary_testapp_booking'::regclass")
                policies = {row[0] for row in cursor.fetchall()}
            assert policies == {"boundary_tenant_isolation", "boundary_admin_bypass"}
        finally:
            with connection.schema_editor() as editor:
                EnableRLS("Booking", app_label="boundary_testapp").database_backwards(owning_app, editor, state, state)


# ── AC-RLS-019: the router and vendor gates (BR-RLS-021) ─────


class _DenyingRouter:
    """A router that refuses every migration on every alias."""

    def allow_migrate(self, db, app_label, **hints):
        return False


class _RecordingRouter:
    """A router that allows everything and records what it was asked.

    Written to Django's documented ``allow_migrate(db, app_label,
    model_name=None, **hints)`` signature, which is the shape a consumer's
    router has, so what it records is what a real router would receive.
    """

    calls: list = []

    def allow_migrate(self, db, app_label, model_name=None, **hints):
        type(self).calls.append((db, app_label, model_name, hints))
        return True


class _AliasDenyingRouter:
    """A router that denies one named alias and allows every other.

    The discriminator for AC-RLS-019: the same router, in the same run, must
    deny ``eu-west`` and admit ``default``. A gate that suppressed the
    operation outright on any denial would pass the denied half and fail
    here.
    """

    denied_alias = "eu-west"

    def allow_migrate(self, db, app_label, model_name=None, **hints):
        return db != self.denied_alias


def _sqlite_rls_state(alias="eu-west"):
    """Return what the SQLite alias can be asked about RLS, which is nothing.

    SQLite has no ``pg_class``, no policies and no notion of row security, so
    "the alias is untouched" cannot be asserted by reading RLS state out of
    it the way the PostgreSQL assertions do. The instrument is
    ``collect_sql=True`` instead: the schema editor appends every statement
    it is handed to a list rather than executing it, so an empty list is
    positive evidence the operation emitted nothing, rather than evidence it
    emitted something the connection silently swallowed.
    """
    from django.db import connections

    return connections[alias].vendor


@pytest.mark.django_db(transaction=True, databases=["default", "eu-west"])
class TestAcRls019RouterAndVendorGates:
    """AC-RLS-019 (BR-RLS-021): ``EnableRLS``, ``CreateTenantPolicy`` and
    ``DropTenantPolicy`` each apply the router gate then the vendor gate, in
    that order, in both directions.

    The ordering is the whole point of having two gates. A router denial is a
    legitimate per-alias skip and stays silent, because a router's
    ``allow_migrate()`` is the documented way to keep a non-PostgreSQL alias
    off the RLS graph and so cannot itself raise; a vendor mismatch on an
    alias the router DID admit is a consumer error and is named, rather than
    surfacing as SQLite's own parser error on a fragment of generated SQL
    (icvoss/django-boundary#75).

    Asserted against the suite's real SQLite ``eu-west`` alias rather than a
    stub connection, so the vendor string is the one a real backend reports.
    """

    #: The three operations under test, each constructed against the
    #: unmigrated ``boundary_testapp.Booking``, whose table the test runner
    #: creates on every alias including ``eu-west``.
    OPERATIONS = (
        ("EnableRLS", EnableRLS),
        ("CreateTenantPolicy", CreateTenantPolicy),
        ("DropTenantPolicy", DropTenantPolicy),
    )

    def test_a_denying_router_emits_nothing_on_eu_west_in_both_directions(self):
        """Given a router denying the SQLite ``eu-west`` alias, when each of
        the three operations runs forwards and backwards against it, then no
        DDL is emitted and nothing is raised.

        ``collect_sql=True`` is the instrument rather than an after-the-fact
        read of the alias: an empty collected list is evidence the operation
        emitted nothing, where reading state back from SQLite could not
        distinguish "emitted nothing" from "emitted something the backend
        ignored". The forward and the reverse each need their own assertion,
        since a guard on the forward alone would let a reverse strip RLS from
        an alias that never had it.
        """
        from django.db import connections
        from django.test import override_settings

        assert _sqlite_rls_state() == "sqlite", "the eu-west alias must be the SQLite one"

        state = _get_fake_state()
        for name, op_class in self.OPERATIONS:
            operation = op_class("Booking")
            with (
                override_settings(DATABASE_ROUTERS=[_DenyingRouter()]),
                connections["eu-west"].schema_editor(collect_sql=True) as editor,
            ):
                operation.database_forwards("boundary_testapp", editor, state, state)
                assert list(editor.collected_sql) == [], f"{name}.database_forwards emitted DDL on a denied alias"
                operation.database_backwards("boundary_testapp", editor, state, state)
                assert list(editor.collected_sql) == [], f"{name}.database_backwards emitted DDL on a denied alias"

    def test_an_allowing_router_on_eu_west_is_refused_naming_operation_alias_and_vendor(self):
        """When the router is changed to admit ``eu-west`` and the same
        operations are applied to it, then each raises a ``BoundaryError``
        subclass whose message names that operation, the alias and the vendor.

        Three separate assertions on one message, all load-bearing. Naming the
        vendor is what tells the consumer the layer is PostgreSQL-only; naming
        the alias is what tells them WHICH of their databases refused, which a
        multi-alias project needs; naming the operation is what keeps a
        consumer reading an ``EnableRLS`` traceback from being told about app
        adoption, a different operation they may not have written at all
        (BR-RLS-021: the error is ``AdoptionRefusedError``'s sibling, not its
        reuse).
        """
        from django.db import connections
        from django.test import override_settings

        from boundary.exceptions import BoundaryError

        state = _get_fake_state()
        for name, op_class in self.OPERATIONS:
            operation = op_class("Booking")
            with (
                override_settings(DATABASE_ROUTERS=[_RecordingRouter()]),
                connections["eu-west"].schema_editor() as editor,
                pytest.raises(BoundaryError) as caught,
            ):
                operation.database_forwards("boundary_testapp", editor, state, state)

            message = str(caught.value)
            assert name in message, f"the refusal must name the operation; got {message!r}"
            assert "eu-west" in message, f"the refusal must name the alias; got {message!r}"
            assert "sqlite" in message, f"the refusal must name the vendor; got {message!r}"
            assert "Row Level Security" in message
            assert "allow_migrate" in message, "the refusal must point at the documented remedy"

    def test_the_reverse_refuses_the_admitted_non_postgresql_alias_too(self):
        """And the same two gates apply in the same order on reverse: an
        admitted non-PostgreSQL alias is refused by name there as well.

        Its own test rather than a second assertion in the forward's, because
        ``DropTenantPolicy.database_backwards()`` delegates to
        ``CreateTenantPolicy.database_forwards()``. A reverse gated only
        inside the delegate would refuse with the delegate's name, telling the
        consumer ``CreateTenantPolicy`` refused when their migration contains
        ``DropTenantPolicy``, so the operation name is asserted here
        specifically.
        """
        from django.db import connections
        from django.test import override_settings

        from boundary.exceptions import BoundaryError

        state = _get_fake_state()
        for name, op_class in self.OPERATIONS:
            operation = op_class("Booking")
            with (
                override_settings(DATABASE_ROUTERS=[_RecordingRouter()]),
                connections["eu-west"].schema_editor() as editor,
                pytest.raises(BoundaryError) as caught,
            ):
                operation.database_backwards("boundary_testapp", editor, state, state)

            message = str(caught.value)
            assert name in message, f"the reverse refusal must name the operation; got {message!r}"
            assert "eu-west" in message
            assert "sqlite" in message

    def test_the_router_is_asked_through_allow_migrate_model_for_the_resolved_model(self):
        """And the router was asked through ``allow_migrate_model``, receiving
        the resolved model's own app label and model name.

        This is the half a consumer's router depends on. A router is written
        to ``allow_migrate(db, app_label, model_name=None, **hints)``,
        boundary's own ``RegionalRouter`` included, so an operation calling
        the bare app-level ``allow_migrate()`` (which is what ``RunSQL``
        does) would hand a router no model to key on at all, and a
        hand-built call passing an app label in the ``model_name`` position
        would make a model-keyed router match the wrong thing.

        The ``hints["model"]`` cross-check is what distinguishes
        ``allow_migrate_model`` from a hand-rolled ``allow_migrate`` call
        with the same two positional arguments: only the model-level form
        passes the model object itself as a hint.
        """
        from django.db import connections
        from django.test import override_settings

        from boundary.exceptions import BoundaryError

        state = _get_fake_state()
        for name, op_class in self.OPERATIONS:
            operation = op_class("Booking")
            _RecordingRouter.calls = []
            with (
                override_settings(DATABASE_ROUTERS=[_RecordingRouter()]),
                connections["eu-west"].schema_editor(collect_sql=True) as editor,
                pytest.raises(BoundaryError),
            ):
                # The vendor gate raises immediately after the router gate on
                # this alias; the router call is what is being asserted, and
                # it happened before the raise.
                operation.database_forwards("boundary_testapp", editor, state, state)

            assert _RecordingRouter.calls, f"{name} must ask the router at all"
            alias, app_label, model_name, hints = _RecordingRouter.calls[0]
            assert alias == "eu-west"
            assert app_label == "boundary_testapp", (
                f"{name} must pass the resolved model's app label; got {app_label!r}"
            )
            assert model_name == "booking", f"{name} must pass the model name, not an app label; got {model_name!r}"
            assert hints["model"]._meta.model_name == "booking", (
                f"{name} must ask through allow_migrate_model, which passes the model as a hint"
            )
            assert len(_RecordingRouter.calls) == 1, (
                f"{name} acts on exactly one model and must ask once; got {len(_RecordingRouter.calls)} calls"
            )

    def test_an_app_label_override_makes_the_router_asked_about_the_overridden_app(self):
        """And with an ``app_label`` override set, the router was asked about
        the overridden app's model rather than the migration's own app, so the
        gate follows BR-RLS-019's resolution.

        The owning app label passed to ``database_forwards()`` is deliberately
        one that holds no ``Booking``, so a gate resolving from the
        migration's own app would raise ``LookupError`` rather than pass
        vacuously.
        """
        from django.db import connections
        from django.test import override_settings

        from boundary.exceptions import BoundaryError

        state = _get_fake_state()
        owning_app = "boundary"  # holds no Booking
        for name, op_class in self.OPERATIONS:
            operation = op_class("Booking", app_label="boundary_testapp")
            _RecordingRouter.calls = []
            with (
                override_settings(DATABASE_ROUTERS=[_RecordingRouter()]),
                connections["eu-west"].schema_editor(collect_sql=True) as editor,
                pytest.raises(BoundaryError),
            ):
                operation.database_forwards(owning_app, editor, state, state)

            assert _RecordingRouter.calls, f"{name} must ask the router at all"
            _, app_label, model_name, _ = _RecordingRouter.calls[0]
            assert app_label == "boundary_testapp", (
                f"{name} must ask about the OVERRIDDEN app, not the migration's; got {app_label!r}"
            )
            assert model_name == "booking"

    def test_the_default_alias_is_processed_normally_in_the_same_run(self):
        """And ``default`` is processed normally by the same router in the same
        run, proving the gate discriminates by alias rather than suppressing
        the operation outright.

        The positive control, and the assertion that fails if the gates were
        implemented as an unconditional skip. One router instance denies
        ``eu-west`` and admits ``default``; the PostgreSQL alias must come out
        with RLS enabled, forced, and both boundary policies present.
        """
        from django.db import connections
        from django.test import override_settings

        state = _get_fake_state()
        with override_settings(DATABASE_ROUTERS=[_AliasDenyingRouter()]):
            # The denied alias first, so the run really is the same one.
            with connections["eu-west"].schema_editor(collect_sql=True) as editor:
                EnableRLS("Booking").database_forwards("boundary_testapp", editor, state, state)
                CreateTenantPolicy("Booking").database_forwards("boundary_testapp", editor, state, state)
                assert list(editor.collected_sql) == [], "the denied alias must receive nothing"

            try:
                with connection.schema_editor() as editor:
                    EnableRLS("Booking").database_forwards("boundary_testapp", editor, state, state)
                    CreateTenantPolicy("Booking").database_forwards("boundary_testapp", editor, state, state)

                enabled, forced = _has_rls("boundary_testapp_booking")
                assert enabled is True, "the admitted alias must have RLS enabled"
                assert forced is True, "the admitted alias must have RLS forced"

                with connection.cursor() as cursor:
                    cursor.execute(
                        "SELECT polname FROM pg_policy WHERE polrelid = 'boundary_testapp_booking'::regclass"
                    )
                    policies = {row[0] for row in cursor.fetchall()}
                assert policies == {"boundary_tenant_isolation", "boundary_admin_bypass"}

                # And the reverse likewise discriminates: the admitted alias
                # is cleaned up through the gated reverse itself, so a reverse
                # that refused an admitted PostgreSQL alias would fail here.
                with connection.schema_editor() as editor:
                    EnableRLS("Booking").database_backwards("boundary_testapp", editor, state, state)
                assert _has_rls("boundary_testapp_booking") == (False, False)
            finally:
                with connection.schema_editor() as editor:
                    EnableRLS("Booking").database_backwards("boundary_testapp", editor, state, state)

    def test_deconstruct_is_byte_identical_for_all_three_operations(self):
        """And ``deconstruct()`` emits exactly ``{"model_name": "Booking"}``
        for each of the three, byte-identical to its output before this rule,
        proving the gates added no constructor argument.

        BR-RLS-021's compatibility clause: the gates are runtime behaviour of
        ``database_forwards()`` and ``database_backwards()``, so every
        migration a consumer has already written must serialise to the same
        bytes and none needs editing. Asserted on the exact dict rather than
        by membership, so a gate that had smuggled in a keyword with a
        default would fail here.
        """
        for name, op_class in self.OPERATIONS:
            qualname, args, kwargs = op_class("Booking").deconstruct()
            assert qualname == name
            assert args == []
            assert kwargs == {"model_name": "Booking"}, f"{name}.deconstruct() gained a keyword; got {kwargs!r}"
