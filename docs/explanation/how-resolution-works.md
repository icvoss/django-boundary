# How tenant resolution works

This document explains what happens between a request arriving and your view running, and how the active tenant flows from `TenantMiddleware` down to every ORM query. Read it to understand the lifecycle, the caching layer, and why the design is safe under threads and async.

For exhaustive option tables, see the [README](../../README.md). For task-focused guides, see the [how-to directory](../how-to/).

## The short version

1. A request arrives at `TenantMiddleware`.
2. The middleware walks the resolver chain in order. The first resolver that returns a tenant wins.
3. The middleware validates the tenant (active check), fires the `tenant_resolved` signal, and sets `request.tenant`.
4. It calls `TenantContext.set(tenant)`. This stores the tenant in a `ContextVar` and writes the PostgreSQL session variable via `set_config()`, optionally inside `transaction.atomic()`.
5. Your view, services, and ORM queries read the active tenant from the same `ContextVar`.
6. After the response is produced, the middleware calls `TenantContext.clear(token)` in a `finally` block, restoring the previous context and resetting the session variable.

The two layers that carry the tenant are the `ContextVar` (read by the ORM in Python) and the PostgreSQL session variable (read by Row Level Security policies in the database). Both are set and torn down together.

## Step 1: the request arrives

`TenantMiddleware` is a `MiddlewareMixin` subclass, so it works under both WSGI and ASGI. It overrides `__call__` directly rather than `process_request`, because it needs to wrap the entire downstream call in a transaction and guarantee teardown in a `finally` block.

## Step 2: walking the resolver chain

The middleware reads `BOUNDARY_RESOLVERS`, a list of dotted import paths. It walks them in order. For each path it imports the resolver class, instantiates it, and calls `resolve(request)`:

```python
BOUNDARY_RESOLVERS = [
    "boundary.resolvers.HeaderResolver",
    "boundary.resolvers.SubdomainResolver",
]
```

First match wins. The first resolver that returns a non-`None` tenant ends the walk, and that resolver is recorded so it can be passed to the `tenant_resolved` signal. With the chain above, a request with an `X-Tenant-ID` header resolves via `HeaderResolver`; a request without that header but on `club-a.example.com` falls through to `SubdomainResolver`.

Resolvers are expected not to raise. If one does, the middleware catches the exception, logs a warning under the `boundary.middleware` logger, and continues to the next resolver. A single broken resolver therefore degrades to fallthrough rather than a 500.

The built-in resolvers are `SubdomainResolver`, `HeaderResolver`, `JWTClaimResolver`, `SessionResolver`, and `ExplicitResolver`. Each reads its own settings key (for example `BOUNDARY_SUBDOMAIN_FIELD`, `BOUNDARY_HEADER_NAME`, `BOUNDARY_JWT_CLAIM`, `BOUNDARY_SESSION_KEY`). See the resolver table in the [README](../../README.md#resolvers) and the ordering guide in [choose-and-order-resolvers](../how-to/choose-and-order-resolvers.md).

Ordering is a security decision: placing `HeaderResolver` first lets any HTTP client name the tenant via a header. For public-facing apps, put `SubdomainResolver` first.

## Step 3: no resolver matched

If the walk finishes with no tenant, behaviour depends on `BOUNDARY_REQUIRED` (default `True`):

- `BOUNDARY_REQUIRED = True`: the middleware fires `tenant_resolution_failed` and returns a 404. The 404 body uses the configured tenant label, so a project with `BOUNDARY_TENANT_LABEL = "merchant"` returns "Merchant not found."
- `BOUNDARY_REQUIRED = False`: the middleware calls the downstream view with no tenant set. `TenantContext.get()` returns `None` in the view. This is the mode for apps that mix public and tenant-scoped routes.

## Step 4: validating the tenant

When a tenant is resolved, the middleware checks for an `is_active` attribute. If the tenant has one and it is falsy, the middleware logs a warning and returns a 403 ("... is inactive."), again using the tenant label. This stops resolution short of setting any context, so an inactive tenant never reaches your view or the database session.

## Step 5: firing the signal and setting request attributes

On success the middleware fires `tenant_resolved` with `sender`, `tenant`, `resolver`, and `request`:

```python
from boundary.signals import tenant_resolved

def on_tenant_resolved(sender, tenant, resolver, request, **kwargs):
    # Wire your own metrics here; boundary takes no metrics dependency.
    statsd.incr(f"tenant.resolved.{resolver.__class__.__name__}")

tenant_resolved.connect(on_tenant_resolved)
```

The three signals exported from `boundary.signals` are `tenant_resolved`, `tenant_resolution_failed`, and `strict_mode_violation`. They exist purely for observability, so you can attach metrics or logging without boundary depending on any metrics library.

The middleware then sets `request.tenant`. `request.tenant` is always set for backwards compatibility. If `BOUNDARY_REQUEST_ATTR` (which defaults to `BOUNDARY_TENANT_FK_FIELD`, in turn defaulting to `"tenant"`) differs from `"tenant"`, the same value is also assigned to that attribute, so a project using merchant terminology can read `request.merchant`.

## Step 6: setting the context and the session variable

This is the heart of the lifecycle. The middleware calls `TenantContext.set(tenant)`, which does two things in order:

1. Stores the tenant in a module-level `ContextVar` and keeps the returned token.
2. Writes the PostgreSQL session variable via a parameterised `SELECT set_config(%s, %s, true)`, using `BOUNDARY_DB_SESSION_VAR` (default `app.current_tenant_id`) and `str(tenant.pk)`.

The third argument to `set_config` is `true`, which scopes the setting to the current transaction. That is why the session variable must be written inside a transaction to have any effect.

`set()` is atomic with respect to its two effects: if the `set_config()` call raises, the `ContextVar` is reset back to its previous value before the exception propagates, so you never end up with the Python context and the database disagreeing.

### Why the atomic wrapper

Because `set_config(..., true)` is transaction-scoped, the middleware always wraps the downstream call in a transaction it controls, when `BOUNDARY_WRAP_ATOMIC` is `True` (the default), using `context._ensure_atomic()`:

```python
with _ensure_atomic():
    token = TenantContext.set(tenant)
    request._boundary_token = token
    try:
        response = self.get_response(request)
    finally:
        TenantContext.clear(token)
```

`_ensure_atomic()` opens `transaction.atomic()` only if no transaction is already open on the connection (`connection.in_atomic_block` is `False`); if one is already open it is a no-op, so nested use never adds an unnecessary savepoint.

Earlier versions (0.5.3 and before) skipped this wrap whenever the default database already had `ATOMIC_REQUESTS = True`, on the assumption that Django's own ATOMIC_REQUESTS transaction would already cover `TenantContext.set()`. That assumption was wrong: Django's `ATOMIC_REQUESTS` wraps the *view* (`BaseHandler.make_view_atomic`), not the middleware chain, so at the point the middleware called `TenantContext.set()` no transaction had opened yet. The `set_config(..., true)` call was therefore transaction-local to nothing and silently discarded before the view's transaction ever began, leaving the PostgreSQL session variable empty while `TenantContext.get()` still reported the tenant correctly. Under `FORCE ROW LEVEL SECURITY` this fails closed (the policy evaluates against a NULL session variable and returns no rows), not as a data leak, but it silently disabled the RLS defence-in-depth layer for every request served with `ATOMIC_REQUESTS = True` on the default database. If you are upgrading from 0.5.3 or earlier and ran with `ATOMIC_REQUESTS = True`, your RLS policies were not seeing the tenant session variable during requests: application-level scoping (the tenant-aware manager, `TenantContext.require()`) was unaffected, but the database-level second line of defence was not applying. Upgrading resolves this with no configuration change required.

With the fix, boundary's own transaction is what makes the session variable visible; when the view's `ATOMIC_REQUESTS` transaction later opens, it nests as a savepoint inside boundary's transaction, and an unhandled exception in the view still unwinds through both, so the view's work still commits or rolls back as a unit.

If `BOUNDARY_WRAP_ATOMIC` is `False`, `_ensure_atomic()` does not open a transaction; it logs a warning if none is already open, and the session variable will not survive past the next statement unless you wrap the request in your own transaction. If your project relies on RLS, keep `BOUNDARY_WRAP_ATOMIC` enabled, or wrap requests in your own `transaction.atomic()` if you disable it.

## Step 7: the context flows to the ORM

Inside the request, every layer reads from the same `ContextVar`:

- The Python ORM layer (the tenant-scoped manager and `TenantContext.require()`) calls `TenantContext.get()` to filter querysets to the active tenant.
- PostgreSQL RLS policies read `current_setting('app.current_tenant_id', true)` and filter rows at the database level, as a second line of defence.

You can read the active tenant anywhere with `TenantContext.get()` (returns `None` if unset) or `TenantContext.require()` (raises `TenantNotSetError` if unset). To scope a block of code to a specific tenant outside the request cycle, use the context manager:

```python
from boundary.context import TenantContext

with TenantContext.using(club):
    Booking.objects.all()  # filtered to club
```

`using()` restores both the `ContextVar` and the DB session variable on exit. It restores the session variable explicitly rather than relying on transaction or savepoint rollback, so nesting works correctly even inside an outer `atomic()` block. This is what makes Celery tasks and management commands safe; see [run-celery-tasks-with-tenant-context](../how-to/run-celery-tasks-with-tenant-context.md).

## Step 8: teardown at request end

When `get_response` returns (or raises), the `finally` block calls `TenantContext.clear(token)`. This:

1. Resets the `ContextVar` to its previous value using the token from `set()`.
2. Resets the PostgreSQL session variable to an empty string via `set_config(..., '', true)`.

DB cleanup is best-effort: if the reset fails, the `ContextVar` is already restored and a warning is logged. The token model means clearing restores the previous tenant rather than blindly clearing to `None`, so nested scopes unwind correctly.

After the request, `TenantContext.get()` returns `None` again. Nothing leaks into the next request handled on the same worker.

## Caching

Resolving a tenant usually means a database lookup by slug, header value, or session id. To avoid repeating that lookup on every request, the subdomain, header, and session resolvers cache the resolved tenant in a process-local, thread-safe dictionary guarded by a lock.

Two settings control the cache:

- `BOUNDARY_RESOLVER_CACHE_SIZE` (default `1000`): the maximum number of cached entries. When the cache is full and a new key arrives, the oldest entry is evicted.
- `BOUNDARY_RESOLVER_CACHE_TTL` (default `60`, in seconds): entries older than this are treated as expired on read and removed.

Cache keys are namespaced per resolver (for example `subdomain:club-a`, `header:<value>`, `session:<id>`), so different resolvers do not collide. The `JWTClaimResolver` and `ExplicitResolver` do not cache, because their input already carries the tenant identity directly.

### Invalidation

The cache is per-process and time-bounded, so a stale entry self-heals within the TTL. When you need an immediate update, for example after renaming a tenant's slug or deactivating a tenant, invalidate explicitly:

```python
from boundary.context import TenantContext

TenantContext.invalidate_cache(tenant)
```

This removes every cache entry pointing at that tenant across all resolver namespaces. Because the cache is process-local, each worker process maintains its own copy; explicit invalidation affects only the process it runs in. Across a multi-process or multi-host deployment, rely on the TTL for eventual consistency, or trigger invalidation on every worker.

## The contextvars model: thread and async safety

Boundary stores the active tenant in a `contextvars.ContextVar`, not in thread-local storage or a global. This choice is what makes the package safe under both threaded and async Django:

- Each thread sees its own `ContextVar` value, so two requests handled by different worker threads never see each other's tenant.
- Under async, `ContextVar` values follow the logical task. An `await` does not leak the tenant into an unrelated coroutine, and each request's context is isolated even when many coroutines run on one event loop.
- The same `ContextVar` propagates into Celery tasks and management commands when you set it explicitly, which is why `TenantContext.using()` works identically everywhere.

The `set()` / `clear()` token pattern comes straight from the `contextvars` API: `set()` returns a token, and `clear()` (which calls `ContextVar.reset(token)`) restores exactly the value that was present before the matching `set()`. This is what lets nested scopes unwind in the correct order.

## Verify your understanding

A quick mental model check against the real behaviour:

- A request to `example.com` (no subdomain) with `BOUNDARY_REQUIRED = True` and only `SubdomainResolver` configured returns a 404, and `TenantContext.get()` is never set.
- The same request with `BOUNDARY_REQUIRED = False` returns 200, and the view sees `TenantContext.get() is None`.
- After any request completes, `TenantContext.get()` is `None`, because teardown ran in the `finally` block.

These are exactly the cases exercised in `tests/test_middleware.py` and `tests/test_context.py`.

## Related

- [Choose and order resolvers](../how-to/choose-and-order-resolvers.md)
- [Run Celery tasks with tenant context](../how-to/run-celery-tasks-with-tenant-context.md)
- [Add RLS policies with migrations](../how-to/add-rls-policies-with-migrations.md)
- [Cross-tenant admin operations](../how-to/cross-tenant-admin-operations.md)
- [README: Resolvers, Context, and How It Works](../../README.md#how-it-works)
