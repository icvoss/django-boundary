"""Boundary ORM layer — automatic tenant filtering.

Provides AbstractTenant, TenantMixin, TenantModel, TenantManager,
and TenantQuerySet for row-level multi-tenancy.
"""

import logging

from django.conf import settings
from django.db import models
from django.utils.translation import gettext_lazy as _

from boundary.conf import boundary_settings
from boundary.context import TenantContext
from boundary.exceptions import TenantNotSetError
from boundary.signals import strict_mode_violation

logger = logging.getLogger("boundary.models")


# ── QuerySet ──────────────────────────────────────────────────


class TenantQuerySet(models.QuerySet):
    """Standard queryset subclass for tenant-scoped models.

    No filtering overrides — filtering is in TenantManager.get_queryset().
    Exists as a named class for consuming code to subclass.
    """


# ── Managers ──────────────────────────────────────────────────


class TenantManager(models.Manager):
    """Default manager for TenantMixin models. Auto-filters by active tenant."""

    def get_queryset(self):
        tenant = TenantContext.get()
        qs = TenantQuerySet(self.model, using=self._db)

        if tenant is not None:
            return qs.filter(tenant=tenant)

        if boundary_settings.STRICT_MODE:
            strict_mode_violation.send(sender=self.model, model=self.model, queryset=qs)
            raise TenantNotSetError(
                f"Query on {self.model.__name__} attempted with no active "
                f"tenant. Set a tenant via TenantContext.using() or "
                f"TenantMiddleware."
            )

        return qs

    def bulk_create(self, objs, **kwargs):
        """Auto-populate tenant on objects where tenant_id is None (BR-ORM-007)."""
        tenant = TenantContext.require()
        for obj in objs:
            if obj.tenant_id is None:
                obj.tenant = tenant
        return super().bulk_create(objs, **kwargs)

    def bulk_update(self, objs, fields, **kwargs):
        """Validate all objects belong to the active tenant (BR-ORM-011)."""
        tenant = TenantContext.require()
        for obj in objs:
            if obj.tenant_id != tenant.pk:
                raise ValueError(
                    f"{obj.__class__.__name__} (pk={obj.pk}) belongs to "
                    f"tenant {obj.tenant_id}, not the active tenant "
                    f"{tenant.pk}. Cross-tenant bulk_update is not allowed."
                )
        return super().bulk_update(objs, fields, **kwargs)


class UnscopedManager(models.Manager):
    """Escape hatch — returns all rows regardless of tenant context.

    Sets _boundary_skip_auto_populate on instances to prevent save()
    from auto-populating the tenant field (BR-ORM-006).
    """

    def create(self, **kwargs):
        instance = self.model(**kwargs)
        instance._boundary_skip_auto_populate = True
        instance.save(force_insert=True, using=self.db)
        return instance

    def bulk_create(self, objs, **kwargs):
        for obj in objs:
            obj._boundary_skip_auto_populate = True
        return super().bulk_create(objs, **kwargs)


# ── Abstract Models ───────────────────────────────────────────


class TenantMixin(models.Model):
    """Abstract mixin that adds a tenant FK and wires up TenantManager.

    Applied to any model to make it tenant-scoped.
    """

    tenant = models.ForeignKey(
        settings.BOUNDARY_TENANT_MODEL,
        on_delete=models.CASCADE,
        db_index=True,
        null=False,
        related_name="%(app_label)s_%(class)s_set",
        verbose_name=_("tenant"),
    )

    objects = TenantManager()
    unscoped = UnscopedManager()

    class Meta:
        abstract = True

    def save(self, **kwargs):
        """Auto-populate tenant from context if not set (BR-ORM-004/005/006)."""
        if self.tenant_id is None and not getattr(self, "_boundary_skip_auto_populate", False):
            self.tenant = TenantContext.require()
        super().save(**kwargs)


class TenantModel(TenantMixin):
    """Convenience base combining TenantMixin with models.Model."""

    class Meta:
        abstract = True


class AbstractTenant(models.Model):
    """Convenience base class for tenant models.

    Provides common fields (name, slug, region, is_active, timestamps).
    Integrators who want full control use a plain model pointed to by
    BOUNDARY_TENANT_MODEL instead.
    """

    name = models.CharField(max_length=200)
    slug = models.SlugField(unique=True)
    region = models.CharField(max_length=50, blank=True, default="")
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True
        ordering = ["name"]

    def __str__(self):
        return self.name
