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


# These external middleware paths are configuration contracts, not imports.
# Both set request.tenant only after their own authorisation checks and bridge
# it into TenantContext. Keeping the legacy identity path during the migration
# window lets consumers upgrade their packages independently.
_EXTERNAL_TENANT_CONTEXT_MIDDLEWARE = (
    "icv_tenants.middleware.TenantContextMiddleware",
    "icv_identity.tenants.middleware.TenantContextMiddleware",
)


def _has_external_tenant_context_middleware(middleware):
    """Return whether a supported external resolver is configured.

    A suffix match intentionally permits a consumer's wrapper module while
    avoiding an import of either optional domain package.
    """
    return any(
        entry.endswith(path) for entry in middleware for path in _EXTERNAL_TENANT_CONTEXT_MIDDLEWARE
    )


@register(Tags.models)
def check_boundary_configuration(app_configs, **kwargs):
    """Validate boundary settings at startup."""
    errors = []

    errors.extend(_check_tenant_model())
    errors.extend(_check_resolvers())
    errors.extend(_check_middleware())
    errors.extend(_check_strict_mode())
    errors.extend(_check_production_posture())
    errors.extend(_check_rls_enabled())
    errors.extend(_check_external_double_resolve())
    errors.extend(_check_rls_bypassable())
    errors.extend(_check_client_controlled_resolver_without_membership_check())
    errors.extend(_check_regional_router_configured())
    errors.extend(_check_subdomain_resolver_without_parent_domain())
    errors.extend(_check_db_session_var_disabled_with_rls_enabled())
    errors.extend(_check_adopted_tables())

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
    """E004: TenantMiddleware (or an equivalent) must be in MIDDLEWARE.

    Satisfied three ways:

    1. A MIDDLEWARE entry is the literal string
       ``boundary.middleware.TenantMiddleware``.
    2. A MIDDLEWARE entry resolves (via ``import_string``) to a subclass of
       ``TenantMiddleware`` (issue #52). A consumer that wraps
       ``TenantMiddleware`` to add its own gating (Magmify's host-scoped
       ``SiteTenantBoundaryMiddleware`` is the documented-in-the-wild case)
       still performs tenant resolution on every request; a literal-string
       test cannot see the subclass. Matched by ``issubclass``, following
       the same pattern ``boundary.W006`` already established for this
       module. An entry that fails to import (``ImportError`` or
       ``AttributeError``, e.g. a dotted path with no such attribute) is
       treated as not satisfying the check rather than raising: Django's own
       middleware loading reports an unimportable entry separately, and this
       check has nothing useful to add for a broken path.
    3. A MIDDLEWARE entry ends with a supported external
       ``TenantContextMiddleware``: the canonical
       ``icv_tenants.middleware.TenantContextMiddleware`` or the legacy
       ``icv_identity.tenants.middleware.TenantContextMiddleware`` (issue
       #90). Each authorises its own tenant selection before bridging it into
       boundary's TenantContext. A deployment using either external resolver
       and no boundary middleware is a documented, intended shape, not a
       misconfiguration. Detection is a string suffix match because boundary
       must never import optional domain packages (ADR-025 T1, ADR-118).

    Otherwise E004 fires: no MIDDLEWARE entry resolves tenant context, which
    is a genuine boundary-only misconfiguration.
    """
    from django.conf import settings
    from django.utils.module_loading import import_string

    from boundary.middleware import TenantMiddleware

    middleware = getattr(settings, "MIDDLEWARE", [])

    if _has_external_tenant_context_middleware(middleware):
        return []

    for entry in middleware:
        if entry == "boundary.middleware.TenantMiddleware":
            return []
        try:
            middleware_class = import_string(entry)
        except (ImportError, AttributeError):
            continue  # not this check's concern; Django reports a broken path itself
        if isinstance(middleware_class, type) and issubclass(middleware_class, TenantMiddleware):
            return []

    return [
        Error(
            "boundary.middleware.TenantMiddleware is not in MIDDLEWARE.",
            hint=(
                "Add 'boundary.middleware.TenantMiddleware' (or a subclass of it) to "
                "MIDDLEWARE before SessionMiddleware. If icv-tenants is installed and "
                "its TenantContextMiddleware is mounted, it already owns authorised tenant "
                "resolution (ADR-118) and boundary needs no middleware of its own. The legacy "
                "icv-identity TenantContextMiddleware remains supported during migration."
            ),
            id="boundary.E004",
        )
    ]


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


#: The two settings that, alone, remove a category of isolation a deployment
#: believes it has (BR-CHK-001). Each entry is the setting name, what turning
#: it off actually does, and the layer-specific remedy sentence.
_POSTURE_SETTINGS = (
    (
        "BOUNDARY_STRICT_MODE",
        (
            "a queryset executed with no active tenant context returns every "
            "tenant's rows instead of raising, which is a cross-tenant read "
            "that nothing logs"
        ),
    ),
    (
        "BOUNDARY_SET_DB_SESSION_VAR",
        (
            "nothing writes the database session variable every Row Level "
            "Security policy reads, so a deployment carrying live policies "
            "has them all evaluating against an empty tenant"
        ),
    ),
)


def _check_production_posture():
    """E008: refuse an unsafe production posture (BR-CHK-001).

    Reports one Error per offending setting when ``settings.DEBUG`` is False
    and either ``BOUNDARY_STRICT_MODE`` or ``BOUNDARY_SET_DB_SESSION_VAR`` is
    False. Both default to the safe value, so reaching this state takes an
    explicit opt-out; an opt-out set during development and never revisited is
    precisely how a deployment ships with no enforced boundary, which is the
    one outcome the package exists to prevent (issue #82).

    Settings-only, no database. This check reads ``settings`` and nothing
    else: it issues no query, opens no connection, and is therefore not gated
    on vendor, connection availability or migration state. It reports
    identically on SQLite, on PostgreSQL and with no database configured at
    all, which is what makes it the one check in the package a consumer cannot
    have silently skipped (contrast the vendor gates on E006, W003 and W009).

    W001 and W009 are unchanged. Both keep warning exactly as they do today at
    every ``DEBUG`` value, so E008 adds a severity at ``DEBUG = False`` rather
    than replacing either: a warning in development, an error in production.

    E008 fires under the test runner, not only in production. Django's test
    runner sets ``settings.DEBUG`` to False for the duration of the run
    (``DiscoverRunner.setup_test_environment()``; pytest-django does the
    same), and ``migrate`` runs the system checks when it creates the test
    database, so a consumer whose test settings module sets either flag to
    False fails at test-database creation before a single test runs. That is
    the first place most consumers meet this check, so the hint names it
    rather than speaking only of production.
    """
    from django.conf import settings

    if getattr(settings, "DEBUG", False):
        return []

    errors = []
    for setting_name, consequence in _POSTURE_SETTINGS:
        if getattr(settings, setting_name, True):
            continue
        errors.append(
            Error(
                f"{setting_name} is False with DEBUG = False. With it off, {consequence}.",
                hint=(
                    f"Set {setting_name} = True (the default). This check fires "
                    f"wherever DEBUG is False, including under the test runner, "
                    f"where DEBUG is False, so a test settings module that "
                    f"disables the flag fails test-database creation. A "
                    f"deliberately ORM-only deployment, or a test suite that "
                    f"means it, records the choice by adding 'boundary.E008' to "
                    f"SILENCED_SYSTEM_CHECKS in the settings module that "
                    f"disables the flag, which for a test-only posture is the "
                    f"test settings module rather than the production one."
                ),
                id="boundary.E008",
            )
        )
    return errors


def _check_external_double_resolve():
    """W002: warn when boundary and an external package both resolve a tenant.

    The canonical icv-tenants middleware, and the legacy icv-identity
    middleware during migration, authorise tenant selection before setting
    request.tenant and bridging into boundary's TenantContext. Boundary's own
    TenantMiddleware and resolver chain are for boundary-only deployments.
    Running either external middleware with boundary's middleware
    double-resolves a tenant per request. Detection is string-only, so
    boundary never imports optional domain packages (ADR-025 T1, ADR-118).
    """
    from django.conf import settings

    middleware = getattr(settings, "MIDDLEWARE", [])

    has_boundary_middleware = any(entry.endswith("boundary.middleware.TenantMiddleware") for entry in middleware)
    has_external_middleware = _has_external_tenant_context_middleware(middleware)

    if not (has_boundary_middleware and has_external_middleware):
        return []

    return [
        Warning(
            "Both boundary.middleware.TenantMiddleware and an external "
            "TenantContextMiddleware are in MIDDLEWARE. This double-resolves the "
            "tenant on every request.",
            hint=(
                "When icv-tenants is present it owns authorised tenant resolution and "
                "bridges into boundary. Remove boundary.middleware.TenantMiddleware and "
                "let the external middleware resolve and bridge, or run boundary-only "
                "without external tenant middleware. The legacy icv-identity middleware "
                "remains supported during migration (ADR-118)."
            ),
            id="boundary.W002",
        )
    ]


def _check_client_controlled_resolver_without_membership_check():
    """W006: warn when a client-controlled resolver is configured alongside
    django.contrib.auth with nothing downstream enforcing membership.

    Per BR-RES-009 (issue #38): boundary resolves WHICH tenant a request
    targets and never establishes WHETHER the caller may access it.
    HeaderResolver and JWTClaimResolver read the tenant straight from
    values the client fully controls (an arbitrary header, an unverified
    JWT claim), so any authenticated caller can name any tenant and every
    downstream layer then works correctly for that choice: the ORM filters
    to it, RLS scopes to it, the session variable is set to it. Isolation
    is intact and pointed at the tenant the caller asked for, not the one
    they belong to.

    This only bites when there are authenticated users to mis-scope in the
    first place, hence gating on django.contrib.auth being installed. An
    application with no authenticated users (a public API keyed entirely
    by an API key that already encodes the tenant, for example) has no
    membership boundary to have missed.

    Matched by importing each configured resolver path and checking
    issubclass against HeaderResolver / JWTClaimResolver, not by string
    suffix like boundary.W002. A consumer subclass
    (``class MyHeaderResolver(HeaderResolver): ...``) inherits the same
    trust boundary unless it overrides resolve() to add its own
    verification, and this check cannot tell those two cases apart from
    the dotted path alone, so it treats every subclass as client-controlled
    and lets a consumer who has actually verified membership silence the
    ID. A path that fails to import is left to boundary.E003 to report;
    this check has nothing useful to add for a broken path.
    """
    from django.conf import settings
    from django.utils.module_loading import import_string

    from boundary.resolvers import HeaderResolver, JWTClaimResolver

    if "django.contrib.auth" not in settings.INSTALLED_APPS:
        return []

    resolver_paths = getattr(
        settings,
        "BOUNDARY_RESOLVERS",
        ["boundary.resolvers.SubdomainResolver"],
    )

    client_controlled = []
    for path in resolver_paths:
        try:
            resolver_class = import_string(path)
        except ImportError:
            continue  # boundary.E003 already reports this path
        if issubclass(resolver_class, (HeaderResolver, JWTClaimResolver)):
            client_controlled.append(path)

    if not client_controlled:
        return []

    return [
        Warning(
            "A client-controlled resolver ("
            + ", ".join(client_controlled)
            + ") is configured in BOUNDARY_RESOLVERS alongside "
            "django.contrib.auth. The tenant is taken directly from a "
            "value the client supplies (a header or an unverified JWT "
            "claim), so any authenticated caller can name any tenant. "
            "boundary resolves tenancy; it does not check that the "
            "authenticated principal is a member of the resolved tenant.",
            hint=(
                "Add a check, after TenantMiddleware and your "
                "authentication middleware, that the authenticated user is "
                "a member of request.tenant, and return 403 otherwise. See "
                "docs/how-to/choose-and-order-resolvers.md#enforce-membership-after-resolution. "
                "If icv-identity is installed, its own middleware provides "
                "this per ADR-025 T1 and you should not hand-roll it. If "
                "you have already handled membership elsewhere, silence "
                "boundary.W006 in SILENCED_SYSTEM_CHECKS."
            ),
            id="boundary.W006",
        )
    ]


def _check_regional_router_configured():
    """E005: BOUNDARY_REGIONS configured but RegionalRouter absent from
    DATABASE_ROUTERS.

    Per BR-REG-002, RegionalRouter is never added to DATABASE_ROUTERS
    automatically: the integrator adds it explicitly so the routing
    configuration is visible, intentional, and auditable in version control.
    That deliberate omission has a failure mode: a project that sets
    BOUNDARY_REGIONS to declare its regions, but forgets the DATABASE_ROUTERS
    line, gets no error. RegionalRouter._route() returns "default" for every
    query the moment BOUNDARY_REGIONS is truthy but the router itself is
    never consulted, so every tenant silently reads and writes the default
    database regardless of its configured region. For a data-residency
    feature (docs/how-to/deploy-multi-region.md), a silent fallback to the
    wrong database is a compliance problem, not a cosmetic one (issue #36).

    An empty BOUNDARY_REGIONS ({}) is treated as unconfigured, matching
    RegionalRouter._route()'s own `if not regions: return "default"` check:
    a project that has not yet populated any region has not opted into
    regional routing, so there is nothing for this check to enforce yet.

    Matched by issubclass against RegionalRouter, not by string suffix like
    boundary.W002. Two things make issubclass the right call here, where
    W002 uses a suffix match and boundary.W006 already established
    issubclass as a workable pattern for this module:

    - Django's own DATABASE_ROUTERS accepts either a dotted string (which
      Django instantiates via import_string) or an already-constructed
      router instance (django.db.utils.ConnectionRouter.routers). A pure
      string check cannot see an instance at all, so it would silently
      pass over a perfectly valid configuration.
    - Subclassing a Django database router to compose it with other routing
      concerns (multi-database read replicas, sharding) is an ordinary
      pattern for this kind of class, even though
      docs/how-to/deploy-multi-region.md does not itself document
      subclassing RegionalRouter the way choose-and-order-resolvers.md
      documents subclassing a resolver. A consumer subclass still performs
      RegionalRouter's own _route() (or delegates to it via super()), so it
      still satisfies BR-REG-002's intent: the routing decision is present
      and explicit in DATABASE_ROUTERS, just wrapped.

    A DATABASE_ROUTERS entry that is a dotted path importing successfully
    but not a RegionalRouter (or subclass) is not this check's concern; an
    unimportable path is a Django-level misconfiguration this check does
    not attempt to diagnose, since DATABASE_ROUTERS has no boundary-owned
    equivalent of boundary.E003 to defer to.
    """
    from django.conf import settings
    from django.utils.module_loading import import_string

    from boundary.routing import RegionalRouter

    regions = getattr(settings, "BOUNDARY_REGIONS", None)
    if not regions:
        return []

    database_routers = getattr(settings, "DATABASE_ROUTERS", [])

    for entry in database_routers:
        if isinstance(entry, str):
            try:
                router = import_string(entry)
            except ImportError:
                continue  # not this check's concern; see docstring
        else:
            # Django also accepts a constructed router instance in
            # DATABASE_ROUTERS; check its class, not the string form.
            router = type(entry)

        if isinstance(router, type) and issubclass(router, RegionalRouter):
            return []

    return [
        Error(
            "BOUNDARY_REGIONS is configured but 'boundary.routing.RegionalRouter' is not present in DATABASE_ROUTERS.",
            hint=(
                "Add 'boundary.routing.RegionalRouter' to DATABASE_ROUTERS. "
                "Without it, every query silently stays on the 'default' "
                "database alias regardless of a tenant's configured region: "
                "see docs/how-to/deploy-multi-region.md#common-pitfalls."
            ),
            id="boundary.E005",
        )
    ]


def _check_subdomain_resolver_without_parent_domain():
    """W008: warn when SubdomainResolver is configured without
    BOUNDARY_SUBDOMAIN_PARENT_DOMAIN.

    Per BR-RES-010 (issue #22): SubdomainResolver.resolve() takes the first
    label of any host with three or more labels and looks it up as a tenant
    slug, with no check that the host belongs to the deployment's own
    domain. A deployment that serves both platform subdomains and
    customer-owned custom domains from the same resolver chain will resolve
    the WRONG tenant for a foreign host whose first label happens to match a
    tenant slug: `shop.example.co.uk`, a domain the deployment never
    intended to serve, still resolves whichever tenant is slugged `shop`.
    That is cross-tenant serving.

    BOUNDARY_SUBDOMAIN_PARENT_DOMAIN closes this by constraining resolution
    to hosts that are exactly one label above one of the configured parent
    domains. Leaving it unset preserves the pre-existing behaviour (a
    project with a single, closed set of hosts behind ALLOWED_HOSTS may
    never hit the foreign-host case in practice), so this check is a
    Warning, not an Error, and does not fire unless SubdomainResolver (or a
    subclass) is actually configured.

    Matched by issubclass against SubdomainResolver, following the pattern
    boundary.W006 already established for this module: a consumer subclass
    inherits the same unconstrained-host behaviour unless it overrides
    resolve() itself, and issubclass sees that where a dotted-path suffix
    match would not. A path that fails to import is left to boundary.E003
    to report; this check has nothing useful to add for a broken path.
    """
    from django.conf import settings
    from django.utils.module_loading import import_string

    from boundary.resolvers import SubdomainResolver

    if getattr(settings, "BOUNDARY_SUBDOMAIN_PARENT_DOMAIN", None):
        return []

    resolver_paths = getattr(
        settings,
        "BOUNDARY_RESOLVERS",
        ["boundary.resolvers.SubdomainResolver"],
    )

    unconstrained = []
    for path in resolver_paths:
        try:
            resolver_class = import_string(path)
        except ImportError:
            continue  # boundary.E003 already reports this path
        if issubclass(resolver_class, SubdomainResolver):
            unconstrained.append(path)

    if not unconstrained:
        return []

    return [
        Warning(
            "SubdomainResolver (" + ", ".join(unconstrained) + ") is configured "
            "in BOUNDARY_RESOLVERS without BOUNDARY_SUBDOMAIN_PARENT_DOMAIN. It "
            "resolves the first label of ANY host with three or more labels, "
            "including a foreign host outside your own domain whose first "
            "label happens to match a tenant slug.",
            hint=(
                "Set BOUNDARY_SUBDOMAIN_PARENT_DOMAIN to your deployment's own "
                "parent domain (or a list of them) so SubdomainResolver only "
                "resolves a host that is exactly <slug>.<your-domain>. See "
                "docs/how-to/choose-and-order-resolvers.md#constrain-subdomainresolver-to-your-own-domain. "
                "If every host this deployment serves is already guaranteed to "
                "be your own (ALLOWED_HOSTS is a closed list with no "
                "customer-owned custom domains), silence boundary.W008 in "
                "SILENCED_SYSTEM_CHECKS."
            ),
            id="boundary.W008",
        )
    ]


def _rls_probe_targets(apps):
    """Yield ``(model, table, adopted)`` for every table RLS must protect.

    The shared table selection behind ``boundary.E006`` and
    ``boundary.W009``, so the two cannot disagree about which tables RLS is
    expected on. Two sources, in this order:

    - Column-bearing models, recognised through ``is_tenant_model()`` and
      ``has_tenant_column()``. Path-scoped models (``make_tenant_path_mixin``)
      have no local tenant column to put a policy on, and this exemption is
      intentional, not a gap: relation-scoped isolation is an
      application-layer-only contract (issue #14). The parent's policy
      protects the parent table (and therefore ORM queries that join through
      the path), but never the child table itself, so there is deliberately
      nothing to check for one. See
      docs/how-to/scope-models-through-a-relation.md.
    - Adopted tables, derived from the live registry against
      ``BOUNDARY_TENANT_APPS`` (BR-RLS-010). An adopted table is exactly the
      case where RLS is load-bearing on its own: there is no manager, no
      field and no ORM filtering behind it, so a table whose RLS was never
      enabled or was later disabled isolates nothing and nothing else in the
      system notices.

    ``adopted`` is what lets the caller word its message and hint for the
    right remedy: ``EnableRLS`` for a column-bearing model, a re-applied
    ``AdoptTenantApp`` for an adopted table, which are different migrations
    in different apps.

    An adopted model that somehow also carries a mixin is yielded once, from
    the column-bearing pass, since the two categories are meant to be
    mutually exclusive and reporting one table twice would read as two
    separate defects.
    """
    from boundary.models import has_tenant_column, is_tenant_model

    seen = set()
    for model in apps.get_models():
        if not is_tenant_model(model):
            continue
        if model._meta.abstract:
            continue
        if not has_tenant_column(model):
            continue
        seen.add(model._meta.db_table)
        yield model, model._meta.db_table, False

    for model, table in _expected_adopted_tables():
        if table in seen:
            continue
        seen.add(table)
        yield model, table, True


def _check_rls_enabled():
    """E006: Verify RLS is enabled on all tenant-scoped tables (PostgreSQL only).

    Recognises models using TenantMixin, make_tenant_mixin(), or any model
    with a ``_boundary_fk_field`` attribute (custom tenant base classes),
    and every table ``BOUNDARY_TENANT_APPS`` expects to be adopted
    (BR-RLS-010), which carries RLS but no Django field at all.
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

    errors = []
    for model, table, adopted in _rls_probe_targets(apps):
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
                    if adopted:
                        message = (
                            f"Table '{table}' (model {model.__name__}) is an "
                            f"adopted table that does not have Row Level "
                            f"Security enabled and forced."
                        )
                        hint = (
                            f"Re-apply the "
                            f"AdoptTenantApp('{model._meta.app_label}') "
                            f"migration, which enables and forces RLS as part "
                            f"of adopting the table. An adopted table has no "
                            f"ORM layer behind it, so RLS is the only thing "
                            f"isolating it. See BR-RLS-011."
                        )
                    else:
                        message = (
                            f"Table '{table}' (model {model.__name__}) does "
                            f"not have Row Level Security enabled and forced. "
                            f"Run EnableRLS migration operation."
                        )
                        hint = None
                    errors.append(Error(message, hint=hint, id="boundary.E006"))
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


def _check_db_session_var_disabled_with_rls_enabled():
    """W009: warn when BOUNDARY_SET_DB_SESSION_VAR is off but RLS is live.

    Issue #53 added BOUNDARY_SET_DB_SESSION_VAR (default True) so a
    deployment using boundary for ORM-layer scoping only, with no RLS
    policies enabled, can skip the set_config() round trip on every context
    entry and exit. RLS enforcement depends entirely on that session
    variable: a deployment that has since enabled RLS on a tenant table but
    left the opt-out on would silently stop writing the variable RLS reads,
    which is a genuine isolation failure, not a performance choice. This
    check closes that footgun by firing only when both conditions hold at
    once.

    Reuses the same pg_class probe and the same table selection as
    ``_check_rls_enabled`` (boundary.E006): ``to_regclass()`` resolves the
    table through the connection's own search_path exactly as an ordinary
    query would, avoiding the unqualified ``WHERE relname = %s`` ambiguity
    issue #34 identified. PostgreSQL-only and skipped when the database is
    unavailable, matching E006's own gates; a table that does not exist yet
    (pre-migration) has nothing for this check to warn about either.

    Adopted tables (BR-RLS-010) are included, and are the sharper case: a
    column-bearing model still has its ``TenantManager`` filtering rows when
    the session variable stops being written, while an adopted table has no
    ORM layer at all and is left with no isolation whatsoever.
    """
    from django.apps import apps
    from django.conf import settings
    from django.db import connection

    if getattr(settings, "BOUNDARY_SET_DB_SESSION_VAR", True):
        return []

    if connection.vendor != "postgresql":
        return []

    model_string = getattr(settings, "BOUNDARY_TENANT_MODEL", None) or getattr(settings, "ICV_TENANT_MODEL", None)
    if not model_string:
        return []  # E001 will catch this

    errors = []
    for model, table, adopted in _rls_probe_targets(apps):
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT relrowsecurity, relforcerowsecurity FROM pg_class WHERE oid = to_regclass(%s)::oid",
                    [table],
                )
                row = cursor.fetchone()
                if row is None:
                    continue
                rls_enabled, rls_forced = row
                if rls_enabled and rls_forced:
                    if adopted:
                        message = (
                            f"BOUNDARY_SET_DB_SESSION_VAR is False but adopted table "
                            f"'{table}' (model {model.__name__}) has Row Level Security "
                            f"enabled and forced. RLS depends on the session variable "
                            f"this setting disables writing, so tenant isolation on this "
                            f"table is not actually enforced."
                        )
                        hint = (
                            f"Set BOUNDARY_SET_DB_SESSION_VAR = True (the default). "
                            f"An adopted table has no ORM filtering behind it, so "
                            f"disabling the session variable leaves it with no "
                            f"isolation at all. Reverse the "
                            f"AdoptTenantApp('{model._meta.app_label}') migration, or "
                            f"remove '{model._meta.app_label}' from "
                            f"BOUNDARY_TENANT_APPS, if the opt-out is intentional. "
                            f"See the BOUNDARY_SET_DB_SESSION_VAR entry in README.md."
                        )
                    else:
                        message = (
                            f"BOUNDARY_SET_DB_SESSION_VAR is False but table '{table}' "
                            f"(model {model.__name__}) has Row Level Security enabled "
                            f"and forced. RLS depends on the session variable this "
                            f"setting disables writing, so tenant isolation on this "
                            f"table is not actually enforced."
                        )
                        hint = (
                            "Set BOUNDARY_SET_DB_SESSION_VAR = True (the default), "
                            "or remove RLS from this table if the opt-out is "
                            "intentional. See the BOUNDARY_SET_DB_SESSION_VAR entry "
                            "in README.md."
                        )
                    errors.append(Warning(message, hint=hint, id="boundary.W009"))
        except _CONNECTION_UNAVAILABLE_ERRORS:
            continue
        except Exception as exc:
            errors.append(
                Warning(
                    f"Could not determine Row Level Security state for table "
                    f"'{table}' (model {model.__name__}) while checking "
                    f"BOUNDARY_SET_DB_SESSION_VAR: {exc}",
                    hint=(
                        "The database connection is available but the query "
                        "against pg_class failed, so boundary.W009 could not "
                        "verify this table."
                    ),
                    id="boundary.W007",
                )
            )

    return errors


def _expected_adopted_tables():
    """Return ``(model, table)`` for every table BOUNDARY_TENANT_APPS adopts.

    The shared selection behind ``boundary.E007``'s per-table conditions and
    the adopted half of ``boundary.E006`` and ``boundary.W009``. Derived from
    the LIVE app registry via ``adoption.expected_adopted_models()``
    (BR-RLS-017), so a model an upstream package added after the adoption
    migration was written is included and its missing column is reported.

    Returns an empty list when nothing is configured for adoption, which is
    every deployment that has not set ``BOUNDARY_TENANT_APPS``, so the
    adopted branch of each check costs one settings read there.

    ``ImproperlyConfigured`` from an unset tenant model is swallowed: E001
    already reports that, and the adopted-set derivation needs the tenant
    model only to skip it, so re-raising here would turn one configuration
    error into a check-framework crash.
    """
    from django.core.exceptions import ImproperlyConfigured

    from boundary import adoption

    try:
        models = adoption.expected_adopted_models()
    except ImproperlyConfigured:
        return []
    return [(model, model._meta.db_table) for model in models]


def _check_adopted_tables():
    """E007: report drift on a table BOUNDARY_TENANT_APPS expects to be adopted.

    BR-RLS-017. An adopted table has no Django field, no manager and no ORM
    layer behind it, so nothing else in the system notices when its schema
    stops matching what adoption established. The load-bearing case is an
    upstream package upgrade whose migration drops and recreates an index:
    the composite ``(tenant_id, ...)`` unique index becomes a global one
    again, restoring cross-tenant uniqueness with no other signal anywhere.
    This check is the only defence against that, which is why every
    condition is an ``Error`` rather than a ``Warning``.

    Reports, one ``Error`` per failing condition per table, each naming
    ``app_label.ModelName``, the table and the condition:

    1. ``tenant_id`` absent, or present with a type other than the one the
       configured tenant model's primary key derives.
    2. Row Level Security not both enabled and forced.
    3. ``boundary_tenant_isolation`` absent.
    4. ``boundary_admin_bypass`` absent.
    5. A unique constraint or unique index other than the primary key whose
       leading column is not ``tenant_id``.
    6. An app label that is deny-listed (BR-RLS-016) or not installed, and a
       ``BOUNDARY_ADOPT_EXCLUDE`` entry naming the configured tenant model or
       a model in no listed app.

    Condition 6 is settings-only: it reads ``BOUNDARY_TENANT_APPS`` and
    ``BOUNDARY_ADOPT_EXCLUDE`` against the app registry and the deny-list,
    and is therefore reported on a non-PostgreSQL backend and with no
    database reachable at all. Every other condition, condition 1 included,
    is a fact about the table that can only be read from the catalogue, so
    the per-table half skips under exactly the gates ``boundary.E006`` uses:
    a non-PostgreSQL vendor, and a connection that is unavailable.

    A probe that fails for any other reason reports ``boundary.W007`` naming
    E007, the same could-not-determine pattern E006 and W003 use, so the
    absence of E007 cannot be misread as a pass when the query never ran.
    """
    from django.conf import settings
    from django.core.exceptions import ImproperlyConfigured
    from django.db import connection

    from boundary import adoption

    errors = []

    # Condition 6, the settings-only half. Runs before any vendor or
    # connection gate, because a deny-listed app label in the setting is a
    # misconfiguration on SQLite and against an unreachable database just as
    # much as it is on a live PostgreSQL connection.
    errors.extend(_check_adopted_settings())

    if connection.vendor != "postgresql":
        return errors

    model_string = getattr(settings, "BOUNDARY_TENANT_MODEL", None) or getattr(settings, "ICV_TENANT_MODEL", None)
    if not model_string:
        return errors  # E001 will catch this

    expected = _expected_adopted_tables()
    if not expected:
        return errors

    try:
        expected_type = _expected_tenant_column_type()
    except (ImproperlyConfigured, LookupError):
        # The tenant model is unset or points nowhere: E001 reports that,
        # and without it there is no type to compare a column against.
        return errors

    for model, table in expected:
        label = adoption.model_label(model)
        try:
            with connection.cursor() as cursor:
                errors.extend(_check_one_adopted_table(cursor, model, table, label, expected_type))
        except _CONNECTION_UNAVAILABLE_ERRORS:
            # The database itself is unreachable: the same legitimate
            # pre-migrate skip boundary.E006 has always made.
            continue
        except Exception as exc:
            errors.append(
                Warning(
                    f"Could not determine the adopted-table state for table '{table}' (model {label}): {exc}",
                    hint=(
                        "The database connection is available but a query "
                        "against the PostgreSQL catalogue failed, so "
                        "boundary.E007 could not verify this adopted table. "
                        "Check the connecting role has SELECT on pg_class, "
                        "pg_attribute, pg_policy, pg_constraint and pg_index, "
                        "and investigate the underlying error before treating "
                        "the absence of boundary.E007 as a pass."
                    ),
                    id="boundary.W007",
                )
            )

    return errors


def _check_adopted_settings():
    """Condition 6 of boundary.E007: what the settings alone can be wrong about.

    Needs no database connection, by BR-RLS-017's own division: a deny-listed
    or uninstalled app label in ``BOUNDARY_TENANT_APPS`` is wrong on its face,
    and reporting it at startup is what stops the consumer discovering it at
    the next ``migrate`` instead.

    ``BOUNDARY_ADOPT_EXCLUDE`` is validated on the same terms. An entry
    naming a model that simply no longer exists stays inert on purpose (an
    upstream upgrade may legitimately have removed a model the consumer had
    excluded, per the setting's own contract), but two entries are reported:
    one naming the configured tenant model, which was never in an adopted set
    to be excluded from and therefore signals a misunderstanding of what
    adoption covers, and one naming a model in no listed app, which excludes
    nothing and most often means the app label was left out of
    ``BOUNDARY_TENANT_APPS``.
    """
    from django.core.exceptions import ImproperlyConfigured

    from boundary import adoption
    from boundary.conf import boundary_settings

    errors = []
    listed_apps = []
    for app_label, refusal in adoption.expected_adopted_apps():
        if refusal is None:
            listed_apps.append(app_label)
            continue
        errors.append(
            Error(
                f"BOUNDARY_TENANT_APPS lists '{app_label}', which cannot be adopted: {refusal}.",
                hint=(
                    "Remove the app label from BOUNDARY_TENANT_APPS. An app "
                    "that is global by construction cannot be tenant-scoped, "
                    "and an app label that is not installed adopts nothing. "
                    "See BR-RLS-016."
                ),
                id="boundary.E007",
            )
        )

    excluded = list(boundary_settings.ADOPT_EXCLUDE)
    if not excluded:
        return errors

    try:
        tenant_label = adoption.tenant_model_label()
    except (ImproperlyConfigured, LookupError):
        tenant_label = None

    listed_app_set = set(listed_apps)
    for entry in excluded:
        if tenant_label is not None and entry == tenant_label:
            errors.append(
                Error(
                    f"BOUNDARY_ADOPT_EXCLUDE names '{entry}', which is the configured tenant model.",
                    hint=(
                        "The tenant model is never in an adopted set, so "
                        "excluding it changes nothing. Remove the entry. If "
                        "the intent was to leave the tenant model's app "
                        "unadopted, remove that app label from "
                        "BOUNDARY_TENANT_APPS instead. See BR-RLS-016."
                    ),
                    id="boundary.E007",
                )
            )
            continue
        entry_app = entry.split(".", 1)[0]
        if entry_app not in listed_app_set:
            errors.append(
                Error(
                    f"BOUNDARY_ADOPT_EXCLUDE names '{entry}', whose app '{entry_app}' "
                    f"is not in BOUNDARY_TENANT_APPS, so the entry excludes nothing.",
                    hint=(
                        f"Add '{entry_app}' to BOUNDARY_TENANT_APPS if that app "
                        f"was meant to be adopted, or remove the entry from "
                        f"BOUNDARY_ADOPT_EXCLUDE. An exclusion only has an "
                        f"effect on an app that is being adopted."
                    ),
                    id="boundary.E007",
                )
            )

    return errors


def _expected_tenant_column_type():
    """Return the PostgreSQL type an adopted ``tenant_id`` column must have.

    The same derivation ``AdoptTenantApp`` applied when it added the column
    (BR-RLS-011 condition 1), reused rather than re-implemented so the check
    cannot disagree with the operation about what "the wrong type" means.
    """
    from boundary.conf import get_tenant_model
    from boundary.migrations_ops import detect_tenant_pg_type

    return detect_tenant_pg_type(get_tenant_model()._meta.pk)


def _check_one_adopted_table(cursor, model, table, label, expected_type):
    """Return every boundary.E007 error for one expected-adopted table.

    Conditions 1 to 5 of BR-RLS-017, each reported separately so an operator
    sees all of what is wrong with a table rather than only the first thing.
    A table that does not exist at all is skipped in silence, the same
    pre-migrate state ``boundary.E006`` skips: the adopted app's own
    migrations have not run yet, and there is nothing to report against.
    """
    from boundary import adoption

    errors = []

    cursor.execute(
        "SELECT relrowsecurity, relforcerowsecurity FROM pg_class WHERE oid = to_regclass(%s)::oid",
        [table],
    )
    row = cursor.fetchone()
    if row is None:
        # The table does not resolve on this search_path: the adopted app's
        # own migrations have not run yet. Nothing to check.
        return errors
    rls_enabled, rls_forced = row

    # Condition 1: the column, and its type.
    found_type = adoption.column_type(cursor, table, "tenant_id")
    if found_type is None:
        errors.append(
            Error(
                f"Table '{table}' (model {label}) is expected to be adopted but has no tenant_id column.",
                hint=(
                    f"Add a migration in your own app applying "
                    f"AdoptTenantApp('{model._meta.app_label}'). An adoption "
                    f"migration skips a table that is already adopted, so a "
                    f"second one adopts only what is new. See BR-RLS-017."
                ),
                id="boundary.E007",
            )
        )
    elif found_type.strip().lower() != expected_type.strip().lower():
        errors.append(
            Error(
                f"Table '{table}' (model {label}) has a tenant_id column of type "
                f"'{found_type}', but the configured tenant model's primary key derives "
                f"type '{expected_type}'.",
                hint=(
                    "A tenant_id column of the wrong type cannot hold the "
                    "tenant primary keys the isolation policy compares "
                    "against. Reverse and re-apply the AdoptTenantApp "
                    "migration for this app, or reconcile the tenant model's "
                    "primary key type. See BR-RLS-011."
                ),
                id="boundary.E007",
            )
        )

    # Condition 2: RLS enabled and forced.
    if not rls_enabled or not rls_forced:
        errors.append(
            Error(
                f"Table '{table}' (model {label}) is an adopted table without Row Level "
                f"Security enabled and forced (relrowsecurity={rls_enabled}, "
                f"relforcerowsecurity={rls_forced}).",
                hint=(
                    f"Re-apply the AdoptTenantApp('{model._meta.app_label}') "
                    f"migration, which enables and forces RLS as part of "
                    f"adopting the table. Without FORCE, the table owner is "
                    f"exempt from every policy on it. See BR-RLS-002."
                ),
                id="boundary.E007",
            )
        )

    # Conditions 3 and 4: both policies.
    found_policies = adoption.policy_names(cursor, table)
    for policy, rule in (
        ("boundary_tenant_isolation", "BR-RLS-008"),
        ("boundary_admin_bypass", "BR-RLS-003"),
    ):
        if policy not in found_policies:
            errors.append(
                Error(
                    f"Table '{table}' (model {label}) is an adopted table missing the '{policy}' policy.",
                    hint=(
                        f"Re-apply the AdoptTenantApp('{model._meta.app_label}') "
                        f"migration, which creates both boundary policies as "
                        f"part of adopting the table. See {rule}."
                    ),
                    id="boundary.E007",
                )
            )

    # Condition 5: every unique form leads with tenant_id.
    errors.extend(_check_adopted_unique_forms(cursor, model, table, label))

    return errors


def _check_adopted_unique_forms(cursor, model, table, label):
    """Return boundary.E007 condition 5 errors for one adopted table.

    Both catalogue forms are inspected, because Django emits the four unique
    sources into two of them under two naming schemes: a ``unique=True``
    field becomes an inline UNIQUE constraint, while a bare
    ``models.Index(unique=...)`` or an upstream hand-written index is a plain
    unique index backing no constraint. A check reading only one form would
    miss exactly the upgrade that recreated the other.

    A unique index that backs a constraint is reported through its
    constraint rather than twice, since the two are one object to
    PostgreSQL and reporting both would make one defect look like two. The
    primary key is excluded by BR-RLS-017's own wording: adoption leaves it
    alone, so a primary key that does not lead with ``tenant_id`` is the
    expected state rather than drift.
    """
    from boundary import adoption

    errors = []
    constraint_backed = set()

    for constraint in adoption.unique_constraints(cursor, table):
        constraint_backed.add(constraint["name"])
        columns = constraint["columns"]
        if columns and columns[0] == "tenant_id":
            continue
        errors.append(
            Error(
                f"Table '{table}' (model {label}) is an adopted table whose unique "
                f"constraint '{constraint['name']}' on ({', '.join(columns) or 'an expression'}) "
                f"does not lead with tenant_id, so the value it constrains is unique "
                f"across every tenant rather than within one.",
                hint=(
                    f"Re-apply the AdoptTenantApp('{model._meta.app_label}') "
                    f"migration, which rewrites each unique form to lead with "
                    f"tenant_id. An upstream migration that dropped and "
                    f"recreated this constraint restored cross-tenant "
                    f"uniqueness with no other signal. See BR-RLS-012."
                ),
                id="boundary.E007",
            )
        )

    for index in adoption.unique_indexes(cursor, table):
        if index["primary"]:
            continue  # BR-RLS-017 excludes the primary key by name
        if index["constraint"] is not None and index["constraint"] in constraint_backed:
            continue  # already reported through its constraint
        columns = index["columns"]
        if columns and columns[0] == "tenant_id":
            continue
        errors.append(
            Error(
                f"Table '{table}' (model {label}) is an adopted table whose unique "
                f"index '{index['name']}' on ({', '.join(columns) or 'an expression'}) "
                f"does not lead with tenant_id, so the value it constrains is unique "
                f"across every tenant rather than within one.",
                hint=(
                    f"Re-apply the AdoptTenantApp('{model._meta.app_label}') "
                    f"migration, which rewrites each unique form to lead with "
                    f"tenant_id. An upstream migration that dropped and "
                    f"recreated this index restored cross-tenant uniqueness "
                    f"with no other signal. See BR-RLS-012."
                ),
                id="boundary.E007",
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
