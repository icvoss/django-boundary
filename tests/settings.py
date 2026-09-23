"""
Django settings for django-boundary standalone tests.

Used by the publish workflow (CI) and for running tests independently
of the monorepo sandbox settings. PostgreSQL is the default backend, and is
required for the RLS tests.

Set ``BOUNDARY_TEST_DB=sqlite`` to run the SQLite leg of the CI matrix
instead, which puts SQLite on the ``default`` alias to prove BR-ENV-002's
quiet returns on a backend with no Row Level Security. That leg runs with
``-m "not rls"``, deselecting the tests that need a PostgreSQL RLS backend
rather than skipping them at runtime (BR-ENV-006).
"""

import os

SECRET_KEY = "boundary-test-secret-key"  # noqa: S105

#: The SQLite leg of the CI matrix (BR-ENV-006). Not a general-purpose knob:
#: it selects a leg of the supported matrix, not a consumer configuration.
BOUNDARY_TEST_DB = os.environ.get("BOUNDARY_TEST_DB", "postgresql").lower()
_SQLITE_LEG = BOUNDARY_TEST_DB == "sqlite"

INSTALLED_APPS = [
    "django.contrib.contenttypes",
    "django.contrib.auth",
    "boundary",
    "boundary_testapp",
    # A stand-in for a third-party package, plus the consuming project's own
    # app that adopts it. The adoption tests need a genuinely passive app
    # (no boundary import, no mixin, no tenant field) and a separate app to
    # put the adoption migration in, because BR-RLS-013 requires the DDL to
    # be applied from the consumer's migration, never the adopted app's.
    "thirdparty",
    "boundary_consumer",
]

# PostgreSQL by default: the RLS tests use raw SQL against pg_class. The
# SQLite leg replaces this alias wholesale (BR-ENV-002, BR-ENV-006).
_DEFAULT_ALIAS = (
    {
        "ENGINE": "django.db.backends.sqlite3",
        # Django rewrites ":memory:" to a shared-cache in-process database
        # (file:memorydb_default?mode=memory&cache=shared), so the extra
        # connections a transaction=True test opens all see the same data.
        "NAME": ":memory:",
    }
    if _SQLITE_LEG
    else {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": os.environ.get("POSTGRES_DB", "boundary_test"),
        "USER": os.environ.get("POSTGRES_USER", "icv_test"),
        "PASSWORD": os.environ.get("POSTGRES_PASSWORD", "icv_test_password"),
        "HOST": os.environ.get("POSTGRES_HOST", "localhost"),
        "PORT": os.environ.get("POSTGRES_PORT", "5432"),
    }
)

DATABASES = {
    "default": _DEFAULT_ALIAS,
    # A second, genuinely separate alias for regional-routing tests that
    # must prove a row landed on a non-default database (issue #62). SQLite
    # keeps this alias free of the PostgreSQL fixture/role setup the tests
    # above need, since these tests only exercise Command.save(using=...)
    # against a plain table, never RLS.
    "eu-west": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": ":memory:",
        # thirdparty and boundary_consumer are the only apps with a real
        # migration graph (see MIGRATION_MODULES below), and the consumer's
        # adoption migration emits PostgreSQL DDL that SQLite cannot parse.
        # A real project keeps a non-PostgreSQL alias off that graph with a
        # router's allow_migrate(); a global DATABASE_ROUTERS is not usable
        # here because test_checks.py sets that setting per-test and would
        # override it. The alias only ever needs boundary_testapp.Tenant,
        # which is unmigrated and is created by the test runner regardless
        # of this flag, so skipping the graph costs the alias nothing.
        "TEST": {"MIGRATE": False},
    },
}

MIGRATION_MODULES = {
    "boundary": None,
    "boundary_testapp": None,
    "contenttypes": None,
    "auth": None,
    # thirdparty and boundary_consumer deliberately KEEP their real migration
    # modules. AC-RLS-011 and AC-RLS-013 drive AdoptTenantApp through
    # MigrationExecutor so real historical state is exercised, and an app
    # mapped to None here has no migration graph to drive at all. It also
    # fixes the ordering: an unmigrated app's tables are created by the test
    # runner in post_migrate, which is after every migration, so the
    # consumer's adoption migration would otherwise run before the tables it
    # adopts exist.
}

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

USE_TZ = True
TIME_ZONE = "UTC"

ALLOWED_HOSTS = ["*"]

# Only the ASGI integration test in test_middleware.py needs a URLconf (it
# drives an async view through django.test.AsyncClient). MIDDLEWARE is
# deliberately not set here: several tests in test_checks.py assert on
# boundary.E004/W002 by setting settings.MIDDLEWARE themselves per-test, and
# a global default here would just be overridden by every one of them, so
# the ASGI test sets MIDDLEWARE itself via the settings fixture instead.
ROOT_URLCONF = "urls"

# Boundary settings
BOUNDARY_TENANT_MODEL = "boundary_testapp.Tenant"
BOUNDARY_STRICT_MODE = True

if _SQLITE_LEG:
    # BR-ENV-006: the SQLite leg needs a router, and that is the point of it.
    # thirdparty and boundary_consumer keep their real migration modules
    # (see MIGRATION_MODULES above), and the consumer's adoption migration
    # applies AdoptTenantApp. On a SQLite alias the router ADMITS, BR-RLS-013
    # and BR-RLS-021 make that operation refuse by name, so the leg could not
    # create its test database at all. Denying allow_migrate() for both apps
    # is the documented way a project keeps a non-PostgreSQL alias off an RLS
    # migration graph, and the test database existing is the router gate's
    # own live exercise rather than scaffolding around it.
    #
    # The caveat, and why this is safe here: tests/test_checks.py assigns
    # settings.DATABASE_ROUTERS directly in its boundary.E005 coverage, so
    # this router is overwritten for the duration of those tests. That is
    # harmless on this leg, because migrations run only once, at
    # test-database creation, long before any test body executes; nothing
    # after that point asks allow_migrate() about these apps. It is NOT a
    # safe place to leave unguarded: a future test that drives a migration
    # after reassigning the setting would meet the vendor refusal, which
    # fails the leg loudly rather than quietly, and is the acceptable
    # failure mode.
    DATABASE_ROUTERS = ["sqlite_leg.DenyRlsMigrationsRouter"]
