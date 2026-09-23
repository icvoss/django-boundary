"""Boundary ORM layer — automatic tenant filtering.

Provides AbstractTenant, TenantMixin, TenantModel, TenantManager,
TenantQuerySet, and make_tenant_mixin() factory for row-level
multi-tenancy with configurable FK field names.
"""

import logging
from typing import ClassVar, Protocol, cast

from django.core.exceptions import ValidationError
from django.db import models
from django.utils.translation import gettext_lazy as _

from boundary.conf import boundary_settings, resolve_tenant_model_setting
from boundary.context import TenantContext
from boundary.exceptions import TenantNotSetError
from boundary.signals import strict_mode_violation

logger = logging.getLogger("boundary.models")


# ── Registry ─────────────────────────────────────────────────

# Models created via TenantMixin or make_tenant_mixin() register here
# so that checks, routing, and other internals can recognise them
# without requiring a strict issubclass(model, TenantMixin) check.
_tenant_model_registry: set[type] = set()


class _HasUnscopedManager(Protocol):
    """The one attribute a registered tenant model is read for structurally.

    ``is_tenant_model()`` is a registry-plus-duck-type check rather than an
    ``issubclass()`` test, deliberately (see the registry note above), so it
    cannot be a ``TypeGuard`` onto a single nominal base: a model built by
    ``make_tenant_mixin()`` gets its managers from a locally defined abstract
    class that shares no base with ``TenantMixin``. Both paths do attach
    ``unscoped`` as a real class attribute, which is what this records, so a
    caller that has already passed ``is_tenant_model()`` can say what it
    expects rather than silencing the resulting attribute error.
    """

    # Quoted: UnscopedManager is defined further down this module, and this
    # module has no ``from __future__ import annotations``, so an unquoted
    # forward reference would raise NameError at import time.
    unscoped: ClassVar["UnscopedManager"]


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


def validate_cross_tenant_fks(instance) -> None:
    """Raise ValidationError if *instance* holds a cross-tenant FK reference (BR-ORM-013).

    Neither isolation layer catches this (issue #39). The ORM layer's
    ``TenantManager.get_queryset()`` filters on the ROW's OWN tenant column,
    so a row correctly stamped for tenant A that points a plain FK at a row
    belonging to tenant B is returned by every query, because its own
    ``tenant_id`` is A. RLS enforces the same thing at the database: the
    ``USING``/``WITH CHECK`` policy on the child table checks the child
    row's own ``tenant_id`` column, not what its FK columns point at, and
    PostgreSQL's referential-integrity check on the FK target runs as the
    table owner, outside the parent table's policy. Both layers correctly
    consider the row isolated; neither notices what it references.

    This is called from ``TenantMixin.clean()`` and the ``clean()`` of the
    mixin ``make_tenant_mixin()`` produces, so both entry points share one
    implementation rather than duplicating the field-walking logic.

    Scope, deliberately:

    - **Only FKs to other tenant-scoped models with their own tenant
      column** are checked (``is_tenant_model`` and ``has_tenant_column``
      both true for the target model). A FK to a non-tenant model (
      ``auth.User``, a lookup table) has no tenant to compare against and
      is skipped without attempting to read one. A FK to a *path-scoped*
      model (``make_tenant_path_mixin``, no local tenant column) is also
      skipped: validating it would mean traversing that target's own
      tenant path, which is a different, unbounded-hop check, not a cheap
      local comparison, and path-scoped models are already documented as
      an application-layer-only isolation contract. This is a known limit
      of this check, not an oversight; see
      docs/explanation/isolation-layers.md.
    - **A None/unset FK is skipped.** There is nothing to validate, and it
      is the caller's job (or a `null=False` field) to require a value.
    - **An unsaved target (no pk yet) is skipped.** Its ``<field>_id`` is
      None, so it falls out of the None check above; there is no row to
      compare against yet, and full_clean() on a form is not the place to
      demand the related object be saved first.
    - **Compared against the instance's OWN tenant FK value, not
      ``TenantContext.get()``.** An admin cross-tenant view, a data import,
      or a management command may legitimately construct or edit a row
      under one tenant's context while intentionally operating on another
      tenant's row (see docs/how-to/cross-tenant-admin-operations.md).
      Comparing against the ambient context would raise for those
      legitimate paths and would also raise TenantNotSetError-shaped
      confusion when no context is active at all. Comparing against the
      instance's own value asks the only question that matters here: do
      this row's own tenant and its FK target's tenant agree.
    - **One query per checked FK field, not zero and not N+1 per row
      collection.** Reading ``instance.some_fk`` would fetch and cache the
      whole related object; this reads only the target's tenant id via
      ``values_list(..., flat=True)`` scoped to the target's pk, so a
      model with three tenant-scoped FKs issues up to three small
      single-column lookups on ``clean()``, never a full object fetch and
      never more than one query per field regardless of how many rows are
      validated elsewhere. Callers validating many instances (a bulk
      import loop) pay this cost per instance; that is the acknowledged
      price of a correctness check that only fires on ``full_clean()``
      paths in the first place (see the CHANGELOG and
      docs/explanation/isolation-layers.md for the residual gap on
      ``save()``, ``bulk_create``, ``bulk_update``, ``update()``, and raw
      SQL, none of which call ``clean()``).
    - **Self-referential FKs work the same way.** A tenant-scoped model
      with an FK to its own model is just another FK whose target model
      happens to equal ``type(instance)``; no special case is needed.

    Raises:
        ValidationError: keyed by the FK field name, when a cross-tenant
            reference is found. Django's ``full_clean()`` collects one or
            more of these into its usual ``{field: [messages]}`` shape.
    """
    own_fk_field = get_tenant_fk_field(type(instance))
    if own_fk_field is None:
        # Path-scoped instance: no local tenant column of its own to
        # compare against, so there is nothing this check can do here.
        return
    own_tenant_id = getattr(instance, f"{own_fk_field}_id", None)
    if own_tenant_id is None:
        # Nothing to compare against yet (e.g. auto-populate has not run).
        return

    errors: dict[str, list[str]] = {}
    for field in instance._meta.get_fields():
        if not (getattr(field, "many_to_one", False) and isinstance(field, models.ForeignKey)):
            continue
        if field.name == own_fk_field:
            continue  # the model's own tenant FK, not a cross-reference

        target_model = field.related_model
        if not isinstance(target_model, type):
            # django-stubs types ``related_model`` as ``type[Model] |
            # Literal["self"]``, because a self-referential FK declared with
            # the string ``"self"`` carries that sentinel until Django
            # resolves the relation. By the time an instance exists to
            # validate, every relation on its concrete model is resolved and
            # this is always a model class; a self-referential FK arrives
            # here as its own model class, which the docstring above already
            # records as needing no special case. Narrowed rather than cast
            # because an unresolved sentinel reaching a validation path would
            # be a real defect to skip, not a type to assert away.
            continue
        if not (is_tenant_model(target_model) and has_tenant_column(target_model)):
            continue

        target_id = getattr(instance, field.attname, None)
        if target_id is None:
            continue

        target_fk_field = get_tenant_fk_field(target_model)
        # is_tenant_model() above established this model carries boundary's
        # managers, but it is a registry/duck-type check rather than an
        # issubclass() test (see _HasUnscopedManager), so the nominal type
        # here is still a bare type[Model].
        target_manager = cast(_HasUnscopedManager, target_model).unscoped
        target_tenant_id = target_manager.filter(pk=target_id).values_list(f"{target_fk_field}_id", flat=True).first()
        if target_tenant_id is None:
            # Target row does not exist (dangling FK). Not this check's
            # concern; Django's own FK validation covers existence.
            continue

        if target_tenant_id != own_tenant_id:
            label = boundary_settings.TENANT_LABEL
            errors[field.name] = [
                f"{field.name} belongs to a different {label} "
                f"({target_tenant_id}) than this {type(instance).__name__} "
                f"({own_tenant_id})."
            ]

    if errors:
        raise ValidationError(errors)


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

    Set explicitly rather than relying on Django's own default (plain
    ``QuerySet``), because ``get_queryset()`` below is overridden and must
    construct via ``self._queryset_class`` to honour
    ``TenantManager.from_queryset(CustomQuerySet)`` (issue #29). Without
    this, a manager built with ``from_queryset()`` would generate methods
    that call ``self.get_queryset()`` then the custom method on the
    result, and since the overridden ``get_queryset()`` ignored
    ``_queryset_class``, the custom method would not be on the returned
    object and the generated method would raise ``AttributeError``.
    """

    _queryset_class = TenantQuerySet

    def get_queryset(self):
        tenant = TenantContext.get()
        qs = self._queryset_class(self.model, using=self._db)

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

        def clean(self):
            """Reject a cross-tenant FK reference (BR-ORM-013, issue #39).

            See :func:`validate_cross_tenant_fks` for the full rationale
            and scope. Only fires on ``full_clean()`` paths (ModelForm
            validation, an explicit call); see
            docs/explanation/isolation-layers.md for what this does and
            does not cover.
            """
            super().clean()
            validate_cross_tenant_fks(self)

    # Add the FK field dynamically. Resolved via resolve_tenant_model_setting()
    # (BOUNDARY_TENANT_MODEL first, falling back to ICV_TENANT_MODEL per
    # ADR-025 T2, issue #15) rather than settings.BOUNDARY_TENANT_MODEL
    # directly, so a project configured with only ICV_TENANT_MODEL can
    # import this module at all. Whichever setting supplies the value is
    # structural: it is baked into this FK (and every consumer's migrations)
    # at import time. An explicit BOUNDARY_TENANT_MODEL always takes
    # precedence, and changing either setting afterwards requires new
    # migrations, exactly as it always has for BOUNDARY_TENANT_MODEL users.
    # Annotated because the target is a settings STRING resolved at import
    # (``"app.Model"``), not a class, so django-stubs has no concrete model to
    # infer the generic parameter from. ``models.Model`` is the honest
    # parameter here: which model this points at is a consumer's choice, made
    # by BOUNDARY_TENANT_MODEL, and is unknowable to this package statically.
    fk: models.ForeignKey[models.Model] = models.ForeignKey(
        resolve_tenant_model_setting(),
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

    Note (issue #14): relation-scoped isolation is an APPLICATION-LAYER
    feature only, by intentional contract. The child table carries NO RLS
    policy, and none is created for it (this model is skipped by boundary's
    RLS system check and provisioning). PostgreSQL RLS is table-specific: the
    parent's policy (e.g. ``Destination``'s policy on its ``merchant_id``)
    constrains scans of the PARENT table, so an ORM query that joins through
    the path is constrained too, but it does NOT constrain direct SQL,
    unscoped managers, or third-party database access run directly against
    THIS table. Rows that need database-level isolation must own a tenant FK
    of their own and use :class:`TenantMixin` or :func:`make_tenant_mixin`
    instead. See docs/how-to/scope-models-through-a-relation.md for the full
    contract.

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

    # Resolved via resolve_tenant_model_setting() rather than
    # settings.BOUNDARY_TENANT_MODEL directly, so a project configured with
    # only ICV_TENANT_MODEL (ADR-025 T2) can import this module (issue #15).
    # The setting is structural: it is baked into this FK (and consumers'
    # migrations) at import time. An explicit BOUNDARY_TENANT_MODEL always
    # takes precedence, and changing either setting later requires new
    # migrations, exactly as before.
    tenant = models.ForeignKey(
        resolve_tenant_model_setting(),
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

    def clean(self):
        """Reject a cross-tenant FK reference (BR-ORM-013, issue #39).

        See :func:`validate_cross_tenant_fks` for the full rationale and
        scope. Only fires on ``full_clean()`` paths (ModelForm validation,
        an explicit call); see docs/explanation/isolation-layers.md for
        what this does and does not cover.
        """
        super().clean()
        validate_cross_tenant_fks(self)


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


# -- Per-tenant uniqueness (BR-ORM-015) ----------------------


class TenantUniqueConstraint(models.UniqueConstraint):
    """A ``UniqueConstraint`` whose field list gains the model's tenant FK.

    Do not instantiate this directly; call :func:`tenant_unique`. The class is
    public only because a migration's ``deconstruct()`` path has to be able to
    import it by name.

    **Why the tenant field is resolved at class preparation, and how
    (BR-ORM-015).** ``tenant_unique("reference")`` is evaluated while the
    ``Meta`` class body is being built, so there is no model to ask: the
    helper cannot read ``_boundary_fk_field`` at that moment. Django offers no
    per-constraint ``contribute_to_class`` hook either, so the resolution has
    to happen at the one point where both the constraint and the prepared
    model are in hand. That point is the ``class_prepared`` signal, which
    ``ModelBase.__new__`` sends at the end of model construction, after
    ``Options.contribute_to_class`` has populated ``_meta.constraints`` and
    after ``_boundary_fk_field`` has been inherited from the mixin.

    A ``class_prepared`` receiver was chosen over reading
    ``_boundary_fk_field`` inside a ``contribute_to_class`` on the constraint
    because Django does not call one: ``Options.contribute_to_class`` treats
    ``Meta.constraints`` as a plain list, and the only thing it does to each
    member is ``clone()`` it for ``%(app_label)s``/``%(class)s`` name
    interpolation. That clone matters and is the reason ``deconstruct()``
    below carries ``tenant_fields``: ``BaseConstraint.clone()`` round-trips
    through ``deconstruct()``, so the object that ends up in
    ``_meta.constraints`` is a fresh instance rebuilt from those kwargs, not
    the one the model author wrote. A subclass whose extra state is not in
    ``deconstruct()`` would silently lose it on the way in.

    The resolved field list is written onto the prepared model's own
    constraint instance, so ``makemigrations`` serialises the resolved form
    (this class with its ``fields`` already leading with the tenant column)
    and the migration needs nothing from boundary at apply time beyond the
    import.

    **A migration state render prepares a model that has no tenant FK
    field.** ``ModelState.render()`` rebuilds the model with
    ``type(name, bases, body)`` on ``models.Model``, not on the tenant mixin,
    so ``_boundary_fk_field`` is absent from the rendered class while the
    constraint it carries is already resolved. That is why ``_resolve()``
    checks for a resolved constraint before it checks for the FK field; see
    its docstring.
    """

    def __init__(self, *, tenant_fields, **kwargs):
        # ``fields`` is a placeholder until the receiver resolves it:
        # UniqueConstraint refuses to be constructed with an empty field list,
        # so the author's fields stand in until the tenant column can be
        # prepended. ``tenant_fields`` is the author's list, kept separately
        # so resolution is idempotent and cannot prepend twice.
        self.tenant_fields = tuple(tenant_fields)
        kwargs.setdefault("fields", self.tenant_fields)
        super().__init__(**kwargs)

    def deconstruct(self):
        path, args, kwargs = super().deconstruct()
        kwargs["tenant_fields"] = self.tenant_fields
        return path, args, kwargs

    def _resolve(self, model):
        """Prepend *model*'s tenant FK field, or raise if it has no column.

        **The already-resolved check runs before the raise, and must.** This
        receiver fires for every ``class_prepared``, and a migration state
        render is one of them: ``ModelState.render()`` rebuilds the model with
        ``type(name, bases, body)`` on ``models.Model``, so the rendered class
        carries the constraint but NOT ``_boundary_fk_field``, which lives on
        the mixin the rendered class does not inherit. The constraint arriving
        in state is nonetheless already resolved, having round-tripped through
        ``deconstruct()`` with both ``fields=("tenant", "slug")`` and
        ``tenant_fields=("slug",)`` written into the migration. Raising for a
        missing FK field before asking whether there is anything left to do
        made every state render fail, which is every ``migrate`` and every
        ``makemigrations``: the test suite could not create its database at
        all (BR-ORM-015).

        Resolution is therefore detected by comparing the two field lists
        rather than by a flag carried through ``deconstruct()``. A flag would
        have to pick a default for the migrations consumers have already
        written, which carry none: defaulting it to unresolved re-raises here
        on exactly the rendered model that has no FK field, and defaulting it
        to resolved leaves a genuinely fresh constraint unresolvable. The
        comparison needs no default and no new serialised kwarg.
        """
        if self.fields != self.tenant_fields:
            # Already resolved: the tenant column has been prepended, so the
            # lists differ. A state-rendered model reaches here (and has no
            # ``_boundary_fk_field`` to offer), as does a re-prepared model or
            # a clone of one.
            return
        fk_field = getattr(model, "_boundary_fk_field", None)
        if not fk_field:
            raise ValueError(
                f"tenant_unique() cannot be used on {model._meta.label}: it has no "
                f"local tenant column. Per-tenant uniqueness needs a tenant column "
                f"to lead the constraint with, so a path-scoped model "
                f"(make_tenant_path_mixin) cannot express it. Use TenantMixin or "
                f"make_tenant_mixin() if this model should own a tenant FK, or a "
                f"plain UniqueConstraint if the uniqueness is genuinely global."
            )
        self.fields = (fk_field, *self.tenant_fields)


def tenant_unique(*fields, name=None):
    """Return a :class:`~django.db.models.UniqueConstraint` scoped to a tenant.

    A field declared ``unique=True`` on a tenant-scoped model is unique across
    every tenant, not within one: two tenants cannot both hold an invoice with
    reference ``INV-001``, and the second one to try gets an ``IntegrityError``
    naming a constraint that mentions neither tenancy nor the other tenant.
    The model author almost always meant per-tenant uniqueness. This helper
    expresses it (BR-ORM-015)::

        class Invoice(TenantModel):
            reference = models.CharField(max_length=32)

            class Meta:
                constraints = [tenant_unique("reference")]

    The resulting constraint's fields are the model's tenant FK field followed
    by *fields* in the order given, so the same call works unchanged on a model
    built from ``make_tenant_mixin("merchant")``, where it resolves to
    ``("merchant", "reference")``. The tenant field is resolved when Django
    prepares the model, not when this function runs; see
    :class:`TenantUniqueConstraint` for the mechanism and why it is the only
    one Django supports cleanly.

    Applied to a model with no local tenant column (``make_tenant_path_mixin``),
    preparation raises naming that model, rather than producing a constraint on
    a field that does not exist.

    Args:
        *fields: The model's own field names, in the order they should follow
            the tenant column.
        name: The constraint name. Defaults to a deterministic name derived
            from the field list and interpolated with the model's app label and
            class name, so two ``tenant_unique()`` calls on one model with
            different field lists cannot collide. An explicitly passed name is
            used verbatim.
    """
    if not fields:
        raise ValueError("tenant_unique() requires at least one field name.")
    if name is None:
        name = _derive_constraint_name(fields)
    return TenantUniqueConstraint(tenant_fields=fields, name=name)


def _derive_constraint_name(fields) -> str:
    """Derive a deterministic constraint name from *fields* (BR-ORM-015).

    The name carries Django's own ``%(app_label)s``/``%(class)s``
    placeholders, which ``Options.contribute_to_class`` interpolates when the
    model is prepared, so the model half of the name comes from Django's
    existing mechanism rather than being guessed here: at the point this runs
    there is no model to read it from.

    The field half is a digest rather than the joined field names, computed
    through the same ``names_digest`` helper Django's
    ``BaseDatabaseSchemaEditor._create_index_name`` uses (the naming path
    BR-RLS-012 also routes through). A digest is what keeps the result inside
    PostgreSQL's 63-character identifier limit for any field list at all,
    which joined names do not: the model half's length is not known until
    interpolation, so a length-dependent scheme could not be resolved here.
    """
    # django-stubs does not declare names_digest, though Django has exported
    # it from this module since 3.0 and still does on every version in
    # BR-ENV-004's supported set (verified against the installed 5.2). A
    # public name the stubs omit, not a private API: BR-RLS-022's
    # schema_compat.py rule governs underscore-prefixed schema-editor
    # attributes and does not reach this one.
    from django.db.backends.utils import names_digest  # type: ignore[attr-defined]

    digest = names_digest(*fields, length=8)
    return f"%(app_label)s_%(class)s_tenant_uniq_{digest}"


def _resolve_tenant_unique_constraints(sender, **kwargs):
    """``class_prepared`` receiver: resolve every TenantUniqueConstraint.

    Connected at import time of this module rather than in ``AppConfig.ready``
    because a consumer model carrying ``tenant_unique()`` must import this
    module to get the helper, which guarantees this receiver is connected
    before that model class is built. ``ready()`` runs after the app registry
    has prepared every model, which would be too late.
    """
    for constraint in getattr(sender._meta, "constraints", ()):
        if isinstance(constraint, TenantUniqueConstraint):
            constraint._resolve(sender)


models.signals.class_prepared.connect(_resolve_tenant_unique_constraints)
