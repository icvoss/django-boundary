"""Create a new tenant record."""

import json

from django.core.management.base import BaseCommand, CommandError
from django.utils.module_loading import import_string

from boundary.conf import boundary_settings, get_tenant_model


class Command(BaseCommand):
    help = "Create a new tenant record."

    def add_arguments(self, parser):
        parser.add_argument("--name", required=True, help="Tenant name")
        parser.add_argument("--slug", required=True, help="Tenant slug (unique)")
        parser.add_argument("--region", default="", help="Region key")
        parser.add_argument(
            "--extra-fields",
            default="{}",
            help="JSON object of additional field values",
        )

    def handle(self, *args, **options):
        TenantModel = get_tenant_model()

        try:
            extra = json.loads(options["extra_fields"])
        except json.JSONDecodeError as e:
            raise CommandError(f"Invalid JSON in --extra-fields: {e}") from e

        kwargs = {
            "name": options["name"],
            "slug": options["slug"],
        }
        if options["region"]:
            kwargs["region"] = options["region"]
        kwargs.update(extra)

        tenant = TenantModel.objects.create(**kwargs)

        # Post-provision hook
        hook_path = boundary_settings.POST_PROVISION_HOOK
        if hook_path:
            hook = import_string(hook_path)
            hook(tenant)

        self.stdout.write(str(tenant.pk))
