"""The RLS layer for smokeapp.Booking, in an app of its own.

Two things shape this file, and both are consumer-visible facts about the
package rather than fixture convenience.

First, why a separate app rather than a second migration inside ``smokeapp``:
the RLS layer is the part a non-PostgreSQL deployment does not get, so it
belongs where it can be reasoned about on its own.

Second, why the operation list is STILL built conditionally now that
icvoss/django-boundary#75 has landed. #75 gave ``EnableRLS`` and
``CreateTenantPolicy`` the router and vendor gates (BR-RLS-021), and the
intent was for this conditional to collapse into a router denying this app
on a non-PostgreSQL alias. It cannot, for a reason the rule itself states:
the router gate asks ``router.allow_migrate_model(alias, model)`` about the
RESOLVED model, which under the ``app_label="smokeapp"`` override is
``smokeapp.Booking``, not this migration's own ``smokerls``. So:

- Denying ``smokerls`` does nothing. The router is never asked about it, and
  the vendor gate then refuses by name with ``RLSOperationRefusedError``.
- Denying ``smokeapp`` denies the table as well. Django's own ``CreateModel``
  calls the identical ``allow_migrate_model(alias, model)`` with the same
  four arguments, and boundary passes no hint distinguishing its RLS gate
  from that call, so no router can separate them. Verified against this
  project on SQLite: with ``smokeapp`` denied, ``migrate`` reports every
  migration as applied and the database ends up with NO smoke tables at all
  (``smokeapp_booking`` and ``smokeapp_organisation`` both absent). The gate
  would then pass while proving nothing, which is worse than this
  conditional.

The router shape BR-RLS-013 and BR-RLS-021 describe works for ADOPTED apps,
where denying the target app correctly denies both its DDL and its RLS layer.
It does not reach a column-bearing model in the consumer's own app, because
there the two are the same ``allow_migrate_model`` question. That is
icvoss/django-boundary#86; this conditional stays until a consumer has a way
to route the RLS layer away from a table without routing the table away with
it, and this comment goes with it then.
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
