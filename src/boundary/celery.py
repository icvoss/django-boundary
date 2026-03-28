"""Celery integration — tenant context propagation across task dispatch.

Tenant UUID and region are serialised into Celery task headers (not kwargs)
at dispatch time, and restored on the worker before task execution.
"""

import logging
from functools import wraps

from boundary.conf import boundary_settings, get_tenant_model
from boundary.context import TenantContext
from boundary.exceptions import TenantNotFoundError

logger = logging.getLogger("boundary.celery")

HEADER_TENANT_ID = "boundary_tenant_id"
HEADER_REGION = "boundary_region"


def _get_tenant_headers():
    """Read current tenant context and return headers dict for Celery dispatch."""
    tenant = TenantContext.get()
    if tenant is None:
        return {}
    headers = {HEADER_TENANT_ID: str(tenant.pk)}
    region = getattr(tenant, boundary_settings.REGION_FIELD, None)
    if region:
        headers[HEADER_REGION] = region
    return headers


def _restore_tenant_context(headers):
    """Restore tenant context from task headers.

    Returns (tenant, token) or (None, None) if no tenant header present.
    Raises TenantNotFoundError if the referenced tenant no longer exists.
    """
    tenant_id = headers.get(HEADER_TENANT_ID) if headers else None
    if not tenant_id:
        return None, None

    TenantModel = get_tenant_model()
    try:
        tenant = TenantModel.objects.get(pk=tenant_id)
    except TenantModel.DoesNotExist as exc:
        raise TenantNotFoundError(
            f"Tenant {tenant_id} no longer exists. Configure dead-letter routing for this task."
        ) from exc

    token = TenantContext.set(tenant)
    logger.info(
        "Tenant context restored for task",
        extra={"tenant_id": tenant_id},
    )
    return tenant, token


def tenant_task(func):
    """Decorator that restores tenant context on the worker side.

    The dispatch side (injecting headers) must be handled by configuring
    Celery signals or using TenantTask as a base class. This decorator
    handles the worker side: restoring context from headers before the
    task function runs.

    Usage::

        @app.task
        @tenant_task
        def send_confirmation(booking_id):
            booking = Booking.objects.get(id=booking_id)
    """

    @wraps(func)
    def wrapper(*args, **kwargs):
        try:
            from celery import current_task

            headers = getattr(current_task.request, "headers", None) or {}
        except ImportError:
            headers = {}

        tenant, token = _restore_tenant_context(headers)
        try:
            return func(*args, **kwargs)
        finally:
            if token is not None:
                TenantContext.clear(token)

    return wrapper


class TenantTask:
    """Mixin base class for Celery tasks with tenant propagation.

    Handles both dispatch (injecting headers) and execution (restoring
    context). Mix with your Celery app's Task class::

        class GenerateReport(TenantTask, app.Task):
            def run(self, report_id):
                ...

    TenantNotFoundError is excluded from autoretry_for (BR-CEL-003).
    """

    reject_on_worker_lost = False

    def apply_async(self, args=None, kwargs=None, **options):
        """Inject tenant headers at dispatch time."""
        headers = options.pop("headers", {}) or {}
        headers.update(_get_tenant_headers())
        options["headers"] = headers
        return super().apply_async(args=args, kwargs=kwargs, **options)

    def __call__(self, *args, **kwargs):
        """Restore tenant context before task execution."""
        headers = getattr(self.request, "headers", None) or {}
        tenant, token = _restore_tenant_context(headers)
        try:
            return self.run(*args, **kwargs)
        finally:
            if token is not None:
                TenantContext.clear(token)
