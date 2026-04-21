"""Boundary ORM layer — automatic tenant filtering.

Provides AbstractTenant, TenantMixin, TenantModel, TenantManager,
TenantQuerySet, and make_tenant_mixin() factory for row-level
multi-tenancy with configurable FK field names.
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


# ── Registry ─────────────────────────────────────────────────

# Models created via TenantMixin or make_tenant_mixin() register here
# so that checks, routing, and other internals can recognise them
# without requiring a strict issubclass(model, TenantMixin) check.
_tenant_model_registry: set[type] = set()


def is_tenant_model(model: type) -> bool:
    """Return True if *model* is a registered tenant-scoped model.

    Checks the registry (populated by make_tenant_mixin and TenantMixin)
    and also performs a duck-type check for models that have the expected
    ``_boundary_fk_field`` attribute.
    """
    if model in _tenant_model_registry:
        return True
    return getattr(model, "_boundary_fk_field", None) is not None


def get_tenant_fk_field(model: type) -> str | None:
    """Return the tenant FK field name for a tenant-scoped model, or None."""
    fk = getattr(model, "_boundary_fk_field", None)
    if fk is not None:
        return fk
    return None


# ── QuerySet ──────────────────────────────────────────────────


class TenantQuerySet(models.QuerySet):
    """Standard queryset subclass for tenant-scoped models.

    No filtering overrides — filtering is in TenantManager.get_queryset().
    Exists as a named class for consuming code to subclass.
    """


# ── Managers ──────────────────────────────────────────────────


class TenantManager(models.Manager):
    """Default manager for tenant-scoped models. Auto-filters by active tenant.

    The FK field name is read from the model's ``_boundary_fk_field``
    attribute (set by TenantMixin or make_tenant_mixin).
    """

    def get_queryset(self):
        tenant = TenantContext.get()
        qs = TenantQuerySet(self.model, using=self._db)

        if tenant is not None:
            fk_field = getattr(self.model, "_boundary_fk_field", "tenant")
            return qs.filter(**{fk_field: tenant})

        if boundary_settings.STRICT_MODE:
            strict_mode_violation.send(sender=self.model, model=self.model, queryset=qs)
            raise TenantNotSetError(
                f"Query on {self.model.__name__} attempted with no active "
                f"tenant. Set a tenant via TenantContext.using() or "
                f"TenantMiddleware."
            )

        return qs

    def bulk_create(self, objs, **kwargs):
        """Auto-populate tenant on objects where the FK is None (BR-ORM-007)."""
        tenant = TenantContext.require()
        fk_field = getattr(self.model, "_boundary_fk_field", "tenant")
        fk_id_field = f"{fk_field}_id"
        for obj in objs:
            if getattr(obj, fk_id_field) is None:
                setattr(obj, fk_field, tenant)
        return super().bulk_create(objs, **kwargs)

    def bulk_update(self, objs, fields, **kwargs):
        """Validate all objects belong to the active tenant (BR-ORM-011)."""
        tenant = TenantContext.require()
        fk_field = getattr(self.model, "_boundary_fk_field", "tenant")
        fk_id_field = f"{fk_field}_id"
        for obj in objs:
            if getattr(obj, fk_id_field) != tenant.pk:
                raise ValueError(
                    f"{obj.__class__.__name__} (pk={obj.pk}) belongs to "
                    f"tenant {getattr(obj, fk_id_field)}, not the active tenant "
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


# ── Factory ──────────────────────────────────────────────────


def make_tenant_mixin(
    fk_field: str | None = None,
    *,
    on_delete=models.CASCADE,
    related_name: str = "%(app_label)s_%(class)s_set",
    db_index: bool = True,
    null: bool = False,
):
    """Create a TenantMixin with a custom FK field name.

    Returns an abstract model class that provides:
    - A ForeignKey to ``BOUNDARY_TENANT_MODEL`` with the given field name
    - ``objects = TenantManager()`` (auto-filtering)
    - ``unscoped = UnscopedManager()`` (bypass)
    - Auto-populate on ``save()``

    Usage::

        # In your models.py
        from boundary.models import make_tenant_mixin

        MerchantMixin = make_tenant_mixin("merchant")

        class Product(MerchantMixin):
            name = models.CharField(max_length=200)
            # Product.merchant is the FK, Product.objects auto-filters by it

    Args:
        fk_field: Name for the ForeignKey field. Defaults to
            ``BOUNDARY_TENANT_FK_FIELD`` setting (which defaults to ``"tenant"``).
        on_delete: Django on_delete behaviour. Default CASCADE.
        related_name: Related name pattern. Default ``"%(app_label)s_%(class)s_set"``.
        db_index: Whether to index the FK column. Default True.
        null: Whether the FK is nullable. Default False.
    """
    if fk_field is None:
        fk_field = boundary_settings.TENANT_FK_FIELD

    fk_id_field = f"{fk_field}_id"

    class _TenantMixin(models.Model):
        _boundary_fk_field = fk_field

        objects = TenantManager()
        unscoped = UnscopedManager()

        class Meta:
            abstract = True

        def save(self, **kwargs):
            """Auto-populate tenant from context if not set (BR-ORM-004/005/006)."""
            if getattr(self, fk_id_field) is None and not getattr(
                self, "_boundary_skip_auto_populate", False
            ):
                setattr(self, fk_field, TenantContext.require())
            super().save(**kwargs)

    # Add the FK field dynamically
    fk = models.ForeignKey(
        settings.BOUNDARY_TENANT_MODEL,
        on_delete=on_delete,
        db_index=db_index,
        null=null,
        related_name=related_name,
        verbose_name=_(fk_field),
    )
    fk.contribute_to_class(_TenantMixin, fk_field)

    # Register for discovery by checks/routing
    _tenant_model_registry.add(_TenantMixin)

    # Give the class a useful name for debugging
    _TenantMixin.__name__ = f"TenantMixin[{fk_field}]"
    _TenantMixin.__qualname__ = f"TenantMixin[{fk_field}]"

    return _TenantMixin


# ── Built-in Mixins ──────────────────────────────────────────


class TenantMixin(models.Model):
    """Abstract mixin that adds a tenant FK and wires up TenantManager.

    Applied to any model to make it tenant-scoped. Uses the default
    field name ``"tenant"``. For a custom field name, use
    :func:`make_tenant_mixin`.
    """

    _boundary_fk_field = "tenant"

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

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        if not cls._meta.abstract:
            _tenant_model_registry.add(cls)

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
