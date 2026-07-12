"""Boundary ORM layer — automatic tenant filtering.

Provides AbstractTenant, TenantMixin, TenantModel, TenantManager,
TenantQuerySet, and make_tenant_mixin() factory for row-level
multi-tenancy with configurable FK field names.
"""

import logging
from typing import ClassVar

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
    if getattr(model, "boundary_tenant_path", None):
        return True
    return getattr(model, "_boundary_fk_field", None) is not None


def get_tenant_fk_field(model: type) -> str | None:
    """Return the *local tenant FK column* name for a model, or None.

    This is the writable column used by auto-populate paths. Path-scoped
    models (those declaring ``boundary_tenant_path``) have no local column,
    so this returns None for them — use :func:`get_tenant_lookup` to get the
    ORM lookup used for filtering instead.
    """
    if getattr(model, "boundary_tenant_path", None):
        return None
    fk = getattr(model, "_boundary_fk_field", None)
    if fk is not None:
        return fk
    return None


def get_tenant_lookup(model: type) -> str | None:
    """Return the ORM lookup used to filter *model* by the active tenant.

    Direct-FK models return their FK field name (e.g. ``"merchant"``).
    Path-scoped models return their declared ``boundary_tenant_path``
    (e.g. ``"destination__merchant"``, possibly multi-hop). Returns None
    if the model is not tenant-scoped.
    """
    path = getattr(model, "boundary_tenant_path", None)
    if path:
        return path
    fk = getattr(model, "_boundary_fk_field", None)
    return fk


def has_tenant_column(model: type) -> bool:
    """Return True if *model* owns a writable local tenant FK column.

    False for path-scoped models, which reach the tenant through a relation
    and therefore have no column to auto-populate or stamp. The write paths
    (save, bulk_create, bulk_update, get_or_create injection) gate on this.
    """
    if getattr(model, "boundary_tenant_path", None):
        return False
    return getattr(model, "_boundary_fk_field", None) is not None


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
            lookup = get_tenant_lookup(self.model) or "tenant"
            return qs.filter(**{lookup: tenant})

        if boundary_settings.STRICT_MODE:
            strict_mode_violation.send(sender=self.model, model=self.model, queryset=qs)
            label = boundary_settings.TENANT_LABEL
            raise TenantNotSetError(
                f"Query on {self.model.__name__} attempted with no active "
                f"{label}. Set a {label} via TenantContext.using() or "
                f"TenantMiddleware."
            )

        return qs

    def bulk_create(self, objs, **kwargs):
        """Auto-populate tenant on objects where the FK is None (BR-ORM-007).

        Path-scoped models have no local tenant column to populate, so the
        populate step is skipped for them and they fall straight through to
        Django's bulk_create.
        """
        if not has_tenant_column(self.model):
            return super().bulk_create(objs, **kwargs)
        tenant = TenantContext.require()
        fk_field = getattr(self.model, "_boundary_fk_field", "tenant")
        fk_id_field = f"{fk_field}_id"
        for obj in objs:
            if getattr(obj, fk_id_field) is None:
                setattr(obj, fk_field, tenant)
        return super().bulk_create(objs, **kwargs)

    def bulk_update(self, objs, fields, **kwargs):
        """Validate all objects belong to the active tenant (BR-ORM-011).

        Path-scoped models have no local tenant column to compare, so the
        cross-tenant validation is skipped for them.
        """
        if not has_tenant_column(self.model):
            return super().bulk_update(objs, fields, **kwargs)
        tenant = TenantContext.require()
        fk_field = getattr(self.model, "_boundary_fk_field", "tenant")
        fk_id_field = f"{fk_field}_id"
        label = boundary_settings.TENANT_LABEL
        for obj in objs:
            if getattr(obj, fk_id_field) != tenant.pk:
                raise ValueError(
                    f"{obj.__class__.__name__} (pk={obj.pk}) belongs to "
                    f"{label} {getattr(obj, fk_id_field)}, not the active "
                    f"{label} {tenant.pk}. Cross-{label} bulk_update is not allowed."
                )
        return super().bulk_update(objs, fields, **kwargs)

    def get_or_create(self, defaults=None, **kwargs):
        """Scope the lookup and stamp the tenant on create (BR-ORM-009).

        Injects the active tenant into both the lookup half (so the get
        cannot match another tenant's row) and ``defaults`` (so the create
        stamps the FK), unless the caller supplied it explicitly. No-op for
        path-scoped models, which rely on the auto-filtered queryset.
        """
        self._inject_tenant_kwargs(kwargs, defaults)
        return super().get_or_create(defaults=defaults, **kwargs)

    def update_or_create(self, defaults=None, create_defaults=None, **kwargs):
        """Scope the lookup and stamp the tenant on create (BR-ORM-009).

        Like :meth:`get_or_create`, but also covers ``create_defaults``
        (Django 5.0+). No-op for path-scoped models.
        """
        self._inject_tenant_kwargs(kwargs, defaults, create_defaults)
        return super().update_or_create(defaults=defaults, create_defaults=create_defaults, **kwargs)

    def _inject_tenant_kwargs(self, lookup, *defaults_dicts):
        """Inject the active tenant into lookup + defaults for direct-FK models.

        Never overwrites a value the caller supplied (by either the FK field
        name or its ``_id`` form). Path-scoped models are left untouched —
        they have no column to write and rely on ``get_queryset`` filtering.
        """
        if not has_tenant_column(self.model):
            return
        fk_field = getattr(self.model, "_boundary_fk_field", "tenant")
        fk_id_field = f"{fk_field}_id"
        tenant = TenantContext.require()
        if fk_field not in lookup and fk_id_field not in lookup:
            lookup[fk_field] = tenant
        for d in defaults_dicts:
            if d is not None and fk_field not in d and fk_id_field not in d:
                d[fk_field] = tenant


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
            """Auto-populate tenant from context if not set (BR-ORM-004/005/006).

            Skipped for path-scoped subclasses (boundary_tenant_path), which
            have no local FK column to populate.
            """
            if (
                has_tenant_column(type(self))
                and getattr(self, fk_id_field) is None
                and not getattr(self, "_boundary_skip_auto_populate", False)
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
        verbose_name=_(boundary_settings.TENANT_LABEL),
    )
    fk.contribute_to_class(_TenantMixin, fk_field)

    # Register for discovery by checks/routing
    _tenant_model_registry.add(_TenantMixin)

    # Give the class a useful name for debugging
    _TenantMixin.__name__ = f"TenantMixin[{fk_field}]"
    _TenantMixin.__qualname__ = f"TenantMixin[{fk_field}]"

    return _TenantMixin


def make_tenant_path_mixin(tenant_path: str):
    """Create a TenantMixin for models scoped *through a relation*.

    Use this when a model is tenant-scoped indirectly — it reaches the tenant
    via a foreign key chain rather than owning a tenant FK column itself. The
    manager auto-filters on the given lookup path, and all column-writing
    paths (save, bulk_create, bulk_update, get_or_create injection) are
    correctly skipped because there is no local column to populate.

    Unlike :func:`make_tenant_mixin`, this adds **no ForeignKey** — the model
    is expected to already have the relation the path traverses.

    Returns an abstract model class that provides:
    - ``objects = TenantManager()`` auto-filtering on ``tenant_path``
    - ``unscoped = UnscopedManager()`` (bypass)
    - No tenant column and no save() auto-populate

    Usage::

        from boundary.models import make_tenant_path_mixin

        ExportScopedMixin = make_tenant_path_mixin("destination__merchant")

        class ExportLog(ExportScopedMixin):
            destination = models.ForeignKey(Destination, on_delete=models.CASCADE)
            # ExportLog.objects auto-filters on destination__merchant

    Multi-hop paths work the same way::

        make_tenant_path_mixin("export_log__destination__merchant")

    Note: path-scoped models cannot carry a PostgreSQL RLS policy on a local
    column (they have none). They inherit isolation from the parent on the
    path, which carries the policy, and are skipped by boundary's RLS system
    check and provisioning. Application-layer auto-filtering still applies.

    Args:
        tenant_path: ORM lookup path to the tenant, e.g.
            ``"destination__merchant"`` (single or multi-hop).
    """

    class _TenantPathMixin(models.Model):
        boundary_tenant_path = tenant_path

        objects = TenantManager()
        unscoped = UnscopedManager()

        class Meta:
            abstract = True

    _tenant_model_registry.add(_TenantPathMixin)
    _TenantPathMixin.__name__ = f"TenantPathMixin[{tenant_path}]"
    _TenantPathMixin.__qualname__ = f"TenantPathMixin[{tenant_path}]"

    return _TenantPathMixin


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

    objects: ClassVar[TenantManager] = TenantManager()
    unscoped: ClassVar[UnscopedManager] = UnscopedManager()

    class Meta:
        abstract = True

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        if not cls._meta.abstract:
            _tenant_model_registry.add(cls)

    def save(self, **kwargs):
        """Auto-populate tenant from context if not set (BR-ORM-004/005/006).

        Skipped for path-scoped subclasses (boundary_tenant_path), which have
        no local FK column to populate.
        """
        if (
            has_tenant_column(type(self))
            and self.tenant_id is None
            and not getattr(self, "_boundary_skip_auto_populate", False)
        ):
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
