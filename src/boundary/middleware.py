"""Tenant middleware — resolves tenant from request and sets context.

Sync-native middleware, served under ASGI via Django's built-in
sync-middleware adaptation (see the async_capable/sync_capable comment
on TenantMiddleware below).
Wraps the request in transaction.atomic() so set_config() has effect.
"""

import logging

from django.http import HttpResponseForbidden, HttpResponseNotFound
from django.utils.deprecation import MiddlewareMixin
from django.utils.module_loading import import_string

from boundary.conf import boundary_settings
from boundary.context import TenantContext, _ensure_atomic
from boundary.exceptions import TenantInactiveError, TenantResolutionError
from boundary.signals import tenant_resolution_failed, tenant_resolved

logger = logging.getLogger("boundary.middleware")


class TenantMiddleware(MiddlewareMixin):
    """Resolve tenant from request and manage context lifecycle.

    Overrides __call__ to wrap the full request in transaction.atomic()
    when BOUNDARY_WRAP_ATOMIC is True, ensuring set_config() has effect.
    """

    # Declared sync-only, not async-native (issue #16). MiddlewareMixin
    # declares async_capable = True, which under ASGI makes Django mark this
    # middleware instance as a coroutine function and hand it an async
    # get_response. But __call__ below is a plain sync function that wraps
    # get_response(request) in transaction.atomic(): calling an async
    # get_response from sync code returns an unawaited coroutine, so the
    # try/finally clears TenantContext (and exits the atomic block) before
    # the view ever runs, leaving every async-served request with no tenant
    # in context and no RLS session variable. A second facet: on the
    # no-tenant 404 and inactive-tenant 403 paths, the coroutine-marked
    # middleware returns a plain HttpResponse, which Django then tries to
    # await, raising TypeError.
    #
    # There is no async-native fix that preserves the atomic-transaction
    # guarantee: Django has no async transactions, so an __acall__ could not
    # hold transaction.atomic() open across an awaited get_response the way
    # __call__ does. Declaring async_capable = False instead makes Django's
    # middleware machinery adapt this middleware under ASGI (wrapping it in
    # sync_to_async, and using async_to_sync inside for anything downstream
    # that is itself async). That keeps TenantContext and the DB session
    # variable active for the complete request, including async views:
    # asgiref propagates contextvars across the sync/async boundary and
    # routes thread-sensitive ORM work back to this middleware's thread,
    # inside its atomic block. Flipping async_capable back to True
    # re-introduces issue #16 (context cleared before the async downstream
    # is ever awaited); do not do that without also rewriting __call__ to be
    # genuinely async and dropping the atomic-transaction guarantee.
    sync_capable = True
    async_capable = False

    def __call__(self, request):
        # Resolve tenant from the configured resolver chain
        tenant, resolver = self._resolve_tenant(request)

        label = boundary_settings.TENANT_LABEL
        label_title = label[:1].upper() + label[1:]

        # No resolver matched
        if tenant is None:
            if boundary_settings.REQUIRED:
                tenant_resolution_failed.send(sender=self.__class__, request=request)
                return HttpResponseNotFound(f"{label_title} not found.")
            # BOUNDARY_REQUIRED=False — proceed without tenant
            return self.get_response(request)

        # Check is_active (BR-RES-004). Build a TenantInactiveError and hand it
        # to _handle_inactive_tenant() so subclasses can re-raise it or return a
        # custom response, while the default translates it to a 403.
        if hasattr(tenant, "is_active") and not tenant.is_active:
            logger.warning(
                "Inactive tenant rejected",
                extra={"tenant_id": str(tenant.pk)},
            )
            exc = TenantInactiveError(f"{label_title} is inactive.")
            return self._handle_inactive_tenant(request, tenant, exc)

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

        # Set context and wrap in a transaction so set_config() has effect
        # (BR-CTX-002, issue #40). ATOMIC_REQUESTS was previously treated as
        # a reason to skip this middleware's own atomic() wrap, on the
        # assumption that Django's ATOMIC_REQUESTS transaction would already
        # cover TenantContext.set(). That assumption is false: Django's
        # ATOMIC_REQUESTS wraps the VIEW (BaseHandler.make_view_atomic), not
        # the middleware chain, so no transaction is open yet at this point
        # in __call__. set_config(..., true) is transaction-local, so the
        # session variable set here was silently discarded before the view's
        # transaction ever opened, leaving RLS with an empty tenant variable
        # while TenantContext.get() still reported the tenant correctly
        # (fail-closed under FORCE ROW LEVEL SECURITY: no rows, not a leak,
        # but the RLS layer was effectively disabled for every request).
        #
        # _ensure_atomic() (context.py) is the single source of truth for
        # "is a transaction already open right now": it checks
        # connection.in_atomic_block, which correctly reads False here
        # regardless of ATOMIC_REQUESTS, because the view has not started.
        # Opening our own atomic() here means the view's later
        # ATOMIC_REQUESTS wrap nests as a savepoint under it rather than
        # being a separate top-level transaction; Django's atomic() nesting
        # already handles that, and the "view's work commits or rolls back
        # as a unit" contract is preserved because an unhandled exception in
        # the view still unwinds through both the savepoint and this outer
        # transaction. BOUNDARY_WRAP_ATOMIC=False is honoured by
        # _ensure_atomic() itself (warn-and-no-op, matching the
        # already-documented behaviour TenantContext.using() relies on),
        # so this call needs no separate gate.
        request.tenant = tenant
        request_attr = boundary_settings.REQUEST_ATTR
        if request_attr and request_attr != "tenant":
            setattr(request, request_attr, tenant)

        with _ensure_atomic():
            token = TenantContext.set(tenant)
            request._boundary_token = token
            try:
                response = self.get_response(request)
            finally:
                TenantContext.clear(token)

        return response

    def _handle_inactive_tenant(self, request, tenant, exc):
        """Return the response for an inactive tenant.

        Override to customise (for example, redirect to a billing page). The
        default translates the raised ``TenantInactiveError`` to a 403.
        """
        label = boundary_settings.TENANT_LABEL
        label_title = label[:1].upper() + label[1:]
        return HttpResponseForbidden(f"{label_title} is inactive.")

    def _resolve_tenant(self, request):
        """Walk the resolver chain. Return (tenant, resolver) or (None, None).

        A resolver that raises is wrapped in ``TenantResolutionError``, logged,
        and skipped so the chain falls through to the next resolver (BR-RES-010).
        """
        resolver_paths = boundary_settings.RESOLVERS
        for path in resolver_paths:
            try:
                resolver_cls = import_string(path)
                resolver = resolver_cls()
                tenant = resolver.resolve(request)
                if tenant is not None:
                    return tenant, resolver
            except Exception as exc:
                error = TenantResolutionError(f"Resolver {path} failed: {exc}")
                error.__cause__ = exc
                logger.warning(
                    "Resolver raised exception",
                    extra={"resolver_name": path},
                    exc_info=True,
                )
                self._on_resolver_error(request, path, error)
        return None, None

    def _on_resolver_error(self, request, resolver_path, error):
        """Hook called when a resolver raises (after logging, before fallthrough).

        The default is a no-op so the chain falls through to the next resolver.
        Override to re-raise ``error`` (a ``TenantResolutionError``) if you want
        a failing resolver to abort resolution rather than be skipped.
        """
        return None
