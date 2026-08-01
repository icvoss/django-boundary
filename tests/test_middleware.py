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

    def test_inactive_hook_receives_exception(self, rf, inactive_tenant, settings):
        """Issue #6: _handle_inactive_tenant gets a TenantInactiveError."""
        from boundary.exceptions import TenantInactiveError

        settings.BOUNDARY_RESOLVERS = ["boundary.resolvers.HeaderResolver"]
        seen = []

        class RecordingMiddleware(TenantMiddleware):
            def _handle_inactive_tenant(self, request, tenant, exc):
                seen.append((tenant, exc))
                return super()._handle_inactive_tenant(request, tenant, exc)

        request = rf.get("/", HTTP_X_TENANT_ID=str(inactive_tenant.pk))
        response = RecordingMiddleware(_get_response)(request)

        assert response.status_code == 403
        assert len(seen) == 1
        tenant, exc = seen[0]
        assert tenant == inactive_tenant
        assert isinstance(exc, TenantInactiveError)


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

    def test_resolver_error_passed_to_hook(self, rf, tenant_a, settings):
        """Issue #6: _on_resolver_error receives a TenantResolutionError."""
        from boundary.exceptions import TenantResolutionError

        settings.BOUNDARY_RESOLVERS = [
            "test_middleware.BrokenResolver",
            "boundary.resolvers.HeaderResolver",
        ]
        seen = []

        class RecordingMiddleware(TenantMiddleware):
            def _on_resolver_error(self, request, resolver_path, error):
                seen.append((resolver_path, error))

        request = rf.get("/", HTTP_X_TENANT_ID=str(tenant_a.pk))
        RecordingMiddleware(_get_response)(request)

        assert len(seen) == 1
        path, error = seen[0]
        assert path == "test_middleware.BrokenResolver"
        assert isinstance(error, TenantResolutionError)
        assert isinstance(error.__cause__, RuntimeError)

    def test_on_resolver_error_can_abort(self, rf, tenant_a, settings):
        """A subclass can re-raise to abort instead of falling through."""
        from boundary.exceptions import TenantResolutionError

        settings.BOUNDARY_RESOLVERS = [
            "test_middleware.BrokenResolver",
            "boundary.resolvers.HeaderResolver",
        ]

        class StrictMiddleware(TenantMiddleware):
            def _on_resolver_error(self, request, resolver_path, error):
                raise error

        request = rf.get("/", HTTP_X_TENANT_ID=str(tenant_a.pk))
        with pytest.raises(TenantResolutionError):
            StrictMiddleware(_get_response)(request)


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


class TestAsyncCapability:
    """Regression for issue #16: TenantMiddleware must declare itself sync-only.

    MiddlewareMixin declares async_capable = True by default. If
    TenantMiddleware ever loses its own async_capable = False override
    (accidentally or via a "helpful" cleanup), Django goes back to treating
    it as async-native under ASGI: it hands the middleware an async
    get_response, __call__'s sync body calls it without awaiting, and the
    try/finally clears TenantContext (and exits the atomic block) before the
    view ever runs. Every async-served request would then run with no
    tenant in context and no RLS session variable, exactly the bug reported
    in #16.
    """

    def test_declares_sync_only(self):
        assert TenantMiddleware.sync_capable is True
        assert TenantMiddleware.async_capable is False


@pytest.mark.django_db
class TestAsgiIntegration:
    """AC for issue #16: an async view sees the tenant on both sides of an await.

    Drives a real async view through django.test.AsyncClient under ASGI, the
    exact path where the bug in #16 occurred: TenantMiddleware being
    misidentified as async-native and clearing context before the coroutine
    was awaited.
    """

    def test_tenant_present_before_and_after_await(self, tenant_a, settings):
        import json

        from asgiref.sync import async_to_sync
        from django.test import AsyncClient

        from boundary.context import TenantContext

        settings.MIDDLEWARE = ["boundary.middleware.TenantMiddleware"]
        settings.BOUNDARY_RESOLVERS = ["boundary.resolvers.HeaderResolver"]

        client = AsyncClient()
        response = async_to_sync(client.get)(
            "/tenant-across-await/",
            HTTP_X_TENANT_ID=str(tenant_a.pk),
        )

        assert response.status_code == 200
        body = json.loads(response.content)
        assert body["before"] == str(tenant_a.pk)
        assert body["after"] == str(tenant_a.pk)
        # Context is cleared once the (adapted, sync) middleware's atomic
        # block exits after the request completes.
        assert TenantContext.get() is None


# Test helper: a resolver that always raises
from boundary.resolvers import BaseResolver  # noqa: E402


class BrokenResolver(BaseResolver):
    def resolve(self, request):
        raise RuntimeError("I'm broken")
