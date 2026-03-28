"""Tests for boundary.resolvers — resolver chain and caching."""

import base64
import json
import time

import pytest
from django.test import RequestFactory

from boundary.resolvers import (
    ExplicitResolver,
    HeaderResolver,
    JWTClaimResolver,
    SessionResolver,
    SubdomainResolver,
    _cache_clear,
)


@pytest.fixture(autouse=True)
def clear_cache():
    """Clear resolver cache before each test."""
    _cache_clear()
    yield
    _cache_clear()


@pytest.fixture
def rf():
    return RequestFactory()


@pytest.mark.django_db
class TestSubdomainResolver:
    """AC-RES-001: Subdomain resolution."""

    def test_resolves_from_subdomain(self, rf, tenant_a):
        request = rf.get("/", HTTP_HOST="club-a.example.com")
        resolver = SubdomainResolver()
        assert resolver.resolve(request) == tenant_a

    def test_returns_none_for_unknown_subdomain(self, rf, tenant_a):
        request = rf.get("/", HTTP_HOST="unknown.example.com")
        resolver = SubdomainResolver()
        assert resolver.resolve(request) is None

    def test_returns_none_without_subdomain(self, rf):
        request = rf.get("/", HTTP_HOST="example.com")
        resolver = SubdomainResolver()
        assert resolver.resolve(request) is None


@pytest.mark.django_db
class TestHeaderResolver:
    """AC-RES-002/014: Header resolution (UUID first, slug fallback)."""

    def test_resolves_by_uuid(self, rf, tenant_a):
        request = rf.get("/", HTTP_X_TENANT_ID=str(tenant_a.pk))
        resolver = HeaderResolver()
        assert resolver.resolve(request) == tenant_a

    def test_resolves_by_slug_fallback(self, rf, tenant_a):
        request = rf.get("/", HTTP_X_TENANT_ID="club-a")
        resolver = HeaderResolver()
        assert resolver.resolve(request) == tenant_a

    def test_returns_none_for_missing_header(self, rf):
        request = rf.get("/")
        resolver = HeaderResolver()
        assert resolver.resolve(request) is None

    def test_returns_none_for_unknown_value(self, rf, tenant_a):
        request = rf.get("/", HTTP_X_TENANT_ID="nonexistent")
        resolver = HeaderResolver()
        assert resolver.resolve(request) is None


@pytest.mark.django_db
class TestSessionResolver:
    """AC-RES-004: Session resolution."""

    def test_resolves_from_session(self, rf, tenant_a):
        request = rf.get("/")
        # Simulate session
        request.session = {"boundary_tenant_id": str(tenant_a.pk)}
        resolver = SessionResolver()
        assert resolver.resolve(request) == tenant_a

    def test_returns_none_without_session(self, rf):
        request = rf.get("/")
        resolver = SessionResolver()
        assert resolver.resolve(request) is None


@pytest.mark.django_db
class TestJWTClaimResolver:
    """AC-RES-003: JWT claim resolution (no signature validation)."""

    def _make_jwt(self, payload):
        """Create a fake JWT with the given payload (no signature)."""
        header = base64.urlsafe_b64encode(json.dumps({"alg": "none"}).encode()).rstrip(b"=")
        body = base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=")
        return f"{header.decode()}.{body.decode()}.fakesig"

    def test_resolves_from_jwt_claim(self, rf, tenant_a):
        token = self._make_jwt({"tenant_id": str(tenant_a.pk)})
        request = rf.get("/", HTTP_AUTHORIZATION=f"Bearer {token}")
        resolver = JWTClaimResolver()
        assert resolver.resolve(request) == tenant_a

    def test_returns_none_without_auth_header(self, rf):
        request = rf.get("/")
        resolver = JWTClaimResolver()
        assert resolver.resolve(request) is None

    def test_returns_none_for_invalid_jwt(self, rf):
        request = rf.get("/", HTTP_AUTHORIZATION="Bearer not.a.jwt.at.all")
        resolver = JWTClaimResolver()
        assert resolver.resolve(request) is None

    def test_returns_none_for_missing_claim(self, rf):
        token = self._make_jwt({"sub": "user123"})
        request = rf.get("/", HTTP_AUTHORIZATION=f"Bearer {token}")
        resolver = JWTClaimResolver()
        assert resolver.resolve(request) is None

    def test_returns_none_for_nonexistent_tenant(self, rf):
        token = self._make_jwt({"tenant_id": "99999"})
        request = rf.get("/", HTTP_AUTHORIZATION=f"Bearer {token}")
        resolver = JWTClaimResolver()
        assert resolver.resolve(request) is None

    def test_returns_none_for_non_bearer_auth(self, rf):
        request = rf.get("/", HTTP_AUTHORIZATION="Basic dXNlcjpwYXNz")
        resolver = JWTClaimResolver()
        assert resolver.resolve(request) is None


@pytest.mark.django_db
class TestExplicitResolver:
    """AC-RES-005: Explicit resolution."""

    def test_resolves_from_request_attribute(self, rf, tenant_a):
        request = rf.get("/")
        request.boundary_tenant = tenant_a
        resolver = ExplicitResolver()
        assert resolver.resolve(request) == tenant_a

    def test_returns_none_without_attribute(self, rf):
        request = rf.get("/")
        resolver = ExplicitResolver()
        assert resolver.resolve(request) is None


@pytest.mark.django_db
class TestResolverCache:
    """AC-RES-011/012/013: Cache hit, invalidation, TTL."""

    def test_cache_hit_avoids_query(self, rf, tenant_a, django_assert_num_queries):
        resolver = SubdomainResolver()
        request = rf.get("/", HTTP_HOST="club-a.example.com")

        # First call hits DB
        resolver.resolve(request)

        # Second call should use cache (0 queries)
        with django_assert_num_queries(0):
            result = resolver.resolve(request)
        assert result == tenant_a

    def test_cache_invalidated_on_save(self, rf, tenant_a):
        resolver = SubdomainResolver()
        request = rf.get("/", HTTP_HOST="club-a.example.com")

        # Populate cache
        resolver.resolve(request)

        # Save tenant (triggers post_save -> cache invalidation)
        tenant_a.name = "Updated"
        tenant_a.save()

        # Next resolve should hit DB again
        result = resolver.resolve(request)
        assert result == tenant_a

    def test_cache_expires_after_ttl(self, rf, tenant_a, settings):
        settings.BOUNDARY_RESOLVER_CACHE_TTL = 0  # Expire immediately
        resolver = SubdomainResolver()
        request = rf.get("/", HTTP_HOST="club-a.example.com")

        resolver.resolve(request)
        time.sleep(0.01)  # Ensure TTL has passed

        # Should miss cache due to TTL
        result = resolver.resolve(request)
        assert result == tenant_a
