"""Tests for boundary.middleware — TenantMiddleware."""

import pytest
from django.http import HttpResponse
from django.test import RequestFactory

from boundary.context import TenantContext
from boundary.middleware import TenantMiddleware


def _get_response(request):
    """Dummy view that records the tenant from context."""
    tenant = TenantContext.get()
    return HttpResponse(
        str(tenant.pk) if tenant else "no-tenant",
        content_type="text/plain",
    )


@pytest.fixture
def rf():
    return RequestFactory()


@pytest.fixture
def middleware():
    return TenantMiddleware(_get_response)


@pytest.mark.django_db
class TestResolverChain:
    """AC-RES-006: Chain fallthrough."""

    def test_fallthrough_to_second_resolver(self, rf, tenant_a, settings):
        settings.BOUNDARY_RESOLVERS = [
            "boundary.resolvers.HeaderResolver",
            "boundary.resolvers.SubdomainResolver",
        ]
        # No header, but subdomain matches
        request = rf.get("/", HTTP_HOST="club-a.example.com")
        mw = TenantMiddleware(_get_response)
        response = mw(request)
        assert response.content.decode() == str(tenant_a.pk)


@pytest.mark.django_db
class TestRequiredMode:
    """AC-RES-007/008: Required mode 404 vs optional mode proceeds."""

    def test_required_returns_404(self, rf, settings):
        settings.BOUNDARY_REQUIRED = True
        settings.BOUNDARY_RESOLVERS = ["boundary.resolvers.SubdomainResolver"]
        request = rf.get("/", HTTP_HOST="example.com")
        mw = TenantMiddleware(_get_response)
        response = mw(request)
        assert response.status_code == 404

    def test_optional_proceeds(self, rf, settings):
        settings.BOUNDARY_REQUIRED = False
        settings.BOUNDARY_RESOLVERS = ["boundary.resolvers.SubdomainResolver"]
        request = rf.get("/", HTTP_HOST="example.com")
        mw = TenantMiddleware(_get_response)
        response = mw(request)
        assert response.status_code == 200
        assert response.content.decode() == "no-tenant"


@pytest.mark.django_db
class TestInactiveTenant:
    """AC-RES-009: Inactive tenant returns 403."""

    def test_inactive_returns_403(self, rf, inactive_tenant, settings):
        settings.BOUNDARY_RESOLVERS = ["boundary.resolvers.HeaderResolver"]
        request = rf.get("/", HTTP_X_TENANT_ID=str(inactive_tenant.pk))
        mw = TenantMiddleware(_get_response)
        response = mw(request)
        assert response.status_code == 403


@pytest.mark.django_db
class TestResolverException:
    """AC-RES-010: Resolver exception is caught and logged."""

    def test_broken_resolver_falls_through(self, rf, tenant_a, settings):
        settings.BOUNDARY_RESOLVERS = [
            "test_middleware.BrokenResolver",
            "boundary.resolvers.HeaderResolver",
        ]
        request = rf.get("/", HTTP_X_TENANT_ID=str(tenant_a.pk))
        mw = TenantMiddleware(_get_response)
        response = mw(request)
        # Broken resolver is skipped; header resolver succeeds
        assert response.status_code == 200
        assert response.content.decode() == str(tenant_a.pk)


@pytest.mark.django_db
class TestContextLifecycle:
    """Verify context is set during request and cleared after."""

    def test_context_cleared_after_request(self, rf, tenant_a, settings):
        settings.BOUNDARY_RESOLVERS = ["boundary.resolvers.HeaderResolver"]
        request = rf.get("/", HTTP_X_TENANT_ID=str(tenant_a.pk))
        mw = TenantMiddleware(_get_response)
        mw(request)
        assert TenantContext.get() is None

    def test_request_has_tenant_attribute(self, rf, tenant_a, settings):
        settings.BOUNDARY_RESOLVERS = ["boundary.resolvers.HeaderResolver"]

        def check_request(request):
            assert request.tenant == tenant_a
            return HttpResponse("ok")

        request = rf.get("/", HTTP_X_TENANT_ID=str(tenant_a.pk))
        mw = TenantMiddleware(check_request)
        mw(request)


@pytest.mark.django_db
class TestWrapAtomic:
    """Verify BOUNDARY_WRAP_ATOMIC=False path."""

    def test_no_wrap_when_disabled(self, rf, tenant_a, settings):
        settings.BOUNDARY_WRAP_ATOMIC = False
        settings.BOUNDARY_RESOLVERS = ["boundary.resolvers.HeaderResolver"]
        request = rf.get("/", HTTP_X_TENANT_ID=str(tenant_a.pk))
        mw = TenantMiddleware(_get_response)
        response = mw(request)
        assert response.status_code == 200
        assert response.content.decode() == str(tenant_a.pk)


class TestSignals:
    """Verify signals are fired."""

    @pytest.mark.django_db
    def test_tenant_resolved_signal(self, rf, tenant_a, settings):
        from boundary.signals import tenant_resolved

        settings.BOUNDARY_RESOLVERS = ["boundary.resolvers.HeaderResolver"]
        received = []

        def handler(sender, **kwargs):
            received.append(kwargs)

        tenant_resolved.connect(handler)
        try:
            request = rf.get("/", HTTP_X_TENANT_ID=str(tenant_a.pk))
            mw = TenantMiddleware(_get_response)
            mw(request)
            assert len(received) == 1
            assert received[0]["tenant"] == tenant_a
        finally:
            tenant_resolved.disconnect(handler)

    @pytest.mark.django_db
    def test_resolution_failed_signal(self, rf, settings):
        from boundary.signals import tenant_resolution_failed

        settings.BOUNDARY_REQUIRED = True
        settings.BOUNDARY_RESOLVERS = ["boundary.resolvers.SubdomainResolver"]
        received = []

        def handler(sender, **kwargs):
            received.append(True)

        tenant_resolution_failed.connect(handler)
        try:
            request = rf.get("/", HTTP_HOST="example.com")
            mw = TenantMiddleware(_get_response)
            mw(request)
            assert len(received) == 1
        finally:
            tenant_resolution_failed.disconnect(handler)


# Test helper: a resolver that always raises
from boundary.resolvers import BaseResolver  # noqa: E402


class BrokenResolver(BaseResolver):
    def resolve(self, request):
        raise RuntimeError("I'm broken")
