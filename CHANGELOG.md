# Changelog

All notable changes to django-boundary are documented here.

## [Unreleased]

## [0.9.0] - 2026-09-21

### Added

- **`BOUNDARY_TENANT_APPS` and `BOUNDARY_ADOPT_EXCLUDE` settings, and the
  `AdoptTenantApp` migration operation** (issue #69, ADR-115). A consumer
  could not tenant-scope the concrete models a third-party app ships:
  allauth, taggit and wagtail each own their models and their migrations, so
  there is no abstract base to compose `TenantMixin` onto and no way to add a
  field without forking the app or taking over its migration history. Tenancy
  is now addable entirely in the database, invisibly to the ORM, because
  Django emits explicit column lists on `INSERT` and `SELECT` and therefore
  never names a column the model does not declare. List an app label in
  `BOUNDARY_TENANT_APPS` and apply `AdoptTenantApp("account")` from a
  migration in your own app: each concrete table of that app gains a
  `tenant_id` column typed from your tenant model's primary key, declared
  `NOT NULL DEFAULT boundary_current_tenant_id()`, with Row Level Security
  enabled and forced, the same `boundary_tenant_isolation` and
  `boundary_admin_bypass` policies a mixin-scoped table gets, and every
  non-primary-key unique constraint rewritten to a composite leading with
  `tenant_id` so uniqueness is per tenant rather than global. The adopted set
  is derived from the app registry rather than hand-listed, which is what
  makes drift after an upstream upgrade detectable;
  `BOUNDARY_ADOPT_EXCLUDE` exempts individual models by
  `"app_label.ModelName"`. The adopted package ships nothing, knows nothing,
  and is not modified: no Django field, no migration in its tree, no
  `MIGRATION_MODULES` relocation, and no entry in Django's migration state.
  A populated table is refused unless `backfill_tenant` names the tenant its
  rows belong to, because PostgreSQL evaluates `ADD COLUMN ... DEFAULT` once
  for pre-existing rows and would otherwise stamp them with whichever tenant
  was current at migration time, which during `migrate` is none. RLS is
  enabled last, after any backfill, because under `FORCE ROW LEVEL SECURITY`
  a migrating role without `BYPASSRLS` would update zero rows with no error.
  The operation is idempotent per table and refuses, rather than silently
  skips, any unique form it cannot safely rewrite (a constraint a foreign key
  targets, a partial unique index, an expression index, a deferrable
  constraint). `contenttypes`, `sessions`, `sites`, `auth.Permission`,
  `admin.LogEntry`, `django_migrations`, and the configured tenant model with
  its through tables are refused outright as global by construction.
  Documented in the new
  `docs/how-to/adopt-a-third-party-app.md`.

- **`boundary.E007` system check** (issues #69, #71). A database-backed,
  PostgreSQL-only Error that derives the expected adopted set from the live
  app registry and reports, per table, a missing or wrongly typed `tenant_id`
  column, Row Level Security that is not both enabled and forced, a missing
  `boundary_tenant_isolation` or `boundary_admin_bypass` policy, and any
  non-primary-key unique constraint that is not composite leading with
  `tenant_id`. It also reports a deny-listed or uninstalled app label in
  `BOUNDARY_TENANT_APPS`, the one condition that needs no database
  connection, and reports `boundary.W007` when a probe fails on a live
  connection so its absence cannot be misread as a pass. The unique-constraint
  condition is the load-bearing one: an upstream migration that drops and
  recreates an index replaces the composite constraint with a global one,
  restoring cross-tenant uniqueness with no other signal anywhere in the
  system, which is why this is an Error rather than a Warning. Because the
  check derives from the live registry while `AdoptTenantApp` derived from
  the historical migration state, a model added by an upstream upgrade is
  reported as a missing column; the remedy is a second `AdoptTenantApp`
  migration, which the per-table idempotency makes safe. Listing your own app
  in `BOUNDARY_TENANT_APPS` is supported, and is how a model in it that
  forgot its mixin gets reported.

- **Optional `app_label` keyword on `EnableRLS`, `CreateTenantPolicy` and
  `DropTenantPolicy`** (issue #70). Each operation now resolves its model
  from the given app label instead of the label of the app owning the
  migration, in both `database_forwards()` and `database_backwards()`, so a
  consumer can enable RLS on another app's table from a migration in their
  own app without relocating that app's migrations via `MIGRATION_MODULES`.
  `describe()` names the target as `app_label.ModelName` when the override is
  set; `deconstruct()` includes the keyword only when it is set, so every
  migration written before this keyword existed serialises byte-identically
  to what it serialised before.

- **`boundary.adoption` module.** The deny-list, the adopted-set derivation
  (usable against either the live app registry or a migration's historical
  state), and the PostgreSQL catalogue introspection helpers the operation
  and `boundary.E007` share.

### Changed

- **README comparison with django-tenants corrected against the vendor's own
  documentation** (#74). The old table attributed a "~500 tenants" ceiling
  that django-tenants never states, described its tenant storage as a
  thread-local when it is held on the database connection object, and called
  its Celery support "manual" when its docs point at the separate
  `tenant-schemas-celery` package. The table now cites the vendor docs and
  gains a third-party apps row.

- **`boundary.E006` and `boundary.W009` now cover adopted tables.** An
  adopted table owns a `tenant_id` column and a policy exactly as a
  column-bearing table does, so the same `pg_class` probe applies. `W009` in
  particular matters more there: an adopted table has no ORM layer to fall
  back on, so `BOUNDARY_SET_DB_SESSION_VAR = False` alongside live RLS
  policies leaves it with no isolation at all.

- **`boundary_deprovision` now covers adopted tables** in its collect, export
  and delete steps, so "all tenant-scoped rows" stays true once an app is
  adopted. An adopted table has no scoped manager, so each step reaches it by
  raw SQL against `tenant_id`, inside `admin_bypass()`, with the export
  streamed through a server-side cursor in `--batch-size` chunks. An adopted
  row's NDJSON line carries `"_adopted": true` and is keyed by **database
  column name** rather than Django field name, so a re-import can tell that
  `tenant_id` is not a Django field and that every value is in its raw
  database form. Adopted rows are deleted before the tenant row, by
  `tenant_id` value only, since the column is not a `ForeignKey` and has no
  database-level cascade. An adopted table whose app is missing from
  `BOUNDARY_TENANT_APPS` is invisible to deprovision and its rows survive the
  tenant.

- **`boundary.testing.assert_rls_enforced()` now covers adopted tables**
  alongside the registered column-bearing models it already inspected.
  Adopted tables are where it matters most: with no ORM layer beneath them, a
  suite running as a `BYPASSRLS` role, or against a database where the
  adoption DDL never applied, exercises no isolation at all and still passes
  every test. Its contract is otherwise unchanged.

### Behaviour change for existing consumers

None, unless `BOUNDARY_TENANT_APPS` is set. Both new settings default to an
empty list; with them unset, `boundary.E007` has nothing to check, `E006`,
`W009`, `boundary_deprovision` and `assert_rls_enforced()` see exactly the
models they saw before, and no existing migration changes what it serialises
or what it applies.

## [0.8.0] - 2026-09-11

### Added

- **`boundary.testing.provision_rls_test_role()` and `assert_rls_enforced()`
  / `rls_enforced()`** (issue #55). `boundary.W003` diagnoses a test/CI
  database role that bypasses Row Level Security, but only under
  `manage.py check`, never under a plain `pytest` run: a suite run as the
  bootstrap superuser most PostgreSQL docker images ship with passes every
  RLS-isolation test having enforced nothing. `provision_rls_test_role()`
  packages the `NOSUPERUSER NOBYPASSRLS` role provisioning
  `.github/workflows/ci.yml` already runs by hand, idempotently, and is a
  no-op on a non-PostgreSQL backend; it returns connection parameters
  rather than repointing `DATABASES["default"]` itself, since pytest-django
  creates the test database before a fixture could do that safely.
  `assert_rls_enforced()` is the fail-closed half: it raises
  `RLSNotEnforcedError`, naming the cause, unless the connecting role is
  neither superuser nor BYPASSRLS and at least one registered tenant table
  has RLS enabled and forced. `rls_enforced()` wraps it in `pytest.exit()`
  for use as a session-scoped autouse fixture, so a misconfigured run stops
  before any test executes. Documented in the README's Testing section.

- **`BOUNDARY_SET_DB_SESSION_VAR` setting and `boundary.W009` check** (issue
  #53). `TenantContext._set_db_session`/`_clear_db_session` previously
  issued `SELECT set_config(...)` unconditionally on every context entry
  and exit, with no way for a deployment using boundary for ORM-layer
  scoping only, with no RLS policies enabled, to skip the round trip.
  `BOUNDARY_SET_DB_SESSION_VAR` (default `True`) gates both methods: set it
  to `False` and they return before issuing any SQL.
  `BOUNDARY_DB_SESSION_VAR` itself stays a pure name, so
  `migrations_ops.py`, which reads the same name for RLS policy
  definitions, is unaffected by this opt-out. Because RLS enforcement
  depends entirely on the session variable, a new `boundary.W009` system
  check warns when the opt-out is on and Row Level Security is actually
  enabled and forced on a tenant-scoped table, the combination that would
  otherwise silently stop enforcing isolation on that table.

### Docs

- **README now mirrors the umbrella spec's "What it does not do" section**
  (issue #58). The package README previously listed three reasons to pick
  a different tool ("When NOT to use boundary") but had no equivalent of
  the spec's six stated exclusions (authentication, membership/RBAC, the
  tenant domain model, non-PostgreSQL RLS, cross-region migration,
  frontend/API/admin components). Added a "What boundary does not do"
  section stating all six, adjacent to the existing "When NOT to use"
  section, per `docs/STANDARDS.md:36`.

### Fixed

- **`boundary_provision --region` now writes the tenant row to its regional
  database** (issue #62). The command previously called
  `TenantModel.objects.create(**kwargs)` with no `using=`. During
  provisioning no tenant is yet active in `TenantContext`, so
  `RegionalRouter._route()` always falls back to `default`, meaning a
  tenant created with `--region` landed on the default database regardless
  of the region requested. The command now resolves the alias with
  `require_region()`, the same helper already documented for this purpose,
  and saves the tenant with `using=<alias>` when `BOUNDARY_REGIONS` is
  configured and a region is given; behaviour is unchanged when regions are
  unconfigured or no region is passed. A region that is not in
  `BOUNDARY_REGIONS` now fails the command with a `CommandError` naming the
  region and the configured keys, instead of silently writing to `default`.
  The post-provision hook still fires exactly once, after the row is
  written, with the same argument as before.

- **`_ensure_atomic()` and the DB session variable helpers no longer swallow
  their degraded paths silently** (issue #56, ADR-101). `_ensure_atomic()`'s
  `except ConnectionDoesNotExist` branch now logs a `logger.debug` naming the
  alias before returning `nullcontext()`, so a caller can distinguish "the
  alias does not exist, atomicity was skipped" from "no transaction was
  needed"; debug, not warning, because this branch is deliberate tolerance
  (typically a regional alias configured for routing tests only).
  `_set_db_session()`/`_clear_db_session()` now log a `logger.warning` naming
  the alias (and, for set, the tenant id) when `connection.connection is
  None`, since a no-op here is a real RLS risk: the `ContextVar` is set
  correctly, but no database state changes, so RLS on that connection sees
  no tenant if it is later opened without going through this call again. No
  new logger: both emit through the existing `boundary.context` logger.

- **`admin_bypass()`'s cleanup no longer masks the caller's original
  exception when clearing the flag itself fails** (issue #60, ADR-101). If
  the block inside `with admin_bypass():` raised while the transaction was
  already aborted, the `finally` block's own unguarded `cursor.execute` to
  clear the flag previously raised `TransactionManagementError`, which
  replaced the original exception as what propagated to the caller: a
  caller's `except IntegrityError:` (or any other specific type) around the
  block would never fire. The cleanup is now wrapped in `try/except
  Exception`, logging a `logger.warning` with `exc_info=True` on the
  `boundary.context` logger and letting the original exception propagate
  unmodified, mirroring the existing pattern at `TenantContext.using()` and
  `TenantContext.clear()`. A failed clear can leave the bypass flag set on
  that connection until it next recycles.

- **`RegionalRouter._route()`'s unmatched-region fallback is now
  operator-visible without flooding** (issue #59, ADR-101). When a tenant's
  region is set but not present in `BOUNDARY_REGIONS`, usually configuration
  drift (a decommissioned region, a bad write) rather than an expected
  routing case, `_route()` previously logged only at `logger.debug`, off by
  default in production. It now also logs a `logger.warning` on the
  `boundary.routing` logger, once per distinct `(tenant, region)` pair via a
  small bounded cache, since `_route()` runs on every ORM query and an
  unconditional warning would flood. The existing `logger.debug` line is
  unchanged. `_route()` still always returns `"default"`, as a Django
  database router must.

- **`boundary.E004` no longer contradicts icv-identity deployments, and now
  recognises a `TenantMiddleware` subclass** (issues #52 and #54).
  `_check_middleware` previously tested `MIDDLEWARE` for the literal string
  `boundary.middleware.TenantMiddleware`, which produced two false errors.
  First, a deployment where icv-identity owns tenant resolution (per ADR-025
  T1, its `TenantContextMiddleware` resolves the tenant and bridges into
  boundary's `TenantContext`) mounts no boundary middleware at all, which is
  the documented, intended shape, yet the literal-string test raised E004
  regardless; the same deployment's `SILENCED_SYSTEM_CHECKS` workaround for
  E004 also silenced `boundary.W002`'s genuine double-resolution warning, so
  no configuration satisfied both checks. Second, a consumer that mounts a
  subclass of `TenantMiddleware` (for example to gate resolution by host)
  still resolves the tenant on every request, but the literal-string test
  could not see past the dotted path. `boundary.E004` now stays silent when a
  `MIDDLEWARE` entry ends with icv-identity's
  `icv_identity.tenants.middleware.TenantContextMiddleware` (matched the same
  way `boundary.W002` already does, importing nothing from icv_identity), or
  resolves via `import_string` to a subclass of `TenantMiddleware` (matched
  the same way `boundary.W006` already does); an entry that fails to import
  is left for Django's own middleware loading to report and no longer raises
  from the check itself. A boundary-only deployment with no tenant-resolving
  middleware at all still fires E004 exactly as before.

## [0.7.0] - 2026-09-03

### Added

- **`BOUNDARY_SUBDOMAIN_PARENT_DOMAIN`: constrain `SubdomainResolver` to your
  own domain(s)** (issue #22). `SubdomainResolver.resolve()` previously took
  the first label of *any* host with three or more labels and looked it up as
  a tenant slug, with no check that the host belonged to the deployment's own
  domain. A deployment serving both platform subdomains and customer-owned
  custom domains from the same resolver chain could therefore resolve the
  wrong tenant for a foreign host whose first label happened to collide with
  a tenant slug: `shop.example.co.uk`, a domain never intended to serve
  tenant-by-subdomain traffic, would still resolve whichever tenant was
  slugged `shop`. That is cross-tenant serving.

  Setting `BOUNDARY_SUBDOMAIN_PARENT_DOMAIN` to your own parent domain (a
  string, or a list of strings for a deployment with several parent domains)
  makes `SubdomainResolver` return `None` unless the host is exactly
  `<one-label>.<parent-domain>` for one of the configured parents. Matching
  is case-insensitive and by label boundary rather than substring, so
  `evilexample.com` and `evil-example.com` do not match a parent of
  `example.com`, and it is exactly one level deep, so
  `club-a.staging.example.com` does not match either. Multi-label parent
  domains (`example.co.uk`) work correctly. A trailing dot on the incoming
  host is stripped before matching.

  **Nothing changes for anyone who does not set this setting.** It is unset
  by default, and `SubdomainResolver` resolves exactly as it always has when
  it is unset. `boundary.W008` warns at startup when `SubdomainResolver` is
  configured without it, as a prompt rather than an error, because a
  deployment whose `ALLOWED_HOSTS` is already a small, fully-trusted, closed
  list has nothing to gain from setting it; silence the warning in
  `SILENCED_SYSTEM_CHECKS` if that describes your deployment. See
  [Constrain SubdomainResolver to your own domain](docs/how-to/choose-and-order-resolvers.md#constrain-subdomainresolver-to-your-own-domain).

- **Documentation: "Scope a package's models into your tenancy"** (issue
  #27), the pattern ADR-074 D5/D6 mandates for tenant-scoping a third-party
  ICV package's abstract model, `class Article(TenantMixin, AbstractArticle)`
  with the manager declared explicitly. No worked example of this composition
  existed anywhere in the docs before this; every prior mention of
  `TenantMixin` covered a consumer's own model only. States inline, at the
  snippet, that a subclass declaring no `objects` silently loses the
  package's manager to boundary's `TenantManager`, with a green test suite.
  Also documents that `TenantManager` is intended to be subclassed
  (`docs/explanation/isolation-layers.md`), so a package keeps its own
  manager contract while boundary's scoping still applies underneath.

### Fixed

- **`SubdomainResolver` no longer queries the database for a host with an
  empty label** (issue #46). A host with a leading dot (`.example.com`) split
  to `["", "example", "com"]`, which passed the three-label guard, so the
  empty string was used as the tenant slug and reached
  `TenantModel.objects.get(slug="")` on every such request. A host with an
  interior empty label (`shop..example.com`) was worse on the unguarded path:
  it took `shop` as the slug and resolved whichever tenant carried that slug
  from a malformed host. `BOUNDARY_SUBDOMAIN_PARENT_DOMAIN` did not close
  either case: `.example.com` satisfies both the label-count and tail
  comparison against parent `example.com` and still yields `""`.

  An empty label is never a valid DNS label (RFC 1035 requires 1 to 63
  octets), so such a host is now rejected before the cache read and the
  database lookup, on both the guarded and unguarded paths. In practice the
  empty-slug lookup raised `DoesNotExist` and was caught, so **no wrong
  tenant was served** for the leading-dot case and nothing changes in what
  your application returns for it; what changes is that the pointless query
  per request is gone, and a tenant row that did carry an empty slug is no
  longer resolvable by any leading-dot host. The interior-empty-label case
  is a genuine behaviour change on the unguarded path: `shop..example.com`
  previously resolved the tenant slugged `shop` and now resolves nothing. No
  well-formed host changes behaviour.

- **`TenantManager.from_queryset(CustomQuerySet)` now works** (issue #29).
  `get_queryset()` previously constructed a hardcoded `TenantQuerySet`
  instead of honouring `self._queryset_class`, so a manager built with
  `TenantManager.from_queryset(CustomQuerySet)` generated methods that
  called `get_queryset()` and then the custom method on the result, and
  since the returned object was never a `CustomQuerySet`, every generated
  method raised `AttributeError` at runtime. `from_queryset()` appeared to
  succeed and mypy was satisfied, so the break was only visible when the
  method was actually called. If you worked around this by hand-copying
  boundary's tenant filter, strict-mode branch, `strict_mode_violation`
  signal, and `TenantNotSetError` message into your own
  `get_queryset()` override, that override can now be deleted:
  `TenantManager.from_queryset(YourQuerySet)` does the same job and stays
  in sync with boundary's own filtering logic. Behaviour for anyone not
  using `from_queryset()` is unchanged: a plain `TenantManager` still
  returns a `TenantQuerySet` instance (now set explicitly via
  `_queryset_class` rather than relying on it being hardcoded).

## [0.6.0] - 2026-09-03

### Added

- **`boundary.E005`: error when `BOUNDARY_REGIONS` is configured but
  `RegionalRouter` is absent from `DATABASE_ROUTERS`** (issue #36). The
  README has documented `boundary.E005` since before 0.5.3; the check
  itself did not exist anywhere in the package, so any consumer who set
  `BOUNDARY_REGIONS` and relied on the documented check to catch a missing
  `DATABASE_ROUTERS` entry got no protection. Per `BR-REG-002`,
  `RegionalRouter` is never added to `DATABASE_ROUTERS` automatically, so
  a project that configures regions but forgets that line previously had
  every query silently stay on the `default` database, with no error and
  no warning: for a data-residency feature, a silent fallback to the
  wrong database is a compliance problem. Matched by `issubclass` against
  `RegionalRouter`, so a consumer subclass, or an already-constructed
  router instance (Django accepts both forms in `DATABASE_ROUTERS`),
  satisfies the check. Fires only when `BOUNDARY_REGIONS` is non-empty,
  matching `RegionalRouter._route()`'s own treatment of an empty dict as
  "not configured".
- **`boundary.W006`: warn when a client-controlled resolver is configured
  alongside `django.contrib.auth`** (issue #38). `HeaderResolver` and
  `JWTClaimResolver` (and any subclass of either) take the tenant directly
  from a value the client supplies: a header, or an unverified JWT claim.
  boundary resolves *which* tenant a request targets; it never checks
  *whether the caller may access it*, so an authenticated user of one
  tenant can simply name another and every layer beneath resolution (the
  ORM manager, RLS, the session variable) scopes correctly to it. Fires
  only when the application also has authenticated users
  (`django.contrib.auth` in `INSTALLED_APPS`), since that is the
  configuration where the missing membership check actually bites; an
  application with no authenticated users has nothing to mis-scope. The
  hint points at the new "Enforce membership after resolution" section in
  [choose-and-order-resolvers.md](docs/how-to/choose-and-order-resolvers.md#enforce-membership-after-resolution),
  which documents the middleware shape and where it sits relative to
  `TenantMiddleware`. A consumer who has already handled membership
  silences it by ID via `SILENCED_SYSTEM_CHECKS`.
- **`admin_bypass()` context manager for the RLS admin bypass flag** (issue
  #37). The flag (`BOUNDARY_ADMIN_FLAG_VAR`, default `app.boundary_admin`)
  previously had no API: consumers were directed by the docs to call
  `set_config` by hand, and correctness depended entirely on passing the
  right third argument. `admin_bypass()` hardcodes the transaction-local
  form, so the unsafe session-scoped form (which can persist across
  `CONN_MAX_AGE` connection reuse or an external pooler handing the
  connection to an unrelated request) is not reachable through this API.
  It reuses the same transaction-guarantee machinery as
  `TenantContext.using()`, verifies the flag actually took effect before
  running the wrapped block, and clears it explicitly on exit. Fires the
  new `admin_bypass_activated` signal on entry (flag variable name and DB
  alias) so use of the escape hatch is auditable. Nested calls on the same
  alias are idempotent; only the outermost call clears the flag.
- **`boundary.exceptions.AdminBypassNotActiveError`**, raised by
  `admin_bypass()` if a read-back after setting the flag does not confirm
  it is active (most likely `BOUNDARY_WRAP_ATOMIC=False` with no ambient
  transaction), rather than silently proceeding as though the bypass were
  in effect.
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
- **`boundary.W007`: warn when `boundary.E006` or `boundary.W003` cannot
  determine the database state they check.** Both checks previously
  swallowed any exception from their `pg_class`/`pg_roles` query and
  reported nothing, which is indistinguishable from "every table is
  correctly protected" or "this role does not bypass RLS": a permissions
  error reading `pg_class`, a statement timeout, or a lock made the checks
  fail open. The exception handling now distinguishes a genuinely
  unreachable connection (`OperationalError`/`InterfaceError`, e.g. before
  the database is provisioned, which stays a silent skip as before) from a
  live connection whose query failed for some other reason, which now
  raises `boundary.W007` instead of vanishing. Fixes #34.
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

- **`docs/reference/settings.md` framed `BOUNDARY_RESOLVERS` purely as a
  URL-shape and API-style choice and said `JWTClaimResolver` alone was
  "usually sufficient" for internal APIs, with no statement that
  resolution is not authorisation** (issue #38). Corrected: the resolver
  table now marks which resolvers are client-controlled (`HeaderResolver`,
  `JWTClaimResolver`, and their subclasses) versus derived from something
  the client cannot freely choose (`SubdomainResolver`, `ExplicitResolver`,
  and `SessionResolver` provided its session key is only ever written
  server-side after a check of its own), and states plainly that the
  consumer must verify the authenticated principal's membership of the
  resolved tenant. Same correction applied to the README resolver table
  and architecture diagram, `docs/explanation/how-resolution-works.md`,
  and `docs/explanation/isolation-layers.md`'s threat model.
- **`BoundaryConfig._connect_cache_invalidation_signals()` read
  `BOUNDARY_TENANT_MODEL` directly and returned early when it was unset,
  ignoring the `ICV_TENANT_MODEL` fallback (ADR-025 T2) that every other
  resolution site in the package applies.** A project configured with
  only `ICV_TENANT_MODEL`, the supported configuration that
  `boundary.E001` explicitly accepts, therefore connected zero
  `post_save`/`post_delete` receivers on its tenant model, with no error
  or warning at startup. Resolver-cache entries in `SubdomainResolver`,
  `HeaderResolver` and `SessionResolver` then went stale for up to
  `BOUNDARY_RESOLVER_CACHE_TTL` seconds (default 60) on every worker
  process after a tenant was updated or deleted, so `TenantMiddleware`
  could keep serving a just-deactivated tenant. The signal connector now
  calls `boundary.conf.resolve_tenant_model_setting()`, catching
  `ImproperlyConfigured` to preserve the no-op-when-unset behaviour for a
  project that has not configured boundary at all yet. Fixes #33.
- **`test_cache_invalidated_on_save` and `test_cache_expires_after_ttl`
  asserted `result == tenant_a`, which holds for a cache hit and a cache
  miss alike, so both passed even with cache invalidation removed
  entirely.** Both now wrap the post-invalidation resolve in
  `django_assert_num_queries(1)`, matching the pattern their sibling
  `test_cache_hit_avoids_query` already used, so they fail if the DB
  isn't actually queried. A new `test_cache_invalidated_on_delete` adds
  the coverage that was previously entirely missing for the `post_delete`
  side of the same signal wiring. Fixes #35.
- **`TenantMiddleware` silently disabled the RLS defence-in-depth layer for
  every request when `ATOMIC_REQUESTS = True` was set on the default
  database.** The middleware skipped its own transaction wrap whenever
  `ATOMIC_REQUESTS` was already on, assuming Django's own transaction would
  already be open when it called `TenantContext.set()`. It was not: Django's
  `ATOMIC_REQUESTS` wraps the view, not the middleware chain, so at the point
  the middleware set the PostgreSQL session variable no transaction had
  opened yet, and the transaction-local `set_config(..., true)` call was
  discarded before the view's transaction began. Application-level tenant
  scoping (`TenantContext.get()`, the tenant-aware manager) was unaffected
  and continued to report the correct tenant, but the RLS policies reading
  `current_setting('app.current_tenant_id', true)` saw an empty session
  variable throughout the request. Under `FORCE ROW LEVEL SECURITY` this
  fails closed (no rows returned) rather than leaking data, but it meant RLS
  provided no protection at all on any consumer running
  `ATOMIC_REQUESTS = True`. `TenantMiddleware` now always wraps the request
  in its own transaction (via `context._ensure_atomic()`, honouring
  `BOUNDARY_WRAP_ATOMIC = False` for consumers who manage transactions
  themselves), so the session variable is reliably in scope regardless of
  the `ATOMIC_REQUESTS` setting; when `ATOMIC_REQUESTS` is also on, the
  view's transaction now nests as a savepoint inside boundary's, and an
  exception in the view still rolls back the whole request. If you run
  `ATOMIC_REQUESTS = True` and rely on RLS, upgrade: no configuration
  change is required. Fixes #40.
- **`docs/explanation/isolation-layers.md` understated the RLS admin bypass
  flag as visibility-only** ("sees every tenant's rows"). Verified against
  the actual policy SQL as a non-superuser: `boundary_admin_bypass` has no
  `WITH CHECK`, so PostgreSQL falls back to its `USING` clause for write
  checks too, and permissive policies are combined with `OR`, so the flag
  alone is sufficient to pass an `INSERT` or `UPDATE` for any tenant.
  Corrected to state read AND write access, matching
  `docs/how-to/cross-tenant-admin-operations.md`, which already had it
  right. Both pages now also document the interaction with `CONN_MAX_AGE`
  and connection pooling.
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
- **`boundary.E006`'s table lookup is now qualified by OID
  (`to_regclass()`) instead of an unscoped `relname` match.** Where the
  same table name exists in more than one schema (a partition archive, a
  staging schema, a multi-entry `search_path`), the previous query
  returned one `pg_class` row per matching schema with no `ORDER BY`, and
  `fetchone()` read whichever row PostgreSQL's planner produced first,
  which was not reliably the table the model's own table name actually
  resolves to. The lookup now resolves the table the same way any
  ordinary query against it would: through `to_regclass()`, which follows
  the connection's `search_path`. Fixes #34.
- **`boundary.E006` never had a positive test proving it can report a
  missing policy.** Every prior test asserted only that it stays silent,
  so its silence had never been distinguished from an inability to see.
  The suite now includes a positive control that deliberately leaves RLS
  absent on a tenant-scoped table and asserts `boundary.E006` fires, paired
  with the existing "stays silent when protected" case. Fixes #34.

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
