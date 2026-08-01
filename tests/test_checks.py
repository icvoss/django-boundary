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
