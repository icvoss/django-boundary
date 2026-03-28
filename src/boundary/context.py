"""Tenant context propagation via contextvars + PostgreSQL session variable.

TenantContext is the core of boundary. All other layers read from it.
It propagates correctly across sync views, async views, middleware,
Celery tasks, and management commands.
"""

import logging
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

from django.db import connections

from boundary.conf import boundary_settings
from boundary.exceptions import TenantNotSetError

logger = logging.getLogger("boundary.context")

_current_tenant: ContextVar[Any | None] = ContextVar("boundary_current_tenant", default=None)


class TenantContext:
    """Static/classmethod API for tenant context management."""

    @staticmethod
    def set(tenant, *, using: str = "default") -> object:
        """Set the active tenant. Returns a token for clear().

        Also sets the PostgreSQL session variable via set_config().
        Per BR-CTX-008, if the DB call fails, the ContextVar is rolled back.
        """
        token = _current_tenant.set(tenant)
        try:
            if tenant is not None:
                TenantContext._set_db_session(str(tenant.pk), using=using)
        except Exception:
            _current_tenant.reset(token)
            raise
        logger.debug(
            "Tenant context set",
            extra={"tenant_id": str(tenant.pk) if tenant else None},
        )
        return token

    @staticmethod
    def get() -> Any | None:
        """Return the active tenant, or None if no tenant is set."""
        return _current_tenant.get()

    @staticmethod
    def clear(token, *, using: str = "default") -> None:
        """Restore the previous context using the token from set()."""
        _current_tenant.reset(token)
        try:
            TenantContext._clear_db_session(using=using)
        except Exception:
            # Best-effort DB cleanup; ContextVar is already restored
            logger.warning("Failed to clear DB session variable", exc_info=True)
        logger.debug("Tenant context cleared")

    @classmethod
    def require(cls) -> Any:
        """Return the active tenant, or raise TenantNotSetError."""
        tenant = cls.get()
        if tenant is None:
            raise TenantNotSetError(
                "No tenant is active in context. Set a tenant via TenantContext.using() or TenantMiddleware."
            )
        return tenant

    @classmethod
    @contextmanager
    def using(cls, tenant, *, using: str = "default"):
        """Context manager for temporary tenant scope.

        On exit, explicitly restores both the ContextVar AND the DB session
        variable. Does NOT rely on savepoint rollback (BR-CTX-007).

        Usage::

            with TenantContext.using(club):
                Booking.objects.all()  # filtered to club
        """
        previous = cls.get()
        token = cls.set(tenant, using=using)
        try:
            yield tenant
        finally:
            _current_tenant.reset(token)
            # Explicitly restore DB session variable (BR-CTX-007)
            try:
                if previous is not None:
                    cls._set_db_session(str(previous.pk), using=using)
                else:
                    cls._clear_db_session(using=using)
            except Exception:
                logger.warning("Failed to restore DB session variable", exc_info=True)

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
