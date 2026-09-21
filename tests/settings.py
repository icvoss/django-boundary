"""
Django settings for django-boundary standalone tests.

Used by the publish workflow (CI) and for running tests independently
of the monorepo sandbox settings. Requires PostgreSQL for RLS tests.
"""

import os

SECRET_KEY = "boundary-test-secret-key"  # noqa: S105

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

# PostgreSQL required — RLS tests use raw SQL against pg_class.
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": os.environ.get("POSTGRES_DB", "boundary_test"),
        "USER": os.environ.get("POSTGRES_USER", "icv_test"),
        "PASSWORD": os.environ.get("POSTGRES_PASSWORD", "icv_test_password"),
        "HOST": os.environ.get("POSTGRES_HOST", "localhost"),
        "PORT": os.environ.get("POSTGRES_PORT", "5432"),
    },
    # A second, genuinely separate alias for regional-routing tests that
    # must prove a row landed on a non-default database (issue #62). SQLite
    # keeps this alias free of the PostgreSQL fixture/role setup the tests
    # above need, since these tests only exercise Command.save(using=...)
    # against a plain table, never RLS.
    "eu-west": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": ":memory:",
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
