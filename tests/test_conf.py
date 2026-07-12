"""Tests for boundary.conf — tenant model resolution and fallback (ADR-025 T2)."""

import pytest

from boundary.conf import boundary_settings, get_tenant_model


@pytest.mark.django_db
class TestGetTenantModel:
    def test_uses_boundary_tenant_model_when_set(self, settings):
        settings.BOUNDARY_TENANT_MODEL = "boundary_testapp.Tenant"
        settings.ICV_TENANT_MODEL = None
        model = get_tenant_model()
        assert model.__name__ == "Tenant"

    def test_falls_back_to_icv_tenant_model_when_boundary_unset(self, settings):
        settings.BOUNDARY_TENANT_MODEL = None
        settings.ICV_TENANT_MODEL = "boundary_testapp.Tenant"
        model = get_tenant_model()
        assert model.__name__ == "Tenant"

    def test_boundary_tenant_model_takes_precedence_over_icv_tenant_model(self, settings):
        settings.BOUNDARY_TENANT_MODEL = "boundary_testapp.Tenant"
        settings.ICV_TENANT_MODEL = "nonexistent.Model"
        model = get_tenant_model()
        assert model.__name__ == "Tenant"

    def test_raises_when_neither_setting_is_set(self, settings):
        settings.BOUNDARY_TENANT_MODEL = None
        settings.ICV_TENANT_MODEL = None
        with pytest.raises(LookupError, match="BOUNDARY_TENANT_MODEL nor ICV_TENANT_MODEL"):
            get_tenant_model()


class TestBoundarySettingsTenantModel:
    def test_reads_boundary_tenant_model_when_set(self, settings):
        settings.BOUNDARY_TENANT_MODEL = "boundary_testapp.Tenant"
        settings.ICV_TENANT_MODEL = "other_app.OtherModel"
        assert boundary_settings.TENANT_MODEL == "boundary_testapp.Tenant"

    def test_falls_back_to_icv_tenant_model_when_boundary_unset(self, settings):
        settings.BOUNDARY_TENANT_MODEL = None
        settings.ICV_TENANT_MODEL = "icv_identity.Tenant"
        assert boundary_settings.TENANT_MODEL == "icv_identity.Tenant"

    def test_none_when_neither_setting_is_set(self, settings):
        settings.BOUNDARY_TENANT_MODEL = None
        settings.ICV_TENANT_MODEL = None
        assert boundary_settings.TENANT_MODEL is None
