"""Regression test for issue #40: the tenant DB session variable under
ATOMIC_REQUESTS.

Before the fix, ``TenantMiddleware.__call__`` treated
``ATOMIC_REQUESTS = True`` on the default database as a reason to skip its
own ``transaction.atomic()`` wrap, on the assumption that Django's
ATOMIC_REQUESTS transaction would already cover ``TenantContext.set()``.
That assumption was false: Django's ATOMIC_REQUESTS wraps the *view*
(``BaseHandler.make_view_atomic``), not the middleware chain, so no
transaction was open yet at the point the middleware called
``TenantContext.set()``. ``_set_db_session`` issues
``SELECT set_config(var, value, true)`` -- the ``true`` makes it
TRANSACTION-LOCAL -- so the setting was silently discarded before the
view's own transaction ever opened, leaving the PostgreSQL session
variable empty while ``TenantContext.get()`` still reported the tenant
correctly. Under ``FORCE ROW LEVEL SECURITY`` this fails CLOSED (the
policy evaluates against NULL and returns no rows), not as a data leak,
but it silently disabled the RLS defence-in-depth layer for every request
served under ``ATOMIC_REQUESTS = True``.

The fix makes ``TenantMiddleware`` always ensure its own atomic block via
``context._ensure_atomic()``, which checks ``connection.in_atomic_block``
directly rather than inferring it from the ``ATOMIC_REQUESTS`` setting.
That check correctly reads ``False`` at middleware time regardless of
``ATOMIC_REQUESTS``, because the view has not started yet, so the
middleware's own ``atomic()`` opens exactly when it is needed and the
later view-level ATOMIC_REQUESTS wrap nests as a savepoint underneath it.

This module drives a real view through the full middleware chain via
Django's test ``Client``, with the view itself reading
``current_setting('app.current_tenant_id', true)`` directly against the
same request-scoped connection, so the response reports the actual
runtime value rather than anything derived from ``TenantContext.get()``.

Uses ``@pytest.mark.django_db(transaction=True)`` throughout: the default
``django_db`` fixture wraps each test in one outer transaction and rolls
back at the end, which would make ATOMIC_REQUESTS' view-level
``transaction.atomic()`` behave like a savepoint inside an already-open
transaction rather than a real transaction boundary, and would make it
impossible to construct a genuinely cold connection. ``transaction=True``
runs against the real database with autocommit-by-default between
statements, matching how requests behave outside the test harness.
"""

import pytest
from django.test import Client


@pytest.fixture
def client():
    return Client()


def _configure(settings, *, atomic_requests):
    """Point MIDDLEWARE/BOUNDARY_RESOLVERS at the probe view and flip
    ATOMIC_REQUESTS on the default connection's settings_dict.

    Django's BaseHandler reads ``connection.settings_dict["ATOMIC_REQUESTS"]``
    fresh via ``get_response`` -> ``_get_response`` -> ``make_view_atomic``
    on every request (the wrapping decision is not cached at import time),
    so mutating settings_dict directly here, rather than
    ``settings.DATABASES``, is sufficient and takes effect on the next
    request with no extra reset needed.
    """
    settings.MIDDLEWARE = ["boundary.middleware.TenantMiddleware"]
    settings.BOUNDARY_RESOLVERS = ["boundary.resolvers.HeaderResolver"]
    from django.db import connections

    connections["default"].settings_dict["ATOMIC_REQUESTS"] = atomic_requests


@pytest.mark.django_db(transaction=True)
class TestAtomicRequestsSessionVariable:
    """Issue #40: the DB session var must survive to the view under
    ATOMIC_REQUESTS, exactly as it does when boundary's own atomic() wrap
    is the only one in play.
    """

    def test_control_atomic_requests_false_reports_healthy_tenant(self, client, tenant_a, settings):
        """CONTROL: ATOMIC_REQUESTS=False. This is the arm that proves the
        harness itself can report a healthy value, so a failure in the
        ATOMIC_REQUESTS=True arms below is attributable to the fix and not
        to a broken probe.
        """
        _configure(settings, atomic_requests=False)

        response = client.get(
            "/report-session-var/",
            headers={"X-Tenant-Id": str(tenant_a.pk)},
        )

        assert response.status_code == 200
        import json

        body = json.loads(response.content)
        assert body["context_tenant"] == str(tenant_a.pk)
        assert body["session_var"] == str(tenant_a.pk), (
            f"Control arm failed to report a healthy session variable: {body!r}. "
            "The test harness cannot distinguish a real regression from a broken probe."
        )

    def test_atomic_requests_true_warm_connection(self, client, tenant_a, settings, django_db_blocker):
        """WARM connection: the default connection is already open (a prior
        query ran on it in this test) before the request is dispatched.

        Regression guard: before the fix, this arm reported an EMPTY
        session variable even though TenantContext.get() (context_tenant)
        reported the tenant correctly, because the middleware relied on
        Django's ATOMIC_REQUESTS transaction, which had not opened yet at
        the point set_config() ran. This must now report the tenant pk.
        """
        _configure(settings, atomic_requests=True)

        # Warm the connection: force it open with a real query before the
        # request is dispatched, so connection.connection is not None when
        # TenantMiddleware runs.
        with django_db_blocker.unblock():
            from django.db import connection

            with connection.cursor() as cursor:
                cursor.execute("SELECT 1")
            assert connection.connection is not None, "Failed to warm the connection"

        response = client.get(
            "/report-session-var/",
            headers={"X-Tenant-Id": str(tenant_a.pk)},
        )

        assert response.status_code == 200
        import json

        body = json.loads(response.content)
        print(f"\n[WARM] ATOMIC_REQUESTS=True response body: {body!r}")

        assert body["in_atomic_block"] is True
        assert body["context_tenant"] == str(tenant_a.pk)
        assert body["session_var"] == str(tenant_a.pk), (
            f"Expected a healthy session variable but got {body['session_var']!r}. "
            "This is issue #40: TenantMiddleware is not keeping the session "
            "variable in scope for the view under ATOMIC_REQUESTS=True."
        )

    def test_atomic_requests_true_cold_connection(self, client, tenant_a, settings):
        """COLD connection: close the default DB connection immediately
        before dispatching the request, so it is not yet open when
        TenantMiddleware runs.

        Regression guard: same as the warm arm above, this must now report
        the tenant pk rather than an empty string.
        """
        _configure(settings, atomic_requests=True)

        from django.db import connection

        connection.close()
        assert connection.connection is None, "Failed to force a cold connection"

        response = client.get(
            "/report-session-var/",
            headers={"X-Tenant-Id": str(tenant_a.pk)},
        )

        assert response.status_code == 200
        import json

        body = json.loads(response.content)
        print(f"\n[COLD] ATOMIC_REQUESTS=True response body: {body!r}")

        assert body["in_atomic_block"] is True
        assert body["context_tenant"] == str(tenant_a.pk)
        assert body["session_var"] == str(tenant_a.pk), (
            f"Expected a healthy session variable but got {body['session_var']!r}. "
            "This is issue #40: TenantMiddleware is not keeping the session "
            "variable in scope for the view under ATOMIC_REQUESTS=True."
        )
