"""Django system checks for boundary configuration.

Registered in AppConfig.ready() and run at startup and during test collection.
"""

from django.core.checks import Error, Tags, Warning, register


@register(Tags.models)
def check_boundary_configuration(app_configs, **kwargs):
    """Validate boundary settings at startup."""
    errors = []

    errors.extend(_check_tenant_model())
    errors.extend(_check_resolvers())
    errors.extend(_check_middleware())
    errors.extend(_check_strict_mode())
    errors.extend(_check_rls_enabled())

    return errors


def _check_tenant_model():
    """E001: BOUNDARY_TENANT_MODEL must be set and refer to an installed model."""
    from django.apps import apps
    from django.conf import settings

    model_string = getattr(settings, "BOUNDARY_TENANT_MODEL", None)
    if not model_string:
        return [
            Error(
                "BOUNDARY_TENANT_MODEL is not set.",
                hint="Add BOUNDARY_TENANT_MODEL = 'app_label.ModelName' to settings.",
                id="boundary.E001",
            )
        ]

    try:
        apps.get_model(model_string)
    except LookupError:
        return [
            Error(
                f"BOUNDARY_TENANT_MODEL = '{model_string}' does not refer to an installed model.",
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

    model_string = getattr(settings, "BOUNDARY_TENANT_MODEL", None)
    if not model_string:
        return []  # E001 will catch this

    from boundary.models import is_tenant_model

    errors = []
    for model in apps.get_models():
        if not is_tenant_model(model):
            continue
        if model._meta.abstract:
            continue

        table = model._meta.db_table
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT relrowsecurity, relforcerowsecurity FROM pg_class WHERE relname = %s",
                    [table],
                )
                row = cursor.fetchone()
                if row is None:
                    continue  # Table doesn't exist yet (pre-migration)
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
        except Exception:
            pass  # DB not available at check time; skip

    return errors
