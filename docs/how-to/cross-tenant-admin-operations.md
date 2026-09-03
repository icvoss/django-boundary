# Run cross-tenant admin operations

## Goal

Operate across every tenant at once, or against a tenant other than the one in the current context. This covers the deliberate escape hatches that boundary provides: the `unscoped` manager, the `all_regions()` context manager, the `boundary_run` and `boundary_run_all` management commands, and the row-level security (RLS) admin bypass flag.

These are sharp tools. By default boundary filters every query to the active tenant so a missing context is fail-closed. The escape hatches turn that protection off on purpose. Read the safety notes in each section before using them in production code.

## Prerequisites

- boundary is installed and `BOUNDARY_TENANT_MODEL` is set. See the [README](../../README.md) for setup.
- Your models inherit from `TenantMixin`, `TenantModel`, or a mixin produced by `make_tenant_mixin()`, so they expose both `objects` (tenant-filtered) and `unscoped` (unfiltered) managers.
- For the regional sections, `BOUNDARY_REGIONS` is configured and `boundary.routing.RegionalRouter` is in `DATABASE_ROUTERS`.
- For the RLS bypass section, you have applied the RLS policies via the migration operations. See the [README RLS section](../../README.md#row-level-security) for how policies are created.

## Steps

### 1. Read or write across all tenants with the `unscoped` manager

Every tenant-scoped model has an `unscoped` manager alongside the default `objects` manager. `unscoped` is a plain manager that does not apply the active-tenant filter, so it returns rows for all tenants regardless of context.

```python
from myapp.models import Booking

# Default manager: filtered to the active tenant (or fail-closed if none).
Booking.objects.count()

# Unscoped manager: every row, every tenant.
Booking.unscoped.count()
Booking.unscoped.filter(court=1)
total = Booking.unscoped.aggregate(total=Sum("price"))
```

Use this for platform-level reporting, analytics, and admin dashboards where you genuinely need to see across tenants.

When you create rows through `unscoped`, boundary does not auto-populate the tenant field from context. You must pass the tenant explicitly:

```python
# Correct: explicit tenant.
Booking.unscoped.create(court=1, tenant=some_tenant)

# Wrong: no tenant, no auto-populate. Raises IntegrityError on a non-null FK.
Booking.unscoped.create(court=1)
```

`unscoped.bulk_create()` behaves the same way: it skips auto-populate, so each object must already carry its tenant.

> Safety: a query on `unscoped` is a query with no isolation. Treat any code path that reaches `unscoped` as privileged. Keep it out of request handlers that serve tenant users, and prefer the tenant-filtered `objects` manager everywhere else.

### 2. Iterate across regional databases with `all_regions()`

In a multi-region deployment, tenant data is sharded across database aliases by region. A single unscoped query only hits the database it is routed to, so to aggregate across regions you must query each alias.

`all_regions()` yields the configured region aliases (or `["default"]` when `BOUNDARY_REGIONS` is unset), so you can loop over them with `.using()`:

```python
from boundary.routing import all_regions
from myapp.models import Booking

grand_total = 0
with all_regions() as aliases:
    for alias in aliases:
        grand_total += Booking.unscoped.using(alias).count()
```

To pin queries to one specific region instead of routing by the active tenant, use `specific_region()`:

```python
from boundary.routing import specific_region

with specific_region("eu-west"):
    bookings = Booking.unscoped.all()  # hits the eu-west database
```

An unknown region key in `specific_region()` falls back to the `default` alias rather than raising.

> Safety: combine `all_regions()` with `unscoped` only for cross-tenant aggregation. If you forget `.using(alias)`, you silently aggregate just one region and under-report.

### 3. Run a command for one specific tenant with `boundary_run`

`boundary_run` activates a tenant context, then calls another management command inside it. Use it to run a per-tenant job against a single tenant from the shell.

```bash
python manage.py boundary_run --tenant club-a send_reminders --dry-run
```

- `--tenant` is required. It accepts the tenant PK or slug. boundary tries PK first, then slug, and raises `CommandError` if neither matches.
- Everything after the inner command name is forwarded verbatim to that command (`--dry-run` above goes to `send_reminders`).

The inner command runs inside `TenantContext.using(tenant)`, so any boundary-aware code it executes is correctly scoped to that one tenant.

### 4. Run a command for every active tenant with `boundary_run_all`

`boundary_run_all` resolves every active tenant (`is_active=True`), then runs the inner command once per tenant, each inside its own tenant context.

```bash
# Sequentially, against all active tenants.
python manage.py boundary_run_all send_reminders

# 4 parallel workers, only EU tenants, machine-readable output.
python manage.py boundary_run_all send_reminders --parallel 4 --region eu-west --json
```

Options:

- `--parallel N`: number of concurrent workers (default `1`). Values above `1` run each tenant in a separate process via a multiprocessing pool.
- `--region REGION`: limit to tenants whose region field matches `REGION`. The field name comes from `BOUNDARY_REGION_FIELD` (default `region`).
- `--exclude PK`: skip a tenant by PK. Repeat the flag to exclude several: `--exclude 7 --exclude 9`.
- `--json`: emit one NDJSON object per tenant for piping into other tools.

Output is one line per tenant. In human mode, successes print `[OK] <slug>` to stdout and failures print `[FAIL] <slug>: <error>` to stderr. In `--json` mode each line is an object with `tenant` and `status` keys (and `error` on failure):

```json
{"tenant": "club-a", "status": "ok"}
{"tenant": "club-b", "status": "error", "error": "..."}
```

A failure in one tenant does not abort the run. Each tenant is isolated, so the loop continues and the failure is reported per tenant.

> Safety: `boundary_run_all` touches every active tenant. Test the inner command with `boundary_run --tenant <one>` first, then widen to `boundary_run_all`. When using `--parallel`, the inner command must be safe to run in multiple processes at once.

### 5. Bypass RLS for trusted maintenance work

When PostgreSQL row-level security is enabled, the database itself rejects rows outside the active tenant, even for the `unscoped` manager, unless the connection is a superuser or the admin bypass flag is set. boundary installs an admin bypass policy that lifts isolation, for both reads AND writes, when the `app.boundary_admin` session variable is `'true'`: the policy has no `WITH CHECK`, so PostgreSQL falls back to its `USING` clause for write checks too, and multiple permissive policies are combined with `OR`, so satisfying this policy alone is enough regardless of the tenant isolation policy's own `WITH CHECK`. An `INSERT` or `UPDATE` for a tenant other than the active one is accepted, not just made visible.

Use `boundary.context.admin_bypass()` to set the flag. It is the only supported way to reach it:

```python
from boundary.context import admin_bypass
from myapp.models import Booking

with admin_bypass():
    # Queries in this block see and write rows for all tenants, even with
    # FORCE ROW LEVEL SECURITY on the table.
    Booking.unscoped.filter(court=1).update(is_paid=True)
```

`admin_bypass()` hardcodes the transaction-local form of `set_config` (the third argument is always `true`): a consumer cannot reach the session-scoped form through this API, because the session-scoped form is unsafe under connection reuse (see below) and the whole point of the function is to remove that failure mode rather than merely warn about it. It guarantees an active transaction (opening one automatically unless `BOUNDARY_WRAP_ATOMIC=False`, matching `TenantContext.using()`), verifies the flag actually took effect before running your code, and clears it explicitly on exit rather than relying only on the transaction ending. It also fires `boundary.signals.admin_bypass_activated` on entry, so every use of this escape hatch is observable. See the [README signals reference](../../README.md#signals) and the docstring on `admin_bypass()` for the full contract, including nested use and multi-region operations.

The variable name is configurable via `BOUNDARY_ADMIN_FLAG_VAR` (default `app.boundary_admin`); `admin_bypass()` always reads it from settings rather than a hardcoded string, so a customised name is honoured automatically.

If you must set the flag with a hand-written `set_config` call (there is no supported reason to, but if you are debugging boundary itself or writing a migration operation), the third argument MUST be `true`, never `false` or omitted. The difference is not cosmetic:

- **Transaction-local (`true`, what `admin_bypass()` always uses):** scoped to the current transaction. It clears automatically on commit or rollback, and it cannot survive a connection being returned to a pool or reused for the next request under `CONN_MAX_AGE`, because the transaction that held it is already gone by the time that happens.
- **Session-scoped (`false`, never reachable through `admin_bypass()`):** persists on the connection until something clears it, across every subsequent statement and transaction on that connection. Under Django's `CONN_MAX_AGE` (a connection reused across multiple requests) or behind an external pooler such as PgBouncer (a connection handed to a completely different, unrelated request), a session-scoped flag set for one piece of maintenance work silently stays active for whatever request or task picks up that connection next, with no isolation at all and no indication why. `DISCARD ALL`, which some poolers run on connection handback, resets session-level `set_config` state, but relying on pooler configuration to clean up after an unsafe API is a second point of failure, not a fix. This is the exact failure mode `admin_bypass()` exists to make unreachable.

> Safety: the admin flag disables the database's last line of defence. Use `admin_bypass()` for trusted maintenance only, never on a connection that serves tenant traffic, and never assume it is still active outside the `with` block: `admin_bypass()` clears it explicitly, but a hand-rolled session-scoped `set_config` would not.

## Verify it worked

- `unscoped`: with two tenants each owning one row, assert `Model.unscoped.count() == 2` while a single tenant is active, and `Model.objects.count() == 1`.
- `all_regions()`: with `BOUNDARY_REGIONS` set to three regions, confirm the yielded aliases match the configured keys; with it unset, confirm you get `["default"]`.
- `boundary_run`: run it with a harmless inner command such as `python manage.py boundary_run --tenant <slug> showmigrations --list` and confirm no `CommandError`.
- `boundary_run_all`: run `python manage.py boundary_run_all showmigrations --json` and confirm one JSON line per active tenant, each with `status: ok`.
- RLS bypass: with isolation applied and rows for two tenants, enter `admin_bypass()` and confirm a raw `SELECT count(*)` returns the full count instead of the per-tenant count, and that `current_setting('app.boundary_admin', true)` reads `''` again immediately after the block exits.

## Common pitfalls

- Calling `unscoped.create()` or `unscoped.bulk_create()` without setting the tenant explicitly: auto-populate is skipped, so a non-null FK raises `IntegrityError`. Always pass `tenant=...`.
- Expecting `unscoped` to cross regions: it only queries the database it routes to. Wrap it in `all_regions()` and use `.using(alias)` to span shards.
- Reaching for `unscoped` in tenant-facing request code: this defeats isolation. Use the default `objects` manager and a proper `TenantContext` instead. See [How tenant resolution works](../explanation/how-resolution-works.md).
- Forgetting that `boundary_run_all` only targets `is_active=True` tenants. Inactive tenants are skipped silently.
- Passing inner-command flags before the inner command name in `boundary_run`. The inner command name comes first, then its arguments.
- Setting `app.boundary_admin` by hand instead of via `admin_bypass()`. A hand-written `set_config` call with the third argument `false` (or a `SET` statement, which is always session-scoped) leaves the flag active on the connection indefinitely, and under `CONN_MAX_AGE` or an external pooler that connection is handed to a later, unrelated request. `admin_bypass()` makes this failure mode unreachable by hardcoding the transaction-local form; there is no supported way to opt into the session-scoped form through it.
- Assuming `admin_bypass()` only widens what is visible. It also lifts the write check (no `WITH CHECK` on the bypass policy, permissive policies OR together), so code inside the block can INSERT or UPDATE rows for any tenant, not only read them.

## Related

- [README](../../README.md) for the full settings reference, RLS setup, and the regional routing model.
- README sections on the [`unscoped` manager](../../README.md#models), [`all_regions` / `specific_region`](../../README.md#multi-region-with-data-residency), the [`boundary_run` / `boundary_run_all` commands](../../README.md#management-commands), and [`admin_bypass()`](../../README.md#admin-bypass).
