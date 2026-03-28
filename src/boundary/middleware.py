"""Tenant middleware — resolves tenant from request and sets context.

Uses Django's MiddlewareMixin for WSGI + ASGI compatibility.
Wraps the request in transaction.atomic() so set_config() has effect.
"""

import logging

from django.http import HttpResponseForbidden, HttpResponseNotFound
from django.utils.deprecation import MiddlewareMixin
from django.utils.module_loading import import_string

from boundary.conf import boundary_settings
from boundary.context import TenantContext
from boundary.signals import tenant_resolution_failed, tenant_resolved

logger = logging.getLogger("boundary.middleware")


class TenantMiddleware(MiddlewareMixin):
    """Resolve tenant from request and manage context lifecycle.

    Overrides __call__ to wrap the full request in transaction.atomic()
    when BOUNDARY_WRAP_ATOMIC is True, ensuring set_config() has effect.
    """

    def __call__(self, request):
        # Resolve tenant from the configured resolver chain
        tenant, resolver = self._resolve_tenant(request)

        # No resolver matched
        if tenant is None:
            if boundary_settings.REQUIRED:
                tenant_resolution_failed.send(sender=self.__class__, request=request)
                return HttpResponseNotFound("Tenant not found.")
            # BOUNDARY_REQUIRED=False — proceed without tenant
            return self.get_response(request)

        # Check is_active (BR-RES-004)
        if hasattr(tenant, "is_active") and not tenant.is_active:
            logger.warning(
                "Inactive tenant rejected",
                extra={"tenant_id": str(tenant.pk)},
            )
            return HttpResponseForbidden("Tenant is inactive.")

        # Fire signal
        tenant_resolved.send(
            sender=self.__class__,
            tenant=tenant,
            resolver=resolver,
            request=request,
        )
        logger.info(
            "Tenant resolved",
            extra={
                "tenant_id": str(tenant.pk),
                "resolver_name": resolver.__class__.__name__,
            },
        )

        # Set context and wrap in transaction if configured
        request.tenant = tenant

        if boundary_settings.WRAP_ATOMIC and not self._is_atomic_requests():
            from django.db import transaction

            with transaction.atomic():
                token = TenantContext.set(tenant)
                request._boundary_token = token
                try:
                    response = self.get_response(request)
                finally:
                    TenantContext.clear(token)
        else:
            token = TenantContext.set(tenant)
            request._boundary_token = token
            try:
                response = self.get_response(request)
            finally:
                TenantContext.clear(token)

        return response

    def _resolve_tenant(self, request):
        """Walk the resolver chain. Return (tenant, resolver) or (None, None)."""
        resolver_paths = boundary_settings.RESOLVERS
        for path in resolver_paths:
            try:
                resolver_cls = import_string(path)
                resolver = resolver_cls()
                tenant = resolver.resolve(request)
                if tenant is not None:
                    return tenant, resolver
            except Exception:
                logger.warning(
                    "Resolver raised exception",
                    extra={"resolver_name": path},
                    exc_info=True,
                )
        return None, None

    @staticmethod
    def _is_atomic_requests():
        """Check if ATOMIC_REQUESTS is already enabled on the default DB."""
        from django.db import connections

        return connections["default"].settings_dict.get("ATOMIC_REQUESTS", False)
