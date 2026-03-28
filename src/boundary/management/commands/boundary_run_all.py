"""Run a management command against every active tenant."""

import json
import multiprocessing

from django.core.management import call_command
from django.core.management.base import BaseCommand

from boundary.conf import boundary_settings, get_tenant_model
from boundary.context import TenantContext


def _run_for_tenant(tenant_pk, command_name, command_args):  # pragma: no cover
    """Execute command for a single tenant (runs in subprocess)."""
    import django

    django.setup()

    TenantModel = get_tenant_model()
    try:
        tenant = TenantModel.objects.get(pk=tenant_pk)
    except TenantModel.DoesNotExist:
        return {"tenant": str(tenant_pk), "status": "error", "error": "not found"}

    try:
        with TenantContext.using(tenant):
            call_command(command_name, *command_args)
        return {"tenant": tenant.slug if hasattr(tenant, "slug") else str(tenant_pk), "status": "ok"}
    except Exception as e:
        return {
            "tenant": tenant.slug if hasattr(tenant, "slug") else str(tenant_pk),
            "status": "error",
            "error": str(e),
        }


class Command(BaseCommand):
    help = "Run a management command against every active tenant."

    def add_arguments(self, parser):
        parser.add_argument("command_name", help="Management command to run")
        parser.add_argument("command_args", nargs="*", help="Arguments for the command")
        parser.add_argument(
            "--parallel",
            type=int,
            default=1,
            help="Number of concurrent workers (default: 1)",
        )
        parser.add_argument("--region", default=None, help="Limit to tenants in this region")
        parser.add_argument(
            "--exclude",
            action="append",
            default=[],
            help="Exclude tenant by PK (can be repeated)",
        )
        parser.add_argument(
            "--json",
            action="store_true",
            dest="json_output",
            help="Output NDJSON for machine parsing",
        )

    def handle(self, *args, **options):
        TenantModel = get_tenant_model()
        qs = TenantModel.objects.filter(is_active=True)

        if options["region"]:
            region_field = boundary_settings.REGION_FIELD
            qs = qs.filter(**{region_field: options["region"]})

        if options["exclude"]:
            qs = qs.exclude(pk__in=options["exclude"])

        tenant_pks = list(qs.values_list("pk", flat=True))

        if not tenant_pks:
            self.stdout.write("No tenants to process.")
            return

        command_name = options["command_name"]
        command_args = options["command_args"]
        parallel = options["parallel"]
        json_output = options["json_output"]

        if parallel > 1:
            results = self._run_parallel(tenant_pks, command_name, command_args, parallel)
        else:
            results = self._run_sequential(tenant_pks, command_name, command_args)

        for result in results:
            if json_output:
                self.stdout.write(json.dumps(result))
            else:
                status = result["status"]
                tenant = result["tenant"]
                if status == "ok":
                    self.stdout.write(f"[OK] {tenant}")
                else:
                    error = result.get("error", "unknown")
                    self.stderr.write(f"[FAIL] {tenant}: {error}")

    def _run_sequential(self, tenant_pks, command_name, command_args):
        TenantModel = get_tenant_model()
        results = []
        for pk in tenant_pks:
            try:
                tenant = TenantModel.objects.get(pk=pk)
            except TenantModel.DoesNotExist:
                results.append({"tenant": str(pk), "status": "error", "error": "not found"})
                continue

            slug = tenant.slug if hasattr(tenant, "slug") else str(pk)
            try:
                with TenantContext.using(tenant):
                    call_command(command_name, *command_args)
                results.append({"tenant": slug, "status": "ok"})
            except Exception as e:
                results.append({"tenant": slug, "status": "error", "error": str(e)})
        return results

    def _run_parallel(self, tenant_pks, command_name, command_args, workers):  # pragma: no cover
        with multiprocessing.Pool(workers) as pool:
            results = pool.starmap(
                _run_for_tenant,
                [(pk, command_name, command_args) for pk in tenant_pks],
            )
        return results
