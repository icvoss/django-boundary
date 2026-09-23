"""Tests for boundary.context — TenantContext."""

import pytest
from django.db import connection

from boundary.context import TenantContext, admin_bypass, tenant_scoped
from boundary.exceptions import AdminBypassNotActiveError, TenantNotSetError


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


@pytest.mark.rls
@pytest.mark.django_db(transaction=True)
class TestTenantContextClearRestoresDbSessionVar:
    """Regression for issue #13: clear() must restore the previous tenant's
    DB session variable, not just the ContextVar.

    Before the fix, clear() reset the ContextVar to the previous tenant but
    only ever cleared the session variable on the removed tenant's aliases:
    it never re-applied the previous tenant's pk. After a nested
    set(a), set(b), clear(token_b), TenantContext.get() correctly reported
    tenant A while current_setting() still read the empty string, so RLS saw
    no tenant even though application code believed one was active.
    """

    def _get_session_var(self):
        from boundary.conf import boundary_settings

        with connection.cursor() as cursor:
            cursor.execute(f"SELECT current_setting('{boundary_settings.DB_SESSION_VAR}', true)")
            return cursor.fetchone()[0]

    def test_clear_reapplies_previous_tenant_session_var(self, tenant_a, tenant_b):
        from django.db import transaction

        with transaction.atomic():
            token_a = TenantContext.set(tenant_a)
            token_b = TenantContext.set(tenant_b)

            TenantContext.clear(token_b)
            assert TenantContext.get() == tenant_a
            assert self._get_session_var() == str(tenant_a.pk), (
                "clear() must re-apply the previous tenant's pk to the session variable, not leave it empty"
            )

            TenantContext.clear(token_a)
            assert TenantContext.get() is None
            assert self._get_session_var() in ("", None)


class TestTenantContextClearRestoresRegionalAlias:
    """Regression for issue #13, regional variant (BR-CTX-009).

    After nested set/set/clear where the removed tenant and the previous
    tenant are in DIFFERENT regions, clear() must clear the removed tenant's
    aliases (default + its region) and re-set the previous tenant's variable
    on ITS OWN alias set (default + its own, different, region). Uses mocked
    _set_db_session/_clear_db_session (as the existing regional tests in
    this module do) since the test settings define only a default database.
    """

    def _regional_tenant(self, pk, region):
        from boundary_testapp.models import Tenant

        t = Tenant(name=f"Tenant {pk}", slug=f"tenant-{pk}")
        t.region = region
        t.pk = pk
        return t

    def test_clear_restores_previous_tenant_on_its_own_region(self, settings, monkeypatch):
        settings.BOUNDARY_REGIONS = {"eu-west": {}, "us-east": {}}

        set_calls = []
        clear_calls = []
        monkeypatch.setattr(
            TenantContext,
            "_set_db_session",
            staticmethod(lambda tenant_id, using="default": set_calls.append((tenant_id, using))),
        )
        monkeypatch.setattr(
            TenantContext,
            "_clear_db_session",
            staticmethod(lambda using="default": clear_calls.append(using)),
        )

        previous_tenant = self._regional_tenant(1, "us-east")
        removed_tenant = self._regional_tenant(2, "eu-west")

        token_previous = TenantContext.set(previous_tenant)
        token_removed = TenantContext.set(removed_tenant)
        set_calls.clear()
        clear_calls.clear()

        TenantContext.clear(token_removed)

        assert TenantContext.get() == previous_tenant
        # The removed tenant's aliases (default + eu-west) were cleared.
        assert "default" in clear_calls
        assert "eu-west" in clear_calls
        # The previous tenant's variable was re-set on its OWN aliases
        # (default + us-east), not on the removed tenant's region.
        restored_aliases = [using for _tid, using in set_calls]
        assert "default" in restored_aliases
        assert "us-east" in restored_aliases
        assert "eu-west" not in restored_aliases
        restored_ids = {tid for tid, _using in set_calls}
        assert restored_ids == {str(previous_tenant.pk)}

        TenantContext.clear(token_previous)


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


@pytest.mark.rls
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


@pytest.mark.rls
@pytest.mark.django_db(transaction=True)
class TestSetDbSessionVarOptOut:
    """Issue #53: BOUNDARY_SET_DB_SESSION_VAR gates whether the session
    variable is written at all.

    The "off" arm asserts zero queries with django_assert_num_queries(0):
    if _set_db_session/_clear_db_session were made to issue SQL regardless
    of the setting, this would fail on a query count, not on an exception.
    The "on" arm re-proves today's behaviour is unchanged at the default.
    """

    def _get_session_var(self):
        with connection.cursor() as cursor:
            cursor.execute("SELECT current_setting('app.current_tenant_id', true)")
            return cursor.fetchone()[0]

    def test_opt_out_issues_no_queries_on_set_and_clear(self, tenant_a, settings, django_assert_num_queries):
        from django.db import transaction

        settings.BOUNDARY_SET_DB_SESSION_VAR = False

        with transaction.atomic():
            with django_assert_num_queries(0):
                token = TenantContext.set(tenant_a)
            with django_assert_num_queries(0):
                TenantContext.clear(token)

    def test_default_still_issues_the_set_config_queries(self, tenant_a, settings, django_assert_num_queries):
        from django.db import transaction

        settings.BOUNDARY_SET_DB_SESSION_VAR = True

        with transaction.atomic():
            with django_assert_num_queries(1):
                token = TenantContext.set(tenant_a)
            assert self._get_session_var() == str(tenant_a.pk)
            with django_assert_num_queries(1):
                TenantContext.clear(token)
            assert self._get_session_var() == ""


@pytest.mark.rls
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


@pytest.mark.rls
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


class TestEnsureAtomicUnconfiguredAlias:
    """Issue #56: _ensure_atomic()'s ConnectionDoesNotExist swallow must log.

    Before the fix, the except ConnectionDoesNotExist branch returned
    nullcontext() with no log line at all, so a caller could not tell "the
    alias does not exist, atomicity was skipped" from "no transaction was
    needed". Debug, not warning: the contract classifies this branch as
    deliberate tolerance (a regional alias configured in BOUNDARY_REGIONS but
    absent from DATABASES, typically in routing-only tests).
    """

    def test_unconfigured_alias_logs_debug_naming_alias(self, caplog):
        from boundary.context import _ensure_atomic

        with caplog.at_level("DEBUG", logger="boundary.context"):
            ctx = _ensure_atomic(using="nonexistent_alias")

        assert ctx is not None  # nullcontext(); still usable as a context manager
        records = [r for r in caplog.records if r.name == "boundary.context"]
        assert any(r.levelname == "DEBUG" and getattr(r, "using", None) == "nonexistent_alias" for r in records), (
            "expected a DEBUG record naming the unconfigured alias"
        )


@pytest.mark.django_db(transaction=True)
class TestSetClearDbSessionUnopenedConnection:
    """Issue #56: _set_db_session/_clear_db_session must warn, not no-op
    silently, when the connection has never been opened.

    This is a real RLS risk: the ContextVar is set correctly, but no
    database state changes, so RLS on that connection sees no tenant if it
    is later opened without going through this call again. Driven by
    calling the methods directly against an alias whose connection is
    guaranteed unopened, rather than mocking the logger.

    The three tests asserting the warning IS issued are marked ``rls``. The
    ``unopened`` alias they build is a copy of ``default``, so on the SQLite
    leg it is a SQLite alias, and BR-ENV-002's per-alias vendor gate then
    correctly returns BEFORE the connection-open check, issuing nothing. That
    is the specified behaviour, not a defect: the warning exists because a
    missing session variable costs RLS its tenant, and an alias with no RLS
    has nothing to lose. Asserting the warning there would assert against
    BR-CTX-002. The opt-out test in this class needs no PostgreSQL, since it
    asserts silence, and stays unmarked.
    """

    @pytest.mark.rls
    def test_set_db_session_warns_naming_alias_and_tenant(self, caplog, settings):
        settings.DATABASES = {**settings.DATABASES, "unopened": dict(settings.DATABASES["default"])}
        from django.db import connections

        # DATABASES is in Django's COMPLEX_OVERRIDE_SETTINGS: the settings
        # fixture updates django.conf.settings but ConnectionHandler.settings
        # is a cached_property with no setting_changed receiver resetting it
        # for this key. configure_settings() also short-circuits on the
        # already-populated _settings ivar, so both must be cleared before
        # the new alias becomes visible.
        connections._settings = None
        del connections.settings

        assert connections["unopened"].connection is None  # sanity: genuinely unopened

        with caplog.at_level("WARNING", logger="boundary.context"):
            TenantContext._set_db_session("some-tenant-id", using="unopened")

        records = [r for r in caplog.records if r.name == "boundary.context"]
        assert any(
            r.levelname == "WARNING"
            and getattr(r, "using", None) == "unopened"
            and getattr(r, "tenant_id", None) == "some-tenant-id"
            for r in records
        ), "expected a WARNING record naming the unopened alias and tenant id"

    @pytest.mark.rls
    def test_clear_db_session_warns_naming_alias(self, caplog, settings):
        settings.DATABASES = {**settings.DATABASES, "unopened": dict(settings.DATABASES["default"])}
        from django.db import connections

        # See test_set_db_session_warns_naming_alias_and_tenant: DATABASES
        # needs the ConnectionHandler cache cleared by hand.
        connections._settings = None
        del connections.settings

        assert connections["unopened"].connection is None  # sanity: genuinely unopened

        with caplog.at_level("WARNING", logger="boundary.context"):
            TenantContext._clear_db_session(using="unopened")

        records = [r for r in caplog.records if r.name == "boundary.context"]
        assert any(r.levelname == "WARNING" and getattr(r, "using", None) == "unopened" for r in records), (
            "expected a WARNING record naming the unopened alias"
        )

    def test_opt_out_silences_the_unopened_connection_warning(self, caplog, settings):
        """Merge interaction (#53 opt-out x #56 warning): BOUNDARY_SET_DB_SESSION_VAR=False
        must return before the connection-open check runs at all, so an
        explicit opt-out on an unopened alias is silent, not a WARNING.

        Both _set_db_session and _clear_db_session gained code from two
        different branches that touch the same function: #53 added an early
        `if not boundary_settings.SET_DB_SESSION_VAR: return` and #56 added
        an `else` branch warning when the connection is unopened. Git merged
        them without a conflict; this proves the opt-out's early return
        actually precedes the connection check in the merged source, not
        just that both branches exist. If the order were reversed (the
        warning check running before the opt-out's return), this assertion
        would fail because the warning would fire regardless of the setting.
        """
        settings.BOUNDARY_SET_DB_SESSION_VAR = False
        settings.DATABASES = {**settings.DATABASES, "unopened": dict(settings.DATABASES["default"])}
        from django.db import connections

        # See test_set_db_session_warns_naming_alias_and_tenant: DATABASES
        # needs the ConnectionHandler cache cleared by hand.
        connections._settings = None
        del connections.settings

        assert connections["unopened"].connection is None  # sanity: genuinely unopened

        with caplog.at_level("WARNING", logger="boundary.context"):
            TenantContext._set_db_session("some-tenant-id", using="unopened")
            TenantContext._clear_db_session(using="unopened")

        records = [r for r in caplog.records if r.name == "boundary.context"]
        assert records == [], f"expected no WARNING records with the opt-out set, got: {[r.message for r in records]}"

    @pytest.mark.rls
    def test_default_still_warns_on_the_same_unopened_alias(self, caplog, settings):
        """Companion to test_opt_out_silences_the_unopened_connection_warning:
        with BOUNDARY_SET_DB_SESSION_VAR at its True default, the same
        unopened alias DOES produce the #56 warning on both methods. Paired
        with the opt-out test, this pins the merged ordering: the opt-out
        return is unconditional and precedes the connection check, and the
        connection check itself is unchanged from #56 at the default.
        """
        settings.BOUNDARY_SET_DB_SESSION_VAR = True
        settings.DATABASES = {**settings.DATABASES, "unopened": dict(settings.DATABASES["default"])}
        from django.db import connections

        connections._settings = None
        del connections.settings

        assert connections["unopened"].connection is None  # sanity: genuinely unopened

        with caplog.at_level("WARNING", logger="boundary.context"):
            TenantContext._set_db_session("some-tenant-id", using="unopened")
            TenantContext._clear_db_session(using="unopened")

        records = [r for r in caplog.records if r.name == "boundary.context" and r.levelname == "WARNING"]
        assert len(records) == 2, (
            f"expected a WARNING from both _set_db_session and _clear_db_session, got: {[r.message for r in records]}"
        )


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


class TestRegionalSessionVarUnit:
    """Issue #7 / BR-CTX-009: alias resolution for the regional session var.

    Pure-unit tests of the alias helpers (no DB): a tenant whose region is a
    configured region routes its tenant-scoped queries to that regional
    connection, so the session var must be set there too, not only on default.
    """

    def test_target_aliases_default_only_without_regions(self, settings):
        from boundary.context import _target_aliases

        settings.BOUNDARY_REGIONS = None

        class T:
            pk = 1
            region = "eu-west"

        assert _target_aliases(T()) == ["default"]

    def test_target_aliases_includes_regional_alias(self, settings):
        from boundary.context import _target_aliases

        settings.BOUNDARY_REGIONS = {"eu-west": {}, "us": {}}

        class T:
            pk = 1
            region = "eu-west"

        assert _target_aliases(T()) == ["default", "eu-west"]

    def test_target_aliases_unknown_region_is_default_only(self, settings):
        from boundary.context import _target_aliases

        settings.BOUNDARY_REGIONS = {"eu-west": {}}

        class T:
            pk = 1
            region = "ap-southeast"  # not configured

        assert _target_aliases(T()) == ["default"]

    def test_target_aliases_none_tenant(self):
        from boundary.context import _target_aliases

        assert _target_aliases(None) == ["default"]


class TestRegionalSessionVarIsSet:
    """Issue #7 / BR-CTX-009: the session var is set ON the regional connection.

    The test settings define only a ``default`` database, so these spy on
    ``_set_db_session`` / ``_clear_db_session`` to assert the fix sets and
    clears the variable on the tenant's regional alias, which pre-fix never
    happened (RLS on the regional DB saw an empty tenant, silent mis-scoping).
    """

    def _regional_tenant(self):
        from boundary_testapp.models import Tenant

        t = Tenant(name="EU Club", slug="eu-club")
        t.region = "eu-west"
        return t

    def test_set_writes_session_var_on_regional_alias(self, settings, monkeypatch):
        settings.BOUNDARY_REGIONS = {"eu-west": {}, "us": {}}
        calls = []
        monkeypatch.setattr(
            TenantContext,
            "_set_db_session",
            staticmethod(lambda tenant_id, using="default": calls.append((tenant_id, using))),
        )

        tenant = self._regional_tenant()
        tenant.pk = 42
        TenantContext.set(tenant)

        aliases = [using for _tid, using in calls]
        assert "default" in aliases
        assert "eu-west" in aliases, "session var must be set on the tenant's regional connection (BR-CTX-009)"

    def test_no_regional_write_when_regions_unconfigured(self, settings, monkeypatch):
        settings.BOUNDARY_REGIONS = None
        calls = []
        monkeypatch.setattr(
            TenantContext,
            "_set_db_session",
            staticmethod(lambda tenant_id, using="default": calls.append((tenant_id, using))),
        )

        tenant = self._regional_tenant()
        tenant.pk = 42
        TenantContext.set(tenant)

        assert [using for _tid, using in calls] == ["default"]

    def test_clear_clears_regional_alias(self, settings, monkeypatch):
        settings.BOUNDARY_REGIONS = {"eu-west": {}}
        monkeypatch.setattr(TenantContext, "_set_db_session", staticmethod(lambda *a, **k: None))
        cleared = []
        monkeypatch.setattr(
            TenantContext,
            "_clear_db_session",
            staticmethod(lambda using="default": cleared.append(using)),
        )

        tenant = self._regional_tenant()
        tenant.pk = 42
        token = TenantContext.set(tenant)
        TenantContext.clear(token)

        assert "default" in cleared
        assert "eu-west" in cleared, "regional connection must be cleared too, not left carrying a stale tenant"


def _get_admin_flag():
    with connection.cursor() as cursor:
        cursor.execute("SELECT current_setting('app.boundary_admin', true)")
        return cursor.fetchone()[0]


@pytest.mark.rls
@pytest.mark.django_db(transaction=True)
class TestAdminBypassFlagLifecycle:
    """Issue #37 AC: the admin flag is set inside the block and cleared after.

    ``transaction=True`` runs the test itself without an ambient transaction
    (real autocommit), matching a management command or Celery task, the same
    condition ``TestTenantContextAutocommit`` exercises for the tenant
    variable. Positive control: ``test_flag_is_true_inside_block`` proves the
    absence check in ``test_flag_is_cleared_after_block`` could have caught a
    flag that was never cleared, rather than the assertion being vacuously
    true because the flag was never set in the first place.
    """

    def test_flag_is_true_inside_block(self):
        assert connection.in_atomic_block is False  # genuinely autocommit
        with admin_bypass():
            assert _get_admin_flag() == "true"

    def test_flag_is_cleared_after_block(self):
        with admin_bypass():
            pass
        assert _get_admin_flag() == ""

    def test_flag_is_cleared_after_exception_in_block(self):
        with pytest.raises(ValueError), admin_bypass():
            assert _get_admin_flag() == "true"
            raise ValueError("boom")
        assert _get_admin_flag() == ""

    def test_honours_custom_admin_flag_var_setting(self, settings):
        """BR-CTX-010 must read BOUNDARY_ADMIN_FLAG_VAR, never hardcode the
        default variable name (mirrors TestCustomSessionVariables in
        test_rls.py for the migration SQL side of the same setting)."""
        settings.BOUNDARY_ADMIN_FLAG_VAR = "myapp.is_admin"

        with admin_bypass():
            with connection.cursor() as cursor:
                cursor.execute("SELECT current_setting('myapp.is_admin', true)")
                assert cursor.fetchone()[0] == "true"
            # The default variable name must NOT have been touched: it was
            # never set on this connection at all, so current_setting(...,
            # true) returns SQL NULL (Python None), not the empty string a
            # set-then-cleared variable would read back as.
            assert _get_admin_flag() is None

        with connection.cursor() as cursor:
            cursor.execute("SELECT current_setting('myapp.is_admin', true)")
            assert cursor.fetchone()[0] == ""


@pytest.mark.rls
@pytest.mark.django_db(transaction=True)
class TestAdminBypassTransactionLocal:
    """The flag does not survive past the block (transaction-local guarantee).

    Distinct from the lifecycle tests above: this proves the underlying
    set_config call really did use the transaction-local (true) form, by
    checking from a SEPARATE connection while the first is still mid-block,
    the same way TestRLSEnforcement's app_conn tests check isolation from
    outside the connection that set the session variable.
    """

    def test_flag_not_visible_on_a_second_connection_while_active(self, app_conn):
        """A positive control for the isolation claim: while admin_bypass()
        holds the flag on the Django `default` connection, a second, entirely
        separate connection (app_conn) must never see it, proving the flag is
        connection-local, not process-global or somehow shared."""
        with admin_bypass():
            assert _get_admin_flag() == "true"
            with app_conn.cursor() as cur:
                cur.execute("SELECT current_setting('app.boundary_admin', true)")
                # app_conn has never set this variable at all, so an
                # untouched GUC reads back as SQL NULL (Python None), not
                # the empty string a set-then-cleared variable would show.
                assert cur.fetchone()[0] is None, "flag must not leak onto an unrelated connection"


@pytest.mark.rls
@pytest.mark.django_db(transaction=True)
class TestAdminBypassWrapAtomicFalse:
    """Issue #37: BOUNDARY_WRAP_ATOMIC=False with no ambient transaction.

    Mirrors TestTenantContextAutocommit.test_wrap_atomic_false_leaves_var_unset_and_warns,
    but admin_bypass() makes a stricter choice than TenantContext.using() for
    this one setting: rather than silently leaving the flag unset (a
    correctness bug that is not self-announcing for the most privileged
    variable in the package), it verifies the flag actually took with a
    read-back and raises AdminBypassNotActiveError when it did not, instead
    of proceeding to yield control believing the bypass is active.
    """

    def test_wrap_atomic_false_without_transaction_raises(self, settings):
        settings.BOUNDARY_WRAP_ATOMIC = False
        assert connection.in_atomic_block is False

        with pytest.raises(AdminBypassNotActiveError), admin_bypass():
            pytest.fail("block body must not run when the flag never activated")

        # The flag must not be left set on the connection after the raise.
        assert _get_admin_flag() == ""

    def test_wrap_atomic_false_with_explicit_atomic_block_works(self, settings):
        """The escape hatch: a caller managing transactions explicitly can
        still use admin_bypass() by wrapping it in transaction.atomic()."""
        from django.db import transaction

        settings.BOUNDARY_WRAP_ATOMIC = False

        with transaction.atomic(), admin_bypass():
            assert _get_admin_flag() == "true"


@pytest.mark.rls
@pytest.mark.django_db(transaction=True)
class TestAdminBypassCleanupExceptionSafety:
    """Issue #60: admin_bypass()'s finally cleanup must not mask the
    caller's original exception.

    Before the fix, if the block raised while the surrounding transaction
    was already aborted (e.g. a real PostgreSQL error), the finally block's
    own unguarded cursor.execute(set_config...) raised
    TransactionManagementError against the aborted transaction, and Python
    propagated THAT as what the caller sees, not the original exception. A
    caller's `except IntegrityError:` (or any other specific type) around
    the `with admin_bypass():` block would never fire.

    The fault is driven end-to-end: a deliberately invalid statement run
    inside the block raises a real DB error and aborts the actual
    transaction (not a mock), so the finally block's own cursor.execute
    genuinely fails against that aborted transaction, exactly as issue #60
    describes. On the unfixed code, the assertion on the exception TYPE
    (pytest.raises(ProgrammingError), not TransactionManagementError) is
    what fails: the caller sees TransactionManagementError instead.
    """

    def test_original_exception_propagates_when_cleanup_also_fails(self, caplog):
        from django.db import connection
        from django.db.utils import ProgrammingError

        assert connection.in_atomic_block is False  # genuinely autocommit

        with (
            caplog.at_level("WARNING", logger="boundary.context"),
            pytest.raises(ProgrammingError),
            admin_bypass(),
        ):
            assert _get_admin_flag() == "true"
            with connection.cursor() as cursor:
                # Deliberately invalid SQL: raises ProgrammingError
                # and aborts the real transaction, so the finally
                # block's own cursor.execute genuinely fails against
                # an aborted transaction below.
                cursor.execute("SELECT boundary_nonexistent_function()")

        records = [r for r in caplog.records if r.name == "boundary.context"]
        assert any(r.levelname == "WARNING" and "Failed to clear admin bypass flag" in r.message for r in records), (
            "expected a WARNING record naming the failed cleanup"
        )


@pytest.mark.rls
@pytest.mark.django_db(transaction=True)
class TestAdminBypassNesting:
    """Issue #37: reentrant use on the same alias is idempotent.

    Only the outermost admin_bypass() call clears the flag on exit; an inner
    call's exit must not turn off an outer call's still-active bypass.
    """

    def test_inner_exit_does_not_clear_outer_flag(self):
        with admin_bypass():
            assert _get_admin_flag() == "true"
            with admin_bypass():
                assert _get_admin_flag() == "true"
            # Inner block exited; outer bypass must still be active.
            assert _get_admin_flag() == "true"
        # Outer block exited; now it is cleared.
        assert _get_admin_flag() == ""

    def test_entering_while_tenant_active_requires_no_special_handling(self, tenant_a):
        """Both session variables coexist: entering admin_bypass() while a
        tenant is active in TenantContext sets the admin flag without
        disturbing the tenant variable, and both are independently readable."""
        with TenantContext.using(tenant_a):
            with admin_bypass():
                assert _get_admin_flag() == "true"
                with connection.cursor() as cursor:
                    cursor.execute("SELECT current_setting('app.current_tenant_id', true)")
                    assert cursor.fetchone()[0] == str(tenant_a.pk)
            # admin_bypass() exited; tenant context is still active and unaffected.
            with connection.cursor() as cursor:
                cursor.execute("SELECT current_setting('app.current_tenant_id', true)")
                assert cursor.fetchone()[0] == str(tenant_a.pk)


@pytest.mark.rls
@pytest.mark.django_db(transaction=True)
class TestAdminBypassSignal:
    """Issue #37: an auditable signal fires on entry with the expected payload."""

    def test_signal_fires_on_entry_with_flag_var_and_alias(self):
        from boundary.signals import admin_bypass_activated

        received = []

        def _receiver(sender, **kwargs):
            received.append(kwargs)

        admin_bypass_activated.connect(_receiver, weak=False)
        try:
            with admin_bypass():
                pass
        finally:
            admin_bypass_activated.disconnect(_receiver)

        assert len(received) == 1
        assert received[0]["flag_var"] == "app.boundary_admin"
        assert received[0]["using"] == "default"

    def test_signal_honours_custom_flag_var_setting(self, settings):
        from boundary.signals import admin_bypass_activated

        settings.BOUNDARY_ADMIN_FLAG_VAR = "myapp.is_admin"
        received = []

        def _receiver(sender, **kwargs):
            received.append(kwargs)

        admin_bypass_activated.connect(_receiver, weak=False)
        try:
            with admin_bypass():
                pass
        finally:
            admin_bypass_activated.disconnect(_receiver)

        assert received[0]["flag_var"] == "myapp.is_admin"

    def test_signal_does_not_fire_when_flag_never_activates(self, settings):
        """Negative control: if _ensure_atomic degrades to a no-op and the
        read-back raises AdminBypassNotActiveError, the signal (an audit
        trail of successful activation) must not have fired for a bypass
        that was never actually active."""
        from boundary.signals import admin_bypass_activated

        settings.BOUNDARY_WRAP_ATOMIC = False
        received = []

        def _receiver(sender, **kwargs):
            received.append(kwargs)

        admin_bypass_activated.connect(_receiver, weak=False)
        try:
            with pytest.raises(AdminBypassNotActiveError), admin_bypass():
                pass
        finally:
            admin_bypass_activated.disconnect(_receiver)

        assert received == []


class TestContextLayerIsQuietOnNonPostgreSQL:
    """icvoss/django-boundary#85: the context layer's own writes are gated
    on the alias's vendor, not just on BOUNDARY_SET_DB_SESSION_VAR.

    BR-ENV-002 makes SQLite a supported backend for the context layer and
    requires the missing RLS layer to be quiet there: "no error, no warning
    and no exception attributable to the missing RLS layer". The only writes
    this layer makes are ``set_config()`` calls (BR-CTX-002), and
    ``set_config`` is a PostgreSQL function, so on any other backend they
    must not be issued at all. Before the fix, ``TenantContext.set()`` raised
    ``OperationalError: no such function: set_config`` on the first request
    that resolved a tenant, which put the ORM filtering layer BR-ENV-002
    promises on SQLite out of reach behind it.

    Unmarked deliberately, so these run on the SQLite leg too (where the
    ``default`` alias is also SQLite and the assertions hold for the same
    reason). They assert against ``eu-west``, which ``tests/settings.py``
    configures as SQLite on BOTH legs, so the non-PostgreSQL alias under
    test is a real one on either. That doubles as this rule's per-alias
    coverage: on the PostgreSQL leg the same process holds a PostgreSQL
    ``default`` and a SQLite ``eu-west``, which is BR-ENV-005's mixed-vendor
    deployment shape, and the gate has to answer differently per alias
    rather than once per process.
    """

    SQLITE_ALIAS = "eu-west"

    @pytest.mark.django_db(transaction=True, databases=["default", "eu-west"])
    def test_using_issues_no_set_config_on_a_sqlite_alias(self, tenant_a, settings):
        """Entering and leaving TenantContext.using() on a SQLite alias
        raises nothing and issues no set_config()."""
        from django.db import connections
        from django.test.utils import CaptureQueriesContext

        settings.DEBUG = True
        sqlite_connection = connections[self.SQLITE_ALIAS]
        sqlite_connection.ensure_connection()

        with (
            CaptureQueriesContext(sqlite_connection) as captured,
            TenantContext.using(tenant_a, using=self.SQLITE_ALIAS) as active,
        ):
            assert active == tenant_a
            assert TenantContext.get() == tenant_a

        assert [q["sql"] for q in captured.captured_queries if "set_config" in (q["sql"] or "")] == []

    @pytest.mark.django_db(transaction=True, databases=["default", "eu-west"])
    def test_set_and_clear_issue_no_set_config_on_a_sqlite_alias(self, tenant_a, settings):
        """The set()/clear() pair underneath using() is gated on both sides.

        A gate on the set side alone would move the failure from context
        entry to context exit rather than removing it.
        """
        from django.db import connections
        from django.test.utils import CaptureQueriesContext

        settings.DEBUG = True
        sqlite_connection = connections[self.SQLITE_ALIAS]
        sqlite_connection.ensure_connection()

        with CaptureQueriesContext(sqlite_connection) as captured:
            token = TenantContext.set(tenant_a, using=self.SQLITE_ALIAS)
            TenantContext.clear(token, using=self.SQLITE_ALIAS)

        assert [q["sql"] for q in captured.captured_queries if "set_config" in (q["sql"] or "")] == []

    @pytest.mark.django_db(transaction=True, databases=["default", "eu-west"])
    def test_admin_bypass_yields_and_raises_nothing_on_a_sqlite_alias(self, settings):
        """admin_bypass() on a SQLite alias yields, sets no flag and does not
        perform the read-back assertion.

        The read-back is the part that would otherwise turn this into a
        refusal: ``current_setting()`` does not exist on SQLite either, so
        without the vendor gate the block raises before the caller's body
        runs, and the documented cross-tenant idiom (``.unscoped`` inside an
        ``admin_bypass()`` block) is unusable on a supported backend.
        """
        from django.db import connections
        from django.test.utils import CaptureQueriesContext

        settings.DEBUG = True
        sqlite_connection = connections[self.SQLITE_ALIAS]
        sqlite_connection.ensure_connection()

        entered = False
        with CaptureQueriesContext(sqlite_connection) as captured, admin_bypass(using=self.SQLITE_ALIAS):
            entered = True

        assert entered
        assert [q["sql"] for q in captured.captured_queries if "set_config" in (q["sql"] or "")] == []
        assert [q["sql"] for q in captured.captured_queries if "current_setting" in (q["sql"] or "")] == []

    @pytest.mark.django_db(transaction=True, databases=["default", "eu-west"])
    def test_unscoped_reaches_other_tenants_rows_inside_the_bypass(self, tenant_a, tenant_b):
        """The reason admin_bypass() yields rather than refusing: what the
        block is FOR still works on a backend with no RLS.

        Cross-tenant access is reached through ``.unscoped`` at the ORM
        layer, which is backend-independent. This asserts the idiom the
        how-to documents completes on SQLite rather than merely that the
        context manager does not raise.
        """
        from boundary_testapp.models import Booking

        Booking.unscoped.create(tenant=tenant_a, court=1)
        Booking.unscoped.create(tenant=tenant_b, court=2)

        with TenantContext.using(tenant_a):
            assert Booking.objects.count() == 1
            with admin_bypass(using=self.SQLITE_ALIAS):
                assert Booking.unscoped.count() == 2

    @pytest.mark.rls
    @pytest.mark.django_db(transaction=True, databases=["default", "eu-west"])
    def test_gate_is_per_alias_not_per_process(self, tenant_a, settings):
        """Positive control for the per-alias claim, paired with the
        negative above.

        Needs a PostgreSQL ``default`` to contrast against the SQLite
        ``eu-west``: on the SQLite leg ``default`` is SQLite too, so there is
        no PostgreSQL alias in the process for the gate to answer differently
        about. On the PostgreSQL leg this is the whole point of gating on
        ``using``: the same process, the same call, one alias writing the
        session variable and the other not.

        Marked ``rls`` rather than skipping at runtime. BR-ENV-006 makes the
        distinction load-bearing: a deselected test is a leg declining a test
        that cannot apply, where a skipped one is indistinguishable from a leg
        whose database or role never materialised, which is the failure the
        zero-skipped rule exists to catch. The SQLite CI leg greps its pytest
        summary for " skipped" and fails on a hit, so the previous
        ``pytest.skip`` would have failed that leg.
        """
        from django.db import connections
        from django.test.utils import CaptureQueriesContext

        settings.DEBUG = True
        pg_connection = connections["default"]
        pg_connection.ensure_connection()

        with CaptureQueriesContext(pg_connection) as captured, TenantContext.using(tenant_a, using="default"):
            pass

        assert [q["sql"] for q in captured.captured_queries if "set_config" in (q["sql"] or "")] != []


@pytest.mark.rls
@pytest.mark.django_db(transaction=True)
class TestContextLayerStillWritesOnPostgreSQL:
    """The vendor gate must not have turned the PostgreSQL path off.

    A gate that returned early everywhere would pass every assertion in
    ``TestContextLayerIsQuietOnNonPostgreSQL`` while silently removing the
    session variable RLS reads, which is an isolation failure rather than a
    quiet backend. These pin the direction: on PostgreSQL the variable is
    still set, still readable, and still cleared.
    """

    def test_session_variable_is_set_and_cleared_on_postgresql(self, tenant_a):
        from boundary.conf import boundary_settings

        with TenantContext.using(tenant_a), connection.cursor() as cursor:
            cursor.execute("SELECT current_setting(%s, true)", [boundary_settings.DB_SESSION_VAR])
            assert cursor.fetchone()[0] == str(tenant_a.pk)

        with connection.cursor() as cursor:
            cursor.execute("SELECT current_setting(%s, true)", [boundary_settings.DB_SESSION_VAR])
            assert cursor.fetchone()[0] in ("", None)

    def test_admin_bypass_still_sets_the_flag_on_postgresql(self):
        from boundary.conf import boundary_settings

        with admin_bypass(), connection.cursor() as cursor:
            cursor.execute("SELECT current_setting(%s, true)", [boundary_settings.ADMIN_FLAG_VAR])
            assert cursor.fetchone()[0] == "true"
