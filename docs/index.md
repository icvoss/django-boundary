# django-boundary documentation

Scalable row-level multi-tenancy for Django with PostgreSQL Row Level Security.

These guides are task-focused. For the exhaustive API and option tables, see the
[README](../README.md), which is the reference layer. For per-setting detail, see
the [settings reference](reference/settings.md).

## I want to...

| I want to... | Go to |
| --- | --- |
| Build a multi-tenant app from scratch | [Build your first multi-tenant app](tutorial/first-multi-tenant-app.md) |
| Define the tenant model itself | [Set up a tenant model](how-to/set-up-a-tenant-model.md) |
| Scope a model that reaches the tenant through a relation | [Scope a model through a relation](how-to/scope-models-through-a-relation.md) |
| Scope a service or task that already holds the tenant | [Scope a function that holds the tenant](how-to/scope-a-function-that-holds-the-tenant.md) |
| Add boundary to an app that already has data | [Add boundary to an existing app](how-to/add-boundary-to-an-existing-app.md) |
| Decide how requests map to tenants | [Choose and order resolvers](how-to/choose-and-order-resolvers.md) |
| Rename "tenant" to merchant, organisation, club | [Customise the terminology](how-to/customise-terminology.md) |
| Write tests that don't leak across tenants | [Write tenant-safe tests](how-to/write-tenant-safe-tests.md) |
| Enforce isolation at the database level | [Add RLS policies with migrations](how-to/add-rls-policies-with-migrations.md) |
| Keep tenant context inside Celery tasks | [Run Celery tasks with tenant context](how-to/run-celery-tasks-with-tenant-context.md) |
| Serve tenants from region-specific databases | [Deploy across multiple regions](how-to/deploy-multi-region.md) |
| Safely operate across all tenants | [Run cross-tenant admin operations](how-to/cross-tenant-admin-operations.md) |
| Create or delete tenants | [Provision and deprovision tenants](how-to/provision-and-deprovision-tenants.md) |
| Understand why isolation has two layers | [Isolation layers and the threat model](explanation/isolation-layers.md) |
| Understand how a request becomes a tenant | [How tenant resolution works](explanation/how-resolution-works.md) |
| Look up a `BOUNDARY_` setting | [Settings reference](reference/settings.md) |
| Fix an error or unexpected behaviour | [Troubleshooting](troubleshooting.md) |

## How these docs are organised

The documentation follows the [Diátaxis](https://diataxis.fr/) model:

- **[Tutorial](tutorial/first-multi-tenant-app.md)** — a single, opinionated path
  from nothing to a working multi-tenant app. Start here if you are new.
- **How-to guides** — recipes for specific tasks. Each one states a goal,
  lists prerequisites, gives runnable steps, and shows how to verify the result.
- **Explanation** — the "why": the threat model, the two isolation layers, and
  the request-to-tenant lifecycle. Read these to build a mental model.
- **[Settings reference](reference/settings.md)** and the
  [README](../README.md) — the exhaustive lookup tables.
- **[Troubleshooting](troubleshooting.md)** — symptom, cause, fix.

## New to django-boundary?

1. Read [Isolation layers and the threat model](explanation/isolation-layers.md)
   to understand what the package protects against.
2. Follow the [tutorial](tutorial/first-multi-tenant-app.md) end to end.
3. Keep the [settings reference](reference/settings.md) and
   [troubleshooting](troubleshooting.md) page open as you integrate.
