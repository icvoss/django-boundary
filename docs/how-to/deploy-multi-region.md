# Deploy across multiple regions

## Goal

Route each tenant's queries to a geographically distinct database so that
tenant data stays in its required region (for example, UK data in a UK
database, EU data in an EU database). This is how django-boundary supports
data residency requirements.

## How regional routing works

Boundary ships a Django database router, `RegionalRouter`, that inspects the
active tenant in `TenantContext` and returns the database alias that matches
the tenant's region.

1. Each tenant stores a region key in a field (default `region`).
2. `BOUNDARY_REGIONS` maps each region key to a database configuration.
3. `RegionalRouter` reads the active tenant's region and routes reads and
   writes for tenant-scoped models to the matching alias.

The router falls back to the `default` alias, never raising, when any of the
following are true:

- `BOUNDARY_REGIONS` is not configured.
- No tenant is active in `TenantContext`.
- The model is not a tenant-scoped model (non-tenant models always use
  `default`).
- The tenant's region key is not present in `BOUNDARY_REGIONS`.

This fail-safe behaviour means a misconfigured region degrades to the default
database rather than crashing a request.

## Prerequisites

- Boundary already configured with `BOUNDARY_TENANT_MODEL` set and a working
  tenant context (see the [README](../../README.md) for base setup).
- Your tenant model has a region field. The supplied tenant base classes
  include a `region` field by default. If you use a different field name, set
  `BOUNDARY_REGION_FIELD` accordingly.
- One database per region, each defined in Django's `DATABASES` setting.

## Steps

1. Define one database alias per region in `DATABASES`. The alias names must
   match the keys you will use in `BOUNDARY_REGIONS`.

   ```python
   # settings.py
   DATABASES = {
       "default": {
           "ENGINE": "django.db.backends.postgresql",
           "NAME": "app_default",
           "HOST": "default.db.example.com",
       },
       "uk": {
           "ENGINE": "django.db.backends.postgresql",
           "NAME": "app_uk",
           "HOST": "uk.db.example.com",
       },
       "eu-west": {
           "ENGINE": "django.db.backends.postgresql",
           "NAME": "app_eu",
           "HOST": "eu.db.example.com",
       },
   }
   ```

2. Configure `BOUNDARY_REGIONS`. The keys are the region values stored on your
   tenants and must match the database aliases above. The values are database
   configuration dicts (the same shape Django uses in `DATABASES`).

   ```python
   # settings.py
   BOUNDARY_REGIONS = {
       "uk":      {"ENGINE": "django.db.backends.postgresql", "HOST": "uk.db.example.com"},
       "eu-west": {"ENGINE": "django.db.backends.postgresql", "HOST": "eu.db.example.com"},
   }
   ```

3. Add `RegionalRouter` to `DATABASE_ROUTERS`.

   ```python
   # settings.py
   DATABASE_ROUTERS = ["boundary.routing.RegionalRouter"]
   ```

4. If your tenant model stores the region under a field other than `region`,
   point Boundary at it.

   ```python
   # settings.py
   BOUNDARY_REGION_FIELD = "data_region"
   ```

5. Set each tenant's region to a key that exists in `BOUNDARY_REGIONS`.

   ```python
   tenant.region = "uk"
   tenant.save()
   ```

## Verify it worked

With a tenant active, the router resolves to the tenant's region alias for
tenant-scoped models, and to `default` for everything else.

```python
from boundary.context import TenantContext
from boundary.routing import RegionalRouter
from myapp.models import Patient  # a tenant-scoped model

uk_tenant.region = "uk"
uk_tenant.save()

router = RegionalRouter()

with TenantContext.using(uk_tenant):
    assert router.db_for_read(Patient) == "uk"
    assert router.db_for_write(Patient) == "uk"

    # Writes land in the UK database automatically
    Patient.objects.create(name="Smith")
```

Non-tenant models, and queries with no active tenant, route to `default`:

```python
from django.contrib.auth.models import User

with TenantContext.using(uk_tenant):
    assert router.db_for_read(User) == "default"
```

## Working across all regions

Platform-wide reporting needs to read from every regional database. Use the
`all_regions()` context manager, which yields the list of configured region
aliases (or `["default"]` when `BOUNDARY_REGIONS` is unset). Pass each alias to
`.using()` explicitly.

```python
from boundary.routing import all_regions
from myapp.models import Patient

with all_regions() as aliases:
    for alias in aliases:
        count = Patient.objects.using(alias).count()
        print(f"{alias}: {count} patients")
```

## Pinning queries to one region

To run queries against a specific region regardless of the active tenant's
region, wrap the block in `specific_region()`. While the context manager is
active, `RegionalRouter` routes every tenant-scoped query to the given region
key.

```python
from boundary.routing import specific_region
from myapp.models import Booking

with specific_region("eu-west"):
    bookings = Booking.objects.all()  # hits the eu-west database

# Outside the block, routing reverts to the active tenant's region
```

If the key passed to `specific_region()` is not present in `BOUNDARY_REGIONS`,
the router falls back to `default` for the duration of the block rather than
raising.

## RegionNotConfiguredError

`boundary.exceptions.RegionNotConfiguredError` represents the case where a
tenant's region is not present in `BOUNDARY_REGIONS`. It is part of the
exception hierarchy and inherits from `BoundaryError`, so you can catch it
alongside the rest of Boundary's exceptions:

```python
from boundary.exceptions import BoundaryError, RegionNotConfiguredError
```

`RegionalRouter` itself never raises this exception: a Django router must
always return a database alias, so it logs a debug message and falls back to
the `default` alias to keep requests working. When silent fallback to `default`
is unacceptable (for example, data residency), use `require_region()` to fail
loudly instead:

```python
from boundary.routing import require_region
from boundary.exceptions import RegionNotConfiguredError

# Resolve the active tenant's region, or raise if it is not routable.
try:
    alias = require_region()           # uses the active tenant by default
except RegionNotConfiguredError:
    # regions unconfigured, no tenant active, or region not in BOUNDARY_REGIONS
    ...

# Or check a specific tenant, e.g. before a cross-region batch job:
alias = require_region(tenant)
```

`require_region(tenant=None)` returns the region alias the tenant routes to, or
raises `RegionNotConfiguredError` if `BOUNDARY_REGIONS` is unset, no tenant is
active, or the tenant's region is not configured. Use it at provisioning time or
before a regional job to reject unroutable tenants before any query runs.

## Common pitfalls

- **Region key does not match a database alias.** A `BOUNDARY_REGIONS` key is
  used directly as the database alias returned by the router. If there is no
  matching entry in `DATABASES`, Django raises `ConnectionDoesNotExist` when
  the query runs. Keep the keys in `BOUNDARY_REGIONS` and the aliases in
  `DATABASES` identical.

- **Forgetting to add the router.** Setting `BOUNDARY_REGIONS` alone does
  nothing: you must also add `boundary.routing.RegionalRouter` to
  `DATABASE_ROUTERS`. Without it, every query stays on `default`. System
  check `boundary.E005` (Error) catches this at startup: it fires when
  `BOUNDARY_REGIONS` is configured but `RegionalRouter` (or a subclass, or
  an already-constructed instance) is absent from `DATABASE_ROUTERS`.

- **Expecting silent fallback to be an error.** An unknown tenant region routes
  to `default` rather than failing. If you need data residency to be strictly
  enforced, call `require_region()` at provisioning time or before a regional job
  (see above) instead of relying on the router, which cannot raise.

- **No tenant in context.** Outside an active `TenantContext`, tenant-scoped
  queries route to `default`. Wrap region-sensitive work in
  `TenantContext.using(tenant)` or rely on the tenant middleware.

- **Cross-region relations.** The router allows queries per region but does not
  manage migrations or relations across regional databases for you. Run
  migrations against each alias and avoid relations that span regions.

## Related

- [README: Regional Routing](../../README.md#regional-routing) for the full
  settings reference, including `BOUNDARY_REGIONS` and `BOUNDARY_REGION_FIELD`.
- [README: Multi-region with data residency](../../README.md#multi-region-with-data-residency)
  for a worked end-to-end example.
