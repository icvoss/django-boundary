"""Tests for boundary.testing, test utilities."""

import pytest
from django.test import TestCase

from boundary.context import TenantContext
from boundary.exceptions import RLSNotEnforcedError, TenantNotSetError
from boundary.testing import TenantTestMixin, call_view, set_tenant, tenant_factory


@pytest.mark.django_db
class TestSetTenant:
    """AC-TEST-001: set_tenant context manager."""

    def test_sets_context(self, tenant_a):
        with set_tenant(tenant_a):
            assert TenantContext.get() == tenant_a

    def test_clears_on_exit(self, tenant_a):
        with set_tenant(tenant_a):
            pass
        assert TenantContext.get() is None


@pytest.mark.django_db
class TestTenantFactory:
    """AC-TEST-004: tenant_factory defaults."""

    def test_creates_with_defaults(self):
        tenant = tenant_factory()
        assert tenant.pk is not None
        assert tenant.slug.startswith("test-")
        assert tenant.name.startswith("Test Tenant")

    def test_accepts_kwargs(self):
        tenant = tenant_factory(name="Custom", slug="custom")
        assert tenant.name == "Custom"
        assert tenant.slug == "custom"

    def test_unique_slugs(self):
        t1 = tenant_factory()
        t2 = tenant_factory()
        assert t1.slug != t2.slug


@pytest.mark.django_db
class TestTenantTestMixin(TenantTestMixin, TestCase):
    """AC-TEST-002/003: TenantTestMixin setup and cleanup."""

    def test_tenant_available(self):
        """AC-TEST-002: self.tenant is pre-created with context active."""
        assert self.tenant is not None
        assert TenantContext.get() == self.tenant

    def test_can_create_scoped_objects(self):
        from boundary_testapp.models import Booking

        booking = Booking.objects.create(court=1)
        assert booking.tenant == self.tenant


@pytest.mark.django_db
class TestCallView:
    """call_view runs a CBV under an active tenant context."""

    def _view_cls(self):
        from boundary_testapp.models import Booking
        from django.http import JsonResponse
        from django.views import View

        class BookingCountView(View):
            def get(self, request, *args, **kwargs):
                return JsonResponse({"count": Booking.objects.count()})

        return BookingCountView

    def test_view_sees_active_tenant_rows(self, tenant_a, tenant_b):
        import json

        from boundary_testapp.models import Booking

        with set_tenant(tenant_a):
            Booking.objects.create(court=1)
            Booking.objects.create(court=2)
        with set_tenant(tenant_b):
            Booking.objects.create(court=3)

        response = call_view(self._view_cls(), tenant=tenant_a)
        assert json.loads(response.content)["count"] == 2

        response = call_view(self._view_cls(), tenant=tenant_b)
        assert json.loads(response.content)["count"] == 1

    def test_without_helper_raises_strict(self, tenant_a, settings):
        """Proves the helper is what fixes the missing-context problem: the
        same view called via a bare RequestFactory raises under strict mode."""
        from django.test import RequestFactory

        settings.BOUNDARY_STRICT_MODE = True
        with set_tenant(tenant_a):
            from boundary_testapp.models import Booking

            Booking.objects.create(court=1)

        request = RequestFactory().get("/")
        with pytest.raises(TenantNotSetError):
            self._view_cls().as_view()(request)


def _app_role_connection_params():
    """Connection params for the icv_app role CI provisions (ci.yml:78).

    Mirrors conftest.py's app_conn fixture: skips the test rather than
    failing it when the role is not available, since a bare local `pytest`
    run without the CI provisioning step has nothing to connect as.
    """
    from django.db import connection

    db = connection.settings_dict
    return {
        "host": db.get("HOST", "localhost"),
        "port": db.get("PORT", 5432),
        "dbname": db["NAME"],
        "user": "icv_app",
        "password": "icv_dev",
    }


def _require_app_role():
    import psycopg

    params = _app_role_connection_params()
    try:
        psycopg.connect(**params).close()
    except Exception as e:
        pytest.skip(f"icv_app role not available: {e}")
    return params


def _default_connection_params():
    """Connection params for the suite's own default (bootstrap/superuser) role."""
    from django.db import connection

    db = connection.settings_dict
    return {
        "host": db.get("HOST", "localhost"),
        "port": db.get("PORT", 5432),
        "dbname": db["NAME"],
        "user": db["USER"],
        "password": db["PASSWORD"],
    }


@pytest.mark.django_db
class TestProvisionRLSTestRole:
    """Issue #55: provision the plain NOSUPERUSER NOBYPASSRLS role."""

    def test_skips_non_postgresql_backends(self, monkeypatch):
        from django.db import connection

        from boundary.testing import provision_rls_test_role

        monkeypatch.setattr(connection, "vendor", "sqlite")
        assert provision_rls_test_role(bootstrap_connection_params={"dbname": "irrelevant"}) == {}

    @pytest.mark.rls
    def test_idempotent_against_an_existing_role(self, settings):
        """The role CI provisions in ci.yml already exists in this suite's
        environment; provisioning it again must not error, and must return
        connection parameters for it."""
        from boundary.testing import provision_rls_test_role

        params = _default_connection_params()
        try:
            result = provision_rls_test_role(bootstrap_connection_params=params)
        except Exception as e:
            pytest.skip(f"bootstrap role cannot provision (e.g. lacks CREATEROLE): {e}")

        if not result:
            pytest.skip("non-PostgreSQL backend")

        assert result["user"] == "icv_app"
        assert result["dbname"] == params["dbname"]

        # Calling it again is the idempotency proof: no "role already exists" error.
        result_again = provision_rls_test_role(bootstrap_connection_params=params)
        assert result_again == result


@pytest.mark.rls
@pytest.mark.django_db
class TestAssertRLSEnforced:
    """Issue #55: the fail-closed half. Proven both ways: it passes for the
    plain icv_app role against an RLS-forced table, and it FAILS (with a
    message naming the cause) for a bypassing role and for an unprotected
    table. The failing half is the load-bearing proof: a helper that only
    ever passes proves nothing (issue #55 triage).
    """

    @pytest.mark.django_db(transaction=True)
    def test_passes_for_the_non_bypassing_role_against_a_forced_table(self, settings):
        """Happy path: proven against the real icv_app role and a table
        with RLS actually enabled and forced, not merely asserted.

        Uses boundary_testapp_booking, not boundary_testapp_tenant: the
        Tenant model is the tenant ROOT (AbstractTenant), not a
        tenant-scoped child, so is_tenant_model(Tenant) is False and
        assert_rls_enforced() never looks at its table at all. Booking is
        a TenantModel with a real tenant FK column, matching what
        is_tenant_model()/has_tenant_column() actually select.

        transaction=True (overriding the class-level plain django_db) is
        required, not cosmetic: assert_rls_enforced() opens its own
        psycopg connection, separate from Django's test connection. Under
        the class's default django_db (non-transactional, wraps the test
        in an outer atomic block that rolls back at teardown), the ALTER
        TABLE below is never committed, so the second connection can never
        see it and the assertion fails closed for the wrong reason.
        """
        from django.db import connection

        from boundary.testing import assert_rls_enforced

        params = _require_app_role()
        settings.BOUNDARY_TENANT_MODEL = "boundary_testapp.Tenant"
        table = "boundary_testapp_booking"

        with connection.cursor() as cursor:
            cursor.execute(f'ALTER TABLE "{table}" ENABLE ROW LEVEL SECURITY')
            cursor.execute(f'ALTER TABLE "{table}" FORCE ROW LEVEL SECURITY')
        try:
            assert_rls_enforced(params)  # must not raise
        finally:
            with connection.cursor() as cursor:
                cursor.execute(f'ALTER TABLE "{table}" NO FORCE ROW LEVEL SECURITY')
                cursor.execute(f'ALTER TABLE "{table}" DISABLE ROW LEVEL SECURITY')

    def test_fails_closed_for_a_bypassing_role(self, settings):
        """Injected condition: connect as the suite's own default role,
        which is the bootstrap superuser/BYPASSRLS role in CI and a typical
        local Postgres image (the same fact TestW003RlsBypassableRole
        relies on in test_checks.py). assert_rls_enforced() must raise
        RLSNotEnforcedError naming rolsuper/rolbypassrls, not silently pass."""
        from django.db import connection

        from boundary.testing import assert_rls_enforced

        settings.BOUNDARY_TENANT_MODEL = "boundary_testapp.Tenant"
        params = _default_connection_params()

        with connection.cursor() as cursor:
            cursor.execute("SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user")
            rolsuper, rolbypassrls = cursor.fetchone()
        assert rolsuper or rolbypassrls, (
            "this proof requires the default test connection to be a "
            "superuser/BYPASSRLS role; if it is not, re-verify this test "
            "against the current environment"
        )

        with pytest.raises(RLSNotEnforcedError) as exc_info:
            assert_rls_enforced(params)

        message = str(exc_info.value)
        assert "rolsuper" in message
        assert "rolbypassrls" in message
        assert "exempts a superuser or BYPASSRLS role from every RLS policy" in message

    def test_fails_closed_for_a_table_with_no_rls(self, settings):
        """Injected condition: the non-bypassing icv_app role connects fine,
        but no tenant table has RLS enabled/forced (the suite's normal
        steady state outside the RLS-specific test modules).
        assert_rls_enforced() must raise RLSNotEnforcedError naming the
        missing enforcement, not silently pass because the role check alone
        was satisfied."""
        from django.db import connection

        from boundary.testing import assert_rls_enforced

        params = _require_app_role()
        settings.BOUNDARY_TENANT_MODEL = "boundary_testapp.Tenant"
        table = "boundary_testapp_tenant"

        # Confirm the precondition this test depends on: nothing pre-existing
        # has left RLS enabled on the table from another test.
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT relrowsecurity, relforcerowsecurity FROM pg_class WHERE oid = to_regclass(%s)::oid",
                [table],
            )
            rls_enabled, rls_forced = cursor.fetchone()
        assert not (rls_enabled and rls_forced), (
            "this proof requires the tenant table to NOT have RLS enabled "
            "and forced going in; another test left it enabled"
        )

        with pytest.raises(RLSNotEnforcedError) as exc_info:
            assert_rls_enforced(params)

        message = str(exc_info.value)
        assert "No registered tenant-scoped table has Row Level Security" in message


@pytest.mark.rls
@pytest.mark.django_db(transaction=True)
class TestAcTest005AssertRlsEnforcedCoversAnAdoptedTable:
    """AC-TEST-005 (BR-PRV-010): assert_rls_enforced covers an adopted table.

    An adopted table is the case where this assertion matters most: it has
    no ORM layer to fall back on, so a suite running as a BYPASSRLS role, or
    against a database where the adoption DDL never applied, would exercise
    no isolation at all and still pass every test.

    The criterion's "a database in which the only tenant-scoped table is an
    adopted thirdparty_widget, with no column-bearing tenant model migrated"
    is reproduced without tearing the schema down: BOUNDARY_TENANT_APPS is
    set to thirdparty (so the adopted table enters the selection) while
    every column-bearing model is confirmed to have no RLS enabled, which
    leaves the adopted table as the only thing that could satisfy the
    assertion. If it were skipped, the call would raise, so a pass here
    cannot come from anywhere else.

    transaction=True for the same reason the class above needs it:
    assert_rls_enforced opens its own psycopg connection, which cannot see
    an uncommitted DDL change made on Django's.
    """

    @pytest.fixture(autouse=True)
    def _adopted_settings(self, settings):
        settings.BOUNDARY_TENANT_MODEL = "boundary_testapp.Tenant"
        settings.BOUNDARY_TENANT_APPS = ["thirdparty"]
        settings.BOUNDARY_ADOPT_EXCLUDE = [
            "thirdparty.Seat",
            "thirdparty.SeatBooking",
            "thirdparty.Coupon",
        ]

    def _assert_no_column_bearing_table_enforces(self):
        """Confirm the precondition the criterion's "only" clause rests on.

        Every column-bearing model's table must NOT have RLS enabled and
        forced, or a pass below could come from one of them and would say
        nothing about the adopted table at all.
        """
        from django.apps import apps
        from django.db import connection

        from boundary.models import has_tenant_column, is_tenant_model

        with connection.cursor() as cursor:
            for model in apps.get_models():
                if not is_tenant_model(model) or model._meta.abstract or not has_tenant_column(model):
                    continue
                cursor.execute(
                    "SELECT relrowsecurity, relforcerowsecurity FROM pg_class WHERE oid = to_regclass(%s)::oid",
                    [model._meta.db_table],
                )
                row = cursor.fetchone()
                if row is None:
                    continue
                assert not (row[0] and row[1]), (
                    f"this proof requires no column-bearing table to be enforcing "
                    f"going in; {model._meta.db_table} is, so another test left it enabled"
                )

    def test_ac_test_005_an_adopted_table_satisfies_the_assertion(self):
        """When assert_rls_enforced(connection_params) is called with a
        plain NOSUPERUSER NOBYPASSRLS role, then it returns without raising,
        having confirmed the adopted table's enabled and forced RLS.

        The negative control is in the same test: with BOUNDARY_TENANT_APPS
        emptied, the adopted table leaves the selection, nothing is left to
        check, and the call returns for the entirely different reason that
        there was nothing to inspect. That distinction is what makes the
        positive half meaningful, so the enforcing state is asserted
        directly from pg_class too rather than inferred from a silent
        return.
        """
        from django.db import connection

        from boundary.testing import assert_rls_enforced

        params = _require_app_role()
        self._assert_no_column_bearing_table_enforces()

        # The adopted table really is enforcing going in: the test database
        # is built with boundary_consumer/0002 applied.
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT relrowsecurity, relforcerowsecurity FROM pg_class WHERE oid = to_regclass(%s)::oid",
                ["thirdparty_widget"],
            )
            rls_enabled, rls_forced = cursor.fetchone()
        assert rls_enabled and rls_forced, (
            "this proof requires the adopted table to be enforcing going in; another test left it un-forced"
        )

        assert_rls_enforced(params)  # must not raise

    def test_ac_test_005_an_un_forced_adopted_table_raises(self, settings):
        """When RLS is then un-forced on that table and the call is
        repeated, then RLSNotEnforcedError is raised naming the condition
        that failed.

        Every other adopted table is un-forced too, not just Widget: the
        assertion returns as soon as ONE inspected table is confirmed
        enforcing, so leaving thirdparty_tag or the through table enforcing
        would make it pass for a reason that has nothing to do with the
        damage under test.
        """
        from django.db import connection

        from boundary.testing import assert_rls_enforced

        params = _require_app_role()
        self._assert_no_column_bearing_table_enforces()

        tables = (
            "thirdparty_widget",
            "thirdparty_widget_tags",
            "thirdparty_tag",
            "thirdparty_gadget",
        )
        with connection.cursor() as cursor:
            for table in tables:
                cursor.execute(f'ALTER TABLE "{table}" NO FORCE ROW LEVEL SECURITY')
        try:
            with pytest.raises(RLSNotEnforcedError) as exc_info:
                assert_rls_enforced(params)

            message = str(exc_info.value)
            assert "Row Level Security both" in message
            assert "relrowsecurity and relforcerowsecurity" in message
            assert "adopted tables" in message, f"the message must name the adopted tables it counted; got {message}"
        finally:
            with connection.cursor() as cursor:
                for table in tables:
                    cursor.execute(f'ALTER TABLE "{table}" FORCE ROW LEVEL SECURITY')

    def test_ac_test_005_a_bypassing_role_still_raises_against_an_adopted_table(self):
        """And a BYPASSRLS role still raises, even with the adopted table
        correctly enforcing.

        The role check fires before any table is inspected, and must keep
        doing so once adopted tables are in the selection: a correctly
        adopted table is exactly the state that would otherwise make a
        bypassing suite look fully protected while PostgreSQL exempts it
        from every policy on that table.
        """
        from django.db import connection

        from boundary.testing import assert_rls_enforced

        params = _default_connection_params()

        with connection.cursor() as cursor:
            cursor.execute("SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user")
            rolsuper, rolbypassrls = cursor.fetchone()
        assert rolsuper or rolbypassrls, (
            "this proof requires the default test connection to be a "
            "superuser/BYPASSRLS role; if it is not, re-verify this test "
            "against the current environment"
        )

        with pytest.raises(RLSNotEnforcedError) as exc_info:
            assert_rls_enforced(params)

        message = str(exc_info.value)
        assert "rolsuper" in message
        assert "rolbypassrls" in message

    def test_ac_test_005_the_adopted_table_is_why_it_passes(self):
        """The discriminator: with BOUNDARY_TENANT_APPS emptied, the same
        database state has nothing for the assertion to inspect.

        Without this, the passing test above is consistent with the adopted
        table never having entered the selection at all, because "returns
        without raising" is also what the helper does when it finds nothing
        to check. Here the selection itself is asserted: thirdparty_widget
        is in it with the setting on, and absent from it with the setting
        off.
        """
        from django.apps import apps
        from django.test import override_settings

        from boundary.checks import _rls_probe_targets

        with override_settings(BOUNDARY_TENANT_APPS=["thirdparty"]):
            tables = {table for _model, table, _adopted in _rls_probe_targets(apps)}
        assert "thirdparty_widget" in tables

        with override_settings(BOUNDARY_TENANT_APPS=[]):
            tables = {table for _model, table, _adopted in _rls_probe_targets(apps)}
        assert "thirdparty_widget" not in tables
