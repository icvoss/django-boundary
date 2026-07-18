# Changelog

All notable changes to django-boundary are documented here.

## [Unreleased]

## [0.5.0] - 2026-07-18

### Added

- **New setting `BOUNDARY_FUNCTION_LEAKPROOF`** (default `False`) controls
  whether `CreateTenantPolicy` declares the `boundary_current_tenant_id()`
  helper function `LEAKPROOF`.

### Fixed

- **`CreateTenantPolicy` no longer aborts on managed PostgreSQL.** The helper
  function was declared `LEAKPROOF` unconditionally, but PostgreSQL only lets a
  superuser create a `LEAKPROOF` function, and managed providers (DigitalOcean,
  AWS RDS, GCP Cloud SQL, Azure, Heroku, Supabase) grant no superuser role. The
  migration failed with `only superuser can define a leakproof function`,
  making RLS unusable on the most common Django production hosting. `LEAKPROOF`
  is now off by default and opt-in via `BOUNDARY_FUNCTION_LEAKPROOF`. It is a
  query-planner optimisation only: tenant isolation is enforced identically with
  or without it (the policy predicate is unchanged), so the default costs no
  security. Superuser deployments on a self-managed cluster set
  `BOUNDARY_FUNCTION_LEAKPROOF = True` to regain the optimisation.

## [0.4.1] - 2026-07-12

### Added

- **New system check `boundary.W002`** warns when both
  `boundary.middleware.TenantMiddleware` and icv-identity's
  `TenantContextMiddleware` are present in `MIDDLEWARE`, since icv-identity owns
  tenant resolution and bridges into boundary when it is installed, and running
  both double-resolves the tenant per request (ADR-025 T1).
- **`src/boundary/py.typed`** marker, so consumers running `mypy` with the
  `django-stubs` plugin get boundary's own types instead of falling back to
  untyped `Any`. Shipped only once `TenantMixin` / `TenantModel` were confirmed
  to resolve cleanly (see Fixed, below); a `py.typed` package with unresolved
  managers is strictly worse for downstream consumers than an untyped one.

### Changed

- **`BOUNDARY_TENANT_MODEL` now falls back to `ICV_TENANT_MODEL`** when unset,
  matching how icv-identity and icv-payments already resolve the tenant model.
  `ICV_TENANT_MODEL` is the single ecosystem-wide tenant-model knob (ADR-025 T2).
- **Dev extra now pins `django-stubs[compatible-mypy]>=5.1,<6`** instead of the
  previous unbounded `mypy>=1.10` + `django-stubs>=5.0`. The unbounded mypy pin
  resolved mypy 2.2.0, which crashes `NewSemanalDjangoPlugin` construction (no
  django-stubs release supports mypy 2.x); `compatible-mypy` keeps the two
  versions from drifting apart. The bare `mypy` line is dropped: the extra
  supplies a compatible mypy on its own.

### Fixed

- **`TenantMixin.objects` / `TenantMixin.unscoped` now carry explicit
  `ClassVar` annotations**, so `mypy` with the `django-stubs` plugin resolves
  `TenantManager[Model]` / `UnscopedManager[Model]` (rather than falling back
  to `Any`) on any model built from `TenantModel` or `TenantMixin`, with no
  configuration on the consumer's side. Verified against a sample model under
  `mypy` + `django-stubs`: reported by a downstream consumer (agentpm).
- **Documented, with a proven workaround, that `make_tenant_mixin()` and
  `make_tenant_path_mixin()` cannot be resolved as base classes by `mypy`.**
  This is a hard `mypy` limitation (`Unsupported dynamic base class` /
  `Invalid base class`, raised by `mypy`'s own semantic analyser before any
  plugin runs) for any base class built from a function call, not a gap in
  boundary's types; no annotation or stub shape changes it. The factory
  functions are unchanged at runtime. See the README's "Static typing and
  `make_tenant_mixin()` / `make_tenant_path_mixin()`" section for the two
  narrow `type: ignore` suppressions consumers need on the factory-built model
  and on its tenant model's reverse accessor, and for when to prefer the
  statically-resolvable `TenantMixin` / `TenantModel` instead.

## [0.4.0] - 2026-06-27

### Added

- **Indirect / traversal tenancy via `make_tenant_path_mixin(path)`.** Models
  that reach the tenant through a relation (e.g. `destination__merchant`,
  including multi-hop paths) can now be first-class tenant-scoped models instead
  of needing a bespoke manager. The manager auto-filters on the lookup path, and
  all column-writing paths (`save`, `bulk_create`, `bulk_update`,
  `get_or_create`/`update_or_create` injection) are correctly skipped because
  the model has no local tenant column. Such models carry no RLS policy on their
  own table (there is no column to scope) and are excluded from the RLS system
  check and provisioning; isolation comes from the parent on the path plus
  application-layer auto-filtering. New helpers `get_tenant_lookup(model)` and
  `has_tenant_column(model)` expose the distinction.
- **`@tenant_scoped(tenant_arg=...)` decorator** (`boundary.context`). Runs a
  service function or task inside `TenantContext.using(<the tenant argument>)`,
  resolving the tenant from a named or positional argument. The blessed idiom
  for "I hold the tenant explicitly" code, replacing hand-rolled managers that
  re-implemented context filtering. Defaults the argument name to
  `BOUNDARY_TENANT_FK_FIELD`.
- **`boundary.testing.call_view(view_cls, *, tenant, ...)`**: calls a
  class-based view directly under an active tenant context. `RequestFactory`
  bypasses middleware, so direct CBV tests otherwise raise `TenantNotSetError`;
  this builds the request and activates the tenant in one line.

### Changed

- **`get_or_create` / `update_or_create` are now tenant-scoped on direct-FK
  models.** The active tenant is injected into both the lookup half (so a `get`
  cannot match another tenant's row) and `defaults` / `create_defaults` (so the
  create stamps the FK), unless the caller supplied it explicitly. This removes
  the need for defensive `merchant=merchant` kwargs and makes the create path
  provably scoped. Behaviour is unchanged when the caller passes the FK; a
  caller that previously relied on an *unscoped* `get_or_create` matching across
  tenants will now be scoped (the safer behaviour). No-op for path-scoped
  models.
- **Minimum Django is now 5.2 LTS** (was 5.0). Django 5.0 and 5.1 are
  end-of-life; supported versions are 5.2 LTS and 6.0. Minimum Python remains
  3.12.

## [0.3.1] - 2026-06-24

### Fixed

- **`boundary_deprovision` no longer skips `make_tenant_mixin()` models.**
  Model discovery used `issubclass(model, TenantMixin)`, which misses models
  built with the `make_tenant_mixin()` factory (they are not `TenantMixin`
  subclasses). Their rows were neither exported nor deleted, while the command
  reported success: a tenant-data-isolation and right-to-erasure hazard.
  Discovery now uses `is_tenant_model()` and the per-model FK name via
  `get_tenant_fk_field()`, matching the rest of the package.

## [0.3.0] - 2026-06-22

### Fixed

- **RLS policies now honour `BOUNDARY_DB_SESSION_VAR` and
  `BOUNDARY_ADMIN_FLAG_VAR`.** `CreateTenantPolicy` previously hardcoded the
  literals `app.current_tenant_id` and `app.boundary_admin` in the generated
  SQL, so customising either setting silently broke isolation (the database
  policy tested a variable the runtime never set). The migration now reads the
  configured names. Because the names are baked into the migration SQL at apply
  time, changing the setting after the policies exist requires re-running the
  policy migration.

### Added

- **`boundary.routing.require_region(tenant=None)`**: returns the database
  alias a tenant routes to, or raises `RegionNotConfiguredError` when regions
  are unconfigured, no tenant is active, or the tenant's region is not in
  `BOUNDARY_REGIONS`. Gives `RegionNotConfiguredError` a real raise site for
  callers that need data residency enforced (the router itself cannot raise, as
  Django routers must always return an alias).
- **`TenantMiddleware._handle_inactive_tenant(request, tenant, exc)`**:
  overridable hook called with a `TenantInactiveError` when a resolved tenant is
  inactive. The default returns the existing HTTP 403; subclasses can return a
  custom response or re-raise.
- **`TenantMiddleware._on_resolver_error(request, resolver_path, error)`**:
  overridable hook called with a `TenantResolutionError` (wrapping the original
  exception) when a resolver raises. The default skips to the next resolver
  (unchanged behaviour); subclasses can re-raise to abort resolution.

## [0.2.0] - 2026-05-03

### Changed

- **Minimum Python is now 3.12** (was 3.11). Adds classifiers for 3.13 and 3.14.
- **Minimum Django is now 5.0** (already enforced by `Django>=5.0` dependency;
  classifiers updated to add 5.2 and drop pre-5.0 references).

### Added

- **Configurable terminology**: `BOUNDARY_TENANT_LABEL` setting controls the
  human-readable term used in error messages, `verbose_name` on FK fields
  created by `make_tenant_mixin()`, and the HTTP response bodies in
  `TenantMiddleware` ("Merchant not found", "Merchant is inactive"). Defaults
  to `BOUNDARY_TENANT_FK_FIELD`, so setting `BOUNDARY_TENANT_FK_FIELD =
  "merchant"` automatically themes errors as "merchant" without a second
  setting.
- **Configurable request attribute**: `BOUNDARY_REQUEST_ATTR` setting
  controls a second attribute name on the request object. `request.tenant`
  is always set for backwards compatibility; when this setting differs from
  `"tenant"`, the same value is also assigned to `request.<custom>` so views
  can read `request.merchant`.
- **Configurable tenant FK field name**: `BOUNDARY_TENANT_FK_FIELD` setting
  (default `"tenant"`) controls the FK field name on `TenantMixin`. Consumers
  who want domain-native names like `merchant` can set this globally.
- **`make_tenant_mixin(fk_field)` factory**: creates a custom `TenantMixin`
  with any FK field name, wired up with `TenantManager`, `UnscopedManager`,
  and auto-populate on `save()`. This is the public extension API for
  consumers who need full control without reimplementing package internals.
- **`is_tenant_model(model)`**: registry-backed check that recognises models
  using `TenantMixin`, `make_tenant_mixin()`, or any class with
  `_boundary_fk_field`. Replaces `issubclass(model, TenantMixin)` checks.
- **`get_tenant_fk_field(model)`**: returns the FK field name for a
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

Initial release: all four implementation phases.

### Added

#### Context Layer
- `TenantContext` with `set()`, `get()`, `clear()`, `require()`, `using()`
- Async-safe via `contextvars.ContextVar`
- PostgreSQL session variable via parameterised `set_config()` (SQL injection safe)
- Atomic ContextVar + DB session updates (rolled back on failure)
- Savepoint-safe nesting (`using()` explicitly restores DB session variable)

#### ORM Layer
- `AbstractTenant`: convenience base with name, slug, region, is_active, timestamps
- `TenantMixin` / `TenantModel`: adds tenant FK, auto-filtering manager, unscoped escape hatch
- `TenantManager`: auto-filters every queryset by active tenant
- `STRICT_MODE` (default: True): raises `TenantNotSetError` on unscoped queries
- Auto-populate `tenant` from context on `save()`
- `bulk_create()` auto-populates tenant; `bulk_update()` validates tenant ownership
- `unscoped` manager bypasses filtering for cross-tenant operations

#### Resolution Layer
- `TenantMiddleware`: WSGI/ASGI compatible via `MiddlewareMixin`
- 5 built-in resolvers: Subdomain, Header (UUID-first + slug fallback), JWT (no signature validation), Session, Explicit
- Pluggable resolver interface (`BaseResolver`)
- Thread-safe LRU cache with signal-based invalidation and configurable TTL
- Transaction wrapping for `set_config()` (respects `ATOMIC_REQUESTS`)

#### RLS Layer
- `EnableRLS` migration operation: enables and forces RLS on tables
- `CreateTenantPolicy`: generates LEAKPROOF `boundary_current_tenant_id()` function, isolation policy with `WITH CHECK` (INSERT enforcement), admin bypass policy
- `DropTenantPolicy`: reversible policy removal
- Type-aware: detects UUID vs integer tenant PKs
- System check `boundary.E006`: verifies RLS is enabled at startup via `pg_class`

#### Celery Integration
- `tenant_task` decorator: restores tenant context from task headers on worker
- `TenantTask` base class: injects headers at dispatch, restores on execution
- Tenant UUID and region serialised into headers (not kwargs)
- `TenantNotFoundError` is non-retriable

#### Regional Routing
- `RegionalRouter`: routes tenant-scoped queries to regional database aliases
- `all_regions()`: context manager yielding all configured region aliases
- `specific_region(key)`: pins queries to a named region
- Non-tenant models always route to `default`
- No silent fallback on unreachable regional DB

#### Management Commands
- `boundary_provision`: create tenant with hooks and extra fields
- `boundary_deprovision`: delete tenant with NDJSON export, dry-run, hooks
- `boundary_run`: execute any command scoped to a single tenant
- `boundary_run_all`: run against all tenants with `--parallel`, `--region`, `--exclude`, `--json`

#### Test Utilities
- `set_tenant()`: context manager for tests
- `tenant_factory()`: creates tenants with unique slugs
- `TenantTestMixin`: TestCase mixin with auto-created `self.tenant`

#### System Checks
- `boundary.E001`: BOUNDARY_TENANT_MODEL validation
- `boundary.E003`: resolver class import validation
- `boundary.E004`: TenantMiddleware in MIDDLEWARE
- `boundary.E005`: BOUNDARY_REGIONS requires DATABASE_ROUTERS
- `boundary.E006`: RLS enabled on TenantModel tables
- `boundary.W001`: STRICT_MODE disabled warning

#### Signals
- `tenant_resolved`: fired after successful tenant resolution
- `tenant_resolution_failed`: fired when no resolver matches
- `strict_mode_violation`: fired before TenantNotSetError is raised
