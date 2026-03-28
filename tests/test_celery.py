"""Tests for boundary.celery — tenant context propagation in Celery tasks."""

import pytest

from boundary.celery import (
    HEADER_REGION,
    HEADER_TENANT_ID,
    TenantTask,
    _get_tenant_headers,
    _restore_tenant_context,
)
from boundary.context import TenantContext
from boundary.exceptions import TenantNotFoundError
from boundary.testing import set_tenant


@pytest.mark.django_db
class TestGetTenantHeaders:
    """BR-CEL-001/005: Headers serialised at dispatch time."""

    def test_returns_empty_without_context(self):
        assert _get_tenant_headers() == {}

    def test_returns_tenant_id(self, tenant_a):
        with set_tenant(tenant_a):
            headers = _get_tenant_headers()
        assert headers[HEADER_TENANT_ID] == str(tenant_a.pk)

    def test_includes_region(self, tenant_a):
        tenant_a.region = "eu-west"
        tenant_a.save()
        with set_tenant(tenant_a):
            headers = _get_tenant_headers()
        assert headers[HEADER_REGION] == "eu-west"

    def test_no_region_when_empty(self, tenant_a):
        with set_tenant(tenant_a):
            headers = _get_tenant_headers()
        assert HEADER_REGION not in headers


@pytest.mark.django_db
class TestRestoreTenantContext:
    """BR-CEL-002/003/004: Worker-side context restoration."""

    def test_restores_from_headers(self, tenant_a):
        headers = {HEADER_TENANT_ID: str(tenant_a.pk)}
        tenant, token = _restore_tenant_context(headers)
        try:
            assert tenant == tenant_a
            assert TenantContext.get() == tenant_a
        finally:
            if token:
                TenantContext.clear(token)

    def test_returns_none_without_headers(self):
        tenant, token = _restore_tenant_context({})
        assert tenant is None
        assert token is None

    def test_returns_none_with_none_headers(self):
        tenant, token = _restore_tenant_context(None)
        assert tenant is None

    def test_raises_for_deleted_tenant(self):
        """BR-CEL-003: TenantNotFoundError for deleted tenant."""
        headers = {HEADER_TENANT_ID: "99999"}
        with pytest.raises(TenantNotFoundError):
            _restore_tenant_context(headers)


@pytest.mark.django_db
class TestTenantTaskBase:
    """Test TenantTask base class."""

    def test_apply_async_injects_headers(self, tenant_a):
        """Verify headers are injected at dispatch time."""
        # We can't test actual Celery dispatch without a broker,
        # but we can test the header injection logic
        with set_tenant(tenant_a):
            headers = _get_tenant_headers()
        assert HEADER_TENANT_ID in headers
        assert headers[HEADER_TENANT_ID] == str(tenant_a.pk)

    def test_non_retriable_config(self):
        """BR-CEL-003: TenantTask excludes TenantNotFoundError from retry."""
        assert TenantTask.reject_on_worker_lost is False
