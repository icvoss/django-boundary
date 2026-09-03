# Isolation layers and the threat model

django-boundary enforces tenant isolation at two independent layers: the Django ORM and the PostgreSQL database. Neither layer is sufficient alone. Together they form defence in depth, where each layer catches the failures the other cannot see. This document explains what each layer does, the threats each one addresses, why you want both, and the role strict mode plays.

## The two layers

### ORM layer: TenantManager auto-filtering

Every tenant-scoped model gets a `TenantManager` as its default `objects` manager (wired up by `TenantMixin`, `TenantModel`, or `make_tenant_mixin()`). `TenantManager.get_queryset()` reads the active tenant from `TenantContext` and, when a tenant is set, transparently adds a `.filter(tenant=...)` clause to every query, using the model's configured FK field name.

The effect is that ordinary application code never has to remember to scope its queries. `Booking.objects.all()` returns only the active tenant's bookings, `Booking.objects.get(pk=...)` cannot reach another tenant's row, and `bulk_create`/`bulk_update` auto-populate or validate the tenant FK against the active context. Writes are covered too: `save()` auto-populates the FK from context when it is unset, and `bulk_update` raises `ValueError` if any object belongs to a different tenant.

The ORM layer is the primary, always-on isolation mechanism. It works on any database backend, including SQLite in tests, because it is pure Python query construction.

### Database layer: PostgreSQL Row Level Security

The database layer is built from the migration operations in `boundary.migrations_ops`: `EnableRLS`, `CreateTenantPolicy`, and `DropTenantPolicy`. `EnableRLS` runs `ALTER TABLE ... ENABLE ROW LEVEL SECURITY` followed by `FORCE ROW LEVEL SECURITY` so the policy applies even to the table owner. `CreateTenantPolicy` installs two policies and a helper function:

- A SQL function, `boundary_current_tenant_id()`, that reads the current tenant from the PostgreSQL session variable `app.current_tenant_id` (configurable via `BOUNDARY_DB_SESSION_VAR`). When `BOUNDARY_FUNCTION_LEAKPROOF` is set (default off), it is additionally declared `LEAKPROOF` so the query planner can push the RLS qualifier down to index scans; `LEAKPROOF` requires a superuser, which managed Postgres does not grant, so it defaults off and isolation is unaffected either way. The function detects whether the tenant primary key is a UUID or an integer and casts accordingly.
- `boundary_tenant_isolation`, a policy with both a `USING` clause (controls which rows are visible to `SELECT`/`UPDATE`/`DELETE`) and a `WITH CHECK` clause (controls which rows `INSERT`/`UPDATE` may write). Both clauses require `tenant_id = boundary_current_tenant_id()`. The `WITH CHECK` clause is what blocks an attempt to insert a row for a different tenant, even in raw SQL.
- `boundary_admin_bypass`, a policy that grants full read AND write access across every tenant when the session variable `app.boundary_admin` (configurable via `BOUNDARY_ADMIN_FLAG_VAR`) is set to `'true'`. It has a `USING` clause and no `WITH CHECK`; PostgreSQL falls back to `USING` for the write check in that case, and since permissive policies are combined with `OR`, satisfying this policy's `USING` is sufficient on its own regardless of `boundary_tenant_isolation`'s `WITH CHECK`. This is not a read-only escape hatch.

The session variables are set for you by `TenantContext`. When you set or enter a tenant scope, `TenantContext` issues `SELECT set_config('app.current_tenant_id', <pk>, true)` against the connection, scoped to the current transaction. So the same `TenantContext` that drives ORM filtering also drives RLS. See [`TenantContext`](../../README.md#context) and [Add RLS policies with migrations](../how-to/add-rls-policies-with-migrations.md).

## Why you want both

The two layers fail in different ways and protect against different mistakes. Running only one leaves a class of bugs uncovered.

### What the ORM layer catches that RLS misses

The ORM layer is ergonomic and database-agnostic. It auto-populates the tenant FK on writes, validates cross-tenant `bulk_update`, and produces clear Python exceptions when context is missing. RLS does none of this: RLS only ever filters or rejects rows, it never fills in a missing `tenant_id` for you, and its errors surface as raw database exceptions rather than `TenantNotSetError`. RLS also requires PostgreSQL, so in test suites and on other backends the ORM layer is the only isolation you have.

### What RLS catches that the ORM layer misses

The ORM layer protects you only as long as queries go through `TenantManager`. It is bypassed by:

- **Raw SQL.** `connection.cursor().execute("SELECT ... FROM bookings")`, `Model.objects.raw(...)`, and queries from reporting tools or analytics jobs never touch `TenantManager`.
- **The unscoped manager.** `Model.unscoped` (an `UnscopedManager`) deliberately returns all rows regardless of context. It is an escape hatch, and any code path that reaches for it loses ORM filtering.
- **Other ORMs, scripts, and direct connections.** Anything connecting to the database that is not your Django application code.

RLS sits below all of this. Because the policy lives in the database and is enforced with `FORCE ROW LEVEL SECURITY`, a non-superuser connection sees only rows matching the session variable, whatever tool issued the query. The RLS test suite demonstrates this: raw SQL counts return only the active tenant's rows, an empty tenant context returns zero rows, and a raw `INSERT` for the wrong tenant is rejected by the `WITH CHECK` clause.

In short: the ORM layer is broad and convenient but only covers code that goes through it; RLS is narrow in what it does (visibility and write checks) but covers everything that reaches the database.

## The threat model

The layers are designed against three concrete failure modes, in roughly increasing severity.

1. **Accidental cross-tenant leaks in application code.** A developer writes a query but forgets to scope it, or builds a related lookup that crosses tenants. This is the most common and most likely failure. The ORM layer handles it by making correct scoping the default: there is no unscoped query to forget, because `objects` is already filtered. Strict mode (below) hardens this further by refusing to run when no tenant is set, rather than silently returning everything.

2. **ORM bypass.** Code legitimately or carelessly steps outside the ORM: raw SQL, `raw()`, the `unscoped` manager, a management command that forgets to set context, a third-party library issuing its own queries. The ORM layer cannot help here because the query never reaches `TenantManager`. RLS is the backstop: as long as the connection runs as a non-superuser with policies in `FORCE` mode, those queries are still constrained to the active tenant's rows.

3. **Raw SQL and direct database access.** Reporting jobs, analytics pipelines, ad hoc psql sessions, or a compromised code path that constructs SQL directly. Only RLS protects against this, and only when the connecting role is not a superuser.

4. **Cross-tenant FK references (issue #39).** A row correctly scoped to tenant A can still hold a foreign key that points at a row belonging to tenant B. Neither of the first two layers catches this, because both only ever check the row's OWN tenant column, never what its FK columns point at:

   - **The ORM layer does not catch it.** `TenantManager.get_queryset()` filters on the row's own tenant lookup. The row's `tenant_id` is A, so it passes the filter regardless of what its FK targets belong to. Reading the relation then silently traverses into another tenant's data.
   - **RLS does not catch it either, for a structural reason, not a configuration gap.** The `boundary_tenant_isolation` policy's `USING`/`WITH CHECK` clauses check the child row's own `tenant_id` column against `boundary_current_tenant_id()`. That predicate is satisfied: the row's own `tenant_id` is correctly A. What the policy does not, and structurally cannot, constrain is the FK target: PostgreSQL's referential-integrity check on a FK (confirming the referenced row exists) runs as the table owner and is evaluated against the referenced table directly, not through the parent table's RLS policy. So an `INSERT`/`UPDATE` that sets a correctly-tenanted row's FK to point at tenant B's row is not rejected by RLS at all, even under `FORCE ROW LEVEL SECURITY` and a non-superuser connection.

   The mitigation is a `clean()` hook: `TenantMixin` and the mixin `make_tenant_mixin()` produces both call `boundary.models.validate_cross_tenant_fks()` from `clean()`, which compares the instance's own tenant FK value against the tenant FK value of any FK target that is itself tenant-scoped and owns a local tenant column, raising `ValidationError` on a mismatch. This is a `full_clean()`-only check, not a database constraint (a composite `UNIQUE (id, tenant_id)` constraint on every referenced table would close the gap airtight, but is a breaking schema change to every consumer for a gap only reachable through a consumer-side bug, so it was not taken). Concretely:

   - **Covered**: any path that calls `full_clean()`, most notably Django `ModelForm` validation, and any code that calls `instance.full_clean()` explicitly.
   - **Not covered, and not silently assumed to be**: `save()` (calling `full_clean()` before `save()` is the caller's responsibility, as with any Django model), `bulk_create()`, `update()`, `bulk_update()`, `get_or_create()`/`update_or_create()`, and raw SQL. None of these call `clean()`. A cross-tenant FK assigned through any of them is written successfully with the reference intact.
   - **Also not covered**: a FK to a *path-scoped* model (`make_tenant_path_mixin`), which has no local tenant column to compare against without traversing its own path. This is a known scope limit of the check, not an oversight.

   A consumer whose write paths go through `save()` without a preceding `full_clean()` (the common case for most save-in-a-service-function code, as opposed to form-driven code) gets no protection from this hook at all for that path. Where that matters, the consumer's own service layer must validate the FK explicitly, the same way it is responsible for anything else `clean()` would have caught but `save()` skips.

### Important limits of RLS

Be precise about what RLS does and does not protect, otherwise you will over-trust it.

- **Superusers and `BYPASSRLS` roles bypass RLS entirely**, even with `FORCE ROW LEVEL SECURITY`. The RLS enforcement tests deliberately connect as a separate non-superuser role (`icv_app`) for exactly this reason. If your Django application connects to PostgreSQL as a superuser, RLS provides no protection. Run your application as a least-privileged, non-superuser role.
- **RLS depends on the session variable being set correctly.** If `app.current_tenant_id` is empty, RLS returns zero rows; if it is set to the wrong tenant, RLS faithfully returns that tenant's rows. RLS enforces "show me rows matching this session variable", not "show me the right tenant". Correctness still depends on `TenantContext` setting the variable to the intended tenant.
- **The admin bypass policy is a deliberate hole.** Any connection that can set `app.boundary_admin` to `'true'` can read AND write every tenant's rows, not just see them: the bypass policy has no `WITH CHECK`, so PostgreSQL falls back to its `USING` clause for write checks too, and that clause only tests the flag. An `INSERT` or `UPDATE` that disagrees with the active tenant context (or has none at all) is accepted. That capability must be guarded as carefully as superuser access, and never set by hand: use [`admin_bypass()`](../../README.md#admin-bypass), which hardcodes the transaction-local form of `set_config` so the flag cannot outlive the transaction that set it, even across pooled or reused connections. See [Cross-tenant admin operations](../how-to/cross-tenant-admin-operations.md).
- **RLS does not auto-populate or validate writes beyond the policy predicate.** It will reject an `INSERT` whose `tenant_id` violates `WITH CHECK`, but it will not fill in a missing value. That ergonomics belongs to the ORM layer.
- **RLS does not constrain what a correctly-tenanted row's FK columns point at.** The policy predicate is evaluated against the row's own `tenant_id`; a FK referencing another tenant's row satisfies that predicate perfectly well, because the row itself is correctly tenanted. Referential-integrity checks on the FK target run as the table owner, outside the referencing table's policy. See threat 4 above and `boundary.models.validate_cross_tenant_fks()` for the (`full_clean()`-only) mitigation.

## Strict mode's role

Strict mode (`BOUNDARY_STRICT_MODE`, default `True`) governs what the ORM layer does when a tenant-scoped query runs with no active tenant in context.

- **With strict mode on**, `TenantManager.get_queryset()` raises `TenantNotSetError` when no tenant is set. It also sends the `strict_mode_violation` signal first, so you can wire up alerting. The query never runs.
- **With strict mode off**, the same query returns an unfiltered queryset across all tenants.

Strict mode closes the most dangerous gap in the ORM layer: a query that runs before context is established. Without it, a forgotten `TenantContext` set, a missing middleware, or a Celery task that did not bootstrap context would silently return or operate on every tenant's data. With it, that same mistake fails loudly and immediately, surfacing the bug in development and tests rather than leaking data in production. This is why it defaults to on, and why turning it off raises the `boundary.W001` system check warning.

Strict mode does not replace RLS, and RLS does not replace strict mode. Strict mode is a fail-closed default for the ORM layer; RLS is enforcement for everything below the ORM. A query that bypasses the ORM also bypasses strict mode, which is precisely the case RLS is there to cover.

## Putting it together

The recommended posture is all three controls active:

- The ORM layer always on, via `TenantMixin`/`TenantModel`/`make_tenant_mixin()`, for ergonomic, default-correct scoping.
- Strict mode on, so missing-context bugs fail closed instead of leaking.
- RLS enabled on every tenant-scoped table, with the application connecting as a non-superuser role, so raw SQL, the `unscoped` manager, and any non-Django access are still constrained at the database.

Boundary surfaces gaps in this posture through system checks: `boundary.E006` flags a tenant-scoped table missing RLS, and `boundary.W001` flags strict mode being disabled. See the [System Checks](../../README.md#system-checks) section of the README.

## Related

- [Add RLS policies with migrations](../how-to/add-rls-policies-with-migrations.md)
- [Cross-tenant admin operations](../how-to/cross-tenant-admin-operations.md)
- [Write tenant-safe tests](../how-to/write-tenant-safe-tests.md)
- [Set up a tenant model](../how-to/set-up-a-tenant-model.md)
- README: [Defence in Depth](../../README.md#how-it-works), [Row Level Security](../../README.md#row-level-security), [Settings Reference](../../README.md#settings-reference)
