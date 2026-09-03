"""Boundary settings with defaults.

All settings use the BOUNDARY_ prefix. Access via boundary_settings
which reads from django.conf.settings lazily at access time.
"""

from django.conf import settings


def _setting(name, default=None):
    return getattr(settings, name, default)


def resolve_tenant_model_setting() -> str:
    """Return the configured tenant model dotted path, resolving the fallback.

    BOUNDARY_TENANT_MODEL takes precedence; ICV_TENANT_MODEL (the single
    ecosystem-wide tenant-model knob, ADR-025 T2) is used when it is unset.
    ``_setting`` treats an explicit ``None`` the same as "not set" because
    ``getattr`` returns the falsy ``None`` value, which is what lets a
    project set ``BOUNDARY_TENANT_MODEL = None`` to defer to ICV_TENANT_MODEL.

    Raises ImproperlyConfigured, naming both settings, when neither is set
    (issue #15). This is a startup-time configuration error, not a "model
    string points somewhere that doesn't exist" error: previously this case
    raised a bare, cryptic AttributeError from settings.BOUNDARY_TENANT_MODEL
    being read directly at import time in boundary.models, before any check
    ran, so a project configured with only ICV_TENANT_MODEL could not start
    at all.
    """
    from django.core.exceptions import ImproperlyConfigured

    model_string = _setting("BOUNDARY_TENANT_MODEL") or _setting("ICV_TENANT_MODEL")
    if not model_string:
        raise ImproperlyConfigured(
            "Neither BOUNDARY_TENANT_MODEL nor ICV_TENANT_MODEL is set. Add one to your Django settings."
        )
    return model_string


def get_tenant_model():
    """Return the concrete tenant model class.

    Lazily resolves BOUNDARY_TENANT_MODEL via django.apps.apps.get_model(),
    falling back to ICV_TENANT_MODEL when unset (ADR-025 T2; other packages
    such as icv-identity and icv-payments already resolve their tenant model
    the same way). Equivalent to django.contrib.auth.get_user_model().

    Raises ImproperlyConfigured if neither setting is set (see
    resolve_tenant_model_setting()). Raises LookupError, unchanged, if the
    configured dotted path does not refer to an installed model.
    """
    from django.apps import apps

    model_string = resolve_tenant_model_setting()
    return apps.get_model(model_string)


class _Settings:
    """Lazy settings that reads from django.conf.settings at access time."""

    @property
    def TENANT_MODEL(self):  # noqa: N802
        # ADR-025 T2: BOUNDARY_TENANT_MODEL falls back to ICV_TENANT_MODEL,
        # the single ecosystem-wide tenant-model knob.
        return _setting("BOUNDARY_TENANT_MODEL") or _setting("ICV_TENANT_MODEL")

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
    def SUBDOMAIN_PARENT_DOMAIN(self):  # noqa: N802
        """One or more parent domains SubdomainResolver is constrained to.

        Accepts a single domain string, a list of domain strings, or None
        (the default). When set, SubdomainResolver.resolve() only resolves a
        host that is exactly ``<one-label>.<parent-domain>`` for one of the
        configured parents; any other host, including one at a different
        depth or with an unrelated suffix, returns None. See BR-RES-010 and
        docs/how-to/choose-and-order-resolvers.md for the cross-tenant
        serving risk this closes.

        A bare string is normalised to a single-item tuple so resolvers
        always iterate a sequence regardless of which form was configured.
        """
        value = _setting("BOUNDARY_SUBDOMAIN_PARENT_DOMAIN")
        if value is None:
            return None
        if isinstance(value, str):
            return (value,)
        return tuple(value)

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
    def FUNCTION_LEAKPROOF(self):  # noqa: N802
        """Whether to declare ``boundary_current_tenant_id()`` LEAKPROOF.

        Defaults to ``False`` because PostgreSQL only lets a superuser create
        or replace a LEAKPROOF function, and managed Postgres providers
        (DigitalOcean, RDS, Cloud SQL, Azure, Heroku, Supabase) grant no
        superuser role. Leaving it off keeps the RLS migrations runnable on
        managed hosting; isolation is enforced identically either way, since
        LEAKPROOF is only a planner optimisation (BR-RLS-009 is a SHOULD).
        Set ``BOUNDARY_FUNCTION_LEAKPROOF = True`` on a self-managed cluster
        where the migrating role is a superuser to regain the optimisation.
        """
        return _setting("BOUNDARY_FUNCTION_LEAKPROOF", False)

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
    def TENANT_LABEL(self):  # noqa: N802
        """Human-readable term used in error messages and verbose_name.

        Defaults to ``BOUNDARY_TENANT_FK_FIELD`` so a project that sets
        ``BOUNDARY_TENANT_FK_FIELD = "merchant"`` automatically gets
        ``"merchant"`` in error messages without a second setting.
        """
        return _setting("BOUNDARY_TENANT_LABEL", self.TENANT_FK_FIELD)

    @property
    def REQUEST_ATTR(self):  # noqa: N802
        """Attribute name used on the request object.

        ``request.tenant`` is always set for backwards compatibility. When
        this differs from ``"tenant"``, the same value is also assigned to
        ``request.<REQUEST_ATTR>`` so views can read ``request.merchant``.
        """
        return _setting("BOUNDARY_REQUEST_ATTR", self.TENANT_FK_FIELD)

    @property
    def POST_PROVISION_HOOK(self):  # noqa: N802
        return _setting("BOUNDARY_POST_PROVISION_HOOK")

    @property
    def PRE_DEPROVISION_HOOK(self):  # noqa: N802
        return _setting("BOUNDARY_PRE_DEPROVISION_HOOK")


boundary_settings = _Settings()
