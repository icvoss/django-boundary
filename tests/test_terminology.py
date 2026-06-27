"""Tests for configurable terminology — TENANT_LABEL and REQUEST_ATTR."""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from boundary.conf import boundary_settings
from boundary.context import TenantContext
from boundary.exceptions import TenantNotSetError
from boundary.middleware import TenantMiddleware


class TestTenantLabel:
    """BOUNDARY_TENANT_LABEL is interpolated into error messages."""

    def test_default_label_is_tenant(self):
        with pytest.raises(TenantNotSetError, match=r"No tenant is active"):
            TenantContext.require()

    def test_label_falls_back_to_fk_field(self, settings):
        settings.BOUNDARY_TENANT_FK_FIELD = "merchant"
        with pytest.raises(TenantNotSetError, match=r"No merchant is active"):
            TenantContext.require()

    def test_explicit_label_overrides_fk_field(self, settings):
        settings.BOUNDARY_TENANT_FK_FIELD = "merchant"
        settings.BOUNDARY_TENANT_LABEL = "shop"
        with pytest.raises(TenantNotSetError, match=r"No shop is active"):
            TenantContext.require()


@pytest.mark.django_db
class TestRequestAttr:
    """BOUNDARY_REQUEST_ATTR controls the alias on request.

    Marked django_db because the middleware sets the tenant context, which
    writes the PostgreSQL session variable via a DB cursor. Without the mark
    these pass only when a prior test happens to leave a connection open.
    """

    def _make_middleware(self, tenant):
        mw = TenantMiddleware(get_response=lambda r: SimpleNamespace())
        mw._resolve_tenant = lambda r: (tenant, MagicMock())
        return mw

    def test_request_tenant_always_set(self, settings):
        settings.BOUNDARY_REQUEST_ATTR = "merchant"
        settings.BOUNDARY_WRAP_ATOMIC = False
        tenant = SimpleNamespace(pk=1, is_active=True)
        request = SimpleNamespace()
        self._make_middleware(tenant)(request)
        assert request.tenant is tenant

    def test_request_alias_set_when_attr_differs(self, settings):
        settings.BOUNDARY_REQUEST_ATTR = "merchant"
        settings.BOUNDARY_WRAP_ATOMIC = False
        tenant = SimpleNamespace(pk=1, is_active=True)
        request = SimpleNamespace()
        self._make_middleware(tenant)(request)
        assert request.merchant is tenant

    def test_no_extra_attribute_when_default(self, settings):
        # Default REQUEST_ATTR resolves to "tenant" → no second attribute set
        settings.BOUNDARY_WRAP_ATOMIC = False
        assert boundary_settings.REQUEST_ATTR == "tenant"
        tenant = SimpleNamespace(pk=1, is_active=True)
        request = SimpleNamespace()
        self._make_middleware(tenant)(request)
        assert not hasattr(request, "merchant")


class TestMiddlewareResponseCopy:
    """HTTP response bodies in middleware use the configured label."""

    def test_not_found_uses_label(self, settings):
        settings.BOUNDARY_TENANT_LABEL = "merchant"
        settings.BOUNDARY_REQUIRED = True
        mw = TenantMiddleware(get_response=lambda r: SimpleNamespace())
        mw._resolve_tenant = lambda r: (None, None)
        response = mw(SimpleNamespace())
        assert b"Merchant not found" in response.content

    def test_inactive_uses_label(self, settings):
        settings.BOUNDARY_TENANT_LABEL = "merchant"
        tenant = SimpleNamespace(pk=1, is_active=False)
        mw = TenantMiddleware(get_response=lambda r: SimpleNamespace())
        mw._resolve_tenant = lambda r: (tenant, MagicMock())
        response = mw(SimpleNamespace())
        assert b"Merchant is inactive" in response.content
