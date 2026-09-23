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
That is not the pattern a consumer should want, and it is here because of
icvoss/django-boundary#75: `EnableRLS` and `CreateTenantPolicy` emit
PostgreSQL DDL unconditionally and never consult `router.allow_migrate()`,
and Django's executor does not consult it on their behalf either, because it
only does so for operations that go through `allow_migrate_model`. A router
therefore cannot gate them today, and pointing the migration at SQLite fails
with `OperationalError: near "ENABLE": syntax error`.

When #75 lands, the condition collapses into an ordinary router and the
comment in that file goes with it.

## Adding to it

Keep it small. Every model here has to earn its place by exercising something
a consumer's CI would exercise, and every addition is another thing that can
fail for a reason that is not about the package.
