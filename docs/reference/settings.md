# Settings Reference

django-boundary is configured entirely through Django's `settings.py`. All settings use the `BOUNDARY_` prefix and are read lazily at access time, so they can be overridden in test suites without restart. The only required setting is `BOUNDARY_TENANT_MODEL`; everything else has a default.

> **Required.** `BOUNDARY_TENANT_MODEL` must be set before the application starts. The system check `boundary.E001` raises at startup if it is missing or refers to an uninstalled model.

---

## Core

### `BOUNDARY_TENANT_MODEL`

| | |
|---|---|
| **Type** | `str` |
| **Default** | No default -- required |

Dotted `app_label.ModelName` path to the concrete model that represents a tenant. Resolved lazily via `django.apps.apps.get_model()`, equivalent to how `AUTH_USER_MODEL` works.

**When to change it:** Set this once, on project initialisation, to point at whichever model plays the tenant role in your domain -- `"tenants.Organisation"`, `"sellers.Merchant"`, `"accounts.Workspace"`, and so on.

**System check:** `boundary.E001` (Error) fires if this is absent or the model cannot be found.

---

### `BOUNDARY_TENANT_FK_FIELD`

| | |
|---|---|
| **Type** | `str` |
| **Default** | `"tenant"` |

The default FK field name used by `make_tenant_mixin()` when no explicit name is passed. Has no direct effect on `TenantMixin` itself (which always uses `"tenant"`), but changing this setting means a plain `make_tenant_mixin()` call without arguments will use the new name.

**When to change it:** When your domain language is not "tenant" -- for example, `"merchant"`, `"organisation"`, or `"workspace"`. Setting this once propagates the name through `BOUNDARY_TENANT_LABEL` and `BOUNDARY_REQUEST_ATTR` automatically (see below).

**Interactions:** `BOUNDARY_TENANT_LABEL` defaults to this value. `BOUNDARY_REQUEST_ATTR` defaults to this value. Changing only this setting is usually sufficient to rename the concept across the whole package.

---

## Terminology

### `BOUNDARY_TENANT_LABEL`

| | |
|---|---|
| **Type** | `str` |
| **Default** | `BOUNDARY_TENANT_FK_FIELD` (evaluated at access time) |

Human-readable term used in error messages, FK `verbose_name` values, and middleware HTTP response bodies. Defaults to whatever `BOUNDARY_TENANT_FK_FIELD` is set to, so a project that sets `BOUNDARY_TENANT_FK_FIELD = "merchant"` automatically gets `"merchant"` in all user-facing strings without a second setting.

**When to change it independently:** When the FK column name and the UI copy should differ. For example, the column might be `"seller_account"` but you want error messages to say `"shop"`.

```python
BOUNDARY_TENANT_FK_FIELD = "seller_account"  # FK column name
BOUNDARY_TENANT_LABEL = "shop"               # used in "No shop is active in context."
```

---

### `BOUNDARY_REQUEST_ATTR`

| | |
|---|---|
| **Type** | `str` |
| **Default** | `BOUNDARY_TENANT_FK_FIELD` (evaluated at access time) |

The additional attribute set on the request object by `TenantMiddleware`. `request.tenant` is always set for backwards compatibility; when this setting differs from `"tenant"`, the same value is also assigned to `request.<REQUEST_ATTR>`, so views can use `request.merchant` or `request.workspace` instead.

**When to change it:** When you want views to read `request.merchant` rather than `request.tenant`, without breaking any code that still uses `request.tenant`. Setting `BOUNDARY_TENANT_FK_FIELD = "merchant"` is usually enough, since this defaults to that value.

**Trade-off:** If you set this to `"tenant"`, no second attribute is added (no duplication). Any other value means both `request.tenant` and `request.<value>` are present and point to the same object.

---

## Safety

### `BOUNDARY_STRICT_MODE`

| | |
|---|---|
| **Type** | `bool` |
| **Default** | `True` |

When `True`, any queryset evaluated against a `TenantModel` without an active tenant context raises `TenantNotSetError`. This is the primary development-time safety net -- it makes accidental cross-tenant data exposure a hard error rather than a silent data leak.

**When to disable it:** Rarely. The most common reason is during a migration away from a non-boundary codebase where some code paths do not yet carry tenant context. Disable temporarily, fix the gaps, then re-enable.

**Trade-off:** Disabling strict mode means unscoped queries return all rows filtered only by the ORM -- if the ORM layer is the only enforcement in place (i.e. RLS is not enabled), this risks data leakage between tenants.

**System check:** `boundary.W001` (Warning) fires if this is `False`.

---

### `BOUNDARY_REQUIRED`

| | |
|---|---|
| **Type** | `bool` |
| **Default** | `True` |

When `True`, `TenantMiddleware` returns a 404 response if no configured resolver can identify a tenant for the request. When `False`, requests that do not match any resolver proceed without a tenant context set.

**When to change it:** Set to `False` for applications that have a mix of tenant-scoped and public (unauthenticated or platform-wide) URLs -- for example, a marketing landing page or a health-check endpoint that must respond without a tenant.

**Trade-off:** Setting this to `False` means views must be written defensively -- any view that assumes a tenant context is active will fail silently or raise `TenantNotSetError` (if strict mode is on) when called from a context-free request.

---

## Resolution

### `BOUNDARY_RESOLVERS`

| | |
|---|---|
| **Type** | `list[str]` |
| **Default** | `["boundary.resolvers.SubdomainResolver"]` |

Ordered list of dotted-path resolver class names. `TenantMiddleware` tries each resolver in order and uses the first non-`None` result. The built-in resolvers are:

| Class | Resolves from | Configured by |
|---|---|---|
| `boundary.resolvers.SubdomainResolver` | Subdomain slug (e.g. `acme.app.com`) | `BOUNDARY_SUBDOMAIN_FIELD` |
| `boundary.resolvers.HeaderResolver` | HTTP header value | `BOUNDARY_HEADER_NAME` |
| `boundary.resolvers.JWTClaimResolver` | JWT payload claim | `BOUNDARY_JWT_CLAIM` |
| `boundary.resolvers.SessionResolver` | Django session key | `BOUNDARY_SESSION_KEY` |
| `boundary.resolvers.ExplicitResolver` | `request.boundary_tenant` set upstream | none |

**When to change it:** Change the list to match your URL and auth strategy. For public-facing SaaS, `SubdomainResolver` first is the right choice. For internal APIs backed by a JWT auth middleware, `JWTClaimResolver` alone is usually sufficient.

**Security note:** Resolver order determines precedence. Placing `HeaderResolver` first allows any HTTP client to set the tenant by sending a header. For public-facing applications keep `HeaderResolver` last or omit it.

**System check:** `boundary.E003` (Error) fires for any class path in this list that cannot be imported.

---

### `BOUNDARY_SUBDOMAIN_FIELD`

| | |
|---|---|
| **Type** | `str` |
| **Default** | `"slug"` |

The field on the tenant model that `SubdomainResolver` uses for the lookup. The subdomain extracted from the hostname is matched against this field.

**When to change it:** If your tenant model uses a field other than `slug` to identify tenants by subdomain -- for example, `"domain"` or `"short_code"`.

**Interactions:** Only used by `SubdomainResolver`. Has no effect if that resolver is not in `BOUNDARY_RESOLVERS`.

---

### `BOUNDARY_HEADER_NAME`

| | |
|---|---|
| **Type** | `str` |
| **Default** | `"X-Tenant-ID"` |

The HTTP header that `HeaderResolver` reads. The header value is interpreted as a UUID first; if that fails, it falls back to a slug lookup.

**When to change it:** When your API gateway or proxy injects the tenant identity under a different header name -- for example, `"X-Organisation-ID"` or `"X-Workspace"`.

**Interactions:** Only used by `HeaderResolver`.

---

### `BOUNDARY_JWT_CLAIM`

| | |
|---|---|
| **Type** | `str` |
| **Default** | `"tenant_id"` |

The claim name within the decoded JWT payload that `JWTClaimResolver` reads to identify the tenant. Boundary reads this claim only -- it does not validate JWT signatures. Signature validation is the responsibility of your auth middleware.

**When to change it:** When your identity provider uses a non-standard claim name -- for example, `"org_id"`, `"account_uuid"`, or `"tid"`.

**Interactions:** Only used by `JWTClaimResolver`.

---

### `BOUNDARY_SESSION_KEY`

| | |
|---|---|
| **Type** | `str` |
| **Default** | `"boundary_tenant_id"` |

The Django session key that `SessionResolver` reads to find the tenant identifier. Useful for internal tools where a user selects their active tenant from a dropdown and that selection is stored in the session.

**When to change it:** If you already have a session key storing the tenant identity under a different name and want to reuse it rather than duplicate the value.

**Interactions:** Only used by `SessionResolver`. Requires Django's session middleware to be active.

---

### `BOUNDARY_RESOLVER_CACHE_SIZE`

| | |
|---|---|
| **Type** | `int` |
| **Default** | `1000` |

Maximum number of entries in the process-local LRU cache used by resolvers that perform database lookups. When the cache is full, the least-recently-used entry is evicted.

**When to change it:** Increase for deployments with many active tenants and a desire to reduce database round-trips on every request. Decrease to reduce memory footprint on constrained instances.

**Trade-off:** The cache is process-local, so multi-process deployments (gunicorn workers) each maintain their own cache independently. Cache entries are invalidated by TTL and by Django signals on tenant save/delete.

**Interactions:** Works alongside `BOUNDARY_RESOLVER_CACHE_TTL`.

---

### `BOUNDARY_RESOLVER_CACHE_TTL`

| | |
|---|---|
| **Type** | `int` |
| **Default** | `60` |

Time-to-live in seconds for resolver cache entries. After this period, the next request triggers a fresh database lookup for that tenant key.

**When to change it:** Lower for deployments where tenant metadata (slug, active status) changes frequently and you need near-real-time invalidation beyond signal-based eviction. Raise to reduce DB load in stable, high-traffic deployments.

**Trade-off:** A long TTL means a tenant deactivated in the database will continue to resolve successfully until the cache entry expires or the tenant is saved (which triggers signal-based eviction). Signal-based eviction fires within the same process only.

---

## Database and RLS

### `BOUNDARY_DB_SESSION_VAR`

| | |
|---|---|
| **Type** | `str` |
| **Default** | `"app.current_tenant_id"` |

The PostgreSQL session-level variable name set by `TenantContext` via `SET LOCAL`. This variable is read by the RLS policy generated by `CreateTenantPolicy` to enforce row-level isolation at the database layer.

**When to change it:** If `app.current_tenant_id` conflicts with another library or in-house convention. The new name must match whatever name your RLS policies reference -- if you change this after generating migrations, regenerate the RLS policies.

**Interactions:** Directly tied to the RLS policy function. Changing this without updating existing RLS policies will break database-level enforcement. The admin bypass flag is stored separately under `BOUNDARY_ADMIN_FLAG_VAR`.

---

### `BOUNDARY_ADMIN_FLAG_VAR`

| | |
|---|---|
| **Type** | `str` |
| **Default** | `"app.boundary_admin"` |

The PostgreSQL session-level variable used by the admin bypass RLS policy. When this variable is set to `"true"` in a database session, the RLS bypass policy grants full table access -- used by management commands such as `boundary_deprovision` and `boundary_run_all`.

**When to change it:** Only if `app.boundary_admin` conflicts with another variable in use. Keep this setting consistent with the RLS policy definition; changing it without regenerating policies will silently break management command access.

**Interactions:** Paired with `BOUNDARY_DB_SESSION_VAR`. Both are set within the same `SET LOCAL` block.

---

### `BOUNDARY_FUNCTION_LEAKPROOF`

| | |
|---|---|
| **Type** | `bool` |
| **Default** | `False` |

Whether `CreateTenantPolicy` declares the `boundary_current_tenant_id()` helper function `LEAKPROOF`. `LEAKPROOF` is a query-planner optimisation: it lets the planner push the RLS qualifier down to index scans instead of ordering it conservatively. It is not required for isolation correctness -- the policy predicate enforces tenant isolation identically with or without it.

**Why it defaults to off:** PostgreSQL only lets a superuser create or replace a `LEAKPROOF` function. Managed Postgres providers (DigitalOcean, AWS RDS, GCP Cloud SQL, Azure, Heroku, Supabase) grant no superuser role, so a hardcoded `LEAKPROOF` aborts the migration with `only superuser can define a leakproof function`. Defaulting off keeps the RLS migrations runnable on managed hosting out of the box.

**When to change it:** Set to `True` on a self-managed PostgreSQL cluster where the role running migrations is a superuser, to regain the planner optimisation. If set to `True` on managed Postgres, `CreateTenantPolicy` (and any reverse of `DropTenantPolicy`) will fail.

**Interactions:** Only affects the `boundary_current_tenant_id()` function generated by `CreateTenantPolicy`. Changing it after policies exist requires re-running `CreateTenantPolicy` (the function is created with `CREATE OR REPLACE`) for the change to take effect.

---

### `BOUNDARY_WRAP_ATOMIC`

| | |
|---|---|
| **Type** | `bool` |
| **Default** | `True` |

When `True`, `TenantMiddleware` wraps each request in `transaction.atomic()`, and `TenantContext.using()` (plus the Celery worker-side restoration in `TenantTask`/`tenant_task`) does the same for its own body whenever it is entered outside an ambient transaction. This ensures that `SET LOCAL` session variables (which are transaction-scoped in PostgreSQL) remain active for as long as the tenant is meant to be active, and are automatically cleared on transaction close. It is a no-op wherever a transaction is already active (inside a request, inside another `using()` block), so it never opens a redundant nested transaction.

This matters well beyond requests: management commands (`boundary_run`, `boundary_run_all`), Celery tasks, and ad hoc scripts all run in autocommit by default. Without this behaviour, `TenantContext.using()` outside a transaction silently sets a session variable that is gone before the next statement runs, and the tenant-scoped write fails deep in the database with an opaque RLS error rather than at the call site. See [issue #6](https://github.com/icvoss/django-boundary/issues/6).

**When to change it:** Set to `False` only if you manage transactions explicitly everywhere `TenantContext` is used (view level, management commands, Celery tasks) and have confirmed that your RLS session variables are still correctly scoped. This is an advanced configuration; the default is safe for virtually all cases. With it `False`, `TenantContext.using()` logs a warning if entered outside an active transaction, since the session variable will not take effect.

**Trade-off:** Disabling this means `SET LOCAL` variables may not persist as expected if a transaction boundary is crossed mid-request (or a management command/Celery task never opens one), potentially causing RLS policy violations or `TenantNotSetError` in subsequent queries.

---

## Regional

### `BOUNDARY_REGIONS`

| | |
|---|---|
| **Type** | `dict[str, dict] \| None` |
| **Default** | `None` |

A dictionary mapping region key strings to Django database configuration dictionaries (the same format as entries in `DATABASES`). When set, activates the `RegionalRouter`, which routes queries for tenant-scoped models to the database associated with the tenant's region.

Non-tenant models (Django internals, sessions, auth) always route to `default`.

**When to change it:** Set when your deployment must store tenant data in geographically distinct databases for data residency compliance (GDPR, NHS, etc.).

**Trade-off:** Regional routing adds operational complexity -- each region needs its own migrations run, its own connection pool, and its own backup strategy. Cross-region joins are not possible via the ORM.

**Interactions:** Requires `"boundary.routing.RegionalRouter"` in `DATABASE_ROUTERS`. System check `boundary.E005` (Error) fires if this is set but `RegionalRouter` is absent from `DATABASE_ROUTERS`. The region key is read from the field named by `BOUNDARY_REGION_FIELD` on the tenant instance.

---

### `BOUNDARY_REGION_FIELD`

| | |
|---|---|
| **Type** | `str` |
| **Default** | `"region"` |

The field on the tenant model that stores the region key. The value must match a key in `BOUNDARY_REGIONS`. `AbstractTenant` includes a `region` field (`CharField(50)`, blank allowed) for this purpose.

**When to change it:** If your tenant model stores the region under a different field name.

**Interactions:** Only meaningful when `BOUNDARY_REGIONS` is set. If the tenant's field value does not match any key in `BOUNDARY_REGIONS`, the router falls back to `default`.

---

## Caching

See `BOUNDARY_RESOLVER_CACHE_SIZE` and `BOUNDARY_RESOLVER_CACHE_TTL` in the [Resolution](#resolution) section above.

---

## Lifecycle Hooks

### `BOUNDARY_POST_PROVISION_HOOK`

| | |
|---|---|
| **Type** | `str \| None` |
| **Default** | `None` |

Dotted-path string to a callable invoked after `boundary_provision` successfully creates a new tenant. The callable receives the newly created tenant instance as its sole argument.

**When to use it:** To trigger post-provisioning steps that do not belong in a Django signal -- for example, sending a welcome email, creating default data, or calling an external billing API.

**Format:**

```python
BOUNDARY_POST_PROVISION_HOOK = "myapp.provisioning.on_tenant_created"

# myapp/provisioning.py
def on_tenant_created(tenant):
    send_welcome_email(tenant.contact_email)
```

**Trade-off:** Errors raised inside the hook propagate and will abort the provision command. Wrap with `try/except` if the hook should be non-fatal.

---

### `BOUNDARY_PRE_DEPROVISION_HOOK`

| | |
|---|---|
| **Type** | `str \| None` |
| **Default** | `None` |

Dotted-path string to a callable invoked before `boundary_deprovision` deletes a tenant. The callable receives the tenant instance about to be deleted. Raising an exception from this hook aborts the deprovision operation.

**When to use it:** To run pre-deletion checks or cleanup that must complete before data is destroyed -- for example, cancelling active subscriptions, notifying users, or finalising billing.

**Format:**

```python
BOUNDARY_PRE_DEPROVISION_HOOK = "myapp.provisioning.before_tenant_deleted"

# myapp/provisioning.py
def before_tenant_deleted(tenant):
    cancel_subscription(tenant.stripe_subscription_id)
```

**Trade-off:** Because this hook runs before the NDJSON export and deletion, a hook failure with `--yes` specified will abort entirely with no data loss. Use `--dry-run` to verify the hook would succeed before running destructively.

---

## Quick Reference

| Setting | Default | Required |
|---|---|---|
| `BOUNDARY_TENANT_MODEL` | -- | Yes |
| `BOUNDARY_TENANT_FK_FIELD` | `"tenant"` | No |
| `BOUNDARY_TENANT_LABEL` | `BOUNDARY_TENANT_FK_FIELD` | No |
| `BOUNDARY_REQUEST_ATTR` | `BOUNDARY_TENANT_FK_FIELD` | No |
| `BOUNDARY_STRICT_MODE` | `True` | No |
| `BOUNDARY_REQUIRED` | `True` | No |
| `BOUNDARY_RESOLVERS` | `["boundary.resolvers.SubdomainResolver"]` | No |
| `BOUNDARY_SUBDOMAIN_FIELD` | `"slug"` | No |
| `BOUNDARY_HEADER_NAME` | `"X-Tenant-ID"` | No |
| `BOUNDARY_JWT_CLAIM` | `"tenant_id"` | No |
| `BOUNDARY_SESSION_KEY` | `"boundary_tenant_id"` | No |
| `BOUNDARY_RESOLVER_CACHE_SIZE` | `1000` | No |
| `BOUNDARY_RESOLVER_CACHE_TTL` | `60` | No |
| `BOUNDARY_DB_SESSION_VAR` | `"app.current_tenant_id"` | No |
| `BOUNDARY_ADMIN_FLAG_VAR` | `"app.boundary_admin"` | No |
| `BOUNDARY_FUNCTION_LEAKPROOF` | `False` | No |
| `BOUNDARY_WRAP_ATOMIC` | `True` | No |
| `BOUNDARY_REGIONS` | `None` | No |
| `BOUNDARY_REGION_FIELD` | `"region"` | No |
| `BOUNDARY_POST_PROVISION_HOOK` | `None` | No |
| `BOUNDARY_PRE_DEPROVISION_HOOK` | `None` | No |
