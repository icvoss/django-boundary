"""Boundary settings with defaults.

All settings use the BOUNDARY_ prefix. Access via boundary_settings
which reads from django.conf.settings lazily at access time.
"""

from django.conf import settings


def _setting(name, default=None):
    return getattr(settings, name, default)


def get_tenant_model():
    """Return the concrete tenant model class.

    Lazily resolves BOUNDARY_TENANT_MODEL via django.apps.apps.get_model().
    Equivalent to django.contrib.auth.get_user_model().
    """
    from django.apps import apps

    model_string = _setting("BOUNDARY_TENANT_MODEL")
    if not model_string:
        raise LookupError("BOUNDARY_TENANT_MODEL is not set. Add it to your Django settings.")
    return apps.get_model(model_string)


class _Settings:
    """Lazy settings that reads from django.conf.settings at access time."""

    @property
    def TENANT_MODEL(self):  # noqa: N802
        return _setting("BOUNDARY_TENANT_MODEL")

    @property
    def STRICT_MODE(self):  # noqa: N802
        return _setting("BOUNDARY_STRICT_MODE", True)

    @property
    def REQUIRED(self):  # noqa: N802
        return _setting("BOUNDARY_REQUIRED", True)

    @property
    def WRAP_ATOMIC(self):  # noqa: N802
        return _setting("BOUNDARY_WRAP_ATOMIC", True)

    @property
    def RESOLVERS(self):  # noqa: N802
        return _setting("BOUNDARY_RESOLVERS", ["boundary.resolvers.SubdomainResolver"])

    @property
    def SUBDOMAIN_FIELD(self):  # noqa: N802
        return _setting("BOUNDARY_SUBDOMAIN_FIELD", "slug")

    @property
    def HEADER_NAME(self):  # noqa: N802
        return _setting("BOUNDARY_HEADER_NAME", "X-Tenant-ID")

    @property
    def JWT_CLAIM(self):  # noqa: N802
        return _setting("BOUNDARY_JWT_CLAIM", "tenant_id")

    @property
    def SESSION_KEY(self):  # noqa: N802
        return _setting("BOUNDARY_SESSION_KEY", "boundary_tenant_id")

    @property
    def RESOLVER_CACHE_SIZE(self):  # noqa: N802
        return _setting("BOUNDARY_RESOLVER_CACHE_SIZE", 1000)

    @property
    def RESOLVER_CACHE_TTL(self):  # noqa: N802
        return _setting("BOUNDARY_RESOLVER_CACHE_TTL", 60)

    @property
    def DB_SESSION_VAR(self):  # noqa: N802
        return _setting("BOUNDARY_DB_SESSION_VAR", "app.current_tenant_id")

    @property
    def ADMIN_FLAG_VAR(self):  # noqa: N802
        return _setting("BOUNDARY_ADMIN_FLAG_VAR", "app.boundary_admin")

    @property
    def REGIONS(self):  # noqa: N802
        return _setting("BOUNDARY_REGIONS")

    @property
    def REGION_FIELD(self):  # noqa: N802
        return _setting("BOUNDARY_REGION_FIELD", "region")

    @property
    def TENANT_FK_FIELD(self):  # noqa: N802
        return _setting("BOUNDARY_TENANT_FK_FIELD", "tenant")

    @property
    def POST_PROVISION_HOOK(self):  # noqa: N802
        return _setting("BOUNDARY_POST_PROVISION_HOOK")

    @property
    def PRE_DEPROVISION_HOOK(self):  # noqa: N802
        return _setting("BOUNDARY_PRE_DEPROVISION_HOOK")


boundary_settings = _Settings()
