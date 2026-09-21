"""Derivation and introspection helpers for third-party app adoption.

Adoption is the third way a model becomes tenant-scoped (BR-RLS-010): rather
than declaring a tenant field, the consumer lists a third-party app in
``BOUNDARY_TENANT_APPS`` and applies :class:`boundary.migrations_ops.AdoptTenantApp`
from a migration in their own app. The adopted tables then carry a
``tenant_id`` column that no Django field models, plus PostgreSQL Row Level
Security, and the adopted package ships nothing, knows nothing and is never
imported by boundary.

This module holds the parts of that work that are neither DDL emission nor
system checks: which models an app's adoption covers, which apps and models
are refused outright, and the PostgreSQL catalogue queries that tell the
operation what unique constraints and indexes it is rewriting.

It is importable without a database. Every function that needs one takes a
cursor, so the derivation and validation halves can be unit-tested and reused
by ``boundary.E007`` (BR-RLS-017) without opening a connection.
"""

from __future__ import annotations

from boundary.conf import boundary_settings, resolve_tenant_model_setting

# ── The deny-list (BR-RLS-016) ───────────────────────────────

#: Apps that are global by construction and are refused outright. Scoping
#: contenttypes breaks Django's own bootstrap; scoping sessions or sites
#: breaks request handling before a tenant is ever resolved.
DENIED_APPS = frozenset({"contenttypes", "sessions", "sites"})

#: Individual models that are global by construction even though their app
#: is adoptable. ``auth`` as a whole is NOT denied (a consumer may adopt it,
#: accepting that createsuperuser and create_permissions then need a tenant
#: context, BR-RLS-018), but Permission within it is, and so is admin's
#: LogEntry.
DENIED_MODELS = frozenset({"auth.Permission", "admin.LogEntry"})

#: Django's own migration bookkeeping table. It is named by table rather than
#: by model label because a consumer cannot reach it through an app label at
#: all; it is listed so the deny-list is complete and auditable against
#: BR-RLS-016 rather than leaving a rule with no code behind it.
DENIED_TABLES = frozenset({"django_migrations"})


def model_label(model) -> str:
    """Return the ``"app_label.ModelName"`` identity used by every exclusion.

    This is the exact string ``BOUNDARY_ADOPT_EXCLUDE`` and the operation's
    own ``exclude`` are matched against, case-sensitively, and the string
    :data:`DENIED_MODELS` holds. Auto-created many-to-many through models get
    the generated class name Django gave them (for example
    ``"thirdparty.Widget_tags"``), which is what makes excluding one of them
    possible at all.
    """
    return f"{model._meta.app_label}.{model.__name__}"


def tenant_model_label() -> str:
    """Return the configured tenant model as ``"app_label.ModelName"``.

    Read from ``BOUNDARY_TENANT_MODEL``, falling back to ``ICV_TENANT_MODEL``
    (ADR-025 T2). Raises ``ImproperlyConfigured`` naming both settings when
    neither is set, before any DDL is issued.

    The setting's value is normalised rather than returned verbatim, because
    a dotted path is case-insensitive in the app-label half as far as
    ``apps.get_model()`` is concerned, while the exclusion matching here is
    case-sensitive. Callers compare against :func:`model_label` output, so
    both sides must agree on casing.
    """
    from django.apps import apps

    model = apps.get_model(resolve_tenant_model_setting())
    return model_label(model)


def refusal_reason(app_label: str, *, apps=None) -> str | None:
    """Return why *app_label* cannot be adopted, or None when it can.

    Two conditions, both of which BR-RLS-016 requires to be errors at
    migrate time and at check time alike:

    - the app is deny-listed, and is therefore global by construction;
    - the app label names no installed app.

    The tenant model's own app is deliberately NOT refused. A consumer's
    tenants app commonly holds membership, settings and invitation models
    beside the tenant model itself, and those are ordinary tenant-scoped
    data; the tenant model and its through tables are skipped from the
    derived set instead, the way an excluded model is. What is refused is
    naming the tenant model itself, which surfaces as an empty adopted set
    and is reported by :func:`refusal_reason_for_model`.
    """
    if app_label in DENIED_APPS:
        return (
            f"app '{app_label}' is global by construction and cannot be tenant-scoped: "
            f"scoping it breaks Django's own bootstrap or request handling before any "
            f"tenant is resolved"
        )

    if apps is None:
        from django.apps import apps as apps_registry

        apps = apps_registry

    try:
        apps.get_app_config(app_label)
    except LookupError:
        return f"app '{app_label}' is not installed"

    return None


def refusal_reason_for_model(model, *, tenant_label: str | None = None) -> str | None:
    """Return why *model* cannot be adopted, or None when it can.

    Distinct from :func:`refusal_reason`, which polices the whole app. A
    model reaching here has already passed the app-level check, so what is
    left is the per-model half of BR-RLS-016: a deny-listed model, the
    configured tenant model itself, or Django's migration bookkeeping table.
    """
    label = model_label(model)

    if label in DENIED_MODELS:
        return f"model '{label}' is global by construction and cannot be tenant-scoped"

    if model._meta.db_table in DENIED_TABLES:
        return f"table '{model._meta.db_table}' (model '{label}') is global by construction and cannot be tenant-scoped"

    if tenant_label is None:
        tenant_label = tenant_model_label()
    if label == tenant_label:
        return (
            f"model '{label}' is the configured tenant model and is global by construction: "
            f"scoping it makes the tenant row unreachable when no tenant is yet active, "
            f"which is every resolution attempt"
        )

    return None


# ── The adopted set (BR-RLS-010) ─────────────────────────────


def _is_already_scoped(model) -> bool:
    """Return True if *model* already carries a boundary mixin.

    Adoption is the third of three mutually exclusive categories, so a model
    that is already column-bearing or path-scoped is skipped rather than
    adopted (BR-RLS-010).

    The mixin question is asked of the LIVE registry, not of the model class
    handed in, and this is the one place the derivation departs from a pure
    read of the migration state. ``StateApps`` rebuilds every model class
    from recorded fields with a plain ``models.Model`` base, so the
    ``_boundary_fk_field`` and ``boundary_tenant_path`` class attributes the
    mixins set are absent from a historical model and
    ``boundary.models.is_tenant_model()`` returns False for every one of
    them. Deriving this half from the state would therefore adopt a
    consumer's own already-scoped model and give it a second tenant
    discriminator.

    Asking the live registry is sound because being mixin-scoped is a fact
    about the model's Python class rather than about the schema at a
    migration point: the class either declares the mixin today or it does
    not, and a class that has since gained one is exactly the case where
    adopting it would be wrong. A model present in the historical state but
    absent from the live registry (deleted upstream since) is treated as not
    scoped, and is then excluded anyway by its table not existing.
    """
    from django.apps import apps as live_apps

    from boundary.models import is_tenant_model

    try:
        live = live_apps.get_model(model._meta.app_label, model.__name__)
    except (LookupError, ValueError):
        return False
    return is_tenant_model(live)


def adopted_models(app_label: str, apps=None, exclude=()):
    """Return the adopted set for *app_label*, in a stable order (BR-RLS-010).

    Every concrete model of the app that is not abstract and not a proxy,
    **including auto-created many-to-many through models**, minus:

    - every model named in ``BOUNDARY_ADOPT_EXCLUDE``;
    - every model named in *exclude* (the operation's own per-migration form,
      unioned with the setting);
    - every model that is already column-bearing or path-scoped;
    - the configured tenant model and its auto-created through tables
      (BR-RLS-016), which are skipped the way an excluded model is rather
      than refusing the whole app;
    - every deny-listed model (``auth.Permission``, ``admin.LogEntry``,
      ``django_migrations``), likewise skipped rather than refused, since
      reaching one of them by adopting its whole app is the ordinary case
      and BR-RLS-016 only refuses naming one directly.

    *apps* is either the live ``django.apps.apps`` registry or a migration
    ``StateApps``. Both expose
    ``get_app_config(label).get_models(include_auto_created=True)``, verified
    on Django 5.2: ``StateApps`` builds an ``AppConfigStub`` whose
    ``get_models()`` is ``AppConfig``'s own, reading ``all_models[label]``,
    which holds the rendered through-model classes. Deriving from
    ``ProjectState.models`` instead would be wrong, because that mapping is
    keyed on declared models only and omits auto-created through models
    entirely, which is exactly the case AC-RLS-012 pins.

    Passing None reads the live registry, which is what ``boundary.E007``
    does; ``AdoptTenantApp`` passes the migration state's apps, so applying
    an old migration to a new database produces the schema that migration
    described rather than the schema today's installed package would.
    """
    if apps is None:
        from django.apps import apps as apps_registry

        apps = apps_registry

    excluded = set(boundary_settings.ADOPT_EXCLUDE) | set(exclude)
    tenant_label = tenant_model_label()
    tenant_through_labels = _tenant_through_labels(apps, tenant_label)

    adopted = []
    for model in apps.get_app_config(app_label).get_models(include_auto_created=True):
        if model._meta.abstract or model._meta.proxy:
            continue
        label = model_label(model)
        if label in excluded:
            continue
        if label == tenant_label or label in tenant_through_labels:
            continue
        if label in DENIED_MODELS or model._meta.db_table in DENIED_TABLES:
            continue
        if _is_already_scoped(model):
            continue
        adopted.append(model)

    return adopted


def _tenant_through_labels(apps, tenant_label: str) -> set[str]:
    """Return the labels of the tenant model's auto-created through tables.

    A through table keyed to the tenant model is as global as the tenant
    model itself: scoping it makes the row unreachable when no tenant is
    active (BR-RLS-016). Resolved from the same registry the adopted set is
    derived from, so a historical state and the live registry each answer for
    their own point in time.

    Only auto-created through models count. An explicit ``through=`` model a
    consumer wrote is an ordinary model with its own fields, and is
    tenant-scopable like any other; boundary does not decide otherwise on the
    consumer's behalf.
    """
    tenant_app_label, tenant_model_name = tenant_label.split(".", 1)
    try:
        tenant_model = apps.get_model(tenant_app_label, tenant_model_name)
    except (LookupError, ValueError):
        return set()

    labels = set()
    for field in tenant_model._meta.get_fields():
        through = getattr(getattr(field, "remote_field", None), "through", None)
        if through is not None and through._meta.auto_created:
            labels.add(model_label(through))
    return labels


# ── PostgreSQL catalogue introspection ───────────────────────
#
# Every helper below takes a cursor rather than opening one, so the caller
# (AdoptTenantApp inside the migration's transaction, boundary.E007 inside
# its own connection handling) controls the connection and its error
# handling. BR-RLS-012 requires the operation to introspect what it is
# rewriting rather than construct the names it expects, because Django
# generates the four unique sources into two different database forms under
# two different naming schemes: a field declared unique=True becomes an
# inline UNIQUE that PostgreSQL names "<table>_<column>_key", while
# unique_together and Meta.constraints become named constraints the schema
# editor hashes. Constructing either name would miss the other.


def column_type(cursor, table: str, column: str) -> str | None:
    """Return the PostgreSQL type name of *column* on *table*, or None.

    None means the column is absent, which for ``tenant_id`` is the
    not-yet-adopted state (and, at check time, condition 1 of
    ``boundary.E007``). ``to_regclass()`` resolves the table through the
    connection's own search_path, exactly as an ordinary query against it
    would, rather than matching ``relname`` across every schema present.
    """
    cursor.execute(
        """
        SELECT format_type(a.atttypid, a.atttypmod)
        FROM pg_attribute a
        WHERE a.attrelid = to_regclass(%s)::oid
          AND a.attname = %s
          AND a.attnum > 0
          AND NOT a.attisdropped
        """,
        [table, column],
    )
    row = cursor.fetchone()
    return row[0] if row else None


def row_count(cursor, table: str) -> int:
    """Return the exact row count of *table*.

    A real ``COUNT(*)``, not a ``reltuples`` estimate: BR-RLS-014 requires
    the backfill UPDATE's affected-row count to equal this number exactly,
    and an estimate would make that comparison meaningless. The table is
    quoted rather than parameterised because a table name is an identifier,
    not a value; it reaches here from the model's own ``_meta.db_table``.
    """
    cursor.execute(f'SELECT COUNT(*) FROM "{table}"')
    return cursor.fetchone()[0]


def unique_constraints(cursor, table: str) -> list[dict]:
    """Return every non-primary-key UNIQUE constraint on *table*.

    One dict per ``pg_constraint`` row with ``contype = 'u'``:

    ``name``
        the constraint name, which is what ``DROP CONSTRAINT`` takes and
        what the ``_tenant``-suffixed replacement name is derived from.
    ``columns``
        the constrained column names, in constraint order.
    ``deferrable``
        ``pg_constraint.condeferrable``. A deferrable constraint is refused
        rather than rewritten (BR-RLS-012).
    ``fk_targeted``
        True when any foreign key on any table targets this constraint,
        which is what ``ForeignKey(to_field=...)`` creates. Dropping it
        would invalidate the referencing constraint and the composite
        replacement cannot satisfy it, so it is refused.

    Primary keys are ``contype = 'p'`` and never appear here: BR-RLS-012
    leaves the primary key alone.
    """
    cursor.execute(
        """
        SELECT
            c.conname,
            ARRAY(
                SELECT a.attname
                FROM unnest(c.conkey) WITH ORDINALITY AS k(attnum, ord)
                JOIN pg_attribute a
                  ON a.attrelid = c.conrelid AND a.attnum = k.attnum
                ORDER BY k.ord
            ) AS columns,
            c.condeferrable,
            EXISTS (
                SELECT 1 FROM pg_constraint f
                WHERE f.contype = 'f' AND f.conindid = c.conindid
            ) AS fk_targeted
        FROM pg_constraint c
        WHERE c.conrelid = to_regclass(%s)::oid
          AND c.contype = 'u'
        ORDER BY c.conname
        """,
        [table],
    )
    return [
        {
            "name": name,
            "columns": list(columns),
            "deferrable": deferrable,
            "fk_targeted": fk_targeted,
        }
        for name, columns, deferrable, fk_targeted in cursor.fetchall()
    ]


def unique_indexes(cursor, table: str) -> list[dict]:
    """Return every unique index on *table*, primary key included.

    One dict per ``pg_index`` row with ``indisunique``:

    ``name``
        the index name.
    ``columns``
        the indexed column names, in index order. Empty for an index whose
        keys are all expressions.
    ``primary``
        ``indisprimary``. The primary key index is reported rather than
        filtered out, because ``boundary.E007``'s condition 5 and
        AC-RLS-010's "no unique index whose leading column is anything other
        than tenant_id, excluding the primary key index" both need to tell
        it apart from the rest.
    ``partial``
        True when ``indpred`` is not null, which is what a
        ``UniqueConstraint(condition=...)`` creates. Refused rather than
        rewritten: the predicate cannot be safely re-expressed against the
        added column (BR-RLS-012).
    ``expression``
        True when ``indexprs`` is not null. Likewise refused.
    ``constraint``
        the name of the constraint this index backs, or None for a bare
        index. An index backing a constraint is dropped by dropping the
        constraint, never by ``DROP INDEX``, so the rewrite must not treat
        it as a separate thing to handle.
    """
    cursor.execute(
        """
        SELECT
            i.relname,
            ARRAY(
                SELECT a.attname
                FROM unnest(x.indkey) WITH ORDINALITY AS k(attnum, ord)
                JOIN pg_attribute a
                  ON a.attrelid = x.indrelid AND a.attnum = k.attnum
                ORDER BY k.ord
            ) AS columns,
            x.indisprimary,
            x.indpred IS NOT NULL AS partial,
            x.indexprs IS NOT NULL AS expression,
            c.conname
        FROM pg_index x
        JOIN pg_class i ON i.oid = x.indexrelid
        LEFT JOIN pg_constraint c ON c.conindid = x.indexrelid AND c.contype IN ('u', 'p')
        WHERE x.indrelid = to_regclass(%s)::oid
          AND x.indisunique
        ORDER BY i.relname
        """,
        [table],
    )
    return [
        {
            "name": name,
            "columns": list(columns),
            "primary": primary,
            "partial": partial,
            "expression": expression,
            "constraint": constraint,
        }
        for name, columns, primary, partial, expression, constraint in cursor.fetchall()
    ]
