"""Create a new tenant record."""

import json

from django.core.management.base import BaseCommand, CommandError
from django.utils.module_loading import import_string

from boundary.conf import boundary_settings, get_tenant_model
from boundary.exceptions import RegionNotConfiguredError
from boundary.routing import require_region


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
        region = options["region"]
        if region:
            kwargs["region"] = region
        kwargs.update(extra)

        # When BOUNDARY_REGIONS is configured and a region was given, write
        # the tenant row straight to its regional database (issue #62).
        # RegionalRouter._route() always falls back to "default" here
        # because no tenant is active in TenantContext during provisioning,
        # so a plain objects.create() would silently land the row on
        # "default" regardless of --region. require_region() is the same
        # helper documented for this purpose in
        # docs/how-to/deploy-multi-region.md; reusing it keeps the alias
        # resolution in one place rather than re-deriving it here.
        if region and boundary_settings.REGIONS:
            tenant = TenantModel(**kwargs)
            try:
                alias = require_region(tenant)
            except RegionNotConfiguredError as e:
                configured = sorted(boundary_settings.REGIONS)
                raise CommandError(f"Region {region!r} is not in BOUNDARY_REGIONS ({configured}).") from e
            tenant.save(using=alias)
        else:
            tenant = TenantModel.objects.create(**kwargs)

        # Post-provision hook
        hook_path = boundary_settings.POST_PROVISION_HOOK
        if hook_path:
            hook = import_string(hook_path)
            hook(tenant)

        self.stdout.write(str(tenant.pk))
