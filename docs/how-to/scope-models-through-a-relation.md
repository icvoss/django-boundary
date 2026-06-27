# Scope a model to a tenant through a relation

## Goal

Make a model tenant-scoped when it does **not** carry its own tenant foreign key
but reaches the tenant through a relation, for example an `ExportLog` that
belongs to a `Destination` which belongs to a merchant. The model should
auto-filter by the active tenant just like a direct-FK model, without dropping
to a hand-written manager.

## Prerequisites

- django-boundary installed and configured with `BOUNDARY_TENANT_MODEL`.
- A model that already has the relation that leads to the tenant (the FK chain
  exists; only the tenant scoping is missing).

## When to use this

Use `make_tenant_path_mixin` when the tenant is reached by a lookup **path**
rather than a local column:

| Shape | Mixin |
| --- | --- |
| Model has its own `tenant`/`merchant` FK | `TenantMixin` / `make_tenant_mixin("merchant")` |
| Model reaches the tenant through a relation | `make_tenant_path_mixin("destination__merchant")` |

If you find yourself writing a bespoke `Manager` with a
`for_tenant`/`for_merchant` method that does `filter(rel__rel__merchant=...)`,
that is exactly the case this replaces.

## Steps

1. Build a mixin from the lookup path to the tenant and apply it:

   ```python
   from django.db import models
   from boundary.models import make_tenant_path_mixin

   ExportScopedMixin = make_tenant_path_mixin("destination__merchant")

   class ExportLog(ExportScopedMixin, TimeStampedModel):
       destination = models.ForeignKey(Destination, on_delete=models.CASCADE)
       # ExportLog.objects auto-filters on destination__merchant
   ```

   The path is just a Django ORM lookup string, so **multi-hop paths work the
   same way**:

   ```python
   make_tenant_path_mixin("export_log__destination__merchant")
   ```

2. Use the model exactly like any tenant-scoped model:

   ```python
   with TenantContext.using(merchant):
       ExportLog.objects.all()          # only this merchant's export logs
       ExportLog.objects.create(destination=dest)   # no tenant kwarg needed
   ```

   The mixin adds **no foreign key** — the model already has the relation the
   path traverses. There is no `tenant`/`merchant` column on the table, and
   nothing to populate on save.

## Verify it worked

```python
with TenantContext.using(merchant_a):
    ExportLog.objects.create(destination=dest_a)
with TenantContext.using(merchant_b):
    ExportLog.objects.create(destination=dest_b)

with TenantContext.using(merchant_a):
    assert ExportLog.objects.count() == 1      # auto-filtered

assert ExportLog.unscoped.count() == 2         # bypass still sees all
```

## How it differs from a direct-FK model

Path-scoped models have no local tenant column, which changes a few behaviours.
boundary handles all of this for you:

- **No auto-populate.** `save()` and `bulk_create()` do not try to set a tenant
  column (there is none); scoping comes entirely from the relation you set.
- **No cross-tenant `bulk_update` check.** There is no column to compare, so the
  validation that direct-FK models get is skipped.
- **`get_or_create` / `update_or_create` inject nothing.** They rely on the
  auto-filtered queryset to scope the lookup.
- **No RLS on this table.** A PostgreSQL Row Level Security policy needs a local
  column. Path-scoped models are **excluded** from boundary's RLS system check
  and provisioning. Database-level isolation must come from the parent on the
  path (e.g. `Destination` carries the RLS policy on its `merchant_id`), with
  application-layer auto-filtering protecting the path model itself. If you need
  RLS directly on the row, give the model its own FK and use `TenantMixin`
  instead.

## Introspection

```python
from boundary.models import (
    is_tenant_model, has_tenant_column, get_tenant_lookup, get_tenant_fk_field,
)

is_tenant_model(ExportLog)      # True
has_tenant_column(ExportLog)    # False — no local column
get_tenant_lookup(ExportLog)    # "destination__merchant"
get_tenant_fk_field(ExportLog)  # None — there is no column
```

## Related

- [Set up a tenant model](set-up-a-tenant-model.md)
- [Add RLS policies with migrations](add-rls-policies-with-migrations.md)
- [Customise the terminology](customise-terminology.md)
