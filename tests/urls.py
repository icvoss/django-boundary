"""URLconf for boundary's own test suite.

Only needed for the ASGI integration test in test_middleware.py, which
drives an async view through django.test.AsyncClient and TenantMiddleware.
"""

import asyncio
import json

from django.http import HttpResponse
from django.urls import path

from boundary.context import TenantContext


async def tenant_across_await(request):
    """Record TenantContext.get() before and after an await (issue #16).

    Proves the tenant set by TenantMiddleware survives an actual await point
    in an async view, which is the ASGI acceptance criterion for #16: before
    the fix, the middleware cleared context (and closed its atomic block)
    before the coroutine returned by an async get_response was ever awaited,
    so an async view saw no tenant at all.
    """
    before = TenantContext.get()
    await asyncio.sleep(0)
    after = TenantContext.get()
    body = {
        "before": str(before.pk) if before else None,
        "after": str(after.pk) if after else None,
    }
    return HttpResponse(json.dumps(body), content_type="application/json")


urlpatterns = [
    path("tenant-across-await/", tenant_across_await, name="tenant-across-await"),
]
