"""Test utilities for consuming projects and boundary's own test suite.

Provides set_tenant(), TenantTestMixin, tenant_factory(), and the RLS
test-role helpers (provision_rls_test_role(), assert_rls_enforced(),
and the rls_enforced pytest fixture) for convenient multi-tenant testing.
"""

import uuid
from contextlib import contextmanager

from boundary.conf import get_tenant_model
from boundary.context import TenantContext
from boundary.exceptions import RLSNotEnforcedError


@contextmanager
def set_tenant(tenant):
    """Context manager for setting tenant in tests.

    Usage::

        with set_tenant(tenant_a):
            Booking.objects.create(court=1)
    """
    with TenantContext.using(tenant):
        yield tenant


def tenant_factory(**kwargs):
    """Create a tenant with sane defaults for tests.

    Generates a unique slug to avoid collisions in parallel tests.
    All kwargs are passed to the tenant model's create() method.
    """
    TenantModel = get_tenant_model()

    defaults = {
        "name": f"Test Tenant {uuid.uuid4().hex[:6]}",
        "slug": f"test-{uuid.uuid4().hex[:8]}",
    }
    defaults.update(kwargs)

    return TenantModel.objects.create(**defaults)


def call_view(
    view_cls,
    *,
    tenant,
    method: str = "get",
    path: str = "/",
    view_kwargs: dict | None = None,
    **request_kwargs,
):
    """Call a class-based view directly under an active tenant context.

    ``RequestFactory`` bypasses middleware, so a CBV called directly in a test
    has no tenant in context and any tenant-scoped query raises
    ``TenantNotSetError``. This builds the request and wraps the call in
    ``TenantContext.using(tenant)`` in one line.

    Usage::

        response = call_view(BookingListView, tenant=tenant_a)
        response = call_view(
            BookingDetailView,
            tenant=tenant_a,
            view_kwargs={"pk": booking.pk},
        )
        response = call_view(
            BookingCreateView, tenant=tenant_a, method="post",
            data={"court": 1},
        )

    Args:
        view_cls: The class-based view (``.as_view()`` is called for you).
        tenant: Tenant instance to activate for the duration of the call.
        method: HTTP method (``"get"``, ``"post"``, ...). Default ``"get"``.
        path: Request path. Default ``"/"``.
        view_kwargs: URL kwargs passed to the view (e.g. ``{"pk": 1}``).
        **request_kwargs: Forwarded to the RequestFactory method
            (e.g. ``data=...``, ``HTTP_HOST=...``).

    Returns:
        The view's response.
    """
    from django.test import RequestFactory

    request = getattr(RequestFactory(), method)(path, **request_kwargs)
    with TenantContext.using(tenant):
        return view_cls.as_view()(request, **(view_kwargs or {}))


class TenantTestMixin:
    """Mixin for TestCase classes. Creates self.tenant before each test.

    Usage::

        class BookingTests(TenantTestMixin, TestCase):
            def test_booking_creation(self):
                booking = Booking.objects.create(court=1)
                assert booking.tenant == self.tenant
    """

    _boundary_context = None

    def get_tenant_factory_kwargs(self):
        """Override to customise the created tenant."""
        return {}

    def setUp(self):
        super().setUp()
        self.tenant = tenant_factory(**self.get_tenant_factory_kwargs())
        self._boundary_context = TenantContext.using(self.tenant)
        self._boundary_context.__enter__()

    def tearDown(self):
        if self._boundary_context is not None:
            self._boundary_context.__exit__(None, None, None)
            self._boundary_context = None
        super().tearDown()


def provision_rls_test_role(
    *,
    bootstrap_connection_params: dict,
    role: str = "icv_app",
    password: str = "icv_dev",
    database: str | None = None,
) -> dict:
    """Provision a plain, non-BYPASSRLS role for RLS-enforcement tests.

    Packages the shell CI has always run by hand (``.github/workflows/ci.yml``,
    "Provision the non-superuser icv_app role"): ``CREATE ROLE ... LOGIN
    PASSWORD ... NOSUPERUSER NOBYPASSRLS``, then ``GRANT CONNECT ON DATABASE``
    and ``GRANT USAGE ON SCHEMA public``. Idempotent: a role that already
    exists is left as-is rather than erroring, since a role created by a
    prior run (or CI's own provisioning step) is the expected steady state,
    not a fault.

    A no-op, returning an empty dict, when ``connection.vendor`` is not
    ``"postgresql"``: BYPASSRLS and RLS itself are PostgreSQL-specific, so
    there is nothing to provision against sqlite or another backend, and
    this function must not error a suite running on a non-Postgres backend
    out of the gate.

    Deliberately does NOT touch ``django.conf.settings.DATABASES`` or
    reconnect Django's own connection. pytest-django creates the test
    database during ``django_db_setup``, before any fixture in a test
    module has run, so a fixture cannot repoint ``DATABASES["default"]``
    early enough for pytest-django to use the new role when creating the
    test database; doing it partway through the run was the exact risk the
    issue #55 triage flagged. Instead this function provisions the role
    and returns its connection parameters (host, port, dbname, user,
    password) so the caller can build its own connection (as
    ``tests/conftest.py``'s ``app_conn`` fixture already does) or, if it
    wants pytest-django itself to connect as this role, set
    ``DATABASES["default"]`` to these values in ``settings.py`` BEFORE
    pytest starts, not from within a fixture.

    Args:
        bootstrap_connection_params: connection kwargs (``host``, ``port``,
            ``dbname``, ``user``, ``password``) for a role that can create
            roles and grant privileges, e.g. the postgres image's bootstrap
            superuser. Passed straight to ``psycopg.connect()``.
        role: name of the role to provision. Default ``"icv_app"``, matching
            the role CI already provisions and ``tests/conftest.py``'s
            ``app_conn`` fixture already connects as.
        password: password to set on the role. Default ``"icv_dev"``,
            matching the CI shell and ``app_conn``.
        database: database to grant CONNECT/USAGE on. Defaults to
            ``bootstrap_connection_params["dbname"]``.

    Returns:
        A dict of connection parameters (``host``, ``port``, ``dbname``,
        ``user``, ``password``) for the provisioned role, or ``{}`` on a
        non-PostgreSQL backend.
    """
    from django.db import connection as django_connection

    if django_connection.vendor != "postgresql":
        return {}

    import psycopg
    from psycopg import sql

    dbname = database or bootstrap_connection_params["dbname"]

    with psycopg.connect(**bootstrap_connection_params, autocommit=True) as conn, conn.cursor() as cursor:
        cursor.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", [role])
        role_exists = cursor.fetchone() is not None

        if not role_exists:
            cursor.execute(
                sql.SQL("CREATE ROLE {role} LOGIN PASSWORD {password} NOSUPERUSER NOBYPASSRLS").format(
                    role=sql.Identifier(role),
                    password=sql.Literal(password),
                )
            )

        cursor.execute(
            sql.SQL("GRANT CONNECT ON DATABASE {db} TO {role}").format(
                db=sql.Identifier(dbname),
                role=sql.Identifier(role),
            )
        )
        cursor.execute(sql.SQL("GRANT USAGE ON SCHEMA public TO {role}").format(role=sql.Identifier(role)))

    return {
        "host": bootstrap_connection_params.get("host", "localhost"),
        "port": bootstrap_connection_params.get("port", 5432),
        "dbname": dbname,
        "user": role,
        "password": password,
    }


def assert_rls_enforced(connection_params: dict, *, alias: str = "default") -> None:
    """Fail-closed assertion that RLS is actually enforced for *connection_params*.

    The load-bearing half of the RLS test-role helper (issue #55): a role
    that only creates a plain role and never checks anything is the exact
    no-op the triage warned about, because a suite can still run entirely
    as a superuser (wrong credentials, a stale settings override, a role
    that was granted BYPASSRLS after provisioning) and every RLS-isolation
    test would keep passing having tested nothing.

    Connects with *connection_params* (as returned by
    ``provision_rls_test_role()``, or built by the caller directly) and
    checks, in order:

    1. ``SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname =
       current_user``, both must be false. Reuses the exact query shape
       ``boundary.W003`` (``checks.py``'s ``_check_rls_bypassable``) already
       probes, since PostgreSQL exempts a superuser or BYPASSRLS role from
       every policy regardless of what the table declares.
    2. At least one tenant-scoped table has both ``relrowsecurity`` and
       ``relforcerowsecurity`` set, using the same
       ``to_regclass()``-qualified query ``boundary.E006``
       (``_check_rls_enabled``) already uses, and literally the same table
       selection, ``checks._rls_probe_targets()``, rather than a hardcoded
       table name a consumer's schema might not have.

       That selection covers registered column-bearing models AND every
       table ``BOUNDARY_TENANT_APPS`` expects to be adopted (BR-PRV-010).
       An adopted table is the case where this assertion matters most: it
       has no ORM layer to fall back on, so a suite running as a
       ``BYPASSRLS`` role, or against a database where the adoption DDL
       never applied, would exercise no isolation at all and still pass
       every test. A consumer whose only tenant-scoped tables are adopted
       ones (no column-bearing model migrated at all) previously had this
       assertion find nothing to check and return silently, which is the
       exact vacuous pass it exists to prevent.

    Raises ``RLSNotEnforcedError``, naming which condition failed and the
    value observed, rather than returning a bool: a caller that only checks
    a falsy return can accidentally ignore it, where a raised exception
    cannot be silently dropped. The ``rls_enforced`` fixture below wraps
    this in ``pytest.exit`` so a real consumer's run stops immediately
    rather than continuing to collect and report a wall of now-meaningless
    downstream failures.

    A backend other than PostgreSQL, or a Django deployment with no
    tenant-scoped table yet migrated (column-bearing or adopted), has
    nothing for this assertion to check: it returns without raising rather
    than treating "nothing to enforce" as a failure.

    **The non-PostgreSQL return happens before any connection is opened, and
    before psycopg is imported** (BR-PRV-010). Until 1.0 the early return was
    promised here and absent from the body: the first action was
    ``psycopg.connect(**connection_params)``, so a consumer calling this on a
    SQLite deployment got a connection error, or an ``ImportError`` where
    psycopg was not installed at all, rather than the documented quiet return
    (icvoss/django-boundary#77). That was BR-ENV-002's quiet-on-SQLite
    guarantee failing at the one helper whose whole purpose is to prove RLS is
    enforced, so the guard must precede the import and not merely the connect.

    The vendor is read from the Django alias rather than from
    *connection_params*, because *connection_params* is a raw psycopg mapping
    with no vendor field and opening it is the thing being avoided. Which
    alias: ``"default"``, matching every other vendor gate in the package
    (``checks.py`` reads ``django.db.connection``), unless the caller passes
    one. A caller who has configured a PostgreSQL alias other than
    ``default`` and asserts against it passes *alias* explicitly.

    Args:
        connection_params: connection kwargs passed to ``psycopg.connect()``,
            as returned by :func:`provision_rls_test_role`.
        alias: the Django database alias whose vendor decides whether to
            proceed. Default ``"default"``. Keyword-only, and added
            compatibly: every existing call keeps working unchanged.
    """
    from django.db import connections

    # Before the psycopg import, not merely before the connect: on a
    # deployment with no psycopg installed at all, an import at function top
    # would raise ImportError and defeat the quiet return entirely.
    if connections[alias].vendor != "postgresql":
        return

    import psycopg
    from django.apps import apps

    from boundary.checks import _rls_probe_targets

    with psycopg.connect(**connection_params) as conn, conn.cursor() as cursor:
        cursor.execute("SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user")
        row = cursor.fetchone()
        if row is not None:
            rolsuper, rolbypassrls = row
            if rolsuper or rolbypassrls:
                raise RLSNotEnforcedError(
                    f"Role {connection_params.get('user')!r} connecting for RLS tests has "
                    f"rolsuper={rolsuper}, rolbypassrls={rolbypassrls}. PostgreSQL exempts "
                    "a superuser or BYPASSRLS role from every RLS policy, so isolation "
                    "tests run as this role would pass without testing anything. Connect "
                    "as a plain NOSUPERUSER NOBYPASSRLS role (see "
                    "boundary.testing.provision_rls_test_role())."
                )

        checked_any_table = False
        for _model, table, _adopted in _rls_probe_targets(apps):
            cursor.execute(
                "SELECT relrowsecurity, relforcerowsecurity FROM pg_class WHERE oid = to_regclass(%s)::oid",
                [table],
            )
            table_row = cursor.fetchone()
            if table_row is None:
                continue  # table not migrated yet on this connection

            checked_any_table = True
            rls_enabled, rls_forced = table_row
            if rls_enabled and rls_forced:
                return  # at least one tenant table is enforcing RLS

        if checked_any_table:
            raise RLSNotEnforcedError(
                "No registered tenant-scoped table has Row Level Security both "
                "enabled and forced (relrowsecurity and relforcerowsecurity), "
                "counting adopted tables as well as column-bearing models. "
                "RLS-isolation tests would pass without any policy actually "
                "restricting rows. Run the EnableRLS/CreateTenantPolicy migration "
                "operations, re-apply the AdoptTenantApp migration for an adopted "
                "app, or see boundary.E006 and boundary.E007."
            )
        # No tenant-scoped table exists yet on this connection (nothing migrated,
        # and nothing adopted): nothing to check, consistent with boundary.E006's
        # own pre-migrate skip.


def rls_enforced(connection_params: dict) -> None:
    """Session-fixture body: fail the pytest run closed if RLS is not enforced.

    Not decorated with ``@pytest.fixture`` itself, because the connection
    parameters (host, port, role, password) are project-specific and this
    module does not import ``pytest`` at module scope for consumers who
    never touch the testing surface. A consumer wires this as a
    session-scoped autouse fixture in their own conftest::

        import pytest
        from boundary.testing import provision_rls_test_role, rls_enforced

        @pytest.fixture(scope="session", autouse=True)
        def _rls_enforced(django_db_setup, django_db_blocker):
            with django_db_blocker.unblock():
                params = provision_rls_test_role(
                    bootstrap_connection_params={
                        "host": "localhost", "port": 5432,
                        "dbname": "myproject_test",
                        "user": "postgres", "password": "postgres",
                    },
                )
                if params:  # empty on a non-PostgreSQL backend
                    rls_enforced(params)

    Calls ``assert_rls_enforced()`` and, on ``RLSNotEnforcedError``, calls
    ``pytest.exit()`` (rather than letting the exception propagate as a
    normal fixture error) so the entire run stops immediately with the
    failure message on stderr, before pytest collects or runs a single
    test. A single fixture-setup error would otherwise report as one
    failure among possibly thousands of collected tests, each of which is
    about to pass vacuously; ``pytest.exit`` makes that unmissable instead
    of scrollable.
    """
    import pytest

    try:
        assert_rls_enforced(connection_params)
    except RLSNotEnforcedError as exc:
        pytest.exit(f"boundary: RLS is not enforced for this test run: {exc}", returncode=1)
