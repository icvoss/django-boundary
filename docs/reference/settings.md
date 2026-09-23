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

**System checks:** `boundary.W001` (Warning) fires if this is `False`, at any `DEBUG` value. `boundary.E008` (Error) additionally fires if this is `False` while `settings.DEBUG` is `False`, so the pairing is a warning in development and a hard error in production. **`DEBUG` is `False` under the test runner too**, so a test settings module that disables this setting fails at test-database creation; see the **Posture** section at the end of this file.

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

| Class | Resolves from | Configured by | Client-controlled? |
|---|---|---|---|
| `boundary.resolvers.SubdomainResolver` | Subdomain slug (e.g. `acme.app.com`) | `BOUNDARY_SUBDOMAIN_FIELD` | No -- derived from the `Host` header, constrained by `ALLOWED_HOSTS` |
| `boundary.resolvers.HeaderResolver` | HTTP header value | `BOUNDARY_HEADER_NAME` | **Yes** -- any caller can set the header to any value |
| `boundary.resolvers.JWTClaimResolver` | JWT payload claim | `BOUNDARY_JWT_CLAIM` | **Yes** -- the claim is read without signature verification; boundary trusts whatever the token says |
| `boundary.resolvers.SessionResolver` | Django session key | `BOUNDARY_SESSION_KEY` | No, provided only server-side code writes the session key -- see the note below |
| `boundary.resolvers.ExplicitResolver` | `request.boundary_tenant` set upstream | none | No -- read-only attribute access; whatever set `request.boundary_tenant` already decided |

**Resolution is not authorisation.** boundary resolves *which* tenant a request targets. It does not establish *whether the caller may access that tenant*. For `HeaderResolver` and `JWTClaimResolver` this is not a hypothetical: the value comes directly from the client, so an authenticated user of tenant A can simply ask for tenant B and get it, because every layer beneath resolution (the ORM manager, RLS, the session variable) then correctly scopes to whichever tenant was named. Isolation works; it is just pointed at the tenant the caller asked for rather than the one they belong to. The consumer is responsible for verifying that the authenticated principal is a member of the resolved tenant, after `TenantMiddleware` has set `request.tenant`. See [choose-and-order-resolvers.md](../how-to/choose-and-order-resolvers.md#enforce-membership-after-resolution) for where that check sits and a worked example, and `boundary.W006` below, which flags the specific configuration where this gap bites.

**When to change it:** Change the list to match your URL and auth strategy. For public-facing SaaS, `SubdomainResolver` first is the right choice, since the tenant then comes from something the client cannot freely choose. For internal APIs backed by a JWT auth middleware, `JWTClaimResolver` still requires a membership check downstream: it is convenient, not sufficient, for authorisation on its own.

**Security note:** Resolver order determines precedence. Placing `HeaderResolver` first allows any HTTP client to set the tenant by sending a header. For public-facing applications keep `HeaderResolver` last or omit it. This governs precedence *within* the chain; it does not substitute for the membership check above, which applies regardless of where a client-controlled resolver sits in the order.

**System check:** `boundary.E003` (Error) fires for any class path in this list that cannot be imported. `boundary.W006` (Warning) fires when a client-controlled resolver (`HeaderResolver`, `JWTClaimResolver`, or a subclass of either) is configured alongside `django.contrib.auth`. `boundary.W008` (Warning) fires when `SubdomainResolver` (or a subclass) is configured without `BOUNDARY_SUBDOMAIN_PARENT_DOMAIN`.

**A note on `SessionResolver`:** the session key itself lives server-side and cannot be forged by tampering with a cookie the way a header can be, but that only holds if nothing in your application lets an authenticated user set `request.session[BOUNDARY_SESSION_KEY]` to an arbitrary value without a membership check of its own (a "switch tenant" view that trusts a client-supplied tenant id, for example). Treat the *code that writes the session key* as the trust boundary to audit, not the resolver.

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

### `BOUNDARY_SUBDOMAIN_PARENT_DOMAIN`

| | |
|---|---|
| **Type** | `str \| list[str] \| None` |
| **Default** | `None` |

Constrains `SubdomainResolver` to hosts that are exactly `<one-label>.<parent-domain>` for one of the configured parent domains. Accepts a single domain string, a list of domain strings (for a deployment that legitimately serves several parent domains, for example `app.example.com` and `app.example.co.uk`), or `None`. Matching is case-insensitive and by label boundary, not substring: `example.com` matches `club-a.example.com` but not `evilexample.com`, `evil-example.com`, or `club-a.staging.example.co.uk` (a different depth). A trailing dot on the incoming host (`club-a.example.com.`) is stripped before matching, since it denotes the same fully-qualified hostname.

**When to change it:** Set this whenever `SubdomainResolver` is configured and the deployment's `ALLOWED_HOSTS` is not a small, fully-trusted, closed list -- in particular whenever any customer-owned custom domain is served from the same resolver chain as platform subdomains. Without it, `SubdomainResolver` takes the first label of *any* host with three or more labels and looks it up as a tenant slug, so a foreign host whose first label happens to collide with a tenant slug (`shop.example.co.uk` on a deployment that never intended to serve that host) resolves the wrong tenant. That is cross-tenant serving.

**Default is unset for backwards compatibility.** Leaving it unset preserves the exact pre-existing `SubdomainResolver` behaviour (any three-plus-label host resolves its first label). This is a deliberate compatibility default, not a recommendation: a deployment that serves any host it does not fully control should set this.

**Interactions:** Only used by `SubdomainResolver` (and its subclasses). Has no effect if that resolver is not in `BOUNDARY_RESOLVERS`.

**System check:** `boundary.W008` (Warning) fires when `SubdomainResolver` (or a subclass) is configured in `BOUNDARY_RESOLVERS` and this setting is unset.

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

### `BOUNDARY_SET_DB_SESSION_VAR`

| | |
|---|---|
| **Type** | `bool` |
| **Default** | `True` |

Whether to write the PostgreSQL session variable named by `BOUNDARY_DB_SESSION_VAR` on every tenant-context entry and exit. Every RLS policy boundary generates reads that variable, so this is what makes the database layer work at all.

**When to change it:** Only for a deployment using boundary for ORM-layer scoping alone, with no RLS policies enabled anywhere, where skipping the `set_config()` round trip per context entry is worth the loss. If RLS is enabled on any tenant table, this must stay `True`.

**Trade-off:** With this `False` and RLS live, the policies still exist and still run, but every one of them evaluates against an empty tenant, so the table is not isolated by the database at all. A mixin-scoped table still has its `TenantManager` filtering rows; an adopted table (see `BOUNDARY_TENANT_APPS`) has no ORM layer beneath it and is left with no isolation whatsoever.

**System checks:** `boundary.W009` (Warning) fires when this is `False` and a tenant table has RLS enabled and forced. `boundary.E008` (Error) fires when this is `False` while `settings.DEBUG` is `False`, regardless of any table's RLS state, including under the test runner; see the **Posture** section at the end of this file.

---

### `BOUNDARY_TENANT_APPS`

| | |
|---|---|
| **Type** | `list[str]` |
| **Default** | `[]` |

App labels whose concrete models are adopted into tenancy at the database layer: each table gains a `tenant_id` column that no Django field models, plus Row Level Security and the same two policies a mixin-scoped table gets. Entries are app labels as `apps.get_app_config()` resolves them (`"account"`, not `"allauth.account"`), never dotted module paths. An empty list means adoption is not in use.

The adopted set for a listed app is derived, never hand-listed: every concrete, non-abstract, non-proxy model the app config returns, auto-created many-to-many through models included, minus `BOUNDARY_ADOPT_EXCLUDE`, minus models already scoped by a boundary mixin or path mixin, minus the tenant model and its own through tables.

**When to change it:** When a third-party app ships concrete models you need tenant-scoped and offers no swappable abstract base to compose a mixin onto. Listing your own app is also supported, and is how a model in it that forgot its mixin gets reported by `boundary.E007`.

**This setting alone changes nothing.** It declares intent and drives the check; the DDL is applied by the `AdoptTenantApp` migration operation from a migration in your own app. See [Adopt a third-party app into your tenancy](../how-to/adopt-a-third-party-app.md).

**Refused apps and models:** `contenttypes`, `sessions`, `sites`, `auth.Permission`, `admin.LogEntry`, the `django_migrations` table, the model named by `BOUNDARY_TENANT_MODEL` (or its `ICV_TENANT_MODEL` fallback), and that model's auto-created through tables. These are global by construction. The deny-list covers the tenant model, not the app that owns it: listing that app is supported, and the tenant model is skipped from the derived set the way an excluded model is.

**Interactions:** Read by `AdoptTenantApp`, `boundary.E007`, `boundary.E006`, `boundary.W009`, `boundary_deprovision`, and `assert_rls_enforced()`. An adopted table whose app is missing from this setting is invisible to deprovision, so its rows survive the tenant.

**System check:** `boundary.E007` (Error) fires for an expected-adopted table missing its column or the expected database state, and for an app label here that is deny-listed or not installed.

---

### `BOUNDARY_ADOPT_EXCLUDE`

| | |
|---|---|
| **Type** | `list[str]` |
| **Default** | `[]` |

`"app_label.ModelName"` strings exempted from adoption, for a genuinely global reference table inside an otherwise adopted app. Matching is exact and case-sensitive against `f"{model._meta.app_label}.{model.__name__}"`.

**When to change it:** When one model of an adopted app must stay global, or when the app owns a model boundary refuses to adopt (a unique constraint a foreign key targets, a partial unique index, an expression index, a deferrable constraint) and you would rather leave that table global than rewrite the constraint by hand. Also the escape route for an app whose own data migration inserts seed rows, which cannot be adopted as it stands.

**An entry matching no model is inert, not an error.** A package upgrade may legitimately remove a model you had excluded, and that should not break your deployment.

**Interactions:** Applies to both `AdoptTenantApp` and `boundary.E007`, so an excluded model is neither adopted nor policed. The operation's own `exclude` argument is the per-migration form, unioned with this setting for that operation; use it to split one app across several operations when different tables need different `backfill_tenant` values.

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

## Posture

### `boundary.E008`: unsafe production posture

Not a setting, but the check that governs the two above. `boundary.E008` reports an **Error**, one per offending setting, when `settings.DEBUG` is `False` and either `BOUNDARY_STRICT_MODE` or `BOUNDARY_SET_DB_SESSION_VAR` is `False`.

Each of those settings alone leaves a deployment with a category of isolation it believes it has. With `BOUNDARY_STRICT_MODE = False`, a queryset run with no active tenant returns every tenant's rows instead of raising, a cross-tenant read nothing logs. With `BOUNDARY_SET_DB_SESSION_VAR = False`, nothing writes the variable every RLS policy reads. Both are legitimate deliberate choices; neither is a safe default to arrive at by inheriting a settings module, which is why `DEBUG` is the discriminator. `boundary.W001` and `boundary.W009` are unchanged, so at `DEBUG = True` a developer sees exactly what they saw before.

E008 is settings-only: it issues no query, opens no connection, and is not gated on database vendor or availability, so it reports identically on SQLite, on PostgreSQL, and with no database configured at all.

**It fires under the test runner.** Django's test runner sets `DEBUG = False` for the run, and `migrate` runs the system checks when it creates the test database, so a test settings module that disables either flag fails at test-database creation before a single test runs. For most projects this is where E008 is met first.

**The remedy is naming the choice, not reversing it.** Either restore the default, or record the deliberate choice by adding the ID to `SILENCED_SYSTEM_CHECKS` **in the settings module that disables the flag**, which for a test-only posture is the test settings module rather than the production one:

```python
# myproject/settings/test.py
BOUNDARY_STRICT_MODE = False
SILENCED_SYSTEM_CHECKS = ["boundary.E008"]
```

Silencing by ID is the documented way to record this, because it is a line in a settings module a reviewer can see and grep for, which an inherited `False` is not.

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
| `BOUNDARY_SET_DB_SESSION_VAR` | `True` | No |
| `BOUNDARY_TENANT_APPS` | `[]` | No |
| `BOUNDARY_ADOPT_EXCLUDE` | `[]` | No |
| `BOUNDARY_REGIONS` | `None` | No |
| `BOUNDARY_REGION_FIELD` | `"region"` | No |
| `BOUNDARY_POST_PROVISION_HOOK` | `None` | No |
| `BOUNDARY_PRE_DEPROVISION_HOOK` | `None` | No |
