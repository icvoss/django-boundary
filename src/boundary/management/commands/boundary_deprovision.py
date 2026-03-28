"""Remove a tenant and optionally export its data."""

import json

from django.apps import apps
from django.core.management.base import BaseCommand, CommandError
from django.utils.module_loading import import_string

from boundary.conf import boundary_settings, get_tenant_model
from boundary.models import TenantMixin


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

        if not options["yes"]:
            self.stdout.write(f"About to delete tenant: {tenant}")
            for model, count in affected:
                self.stdout.write(f"  {model.__name__}: {count} rows")
            confirm = input("Type 'yes' to confirm: ")
            if confirm != "yes":
                self.stdout.write("Aborted.")
                return

        # Export
        if options["export"]:
            self._export(tenant, options["export"], options["batch_size"])

        # Delete (use unscoped to bypass TenantManager)
        for model, _ in affected:
            model.unscoped.filter(tenant=tenant).delete()
        tenant.delete()
        self.stdout.write(f"Tenant {tenant.pk} deleted.")

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
        """Return list of (model_class, row_count) for tenant-scoped models."""
        result = []
        for model in apps.get_models():
            if not issubclass(model, TenantMixin) or model._meta.abstract:
                continue
            # Use unscoped to bypass TenantManager strict mode
            count = model.unscoped.filter(tenant=tenant).count()
            if count > 0:
                result.append((model, count))
        return result

    def _print_dry_run(self, tenant, affected):
        self.stdout.write(f"[DRY RUN] Would delete tenant: {tenant}")
        for model, count in affected:
            self.stdout.write(f"  {model.__name__}: {count} rows")

    def _export(self, tenant, path, batch_size):
        """Stream tenant data to NDJSON file."""
        with open(path, "w") as f:
            for model in apps.get_models():
                if not issubclass(model, TenantMixin) or model._meta.abstract:
                    continue
                qs = model.unscoped.filter(tenant=tenant).iterator(chunk_size=batch_size)
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
        self.stdout.write(f"Exported to {path}")
