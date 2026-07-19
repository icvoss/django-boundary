"""Tests for boundary.context — TenantContext."""

import pytest
from django.db import connection

from boundary.context import TenantContext, tenant_scoped
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


@pytest.mark.django_db(transaction=True)
class TestTenantContextAutocommit:
    """Regression for #6: using() must not silently no-op in autocommit.

    ``@pytest.mark.django_db(transaction=True)`` runs the test itself without
    an ambient transaction (real autocommit), the same condition management
    commands and Celery workers run under in production. Without the fix,
    ``set_config(..., true)`` set inside ``using()`` vanishes before the
    assertion's own SELECT, because each is its own implicit transaction.
    """

    def _get_session_var(self):
        from django.db import connection

        with connection.cursor() as cursor:
            cursor.execute("SELECT current_setting('app.current_tenant_id', true)")
            return cursor.fetchone()[0]

    def test_using_sets_db_session_var_outside_any_transaction(self, tenant_a):
        """BR-CTX-003: using() must open its own transaction when none is active."""
        from django.db import connection

        assert connection.in_atomic_block is False  # sanity: genuinely autocommit

        with TenantContext.using(tenant_a):
            assert self._get_session_var() == str(tenant_a.pk)

    def test_using_write_survives_rls_outside_any_transaction(self, tenant_a):
        """A tenant-scoped write inside using() must not hit an empty RLS var
        when called with no surrounding transaction.atomic() (the exact
        failure mode reported in #6: an opaque RLS violation from a
        management command or Celery task)."""
        from boundary_testapp.models import Booking
        from django.db import connection

        assert connection.in_atomic_block is False

        with TenantContext.using(tenant_a):
            booking = Booking.objects.create(court=1)

        assert booking.tenant_id == tenant_a.pk

    def test_using_is_noop_wrap_when_already_atomic(self, tenant_a):
        """using() must not open a redundant nested transaction when one is
        already active (e.g. called from inside TenantMiddleware's atomic
        block, or nested using() calls)."""
        from django.db import transaction

        with transaction.atomic(), TenantContext.using(tenant_a):
            assert self._get_session_var() == str(tenant_a.pk)
            # Still the *same* outer transaction, not a new one.
            assert transaction.get_connection().in_atomic_block is True

    def test_wrap_atomic_false_leaves_var_unset_and_warns(self, tenant_a, settings, caplog):
        """With BOUNDARY_WRAP_ATOMIC=False, using() must not silently pretend
        to work: the session variable has no effect (documented trade-off),
        and a warning is logged identifying the call as ineffective."""
        settings.BOUNDARY_WRAP_ATOMIC = False

        with caplog.at_level("WARNING", logger="boundary.context"), TenantContext.using(tenant_a):
            val = self._get_session_var()

        assert val == ""
        assert any("outside an active transaction" in record.message for record in caplog.records)


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


@pytest.mark.django_db
class TestTenantScopedDecorator:
    """tenant_scoped runs the function inside TenantContext.using()."""

    def test_named_arg(self, tenant_a):
        @tenant_scoped("club")
        def inner(club):
            return TenantContext.get()

        assert inner(club=tenant_a) == tenant_a
        assert TenantContext.get() is None  # restored after

    def test_positional_arg(self, tenant_a):
        @tenant_scoped("club")
        def inner(club):
            return TenantContext.get()

        assert inner(tenant_a) == tenant_a

    def test_default_arg_name_from_setting(self, tenant_a, settings):
        settings.BOUNDARY_TENANT_FK_FIELD = "merchant"

        @tenant_scoped()
        def inner(merchant):
            return TenantContext.get()

        assert inner(merchant=tenant_a) == tenant_a

    def test_missing_arg_raises_typeerror(self, tenant_a):
        @tenant_scoped("merchant")
        def inner(something_else):
            return TenantContext.get()

        with pytest.raises(TypeError, match="no argument 'merchant'"):
            inner(something_else=tenant_a)

    def test_nested_scope_restores_previous(self, tenant_a, tenant_b):
        @tenant_scoped("club")
        def inner(club):
            return TenantContext.get()

        with TenantContext.using(tenant_a):
            assert inner(tenant_b) == tenant_b
            # previous scope restored
            assert TenantContext.get() == tenant_a

    def test_exception_in_body_restores_context(self, tenant_a):
        @tenant_scoped("club")
        def inner(club):
            raise ValueError("boom")

        with pytest.raises(ValueError, match="boom"):
            inner(tenant_a)
        assert TenantContext.get() is None

    def test_scope_makes_manager_filter(self, tenant_a, tenant_b):
        from boundary_testapp.models import Booking

        with TenantContext.using(tenant_a):
            Booking.objects.create(court=1)
        with TenantContext.using(tenant_b):
            Booking.objects.create(court=2)

        @tenant_scoped("club")
        def count_for(club):
            return Booking.objects.count()

        assert count_for(tenant_a) == 1
        assert count_for(tenant_b) == 1
