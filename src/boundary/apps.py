"""Boundary Django app configuration."""

from django.apps import AppConfig


class BoundaryConfig(AppConfig):
    name = "boundary"
    label = "boundary"
    verbose_name = "Boundary"
    default_auto_field = "django.db.models.BigAutoField"

    def ready(self):
        self._connect_cache_invalidation_signals()

    def _connect_cache_invalidation_signals(self):
        """Connect post_save/post_delete on the tenant model for cache invalidation."""
        from django.conf import settings
        from django.db.models.signals import post_delete, post_save

        model_string = getattr(settings, "BOUNDARY_TENANT_MODEL", None)
        if not model_string:
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
