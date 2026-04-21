# Changelog

All notable changes to django-boundary are documented here.

## [0.2.0] - 2026-04-21

### Added

- **Configurable tenant FK field name** — `BOUNDARY_TENANT_FK_FIELD` setting
  (default `"tenant"`) controls the FK field name on `TenantMixin`. Consumers
  who want domain-native names like `merchant` can set this globally.
- **`make_tenant_mixin(fk_field)` factory** — creates a custom `TenantMixin`
  with any FK field name, wired up with `TenantManager`, `UnscopedManager`,
  and auto-populate on `save()`. This is the public extension API for
  consumers who need full control without reimplementing package internals.
- **`is_tenant_model(model)`** — registry-backed check that recognises models
  using `TenantMixin`, `make_tenant_mixin()`, or any class with
  `_boundary_fk_field`. Replaces `issubclass(model, TenantMixin)` checks.
- **`get_tenant_fk_field(model)`** — returns the FK field name for a
  registered tenant-scoped model.

### Changed

- System check `boundary.E006` now uses `is_tenant_model()` instead of
  `issubclass(model, TenantMixin)`, so custom tenant base classes created
  via `make_tenant_mixin()` are verified by RLS checks.
- `RegionalRouter` uses `is_tenant_model()` for routing decisions, supporting
  custom FK field names.
- `TenantManager` reads the FK field name from `model._boundary_fk_field`
  rather than hardcoding `tenant`, so filtering, `bulk_create()`, and
  `bulk_update()` all work with custom field names.
- `CreateTenantPolicy` and `DropTenantPolicy` migration ops accept
  `tenant_column=None` and derive the default from the model when possible.

## [0.1.0] - 2026-03-27

Initial release — all four implementation phases.

### Added

#### Context Layer
- `TenantContext` with `set()`, `get()`, `clear()`, `require()`, `using()`
- Async-safe via `contextvars.ContextVar`
- PostgreSQL session variable via parameterised `set_config()` (SQL injection safe)
- Atomic ContextVar + DB session updates (rolled back on failure)
- Savepoint-safe nesting (`using()` explicitly restores DB session variable)

#### ORM Layer
- `AbstractTenant` — convenience base with name, slug, region, is_active, timestamps
- `TenantMixin` / `TenantModel` — adds tenant FK, auto-filtering manager, unscoped escape hatch
- `TenantManager` — auto-filters every queryset by active tenant
- `STRICT_MODE` (default: True) — raises `TenantNotSetError` on unscoped queries
- Auto-populate `tenant` from context on `save()`
- `bulk_create()` auto-populates tenant; `bulk_update()` validates tenant ownership
- `unscoped` manager bypasses filtering for cross-tenant operations

#### Resolution Layer
- `TenantMiddleware` — WSGI/ASGI compatible via `MiddlewareMixin`
- 5 built-in resolvers: Subdomain, Header (UUID-first + slug fallback), JWT (no signature validation), Session, Explicit
- Pluggable resolver interface (`BaseResolver`)
- Thread-safe LRU cache with signal-based invalidation and configurable TTL
- Transaction wrapping for `set_config()` (respects `ATOMIC_REQUESTS`)

#### RLS Layer
- `EnableRLS` migration operation — enables and forces RLS on tables
- `CreateTenantPolicy` — generates LEAKPROOF `boundary_current_tenant_id()` function, isolation policy with `WITH CHECK` (INSERT enforcement), admin bypass policy
- `DropTenantPolicy` — reversible policy removal
- Type-aware: detects UUID vs integer tenant PKs
- System check `boundary.E006` — verifies RLS is enabled at startup via `pg_class`

#### Celery Integration
- `tenant_task` decorator — restores tenant context from task headers on worker
- `TenantTask` base class — injects headers at dispatch, restores on execution
- Tenant UUID and region serialised into headers (not kwargs)
- `TenantNotFoundError` is non-retriable

#### Regional Routing
- `RegionalRouter` — routes tenant-scoped queries to regional database aliases
- `all_regions()` — context manager yielding all configured region aliases
- `specific_region(key)` — pins queries to a named region
- Non-tenant models always route to `default`
- No silent fallback on unreachable regional DB

#### Management Commands
- `boundary_provision` — create tenant with hooks and extra fields
- `boundary_deprovision` — delete tenant with NDJSON export, dry-run, hooks
- `boundary_run` — execute any command scoped to a single tenant
- `boundary_run_all` — run against all tenants with `--parallel`, `--region`, `--exclude`, `--json`

#### Test Utilities
- `set_tenant()` — context manager for tests
- `tenant_factory()` — creates tenants with unique slugs
- `TenantTestMixin` — TestCase mixin with auto-created `self.tenant`

#### System Checks
- `boundary.E001` — BOUNDARY_TENANT_MODEL validation
- `boundary.E003` — resolver class import validation
- `boundary.E004` — TenantMiddleware in MIDDLEWARE
- `boundary.E005` — BOUNDARY_REGIONS requires DATABASE_ROUTERS
- `boundary.E006` — RLS enabled on TenantModel tables
- `boundary.W001` — STRICT_MODE disabled warning

#### Signals
- `tenant_resolved` — fired after successful tenant resolution
- `tenant_resolution_failed` — fired when no resolver matches
- `strict_mode_violation` — fired before TenantNotSetError is raised
