# django-boundary

Scalable row-level multi-tenancy for Django with PostgreSQL Row Level Security.

---

## Who Is This For?

django-boundary is for Django projects that serve multiple tenants from a
single database. If your users belong to organisations, workspaces, teams,
schools, clinics, clubs, or any other entity that should only see its own
data — boundary handles the isolation.

### Common use cases

**SaaS platforms** — Each customer (organisation, workspace, account) is a
tenant. Their data is isolated at the ORM and database level. New tenants are
provisioned via management command; no schema migrations required.

**Marketplace platforms** — Sellers, venues, or merchants each have their own
tenant. Products, orders, and analytics are scoped per-tenant. Platform-wide
reporting uses the `unscoped` manager.

**Education / healthcare / government** — Schools, clinics, or departments are
tenants. Data residency requirements are met via regional routing (e.g. UK data
stays in UK database, EU data in EU database).

**Agency or white-label products** — Each client gets their own tenant, resolved
by subdomain (`client-a.app.com`) or JWT claim from the auth provider.

**Internal tools** — Departments or business units are tenants, resolved via
session or header. `STRICT_MODE` catches accidental cross-department data
exposure during development.

### When NOT to use boundary

- **Single-tenant apps** — no need for isolation machinery.
- **Schema-per-tenant** — use [django-tenants](https://github.com/django-tenants/django-tenants) instead (different trade-offs at scale).
- **Non-PostgreSQL databases** — the ORM layer works on any database, but RLS enforcement requires PostgreSQL 14+.

---

## Features

- **Automatic ORM filtering** — queries are scoped to the active tenant by default
- **PostgreSQL RLS** — database-level enforcement as a second layer of defence
- **Async-native** — context propagation via `contextvars`, works with sync and async Django
- **Pluggable resolvers** — subdomain, header, JWT claim, session, or custom
- **Strict mode** — raises on unscoped queries (default: on), catches data leaks at development time
- **Regional routing** — route queries to geographically distinct databases for data residency compliance
- **Celery integration** — tenant context propagated via task headers, restored on workers
- **Management commands** — provision, deprovision (with NDJSON export), scoped run, run-all with parallelism
- **Test utilities** — `set_tenant()`, `TenantTestMixin`, `tenant_factory()`
- **System checks** — validates configuration at startup
- **LEAKPROOF RLS functions** — prevents query planner information leakage
- **Zero assumptions** — no opinion on auth, URL structure, or domain model

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
BOUNDARY_STRICT_MODE = True  # default — raises on unscoped queries

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

That's it. `Booking.objects.all()` now automatically filters by the active
tenant. Creating a booking auto-populates the `tenant` field from context.

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
# In a view — no tenant filtering needed, it's automatic
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
Boundary only reads the claim — it never validates signatures.

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
# Seller's view — only sees their own products
def my_products(request):
    return Product.objects.all()

# Admin analytics — sees all sellers
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
# Tenant has region="uk" — all queries automatically hit the UK database
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
  RESOLUTION LAYER — TenantMiddleware + pluggable Resolvers
           |
           v
  CONTEXT LAYER — TenantContext (ContextVar + DB session variable)
           |
           v
  ORM LAYER — TenantManager auto-filters every queryset
           |
           v
  ROUTING LAYER (optional) — RegionalRouter per-tenant DB alias
           |
           v
  DATABASE LAYER — PostgreSQL RLS policies (defence in depth)
```

### Defence in Depth

Two independent layers enforce tenant isolation:

1. **ORM layer** — `TenantManager` filters every queryset by the active tenant.
   This catches standard Django ORM usage.
2. **PostgreSQL RLS** — Row Level Security policies enforce isolation at the
   database level, catching raw SQL, third-party packages, and ORM bugs.

A bug in one layer is caught by the other.

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
- `objects` — `TenantManager` that auto-filters by active tenant
- `unscoped` — plain `Manager` for cross-tenant operations (admin, analytics)

```python
class Booking(TenantModel):
    court = models.IntegerField()
```

**Auto-populate on save:** When no `tenant` is set explicitly,
`TenantModel.save()` reads from `TenantContext` automatically.

**Bulk operations:**
- `bulk_create()` — auto-populates tenant on objects where `tenant_id` is None
- `bulk_update()` — validates all objects belong to the active tenant

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

---

## Resolvers

Resolvers determine which tenant applies to an incoming request. Configure
via `BOUNDARY_RESOLVERS` — first match wins.

| Resolver | Source | Setting |
|----------|--------|---------|
| `SubdomainResolver` | `club.example.com` -> slug lookup | `BOUNDARY_SUBDOMAIN_FIELD` |
| `HeaderResolver` | `X-Tenant-ID` header (UUID first, slug fallback) | `BOUNDARY_HEADER_NAME` |
| `JWTClaimResolver` | JWT payload claim (no signature validation) | `BOUNDARY_JWT_CLAIM` |
| `SessionResolver` | Django session key | `BOUNDARY_SESSION_KEY` |
| `ExplicitResolver` | `request.boundary_tenant` set by upstream code | None |

**Security note:** Resolver ordering determines precedence. Placing
`HeaderResolver` first allows any HTTP client to set the tenant via header.
For public-facing apps, place `SubdomainResolver` first.

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
- A `LEAKPROOF` helper function (`boundary_current_tenant_id()`) that safely
  casts the session variable to the correct type
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
| `BOUNDARY_TENANT_MODEL` | **Required** | Dotted path to tenant model, e.g. `"tenants.Organisation"` |
| `BOUNDARY_STRICT_MODE` | `True` | Raise `TenantNotSetError` on unscoped queries |
| `BOUNDARY_REQUIRED` | `True` | Return 404 if no resolver matches |
| `BOUNDARY_RESOLVERS` | `["...SubdomainResolver"]` | Ordered resolver class paths |
| `BOUNDARY_SUBDOMAIN_FIELD` | `"slug"` | Tenant field for subdomain lookup |
| `BOUNDARY_HEADER_NAME` | `"X-Tenant-ID"` | HTTP header for HeaderResolver |
| `BOUNDARY_JWT_CLAIM` | `"tenant_id"` | JWT payload claim |
| `BOUNDARY_SESSION_KEY` | `"boundary_tenant_id"` | Session key for SessionResolver |
| `BOUNDARY_REGIONS` | `None` | Regional DB configs (activates routing) |
| `BOUNDARY_REGION_FIELD` | `"region"` | Tenant field storing region key |
| `BOUNDARY_DB_SESSION_VAR` | `"app.current_tenant_id"` | PostgreSQL session variable |
| `BOUNDARY_WRAP_ATOMIC` | `True` | Wrap requests in `transaction.atomic()` |
| `BOUNDARY_RESOLVER_CACHE_SIZE` | `1000` | LRU cache max entries |
| `BOUNDARY_RESOLVER_CACHE_TTL` | `60` | Cache TTL in seconds |
| `BOUNDARY_POST_PROVISION_HOOK` | `None` | Callable after tenant provisioning |
| `BOUNDARY_PRE_DEPROVISION_HOOK` | `None` | Callable before tenant deletion |

---

## System Checks

| ID | Severity | Condition |
|----|----------|-----------|
| `boundary.E001` | Error | `BOUNDARY_TENANT_MODEL` missing or invalid |
| `boundary.E003` | Error | Resolver class cannot be imported |
| `boundary.E004` | Error | TenantMiddleware not in MIDDLEWARE |
| `boundary.E005` | Error | BOUNDARY_REGIONS set but RegionalRouter not in DATABASE_ROUTERS |
| `boundary.E006` | Error | TenantModel table missing RLS (queries pg_class at startup) |
| `boundary.W001` | Warning | STRICT_MODE is False |

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

---

## Signals

| Signal | Arguments | Fired when |
|--------|-----------|------------|
| `tenant_resolved` | `tenant, resolver, request` | After successful resolution |
| `tenant_resolution_failed` | `request` | No resolver matched (REQUIRED=True) |
| `strict_mode_violation` | `model, queryset` | Before TenantNotSetError is raised |

---

## Requirements

- Python 3.10+
- Django 4.2+ (LTS) or Django 5.x
- PostgreSQL 14+ (for RLS; ORM layer works with any database)

---

## Comparison with django-tenants

| | django-tenants | django-boundary |
|-|---------------|-----------------|
| Isolation | PostgreSQL schemas | Row-level + RLS |
| Scale ceiling | ~500 tenants | No architectural ceiling |
| Migration cost | O(n tenants) | O(1) |
| Async support | Thread-local (breaks async) | contextvars (native async) |
| Celery | Manual | Automatic via headers |
| Regional routing | Not supported | First-class |
| Dev enforcement | None | STRICT_MODE |

---

## Licence

MIT
