# Troubleshooting

Symptom-first reference for django-boundary. Find your symptom, read the likely cause, apply the fix. For the exhaustive settings and check tables, see the [README](../README.md).

All boundary exceptions subclass `boundary.exceptions.BoundaryError`, so you can catch the whole family with a single `except BoundaryError`.

---

## Quick lookup

| Symptom | Likely cause | Fix |
|---------|--------------|-----|
| `TenantNotSetError` on a query | No active tenant in context and `BOUNDARY_STRICT_MODE` is on | Wrap the call in `TenantContext.using(tenant)` or run inside a request handled by `TenantMiddleware` |
| HTTP 404 "Tenant not found" | No resolver matched the request and `BOUNDARY_REQUIRED` is on | Fix the resolver input (subdomain, header, session) or set `BOUNDARY_REQUIRED = False` |
| HTTP 403 "Tenant is inactive" | Resolved tenant has `is_active = False` | Reactivate the tenant or expect this for suspended tenants |
| `TenantNotFoundError` in a Celery task | Task header references a tenant pk that no longer exists | Configure dead-letter routing; do not retry (it is excluded from `autoretry_for`) |
| `RegionNotConfiguredError` | Active tenant's region is not in `BOUNDARY_REGIONS` | Add the region to `BOUNDARY_REGIONS` or correct the tenant's region value |
| Queries return nothing | No tenant set, or wrong tenant set | Set the correct tenant in context before querying |
| Data not isolated between tenants | `BOUNDARY_STRICT_MODE = False` and/or RLS not applied | Enable strict mode and add the `EnableRLS` migration operation |
| Tenant context lost in a Celery task or thread | Context does not auto-propagate to new threads/processes | Use `TenantTask` / `@tenant_task`, or set context manually inside the worker |
| `boundary.E001` at startup | `BOUNDARY_TENANT_MODEL` missing or invalid | Set `BOUNDARY_TENANT_MODEL = "app_label.ModelName"` |
| `boundary.E003` at startup | A resolver in `BOUNDARY_RESOLVERS` cannot be imported | Fix the dotted path |
| `boundary.E004` at startup | `TenantMiddleware` not in `MIDDLEWARE` | Add `boundary.middleware.TenantMiddleware` to `MIDDLEWARE` |
| `boundary.E006` at startup | A tenant-scoped table is missing forced RLS | Run the `EnableRLS` migration operation for that model |
| `boundary.W001` at startup | `BOUNDARY_STRICT_MODE` is off | Set `BOUNDARY_STRICT_MODE = True` |

---

## Exceptions

### `TenantNotSetError`

**Triggers when:** you run a query through a `TenantManager` (the default manager on tenant-scoped models) while no tenant is active in context, and `BOUNDARY_STRICT_MODE` is `True` (the default). It is also raised by `TenantContext.require()` directly.

The message names your configured tenant label, for example "No merchant is active in context" when `BOUNDARY_TENANT_LABEL` (or `BOUNDARY_TENANT_FK_FIELD`) is `merchant`.

**Fix:** establish a tenant before the query.

```python
from boundary.context import TenantContext

with TenantContext.using(tenant):
    bookings = Booking.objects.all()  # filtered to tenant
```

Inside a normal request this is handled for you by `TenantMiddleware`. The error usually appears in management commands, shells, Celery tasks, or background threads where no middleware ran. See [Common gotchas](#common-gotchas) below.

For a service function or task that already receives the tenant as an argument, the `@tenant_scoped` decorator is tidier than a manual `with` block:

```python
from boundary.context import tenant_scoped

@tenant_scoped("merchant")
def run_audit(merchant, since):
    return AccountAudit.objects.filter(created__gte=since)  # auto-scoped
```

In a test that calls a class-based view directly (where `RequestFactory` bypasses `TenantMiddleware`), use `call_view` from `boundary.testing`, which activates the tenant for you:

```python
from boundary.testing import call_view

response = call_view(BookingListView, tenant=tenant_a)
```

> Note: a `strict_mode_violation` signal fires just before this exception is raised, so you can hook logging or metrics onto it.

### `TenantResolutionError`

**Triggers when:** a resolver raises an unexpected exception during resolution.

In the shipped `TenantMiddleware`, a failing resolver is wrapped in a `TenantResolutionError`, logged, and passed to the `_on_resolver_error(request, resolver_path, error)` hook before the chain falls through to the next resolver. The default hook is a no-op (skip and continue). Override it in a `TenantMiddleware` subclass and `raise error` if you want a failing resolver to abort resolution rather than be skipped. In custom resolution code, raise `TenantResolutionError` for unexpected failures and let `return None` mean "no match".

**Fix:** inspect the logged traceback for the failing resolver (logged under `boundary.middleware` with `resolver_name`), then fix the resolver logic or its inputs.

### `TenantInactiveError`

**Triggers when:** a resolved tenant has `is_active = False`.

In a request, `TenantMiddleware` builds a `TenantInactiveError` and hands it to the `_handle_inactive_tenant(request, tenant, exc)` hook, which by default translates it to an HTTP 403 ("<Label> is inactive.") so end users see a clean rejection. Override that hook in a subclass to customise the response (for example, redirect to a billing page) or to re-raise the exception. Use `TenantInactiveError` in your own service or resolver code when you need to signal an inactive tenant programmatically.

**Fix:** reactivate the tenant (`is_active = True`) if the rejection is wrong, or treat the 403 as expected for suspended tenants.

### `TenantNotFoundError`

**Triggers when:** a Celery task carries a tenant pk in its headers, but that tenant no longer exists in the database when the worker tries to restore context (`_restore_tenant_context`).

**Fix:** this is deliberately excluded from `autoretry_for` (`BR-CEL-003`) because retrying will never succeed. Configure dead-letter routing for affected tasks so they are parked rather than retried forever. If tenants are deleted while tasks are in flight, drain or revoke their queued tasks as part of deprovisioning (see `BOUNDARY_PRE_DEPROVISION_HOOK`).

### `RegionNotConfiguredError`

**Triggers when:** `boundary.routing.require_region()` is called and the active (or given) tenant's region is not present in `BOUNDARY_REGIONS`, no tenant is active, or `BOUNDARY_REGIONS` is unset.

**Fix:** add the region to `BOUNDARY_REGIONS`, or correct the tenant's region value. Note that `RegionalRouter` itself never raises this: a Django router must return an alias, so it falls back to the `default` database (logging at debug level). Call `require_region()` where silent fallback to `default` is unacceptable, for example at provisioning time or before a regional batch job.

**Fix:** add the missing region key to `BOUNDARY_REGIONS`, or correct the value stored on the tenant's region field (`BOUNDARY_REGION_FIELD`, default `region`).

---

## System checks

Boundary registers Django system checks that run at startup and during test collection. Resolve errors (`E0xx`) before deploying; warnings (`W0xx`) are advisory.

### `boundary.E001` — tenant model missing or invalid

**Triggers when:** `BOUNDARY_TENANT_MODEL` is unset, or its value does not refer to an installed model.

**Fix:**

```python
# settings.py
BOUNDARY_TENANT_MODEL = "accounts.Tenant"  # app_label.ModelName
```

Confirm the app is in `INSTALLED_APPS` and the `app_label.ModelName` format is exact.

### `boundary.E003` — resolver cannot be imported

**Triggers when:** a dotted path in `BOUNDARY_RESOLVERS` raises `ImportError` when imported.

**Fix:** correct the path. The default is `["boundary.resolvers.SubdomainResolver"]`. Each entry must be an importable resolver class.

### `boundary.E004` — middleware missing

**Triggers when:** `boundary.middleware.TenantMiddleware` is not in `MIDDLEWARE`.

**Fix:** add it, before `SessionMiddleware` if you use the session resolver:

```python
MIDDLEWARE = [
    "boundary.middleware.TenantMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    # ...
]
```

### `boundary.E006` — Row Level Security not enabled

**Triggers when:** running on PostgreSQL, a tenant-scoped table does not have RLS both *enabled* and *forced* (`relrowsecurity` and `relforcerowsecurity` in `pg_class`). The check recognises models using `TenantMixin`, `make_tenant_mixin()`, or any model exposing a `_boundary_fk_field` attribute. Tables that do not exist yet (pre-migration) are skipped, and the check is a no-op on non-PostgreSQL backends.

**Fix:** add the `EnableRLS` migration operation for the affected model so the database enforces isolation even for raw SQL and superuser-less connections. RLS is the defence-in-depth layer beneath ORM filtering; do not rely on the ORM alone.

### `boundary.W001` — strict mode off

**Triggers when:** `BOUNDARY_STRICT_MODE` is `False`.

With strict mode off, a `TenantManager` query with no active tenant returns the **full, unfiltered** queryset instead of raising `TenantNotSetError`. That is a silent cross-tenant leak risk.

**Fix:** set `BOUNDARY_STRICT_MODE = True` (the default) so missing context fails loudly. Only disable it deliberately, and only when RLS (`E006`) is enforcing isolation at the database layer.

---

## Common gotchas

### Queries return nothing

**Cause:** no tenant is set, or the wrong tenant is set. A `TenantManager` filters by the active tenant via `TenantContext.get()`. With strict mode off and no tenant, you instead get *everything*; with the wrong tenant, you get that tenant's rows only.

**Fix:** confirm the active tenant before querying.

```python
from boundary.context import TenantContext

assert TenantContext.get() is not None  # who is active?

with TenantContext.using(expected_tenant):
    rows = Booking.objects.all()
```

In a request, verify `TenantMiddleware` resolved a tenant (`request.tenant`, or `request.<BOUNDARY_REQUEST_ATTR>`). Outside a request, you must set context yourself.

### Data not isolated between tenants

**Cause:** isolation has two layers and one is missing.

1. **ORM filtering** depends on tenant-scoped models using `TenantManager` *and* `BOUNDARY_STRICT_MODE = True`. With strict mode off, unscoped queries leak across tenants (`W001`).
2. **Row Level Security** is the database-level guarantee. If RLS is not enabled and forced, raw SQL, `.using()` to a region, or queries through a non-tenant manager can cross boundaries (`E006`).

**Fix:** keep strict mode on, ensure scoped models use `TenantMixin` / `make_tenant_mixin()`, and apply the `EnableRLS` migration operation. Resolve any `E006` errors before trusting isolation in production.

### Regional queries all hit the default database

**Cause:** `BOUNDARY_REGIONS` is set, but `boundary.routing.RegionalRouter` is
not in `DATABASE_ROUTERS`. Without the router, tenant-scoped queries are never
routed to their region database, so everything falls through to `default`. There
is no system check for this, so it fails silently.

**Fix:**

```python
DATABASE_ROUTERS = ["boundary.routing.RegionalRouter"]
```

See [Deploy across multiple regions](./how-to/deploy-multi-region.md).

### Context lost in Celery or background threads

**Cause:** `TenantContext` is backed by a `ContextVar`. It propagates correctly across sync and async views, middleware, and management commands within the same execution context, but it does **not** automatically cross into a Celery worker process or a freshly spawned thread.

**Fix for Celery:** use the provided helpers, which serialise the tenant (and region) into task *headers* at dispatch and restore context on the worker.

```python
from boundary.celery import tenant_task, TenantTask

# Function decorator (worker side restore)
@app.task
@tenant_task
def send_confirmation(booking_id):
    booking = Booking.objects.get(id=booking_id)

# Or a base class that handles both dispatch and execution
class GenerateReport(TenantTask, app.Task):
    def run(self, report_id):
        ...
```

If the referenced tenant has been deleted by the time the task runs, you get `TenantNotFoundError` (see above).

**Fix for threads:** set context explicitly inside the new thread, since `ContextVar` values are not copied automatically across thread boundaries here.

```python
from boundary.context import TenantContext

def worker(tenant):
    with TenantContext.using(tenant):
        ...  # tenant is active in this thread

threading.Thread(target=worker, args=(tenant,)).start()
```

---

## Related

- [README](../README.md) — full settings reference and system check table.
- Configuration keys referenced here: `BOUNDARY_TENANT_MODEL`, `BOUNDARY_STRICT_MODE`, `BOUNDARY_REQUIRED`, `BOUNDARY_RESOLVERS`, `BOUNDARY_REGIONS`, `BOUNDARY_REGION_FIELD`, `BOUNDARY_TENANT_FK_FIELD`, `BOUNDARY_TENANT_LABEL`, `BOUNDARY_REQUEST_ATTR`.
