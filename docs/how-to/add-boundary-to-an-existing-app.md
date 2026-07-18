# Add boundary to an existing app

## Goal

Retrofit tenant isolation onto a Django app that already has models and live data. By the end, your existing models are tenant-scoped, every existing row is assigned to a tenant, the middleware resolves the tenant per request, and PostgreSQL Row Level Security enforces isolation at the database level.

The order matters. Adding a non-nullable FK to a populated table will fail unless you stage it correctly, and turning on strict mode or RLS before every row has a tenant will break reads. This guide does it in a safe sequence:

1. Install and configure (no behaviour change yet).
2. Add the tenant FK as nullable.
3. Backfill the tenant on existing rows with a data migration.
4. Make the FK non-nullable.
5. Turn on the middleware.
6. Add RLS last.

## Prerequisites

- Django 5.1+ and PostgreSQL 14+ (the ORM layer works on any database, RLS needs PostgreSQL).
- An existing app with models and data you want to isolate.
- A tenant model, or the ability to add one. See [choosing a tenant model](#step-2-choose-and-configure-a-tenant-model) below.
- A clear answer to: which tenant does each existing row belong to? You cannot backfill without a rule. If all current data belongs to one tenant (the common single-to-multi-tenant case), that rule is trivial. If not, you need a column or join that identifies the owner.

## Steps

### Step 1: Install

```bash
pip install django-boundary
```

Add the app to `INSTALLED_APPS`:

```python
# settings.py
INSTALLED_APPS = [
    # ...
    "boundary",
    # ...
]
```

### Step 2: Choose and configure a tenant model

The tenant model is the entity each row belongs to: organisation, workspace, merchant, club, school. You have two options.

**Option A: subclass `AbstractTenant`** for the common fields (`name`, `slug`, `region`, `is_active`, `created_at`, `updated_at`):

```python
# tenants/models.py
from django.db import models
from boundary.models import AbstractTenant


class Organisation(AbstractTenant):
    plan = models.CharField(max_length=50, default="free")
```

**Option B: point `BOUNDARY_TENANT_MODEL` at any existing model** you already have. Boundary makes no assumptions about its fields, though the middleware will honour an `is_active` field (rejecting inactive tenants with a 403) and the `SubdomainResolver` looks up the field named by `BOUNDARY_SUBDOMAIN_FIELD` (default `slug`).

Configure the model and keep strict mode off for now so existing code keeps working while you migrate:

```python
# settings.py
BOUNDARY_TENANT_MODEL = "tenants.Organisation"

# Off during retrofit. Turn on once the backfill is done and middleware is live.
BOUNDARY_STRICT_MODE = False
```

If your domain calls the relationship something other than `tenant` (for example `merchant`), set it globally now so every later step uses that name:

```python
# settings.py: optional, only if you want a non-"tenant" FK name
BOUNDARY_TENANT_FK_FIELD = "merchant"
```

Make and run the migration for the tenant model itself (skip if you reused an existing model):

```bash
python manage.py makemigrations tenants
python manage.py migrate
```

Create at least one tenant to backfill into. From a shell or a migration:

```python
Organisation.objects.create(name="Acme", slug="acme")
```

### Step 3: Add the tenant mixin to an existing model, FK nullable

This is the step that breaks naive retrofits. Adding `TenantModel`/`TenantMixin` directly gives you a non-nullable FK (`null=False`), and Django cannot add a non-nullable column to a table that already has rows without a default. So add the FK as **nullable** first.

Use `make_tenant_mixin()` with `null=True` rather than the built-in `TenantModel`, because `TenantModel` always uses `null=False`:

```python
# bookings/models.py
from django.db import models
from boundary.models import make_tenant_mixin

# Nullable for the retrofit. Same default field name ("tenant") as TenantModel.
NullableTenantMixin = make_tenant_mixin(null=True)


class Booking(NullableTenantMixin):
    court = models.IntegerField()
    start_time = models.DateTimeField()
    is_paid = models.BooleanField(default=False)
```

If you set `BOUNDARY_TENANT_FK_FIELD = "merchant"` in step 2, `make_tenant_mixin(null=True)` reads that setting and names the field `merchant` automatically. To name it explicitly regardless of the setting, pass it: `make_tenant_mixin("merchant", null=True)`.

The mixin also wires up the managers and auto-populate behaviour: `Booking.objects` is a `TenantManager` (auto-filtering), `Booking.unscoped` is an `UnscopedManager` (returns all rows regardless of context), and `save()`/`bulk_create()` auto-populate the FK from the active tenant when it is not set.

Generate the schema migration:

```bash
python manage.py makemigrations bookings
```

This produces an `AddField` for a nullable FK, which applies safely to a populated table. Do not run it on its own yet. Add the backfill in the next step so they apply together.

### Step 4: Backfill the tenant on existing rows (data migration)

Existing rows now have `tenant_id = NULL`. Assign each one a tenant before you make the column non-nullable.

The critical detail: inside the migration, query through the **historical model** and use the `unscoped` manager. The default `objects` manager auto-filters by the active tenant, and during a migration there is no tenant in context, so `objects` would either return nothing or raise (in strict mode). `unscoped` bypasses filtering and sees every row.

`make_tenant_mixin` and `TenantMixin` register `unscoped` on the model, but historical models in migrations are reconstructed from migration state and only carry the default manager. So resolve the **concrete** model from the app registry inside the migration and call `unscoped` on it. The simplest reliable rule for a single-to-multi-tenant migration is to assign every NULL row to one tenant:

```python
# bookings/migrations/0002_backfill_tenant.py
from django.db import migrations


def backfill_tenant(apps, schema_editor):
    # Resolve the CONCRETE model so the `unscoped` manager is available.
    # Historical models from apps.get_model() do not carry custom managers.
    from django.apps import apps as live_apps

    Booking = live_apps.get_model("bookings", "Booking")
    Organisation = live_apps.get_model("tenants", "Organisation")

    default_tenant = Organisation.objects.get(slug="acme")

    # unscoped bypasses tenant filtering, so it sees rows with tenant_id = NULL.
    Booking.unscoped.filter(tenant__isnull=True).update(tenant=default_tenant)


def reverse(apps, schema_editor):
    from django.apps import apps as live_apps

    Booking = live_apps.get_model("bookings", "Booking")
    Booking.unscoped.update(tenant=None)


class Migration(migrations.Migration):

    dependencies = [
        ("bookings", "0001_add_nullable_tenant"),  # the AddField from step 3
        ("tenants", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(backfill_tenant, reverse),
    ]
```

Notes on the backfill:

- `update()` is a single bulk SQL statement and does not call `save()`, so there is no per-row auto-populate or signal overhead. This is what you want for a backfill over many rows.
- If different rows belong to different tenants, replace the single `update()` with logic that maps each row to its owner, for example joining on an existing `owner_id` or legacy column. Keep using `unscoped` so you can see and write every row.
- The custom FK name applies here too. If your field is `merchant`, filter on `merchant__isnull=True` and update `merchant=...`.
- Using `live_apps.get_model(...)` (the live registry) rather than the migration's `apps` argument is what gives you the real `unscoped` manager. The trade-off is that this migration reflects the current model definition, so run it promptly rather than leaving it unapplied across many later model changes.

### Step 5: Make the FK non-nullable

Every row now has a tenant. Switch the model to the standard, non-nullable base. For the default field name, use `TenantModel`:

```python
# bookings/models.py
from django.db import models
from boundary.models import TenantModel


class Booking(TenantModel):
    court = models.IntegerField()
    start_time = models.DateTimeField()
    is_paid = models.BooleanField(default=False)
```

For a custom field name, drop the `null=True` you passed earlier:

```python
MerchantMixin = make_tenant_mixin("merchant")  # null defaults to False


class Product(MerchantMixin):
    sku = models.CharField(max_length=50)
```

Generate and apply the `AlterField` that flips the column to `NOT NULL`:

```bash
python manage.py makemigrations bookings
python manage.py migrate
```

This succeeds because there are no NULL values left. If it fails with a not-null constraint error, the backfill missed some rows. Roll back, fix the backfill rule, and retry.

### Step 6: Turn on the middleware

Add `TenantMiddleware` so each request resolves a tenant and sets the context. Place it early, after `SecurityMiddleware` but before session and auth middleware:

```python
# settings.py
MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "boundary.middleware.TenantMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    # ...
]
```

Choose a resolver. The default is `SubdomainResolver`. For an existing app that is not subdomain-based, pick the one that matches how you identify tenants:

```python
# settings.py
BOUNDARY_RESOLVERS = [
    "boundary.resolvers.SubdomainResolver",  # acme.app.com -> slug lookup
]
```

See the [resolvers table in the README](../../README.md#resolvers) for `HeaderResolver`, `JWTClaimResolver`, `SessionResolver`, and `ExplicitResolver`, plus the security note on ordering.

During the transition you may want `BOUNDARY_REQUIRED = False` so requests that do not resolve a tenant (health checks, public pages) are not turned into 404s. Once every entry point resolves a tenant, you can return it to the default `True`.

Now turn strict mode back on so any query that runs without a tenant raises instead of silently leaking across tenants:

```python
# settings.py
BOUNDARY_STRICT_MODE = True
```

With strict mode on, code paths that query tenant-scoped models outside a request (management commands, Celery tasks, scripts) must establish a tenant explicitly:

```python
from boundary.context import TenantContext

with TenantContext.using(organisation):
    Booking.objects.all()  # filtered to this organisation
```

### Step 7: Add RLS last

RLS is the database-level second layer. Add it only after the FK is non-nullable, the data is backfilled, and the app behaves correctly with ORM filtering. Enabling RLS on a table whose rows do not all have a tenant, or before the session variable is being set, will make rows disappear from reads.

Create an empty migration and add the boundary operations:

```bash
python manage.py makemigrations bookings --empty --name enable_rls
```

```python
# bookings/migrations/0004_enable_rls.py
from django.db import migrations
from boundary.migrations_ops import EnableRLS, CreateTenantPolicy


class Migration(migrations.Migration):

    dependencies = [
        ("bookings", "0003_make_tenant_non_nullable"),
    ]

    operations = [
        EnableRLS("Booking"),
        CreateTenantPolicy("Booking"),
    ]
```

`CreateTenantPolicy` defaults the tenant column to `tenant_id`. If you used a custom FK name, pass the column explicitly:

```python
CreateTenantPolicy("Product", tenant_column="merchant_id")
```

Apply it:

```bash
python manage.py migrate
```

`CreateTenantPolicy` creates a helper function that casts the PostgreSQL session variable to the right type (it detects UUID vs integer primary keys), an isolation policy with `USING` and `WITH CHECK`, and an admin bypass policy for management commands. The helper is declared `LEAKPROOF` only when `BOUNDARY_FUNCTION_LEAKPROOF` is set (default off, because `LEAKPROOF` needs a superuser that managed Postgres does not grant). Both operations are reversible via `migrate --reverse`.

The session variable is set by `TenantContext` (which the middleware drives) within a transaction. By default `BOUNDARY_WRAP_ATOMIC = True` wraps each request in `transaction.atomic()` so the session variable takes effect. Keep that on, or enable `ATOMIC_REQUESTS` on the database, otherwise the per-transaction session variable has no scope to apply to.

## Verify it worked

1. **Schema is correct.** The FK column is `NOT NULL` and no rows are orphaned:

   ```bash
   python manage.py shell -c "from bookings.models import Booking; print(Booking.unscoped.filter(tenant__isnull=True).count())"
   # Expect: 0
   ```

2. **ORM filtering isolates tenants.** Two tenants should not see each other's rows:

   ```python
   from boundary.context import TenantContext
   from bookings.models import Booking

   with TenantContext.using(org_a):
       a_count = Booking.objects.count()

   with TenantContext.using(org_b):
       b_count = Booking.objects.count()

   total = Booking.unscoped.count()
   assert a_count + b_count <= total  # each context sees only its own rows
   ```

3. **Strict mode catches unscoped access.** With `BOUNDARY_STRICT_MODE = True` and no tenant in context, a query raises:

   ```python
   from boundary.exceptions import TenantNotSetError
   import pytest

   with pytest.raises(TenantNotSetError):
       Booking.objects.count()
   ```

4. **The model is recognised as tenant-scoped:**

   ```python
   from boundary.models import is_tenant_model, get_tenant_fk_field
   assert is_tenant_model(Booking)
   assert get_tenant_fk_field(Booking) == "tenant"  # or "merchant" for a custom FK
   ```

5. **RLS enforces at the database.** Set the session variable to one tenant in `psql` and confirm a raw `SELECT` returns only that tenant's rows:

   ```sql
   SELECT set_config('app.current_tenant_id', '<tenant-pk>', false);
   SELECT count(*) FROM bookings_booking;  -- only that tenant's rows
   ```

   The session variable name is `BOUNDARY_DB_SESSION_VAR` (default `app.current_tenant_id`).

Run `python manage.py check` to confirm the system checks pass. `boundary.E004` flags a missing middleware, `boundary.E006` flags a tenant-scoped table without RLS, and `boundary.W001` warns if strict mode is off.

## Common pitfalls

- **Adding `TenantModel` directly to a populated table.** It uses `null=False`, so the migration fails. Add the FK nullable first via `make_tenant_mixin(null=True)`, backfill, then switch.
- **Using `objects` in the backfill.** The default manager auto-filters by the active tenant. During a migration there is no tenant, so it returns nothing or raises in strict mode. Always backfill through `unscoped`.
- **Calling `unscoped` on the migration's historical model.** `apps.get_model()` inside `RunPython` returns a state-reconstructed model without custom managers. Resolve the concrete model from the live app registry (`from django.apps import apps as live_apps`) to get `unscoped`.
- **Turning on strict mode or RLS before the backfill.** Strict mode breaks every unscoped read; RLS hides rows that have no tenant or are read without the session variable set. Do both last.
- **Looping `save()` over rows to backfill.** That triggers per-row auto-populate and signals. Use a single `update()` (or `bulk_update`) instead.
- **Forgetting the custom column in `CreateTenantPolicy`.** It defaults to `tenant_id`. With a `merchant` FK the column is `merchant_id`; pass `tenant_column="merchant_id"` or the policy targets a column that does not exist.
- **RLS with `BOUNDARY_WRAP_ATOMIC = False` and no `ATOMIC_REQUESTS`.** The session variable is transaction-scoped (`set_config(..., true)`), so without a surrounding transaction it never applies and RLS hides everything.

## Related

- [README: Models, `make_tenant_mixin`, custom FK names, and full settings reference](../../README.md#models)
- [README: Resolvers](../../README.md#resolvers)
- [README: Row Level Security](../../README.md#row-level-security)
- [README: Testing utilities (`set_tenant`, `tenant_factory`, `TenantTestMixin`)](../../README.md#testing)
