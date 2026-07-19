# Run Celery tasks with tenant context

## Goal

Make Celery tasks run under the same tenant that dispatched them, so that
tenant-scoped queries inside the task filter correctly without you passing a
tenant ID through task arguments.

## Prerequisites

- django-boundary installed and configured with `BOUNDARY_TENANT_MODEL`. See the
  [README](../../README.md) for base setup.
- A working Celery app with a broker.
- A tenant active in the dispatching context (set by `TenantMiddleware`, or
  manually via `TenantContext.using()`).

## How it works

boundary propagates the tenant through Celery task **headers**, not task
arguments:

1. **At dispatch**, the active tenant's primary key is serialised into two
   headers: `boundary_tenant_id` (exposed as `HEADER_TENANT_ID`) and, when the
   tenant has a region, `boundary_region` (exposed as `HEADER_REGION`).
2. **On the worker**, before the task body runs, the headers are read and the
   tenant is loaded from the database and pushed into `TenantContext`. The
   context is cleared again once the task finishes.

Because the tenant travels in headers, your task signatures stay clean and you
never risk leaking the tenant ID into task arguments or result backends in an
unexpected shape.

There are two ways to wire this in: the `TenantTask` base class (handles both
dispatch and worker sides) and the `@tenant_task` decorator (handles the worker
side only).

## Steps

### Option A: `TenantTask` base class (recommended)

`TenantTask` handles **both** sides: it injects headers in `apply_async` and
restores context in `__call__`. Mix it with your Celery app's `Task` class.

1. Define the task using `TenantTask` as the first base:

   ```python
   # bookings/tasks.py
   from celery import shared_task

   from boundary.celery import TenantTask
   from myproject.celery import app  # your Celery() instance

   class GenerateReport(TenantTask, app.Task):
       def run(self, report_id):
           # Tenant context is already active here.
           # Tenant-scoped queries filter automatically.
           report = Report.objects.get(id=report_id)
           report.build()

   GenerateReport = app.register_task(GenerateReport())
   ```

2. Dispatch it from anywhere that has an active tenant:

   ```python
   from boundary.context import TenantContext

   with TenantContext.using(club):
       GenerateReport.delay(report_id=42)
       # headers boundary_tenant_id (and boundary_region, if set)
       # are injected automatically at dispatch time
   ```

   In a request handled by `TenantMiddleware`, the tenant is already in context,
   so a bare `GenerateReport.delay(report_id=42)` picks it up.

### Option B: `@tenant_task` decorator (worker side only)

Use the decorator when you cannot change the task base class, for example when
you want a plain function task. The decorator restores context on the worker but
does **not** inject headers at dispatch. You must arrange header injection
yourself (typically by also using `TenantTask` for `apply_async`, or via your
own Celery `before_task_publish` signal that calls into the header helpers).

1. Decorate the task function, placing `@tenant_task` **below** `@app.task`:

   ```python
   # bookings/tasks.py
   from boundary.celery import tenant_task
   from myproject.celery import app

   @app.task
   @tenant_task
   def send_confirmation(booking_id):
       # Tenant context is restored from headers before this runs.
       booking = Booking.objects.get(id=booking_id)
       booking.send_confirmation_email()
   ```

## Verify it worked

Inspect the headers produced for the active tenant. This is the exact logic that
runs at dispatch:

```python
from boundary.celery import HEADER_TENANT_ID, HEADER_REGION, _get_tenant_headers
from boundary.context import TenantContext

with TenantContext.using(club):
    headers = _get_tenant_headers()

assert headers[HEADER_TENANT_ID] == str(club.pk)
# HEADER_REGION is present only when the tenant has a region value
```

Inside the running task, confirm the tenant is active:

```python
from boundary.context import TenantContext

def run(self, report_id):
    assert TenantContext.get() is not None
    ...
```

For tests, use the `set_tenant` helper from `boundary.testing` to establish a
tenant before dispatching:

```python
from boundary.testing import set_tenant

with set_tenant(tenant_a):
    headers = _get_tenant_headers()
assert headers[HEADER_TENANT_ID] == str(tenant_a.pk)
```

## Common pitfalls

- **Celery workers run in autocommit; boundary handles this for you.** Both
  `TenantTask.__call__` and `@tenant_task` wrap the restored tenant context in
  a transaction for the duration of the task body when one is not already
  active, so the DB session variable `set_config()` establishes survives for
  the whole task rather than vanishing after the first statement. This is
  controlled by `BOUNDARY_WRAP_ATOMIC` (default `True`); you do not need to
  wrap your task body in `transaction.atomic()` yourself. See
  [issue #6](https://github.com/icvoss/django-boundary/issues/6).

- **`TenantNotFoundError` is not retriable.** If the tenant referenced by the
  header no longer exists when the worker restores context, boundary raises
  `boundary.exceptions.TenantNotFoundError`. This is deliberate: retrying will
  never succeed because the tenant is gone. `TenantTask` sets
  `reject_on_worker_lost = False` and excludes this error from autoretry. Route
  such tasks to a dead-letter queue rather than retrying them. Catch it with
  `from boundary.exceptions import TenantNotFoundError`.

- **Decorator order matters.** With Option B, `@tenant_task` must sit directly on
  the function and `@app.task` above it. Reversing them means Celery wraps the
  undecorated function and context is never restored.

- **`@tenant_task` alone does not inject headers.** It only restores context on
  the worker. If you dispatch a decorated function with no tenant headers, the
  task runs with no active tenant. Use `TenantTask` (Option A) when you need
  dispatch-side injection too.

- **No tenant active at dispatch means no headers.** `_get_tenant_headers()`
  returns an empty dict when `TenantContext.get()` is `None`, so the worker runs
  with no tenant. Ensure a tenant is in context before calling `.delay()` /
  `.apply_async()`.

- **Region header is conditional.** `boundary_region` is only added when the
  tenant has a truthy value in its region field (`BOUNDARY_REGION_FIELD`,
  default `region`). Do not assume `HEADER_REGION` is always present.

## Related

- [README: Celery integration and Context](../../README.md) for the full
  settings reference and `TenantContext` API.
