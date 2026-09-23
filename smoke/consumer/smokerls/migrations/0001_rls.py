"""The RLS layer for smokeapp.Booking, in an app of its own.

Two things shape this file, and both are consumer-visible facts about the
package rather than fixture convenience.

First, why a separate app rather than a second migration inside ``smokeapp``:
the RLS layer is the part a non-PostgreSQL deployment does not get, so it
belongs where it can be reasoned about (and, once
icvoss/django-boundary#75 lands, routed) on its own.

Second, why the operation list is built conditionally rather than gated by
the project's router: ``EnableRLS`` and ``CreateTenantPolicy`` emit
PostgreSQL DDL unconditionally and never consult
``router.allow_migrate()``, and Django's executor does not consult it on
their behalf either, because it only does so for operations that go through
``allow_migrate_model``. A router therefore cannot keep these operations off
a SQLite alias today, and pointing this migration at SQLite fails with
``OperationalError: near "ENABLE": syntax error``. That is
icvoss/django-boundary#75; when it lands, this condition collapses into the
router in ``routers.py`` and this comment goes with it.
"""

from django.conf import settings
from django.db import migrations

from boundary.migrations_ops import CreateTenantPolicy, EnableRLS

_IS_POSTGRESQL = settings.DATABASES["default"]["ENGINE"].endswith("postgresql")


class Migration(migrations.Migration):
    initial = True

    dependencies = [
        ("smokeapp", "0001_initial"),
    ]

    operations = (
        [
            EnableRLS("Booking", app_label="smokeapp"),
            CreateTenantPolicy("Booking", app_label="smokeapp"),
        ]
        if _IS_POSTGRESQL
        else []
    )
