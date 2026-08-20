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
