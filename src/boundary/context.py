"""Tenant context propagation via contextvars + PostgreSQL session variable.

TenantContext is the core of boundary. All other layers read from it.
It propagates correctly across sync views, async views, middleware,
Celery tasks, and management commands.
"""

import functools
import inspect
import logging
from contextlib import ExitStack, contextmanager, nullcontext
from contextvars import ContextVar
from typing import Any

from django.db import connections

from boundary.conf import boundary_settings
from boundary.exceptions import TenantNotSetError

logger = logging.getLogger("boundary.context")

_current_tenant: ContextVar[Any | None] = ContextVar("boundary_current_tenant", default=None)


def _regional_alias(tenant: Any) -> str | None:
    """Return the regional DB alias a tenant routes to, or None (BR-CTX-009).

    Mirrors :class:`boundary.routing.RegionalRouter._route`: when
    ``BOUNDARY_REGIONS`` is configured and the tenant's region field names a
    configured region, tenant-scoped queries route to that regional alias.
    Returns ``None`` when no regional routing applies (regions unconfigured,
    the tenant has no region, or its region is not in ``BOUNDARY_REGIONS``),
    and never returns ``"default"`` (the caller always covers default).

    Computed from settings here rather than importing ``routing`` so
    ``context`` stays free of the ``context -> routing -> context`` import
    cycle.
    """
    if tenant is None:
        return None
    regions = boundary_settings.REGIONS
    if not regions:
        return None
    region = getattr(tenant, boundary_settings.REGION_FIELD, None)
    if not region or region not in regions or region == "default":
        return None
    return region


def _target_aliases(tenant: Any) -> list[str]:
    """DB aliases the tenant session variable must be set on for a tenant.

    Always includes ``"default"`` (non-tenant models and the default
    connection route there). Adds the tenant's regional alias when regional
    routing sends its tenant-scoped queries to a separate connection: without
    this, RLS on the regional database sees an empty tenant variable and a
    tenant-scoped write is unscoped or fails (BR-CTX-009, issue #7).
    """
    aliases = ["default"]
    regional = _regional_alias(tenant)
    if regional is not None and regional not in aliases:
        aliases.append(regional)
    return aliases


def _ensure_atomic(using: str = "default"):
    """Return a context manager that guarantees an active transaction.

    ``set_config(..., true)`` is transaction-local (BR-CTX-002): it has no
    effect once the surrounding transaction commits, and none at all if there
    is no surrounding transaction. Inside a request, ``TenantMiddleware``
    already wraps the call in ``transaction.atomic()``. Outside a request
    (management commands, Celery tasks, ad hoc scripts) Django's default
    ``AUTOCOMMIT = True`` means every statement is its own implicit
    transaction, so the session variable set by ``TenantContext`` is gone
    before the next query runs and RLS silently sees no tenant (#6).

    This opens ``transaction.atomic(using=using)`` only when there is no
    ambient transaction already, so nested use inside a request or an
    existing ``atomic()`` block is a no-op (avoids an unnecessary savepoint).
    Gated by ``BOUNDARY_WRAP_ATOMIC`` (default ``True``) so integrators who
    manage transactions explicitly can opt out, matching the setting
    ``TenantMiddleware`` already honours.
    """
    from django.utils.connection import ConnectionDoesNotExist

    try:
        connection = connections[using]
    except ConnectionDoesNotExist:
        # Connection does not exist (e.g. a regional alias that is configured
        # in BOUNDARY_REGIONS but not in DATABASES, typically in tests that
        # only want to verify routing logic). Treat as safe no-op: tests that
        # mock _set_db_session/_clear_db_session don't require actual atomicity.
        return nullcontext()
    if not boundary_settings.WRAP_ATOMIC:
        if not connection.in_atomic_block:
            logger.warning(
                "TenantContext.using() entered outside an active transaction with "
                "BOUNDARY_WRAP_ATOMIC=False. The tenant session variable will not "
                "survive past the next statement and RLS will see no tenant. Wrap "
                "this call in transaction.atomic(using=%r) explicitly.",
                using,
            )
        return nullcontext()
    if connection.in_atomic_block:
        return nullcontext()
    from django.db import transaction

    return transaction.atomic(using=using)


class TenantContext:
    """Static/classmethod API for tenant context management."""

    @staticmethod
    def set(tenant, *, using: str = "default") -> object:
        """Set the active tenant. Returns a token for clear().

        Also sets the PostgreSQL session variable via set_config(), on the
        ``default`` connection AND on the tenant's regional connection when
        regional routing is active (BR-CTX-009): the regional DB is where the
        tenant's RLS-scoped queries actually run, so the variable must be set
        there too or RLS sees no tenant.

        ``using`` names the base connection (default ``"default"``); the
        regional alias is added automatically. Per BR-CTX-008, if any DB call
        fails, the ContextVar is rolled back.
        """
        token = _current_tenant.set(tenant)
        try:
            if tenant is not None:
                for alias in TenantContext._aliases_for(tenant, using):
                    TenantContext._set_db_session(str(tenant.pk), using=alias)
        except Exception:
            _current_tenant.reset(token)
            raise
        logger.debug(
            "Tenant context set",
            extra={"tenant_id": str(tenant.pk) if tenant else None},
        )
        return token

    @staticmethod
    def _aliases_for(tenant, using: str) -> list[str]:
        """The DB aliases to set/clear the session var on for ``tenant``.

        ``using`` (the caller's base alias, normally ``"default"``) plus the
        tenant's regional alias when regional routing applies (BR-CTX-009).
        De-duplicated, ``using`` first.
        """
        aliases = [using]
        for alias in _target_aliases(tenant):
            if alias != using and alias not in aliases:
                aliases.append(alias)
        return aliases

    @staticmethod
    def get() -> Any | None:
        """Return the active tenant, or None if no tenant is set."""
        return _current_tenant.get()

    @staticmethod
    def clear(token, *, using: str = "default") -> None:
        """Restore the previous context using the token from set().

        Restores BOTH the ContextVar and the DB session variable (issue #13).
        Resetting the token only rewinds the ContextVar to whichever tenant
        was active before the corresponding set() call; without also
        re-applying that tenant's pk to the session variable, a nested
        set(a), set(b), clear(token_b) left the ContextVar correctly back on
        tenant A while current_setting() still read the empty string cleared
        for tenant B, so RLS saw no tenant even though TenantContext.get()
        reported one. Mirrors the restore already done in using()'s finally
        block: clear the DB session variable on the removed tenant's aliases
        (default and its regional alias, BR-CTX-009), then, if a previous
        tenant is now active, re-set the variable to that tenant's pk on ITS
        own alias set, which may be a different regional connection.
        """
        # Read the tenant that is about to be cleared BEFORE resetting, so its
        # regional alias can be cleared too.
        active = _current_tenant.get()
        _current_tenant.reset(token)
        previous = _current_tenant.get()

        aliases = TenantContext._aliases_for(active, using) if active is not None else [using]
        for alias in aliases:
            try:
                TenantContext._clear_db_session(using=alias)
            except Exception:
                # Best-effort DB cleanup; ContextVar is already restored
                logger.warning("Failed to clear DB session variable on %r", alias, exc_info=True)

        if previous is not None:
            for alias in TenantContext._aliases_for(previous, using):
                try:
                    TenantContext._set_db_session(str(previous.pk), using=alias)
                except Exception:
                    # Best-effort DB cleanup; ContextVar is already restored
                    logger.warning("Failed to restore DB session variable on %r", alias, exc_info=True)

        logger.debug("Tenant context cleared")

    @classmethod
    def require(cls) -> Any:
        """Return the active tenant, or raise TenantNotSetError."""
        tenant = cls.get()
        if tenant is None:
            label = boundary_settings.TENANT_LABEL
            raise TenantNotSetError(
                f"No {label} is active in context. Set a {label} via TenantContext.using() or TenantMiddleware."
            )
        return tenant

    @classmethod
    @contextmanager
    def using(cls, tenant, *, using: str = "default"):
        """Context manager for temporary tenant scope.

        On exit, explicitly restores both the ContextVar AND the DB session
        variable. Does NOT rely on savepoint rollback (BR-CTX-007).

        Guarantees the DB session variable actually takes effect even when
        called outside an ambient transaction (management commands, Celery
        tasks, ad hoc scripts running under Django's default autocommit).
        Without an active transaction, ``set_config(..., true)`` (BR-CTX-002)
        is scoped to a one-statement implicit transaction and vanishes before
        the next query runs, so tenant-scoped writes hit RLS with an empty
        tenant var. ``using()`` opens ``transaction.atomic()`` for its own
        body in that case (see ``_ensure_atomic``, gated by
        ``BOUNDARY_WRAP_ATOMIC``), so the body always runs under the tenant
        it just set (#6).

        Usage::

            with TenantContext.using(club):
                Booking.objects.all()  # filtered to club
        """
        previous = cls.get()
        # Open an atomic block on EVERY alias the tenant's session var will be
        # set on (default + regional), so set_config(..., true) survives on the
        # regional connection too under autocommit (BR-CTX-002, BR-CTX-009).
        aliases = cls._aliases_for(tenant, using)
        with ExitStack() as stack:
            for alias in aliases:
                stack.enter_context(_ensure_atomic(alias))
            token = cls.set(tenant, using=using)
            try:
                yield tenant
            finally:
                _current_tenant.reset(token)
                # Explicitly restore the DB session variable (BR-CTX-007). First
                # clear every alias this scope set (default + this tenant's
                # regional alias), so no connection is left carrying this
                # tenant. Then, if there was a previous tenant, re-apply it on
                # ITS own alias set (which may include a different regional
                # connection).
                for alias in aliases:
                    try:
                        cls._clear_db_session(using=alias)
                    except Exception:
                        logger.warning("Failed to clear DB session variable on %r", alias, exc_info=True)
                if previous is not None:
                    for alias in cls._aliases_for(previous, using):
                        try:
                            cls._set_db_session(str(previous.pk), using=alias)
                        except Exception:
                            logger.warning("Failed to restore DB session variable on %r", alias, exc_info=True)

    @staticmethod
    def _set_db_session(tenant_id: str, using: str = "default") -> None:
        """Set the PostgreSQL session variable via parameterised set_config().

        Uses SELECT set_config(%s, %s, true) — the third argument scopes
        the setting to the current transaction (BR-CTX-002).
        """
        connection = connections[using]
        if connection.connection is not None:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT set_config(%s, %s, true)",
                    [boundary_settings.DB_SESSION_VAR, tenant_id],
                )

    @staticmethod
    def _clear_db_session(using: str = "default") -> None:
        """Reset the PostgreSQL session variable to empty string."""
        connection = connections[using]
        if connection.connection is not None:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT set_config(%s, '', true)",
                    [boundary_settings.DB_SESSION_VAR],
                )

    @staticmethod
    def invalidate_cache(tenant) -> None:
        """Remove cache entries for the given tenant from the resolver LRU."""
        from boundary.resolvers import _cache_invalidate

        _cache_invalidate(tenant)


def tenant_scoped(tenant_arg: str | None = None):
    """Run a function inside ``TenantContext.using(<the tenant argument>)``.

    The blessed idiom for service functions and Celery tasks that receive a
    tenant explicitly and need it active in context (so manager auto-filtering
    works) without hand-rolling ``with TenantContext.using(...)`` or, worse, a
    bespoke manager.

    The tenant is resolved from a named or positional argument of the wrapped
    function and the whole call runs inside that scope.

    Usage::

        from boundary.context import tenant_scoped

        @tenant_scoped("merchant")
        def run_audit(merchant, since):
            AccountAudit.objects.filter(created__gte=since)  # auto-scoped

        @shared_task
        @tenant_scoped("merchant")
        def rebuild_index(merchant):
            ...

    The resolved argument is passed straight to ``TenantContext.using``, so it
    must be a tenant **instance** (the same thing you would pass to
    ``using()``), not a bare pk. If a task only receives an id, resolve it to
    an instance before the call (or in a thin wrapper) rather than decorating
    with the id argument.

    Args:
        tenant_arg: Name of the argument holding the tenant. Defaults to
            ``BOUNDARY_TENANT_FK_FIELD`` (e.g. ``"merchant"`` or ``"tenant"``),
            resolved at call time.
    """

    def decorator(func):
        sig = inspect.signature(func)

        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            arg_name = tenant_arg or boundary_settings.TENANT_FK_FIELD
            bound = sig.bind(*args, **kwargs)
            bound.apply_defaults()
            if arg_name not in bound.arguments:
                raise TypeError(f"tenant_scoped: {func.__qualname__} has no argument {arg_name!r} to scope by.")
            tenant = bound.arguments[arg_name]
            with TenantContext.using(tenant):
                return func(*args, **kwargs)

        return wrapper

    return decorator
