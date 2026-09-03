"""Tests for boundary.checks — Django system checks."""

import pytest

from boundary.checks import check_boundary_configuration


@pytest.mark.django_db
class TestSystemChecks:
    def test_no_errors_with_valid_config(self, settings):
        settings.BOUNDARY_TENANT_MODEL = "boundary_testapp.Tenant"
        settings.BOUNDARY_RESOLVERS = ["boundary.resolvers.SubdomainResolver"]
        settings.BOUNDARY_STRICT_MODE = True
        settings.MIDDLEWARE = ["boundary.middleware.TenantMiddleware"]
        errors = check_boundary_configuration(None)
        assert not any(e.id == "boundary.E001" for e in errors)
        assert not any(e.id == "boundary.E003" for e in errors)
        assert not any(e.id == "boundary.E004" for e in errors)

    def test_e001_missing_tenant_model(self, settings):
        settings.BOUNDARY_TENANT_MODEL = None
        errors = check_boundary_configuration(None)
        assert any(e.id == "boundary.E001" for e in errors)

    def test_e001_invalid_tenant_model(self, settings):
        settings.BOUNDARY_TENANT_MODEL = "nonexistent.Model"
        errors = check_boundary_configuration(None)
        assert any(e.id == "boundary.E001" for e in errors)

    def test_e003_invalid_resolver(self, settings):
        settings.BOUNDARY_TENANT_MODEL = "boundary_testapp.Tenant"
        settings.BOUNDARY_RESOLVERS = ["nonexistent.Resolver"]
        settings.MIDDLEWARE = ["boundary.middleware.TenantMiddleware"]
        errors = check_boundary_configuration(None)
        assert any(e.id == "boundary.E003" for e in errors)

    def test_e004_missing_middleware(self, settings):
        settings.BOUNDARY_TENANT_MODEL = "boundary_testapp.Tenant"
        settings.BOUNDARY_RESOLVERS = ["boundary.resolvers.SubdomainResolver"]
        settings.MIDDLEWARE = []
        errors = check_boundary_configuration(None)
        assert any(e.id == "boundary.E004" for e in errors)

    def test_w001_strict_mode_disabled(self, settings):
        settings.BOUNDARY_TENANT_MODEL = "boundary_testapp.Tenant"
        settings.BOUNDARY_RESOLVERS = ["boundary.resolvers.SubdomainResolver"]
        settings.BOUNDARY_STRICT_MODE = False
        settings.MIDDLEWARE = ["boundary.middleware.TenantMiddleware"]
        errors = check_boundary_configuration(None)
        assert any(e.id == "boundary.W001" for e in errors)

    def test_w002_both_boundary_and_identity_middleware_present(self, settings):
        settings.BOUNDARY_TENANT_MODEL = "boundary_testapp.Tenant"
        settings.BOUNDARY_RESOLVERS = ["boundary.resolvers.SubdomainResolver"]
        settings.MIDDLEWARE = [
            "boundary.middleware.TenantMiddleware",
            "icv_identity.tenants.middleware.TenantContextMiddleware",
        ]
        errors = check_boundary_configuration(None)
        assert any(e.id == "boundary.W002" for e in errors)

    def test_w002_absent_when_only_boundary_middleware_present(self, settings):
        """Boundary-only deployments must never warn (no icv-identity installed)."""
        settings.BOUNDARY_TENANT_MODEL = "boundary_testapp.Tenant"
        settings.BOUNDARY_RESOLVERS = ["boundary.resolvers.SubdomainResolver"]
        settings.MIDDLEWARE = ["boundary.middleware.TenantMiddleware"]
        errors = check_boundary_configuration(None)
        assert not any(e.id == "boundary.W002" for e in errors)

    def test_w002_absent_when_only_identity_middleware_present(self, settings):
        """No boundary TenantMiddleware configured: not boundary's concern to warn."""
        settings.BOUNDARY_TENANT_MODEL = "boundary_testapp.Tenant"
        settings.BOUNDARY_RESOLVERS = ["boundary.resolvers.SubdomainResolver"]
        settings.MIDDLEWARE = ["icv_identity.tenants.middleware.TenantContextMiddleware"]
        errors = check_boundary_configuration(None)
        assert not any(e.id == "boundary.W002" for e in errors)

    def test_w002_absent_when_neither_middleware_present(self, settings):
        settings.BOUNDARY_TENANT_MODEL = "boundary_testapp.Tenant"
        settings.BOUNDARY_RESOLVERS = ["boundary.resolvers.SubdomainResolver"]
        settings.MIDDLEWARE = []
        errors = check_boundary_configuration(None)
        assert not any(e.id == "boundary.W002" for e in errors)


@pytest.mark.django_db
class TestE001IcvTenantModelFallback:
    """Issue #15: _check_tenant_model() must accept ICV_TENANT_MODEL too
    (ADR-025 T2), not only BOUNDARY_TENANT_MODEL."""

    def test_no_e001_when_only_icv_tenant_model_is_set(self, settings):
        settings.BOUNDARY_TENANT_MODEL = None
        settings.ICV_TENANT_MODEL = "boundary_testapp.Tenant"
        settings.BOUNDARY_RESOLVERS = ["boundary.resolvers.SubdomainResolver"]
        settings.MIDDLEWARE = ["boundary.middleware.TenantMiddleware"]
        errors = check_boundary_configuration(None)
        assert not any(e.id == "boundary.E001" for e in errors)

    def test_e001_when_neither_setting_is_set(self, settings):
        settings.BOUNDARY_TENANT_MODEL = None
        settings.ICV_TENANT_MODEL = None
        errors = check_boundary_configuration(None)
        assert any(e.id == "boundary.E001" for e in errors)

    def test_e001_when_icv_tenant_model_names_a_missing_model(self, settings):
        settings.BOUNDARY_TENANT_MODEL = None
        settings.ICV_TENANT_MODEL = "nonexistent.Model"
        errors = check_boundary_configuration(None)
        assert any(e.id == "boundary.E001" for e in errors)


@pytest.mark.django_db
class TestW003RlsBypassableRole:
    """Issue #21: warn when the connecting role bypasses RLS entirely.

    PostgreSQL exempts superuser/BYPASSRLS roles from every policy, even
    FORCE ROW LEVEL SECURITY tables. E006 verifies RLS is enabled and
    forced on the tables; it says nothing about whether the connecting
    role can bypass what those tables declare. A consumer's dev/CI role
    is very often exactly this (the postgres-image bootstrap superuser),
    which is why the default `settings`/`db` fixture connection used
    throughout this suite is itself expected to trigger W003: that is the
    proof that the check fires in precisely the situation it exists to
    catch, not an artefact to work around.
    """

    def test_w003_fires_for_the_default_test_connection(self, settings):
        """The stock test-suite connection (icv_test) is bootstrap-superuser
        by default in the CI postgres:16 service and in a typical local
        docker-compose Postgres image, so W003 must fire against it."""
        settings.BOUNDARY_TENANT_MODEL = "boundary_testapp.Tenant"
        errors = check_boundary_configuration(None)
        w003 = [e for e in errors if e.id == "boundary.W003"]
        assert w003, "expected boundary.W003 to fire against the default (superuser) test connection"

    def test_w003_message_names_consequence_remedy_and_escape_hatch(self, settings):
        settings.BOUNDARY_TENANT_MODEL = "boundary_testapp.Tenant"
        errors = check_boundary_configuration(None)
        w003 = next(e for e in errors if e.id == "boundary.W003")
        # Consequence: not just "bypasses RLS" but what that means for tests.
        assert "will not be enforced" in w003.msg
        assert "tenant-isolation tests will pass without testing anything" in w003.msg
        # Remedy: the one-line role fix.
        assert "without SUPERUSER or BYPASSRLS" in w003.msg
        # Escape hatch by ID: a deliberate superuser connection is legitimate.
        assert "SILENCED_SYSTEM_CHECKS" in w003.msg
        assert "boundary.W003" in w003.msg

    def test_w003_absent_for_a_non_bypassing_role(self, settings):
        """Proven silent: a plain NOSUPERUSER NOBYPASSRLS role must not warn."""
        import psycopg
        from django.db import connection

        settings.BOUNDARY_TENANT_MODEL = "boundary_testapp.Tenant"

        db = connection.settings_dict
        try:
            plain_conn = psycopg.connect(
                host=db.get("HOST", "localhost"),
                port=db.get("PORT", 5432),
                dbname=db["NAME"],
                user="icv_app",
                password="icv_dev",
            )
        except Exception as e:
            pytest.skip(f"icv_app role not available: {e}")

        from boundary.checks import _check_rls_bypassable

        orig_cursor = connection.cursor
        try:
            connection.cursor = plain_conn.cursor
            errors = _check_rls_bypassable()
        finally:
            connection.cursor = orig_cursor
            plain_conn.close()

        assert not any(e.id == "boundary.W003" for e in errors)

    def test_w003_skips_non_postgresql_backends(self, settings, monkeypatch):
        from django.db import connection

        settings.BOUNDARY_TENANT_MODEL = "boundary_testapp.Tenant"
        monkeypatch.setattr(connection, "vendor", "sqlite")

        from boundary.checks import _check_rls_bypassable

        assert _check_rls_bypassable() == []


@pytest.mark.django_db(transaction=True)
class TestW003ReproducesTheConsumerProof:
    """Issue #21: reproduce Magmify's raw proof that FORCE ROW LEVEL SECURITY
    plus a USING(false) policy still returns rows under a bypassing role,
    confirming the mechanism this check exists to surface, not merely
    asserting the documented claim.
    """

    def test_force_rls_with_using_false_still_returns_rows_for_bypassing_role(self, settings):
        from django.db import connection

        settings.BOUNDARY_TENANT_MODEL = "boundary_testapp.Tenant"
        table = "boundary_testapp_tenant"

        with connection.cursor() as cursor:
            cursor.execute(f'ALTER TABLE "{table}" ENABLE ROW LEVEL SECURITY')
            cursor.execute(f'ALTER TABLE "{table}" FORCE ROW LEVEL SECURITY')
            cursor.execute(f'CREATE POLICY w003_proof_deny_all ON "{table}" USING (false)')
        try:
            from boundary_testapp.models import Tenant

            Tenant.objects.create(name="Proof Tenant", slug="proof-tenant")

            # The default test connection role bypasses RLS (superuser or
            # BYPASSRLS), so a USING(false) policy that should hide every
            # row is expected to have no effect: this is Magmify's proof,
            # reproduced here, not a description of the mechanism.
            with connection.cursor() as cursor:
                cursor.execute("SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user")
                rolsuper, rolbypassrls = cursor.fetchone()
            assert rolsuper or rolbypassrls, (
                "this proof requires the default test connection to be a "
                "superuser/BYPASSRLS role; if it is not, the proof and the "
                "W003 check both need re-verifying against this environment"
            )

            with connection.cursor() as cursor:
                cursor.execute(f'SELECT count(*) FROM "{table}" WHERE slug = %s', ["proof-tenant"])
                count = cursor.fetchone()[0]
            assert count == 1, (
                "expected FORCE ROW LEVEL SECURITY + USING(false) to still "
                "return the row for a bypassing role (nothing binds "
                "BYPASSRLS/superuser); got 0, which would mean the "
                "mechanism this check exists to catch does not hold here"
            )
        finally:
            with connection.cursor() as cursor:
                cursor.execute(f'DROP POLICY IF EXISTS w003_proof_deny_all ON "{table}"')
                cursor.execute(f'ALTER TABLE "{table}" NO FORCE ROW LEVEL SECURITY')
                cursor.execute(f'ALTER TABLE "{table}" DISABLE ROW LEVEL SECURITY')


@pytest.mark.django_db
class TestE006SkipsPathScopedModels:
    """Issue #14: _check_rls_enabled() must not flag path-scoped models.

    make_tenant_path_mixin() models have no local tenant column, so they
    have no table to put an RLS policy on, and the exemption is intentional
    per the documented ORM-only contract (see
    docs/how-to/scope-models-through-a-relation.md), not a false negative to
    be fixed later. This test asserts the system-check surface reports
    nothing for them even though their table genuinely has no RLS.
    """

    def test_no_e006_for_path_scoped_model(self, settings):
        from boundary.checks import _check_rls_enabled

        settings.BOUNDARY_TENANT_MODEL = "boundary_testapp.Tenant"
        errors = _check_rls_enabled()
        flagged = {e.msg for e in errors}
        assert not any("BrandAsset" in m for m in flagged)
        assert not any("AssetVariant" in m for m in flagged)


def _fake_migration_state():
    from django.apps import apps

    return type("FakeState", (), {"apps": apps})()


def _apply_rls_to_booking():
    """Apply RLS to boundary_testapp_booking via the real migration
    operations, the same mechanism tests/test_rls.py uses. This is the only
    way RLS ends up on a table in this suite: tests/settings.py sets
    MIGRATION_MODULES to None for every app, so pytest-django creates
    tables straight from the model definitions with no RLS operations run,
    and a table carries RLS only when a test applies it explicitly.
    """
    from django.db import connection

    from boundary.migrations_ops import CreateTenantPolicy, EnableRLS

    state = _fake_migration_state()
    with connection.schema_editor() as editor:
        EnableRLS("Booking").database_forwards("boundary_testapp", editor, state, state)
        CreateTenantPolicy("Booking").database_forwards("boundary_testapp", editor, state, state)


def _remove_rls_from_booking():
    from django.db import connection

    from boundary.migrations_ops import EnableRLS

    state = _fake_migration_state()
    with connection.schema_editor() as editor:
        EnableRLS("Booking").database_backwards("boundary_testapp", editor, state, state)


@pytest.mark.django_db
class TestE006FiresOnMissingRls:
    """Issue #34: boundary.E006 must be proven to actually fire.

    Every prior test touching E006 (this file, tests/test_traversal.py)
    only ever asserted it stayed silent, so its silence was never proven to
    mean "RLS is present" rather than "the check cannot see". These tests
    are the positive control: tests/settings.py disables Django migrations
    for every app (MIGRATION_MODULES = None), so pytest-django's `db`
    fixture creates boundary_testapp_booking straight from the model
    definition with no RLS operations applied, which is genuinely the
    "RLS absent" state, verified directly against pg_class below rather
    than assumed. The "silent when present" arm then applies RLS via the
    real EnableRLS/CreateTenantPolicy migration operations (the same
    mechanism tests/test_rls.py uses to exercise them) and re-runs the same
    check, so both arms exercise the identical code path and only the
    database state differs.
    """

    def test_e006_fires_when_rls_absent(self, settings):
        from django.db import connection

        from boundary.checks import _check_rls_enabled

        settings.BOUNDARY_TENANT_MODEL = "boundary_testapp.Tenant"

        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT relrowsecurity, relforcerowsecurity FROM pg_class WHERE relname = %s",
                ["boundary_testapp_booking"],
            )
            rls_enabled, rls_forced = cursor.fetchone()
        assert not rls_enabled and not rls_forced, (
            "precondition failed: boundary_testapp_booking already has RLS, "
            "so this is not the absent state the test claims to construct"
        )

        errors = _check_rls_enabled()
        e006 = [e for e in errors if e.id == "boundary.E006"]
        assert any("boundary_testapp_booking" in e.msg and "Booking" in e.msg for e in e006), (
            f"expected boundary.E006 to report the unprotected Booking table; got {[e.msg for e in e006]}"
        )

    def test_e006_silent_when_rls_enabled_and_forced(self, settings):
        from django.db import connection

        from boundary.checks import _check_rls_enabled

        settings.BOUNDARY_TENANT_MODEL = "boundary_testapp.Tenant"

        _apply_rls_to_booking()
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT relrowsecurity, relforcerowsecurity FROM pg_class WHERE relname = %s",
                    ["boundary_testapp_booking"],
                )
                rls_enabled, rls_forced = cursor.fetchone()
            assert rls_enabled and rls_forced, (
                "precondition failed: RLS was not actually applied to "
                "boundary_testapp_booking, so this is not the present "
                "state the test claims to construct"
            )

            errors = _check_rls_enabled()
            e006 = [e for e in errors if e.id == "boundary.E006" and "boundary_testapp_booking" in e.msg]
            assert not e006, f"expected boundary.E006 to stay silent for a protected table; got {[e.msg for e in e006]}"
        finally:
            _remove_rls_from_booking()


@pytest.mark.django_db
class TestE006CannotDetermineRlsState:
    """Issue #34: E006's exception handling must not fail open.

    A bare ``except Exception: pass`` produces zero errors for a permissions
    failure, a dead connection, or a query timeout, which reads exactly
    like "every table is correctly protected". These tests draw the line
    the fix makes: django.db.utils.OperationalError/InterfaceError (the
    connection itself is unreachable, e.g. before the database is
    provisioned) stays a silent skip, because there is nothing to report
    against; anything else (the connection is live and the query against
    pg_class failed) must surface as boundary.W007 instead of vanishing.
    """

    def test_w007_fires_when_the_query_fails_on_a_live_connection(self, settings, monkeypatch):
        from django.db import connection
        from django.db.utils import ProgrammingError

        from boundary.checks import _check_rls_enabled

        settings.BOUNDARY_TENANT_MODEL = "boundary_testapp.Tenant"

        class _RaisingCursor:
            def __enter__(self):
                raise ProgrammingError("permission denied for table pg_class")

            def __exit__(self, exc_type, exc_value, traceback):
                return False

        monkeypatch.setattr(connection, "cursor", lambda: _RaisingCursor())

        errors = _check_rls_enabled()
        w007 = [e for e in errors if e.id == "boundary.W007"]
        assert w007, "expected boundary.W007 when the pg_class query fails on a live connection"
        assert "boundary_testapp_booking" in w007[0].msg
        assert "permission denied" in w007[0].msg
        assert not any(e.id == "boundary.E006" for e in errors), (
            "a query failure must not also be reported as a confirmed missing policy"
        )

    def test_no_w007_when_the_connection_is_genuinely_unavailable(self, settings, monkeypatch):
        from django.db import connection
        from django.db.utils import OperationalError

        from boundary.checks import _check_rls_enabled

        settings.BOUNDARY_TENANT_MODEL = "boundary_testapp.Tenant"

        class _RaisingCursor:
            def __enter__(self):
                raise OperationalError("connection refused")

            def __exit__(self, exc_type, exc_value, traceback):
                return False

        monkeypatch.setattr(connection, "cursor", lambda: _RaisingCursor())

        errors = _check_rls_enabled()
        assert not any(e.id == "boundary.W007" for e in errors), (
            "an unreachable connection is the legitimate pre-migrate skip, not a W007 case"
        )
        assert not any(e.id == "boundary.E006" for e in errors)

    def test_w007_fires_for_rls_bypassable_when_the_query_fails(self, settings, monkeypatch):
        from django.db import connection
        from django.db.utils import ProgrammingError

        from boundary.checks import _check_rls_bypassable

        settings.BOUNDARY_TENANT_MODEL = "boundary_testapp.Tenant"

        class _RaisingCursor:
            def __enter__(self):
                raise ProgrammingError("permission denied for table pg_roles")

            def __exit__(self, exc_type, exc_value, traceback):
                return False

        monkeypatch.setattr(connection, "cursor", lambda: _RaisingCursor())

        errors = _check_rls_bypassable()
        w007 = [e for e in errors if e.id == "boundary.W007"]
        assert w007, "expected boundary.W007 when the pg_roles query fails on a live connection"
        assert "permission denied" in w007[0].msg
        assert not any(e.id == "boundary.W003" for e in errors)

    def test_no_w007_for_rls_bypassable_when_the_connection_is_unavailable(self, settings, monkeypatch):
        from django.db import connection
        from django.db.utils import OperationalError

        from boundary.checks import _check_rls_bypassable

        settings.BOUNDARY_TENANT_MODEL = "boundary_testapp.Tenant"

        class _RaisingCursor:
            def __enter__(self):
                raise OperationalError("connection refused")

            def __exit__(self, exc_type, exc_value, traceback):
                return False

        monkeypatch.setattr(connection, "cursor", lambda: _RaisingCursor())

        errors = _check_rls_bypassable()
        assert errors == []


@pytest.mark.django_db(transaction=True)
class TestE006QualifiesTableLookupByOid:
    """Issue #34: the pg_class lookup must resolve the model's real table,
    not an arbitrary same-named row from another schema.

    ``WHERE relname = %s`` with no schema qualification returns one row per
    schema containing a same-named table, and the old code's ``fetchone()``
    read whichever row PostgreSQL's index scan produced first. On this
    instance ``public`` always carries a low, fixed system OID (2200), so a
    naive "same name in two schemas" probe with the real table left in
    ``public`` cannot by itself distinguish old from new: ``public`` sorts
    first regardless. To build a discriminator that is not an accident of
    OID ordering, this test moves the REAL table out of ``public`` into a
    schema placed first on the connection's ``search_path`` (so it is
    genuinely where Django resolves the table from), then creates an
    unprotected decoy under the same name back in ``public``. The old,
    unqualified query has no way to know which schema `search_path`
    prefers and returns the first row by OID, which is the wrong, decoy
    row here; ``to_regclass()`` follows ``search_path`` the same way any
    ordinary query against the table would, and resolves the real one.
    """

    def test_check_resolves_the_search_path_table_not_a_decoy_in_public(self, settings):
        from django.db import connection

        from boundary.checks import _check_rls_enabled

        settings.BOUNDARY_TENANT_MODEL = "boundary_testapp.Tenant"
        table = "boundary_testapp_booking"

        with connection.cursor() as cursor:
            cursor.execute("CREATE SCHEMA IF NOT EXISTS tenantschema")
            cursor.execute(f'ALTER TABLE public."{table}" SET SCHEMA tenantschema')
            cursor.execute("SET search_path = tenantschema, public")

        # EnableRLS/CreateTenantPolicy resolve the table name through this
        # same connection, so with search_path set they operate on the
        # relocated tenantschema.booking, the table Django itself now
        # resolves the model to.
        _apply_rls_to_booking()
        try:
            with connection.cursor() as cursor:
                # The decoy: same table name, back in public, deliberately
                # left WITHOUT RLS.
                cursor.execute(f'CREATE TABLE public."{table}" (id serial primary key)')

                cursor.execute(
                    "SELECT n.nspname, c.relrowsecurity, c.relforcerowsecurity "
                    "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                    "WHERE c.relname = %s ORDER BY n.nspname",
                    [table],
                )
                rows = dict((schema, (enabled, forced)) for schema, enabled, forced in cursor.fetchall())
                assert rows == {"public": (False, False), "tenantschema": (True, True)}, (
                    f"expected an unprotected decoy in public and the real, protected table in tenantschema; got {rows}"
                )

                # Prove the OLD, unqualified query actually reads the wrong
                # row in this construction: fetchone() takes whichever row
                # comes first with no regard for search_path, and here that
                # is the unprotected public decoy.
                cursor.execute(
                    "SELECT relrowsecurity, relforcerowsecurity FROM pg_class WHERE relname = %s",
                    [table],
                )
                old_query_result = cursor.fetchone()
                assert old_query_result == (False, False), (
                    f"expected this construction to reproduce the old query's false read; got {old_query_result}"
                )

            errors = _check_rls_enabled()
            e006 = [e for e in errors if e.id == "boundary.E006" and table in e.msg]
            assert not e006, (
                f"expected the OID-qualified lookup to resolve the real, "
                f"RLS-protected tenantschema.{table} via search_path, not "
                f"the unprotected public decoy; got {[e.msg for e in e006]}"
            )
        finally:
            from boundary.migrations_ops import DropTenantPolicy

            with connection.cursor() as cursor:
                cursor.execute(f'DROP TABLE IF EXISTS public."{table}"')
            state = _fake_migration_state()
            with connection.schema_editor() as editor:
                DropTenantPolicy("Booking").database_forwards("boundary_testapp", editor, state, state)
            _remove_rls_from_booking()
            with connection.cursor() as cursor:
                cursor.execute(f'ALTER TABLE tenantschema."{table}" SET SCHEMA public')
                cursor.execute("RESET search_path")
                cursor.execute("DROP SCHEMA IF EXISTS tenantschema CASCADE")

    def test_to_regclass_returns_null_for_a_nonexistent_table(self):
        """Sanity check for the row-is-None branch: to_regclass() must
        behave the same as the old fetchone() == None case for a table
        that genuinely does not exist, not raise."""
        from django.db import connection

        with connection.cursor() as cursor:
            cursor.execute("SELECT to_regclass(%s)", ["definitely_does_not_exist_xyz"])
            resolved = cursor.fetchone()[0]
        assert resolved is None
