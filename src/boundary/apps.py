"""Boundary Django app configuration."""

from django.apps import AppConfig


class BoundaryConfig(AppConfig):
    name = "boundary"
    label = "boundary"
    verbose_name = "Boundary"
    default_auto_field = "django.db.models.BigAutoField"

    def ready(self):
        # boundary.checks registers every boundary.* system check via the
        # @register decorator at import time. Nothing else in the package
        # imports that module, so without this import the whole check suite
        # (E001, E003, E004, E005, E006, W001, W002, W003) never registers
        # and `manage.py check`/`migrate`/runserver silently run none of
        # them; they had only ever appeared to work because
        # tests/test_checks.py imports the module directly.
        import boundary.checks  # noqa: F401

        self._connect_cache_invalidation_signals()

    def _connect_cache_invalidation_signals(self):
        """Connect post_save/post_delete on the tenant model for cache invalidation."""
        from django.core.exceptions import ImproperlyConfigured
        from django.db.models.signals import post_delete, post_save

        from boundary.conf import resolve_tenant_model_setting

        # Every other resolution site in the package falls back from
        # BOUNDARY_TENANT_MODEL to ICV_TENANT_MODEL per ADR-025 T2
        # (conf.resolve_tenant_model_setting, conf.get_tenant_model,
        # checks._check_tenant_model). This site read BOUNDARY_TENANT_MODEL
        # directly and returned early when unset, so a project configured
        # with only ICV_TENANT_MODEL, the supported configuration that
        # checks.E001 explicitly passes, connected zero cache-invalidation
        # signals, with no error and no warning (issue #33). Calling the
        # shared resolver here keeps this site consistent with every other
        # one. ImproperlyConfigured is caught to preserve the pre-existing
        # no-op-when-unset behaviour for a project that has not configured
        # boundary at all yet: ready() must not explode at startup for a
        # condition that boundary.E001 already reports properly.
        try:
            model_string = resolve_tenant_model_setting()
        except ImproperlyConfigured:
            return

        def _invalidate_on_save(sender, instance, **kwargs):
            from boundary.resolvers import _cache_invalidate

            _cache_invalidate(instance)

        def _invalidate_on_delete(sender, instance, **kwargs):
            from boundary.resolvers import _cache_invalidate

            _cache_invalidate(instance)

        # String sender — Django resolves lazily
        post_save.connect(_invalidate_on_save, sender=model_string, weak=False)
        post_delete.connect(_invalidate_on_delete, sender=model_string, weak=False)
