# The ADR-027 consumer smoke gate

A throwaway Django project that installs django-boundary the way a real
consumer does, and runs the checks a real consumer runs. It is CI fixture
code: it is not shipped in the wheel, and it is not part of the test suite.

## Why it exists

ADR-027 was written after two defects reached the index that only a real
consumer could have caught: a model change shipped with no migration (which a
consumer cannot self-remedy, because Django wants to write the migration into
site-packages) and a package that did not typecheck clean under django-stubs.
Boundary's own CI runs the package's tests against the package's own bundled
test settings, which never exercises it the way an installing project does.

So this fixture installs a **built wheel into a clean virtualenv** and runs:

1. `manage.py makemigrations --check --dry-run`
2. `manage.py migrate` on a fresh database, then `manage.py check`
3. `mypy` with the django-stubs plugin, under the pair `pyproject.toml`'s dev
   extra declares

once on SQLite and once on PostgreSQL, because a migration or typing defect
can appear on only one backend.

Not editable, and not boundary's own `tests/boundary_testapp`: an editable
install puts `src/` on `sys.path`, and the gate would then be testing the
working tree. `smoke/run.sh` asserts `boundary.__file__` resolves inside
`site-packages` before it runs anything.

## Running it locally

```
python -m build --wheel --outdir dist/
./smoke/run.sh sqlite
./smoke/run.sh sqlite --mypy-only
```

For the PostgreSQL pass, create a database and a non-superuser role, then
point the standard `PG*` variables at it:

```
psql -h localhost -U icv_test -d postgres -c "CREATE DATABASE boundary_smoke;"
psql -h localhost -U icv_test -d boundary_smoke \
    -c "GRANT CONNECT ON DATABASE boundary_smoke TO icv_app;
        GRANT USAGE, CREATE ON SCHEMA public TO icv_app;"

PGHOST=localhost PGDATABASE=boundary_smoke PGUSER=icv_app PGPASSWORD=icv_dev \
    ./smoke/run.sh postgresql
```

The consumer connects as `icv_app` rather than the bootstrap superuser, so
`manage.py check` sees a non-bypassing role and its output is representative
of a deployment. Connecting as a superuser would suppress nothing but would
add a `boundary.W003` that a real deployment does not see.

`run.sh` drops the smoke tables before migrating, because ADR-027 step 2 is
"migrate on a **fresh** database" and a migrate that no-ops against tables
left by the previous run proves nothing.

## What is in here

| Path | What it is |
|---|---|
| `run.sh` | Builds the venv, installs the wheel, runs the steps |
| `consumer/settings.py` | One project, both backends, selected by `BOUNDARY_SMOKE_DB` |
| `consumer/smokeapp/` | The consumer's own tenant (`Organisation`) and one scoped model (`Booking`) |
| `consumer/smokerls/` | The RLS migration, in an app of its own (see below) |
| `consumer/mypy.ini` | The django-stubs plugin, pointed at the consumer's settings |

The consumer uses boundary the documented way, because ADR-027 says a fixture
that does not use the package the way real consumers do "tests nothing": a
concrete tenant inheriting `AbstractTenant` and named by
`BOUNDARY_TENANT_MODEL`, a scoped model inheriting `TenantMixin`,
`TenantMiddleware` mounted, and `EnableRLS` plus `CreateTenantPolicy` in a
migration.

## The one piece of scaffolding, and why

`smokerls/migrations/0001_rls.py` builds its `operations` list conditionally
on the configured engine, rather than letting a router keep it off SQLite.
That is not the pattern a consumer should want, and it stays for a reason
that outlived the one it was written for.

It was originally here because of icvoss/django-boundary#75: `EnableRLS` and
`CreateTenantPolicy` consulted no router at all, so nothing could keep them
off a SQLite alias. #75 has now landed, and BR-RLS-021 gives all four RLS
operations the router and vendor gates. The condition still cannot collapse
into a router, because of **which model the router is asked about**.

Gate 1 asks `router.allow_migrate_model(alias, model)` about the *resolved*
model, which under this migration's `app_label="smokeapp"` override is
`smokeapp.Booking`, not `smokerls`. So both available router keys fail:

- Denying `smokerls` does nothing, because the router is never asked about
  it. Gate 2 then refuses by name with `RLSOperationRefusedError`, whose
  message advises the very thing that does not work here.
- Denying `smokeapp` denies the **table** too, because Django's own
  `CreateModel` makes the identical `allow_migrate_model(alias, model)` call
  and boundary passes no hint distinguishing its RLS gate from it. Verified
  on SQLite: `migrate` reports everything applied, exits 0, and the database
  ends up with no smoke tables at all. The gate would pass while proving
  nothing.

The router shape BR-RLS-013 and BR-RLS-021 describe works for **adopted**
apps, where denying the target app correctly denies both its DDL and its RLS
layer. It does not reach a column-bearing model in the consumer's own app.
That is icvoss/django-boundary#86, and this condition stays until it is
resolved.

## Adding to it

Keep it small. Every model here has to earn its place by exercising something
a consumer's CI would exercise, and every addition is another thing that can
fail for a reason that is not about the package.
