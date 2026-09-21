"""Remove a tenant and optionally export its data.

Covers all three tenant-scoped model categories (BR-RLS-010). Column-bearing
and path-scoped models are reached through their scoped managers; adopted
tables (BR-PRV-009) have no manager, no Django field and no ORM lookup at
all, so each step reaches them by raw SQL against the ``tenant_id`` column
that adoption added, inside ``admin_bypass()``.

The scoped branch gets its database alias from the router because it goes
through the ORM; the adopted branch asks the router itself, so both reach the
same alias under ``BOUNDARY_REGIONS``.
"""

import json

from django.apps import apps
from django.core.management.base import BaseCommand, CommandError
from django.core.serializers.json import DjangoJSONEncoder
from django.utils.module_loading import import_string

from boundary.conf import boundary_settings, get_tenant_model
from boundary.models import get_tenant_lookup, is_tenant_model


class Command(BaseCommand):
    help = "Remove a tenant and optionally export its data."

    def add_arguments(self, parser):
        parser.add_argument("--tenant", required=True, help="Tenant UUID, PK, or slug")
        parser.add_argument("--export", default=None, help="Export tenant data to NDJSON file")
        parser.add_argument(
            "--batch-size",
            type=int,
            default=1000,
            help="Batch size for export streaming (default: 1000)",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Print what would be deleted without deleting",
        )
        parser.add_argument("--yes", action="store_true", help="Skip confirmation prompt")

    def handle(self, *args, **options):
        tenant = self._resolve_tenant(options["tenant"])

        # Pre-deprovision hook
        hook_path = boundary_settings.PRE_DEPROVISION_HOOK
        if hook_path:
            hook = import_string(hook_path)
            hook(tenant)

        # Collect affected models
        affected = self._collect_affected(tenant)

        if options["dry_run"]:
            self._print_dry_run(tenant, affected)
            return

        adopted_tables = _adopted_table_set()

        if not options["yes"]:
            self.stdout.write(f"About to delete tenant: {tenant}")
            for model, count in affected:
                self.stdout.write(f"  {self._describe(model, adopted_tables)}: {count} rows")
            confirm = input("Type 'yes' to confirm: ")
            if confirm != "yes":
                self.stdout.write("Aborted.")
                return

        # Export, strictly before any delete: BR-PRV-004 makes the export the
        # operator's only copy of what is about to be destroyed.
        if options["export"]:
            self._export(tenant, options["export"], options["batch_size"])

        # Delete (use unscoped to bypass TenantManager). Adopted rows go
        # first, along with every scoped row, so nothing referencing the
        # tenant's primary key survives it: an adopted tenant_id column is
        # not a Django ForeignKey and therefore has no database-level
        # cascade behind it (BR-PRV-009).
        for model, _ in affected:
            if model._meta.db_table in adopted_tables:
                self._delete_adopted_rows(model, tenant)
                continue
            lookup = get_tenant_lookup(model) or "tenant"
            model.unscoped.filter(**{lookup: tenant}).delete()
        tenant.delete()
        self.stdout.write(f"Tenant {tenant.pk} deleted.")

    def _describe(self, model, adopted_tables):
        """Name a model for operator output, marking an adopted table as one.

        A scoped model keeps the bare class name the command has always
        printed. An adopted table is named by ``app_label.ModelName`` and
        marked, because BR-PRV-009 requires an operator to be able to tell
        the two apart: an adopted table's rows are not reachable through the
        ORM at all, so "which of these is adopted" changes what a re-import
        of the export has to do.
        """
        if model._meta.db_table in adopted_tables:
            return f"{model._meta.app_label}.{model.__name__} (adopted table {model._meta.db_table})"
        return model.__name__

    def _resolve_tenant(self, identifier):
        TenantModel = get_tenant_model()
        # Try PK first, then slug
        try:
            return TenantModel.objects.get(pk=identifier)
        except (TenantModel.DoesNotExist, ValueError, TypeError):
            pass
        try:
            return TenantModel.objects.get(slug=identifier)
        except TenantModel.DoesNotExist as exc:
            raise CommandError(f"Tenant not found: {identifier}") from exc

    def _collect_affected(self, tenant):
        """Return ``(model_class, row_count)`` for every table holding the tenant's rows.

        Both categories, in one list, per BR-PRV-009's collect row. A
        column-bearing or path-scoped model is counted through its unscoped
        manager; an adopted table has no manager and no ORM lookup, so it is
        counted with ``SELECT count(*) FROM <table> WHERE tenant_id = %s``
        inside ``admin_bypass()``.

        The bypass is required, not defensive: the adopted table carries
        ``FORCE ROW LEVEL SECURITY`` and this command runs with no tenant
        context of its own, so without it ``boundary_tenant_isolation``
        would match no rows and the count would be zero for every tenant.
        That would read as "nothing to delete" and silently leave every
        adopted row behind. This is one of the two cases BR-RLS-018 names
        ``admin_bypass()`` as the correct remedy for: raw SQL naming
        ``tenant_id`` explicitly, reading across tenants.

        A table with no rows for this tenant is omitted, as it always has
        been, so the dry run and the confirmation prompt list only what the
        deletion will actually touch.
        """
        result = []
        for model in apps.get_models():
            if not is_tenant_model(model) or model._meta.abstract:
                continue
            lookup = get_tenant_lookup(model) or "tenant"
            # Use unscoped to bypass TenantManager strict mode
            count = model.unscoped.filter(**{lookup: tenant}).count()
            if count > 0:
                result.append((model, count))

        for model in _adopted_models():
            count = self._count_adopted_rows(model, tenant)
            if count > 0:
                result.append((model, count))

        return result

    def _count_adopted_rows(self, model, tenant):
        """Return the adopted table's row count for *tenant* (BR-PRV-009).

        On the alias the router writes the model to, so a ``BOUNDARY_REGIONS``
        deployment counts the rows where they actually live; see
        :meth:`_adopted_connection`.
        """
        from boundary.context import admin_bypass

        alias, connection = self._adopted_connection(model, write=True)
        table = connection.ops.quote_name(model._meta.db_table)
        with admin_bypass(using=alias), connection.cursor() as cursor:
            cursor.execute(f"SELECT count(*) FROM {table} WHERE tenant_id = %s", [tenant.pk])
            return cursor.fetchone()[0]

    def _delete_adopted_rows(self, model, tenant):
        """Delete the adopted table's rows for *tenant* (BR-PRV-009).

        By ``tenant_id`` value only. The adopted column is not a Django
        ``ForeignKey`` and has no ``ON DELETE`` behind it, so deleting the
        tenant row would otherwise leave every adopted row pointing at a
        primary key that no longer exists.

        On the router's write alias, matching the count, so a regional
        deployment deletes the rows it reported rather than none.
        """
        from boundary.context import admin_bypass

        alias, connection = self._adopted_connection(model, write=True)
        table = connection.ops.quote_name(model._meta.db_table)
        with admin_bypass(using=alias), connection.cursor() as cursor:
            cursor.execute(f"DELETE FROM {table} WHERE tenant_id = %s", [tenant.pk])

    def _adopted_connection(self, model, *, write):
        """Return ``(alias, connection)`` for an adopted model's raw SQL.

        The scoped branch of every step goes through the ORM, so the router
        routes it; the adopted branch issues raw SQL and has to ask the router
        itself. Hardcoding ``default`` here would make a ``BOUNDARY_REGIONS``
        deployment count zero rows on the wrong database, report nothing to
        delete, and leave every adopted row of the deprovisioned tenant behind
        on the regional alias, isolated by RLS and therefore visible to nobody
        and deleted by nothing.

        The count and the delete both take the WRITE alias, not one each, so
        the number the operator confirms is the number of rows the delete then
        matches. The export takes the read alias, which is where a replica
        deployment wants its bulk read.

        ``using`` is passed on to ``admin_bypass()`` as well, because the
        bypass flag is a session variable on one connection: set on
        ``default`` while the statements run on a regional alias, the flag
        would be inert there and RLS would match no rows at all.

        No ``default`` fallback is needed: Django's ``ConnectionRouter``
        returns ``DEFAULT_DB_ALIAS`` itself when no installed router answers,
        which is every deployment that has configured none.
        """
        from django.db import connections, router

        alias = router.db_for_write(model) if write else router.db_for_read(model)
        return alias, connections[alias]

    def _print_dry_run(self, tenant, affected):
        adopted_tables = _adopted_table_set()
        self.stdout.write(f"[DRY RUN] Would delete tenant: {tenant}")
        for model, count in affected:
            self.stdout.write(f"  {self._describe(model, adopted_tables)}: {count} rows")

    def _export(self, tenant, path, batch_size):
        """Stream tenant data to NDJSON file.

        Scoped models first, then adopted tables (BR-PRV-009), so the file's
        order matches the order the command reports and deletes in.
        """
        with open(path, "w") as f:
            for model in apps.get_models():
                if not is_tenant_model(model) or model._meta.abstract:
                    continue
                lookup = get_tenant_lookup(model) or "tenant"
                qs = model.unscoped.filter(**{lookup: tenant}).iterator(chunk_size=batch_size)
                for obj in qs:
                    row = {
                        "_model": f"{model._meta.app_label}.{model.__name__}",
                        "_pk": str(obj.pk),
                    }
                    for field in model._meta.get_fields():
                        if hasattr(field, "attname"):
                            val = getattr(obj, field.attname, None)
                            row[field.attname] = str(val) if val is not None else None
                    f.write(json.dumps(row) + "\n")

            for model in _adopted_models():
                self._export_adopted_rows(f, model, tenant, batch_size)
        self.stdout.write(f"Exported to {path}")

    def _export_adopted_rows(self, handle, model, tenant, batch_size):
        """Write one NDJSON line per adopted row, streamed server-side (BR-PRV-009).

        ``SELECT *``, so the export carries whatever columns the table
        actually has rather than a column list boundary derived from a model
        it does not have. Each line is keyed by **column name**, not Django
        field name, which is what tells a consumer re-importing the file that
        ``tenant_id`` is not a Django field and must be written back as a raw
        column, and that every other value is in its raw database form rather
        than passed through a field's ``to_python()``. ``_adopted: true``
        makes that distinction machine-readable rather than something the
        consumer must infer from the model name.

        Streamed through a **named** psycopg cursor, which is a PostgreSQL
        server-side cursor: rows are fetched ``batch_size`` at a time rather
        than the whole result set being buffered in the command's memory,
        which is the difference between exporting a large adopted table and
        exhausting the host. ``itersize`` is what psycopg uses for the
        underlying ``FETCH`` size.

        The named cursor is opened on ``connection.connection``, the raw
        psycopg connection under Django's wrapper, because Django's own
        cursor wrapper offers no way to name one. It is opened inside
        ``admin_bypass()``, which also guarantees the transaction a
        server-side cursor requires: a named cursor outside a transaction is
        closed at the end of the first statement and yields nothing.

        Values are serialised with ``DjangoJSONEncoder`` so a ``date``,
        ``datetime``, ``time``, ``timedelta``, ``Decimal`` or ``UUID`` column
        round-trips as the ISO/string form Django itself writes, rather than
        raising ``TypeError`` mid-export and truncating the operator's only
        copy of the data.

        Read on the router's READ alias (see :meth:`_adopted_connection`), so
        a regional deployment exports the rows it is about to delete rather
        than an empty file, and a replica deployment takes the bulk read off
        the primary.
        """
        from boundary.context import admin_bypass

        alias, connection = self._adopted_connection(model, write=False)
        table = connection.ops.quote_name(model._meta.db_table)
        label = f"{model._meta.app_label}.{model.__name__}"
        pk_column = model._meta.pk.column

        with admin_bypass(using=alias):
            # connection.connection is None until Django has actually opened
            # the underlying connection; admin_bypass() has just issued
            # set_config on it, so it is open by here.
            cursor_name = f"boundary_deprovision_{model._meta.db_table}"
            with connection.connection.cursor(name=cursor_name) as cursor:
                cursor.itersize = batch_size
                cursor.execute(f"SELECT * FROM {table} WHERE tenant_id = %s", [tenant.pk])
                columns = [column.name for column in cursor.description]
                for values in cursor:
                    record = dict(zip(columns, values, strict=True))
                    row = {
                        "_model": label,
                        "_pk": str(record.get(pk_column)),
                        "_adopted": True,
                    }
                    row.update(record)
                    handle.write(json.dumps(row, cls=DjangoJSONEncoder) + "\n")


def _adopted_models():
    """Return every model BOUNDARY_TENANT_APPS expects to be adopted.

    Shared with ``boundary.E007`` through
    ``adoption.expected_adopted_models()`` (BR-RLS-010), so the command
    cannot disagree with the check about which tables are adopted: a table
    E007 polices but deprovision skips would leave a deleted tenant's rows
    behind under RLS, visible to nobody and deleted by nothing.

    Empty for every deployment that has adopted nothing, so the adopted
    branch of each step costs one settings read there.
    """
    from django.core.exceptions import ImproperlyConfigured

    from boundary import adoption

    try:
        return adoption.expected_adopted_models()
    except ImproperlyConfigured:
        # No tenant model configured: boundary.E001 reports that, and
        # _resolve_tenant() will already have failed before reaching here.
        return []


def _adopted_table_set():
    """Return the ``db_table`` of every expected-adopted model.

    Used to tell an adopted entry from a scoped one in a ``(model, count)``
    list, which is what lets the dry run mark it and the delete loop route
    it to raw SQL.
    """
    return {model._meta.db_table for model in _adopted_models()}
