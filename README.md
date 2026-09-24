<p align="center">
  <img src="https://raw.githubusercontent.com/icvoss/django-boundary/main/.github/logo.svg" alt="" width="88">
</p>

# django-boundary

[![CI](https://github.com/icvoss/django-boundary/actions/workflows/ci.yml/badge.svg)](https://github.com/icvoss/django-boundary/actions/workflows/ci.yml)
[![PyPI version](https://img.shields.io/pypi/v/django-boundary.svg)](https://pypi.org/project/django-boundary/)
[![Python versions](https://img.shields.io/pypi/pyversions/django-boundary.svg)](https://pypi.org/project/django-boundary/)
[![Django versions](https://img.shields.io/pypi/djversions/django-boundary.svg)](https://pypi.org/project/django-boundary/)
[![Licence: MIT](https://img.shields.io/badge/Licence-MIT-blue.svg)](https://opensource.org/licenses/MIT)

Scalable row-level multi-tenancy for Django with PostgreSQL Row Level Security.

---

## Who Is This For?

django-boundary is for Django projects that serve multiple tenants from a
single database. If your users belong to organisations, workspaces, teams,
schools, clinics, clubs, or any other entity that should only see its own
data, boundary handles the isolation.

### Common use cases

**SaaS platforms**: Each customer (organisation, workspace, account) is a
tenant. Their data is isolated at the ORM and database level. New tenants are
provisioned via management command; no schema migrations required.

**Marketplace platforms**: Sellers, venues, or merchants each have their own
tenant. Products, orders, and analytics are scoped per-tenant. Platform-wide
reporting uses the `unscoped` manager.

**Education / healthcare / government**: Schools, clinics, or departments are
tenants. Data residency requirements are met via regional routing (e.g. UK data
stays in UK database, EU data in EU database).

**Agency or white-label products**: Each client gets their own tenant, resolved
by subdomain (`client-a.app.com`) or JWT claim from the auth provider.

**Internal tools**: Departments or business units are tenants, resolved via
session or header. `STRICT_MODE` catches accidental cross-department data
exposure during development.

### What boundary does not do

- **Authentication, session management or login.** That is your own auth
  stack. boundary's resolvers read an already-authenticated request; they
  never authenticate one.
- **Membership, RBAC or authorisation.** Resolving a tenant is not
  authorising a caller against it. Your project, or `icv-tenants` for ICV
  ecosystem consumers, owns that check.
- **Providing the tenant domain model itself.** You define your own tenant
  model and point `BOUNDARY_TENANT_MODEL` at it, or use the bundled
  `AbstractTenant` convenience base.
- **Non-PostgreSQL Row Level Security.** The ORM filtering layer works on
  any Django-supported database, but RLS enforcement requires PostgreSQL
  14+, with no database-level backstop elsewhere. The RLS migration
  operations are a logged no-op there rather than an error, so the same
  migration files run on a SQLite development database.
- **Cross-region data migration.** Moving a tenant's data from one regional
  database to another is your own operational tooling.
- **Frontend, API or Django admin components.** You wire your own admin and
  views to use the `unscoped` manager or tenant-filtered querysets.

### When NOT to use boundary

- **Single-tenant apps**: no need for isolation machinery.
- **Schema-per-tenant**: use [django-tenants](https://github.com/django-tenants/django-tenants) instead (different trade-offs at scale).

---

## Features

- **Automatic ORM filtering**: queries are scoped to the active tenant by default
- **PostgreSQL RLS**: database-level enforcement as a second layer of defence
- **Async-native**: context propagation via `contextvars`, works with sync and async Django
- **Pluggable resolvers**: subdomain, header, JWT claim, session, or custom
- **Strict mode**: raises on unscoped queries (default: on), catches data leaks at development time
- **Regional routing**: route queries to geographically distinct databases for data residency compliance
- **Celery integration**: tenant context propagated via task headers, restored on workers
- **Management commands**: provision, deprovision (with NDJSON export), scoped run, run-all with parallelism
- **Test utilities**: `set_tenant()`, `TenantTestMixin`, `tenant_factory()`
- **System checks**: validates configuration at startup
- **Optional LEAKPROOF RLS functions**: opt-in planner optimisation (off by default so RLS migrations run on managed Postgres)
- **Zero assumptions**: no opinion on auth, URL structure, or domain model

---

## Installation

```bash
pip install django-boundary
```

Add to `INSTALLED_APPS`:

```python
INSTALLED_APPS = [
    ...
    "boundary",
    ...
]
```

---

## Quick Start

### 1. Define your tenant model

```python
# tenants/models.py
from boundary.models import AbstractTenant

class Organisation(AbstractTenant):
    # Inherits: name, slug, region, is_active, created_at, updated_at
    plan = models.CharField(max_length=50, default="free")
```

### 2. Configure settings

```python
# settings.py
BOUNDARY_TENANT_MODEL = "tenants.Organisation"
BOUNDARY_STRICT_MODE = True  # default: raises on unscoped queries

# Resolver chain: first match wins.
# For public-facing apps, SubdomainResolver should be first.
BOUNDARY_RESOLVERS = [
    "boundary.resolvers.SubdomainResolver",
]
```

### 3. Add middleware

```python
MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "boundary.middleware.TenantMiddleware",  # before session/auth
    "django.contrib.sessions.middleware.SessionMiddleware",
    ...
]
```

### 4. Make models tenant-scoped

```python
# bookings/models.py
from boundary.models import TenantModel

class Booking(TenantModel):
    court = models.IntegerField()
    start_time = models.DateTimeField()
```

`Booking.objects.all()` now automatically filters by the active tenant.
Creating a booking auto-populates the `tenant` field from context. That is
the ORM layer. Add the database layer before you ship.

### 5. Turn on Row Level Security (PostgreSQL)

Write the migration by hand, in the app owning the model
(`python manage.py makemigrations bookings --empty --name rls`):

```python
from boundary.migrations_ops import CreateTenantPolicy, EnableRLS

operations = [
    EnableRLS("Booking"),
    CreateTenantPolicy("Booking"),
]
```

Apply it, then confirm `python manage.py check` reports neither
`boundary.E006` nor `boundary.W003`. Connect as a `NOSUPERUSER NOBYPASSRLS`
role: superusers and BYPASSRLS roles are exempt from every policy, so the
layer exists but enforces nothing for them.

**The same migration runs on SQLite.** `EnableRLS`, `CreateTenantPolicy` and
`DropTenantPolicy` apply on PostgreSQL and are a logged no-op on any other
backend: one `logger.info` line each on the `boundary.migrations` logger
naming the operation, the model, the alias and the vendor, then no DDL and no
error. So you write this migration once and run it unchanged against a SQLite
development database and a PostgreSQL production one, with no backend
conditional and no router. Your model keeps its ORM-layer tenant filtering on
SQLite; it is the RLS layer, and only that, which is absent there.

`AdoptTenantApp` is the one operation that still refuses off PostgreSQL,
because an adopted table has no ORM layer beneath the policy and would be
left with no isolation at all rather than reduced isolation. See
[Add RLS policies with migrations](docs/how-to/add-rls-policies-with-migrations.md)
for the migrating-role caveats and verification.

---

## Example Configurations

### SaaS with subdomain routing

Each customer gets a subdomain: `acme.app.com`, `globex.app.com`.

```python
# models.py
class Workspace(AbstractTenant):
    plan = models.CharField(max_length=20, default="starter")
    max_users = models.IntegerField(default=5)

class Project(TenantModel):
    name = models.CharField(max_length=200)

class Task(TenantModel):
    project = models.ForeignKey(Project, on_delete=models.CASCADE)
    title = models.CharField(max_length=200)
    completed = models.BooleanField(default=False)

# settings.py
BOUNDARY_TENANT_MODEL = "core.Workspace"
BOUNDARY_RESOLVERS = ["boundary.resolvers.SubdomainResolver"]
```

```python
# In a view: no tenant filtering needed, it's automatic
def dashboard(request):
    projects = Project.objects.all()  # only this workspace's projects
    tasks = Task.objects.filter(completed=False)  # only this workspace's tasks
    return render(request, "dashboard.html", {"projects": projects, "tasks": tasks})
```

### API with JWT-based tenancy

A React/mobile frontend sends a JWT containing the tenant ID. Useful for
single-page apps where subdomains aren't practical.

```python
# settings.py
BOUNDARY_TENANT_MODEL = "accounts.Account"
BOUNDARY_RESOLVERS = [
    "boundary.resolvers.JWTClaimResolver",  # reads tenant_id from JWT
]
BOUNDARY_JWT_CLAIM = "org_id"  # custom claim name
```

The JWT is validated by your auth middleware (DRF, django-allauth, etc.).
Boundary only reads the claim; it never validates signatures.

### Marketplace with seller isolation

Sellers manage their own products, orders, and inventory. Platform admins
see everything via the `unscoped` manager.

```python
class Seller(AbstractTenant):
    contact_email = models.EmailField()
    stripe_account_id = models.CharField(max_length=100, blank=True)

class Product(TenantModel):
    name = models.CharField(max_length=200)
    price = models.DecimalField(max_digits=10, decimal_places=2)

class Order(TenantModel):
    product = models.ForeignKey(Product, on_delete=models.PROTECT)
    quantity = models.IntegerField()

# settings.py
BOUNDARY_TENANT_MODEL = "sellers.Seller"
BOUNDARY_RESOLVERS = [
    "boundary.resolvers.HeaderResolver",  # internal API, trusted clients
]
```

```python
# Seller's view: only sees their own products
def my_products(request):
    return Product.objects.all()

# Admin analytics: sees all sellers
def platform_revenue():
    return Order.unscoped.aggregate(total=Sum("product__price"))
```

### Multi-region with data residency

UK customer data must stay in the UK database; EU data in the EU database.

```python
# settings.py
BOUNDARY_TENANT_MODEL = "orgs.Organisation"
BOUNDARY_REGIONS = {
    "uk":      {"ENGINE": "django.db.backends.postgresql", "HOST": "uk.db.example.com", ...},
    "eu-west": {"ENGINE": "django.db.backends.postgresql", "HOST": "eu.db.example.com", ...},
    "us-east": {"ENGINE": "django.db.backends.postgresql", "HOST": "us.db.example.com", ...},
}
DATABASE_ROUTERS = ["boundary.routing.RegionalRouter"]
```

```python
# Tenant has region="uk": all queries automatically hit the UK database
with TenantContext.using(uk_tenant):
    Patient.objects.create(name="Smith", nhs_number="123")  # stored in UK DB

# Platform-wide reporting across all regions
from boundary.routing import all_regions
with all_regions() as aliases:
    for alias in aliases:
        count = Patient.objects.using(alias).count()
        print(f"{alias}: {count} patients")
```

### Internal tool with session-based switching

Staff users switch between departments via a dropdown. The selected
department is stored in the session.

```python
# settings.py
BOUNDARY_TENANT_MODEL = "departments.Department"
BOUNDARY_REQUIRED = False  # allow unauthenticated pages
BOUNDARY_RESOLVERS = [
    "boundary.resolvers.SessionResolver",
]
```

```python
# Switch department view
def switch_department(request, dept_id):
    dept = Department.objects.get(pk=dept_id)
    request.session["boundary_tenant_id"] = str(dept.pk)
    return redirect("dashboard")
```

---

## How It Works

### Architecture

```
  HTTP Request / Celery Task / Management Command
           |
           v
  RESOLUTION LAYER: TenantMiddleware + pluggable Resolvers
           |
           v
  CONTEXT LAYER: TenantContext (ContextVar + DB session variable)
           |
           v
  ORM LAYER: TenantManager auto-filters every queryset
           |
           v
  ROUTING LAYER (optional): RegionalRouter per-tenant DB alias
           |
           v
  DATABASE LAYER: PostgreSQL RLS policies (defence in depth)
```

Every layer below RESOLUTION LAYER faithfully enforces isolation for
whichever tenant was resolved. None of them ask whether the caller is
allowed to act as that tenant: that is what RESOLUTION LAYER decides, and
for a client-controlled resolver (`HeaderResolver`, `JWTClaimResolver`) it
decides purely from what the client sent. boundary's isolation guarantee is
"every layer scopes to the resolved tenant correctly", not "the resolved
tenant is the one this caller is allowed to see". Closing that second gap
(authenticating the caller, then checking their membership of the resolved
tenant) is the consumer's responsibility; see
[Resolvers](#resolvers) and
[Enforce membership after resolution](docs/how-to/choose-and-order-resolvers.md#enforce-membership-after-resolution).

### Defence in Depth

Two independent layers enforce tenant isolation:

1. **ORM layer**: `TenantManager` filters every queryset by the active tenant.
   This catches standard Django ORM usage.
2. **PostgreSQL RLS**: Row Level Security policies enforce isolation at the
   database level, catching raw SQL, third-party packages, and ORM bugs.

A bug in one layer is caught by the other. This RLS layer only exists for
models with their own tenant column (`TenantMixin` / `make_tenant_mixin`).
Relation-scoped (path-scoped) models built with `make_tenant_path_mixin` are
protected at the ORM layer only: see
[Scope a model through a relation](docs/how-to/scope-models-through-a-relation.md)
for the exact contract before relying on RLS to catch direct SQL against one.

---

## Models

### AbstractTenant

Convenience base for your tenant model. Provides common fields:

| Field | Type | Description |
|-------|------|-------------|
| `name` | CharField(200) | Tenant name |
| `slug` | SlugField(unique) | URL-safe identifier |
| `region` | CharField(50) | Regional routing key (blank if single-region) |
| `is_active` | BooleanField | Inactive tenants are rejected by middleware (403) |
| `created_at` | DateTimeField | Auto-set on creation |
| `updated_at` | DateTimeField | Auto-set on save |

### TenantModel / TenantMixin

Base class for tenant-scoped data models. Adds:

- `tenant` ForeignKey to your tenant model (CASCADE, non-nullable)
- `objects`: `TenantManager` that auto-filters by active tenant
- `unscoped`: plain `Manager` for cross-tenant operations (admin, analytics)

```python
class Booking(TenantModel):
    court = models.IntegerField()
```

**Auto-populate on save:** When no `tenant` is set explicitly,
`TenantModel.save()` reads from `TenantContext` automatically.

**Bulk operations:**
- `bulk_create()`: auto-populates tenant on objects where `tenant_id` is None
- `bulk_update()`: validates all objects belong to the active tenant

### Custom FK Field Names: `make_tenant_mixin()`

If your domain uses a different name for the tenant relationship (e.g.
`merchant`, `organisation`, `workspace`), use the factory instead of
`TenantMixin`:

```python
from boundary.models import make_tenant_mixin

MerchantMixin = make_tenant_mixin("merchant")

class Product(MerchantMixin):
    sku = models.CharField(max_length=50)

# Product.merchant is the FK: auto-filtering, auto-populate, bulk ops all work
# Product.objects.all() : filters by active tenant via the "merchant" field
# product.merchant      : returns the tenant instance
```

The factory accepts the same FK options as Django's `ForeignKey`:

```python
make_tenant_mixin(
    "merchant",
    on_delete=models.PROTECT,       # default: CASCADE
    related_name="products",        # default: "%(app_label)s_%(class)s_set"
    db_index=True,                  # default: True
    null=False,                     # default: False
)
```

Alternatively, set `BOUNDARY_TENANT_FK_FIELD` in your settings to change
the default field name globally. `TenantMixin` itself always uses `"tenant"`,
but the factory reads the setting when no explicit `fk_field` is passed.

#### Static typing and `make_tenant_mixin()` / `make_tenant_path_mixin()`

`TenantModel` / `TenantMixin` are ordinary module-level classes: `mypy` with
the `django-stubs` plugin resolves `objects`, `unscoped`, and the `tenant` FK
on any model built from them with no extra configuration.

`make_tenant_mixin()` and `make_tenant_path_mixin()` build and return a class
from *inside a function call*. This is a hard `mypy` limitation, not a gap in
boundary's types: `mypy` rejects any base class that is a function call (or a
variable holding one) at the semantic-analysis stage, before plugins run
(`Unsupported dynamic base class` / `Invalid base class`), and there is no
annotation or stub shape that changes this. A model built from the factory
will type-check as follows:

```python
from boundary.models import make_tenant_mixin

MerchantMixin = make_tenant_mixin("merchant")


class Product(MerchantMixin):  # type: ignore[valid-type,misc]
    sku = models.CharField(max_length=50)
```

`Product.objects` / `Product.unscoped` then type as `Any` rather than
`TenantManager[Product]` / `UnscopedManager[Product]` — no false positives,
but no manager-level type safety either. Separately, the tenant model on the
other end of the relationship needs its own narrow suppression for the
reverse accessor `django-stubs` cannot synthesise for a runtime-attached FK:

```python
class Merchant(models.Model):  # type: ignore[django-manager-missing]
    name = models.CharField(max_length=200)
```

If a model's FK field name can be `"tenant"`, prefer `TenantModel` /
`TenantMixin` over the factory: it is fully type-checked with none of the
above. Reserve `make_tenant_mixin()` for genuinely custom FK names, and
`make_tenant_path_mixin()` for path-scoped models, accepting the two
suppressions above under `django-stubs`.

### Custom Terminology

Boundary error messages, `verbose_name` on FK fields, and middleware HTTP
responses all use a configurable label. By default the label tracks
`BOUNDARY_TENANT_FK_FIELD`, so a single setting changes everything:

```python
# settings.py
BOUNDARY_TENANT_FK_FIELD = "merchant"
# → "No merchant is active in context."
# → 404 body: "Merchant not found."
# → FK verbose_name: "merchant"
```

To override independently, set `BOUNDARY_TENANT_LABEL` (used in user-facing
strings) and/or `BOUNDARY_REQUEST_ATTR` (the alias attached to the request
alongside `request.tenant`):

```python
BOUNDARY_TENANT_FK_FIELD = "merchant"   # FK column name
BOUNDARY_TENANT_LABEL = "shop"          # error/UI copy says "shop"
BOUNDARY_REQUEST_ATTR = "merchant"      # views read request.merchant
```

`request.tenant` is always set for backwards compatibility; the alias is
added in addition, never as a replacement.

### Model Introspection

```python
from boundary.models import is_tenant_model, get_tenant_fk_field

is_tenant_model(Product)       # True
get_tenant_fk_field(Product)   # "merchant"

is_tenant_model(Booking)       # True
get_tenant_fk_field(Booking)   # "tenant"
```

System checks, regional routing, and RLS verification all use
`is_tenant_model()` internally, so custom FK models are automatically
recognised.

### Per-tenant uniqueness: `tenant_unique()`

`unique=True` on a field of a tenant-scoped model is enforced across every
tenant, not within one, so two tenants cannot both hold an invoice with
reference `INV-001`. `tenant_unique()` returns a `UniqueConstraint` whose
columns are the model's tenant foreign key followed by the fields you give:

```python
from boundary.models import TenantModel, tenant_unique


class Invoice(TenantModel):
    reference = models.CharField(max_length=32)     # not unique=True

    class Meta:
        constraints = [tenant_unique("reference")]
```

The tenant field is read off the model when Django prepares it, not when the
`Meta` body runs, so the same call resolves to `("merchant", "reference")` on a
model built from `make_tenant_mixin("merchant")` without naming the field
twice. Pass `name=` to control the constraint name; without one, boundary
derives a deterministic name from the model and the field list. A path-scoped
model (`make_tenant_path_mixin()`) has no tenant column to lead with, so the
helper raises when that model class is prepared.

See
[Uniqueness within a tenant](docs/how-to/set-up-a-tenant-model.md#uniqueness-within-a-tenant)
for the migration consequences of converting an existing global constraint.

### Cross-tenant foreign key validation

A row correctly scoped to tenant A can still hold a foreign key pointing at
tenant B's row. Neither isolation layer catches that on its own: the row's
own `tenant_id` is A, so the ORM filter passes it and the RLS policy
predicate is satisfied. Tenant-scoped models therefore validate their
foreign keys on `clean()`:

```python
booking = Booking(tenant=tenant_a, venue=venue_owned_by_tenant_b)
booking.full_clean()   # ValidationError, keyed by the FK field
```

Only foreign keys whose target is itself tenant-scoped and owns a local
tenant column are checked. A foreign key to `auth.User` or a lookup table
is skipped, as is a `None` value, and the comparison is against the
instance's own tenant rather than the active context, so an admin or import
path operating on another tenant's row is not falsely rejected.

**This fires on `full_clean()` paths only**, most notably `ModelForm`
validation. Django does not call `clean()` from `save()`, `bulk_create()`,
`update()`, `bulk_update()` or raw SQL, so a cross-tenant foreign key
assigned through any of those is written with the reference intact. Where
your writes go through `save()` in a service function rather than a form,
validate explicitly. See
[Isolation layers](docs/explanation/isolation-layers.md) for the full
threat model.

### Adopting a third-party app

The models above are ones you declare. A third-party app that ships concrete
models and owns its migrations (allauth, taggit, wagtail) gives you nothing to
compose a mixin onto. Those apps are scoped at the database layer instead: list
the app label in `BOUNDARY_TENANT_APPS` and apply one `AdoptTenantApp` operation
from a migration in your own app.

```python
# settings.py
BOUNDARY_TENANT_APPS = ["account"]

# myapp/migrations/0007_adopt_allauth_account.py
from boundary.migrations_ops import AdoptTenantApp

operations = [AdoptTenantApp("account")]
```

Each adopted table gains a `tenant_id` column that **no Django field models**,
declared `NOT NULL DEFAULT boundary_current_tenant_id()`, with RLS enabled and
forced, both boundary policies, and every non-primary-key unique constraint
rewritten to a composite leading with `tenant_id`. The adopted package is not
modified, forked, or made aware of any of this.

The trade is explicit and permanent: adopted tables are **RLS-only**. No ORM
filtering, no `BOUNDARY_STRICT_MODE`, no `TenantNotSetError`, and no isolation at
all on a non-PostgreSQL backend. `boundary.E007` and a real-PostgreSQL test suite
carry the whole burden. A populated table is refused without an explicit
`backfill_tenant`, and reversing an adoption destroys the tenant assignment of
every row.

See
[Adopt a third-party app into your tenancy](docs/how-to/adopt-a-third-party-app.md)
for the full procedure, the deny-list, the bootstrap paths that need a tenant
context, and the limits in full. Where the package **does** ship a swappable
abstract base, prefer
[Scope a package's models into your tenancy](docs/how-to/scope-a-packages-models.md),
which gives you both isolation layers.

---

## Context

### TenantContext

The core API for tenant context management:

```python
from boundary.context import TenantContext

# Set and get
token = TenantContext.set(tenant)
tenant = TenantContext.get()       # returns tenant or None
tenant = TenantContext.require()   # returns tenant or raises TenantNotSetError
TenantContext.clear(token)

# Context manager (recommended)
with TenantContext.using(tenant):
    Booking.objects.all()  # filtered to this tenant
# Context automatically restored on exit
```

The context manager is savepoint-safe: it explicitly restores the DB session
variable on exit rather than relying on PostgreSQL savepoint rollback.

### Admin Bypass

`admin_bypass()` is the only supported way to set the RLS admin bypass flag
(`BOUNDARY_ADMIN_FLAG_VAR`, default `app.boundary_admin`). It hardcodes the
transaction-local form of `set_config`, so the unsafe session-scoped form
(which can outlive a transaction and leak across pooled or reused
connections) is not reachable through this API:

```python
from boundary.context import admin_bypass

with admin_bypass():
    # Full read/write access across every tenant in this block, even with
    # FORCE ROW LEVEL SECURITY on the table. Clears automatically on exit.
    Booking.unscoped.filter(court=1).update(is_paid=True)
```

The flag grants both visibility AND write access: the `boundary_admin_bypass`
policy has no `WITH CHECK`, so PostgreSQL uses its `USING` clause for write
checks too, and permissive policies are OR'd, so this policy alone is
sufficient regardless of `boundary_tenant_isolation`'s own `WITH CHECK`. Treat
it as full cross-tenant access, not a read-only viewer.

Fires `boundary.signals.admin_bypass_activated` on entry (flag variable name
and DB alias) for audit trails. Nested calls on the same alias are
idempotent; only the outermost call clears the flag on exit. See the
docstring on `admin_bypass()` for the full contract, including the
`BOUNDARY_WRAP_ATOMIC=False` case and multi-region use with `all_regions()`.
See also [Cross-tenant admin operations](docs/how-to/cross-tenant-admin-operations.md#5-bypass-rls-for-trusted-maintenance-work).

---

## Resolvers

Resolvers determine which tenant applies to an incoming request. Configure
via `BOUNDARY_RESOLVERS`; first match wins.

| Resolver | Source | Setting | Client-controlled? |
|----------|--------|---------|---------------------|
| `SubdomainResolver` | `club.example.com` -> slug lookup | `BOUNDARY_SUBDOMAIN_FIELD`, `BOUNDARY_SUBDOMAIN_PARENT_DOMAIN` | No (constrained by `ALLOWED_HOSTS`, and by `BOUNDARY_SUBDOMAIN_PARENT_DOMAIN` when set) |
| `HeaderResolver` | `X-Tenant-ID` header (UUID first, slug fallback) | `BOUNDARY_HEADER_NAME` | **Yes** |
| `JWTClaimResolver` | JWT payload claim (no signature validation) | `BOUNDARY_JWT_CLAIM` | **Yes** |
| `SessionResolver` | Django session key | `BOUNDARY_SESSION_KEY` | No, provided the session key is only ever set server-side after its own check |
| `ExplicitResolver` | `request.boundary_tenant` set by upstream code | None | No |

**Resolution is not authorisation.** boundary resolves *which* tenant a
request targets; it does not check *whether the caller may access it*. With
`HeaderResolver` or `JWTClaimResolver` an authenticated user of one tenant
can simply name another and every layer beneath resolution scopes correctly
to it. Verifying that the authenticated principal is a member of the
resolved tenant is the consumer's responsibility; see
[Enforce membership after resolution](docs/how-to/choose-and-order-resolvers.md#enforce-membership-after-resolution).
`boundary.W006` warns when a client-controlled resolver is configured
alongside `django.contrib.auth`.

**`ALLOWED_HOSTS` alone is not a domain boundary.** `SubdomainResolver`
resolves the first label of any host with three or more labels, including a
foreign, customer-owned domain that happens to pass `ALLOWED_HOSTS` and whose
first label collides with a tenant slug. Set
`BOUNDARY_SUBDOMAIN_PARENT_DOMAIN` to constrain resolution to your own
domain(s); see [Constrain SubdomainResolver to your own domain](docs/how-to/choose-and-order-resolvers.md#constrain-subdomainresolver-to-your-own-domain).
`boundary.W008` warns when `SubdomainResolver` is configured without it.

**Security note:** Resolver ordering determines precedence. Placing
`HeaderResolver` first allows any HTTP client to set the tenant via header.
For public-facing apps, place `SubdomainResolver` first. This is about
precedence within the chain, and is a narrower concern than the
authorisation gap above: it applies regardless of ordering.

### Custom resolvers

```python
from boundary.resolvers import BaseResolver

class PathResolver(BaseResolver):
    def resolve(self, request):
        parts = request.path.split("/")
        if len(parts) >= 3 and parts[1] == "t":
            TenantModel = self.get_tenant_model()
            try:
                return TenantModel.objects.get(slug=parts[2], is_active=True)
            except TenantModel.DoesNotExist:
                return None
        return None
```

### Resolver cache

Resolvers that perform DB lookups cache results in a process-local LRU cache.
Cache is invalidated automatically on tenant save/delete via Django signals,
and by TTL (default: 60 seconds).

---

## Row Level Security

RLS provides database-level enforcement independent of application code.

### Migration operations

```python
# In your migration file
from boundary.migrations_ops import EnableRLS, CreateTenantPolicy

class Migration(migrations.Migration):
    operations = [
        migrations.CreateModel(name="Booking", ...),
        EnableRLS("Booking"),
        CreateTenantPolicy("Booking"),
    ]
```

`CreateTenantPolicy` generates:
- A helper function (`boundary_current_tenant_id()`) that safely casts the
  session variable to the correct type. Declared `LEAKPROOF` only when
  `BOUNDARY_FUNCTION_LEAKPROOF` is set (default off, because `LEAKPROOF` needs a
  superuser that managed Postgres does not grant); it is a planner optimisation,
  not an isolation requirement
- An isolation policy with `USING` + `WITH CHECK` (enforces on SELECT, INSERT,
  UPDATE, DELETE)
- An admin bypass policy for management commands

### Type-aware

The RLS function detects whether your tenant model uses UUID or integer primary
keys and generates the appropriate type cast.

### Reversible

All operations are fully reversible via `migrate --reverse`.

---

## Regional Routing

Route queries to geographically distinct databases for data residency compliance.

```python
# settings.py
BOUNDARY_REGIONS = {
    "eu-west": {"ENGINE": "django.db.backends.postgresql", "HOST": "eu.db.example.com", ...},
    "us":      {"ENGINE": "django.db.backends.postgresql", "HOST": "us.db.example.com", ...},
}

DATABASE_ROUTERS = ["boundary.routing.RegionalRouter"]
```

Tenant-scoped queries are routed to the tenant's region. Non-tenant models
(auth, sessions, etc.) always route to `default`.

```python
from boundary.routing import all_regions, specific_region

# Iterate all regions
with all_regions() as aliases:
    for alias in aliases:
        count = Booking.objects.using(alias).count()

# Pin to a specific region
with specific_region("eu-west"):
    bookings = Booking.objects.all()
```

---

## Celery Integration

Tenant context is propagated to Celery tasks via headers.

```python
from boundary.celery import tenant_task

@app.task
@tenant_task
def send_confirmation(booking_id):
    # TenantContext.get() returns the correct tenant
    booking = Booking.objects.get(id=booking_id)
```

For class-based tasks:

```python
from boundary.celery import TenantTask

class GenerateReport(TenantTask, app.Task):
    def run(self, report_id):
        ...
```

---

## Management Commands

### boundary_provision

```bash
python manage.py boundary_provision --name "Club A" --slug "club-a" --region eu-west
# Outputs: the new tenant's PK
```

### boundary_deprovision

```bash
python manage.py boundary_deprovision --tenant club-a --export data.ndjson --yes
# Streams tenant data to NDJSON, then deletes
```

Supports `--dry-run`, `--batch-size`, `--yes` (skip confirmation).

### boundary_run

```bash
python manage.py boundary_run --tenant club-a send_reminders
# Runs send_reminders with tenant context active
```

### boundary_run_all

```bash
python manage.py boundary_run_all send_reminders --parallel 4 --region eu-west --json
# Runs against all active tenants, 4 workers, EU only, NDJSON output
```

---

## Settings Reference

| Setting | Default | Description |
|---------|---------|-------------|
| `BOUNDARY_TENANT_MODEL` | **Required**, falls back to `ICV_TENANT_MODEL` | Dotted path to tenant model, e.g. `"tenants.Organisation"`. `ICV_TENANT_MODEL` is the single ecosystem-wide tenant-model knob (ADR-025 T2); set it once if other packages (e.g. icv-identity) already read it. `BOUNDARY_TENANT_MODEL` always wins if both are set. Whichever setting resolves is structural: it is baked into `TenantMixin`'s and `make_tenant_mixin()`'s foreign key (and therefore into your migrations) at import time, so changing either setting afterwards needs a new migration, the same as changing any other FK target. A project with neither setting configured fails fast at startup with `ImproperlyConfigured` naming both settings |
| `BOUNDARY_TENANT_FK_FIELD` | `"tenant"` | Default FK field name used by `make_tenant_mixin()` when no explicit name is passed |
| `BOUNDARY_TENANT_LABEL` | `BOUNDARY_TENANT_FK_FIELD` | Human-readable term used in error messages, FK `verbose_name`, and middleware HTTP response bodies |
| `BOUNDARY_REQUEST_ATTR` | `BOUNDARY_TENANT_FK_FIELD` | Extra attribute set on the request object alongside `request.tenant` (e.g. `request.merchant`). When equal to `"tenant"`, no second attribute is added |
| `BOUNDARY_STRICT_MODE` | `True` | Raise `TenantNotSetError` on unscoped queries |
| `BOUNDARY_REQUIRED` | `True` | Return 404 if no resolver matches |
| `BOUNDARY_RESOLVERS` | `["...SubdomainResolver"]` | Ordered resolver class paths |
| `BOUNDARY_SUBDOMAIN_FIELD` | `"slug"` | Tenant field for subdomain lookup |
| `BOUNDARY_SUBDOMAIN_PARENT_DOMAIN` | `None` | Constrain `SubdomainResolver` to hosts exactly one label above this domain (or list of domains); closes cross-tenant serving from foreign hosts |
| `BOUNDARY_HEADER_NAME` | `"X-Tenant-ID"` | HTTP header for HeaderResolver |
| `BOUNDARY_JWT_CLAIM` | `"tenant_id"` | JWT payload claim |
| `BOUNDARY_SESSION_KEY` | `"boundary_tenant_id"` | Session key for SessionResolver |
| `BOUNDARY_REGIONS` | `None` | Regional DB configs (activates routing) |
| `BOUNDARY_REGION_FIELD` | `"region"` | Tenant field storing region key |
| `BOUNDARY_DB_SESSION_VAR` | `"app.current_tenant_id"` | PostgreSQL session variable |
| `BOUNDARY_SET_DB_SESSION_VAR` | `True` | Whether to write the PostgreSQL session variable at all. Set to `False` to skip the `set_config()` round trip on every context entry and exit for a deployment using boundary for ORM-layer scoping only, with no RLS policies enabled. Disabling this while RLS is actually enabled on a tenant table is a genuine isolation failure, not a performance choice; `boundary.W009` warns when both are true at once |
| `BOUNDARY_WRAP_ATOMIC` | `True` | Wrap requests in `transaction.atomic()` |
| `BOUNDARY_TENANT_APPS` | `[]` | App labels (not dotted module paths) whose concrete models are adopted into tenancy at the database layer: a `tenant_id` column no Django field models, plus RLS and both policies. Declares intent and drives `boundary.E007`, `boundary_deprovision` and `assert_rls_enforced()`; the DDL itself is applied by the `AdoptTenantApp` operation from a migration in your own app. Listing your own app is supported, and is how a model that forgot its mixin gets reported |
| `BOUNDARY_ADOPT_EXCLUDE` | `[]` | `"app_label.ModelName"` strings exempted from adoption, for a genuinely global reference table inside an otherwise adopted app. Applies to both `AdoptTenantApp` and `boundary.E007`. An entry matching no model is inert, not an error |
| `BOUNDARY_RESOLVER_CACHE_SIZE` | `1000` | LRU cache max entries |
| `BOUNDARY_RESOLVER_CACHE_TTL` | `60` | Cache TTL in seconds |
| `BOUNDARY_POST_PROVISION_HOOK` | `None` | Callable after tenant provisioning |
| `BOUNDARY_PRE_DEPROVISION_HOOK` | `None` | Callable before tenant deletion |

---

## System Checks

| ID | Severity | Condition |
|----|----------|-----------|
| `boundary.E001` | Error | Neither `BOUNDARY_TENANT_MODEL` nor its `ICV_TENANT_MODEL` fallback is set, or whichever one is set is invalid |
| `boundary.E003` | Error | Resolver class cannot be imported |
| `boundary.E004` | Error | No boundary or supported external tenant-resolution middleware is in `MIDDLEWARE`. `icv_tenants.middleware.TenantContextMiddleware` is the canonical external path; the legacy `icv_identity.tenants.middleware.TenantContextMiddleware` remains recognised during migration |
| `boundary.E005` | Error | BOUNDARY_REGIONS set but RegionalRouter not in DATABASE_ROUTERS |
| `boundary.E006` | Error | Tenant-scoped table missing RLS; recognises TenantMixin and make_tenant_mixin models, and adopted tables |
| `boundary.E007` | Error | An expected-adopted table (a concrete model of an app in `BOUNDARY_TENANT_APPS`, not excluded, not already mixin- or path-scoped, and not the tenant model) is missing its `tenant_id` column, carries one of an unexpected type, lacks enabled-and-forced RLS, is missing either policy, or carries a unique constraint that is not composite leading with `tenant_id`. Also reports a deny-listed or uninstalled app label in the setting, the one condition needing no database connection. The expected set is derived from the live app registry, so a model added by an upstream upgrade is reported; the remedy is a second `AdoptTenantApp` migration (issues #69, #71) |
| `boundary.E008` | Error | `settings.DEBUG` is `False` and `BOUNDARY_STRICT_MODE` or `BOUNDARY_SET_DB_SESSION_VAR` is `False`, one Error per offending setting. Each removes a category of isolation the deployment believes it has, and both default to the safe value, so reaching this state takes an explicit opt-out. Settings-only: no query, no connection, no vendor gate, so it is the one check that cannot be silently skipped. **It fires under the test runner too**, which sets `DEBUG = False`; record a deliberate choice with `"boundary.E008"` in `SILENCED_SYSTEM_CHECKS` (issue #82) |
| `boundary.W001` | Warning | STRICT_MODE is False |
| `boundary.W002` | Warning | Both `boundary.middleware.TenantMiddleware` and an external `TenantContextMiddleware` are in `MIDDLEWARE`, which double-resolves the tenant. The canonical external middleware is `icv_tenants.middleware.TenantContextMiddleware`; the legacy identity path remains recognised during migration |
| `boundary.W003` | Warning | The connecting database role is a superuser or has BYPASSRLS: RLS policies are not enforced for this connection, so `boundary.E006` passing gives no guarantee tenant isolation actually works (issue #21) |
| `boundary.W006` | Warning | A client-controlled resolver (`HeaderResolver`, `JWTClaimResolver`, or a subclass) is in `BOUNDARY_RESOLVERS` alongside `django.contrib.auth`: resolution names a tenant from client input with no membership check downstream (issue #38) |
| `boundary.W007` | Warning | `boundary.E006` or `boundary.W003` could not determine the database state it checks. The connection was available but the query against `pg_class`/`pg_roles` failed, so the absence of E006 or W003 must not be read as a pass (issue #34) |
| `boundary.W008` | Warning | `SubdomainResolver` (or a subclass) is in `BOUNDARY_RESOLVERS` without `BOUNDARY_SUBDOMAIN_PARENT_DOMAIN` set: it resolves the first label of any three-plus-label host, including a foreign host outside the deployment's own domain (issue #22) |
| `boundary.W009` | Warning | `BOUNDARY_SET_DB_SESSION_VAR` is `False` but Row Level Security is enabled and forced on a tenant-scoped table, or on an adopted table: RLS depends on the session variable the opt-out stops writing, so isolation on that table is not enforced. An adopted table has no ORM layer to fall back on, so the combination leaves it with no isolation at all (issue #53) |

---

## Testing

### In your tests

```python
from boundary.testing import set_tenant, tenant_factory, TenantTestMixin

# Context manager
def test_isolation():
    tenant_a = tenant_factory(name="A", slug="a")
    tenant_b = tenant_factory(name="B", slug="b")

    with set_tenant(tenant_a):
        Booking.objects.create(court=1)

    with set_tenant(tenant_b):
        assert Booking.objects.count() == 0  # tenant_b sees nothing

# Mixin for TestCase
class BookingTests(TenantTestMixin, TestCase):
    def test_auto_populate(self):
        booking = Booking.objects.create(court=1)
        assert booking.tenant == self.tenant
```

### Unscoped operations

```python
# Cross-tenant admin/analytics queries
all_bookings = Booking.unscoped.all()

# Explicitly set tenant on unscoped create
Booking.unscoped.create(court=1, tenant=specific_tenant)
```

### RLS test role and fail-closed assertion

`boundary.W003` (above) diagnoses a test/CI database role that bypasses Row
Level Security, but only under `manage.py check`, never under a plain
`pytest` run. A suite run as the bootstrap superuser that most PostgreSQL
docker images ship with passes every RLS-isolation test having enforced
nothing.

`boundary.testing` ships the fix as two pieces:

`provision_rls_test_role()` provisions a plain `NOSUPERUSER NOBYPASSRLS`
role (idempotent: safe to call against a role that already exists), mirroring
the shell `.github/workflows/ci.yml` already runs by hand. It is a no-op on
a non-PostgreSQL backend. **It does not, and cannot, repoint
`DATABASES["default"]` for you**: pytest-django creates the test database
during `django_db_setup`, before any fixture in a test module has run, so a
fixture cannot swap the role early enough to affect that. Instead it returns
the provisioned role's connection parameters, and you either build your own
connection from them (as this package's own `app_conn` test fixture does)
or set `DATABASES["default"]` to point at the role's credentials in your
`settings.py` yourself, before pytest starts.

`assert_rls_enforced(connection_params)` is the load-bearing half: it
connects with the given parameters and raises `RLSNotEnforcedError`, naming
what it found, unless BOTH `current_user` is neither a superuser nor
BYPASSRLS, AND at least one registered tenant model's table has RLS enabled
and forced. Wire it as a session-scoped autouse fixture so a misconfigured
run stops before any test runs, rather than passing every isolation test
vacuously:

```python
# conftest.py
import pytest
from boundary.testing import provision_rls_test_role, rls_enforced

@pytest.fixture(scope="session", autouse=True)
def _rls_enforced(django_db_setup, django_db_blocker):
    with django_db_blocker.unblock():
        params = provision_rls_test_role(
            bootstrap_connection_params={
                "host": "localhost",
                "port": 5432,
                "dbname": "myproject_test",
                "user": "postgres",
                "password": "postgres",
            },
        )
        if params:  # empty on a non-PostgreSQL backend
            rls_enforced(params)
```

`rls_enforced()` calls `pytest.exit()` on failure, so the whole run stops
immediately with the cause on stderr, rather than reporting as one failed
fixture among a wall of now-meaningless downstream test results.

---

## Signals

| Signal | Arguments | Fired when |
|--------|-----------|------------|
| `tenant_resolved` | `tenant, resolver, request` | After successful resolution |
| `tenant_resolution_failed` | `request` | No resolver matched (REQUIRED=True) |
| `strict_mode_violation` | `model, queryset` | Before TenantNotSetError is raised |
| `admin_bypass_activated` | `flag_var, using` | On entry to `admin_bypass()` |

---

## Requirements

- Python 3.12 and 3.13
- Django 5.2 (LTS), 6.0 and 6.1
- PostgreSQL 14 to 16 for the RLS layer; the ORM layer also runs on SQLite

See [Supported environments](#supported-environments) below for what each of
those means in practice, and for the isolation consequence of running on
SQLite.

---

## Supported environments

Every combination below is exercised by a CI leg. Anything not listed is
untested rather than actively blocked: boundary adds no refusal for it, and
running outside this matrix is at your own risk.

### Databases

| Backend | Support |
|---|---|
| PostgreSQL 14, 15, 16 | Fully supported, both isolation layers. A release above 16 is not refused and is expected to work, since nothing in the generated DDL uses a feature newer than 14, but it is outside the verified matrix until a leg runs it. |
| SQLite | Supported as an **ORM-only** backend, for local development and CI. |
| MySQL, MariaDB | **Unsupported and untested.** Neither has Row Level Security, so the RLS layer cannot exist there, and no CI leg exercises the ORM layer on them. |

**What SQLite gives you, and what it does not.** The ORM filtering layer, the
context layer, resolution, Celery propagation and the management commands all
work. Row Level Security does not exist on SQLite, and boundary keeps that
absence quiet rather than noisy: `boundary.E006`, `boundary.W003`,
`boundary.W007` and `boundary.W009` return nothing, `assert_rls_enforced()`
returns without raising, `TenantContext` issues no session variable, and the
three RLS migration operations log one line each and apply no DDL. You can run
the same migration files on both backends.

**The consequence, stated plainly: an adopted table has no isolation at all on
SQLite.** A model using boundary's mixins keeps its ORM-layer filtering when
the policy is skipped, so it is left with less isolation. An adopted
third-party table (`AdoptTenantApp`) has no ORM layer beneath the policy, so
on SQLite it has none whatsoever. `AdoptTenantApp` therefore still refuses to
run off PostgreSQL, where the other three operations skip quietly.

More generally on a non-PostgreSQL alias, a mixin-scoped model has ORM-layer
isolation only: `TenantManager` filtering and `BOUNDARY_STRICT_MODE` behave
exactly as on PostgreSQL, but `raw()`, `extra()`, hand-built SQL and any
third-party package writing directly have no backstop, because the backstop is
RLS. Use PostgreSQL wherever isolation matters.

### Python and Django

| | Supported |
|---|---|
| Python | 3.12, 3.13 |
| Django | 5.2 (LTS), 6.0, 6.1 |

Every combination of the two is supported and runs in CI. Python 3.14 is not
supported in the 1.0 line.

### Deployment shapes

- **ASGI.** Context propagation is `contextvars` throughout, and
  `TenantMiddleware` is a `MiddlewareMixin` serving both WSGI and ASGI. An
  async view, async middleware downstream of boundary's, and a sync view in
  the same process all see the same tenant context semantics.
- **Celery.** Tenant context crosses a task dispatch through the shipped
  signal handlers and the `tenant_task` decorator or `TenantTask` base class,
  once you wire them. Boundary does not auto-install them into your Celery
  app.
- **Multiple database aliases.** More than one alias is supported through
  `RegionalRouter` and, for migration-time DDL, through the router gates. A
  deployment may hold a PostgreSQL alias carrying the RLS layer beside a
  non-PostgreSQL alias carrying none, in one process, provided its router
  keeps the RLS migrations off the second alias. The vendor gates are applied
  per alias, not per deployment.

---

## Comparison with django-tenants

Claims about django-tenants below are taken from its own documentation
(https://django-tenants.readthedocs.io/) at 3.14.0.

| | django-tenants | django-boundary |
|-|---------------|-----------------|
| Isolation | PostgreSQL schemas (Postgres only) | ORM filter on any backend, plus RLS on Postgres |
| Migration cost | Once per tenant schema (`migrate_schemas`) | Once, for models using boundary's mixins |
| Third-party apps | Any app, by listing it in `TENANT_APPS`; whole app only | Own models and swappable-base packages; concrete third-party models need adoption (icvoss/django-boundary#69) |
| Tenant context | Held on the database connection; async not documented | contextvars; async supported |
| Celery | Separate `tenant-schemas-celery` package | boundary's Celery signals, once wired (see how-to) |
| Regional routing | Not supported | First-class |
| Outside a tenant | Public schema: missing-relation error | `TenantNotSetError` under `STRICT_MODE` |

---

## Licence

MIT
