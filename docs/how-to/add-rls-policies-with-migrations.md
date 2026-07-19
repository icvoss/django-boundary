# Add RLS policies with migrations

## Goal

Enforce tenant isolation at the database level by adding PostgreSQL Row Level Security (RLS) to a tenant-scoped table, using boundary's migration operations. RLS is a second layer of defence: even raw SQL, a leaked connection, or a bug in your ORM scoping cannot read or write another tenant's rows.

The ORM layer (the tenant manager and middleware) works on any database. RLS enforcement is **PostgreSQL-only** and requires PostgreSQL 14 or later.

## Prerequisites

- A model that is already tenant-scoped (it has a tenant foreign key, the column is `tenant_id` by default). See the [README](../../README.md) for how models acquire the tenant FK.
- `BOUNDARY_TENANT_MODEL` configured in settings.
- PostgreSQL 14+. On any other database, do not add these operations: they emit PostgreSQL-specific DDL.
- The migration that creates the table (or adds the tenant FK column) already exists, or is created in the same file before the RLS operations.

## Steps

The three operations live in `boundary.migrations_ops`:

| Operation | Constructor signature | Effect |
| --- | --- | --- |
| `EnableRLS` | `EnableRLS(model_name)` | `ENABLE` + `FORCE ROW LEVEL SECURITY` on the table |
| `CreateTenantPolicy` | `CreateTenantPolicy(model_name, tenant_column=None)` | Creates the isolation and admin-bypass policies plus the `boundary_current_tenant_id()` helper function |
| `DropTenantPolicy` | `DropTenantPolicy(model_name, tenant_column=None)` | Drops both policies |

`model_name` is the model's name within its app (for example `"Booking"`), exactly as you would pass to `migrations.CreateModel`. It is **not** the app label or `app_label.Model` dotted path; the operation resolves the table from migration state via `app_label`.

1. Create an empty migration in the app that owns the model:

   ```bash
   python manage.py makemigrations bookings --empty --name enable_rls
   ```

2. Add `EnableRLS` followed by `CreateTenantPolicy` to the `operations` list. `CreateTenantPolicy` must run **after** `EnableRLS`, and both must run after the table and its tenant FK column exist.

   ```python
   from django.db import migrations

   from boundary.migrations_ops import CreateTenantPolicy, EnableRLS


   class Migration(migrations.Migration):
       dependencies = [
           ("bookings", "0001_initial"),
       ]

       operations = [
           EnableRLS("Booking"),
           CreateTenantPolicy("Booking"),
       ]
   ```

3. If you create the table in the same migration, order the operations so the table exists first:

   ```python
   operations = [
       migrations.CreateModel(name="Booking", fields=[...]),
       EnableRLS("Booking"),
       CreateTenantPolicy("Booking"),
   ]
   ```

4. If your tenant column is not `tenant_id`, pass `tenant_column`. The value is the database column name, so include the `_id` suffix for a foreign key:

   ```python
   CreateTenantPolicy("Booking", tenant_column="org_id")
   ```

   `CreateTenantPolicy` inspects the FK target's primary key and generates the matching cast in the helper function, so both UUID and integer tenant primary keys work without further configuration.

5. Apply the migration:

   ```bash
   python manage.py migrate bookings
   ```

### What `CreateTenantPolicy` creates

- A helper function, `boundary_current_tenant_id()`, which reads the session variable, casts it to the tenant PK type, and returns `NULL` on any error (so a missing or empty value yields zero rows rather than an error). It is declared `LEAKPROOF` only when `BOUNDARY_FUNCTION_LEAKPROOF` is set (default off), since `LEAKPROOF` requires a superuser that managed Postgres does not grant. `LEAKPROOF` is a planner optimisation, not an isolation requirement. See [`BOUNDARY_FUNCTION_LEAKPROOF`](../reference/settings.md#boundary_function_leakproof).
- An isolation policy, `boundary_tenant_isolation`, with both `USING` and `WITH CHECK` clauses. `USING` filters `SELECT`, `UPDATE`, and `DELETE`. `WITH CHECK` blocks `INSERT` (and `UPDATE`) of rows belonging to a different tenant.
- An admin-bypass policy, `boundary_admin_bypass`, that returns all rows when the admin flag session variable is `'true'`.

## How the session variables drive the policies

The policies read two PostgreSQL session variables at query time:

- The tenant variable, configured via `BOUNDARY_DB_SESSION_VAR` (default `app.current_tenant_id`). Boundary sets this for you inside `TenantContext`: entering a tenant context runs `SELECT set_config(<var>, <tenant_pk>, true)`, and the `true` scopes it to the current transaction. This is why request handling and `TenantContext.using(...)` must run inside a transaction (the middleware wraps requests in `transaction.atomic()` when `BOUNDARY_WRAP_ATOMIC` is `True`).
- The admin-bypass variable, configured via `BOUNDARY_ADMIN_FLAG_VAR` (default `app.boundary_admin`). Setting it to `'true'` makes `boundary_admin_bypass` return every row, regardless of tenant. Use this only for trusted cross-tenant work such as management commands and analytics. Boundary does not set this flag automatically; set it explicitly with `set_config` when you need it, and prefer transaction-local scope:

  ```python
  from django.db import connection

  with connection.cursor() as cursor:
      cursor.execute("SELECT set_config('app.boundary_admin', 'true', true)")
  ```

> **Important:** the generated SQL reads `BOUNDARY_DB_SESSION_VAR` and `BOUNDARY_ADMIN_FLAG_VAR` when the migration runs, so the policies test the same session variables the runtime sets. Because the names are baked into the migration SQL at apply time, changing either setting after the policies exist requires re-running the policy migration (drop and recreate, for example via `DropTenantPolicy` then `CreateTenantPolicy`) so the database picks up the new names. If the setting and the policy ever disagree, isolation breaks (every query returns zero rows), so keep them in sync.

### A note on superusers

PostgreSQL superusers bypass RLS even with `FORCE ROW LEVEL SECURITY`. Run your application on a **non-superuser** database role so the policies actually apply. Verify enforcement using that role, not a superuser connection.

## Verify it worked

1. Confirm RLS is enabled and forced on the table:

   ```sql
   SELECT relrowsecurity, relforcerowsecurity
   FROM pg_class WHERE relname = 'bookings_booking';
   -- expect: t | t
   ```

2. Confirm both policies and the helper function exist:

   ```sql
   SELECT polname FROM pg_policy
   WHERE polrelid = 'bookings_booking'::regclass ORDER BY polname;
   -- expect: boundary_admin_bypass, boundary_tenant_isolation

   SELECT proname, proleakproof FROM pg_proc WHERE proname = 'boundary_current_tenant_id';
   -- expect: one row. proleakproof is f by default, or t if you set
   -- BOUNDARY_FUNCTION_LEAKPROOF = True on a superuser cluster.
   ```

3. Confirm isolation as a non-superuser role. With the tenant variable set, only that tenant's rows are visible:

   ```sql
   BEGIN;
   SELECT set_config('app.current_tenant_id', '<tenant-a-pk>', true);
   SELECT count(*) FROM bookings_booking;  -- only tenant A's rows
   COMMIT;
   ```

   With no tenant set, you should see zero rows:

   ```sql
   BEGIN;
   SELECT set_config('app.current_tenant_id', '', true);
   SELECT count(*) FROM bookings_booking;  -- 0
   COMMIT;
   ```

4. The system check `boundary.E006` flags tenant-scoped tables that are missing RLS. Run `python manage.py check` and confirm the table is no longer reported.

## Reversibility

All three operations are fully reversible:

- Reversing `EnableRLS` drops the boundary policies, then disables and unforces RLS on the table.
- Reversing `CreateTenantPolicy` drops both policies (it leaves the helper function in place, since it is shared across tables).
- Reversing `DropTenantPolicy` re-creates both policies.

Roll back with `python manage.py migrate bookings <previous_migration>` or `migrate bookings zero`.

Use `DropTenantPolicy` when you want to remove the policies but keep RLS enabled on the table (for example to replace them with custom policies), rather than reversing the whole migration.

## Common pitfalls

- **Running on a non-PostgreSQL database.** These operations emit PostgreSQL DDL and will fail elsewhere. Gate them behind a PostgreSQL-only migration, or only add them in deployments that use PostgreSQL.
- **Connecting as a superuser.** Superusers bypass RLS. Isolation will appear broken (all rows visible) until you switch to a non-superuser role.
- **`only superuser can define a leakproof function` on managed Postgres.** `LEAKPROOF` may only be declared by a superuser, which DigitalOcean, RDS, Cloud SQL, Azure, Heroku, and Supabase do not grant. This is off by default (`BOUNDARY_FUNCTION_LEAKPROOF = False`), so managed Postgres works out of the box; the optimisation only affects planner qualifier ordering, not isolation. Set `BOUNDARY_FUNCTION_LEAKPROOF = True` only on a self-managed cluster where the migrating role is a superuser.
- **Changing `BOUNDARY_DB_SESSION_VAR` or `BOUNDARY_ADMIN_FLAG_VAR` after the policies exist.** The names are baked into the policy SQL at migration time. If you change either setting later, re-run the policy migration so the database picks up the new name, otherwise the runtime variable and the policy disagree and isolation breaks.
- **Querying outside a transaction.** `set_config(..., true)` is transaction-scoped. Outside a transaction the tenant variable is not applied, so a non-superuser sees zero rows. `TenantMiddleware` and `TenantContext.using()` (which underlies `tenant_scoped`, `boundary_run`, `boundary_run_all`, and the Celery worker restoration) both open a transaction for you automatically when `BOUNDARY_WRAP_ATOMIC` is `True` (the default) and none is already active, so this normally cannot happen through boundary's own APIs. It remains a risk if you call `_set_db_session()` directly, disable `BOUNDARY_WRAP_ATOMIC`, or run raw SQL outside any of boundary's context helpers, in which case wrap the work in `transaction.atomic()` yourself.
- **Wrong `model_name`.** Pass the bare model name (`"Booking"`), not the app label or a dotted path. The wrong name raises a lookup error during migration.
- **Forgetting the `_id` suffix in `tenant_column`.** The argument is the database column name; for a foreign key that is `tenant_id`, `org_id`, and so on, not `tenant` or `org`.
- **Wrong ordering.** `CreateTenantPolicy` after `EnableRLS`, and both after the table and tenant FK column exist. Reorder or add a `dependencies` entry if the column is added in a later migration.

## Related

- [README: Row Level Security](../../README.md#row-level-security) for the operation reference and type-awareness details.
- [README: settings](../../README.md) for the full `BOUNDARY_` settings table, including `BOUNDARY_DB_SESSION_VAR`, `BOUNDARY_ADMIN_FLAG_VAR`, and `BOUNDARY_WRAP_ATOMIC`.
- [README: system checks](../../README.md) for `boundary.E006` (tenant-scoped table missing RLS).
