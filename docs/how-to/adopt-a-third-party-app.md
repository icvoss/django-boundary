# Adopt a third-party app into your tenancy

## Goal

Tenant-scope the concrete models an installed third-party app ships, when you
cannot add a boundary mixin to them. By the end, every table that app owns
carries a `tenant_id` column that PostgreSQL enforces with Row Level Security,
uniqueness on those tables is per tenant rather than global, and a system check
tells you when an upgrade to that package has drifted away from that state.

The adopted package is not modified, forked, or made aware of any of this. You
list its app in your settings and apply one migration operation from a migration
in your own app; the tenant column exists only in the database and is invisible
to Django's ORM.

This is the path for a package with no swappable abstract base. If the package
does ship one (`AbstractArticle` plus a settings key such as
`ICV_ARTICLES_ARTICLE_MODEL`), compose the mixin instead: see
[Scope a package's models into your tenancy](scope-a-packages-models.md). That
route gives you both isolation layers. This one gives you the database layer
only, permanently, and the limits section below states what that costs.

## Prerequisites

- PostgreSQL. Adoption is RLS-only, and SQLite and MySQL have no RLS, so an
  adopted table on either has no isolation at all and no ORM layer beneath it
  to compensate.
- django-boundary installed with `BOUNDARY_TENANT_MODEL` set and its tenant
  table migrated. See [Set up a tenant model](set-up-a-tenant-model.md).
- The third-party app installed and its own migrations already applied. Adoption
  alters tables that must already exist.
- An app of your own to hold the migration. The operation never goes in the
  third-party app's migrations, and you never relocate them with
  `MIGRATION_MODULES`.
- If the tables already hold rows, know which tenant those rows belong to. You
  will need its primary key.

## Steps

### 1. List the apps you want adopted

Two settings drive adoption, and both take effect at startup as well as at
migrate time.

```python
# settings.py
BOUNDARY_TENANT_APPS = ["account", "taggit"]
BOUNDARY_ADOPT_EXCLUDE = ["taggit.Tag"]
```

`BOUNDARY_TENANT_APPS` is a list of **app labels**, as
`apps.get_app_config()` resolves them, not dotted module paths. The app label
for `allauth.account` is `account`; for `django.contrib.auth` it is `auth`.

`BOUNDARY_ADOPT_EXCLUDE` is a list of `"app_label.ModelName"` strings exempted
from adoption, for a genuinely global reference table inside an otherwise
adopted app. Matching is exact and case-sensitive. An entry matching no model is
inert rather than an error, so an exclusion you wrote for a model a later
package version removes does not break your deployment.

You never list the models themselves. The adopted set for an app is **derived**:
every concrete model the app config returns, abstract and proxy models excluded,
auto-created many-to-many through models included, minus your exclusions, minus
any model you have already scoped with a boundary mixin or a path mixin, minus
the tenant model and its own through tables. Deriving it rather than hand-listing
it is what makes a model added by a later package version detectable.

Listing your **own** app is supported and useful: its mixin-scoped models keep
both isolation layers exactly as before, and any model in it that forgot its
mixin is reported until you either add the mixin or adopt the table.

### 2. Write the migration in your own app

```python
# myapp/migrations/0007_adopt_allauth_account.py
from django.db import migrations

from boundary.migrations_ops import AdoptTenantApp


class Migration(migrations.Migration):
    dependencies = [
        ("myapp", "0006_previous"),
        ("account", "0001_initial"),
    ]

    operations = [
        AdoptTenantApp("account"),
    ]
```

Depend on the adopted app's own migrations, so its tables exist before the
operation alters them.

The operation takes three arguments:

| Argument | Type | Required | Default | Meaning |
|---|---|---|---|---|
| `app_label` | `str` | Yes | none | The app whose models this operation adopts. |
| `exclude` | sequence of `"app_label.ModelName"` | No | `()` | Models exempted in addition to `BOUNDARY_ADOPT_EXCLUDE`. |
| `backfill_tenant` | a tenant primary key (`uuid.UUID`, `int`, `str`), or `None` | No | `None` | The tenant every pre-existing row is stamped with. Required for a populated table. |

The operation derives its adopted set from the **historical migration state**,
not from the app registry as it stands today, so applying this migration to a
fresh database years later produces the schema it described when it was written
rather than the schema today's installed version would describe.

It adds nothing to Django's migration state. The column and the rewritten
indexes exist only in the database, which is exactly what keeps the third-party
app's own `makemigrations` output unchanged.

### 3. Choose between an empty table and a populated one

**An empty table needs nothing extra.** The column is added in one statement, as
`NOT NULL DEFAULT boundary_current_tenant_id()`, which is cheap and equivalent
on zero rows.

**A populated table is refused** unless you name the tenant its rows belong to.
The refusal names the table and its row count. This is deliberate: PostgreSQL
evaluates `ADD COLUMN ... DEFAULT` **once** for pre-existing rows, so a one-shot
default would stamp every existing row with whichever tenant happened to be
active at migration time, which during `migrate` is normally no tenant at all.

```python
operations = [
    AdoptTenantApp("account", backfill_tenant="0f8b...c31e"),
]
```

With a backfill tenant given, the column is added nullable, every row is updated
to that tenant, and only then is the column made `NOT NULL` with its default.
The `UPDATE`'s affected-row count must equal the count the refusal probe read;
if it does not, the operation raises naming both counts and the table, because a
difference means some rows' tenant assignment is not what the migration file
says it is.

`backfill_tenant` applies to **every** table the operation adopts in that call.
If different tables belong to different tenants, write one operation per table
using `exclude`, so each stamped assignment is visible in the migration file
rather than inferred.

boundary does not verify that the value names an existing tenant row: under
regional routing the tenant table may live on another database alias. A wrong
value silently assigns every pre-existing row to a tenant that may not exist.

### 4. Know what the operation rewrites, and what it refuses

Per adopted table, the operation adds the `tenant_id` column (typed from your
tenant model's primary key: `uuid` for a UUID key, `bigint` for an integer one;
any other key type is refused by name rather than guessed),
rewrites the unique constraints, enables and forces Row Level Security, and
creates the same two policies a mixin-scoped table gets. From PostgreSQL's side
an adopted table is indistinguishable from a column-bearing one.

**Every unique constraint on the table becomes composite, leading with
`tenant_id`.** This is what makes uniqueness per tenant instead of global, and
it covers all four ways Django produces one: a field declared `unique=True`,
`Meta.unique_together`, an unconditional `UniqueConstraint` in
`Meta.constraints`, and the implicit constraint on an auto-created many-to-many
through table's column pair. The primary key is not rewritten. The package's own
`unique=True` form validation keeps working: its lookup is scoped by RLS to the
active tenant, and the composite index agrees with that scope, so two tenants may
hold the same value and neither sees the other's.

The operation reads the PostgreSQL catalogue to find what it is rewriting rather
than guessing constraint names, and **five forms make it refuse outright**,
naming the constraint or index and the model:

| Form | Why it is refused |
|---|---|
| A unique constraint that a foreign key targets (what `ForeignKey(to_field=...)` creates) | Dropping it would invalidate the referencing constraint, and the composite replacement cannot satisfy it |
| A partial unique index (one with a condition) | Its predicate cannot safely be re-expressed against the added column |
| An expression index | Same reason: the definition cannot be reproduced safely |
| A deferrable constraint | Cannot be reproduced safely |
| A bare unique index backing no constraint (a `models.Index` with `unique=True`, or one created by hand) | Replacing an index with a composite UNIQUE constraint is not an equivalent rewrite, and your next `makemigrations` would not recognise the result |

Refusing is the point. A unique constraint skipped silently stays globally
unique, which is precisely the cross-tenant collapse this whole mechanism exists
to prevent. Your answer in each case is to exclude that model, via
`BOUNDARY_ADOPT_EXCLUDE` or the operation's own `exclude`, or to write the
rewrite by hand in your own migration.

The operation checks every table in the set before it issues any statement, so
a refusal on one table means nothing was changed on any table. The migration's
own transaction is the backstop behind that, not the mechanism: a failure
mid-way through the DDL is rolled back with it. It is idempotent per table: a table that already carries a `tenant_id`
column of the expected type is skipped unchanged, which is what makes a second
`AdoptTenantApp` migration after a package upgrade adopt only the newly added
tables. A table carrying a `tenant_id` column of a **different** type is refused,
because boundary cannot tell its own column from one the app genuinely declares.

The operation also honours your database router. When `allow_migrate` says no
for the alias being migrated, it issues nothing there and raises nothing, the
same way Django's own `RunSQL` behaves; that is the intended way to keep a
SQLite or MySQL alias out of the picture. When the router allows an alias that
is not PostgreSQL, the operation refuses by naming the vendor rather than
sending DDL that backend cannot run.

### 5. Know which apps cannot be adopted

Adoption is refused, with an error rather than a warning, for all of the
following, because each is global by construction:

- the `contenttypes` app
- the `sessions` app
- the `sites` app
- `auth.Permission`
- `admin.LogEntry`
- the `django_migrations` table
- the model named by `BOUNDARY_TENANT_MODEL` (or its `ICV_TENANT_MODEL`
  fallback), and that model's auto-created many-to-many through tables

Scoping `contenttypes` or `django_migrations` breaks Django's own bootstrap.
Scoping `sessions` or `sites` breaks request handling before a tenant has been
resolved. Scoping the tenant model itself, or a through table keyed to it, makes
the tenant row unreachable whenever no tenant is active, which is every
resolution attempt.

**The deny-list covers the tenant model, not the app that owns it.** A tenants
app commonly holds membership, settings and invitation models beside the tenant
model, and those are ordinary tenant-scoped data. Listing that app is supported;
the tenant model and its through tables are simply skipped from the derived set,
the way an excluded model is.

The refusal applies at migrate time and again at startup: a deny-listed app
label in `BOUNDARY_TENANT_APPS` is reported before any migration runs, as is an
app label naming nothing installed.

### 6. Give every bootstrap path a tenant context

A write to an adopted table with no active tenant fails closed:
`boundary_current_tenant_id()` returns `NULL`, the `NOT NULL` constraint rejects
the row, and the caller sees an `IntegrityError`. That is correct behaviour, and
it must not be softened by making the column nullable or relaxing the policy.

**`admin_bypass()` is not the remedy.** The failure is a `NOT NULL` constraint
rejecting a `NULL` the column default produced, and a constraint is evaluated
before any policy is consulted. The bypass flag changes which rows the bypass
policy admits; it has no bearing on what `boundary_current_tenant_id()` returns,
so an ORM write inside `admin_bypass()` with no tenant active still raises
`IntegrityError`.

**The remedy is `TenantContext.using(tenant)`** for the tenant the rows belong
to. Inside it the session variable is set, the column default stamps that
tenant, and the isolation policy's write check is satisfied with no bypass at
all.

These are the paths that run without a tenant, and therefore need your attention
once an app is adopted:

| Path | Why it runs without a tenant | Remedy |
|---|---|---|
| `createsuperuser` | An interactive management command, no request and no resolved tenant | Run it inside `TenantContext.using(tenant)`, or wrap it, if `auth` is adopted |
| `loaddata` | Fixtures load outside any request cycle | Run it inside `TenantContext.using(tenant)` for the tenant the fixture belongs to |
| `post_migrate` handlers | Run at the end of `migrate`, before any request or resolver, with no call site for you to wrap | None |
| An adopted app's own data migrations | Run inside `migrate`, in the same no-tenant state | None |

**The last two have no clean remedy, and this page states that rather than
implying a wrapper exists.** An app whose data migration inserts seed rows
cannot be adopted as it stands. Either exclude that model with
`BOUNDARY_ADOPT_EXCLUDE`, leaving its table global, or seed the rows per tenant
from your own code inside `TenantContext.using(tenant)`.

Django's own two shipped `post_migrate` handlers write only `auth.Permission`
and `contenttypes.ContentType`, both deny-listed, so neither is affected unless
you adopt `auth` itself. `auth` is adoptable, since only `auth.Permission` is
deny-listed rather than the whole app, but adopting it means both
`createsuperuser` and the permission-creation handler need a tenant context and
the handler has nowhere to get one. That is a decision to make and document for
your own deployment. boundary neither refuses `auth` nor wraps anything on your
behalf.

`admin_bypass()` remains the right tool for exactly two other cases: a
cross-tenant **read**, and **raw SQL that supplies `tenant_id` explicitly**,
which is how `boundary_deprovision` reaches adopted tables.

### 7. Understand what reversing costs before you need it

`AdoptTenantApp` is reversible. Reversing drops both policies, disables and
un-forces RLS, restores each unique constraint it rewrote to its original global
definition, and drops the `tenant_id` column. It leaves the
`boundary_current_tenant_id()` helper in place, since other tables' policies may
still depend on it.

**Reversing destroys the tenant assignment of every row on the adopted tables.**
The assignment lives only in the dropped column, and boundary does not snapshot
it. Re-applying the migration forwards against the now-populated table is refused
unless you supply a `backfill_tenant`, and no single backfill value can recover
the per-row assignment the reversal discarded.

**Reversing strips every adopted table of that app it can see, not only the
ones this migration adopted.** The operation carries no record of which
migration adopted which table, so if a second `AdoptTenantApp("account")`
migration was added after a package upgrade to pick up new models, reversing
that second migration also un-adopts the tables the first one adopted. Reverse
the later migration only when you mean to un-adopt the app, and re-apply both
if you did not.

Restoration re-derives rather than replays: the operation reads each model's
historical `_meta` from the migration state and recreates the original unique
constraints through the schema editor's own naming, so what comes back is what
Django would generate for that model at that migration state.

## Verify it worked

**Run the system check.** `boundary.E007` derives the expected adopted set from
the live app registry and reports, per table, a missing or wrongly typed
`tenant_id` column, Row Level Security that is not both enabled and forced, a
missing isolation or admin-bypass policy, and any non-primary-key unique
constraint that is not composite leading with `tenant_id`. It also reports a
deny-listed or uninstalled app label in your settings.

```bash
python manage.py check
```

A clean run is the signal that the DDL is in place on every table boundary
expects to be adopted. `boundary.E007` is an Error rather than a Warning because
of one condition in particular: an upstream migration that drops and recreates an
index replaces your composite constraint with a global one, restoring
cross-tenant uniqueness with no other signal anywhere in the system. Run
`check` before you deploy an upgrade to an adopted package.

If `boundary.E007` reports a missing column on a model you have never seen
before, the upstream package added it after your adoption migration was written.
That is the intended signal, not a false positive. The fix is a second
`AdoptTenantApp` migration for the same app, which adopts only the new tables
because already-adopted ones are skipped.

If the check reports `boundary.W007` instead, the probe itself failed on a live
connection, so read it as "could not determine", never as a pass.

**Assert enforcement in your tests.** `assert_rls_enforced()` from
`boundary.testing` includes adopted tables in the tables it inspects. This is
where it matters most: an adopted table has no ORM layer to fall back on, so a
suite running as a `BYPASSRLS` role, or against a database where the adoption
DDL never applied, exercises no isolation at all and still passes every test.
See [Write tenant-safe tests](write-tenant-safe-tests.md).

**Check isolation directly.** With rows created under two tenants, a read inside
one tenant's context returns only that tenant's rows, even though the adopted
model has no boundary manager of any kind:

```python
from boundary.context import TenantContext
from allauth.account.models import EmailAddress

with TenantContext.using(tenant_a):
    EmailAddress.objects.create(user=user_a, email="a@example.com")
with TenantContext.using(tenant_b):
    EmailAddress.objects.create(user=user_b, email="b@example.com")

with TenantContext.using(tenant_a):
    assert EmailAddress.objects.count() == 1
```

The filtering there is done entirely by PostgreSQL. Nothing in the ORM knows the
column exists.

## Limits you are accepting

Adoption buys one isolation layer, not two, and that asymmetry is permanent by
design rather than a gap scheduled to be closed.

- **No ORM filtering, ever.** The model declares no tenant field, so no manager
  can filter on one. `is_tenant_model()`, `has_tenant_column()`,
  `get_tenant_lookup()` and `get_tenant_fk_field()` all keep returning
  `False`/`None` for an adopted model. Every ORM-layer rule boundary documents
  for mixin-scoped models is simply inapplicable here.
- **No strict mode.** A missing tenant context surfaces as an `IntegrityError`
  on write or an empty result on read, never as `TenantNotSetError`, and fires no
  `strict_mode_violation` signal. A read with no tenant returns zero rows
  silently.
- **No isolation on a non-PostgreSQL backend.** SQLite and MySQL have no RLS, so
  an adopted table on either is unisolated, with nothing underneath it to
  compensate. Tests that exercise adopted-app tenancy must run against real
  PostgreSQL.
- **No cross-tenant foreign key validation.** `validate_cross_tenant_fks()`
  walks declared `ForeignKey` fields and compares against a declared local tenant
  column. An adopted model has neither, so it inspects nothing and raises
  nothing.

Everything that remains is enforced by the database, which is why
`boundary.E007` and a real-PostgreSQL test suite carry the whole burden here.

## Related

- [Scope a package's models into your tenancy](scope-a-packages-models.md): the
  mixin route, for a package that ships a swappable abstract base. Prefer it
  where it is available.
- [Isolation layers and the threat model](../explanation/isolation-layers.md):
  why the two layers exist and what each catches, including the adopted-table
  asymmetry.
- [Run cross-tenant admin operations](cross-tenant-admin-operations.md): what
  `admin_bypass()` is and is not for.
- [Write tenant-safe tests](write-tenant-safe-tests.md): `assert_rls_enforced()`
  and the real-PostgreSQL requirement.
- [Provision and deprovision tenants](provision-and-deprovision-tenants.md):
  how deprovision reaches adopted tables.
- [Add RLS policies with migrations](add-rls-policies-with-migrations.md): the
  single-table operations, and their `app_label` override.
- [Settings reference](../reference/settings.md): `BOUNDARY_TENANT_APPS` and
  `BOUNDARY_ADOPT_EXCLUDE` in full.
