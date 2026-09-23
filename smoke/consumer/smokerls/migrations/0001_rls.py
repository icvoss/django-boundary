"""The RLS layer for smokeapp.Booking, in an app of its own.

Two plain operations, no backend conditional and no router. This is the whole
point of the icvoss/django-boundary#86 ruling: ``EnableRLS`` and
``CreateTenantPolicy`` apply on PostgreSQL and are a **logged no-op**
elsewhere, so one migration file serves both backends and a consumer
developing on SQLite writes nothing special.

What each backend does with this file, which the smoke gate runs on both:

- **PostgreSQL**: both operations apply. ``smokeapp_booking`` ends up with
  row security enabled and forced, carrying ``boundary_tenant_isolation`` and
  ``boundary_admin_bypass``.
- **SQLite**: both operations return after one ``logger.info`` line each on
  ``boundary.migrations``, naming the operation, ``smokeapp.Booking``, the
  alias and the vendor. No DDL, no error, and ``migrate`` reports the
  migration applied. ``Booking`` keeps its ORM-layer tenant filtering, which
  is what BR-ENV-002 promises on that backend; it is the RLS layer, and only
  that, which is absent.

Why a separate app rather than a second migration inside ``smokeapp``: the
RLS layer is the part a non-PostgreSQL deployment does not get, so it stays
where it can be reasoned about on its own. That is now a readability choice
rather than a routing one; nothing keys on the app boundary.

History, because this file was the evidence in two rulings. It carried a
``settings.DATABASES[...]["ENGINE"].endswith("postgresql")`` conditional
while the operations consulted no router (#75), then still carried it after
#75 landed, because the router gate asks about the RESOLVED model and so
could not distinguish boundary's RLS gate from Django's own ``CreateModel``
call for the same model: denying the app dropped the table with it (#86).
The #86 ruling removed the need for either by making the vendor gate a
logged no-op, and this file is what a consumer writes now.
"""

from django.db import migrations

from boundary.migrations_ops import CreateTenantPolicy, EnableRLS


class Migration(migrations.Migration):
    initial = True

    dependencies = [
        ("smokeapp", "0001_initial"),
    ]

    operations = [
        EnableRLS("Booking", app_label="smokeapp"),
        CreateTenantPolicy("Booking", app_label="smokeapp"),
    ]
