"""Tests for boundary.conf — tenant model resolution and fallback (ADR-025 T2)."""

import pytest
from django.core.exceptions import ImproperlyConfigured

from boundary.conf import boundary_settings, get_tenant_model, resolve_tenant_model_setting


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

    def test_raises_improperly_configured_when_neither_setting_is_set(self, settings):
        """Issue #15: a missing setting is a startup config error, not a
        LookupError. Before the fix this path never even reached
        get_tenant_model(): boundary.models read settings.BOUNDARY_TENANT_MODEL
        directly at import time and raised a bare AttributeError instead."""
        settings.BOUNDARY_TENANT_MODEL = None
        settings.ICV_TENANT_MODEL = None
        with pytest.raises(ImproperlyConfigured, match="BOUNDARY_TENANT_MODEL nor ICV_TENANT_MODEL"):
            get_tenant_model()


@pytest.mark.django_db
class TestResolveTenantModelSetting:
    """Issue #15: the helper both models.py FK declarations and
    get_tenant_model() rely on to resolve the configured tenant model
    string, with a clear error when neither setting is configured."""

    def test_boundary_tenant_model_takes_precedence(self, settings):
        settings.BOUNDARY_TENANT_MODEL = "boundary_testapp.Tenant"
        settings.ICV_TENANT_MODEL = "boundary_testapp.Product"
        assert resolve_tenant_model_setting() == "boundary_testapp.Tenant"

    def test_falls_back_to_icv_tenant_model(self, settings):
        settings.BOUNDARY_TENANT_MODEL = None
        settings.ICV_TENANT_MODEL = "boundary_testapp.Tenant"
        assert resolve_tenant_model_setting() == "boundary_testapp.Tenant"

    def test_raises_improperly_configured_when_neither_is_set(self, settings):
        settings.BOUNDARY_TENANT_MODEL = None
        settings.ICV_TENANT_MODEL = None
        with pytest.raises(ImproperlyConfigured, match="BOUNDARY_TENANT_MODEL nor ICV_TENANT_MODEL"):
            resolve_tenant_model_setting()


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
