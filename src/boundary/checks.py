"""Django system checks for boundary configuration.

Registered in AppConfig.ready() and run at startup and during test collection.
"""

from django.core.checks import Error, Tags, Warning, register
from django.db.utils import InterfaceError, OperationalError

# Exceptions that mean "the database connection itself is unavailable",
# distinct from a query against a reachable database failing for some
# other reason (permissions, a malformed statement, a lock timeout).
# OperationalError covers connection refused, auth failure at the socket,
# and DNS/host resolution failures; InterfaceError covers a connection
# that has already been closed. Both are raised by connection.cursor()
# before any SQL runs, which is exactly the pre-migrate /
# DB-not-provisioned-yet case this module has always meant to skip
# silently. A DatabaseError subclass raised BY the query itself
# (ProgrammingError for a permissions failure reading pg_class, DataError,
# etc.) is a different fact: the database is there and something is
# wrong, which boundary.W007 exists to surface rather than swallow.
_CONNECTION_UNAVAILABLE_ERRORS = (OperationalError, InterfaceError)


@register(Tags.models)
def check_boundary_configuration(app_configs, **kwargs):
    """Validate boundary settings at startup."""
    errors = []

    errors.extend(_check_tenant_model())
    errors.extend(_check_resolvers())
    errors.extend(_check_middleware())
    errors.extend(_check_strict_mode())
    errors.extend(_check_rls_enabled())
    errors.extend(_check_identity_double_resolve())
    errors.extend(_check_rls_bypassable())

    return errors


def _check_tenant_model():
    """E001: a tenant model must be set (BOUNDARY_TENANT_MODEL or its
    ICV_TENANT_MODEL fallback, ADR-025 T2) and refer to an installed model.
    """
    from django.apps import apps
    from django.conf import settings

    boundary_setting = getattr(settings, "BOUNDARY_TENANT_MODEL", None)
    icv_setting = getattr(settings, "ICV_TENANT_MODEL", None)
    model_string = boundary_setting or icv_setting
    if not model_string:
        return [
            Error(
                "Neither BOUNDARY_TENANT_MODEL nor ICV_TENANT_MODEL is set.",
                hint=(
                    "Add BOUNDARY_TENANT_MODEL = 'app_label.ModelName' to settings, or "
                    "ICV_TENANT_MODEL = 'app_label.ModelName' if another ecosystem package "
                    "(e.g. icv-identity) already sets it (ADR-025 T2)."
                ),
                id="boundary.E001",
            )
        ]

    source_setting = "BOUNDARY_TENANT_MODEL" if boundary_setting else "ICV_TENANT_MODEL"
    try:
        apps.get_model(model_string)
    except LookupError:
        return [
            Error(
                f"{source_setting} = '{model_string}' does not refer to an installed model.",
                hint="Check the app_label.ModelName format and ensure the app is in INSTALLED_APPS.",
                id="boundary.E001",
            )
        ]

    return []


def _check_resolvers():
    """E003: All configured resolver classes must be importable."""
    from django.conf import settings
    from django.utils.module_loading import import_string

    resolver_paths = getattr(
        settings,
        "BOUNDARY_RESOLVERS",
        ["boundary.resolvers.SubdomainResolver"],
    )

    errors = []
    for path in resolver_paths:
        try:
            import_string(path)
        except ImportError:
            errors.append(
                Error(
                    f"Resolver class '{path}' cannot be imported.",
                    hint="Check the dotted path in BOUNDARY_RESOLVERS.",
                    id="boundary.E003",
                )
            )
    return errors


def _check_middleware():
    """E004: TenantMiddleware must be in MIDDLEWARE."""
    from django.conf import settings

    middleware = getattr(settings, "MIDDLEWARE", [])
    if "boundary.middleware.TenantMiddleware" not in middleware:
        return [
            Error(
                "boundary.middleware.TenantMiddleware is not in MIDDLEWARE.",
                hint="Add 'boundary.middleware.TenantMiddleware' to MIDDLEWARE before SessionMiddleware.",
                id="boundary.E004",
            )
        ]
    return []


def _check_strict_mode():
    """W001: Warn if STRICT_MODE is disabled."""
    from django.conf import settings

    strict = getattr(settings, "BOUNDARY_STRICT_MODE", True)
    if not strict:
        return [
            Warning(
                "BOUNDARY_STRICT_MODE is False. Queries without an active tenant context will not raise an error.",
                hint="Set BOUNDARY_STRICT_MODE = True for development safety.",
                id="boundary.W001",
            )
        ]
    return []


def _check_identity_double_resolve():
    """W002: warn when boundary and icv-identity both resolve the tenant.

    Per ADR-025 T1, when icv-identity is installed it owns request-to-tenant
    resolution: its TenantContextMiddleware resolves the tenant, sets
    request.tenant, and bridges into boundary's TenantContext. Boundary's own
    TenantMiddleware and resolver chain are for boundary-only deployments (no
    identity). Running both middlewares double-resolves the tenant per
    request. Detected by string suffix match on MIDDLEWARE entries; boundary
    must never import icv_identity (ADR-002, ADR-025 T1).
    """
    from django.conf import settings

    middleware = getattr(settings, "MIDDLEWARE", [])

    has_boundary_middleware = any(entry.endswith("boundary.middleware.TenantMiddleware") for entry in middleware)
    has_identity_middleware = any(
        entry.endswith("icv_identity.tenants.middleware.TenantContextMiddleware") for entry in middleware
    )

    if not (has_boundary_middleware and has_identity_middleware):
        return []

    return [
        Warning(
            "Both boundary.middleware.TenantMiddleware and icv-identity's "
            "TenantContextMiddleware are in MIDDLEWARE. This double-resolves the "
            "tenant on every request.",
            hint=(
                "Per ADR-025 T1, when icv-identity is present it owns tenant "
                "resolution and bridges into boundary. Remove "
                "boundary.middleware.TenantMiddleware and let icv-identity resolve "
                "and bridge, or run boundary-only without icv-identity's middleware."
            ),
            id="boundary.W002",
        )
    ]


def _check_rls_enabled():
    """E006: Verify RLS is enabled on all tenant-scoped tables (PostgreSQL only).

    Recognises models using TenantMixin, make_tenant_mixin(), or any model
    with a ``_boundary_fk_field`` attribute (custom tenant base classes).
    """
    from django.apps import apps
    from django.conf import settings
    from django.db import connection

    if connection.vendor != "postgresql":
        return []

    # Resolved the same way as boundary.conf.get_tenant_model(): either
    # setting is enough to gate on, and if neither is set E001 already
    # reports it, so this check has nothing useful to add.
    model_string = getattr(settings, "BOUNDARY_TENANT_MODEL", None) or getattr(settings, "ICV_TENANT_MODEL", None)
    if not model_string:
        return []  # E001 will catch this

    from boundary.models import has_tenant_column, is_tenant_model

    errors = []
    for model in apps.get_models():
        if not is_tenant_model(model):
            continue
        if model._meta.abstract:
            continue
        # Path-scoped models (make_tenant_path_mixin) have no local tenant
        # column to put an RLS policy on, and this exemption is intentional,
        # not a gap: relation-scoped isolation is an application-layer-only
        # contract (issue #14). The parent's policy protects the parent
        # table (and therefore ORM queries that join through the path), but
        # never the child table itself, so there is deliberately nothing to
        # check here. See docs/how-to/scope-models-through-a-relation.md.
        if not has_tenant_column(model):
            continue

        table = model._meta.db_table
        try:
            with connection.cursor() as cursor:
                # to_regclass() resolves *table* through the connection's own
                # search_path, exactly as any ordinary query against the
                # model's table would, and returns a single OID (or NULL if
                # nothing resolves). WHERE relname = %s with no schema
                # qualification instead returns one row per schema that
                # happens to contain a same-named table (a partition
                # archive, a staging schema, a multi-entry search_path), and
                # fetchone() silently reads whichever row the planner's
                # index scan produced first, which is not necessarily the
                # table Django is actually configured against (issue #34).
                cursor.execute(
                    "SELECT relrowsecurity, relforcerowsecurity FROM pg_class WHERE oid = to_regclass(%s)::oid",
                    [table],
                )
                row = cursor.fetchone()
                if row is None:
                    # to_regclass() returns NULL for a name that resolves to
                    # nothing on this search_path, which is the same
                    # pre-migration state the old query's fetchone() == None
                    # branch handled: the table doesn't exist yet, so there
                    # is nothing to check.
                    continue
                rls_enabled, rls_forced = row
                if not rls_enabled or not rls_forced:
                    errors.append(
                        Error(
                            f"Table '{table}' (model {model.__name__}) does "
                            f"not have Row Level Security enabled and forced. "
                            f"Run EnableRLS migration operation.",
                            id="boundary.E006",
                        )
                    )
        except _CONNECTION_UNAVAILABLE_ERRORS:
            # The database itself is not reachable (connection refused,
            # auth failure, closed connection): the legitimate skip this
            # branch has always existed for, e.g. running `manage.py check`
            # before the database is provisioned. Silence here is correct
            # because there is nothing to report against.
            continue
        except Exception as exc:
            # The connection IS there and the query failed for some other
            # reason (a permissions error reading pg_class, a statement
            # timeout, a lock). That is indistinguishable from "every table
            # is correctly protected" if swallowed, which makes this check
            # fail open exactly where it matters (issue #34). Report it
            # instead of staying silent.
            errors.append(
                Warning(
                    f"Could not determine Row Level Security state for table '{table}' (model {model.__name__}): {exc}",
                    hint=(
                        "The database connection is available but the query "
                        "against pg_class failed, so boundary.E006 could not "
                        "verify this table. Check the connecting role has "
                        "SELECT on pg_class, and investigate the underlying "
                        "error before treating the absence of boundary.E006 "
                        "as a pass."
                    ),
                    id="boundary.W007",
                )
            )

    return errors


def _check_rls_bypassable():
    """W003: warn when the connecting role bypasses RLS entirely.

    PostgreSQL exempts superusers and BYPASSRLS roles from every policy,
    including FORCE ROW LEVEL SECURITY tables (issue #21). E006 verifies
    RLS is enabled and forced on the tables; it says nothing about whether
    the connecting role can bypass what those tables declare. A dev/CI role
    that is a BYPASSRLS superuser (the default bootstrap role for nearly
    every postgres docker image) makes E006 pass and every RLS-policy test
    pass vacuously: the policies are configured correctly and enforce
    nothing for this connection.

    Deliberately not skipped under pytest/DEBUG. The failure this check
    exists to catch lives precisely in local/CI test runs on a bypassing
    role; suppressing it there would rebuild the silent-default trap the
    check exists to close. A consumer that has deliberately chosen a
    superuser connection (initial provisioning, some managed deployments)
    silences it by ID via SILENCED_SYSTEM_CHECKS, which is greppable and
    reviewed, rather than the check falling silent by default.
    """
    from django.db import connection

    if connection.vendor != "postgresql":
        return []

    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user")
            row = cursor.fetchone()
    except _CONNECTION_UNAVAILABLE_ERRORS:
        # Genuinely unreachable connection: the same legitimate pre-migrate
        # skip as _check_rls_enabled (issue #34).
        return []
    except Exception as exc:
        # The connection is there and the query against pg_roles failed for
        # some other reason. Silence here is the same fail-open trap E006
        # had: report it as boundary.W007 instead of letting it read as
        # "this role does not bypass RLS".
        return [
            Warning(
                f"Could not determine whether the database connection role bypasses Row Level Security: {exc}",
                hint=(
                    "The database connection is available but the query "
                    "against pg_roles failed, so boundary.W003 could not "
                    "verify the connecting role. Check the connecting role "
                    "has SELECT on pg_roles, and investigate the underlying "
                    "error before treating the absence of boundary.W003 as "
                    "a pass."
                ),
                id="boundary.W007",
            )
        ]

    if row is None:
        return []

    rolsuper, rolbypassrls = row
    if not (rolsuper or rolbypassrls):
        return []

    return [
        Warning(
            "The database connection role bypasses Row Level Security, so "
            "RLS policies will not be enforced for this connection: "
            "tenant-isolation tests will pass without testing anything. "
            "Connect as a role without SUPERUSER or BYPASSRLS. If this "
            "role is deliberate (initial provisioning, some managed "
            "deployments), silence boundary.W003 in SILENCED_SYSTEM_CHECKS.",
            hint=(
                "PostgreSQL exempts superuser and BYPASSRLS roles from "
                "every RLS policy, even FORCE ROW LEVEL SECURITY tables. "
                "See docs/explanation/isolation-layers.md#important-limits-of-rls."
            ),
            id="boundary.W003",
        )
    ]
