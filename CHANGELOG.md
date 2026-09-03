# Changelog

All notable changes to django-boundary are documented here.

## [Unreleased]

### Added

- **`boundary.W003`: warn when the database connection role bypasses Row
  Level Security** (superuser or `BYPASSRLS`). PostgreSQL exempts such
  roles from every RLS policy, including `FORCE ROW LEVEL SECURITY`
  tables, so `boundary.E006` passing (RLS enabled and forced on the
  tables) gives no guarantee that isolation actually applies to the
  connection in use. The stock bootstrap role of nearly every Postgres
  docker image is exactly this kind of role, so a consumer's RLS-policy
  tests can pass on every developer machine while testing nothing. The
  warning is deliberately not silenced under pytest/DEBUG: the failure
  it exists to catch lives precisely in local and CI test runs on a
  bypassing role. A deliberately chosen superuser connection (initial
  provisioning, some managed deployments) is silenced by ID via
  `SILENCED_SYSTEM_CHECKS`. Fixes #21.
- **Django 6.1 added to the CI test matrix** and declared via the
  `Framework :: Django :: 6.1` classifier.
- **`clean()` validation rejects a cross-tenant foreign key reference**
  (BR-ORM-013). Neither isolation layer catches a tenant-scoped row whose
  own `tenant_id` is correct but whose FK points at another tenant's row:
  the ORM manager filters on the row's own tenant column, and RLS's
  `USING`/`WITH CHECK` predicate checks the same column, not what the
  row's FK targets belong to. `TenantMixin.clean()` and the `clean()` on
  the mixin `make_tenant_mixin()` produces now call
  `boundary.models.validate_cross_tenant_fks()`, which raises
  `django.core.exceptions.ValidationError` (keyed by field name) when an
  FK to another tenant-scoped model (one with its own tenant column)
  points at a row belonging to a different tenant than the instance
  itself. A `None` FK, a FK to a non-tenant model, and a FK to a
  path-scoped model are all left alone. **This fires on `full_clean()`
  paths only** (`ModelForm` validation, an explicit `full_clean()` call):
  `save()`, `bulk_create()`, `update()`, `bulk_update()`, and raw SQL do
  not call `clean()` and remain unprotected, same as any other Django
  model validation. See
  [`docs/explanation/isolation-layers.md`](docs/explanation/isolation-layers.md)
  for the full threat-model entry. Fixes #39.

### Fixed

- **`boundary.checks` was never imported anywhere the package itself
  runs, so every system check it defines (`E001`, `E003`, `E004`,
  `E006`, `W001`, `W002`, and now `W003`) silently never registered on a
  real `manage.py check`, `migrate`, or server startup.** They only
  appeared to work because `tests/test_checks.py` imports the module
  directly, so the test suite exercised the check functions while the
  application never did. `BoundaryConfig.ready()` now imports
  `boundary.checks`, which registers the whole suite via its `@register`
  decorators the same way Django's own checks register.
- **CI now provisions the non-superuser `icv_app` role** the RLS
  enforcement tests and the `boundary.W003` "stays silent" test require,
  so those tests run for real in CI instead of skipping (the `icv_test`
  bootstrap role from the `postgres:16` service image is itself a
  `BYPASSRLS` superuser).

## [0.5.3] - 2026-08-01

### Fixed

- **`TenantMiddleware` no longer breaks under ASGI.** It subclassed
  `MiddlewareMixin`, which declares `async_capable = True`, while overriding
  `__call__` with a purely synchronous body. Under ASGI, Django therefore
  handed it an async `get_response` and treated the instance as a coroutine
  function; the sync `__call__` called that async `get_response` without
  awaiting it, so the `try`/`finally` cleared `TenantContext` (and exited the
  `transaction.atomic()` wrap) before the returned coroutine was ever
  awaited. Every async-served request ran its view with no tenant in context
  and no RLS session variable. A second facet: on the no-tenant 404 and
  inactive-tenant 403 paths, the coroutine-marked middleware returned a plain
  `HttpResponse`, which Django then tried to await, raising `TypeError`.
  `TenantMiddleware` now declares `sync_capable = True` and
  `async_capable = False` explicitly, so Django's middleware machinery
  adapts it under ASGI instead (wrapping it in `sync_to_async`, with
  `async_to_sync` for anything downstream that is itself async), which keeps
  the tenant context and the DB session variable active for the whole
  request, including async views. (#16)

- **`TenantContext.clear()` now restores the previous tenant's DB session
  variable, not only the ContextVar.** After a nested `set(a)`, `set(b)`,
  `clear(token_b)`, the ContextVar correctly reported tenant A again, but the
  PostgreSQL session variable had only ever been reset to an empty string
  for the removed tenant B: it was never re-applied for tenant A. RLS
  therefore saw no active tenant even though application code believed one
  was set. `clear()` now mirrors the restore already done in `using()`'s
  `finally` block: it clears the session variable on the removed tenant's
  aliases (default and its regional alias, BR-CTX-009), then, if a previous
  tenant is now active, re-sets the variable to that tenant's pk on its own
  alias set. (#13)

- **`ICV_TENANT_MODEL` alone is now enough to start a project.**
  `boundary.conf.get_tenant_model()` and `boundary_settings.TENANT_MODEL`
  already fell back from `BOUNDARY_TENANT_MODEL` to `ICV_TENANT_MODEL`
  (ADR-025 T2), but `boundary.models` read `settings.BOUNDARY_TENANT_MODEL`
  directly at import time in both `TenantMixin`'s and `make_tenant_mixin()`'s
  foreign key declarations, raising a bare `AttributeError` before either
  setting's fallback ever got a chance to run. `boundary.checks` had the
  same gap: `_check_tenant_model()` (`boundary.E001`) and
  `_check_rls_enabled()` read only `BOUNDARY_TENANT_MODEL`. A new
  `boundary.conf.resolve_tenant_model_setting()` helper (`BOUNDARY_TENANT_MODEL`
  first, then `ICV_TENANT_MODEL`, raising `ImproperlyConfigured` naming both
  settings if neither is set) now backs both FK declarations and both
  checks, so a project configured with only `ICV_TENANT_MODEL` starts
  cleanly. One deliberate exception-type change rides along:
  `get_tenant_model()` now raises `ImproperlyConfigured` for the
  missing-setting case (previously `LookupError`), the Django idiom for a
  configuration error; the `LookupError` raised when the dotted path does
  not name an installed model is unchanged. Whichever setting resolves
  remains structural: it is baked into the FK (and your migrations) at
  import time, exactly as before. (#15)

### Changed

- **The documented contract for relation-scoped (path-scoped) models is now
  explicit: they are protected at the ORM layer only, never at the database
  layer.** `make_tenant_path_mixin()` models have no local tenant column and
  therefore no RLS policy, and `boundary.checks` intentionally exempts them
  from `boundary.E006`. The previous docstring and README wording described
  this as "inheriting isolation from the parent on the path", which reads as
  database-level protection; it is not. PostgreSQL RLS is table-specific:
  the parent's policy constrains scans of the *parent* table (so an ORM
  query joining through the path is constrained too), but it does **not**
  constrain direct SQL, unscoped managers, or third-party access run
  straight against the child table. `make_tenant_path_mixin()`'s docstring,
  the `boundary.E006` skip comment, the
  `docs/how-to/scope-models-through-a-relation.md` how-to (now with an
  explicit warning), and the README's defence-in-depth section are corrected
  to state this plainly, and a new raw-SQL test in `tests/test_rls.py` pins
  the contract: an unscoped query against a path-scoped child table returns
  every tenant's rows even with RLS applied to its parent. (#14)

## [0.5.2] - 2026-07-28

### Fixed

- **Regional RLS: the tenant session variable is now set on the regional
  connection, not only `default`** (issue #7, BR-CTX-009). With
  `BOUNDARY_REGIONS` configured, `RegionalRouter` sends a tenant's
  tenant-scoped queries to its regional database alias, but `TenantContext`
  only ever ran `set_config()` on `default`. Row-level security on the
  regional database therefore saw an empty tenant variable, so a
  tenant-scoped write was silently mis-scoped or failed with no indication at
  the call site, the same silent-data-loss class as the autocommit gap fixed
  in 0.5.1. `TenantContext.set()`, `using()`, and `clear()` now resolve the
  tenant's regional alias (via the new internal `_regional_alias()` /
  `_aliases_for()` helpers, computed from settings to avoid a
  `context -> routing` import cycle) and set, restore, and clear the session
  variable on `default` AND that regional connection. `using()` opens the
  autocommit-guard transaction on every target alias so the transaction-local
  `set_config(..., true)` survives on the regional connection too. No effect
  when regions are unconfigured (the single-region path is unchanged).

### Fixed

- **`TenantContext.using()` no longer silently no-ops outside a transaction.**
  `set_config(..., true)` is transaction-local; outside a request (management
  commands, Celery tasks, ad hoc scripts, all of which run in autocommit by
  default), the session variable it set was gone before the next statement
  ran, and a tenant-scoped write then failed deep in the database with an
  opaque RLS error rather than at the `using()` call site. `using()` now opens
  `transaction.atomic()` for its own body whenever one is not already active,
  controlled by `BOUNDARY_WRAP_ATOMIC` (default `True`, matching the setting
  `TenantMiddleware` already honours); it is a no-op when a transaction is
  already active, so nesting inside a request or another `using()` block does
  not open a redundant transaction. The Celery worker-side restoration
  (`TenantTask.__call__`, `@tenant_task`) gets the same guarantee, since
  Celery workers also run in autocommit by default. This fixes `using()`
  itself and every API built on it: `tenant_scoped`, `boundary_run`,
  `boundary_run_all`, and `boundary.testing`'s `set_tenant` / `call_view` /
  `TenantTestMixin`. If `BOUNDARY_WRAP_ATOMIC` is deliberately set to `False`,
  `using()` now logs a warning when entered outside a transaction rather than
  failing silently. (#6)

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
