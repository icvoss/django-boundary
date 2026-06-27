# Scope a service or task that already holds the tenant

## Goal

Run a service function or background task under a tenant you already have in
hand (passed as an argument), so tenant-scoped queries inside it auto-filter,
without hand-writing `with TenantContext.using(...)` everywhere — and without
reaching for a bespoke manager.

## Prerequisites

- django-boundary installed and configured with `BOUNDARY_TENANT_MODEL`.
- A function that receives a tenant **instance** as one of its arguments.

## When to use this

This is the blessed idiom for the "I hold the tenant explicitly" case:

- a service function called from a management command or another service, where
  no `TenantMiddleware` ran to establish context;
- a Celery task that receives the tenant directly (rather than through
  boundary's Celery **headers** — see
  [Run Celery tasks with tenant context](run-celery-tasks-with-tenant-context.md)
  for that mechanism);
- any code where you were tempted to write a manager method like
  `Model.objects.for_tenant(t)` "so it works without a request context."

If you catch yourself re-implementing tenant filtering inline, use this instead.

## Steps

1. Decorate the function, naming the argument that holds the tenant:

   ```python
   from boundary.context import tenant_scoped

   @tenant_scoped("merchant")
   def run_audit(merchant, since):
       # merchant is active in context for the whole call
       return AccountAudit.objects.filter(created__gte=since)  # auto-scoped
   ```

2. If your project renamed the tenant (via `BOUNDARY_TENANT_FK_FIELD`), the
   default argument name matches it, so you can omit the name:

   ```python
   # BOUNDARY_TENANT_FK_FIELD = "merchant"
   @tenant_scoped()                 # resolves the "merchant" argument
   def run_audit(merchant, since):
       ...
   ```

3. With a Celery task that is handed the tenant directly, place `@tenant_scoped`
   below `@shared_task`:

   ```python
   @shared_task
   @tenant_scoped("merchant")
   def rebuild_index(merchant):
       ...
   ```

## Verify it worked

```python
@tenant_scoped("merchant")
def count(merchant):
    return Product.objects.count()

assert count(merchant_a) == count_for_a
assert count(merchant_b) == count_for_b
# context is restored afterwards
assert TenantContext.get() is None
```

## Common pitfalls

- **Pass a tenant instance, not a pk.** The argument is handed straight to
  `TenantContext.using()`, which expects the tenant object. If your task only
  receives an id, load the instance first (in a thin wrapper or the body) rather
  than decorating with the id argument.
- **The named argument must exist.** If the function has no argument by that
  name, the decorator raises `TypeError` at call time — fail fast rather than
  silently running with no tenant.
- **Nested scopes restore correctly.** Calling a `@tenant_scoped` function from
  inside another tenant's `TenantContext.using()` block restores the outer
  tenant on return, so it is safe to compose.
- **This is not the Celery-header mechanism.** `@tenant_scoped` scopes by an
  argument you pass explicitly. To propagate the *dispatching* request's tenant
  to a worker automatically, use `TenantTask` / `@tenant_task` instead.

## Related

- [Run Celery tasks with tenant context](run-celery-tasks-with-tenant-context.md)
  — propagate context through task headers instead of an argument.
- [Cross-tenant admin operations](cross-tenant-admin-operations.md)
