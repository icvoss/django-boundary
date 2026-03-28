"""Tests for boundary.context — TenantContext."""

import pytest
from django.db import connection

from boundary.context import TenantContext
from boundary.exceptions import TenantNotSetError


class TestTenantContextSetAndGet:
    """AC-CTX-001: Set and get tenant."""

    def test_set_and_get(self, tenant_a):
        token = TenantContext.set(tenant_a)
        try:
            assert TenantContext.get() == tenant_a
        finally:
            TenantContext.clear(token)

    def test_get_returns_none_when_not_set(self):
        assert TenantContext.get() is None


class TestTenantContextClear:
    """AC-CTX-002: Clear restores previous."""

    def test_clear_restores_previous(self, tenant_a, tenant_b):
        token_a = TenantContext.set(tenant_a)
        try:
            token_b = TenantContext.set(tenant_b)
            assert TenantContext.get() == tenant_b
            TenantContext.clear(token_b)
            assert TenantContext.get() == tenant_a
        finally:
            TenantContext.clear(token_a)


class TestTenantContextNesting:
    """AC-CTX-003: Context manager nesting."""

    def test_nested_using(self, tenant_a, tenant_b):
        with TenantContext.using(tenant_a):
            assert TenantContext.get() == tenant_a
            with TenantContext.using(tenant_b):
                assert TenantContext.get() == tenant_b
            assert TenantContext.get() == tenant_a
        assert TenantContext.get() is None


class TestTenantContextRequire:
    """AC-CTX-004: Require raises when no tenant."""

    def test_require_raises(self):
        with pytest.raises(TenantNotSetError):
            TenantContext.require()

    def test_require_returns_tenant(self, tenant_a):
        with TenantContext.using(tenant_a):
            assert TenantContext.require() == tenant_a


@pytest.mark.django_db(transaction=True)
class TestTenantContextDBSession:
    """AC-CTX-005/006: DB session variable set and cleared."""

    def _get_session_var(self):
        with connection.cursor() as cursor:
            cursor.execute("SELECT current_setting('app.current_tenant_id', true)")
            return cursor.fetchone()[0]

    def test_db_session_variable_set(self, tenant_a):
        from django.db import transaction

        with transaction.atomic():
            token = TenantContext.set(tenant_a)
            try:
                val = self._get_session_var()
                assert val == str(tenant_a.pk)
            finally:
                TenantContext.clear(token)

    def test_db_session_variable_cleared(self, tenant_a):
        from django.db import transaction

        with transaction.atomic():
            token = TenantContext.set(tenant_a)
            TenantContext.clear(token)
            val = self._get_session_var()
            assert val == ""


@pytest.mark.django_db(transaction=True)
class TestTenantContextSavepointBehaviour:
    """AC-CTX-008: Nested context restores DB session variable after savepoint."""

    def _get_session_var(self):
        with connection.cursor() as cursor:
            cursor.execute("SELECT current_setting('app.current_tenant_id', true)")
            return cursor.fetchone()[0]

    def test_using_restores_db_var_after_inner_block(self, tenant_a, tenant_b):
        from django.db import transaction

        with transaction.atomic():
            token = TenantContext.set(tenant_a)
            try:
                assert self._get_session_var() == str(tenant_a.pk)

                with TenantContext.using(tenant_b):
                    assert self._get_session_var() == str(tenant_b.pk)

                # After exiting inner block, DB var should be restored
                assert self._get_session_var() == str(tenant_a.pk)
            finally:
                TenantContext.clear(token)


class TestTenantContextAtomicRollback:
    """BR-CTX-008: ContextVar rolled back if _set_db_session fails."""

    def test_contextvar_rolled_back_on_db_error(self, tenant_a, monkeypatch):
        original = TenantContext.get()

        def failing_set_db(*args, **kwargs):
            raise RuntimeError("DB failure")

        monkeypatch.setattr(TenantContext, "_set_db_session", staticmethod(failing_set_db))

        with pytest.raises(RuntimeError, match="DB failure"):
            TenantContext.set(tenant_a)

        # ContextVar should be restored to original
        assert TenantContext.get() == original
