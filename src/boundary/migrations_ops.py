"""Custom migration operations for PostgreSQL Row Level Security.

These operations are included in auto-generated migrations for TenantModel
subclasses, or can be added manually by developers.
"""

from dataclasses import dataclass

from django.db import migrations

from boundary.conf import boundary_settings


def _sql_string_literal(value: str) -> str:
    """Quote a value as a PostgreSQL string literal.

    The session-variable names come from project settings
    (``BOUNDARY_DB_SESSION_VAR`` / ``BOUNDARY_ADMIN_FLAG_VAR``) and are
    interpolated into ``current_setting('...')`` calls, so they are escaped
    to avoid breaking the generated SQL.
    """
    escaped = value.replace("'", "''")
    return f"'{escaped}'"


def create_tenant_id_helper(schema_editor, pg_type: str) -> None:
    """``CREATE OR REPLACE`` the ``boundary_current_tenant_id()`` helper.

    Shared by :class:`CreateTenantPolicy` and :class:`AdoptTenantApp` so both
    emit byte-identical SQL for the function every boundary policy and every
    adopted column default references (BR-RLS-011 step 1). ``CREATE OR
    REPLACE FUNCTION`` is idempotent, so emitting it repeatedly is safe and
    removes any ordering dependency between the operations;
    :class:`AdoptTenantApp` emits it once per call, ahead of the first
    table's DDL.

    *pg_type* is the PostgreSQL type the configured tenant model's primary
    key derives, as :func:`detect_tenant_pg_type` maps it.
    """
    session_var = _sql_string_literal(boundary_settings.DB_SESSION_VAR)

    # LEAKPROOF is a planner optimisation, not an isolation requirement, and
    # only a superuser may declare it. Managed Postgres (DigitalOcean, RDS,
    # Cloud SQL, Azure, Heroku, Supabase) grants no superuser role, so the
    # migration would abort with "only superuser can define a leakproof
    # function". Default off; opt in via BOUNDARY_FUNCTION_LEAKPROOF on a
    # self-managed cluster where the migrating role is a superuser.
    leakproof = " LEAKPROOF" if boundary_settings.FUNCTION_LEAKPROOF else ""

    schema_editor.execute(f"""
            CREATE OR REPLACE FUNCTION boundary_current_tenant_id()
            RETURNS {pg_type} AS $$
            BEGIN
                RETURN NULLIF(
                    current_setting({session_var}, true), ''
                )::{pg_type};
            EXCEPTION WHEN OTHERS THEN
                RETURN NULL;
            END;
            $$ LANGUAGE plpgsql STABLE{leakproof}
        """)


def create_tenant_policies(schema_editor, table: str, tenant_column: str = "tenant_id") -> None:
    """Create ``boundary_tenant_isolation`` and ``boundary_admin_bypass`` on *table*.

    Shared by :class:`CreateTenantPolicy` and :class:`AdoptTenantApp`, so an
    adopted table is indistinguishable from a column-bearing one from
    PostgreSQL's side (BR-RLS-011 step 7). The session-variable names come
    from settings rather than being hardcoded, since a project that
    customises them would otherwise get policies reading a variable nothing
    sets.
    """
    admin_flag = _sql_string_literal(boundary_settings.ADMIN_FLAG_VAR)

    # Isolation policy with WITH CHECK for INSERT enforcement (BR-RLS-008)
    schema_editor.execute(
        f'CREATE POLICY boundary_tenant_isolation ON "{table}" '
        f"USING ({tenant_column} = boundary_current_tenant_id()) "
        f"WITH CHECK ({tenant_column} = boundary_current_tenant_id())"
    )

    # Admin bypass policy
    schema_editor.execute(
        f"CREATE POLICY boundary_admin_bypass ON \"{table}\" USING (current_setting({admin_flag}, TRUE) = 'true')"
    )


# Django database types for a primary key, mapped to the PostgreSQL type the
# tenant discriminator column and the helper function's return value take.
# Additive-safe to extend; narrowing it is a compatibility-assessed change.
_PG_TYPE_MAP = {
    "uuid": "uuid",
    "integer": "bigint",
    "bigint": "bigint",
    "serial": "bigint",
    "bigserial": "bigint",
}


#: PostgreSQL identifier length limit. A generated constraint name longer
#: than this is silently truncated by the server, which would make the name
#: unreproducible, so the operation truncates it deliberately instead.
_MAX_IDENTIFIER_LENGTH = 63


def composite_constraint_name(schema_editor, table: str, original_name: str, columns) -> str:
    """Derive the ``_tenant``-suffixed name for a rewritten unique constraint.

    The original name with ``_tenant`` appended, so the name is reproducible
    from the original alone (BR-RLS-012). When that exceeds PostgreSQL's
    63-character identifier limit, the name is regenerated through the schema
    editor's own truncate-and-hash scheme
    (``BaseDatabaseSchemaEditor._create_index_name``) rather than being cut
    off, which would risk colliding with a sibling constraint whose name
    shares the surviving prefix.
    """
    suffixed = f"{original_name}_tenant"
    if len(suffixed) <= _MAX_IDENTIFIER_LENGTH:
        return suffixed
    return schema_editor._create_index_name(table, list(columns), suffix="_tenant")


def _pg_types_match(found: str, expected: str) -> bool:
    """Return True if a ``format_type()`` result names the *expected* type.

    ``format_type()`` returns the SQL spelling (``"uuid"``, ``"bigint"``),
    which matches the map's values directly. The comparison is written as a
    function so the aliasing ``bigint`` shares with its serial spellings has
    one home rather than being inlined at the call site.
    """
    return found.strip().lower() == expected.strip().lower()


def _historical_unique_column_sets(model):
    """Yield the field tuples Django would build unique constraints from.

    Read from the model's own ``_meta``, which on the reverse path is the
    historical state's ``_meta`` rather than the live class's, so what is
    restored matches that migration state (BR-RLS-020). Covers the three
    sources the forward rewrites into constraints:

    - a field declared ``unique=True`` (excluding the primary key, which is
      never rewritten);
    - each ``Meta.unique_together`` tuple;
    - each unconditional ``Meta.constraints`` ``UniqueConstraint`` over plain
      fields.

    A ``UniqueConstraint`` carrying a ``condition``, ``expressions``,
    ``deferrable``, ``include``, ``opclasses`` or ``nulls_distinct`` is
    skipped here, because the forward refused the table outright rather than
    rewriting such a form, so there is nothing for the reverse to restore.
    """
    from django.db.models import UniqueConstraint

    for field in model._meta.local_fields:
        if field.unique and not field.primary_key:
            yield [field]

    for field_names in model._meta.unique_together:
        yield [model._meta.get_field(name) for name in field_names]

    for constraint in model._meta.constraints:
        if not isinstance(constraint, UniqueConstraint):
            continue
        if not constraint.fields:
            continue
        if any(
            getattr(constraint, attribute, None)
            for attribute in ("condition", "expressions", "deferrable", "include", "opclasses")
        ):
            continue
        yield [model._meta.get_field(name) for name in constraint.fields]


def detect_tenant_pg_type(pk_field) -> str:
    """Map a tenant model's primary-key field to its PostgreSQL cast type.

    ``uuid`` for a UUID primary key, ``bigint`` for any integer one, and
    ``bigint`` as the fallback for a type the map does not cover, which is
    the same unrecognised-type fallback ``rls-policy.v1`` has always carried.
    """
    from django.db import connection

    return _PG_TYPE_MAP.get(pk_field.db_type(connection), "bigint")


def _router_allows(schema_editor, model) -> bool:
    """Return whether the router admits *model* on this alias (BR-RLS-021 gate 1).

    Asked through ``router.allow_migrate_model()``, which is the call
    Django's own ``CreateModel`` and ``AddField`` make for model-level DDL.
    It reaches a consumer's routers as ``allow_migrate(db, app_label,
    model_name=<model name>, model=<model>)`` carrying the RESOLVED model's
    own app label, which under BR-RLS-019's ``app_label`` override is the
    overridden app rather than the app whose migration carries the
    operation: the table under consideration is that app's table, and a
    router keying on either argument expects to be told about the table it
    will actually route.

    Not covered by Django's own machinery. A ``reduces_to_sql`` operation is
    never passed through ``allow_migrate_model`` by the migration executor,
    so without this gate a project whose router keeps a second alias off the
    RLS graph still has ``ALTER TABLE ... ENABLE ROW LEVEL SECURITY`` pushed
    at it, and the whole graph for that alias fails on the backend's parser
    (icvoss/django-boundary#75).

    Asked once, not per model: these three operations act on exactly one
    model, where ``AdoptTenantApp`` derives a set and must therefore ask per
    member of it.

    A denial is a legitimate per-alias skip, so the caller returns without
    DDL and without raising. Only once the router has admitted the model is
    the vendor read, because that router is the documented way to keep a
    non-PostgreSQL alias off this graph and must not itself become a hard
    failure.
    """
    from django.db import router

    return router.allow_migrate_model(schema_editor.connection.alias, model)


def _require_postgresql(schema_editor, operation: str, model) -> None:
    """Refuse a non-PostgreSQL connection the router did admit (BR-RLS-021 gate 2).

    Raises :class:`~boundary.exceptions.RLSOperationRefusedError` naming
    *operation*, the alias and the observed vendor, before any statement
    runs. *operation* is the class name of the operation raising, so a
    consumer reading the traceback is told which of the three refused rather
    than being told about adoption, which is a different operation raising a
    different exception.

    Only PostgreSQL has Row Level Security. On SQLite or MySQL the generated
    ``ENABLE ROW LEVEL SECURITY`` and ``CREATE POLICY`` statements are a
    syntax error, so the migration fails either way and what differs is the
    message: the backend's own parser error names a fragment of generated SQL
    and leaves the consumer to infer that this layer is PostgreSQL-only.

    Reached only after :func:`_router_allows` has admitted the model, so an
    alias the router denies is never asked about its vendor at all.
    """
    from boundary.exceptions import RLSOperationRefusedError

    vendor = schema_editor.connection.vendor
    if vendor == "postgresql":
        return
    raise RLSOperationRefusedError(
        f"{operation} cannot run against model '{model._meta.label}' on database alias "
        f"'{schema_editor.connection.alias}': its backend vendor is '{vendor}', and this "
        f"operation emits PostgreSQL Row Level Security, which no other backend has. Keep "
        f"this alias off the operation's graph with a router's allow_migrate(), or run the "
        f"migration against PostgreSQL."
    )


class EnableRLS(migrations.operations.base.Operation):
    """Enable Row Level Security on a table.

    Reversible: disables RLS on the table.

    ``app_label`` overrides the app the model is resolved from (BR-RLS-019),
    so a consumer can enable RLS on a third-party app's table from a
    migration in their own app without relocating that app's migrations via
    ``MIGRATION_MODULES``. It is omitted from ``deconstruct()`` when unset,
    so every migration written before the keyword existed serialises
    byte-identically to what it serialised before.
    """

    reduces_to_sql = True
    reversible = True

    def __init__(self, model_name: str, app_label: str | None = None):
        self.model_name = model_name
        self.app_label = app_label

    def state_forwards(self, app_label, state):
        pass

    def database_forwards(self, app_label, schema_editor, from_state, to_state):
        model = to_state.apps.get_model(self.app_label or app_label, self.model_name)
        # The two gates, in BR-RLS-021's order: the router first, whose denial
        # is a silent per-alias skip, then the vendor, whose mismatch on an
        # alias the router admitted is named rather than left to the backend's
        # parser. Both run before any statement is issued.
        if not _router_allows(schema_editor, model):
            return
        _require_postgresql(schema_editor, "EnableRLS", model)

        table = model._meta.db_table
        schema_editor.execute(f'ALTER TABLE "{table}" ENABLE ROW LEVEL SECURITY')
        schema_editor.execute(f'ALTER TABLE "{table}" FORCE ROW LEVEL SECURITY')

    def database_backwards(self, app_label, schema_editor, from_state, to_state):
        model = from_state.apps.get_model(self.app_label or app_label, self.model_name)
        # Both gates apply on reverse too. A guard on the forward alone would
        # let the reverse disable RLS on an alias the router keeps off this
        # graph, which never had it enabled there in the first place.
        if not _router_allows(schema_editor, model):
            return
        _require_postgresql(schema_editor, "EnableRLS", model)

        table = model._meta.db_table
        # Drop all boundary policies first
        schema_editor.execute(f'DROP POLICY IF EXISTS boundary_tenant_isolation ON "{table}"')
        schema_editor.execute(f'DROP POLICY IF EXISTS boundary_admin_bypass ON "{table}"')
        schema_editor.execute(f'ALTER TABLE "{table}" DISABLE ROW LEVEL SECURITY')
        schema_editor.execute(f'ALTER TABLE "{table}" NO FORCE ROW LEVEL SECURITY')

    def describe(self):
        return f"Enable Row Level Security on {self._target_description()}"

    def _target_description(self):
        """Name the target as ``app_label.ModelName`` when the override is set."""
        if self.app_label:
            return f"{self.app_label}.{self.model_name}"
        return self.model_name

    def deconstruct(self):
        kwargs = {"model_name": self.model_name}
        if self.app_label is not None:
            kwargs["app_label"] = self.app_label
        return (
            self.__class__.__qualname__,
            [],
            kwargs,
        )


class CreateTenantPolicy(migrations.operations.base.Operation):
    """Create tenant isolation and admin bypass RLS policies.

    Must be applied after EnableRLS. Creates the boundary_current_tenant_id()
    helper function if it does not already exist. The function is declared
    LEAKPROOF only when BOUNDARY_FUNCTION_LEAKPROOF is set (default off), since
    LEAKPROOF requires a superuser and managed Postgres grants none.

    Reversible: drops both policies.

    ``app_label`` overrides the app the model is resolved from (BR-RLS-019),
    and is omitted from ``deconstruct()`` when unset so pre-existing
    migrations serialise byte-identically.
    """

    reduces_to_sql = True
    reversible = True

    def __init__(self, model_name: str, tenant_column: str | None = None, app_label: str | None = None):
        self.model_name = model_name
        self.tenant_column = tenant_column or "tenant_id"
        self.app_label = app_label

    def state_forwards(self, app_label, state):
        pass

    def database_forwards(self, app_label, schema_editor, from_state, to_state):
        model = to_state.apps.get_model(self.app_label or app_label, self.model_name)
        # The two gates, in BR-RLS-021's order. Checked ahead of the tenant
        # column type detection as well as the DDL, so a denied alias reads
        # nothing from the model either.
        if not _router_allows(schema_editor, model):
            return
        _require_postgresql(schema_editor, "CreateTenantPolicy", model)

        table = model._meta.db_table

        # Detect tenant column's database type for the helper function
        pg_type = self._detect_tenant_column_type(model)

        # Create the helper function (idempotent via CREATE OR REPLACE), then
        # both policies. Both SQL shapes are shared with AdoptTenantApp so an
        # adopted table is indistinguishable from this one (BR-RLS-011).
        create_tenant_id_helper(schema_editor, pg_type)
        create_tenant_policies(schema_editor, table, self.tenant_column)

    def _detect_tenant_column_type(self, model):
        """Detect the PostgreSQL type of the tenant FK's target PK."""
        field_name = self.tenant_column.removesuffix("_id")
        tenant_field = model._meta.get_field(field_name)
        return detect_tenant_pg_type(tenant_field.related_model._meta.pk)

    def database_backwards(self, app_label, schema_editor, from_state, to_state):
        model = from_state.apps.get_model(self.app_label or app_label, self.model_name)
        if not _router_allows(schema_editor, model):
            return
        _require_postgresql(schema_editor, "CreateTenantPolicy", model)

        table = model._meta.db_table
        schema_editor.execute(f'DROP POLICY IF EXISTS boundary_tenant_isolation ON "{table}"')
        schema_editor.execute(f'DROP POLICY IF EXISTS boundary_admin_bypass ON "{table}"')

    def describe(self):
        return f"Create tenant RLS policies on {self._target_description()}"

    def _target_description(self):
        """Name the target as ``app_label.ModelName`` when the override is set."""
        if self.app_label:
            return f"{self.app_label}.{self.model_name}"
        return self.model_name

    def deconstruct(self):
        kwargs = {"model_name": self.model_name}
        if self.tenant_column != "tenant_id":
            kwargs["tenant_column"] = self.tenant_column
        if self.app_label is not None:
            kwargs["app_label"] = self.app_label
        return (
            self.__class__.__qualname__,
            [],
            kwargs,
        )


class DropTenantPolicy(migrations.operations.base.Operation):
    """Drop boundary RLS policies from a table.

    Reversible: re-creates the policies.

    ``app_label`` overrides the app the model is resolved from (BR-RLS-019),
    and is omitted from ``deconstruct()`` when unset so pre-existing
    migrations serialise byte-identically.
    """

    reduces_to_sql = True
    reversible = True

    def __init__(self, model_name: str, tenant_column: str | None = None, app_label: str | None = None):
        self.model_name = model_name
        self.tenant_column = tenant_column or "tenant_id"
        self.app_label = app_label

    def state_forwards(self, app_label, state):
        pass

    def database_forwards(self, app_label, schema_editor, from_state, to_state):
        model = to_state.apps.get_model(self.app_label or app_label, self.model_name)
        if not _router_allows(schema_editor, model):
            return
        _require_postgresql(schema_editor, "DropTenantPolicy", model)

        table = model._meta.db_table
        schema_editor.execute(f'DROP POLICY IF EXISTS boundary_tenant_isolation ON "{table}"')
        schema_editor.execute(f'DROP POLICY IF EXISTS boundary_admin_bypass ON "{table}"')

    def database_backwards(self, app_label, schema_editor, from_state, to_state):
        # Re-create policies on reverse. The override is passed through so the
        # reverse targets the same table the forward dropped from.
        #
        # Gated here as well as inside the delegate, and deliberately so. The
        # delegate carries its own copy of both gates, but it reports them as
        # CreateTenantPolicy, and a consumer reversing DropTenantPolicy on a
        # non-PostgreSQL alias must be told which operation in their migration
        # refused. Gating first means the refusal names DropTenantPolicy and
        # the delegate is never reached; the router half is idempotent, so
        # asking twice on an admitted alias changes nothing.
        #
        # Resolved from ``from_state`` here, which is the state a reverse
        # reads (BR-RLS-019) and what the other five gated methods use in
        # this direction. The delegate re-resolves from ``to_state``, as it
        # has always done; the two agree for any model whose recorded fields
        # match, and the gate's question is which TABLE the router routes,
        # which does not differ between the two states.
        model = from_state.apps.get_model(self.app_label or app_label, self.model_name)
        if not _router_allows(schema_editor, model):
            return
        _require_postgresql(schema_editor, "DropTenantPolicy", model)

        create_op = CreateTenantPolicy(self.model_name, self.tenant_column, app_label=self.app_label)
        create_op.database_forwards(app_label, schema_editor, from_state, to_state)

    def describe(self):
        return f"Drop tenant RLS policies from {self._target_description()}"

    def _target_description(self):
        """Name the target as ``app_label.ModelName`` when the override is set."""
        if self.app_label:
            return f"{self.app_label}.{self.model_name}"
        return self.model_name

    def deconstruct(self):
        kwargs = {"model_name": self.model_name}
        if self.tenant_column != "tenant_id":
            kwargs["tenant_column"] = self.tenant_column
        if self.app_label is not None:
            kwargs["app_label"] = self.app_label
        return (
            self.__class__.__qualname__,
            [],
            kwargs,
        )


@dataclass(frozen=True)
class _AdoptionPlan:
    """What ``AdoptTenantApp`` will do to one table, decided before any DDL.

    Produced by the read-only validation pass and consumed by the DDL pass, so
    every refusal for every table in the derived set is raised while the
    database is still untouched. The catalogue reads the plan carries
    (``existing_rows``, ``rewrites``) are the same ones the DDL would
    otherwise have made mid-flight.
    """

    model: object
    table: str
    label: str
    pg_type: str
    existing_rows: int
    rewrites: list


class AdoptTenantApp(migrations.operations.base.Operation):
    """Tenant-scope every concrete table of a third-party app (BR-RLS-013).

    Applied from a migration in the **consuming project's own app**, never by
    a post_migrate handler or an AppConfig.ready() hook. The adopted app
    ships nothing, knows nothing, is never imported by boundary, and its own
    migration state, model classes and MIGRATION_MODULES are untouched.

    Per adopted table, the operation adds a ``tenant_id`` column that no
    Django field models, rewrites every non-primary-key unique constraint to
    lead with it, and enables PostgreSQL Row Level Security with the same two
    policies and the same helper function a column-bearing table gets. From
    PostgreSQL's side an adopted table is indistinguishable from a
    column-bearing one; from Django's side it is unchanged.

    ``state_forwards()`` is a no-op. Recording the column in migration state
    would put it back into ``makemigrations``' field-level view of the
    third-party app, which is precisely what this operation avoids.

    **Reversal loses tenant assignment.** The per-row assignment lives only
    in the dropped column, and boundary snapshots nothing before dropping it
    (BR-RLS-020). Re-applying forwards against the now-populated table is
    then refused unless a ``backfill_tenant`` is supplied, and no backfill
    can recover what the reverse discarded.

    :param app_label: the app whose models this operation adopts.
    :param exclude: ``"app_label.ModelName"`` strings exempted in addition to
        ``BOUNDARY_ADOPT_EXCLUDE``. The per-migration form, used to split one
        app across several operations when different tables need different
        ``backfill_tenant`` values.
    :param backfill_tenant: the tenant primary-key value every pre-existing
        row is stamped with. Required for a populated table (BR-RLS-014),
        and applied to every table this operation adopts in that call.
    """

    reduces_to_sql = True
    reversible = True

    # atomic is deliberately left at Django's default of True (BR-RLS-013).
    # A refusal or a failure on any one table must roll back every table the
    # same call had already altered, and PostgreSQL's transactional DDL is
    # what makes that hold for the ALTER TABLE and CREATE POLICY statements
    # as well as for the backfill UPDATE.

    def __init__(self, app_label: str, exclude=(), backfill_tenant=None):
        self.app_label = app_label
        self.exclude = tuple(exclude)
        self.backfill_tenant = backfill_tenant

    def state_forwards(self, app_label, state):
        pass

    # ── Forwards ─────────────────────────────────────────────

    def database_forwards(self, app_label, schema_editor, from_state, to_state):
        """Adopt every table in the derived set, in the order BR-RLS-011 fixes.

        *app_label* is the label of the app owning the migration, which is
        the consumer's own app and is deliberately not the app being
        adopted; ``self.app_label`` names that one.

        The adopted set comes from the migration state's historical apps, so
        applying an old migration to a new database produces the schema that
        migration described rather than the schema today's installed package
        version would describe (BR-RLS-013).

        **Every refusal is evaluated for every table before any DDL is
        issued.** The operation is atomic, so a refusal on the last table
        rolls back the DDL the earlier ones had already taken; but a rollback
        is not the same thing as never having issued the statements. A
        migration run against a role or a database where the ALTER succeeds
        and the rollback does not (a non-transactional backend, an
        autocommitting wrapper, a connection dropped mid-migration) would
        leave a table part-adopted that the refusal says was never adopted at
        all. Validating the whole set first also makes the refusal message
        the FIRST thing a consumer sees, rather than the tail of a stack of
        statements they now have to reason about.
        """
        from boundary import adoption
        from boundary.exceptions import AdoptionRefusedError

        refusal = adoption.refusal_reason(self.app_label, apps=to_state.apps)
        if refusal is not None:
            raise AdoptionRefusedError(f"AdoptTenantApp cannot adopt: {refusal}")

        models = adoption.adopted_models(self.app_label, apps=to_state.apps, exclude=self.exclude)

        # A single-model app whose only model is the tenant model derives an
        # empty set. That is a refusal rather than a silent no-op: the
        # consumer asked for something that cannot happen, and BR-RLS-016
        # requires naming the model rather than leaving them to discover
        # nothing was adopted. Checked ahead of the router gate, because an
        # app with nothing adoptable is a mistake in the migration file and
        # is the same mistake on every alias, where a router denial is a
        # legitimate per-alias skip.
        if not models:
            self._refuse_empty_set(to_state.apps)

        # Gate one: the router, per model. A denied table drops out of the
        # set for this alias; an empty remainder means this alias holds none
        # of the app's tables, so the operation issues nothing at all, not
        # even the helper, and the vendor gate is never reached.
        models = self._router_allowed_models(schema_editor, models)
        if not models:
            return

        # Gate two: the vendor, only now that at least one table is in play.
        self._require_postgresql(schema_editor)

        pg_type = self._tenant_pg_type()

        # Pass one: read-only. Every per-table refusal for every table in the
        # derived set, with nothing written. A plan of None means the table is
        # already adopted and is skipped unchanged (BR-RLS-013's per-table
        # idempotency).
        plans = []
        with schema_editor.connection.cursor() as cursor:
            for model in models:
                plan = self._validate_table(cursor, model, pg_type)
                if plan is not None:
                    plans.append(plan)

        # Pass two: the DDL. The helper both the column DEFAULT and both
        # policies reference is created once for the whole operation rather
        # than per table; CREATE OR REPLACE FUNCTION is idempotent either way,
        # and emitting it once keeps it out of the per-table step order
        # without changing what any table ends up referencing.
        if plans:
            create_tenant_id_helper(schema_editor, pg_type)
        for plan in plans:
            self._adopt_table(schema_editor, plan)

    def _router_allowed_models(self, schema_editor, models):
        """Return the models the router permits on this alias (BR-RLS-013).

        Asked per model through ``router.allow_migrate_model()``, which is
        the call Django's own ``CreateModel`` and ``AddField`` make for
        model-level DDL. It reaches a consumer's routers as
        ``allow_migrate(db, app_label, model_name=<model name>,
        model=<model>)`` with the ADOPTED app's own label, which is what a
        router keying on either argument expects to receive: the table under
        consideration is a table of the adopted app, not of the app whose
        migration happens to carry the operation.

        Asking per model rather than once for the whole operation is what
        makes the gate answerable. A router is written to answer "may this
        table exist on this alias", and under regional routing an adopted
        app's tables exist only on the aliases the router admits; a single
        app-level question has no answer when the router admits some tables
        and not others. A denied table is skipped on that alias with no DDL
        and no error, and an empty remainder makes the whole operation a
        silent no-op there.

        Not covered by Django's own machinery: ``reduces_to_sql`` operations
        such as this one are never passed through ``allow_migrate_model`` by
        the executor, so without this gate a project whose router keeps an
        alias off the adoption graph still has the DDL pushed at it.
        """
        from django.db import router

        alias = schema_editor.connection.alias
        return [model for model in models if router.allow_migrate_model(alias, model)]

    def _require_postgresql(self, schema_editor) -> None:
        """Refuse a non-PostgreSQL connection the router did allow.

        Adoption is Row Level Security, and only PostgreSQL has it. On SQLite
        or MySQL the ``CREATE POLICY`` statements are a syntax error, so the
        migration fails either way; what differs is the message. A vendor
        refusal names the backend and the rule, where the backend's own
        parser error names a fragment of generated SQL and leaves the
        consumer to work out that adoption is PostgreSQL-only
        (BR-RLS-015: an adopted table on a non-PostgreSQL backend is
        unisolated and has no ORM layer underneath it to compensate).

        Checked only once the router has left at least one table in the set,
        so the documented way to keep a non-PostgreSQL alias off the adoption
        graph (a router's ``allow_migrate``) stays a silent no-op rather than
        becoming a refusal. An alias the router denies every table on is
        never asked about its vendor at all.
        """
        from boundary.exceptions import AdoptionRefusedError

        vendor = schema_editor.connection.vendor
        if vendor == "postgresql":
            return
        raise AdoptionRefusedError(
            f"AdoptTenantApp cannot adopt app '{self.app_label}' on database alias "
            f"'{schema_editor.connection.alias}': its backend vendor is '{vendor}', and adoption "
            f"is PostgreSQL Row Level Security, which no other backend has. Keep this alias off "
            f"the adoption migration's graph with a router's allow_migrate(), or run the migration "
            f"against PostgreSQL."
        )

    def _tenant_pg_type(self) -> str:
        """Return the adopted column's type, refusing an unmappable tenant key.

        ``detect_tenant_pg_type`` falls back to ``bigint`` for a type its map
        does not cover, which is correct for ``CreateTenantPolicy`` (that
        operation acts on a column a Django field already declares, so the
        type is not boundary's to get wrong) and wrong here: adoption CREATES
        the column, and a silent ``bigint`` for, say, a ``CharField``
        primary key puts a column of the wrong type on every adopted table.
        Because the column is invisible to the ORM, the mismatch first
        surfaces as a cast failure inside ``boundary_current_tenant_id()``,
        which returns NULL on any exception, so it reads as empty results and
        NOT NULL violations with nothing naming the cause (BR-RLS-011 step 1).
        """
        from django.db import connection

        from boundary.conf import get_tenant_model, resolve_tenant_model_setting
        from boundary.exceptions import AdoptionRefusedError

        pk_field = get_tenant_model()._meta.pk
        db_type = pk_field.db_type(connection)
        pg_type = _PG_TYPE_MAP.get(db_type)
        if pg_type is None:
            raise AdoptionRefusedError(
                f"AdoptTenantApp cannot adopt app '{self.app_label}': the configured tenant model "
                f"'{resolve_tenant_model_setting()}' has primary key '{pk_field.name}' of type "
                f"'{type(pk_field).__name__}', whose database type '{db_type}' maps to neither "
                f"uuid nor bigint. Adoption creates the tenant_id column itself, so it refuses "
                f"rather than guess a type: a wrong one is invisible to the ORM and surfaces only "
                f"as empty reads and NOT NULL violations. Give the tenant model a UUID or integer "
                f"primary key, or scope these tables with a declared tenant field instead."
            )
        return pg_type

    def _refuse_empty_set(self, apps):
        """Raise naming why the derived set is empty, never return normally.

        The interesting case is an app whose every model is skipped for a
        reason BR-RLS-016 makes an error when named directly, above all the
        configured tenant model. An app with genuinely no concrete models is
        reported as such rather than mislabelled.
        """
        from boundary import adoption
        from boundary.exceptions import AdoptionRefusedError

        tenant_label = adoption.tenant_model_label()
        reasons = []
        for model in apps.get_app_config(self.app_label).get_models(include_auto_created=True):
            if model._meta.abstract or model._meta.proxy:
                continue
            reason = adoption.refusal_reason_for_model(model, tenant_label=tenant_label)
            if reason is not None:
                reasons.append(reason)

        if reasons:
            raise AdoptionRefusedError(
                f"AdoptTenantApp cannot adopt app '{self.app_label}': it has no adoptable model. " + "; ".join(reasons)
            )
        raise AdoptionRefusedError(
            f"AdoptTenantApp cannot adopt app '{self.app_label}': it has no adoptable model. "
            f"Every concrete model is excluded, already tenant-scoped, or the app has none."
        )

    def _validate_table(self, cursor, model, pg_type: str):
        """Evaluate every per-table refusal, writing nothing.

        Returns an ``_AdoptionPlan`` for a table that will be adopted, or
        None for one already carrying a ``tenant_id`` column of the expected
        type, which is skipped unchanged (BR-RLS-013's per-table
        idempotency). Raises ``AdoptionRefusedError`` for any of the four
        per-table refusals BR-RLS-012, BR-RLS-014 and BR-RLS-016 define:
        a column of the wrong type, a deny-listed model, a populated table
        with no backfill tenant, and a unique form the rewrite cannot
        express.

        Everything here is a catalogue read, so calling it for the whole
        derived set before any statement is issued costs one extra pass over
        ``pg_attribute``, ``pg_constraint`` and ``pg_index`` and buys the
        guarantee that a refusal leaves the database untouched rather than
        rolled back.
        """
        from boundary import adoption
        from boundary.exceptions import AdoptionRefusedError

        table = model._meta.db_table
        label = adoption.model_label(model)

        # Per-table idempotency. A table already carrying a tenant_id column
        # of the expected type is skipped unchanged, so a second
        # AdoptTenantApp migration added after a package upgrade adopts only
        # the newly added models. One of a DIFFERENT type is refused, since
        # boundary cannot tell its own column from a column the adopted app
        # genuinely declares.
        existing = adoption.column_type(cursor, table, "tenant_id")
        if existing is not None:
            if _pg_types_match(existing, pg_type):
                return None
            raise AdoptionRefusedError(
                f"AdoptTenantApp cannot adopt table '{table}' (model '{label}'): it already "
                f"carries a tenant_id column of type '{existing}', but the configured tenant "
                f"model's primary key derives type '{pg_type}'. Boundary cannot tell its own "
                f"column from one the adopted app declares, so it refuses rather than guess."
            )

        # The per-model half of the deny-list. The app-level half already ran
        # once for the whole operation.
        model_refusal = adoption.refusal_reason_for_model(model)
        if model_refusal is not None:
            raise AdoptionRefusedError(f"AdoptTenantApp cannot adopt: {model_refusal}")

        # Probe the row count, and refuse a populated table with no backfill
        # tenant. PostgreSQL evaluates ADD COLUMN ... DEFAULT once for
        # existing rows, so the one-shot form would stamp every pre-existing
        # row with whichever tenant was current at migration time, which
        # during `migrate` is normally no tenant at all.
        existing_rows = adoption.row_count(cursor, table)
        if existing_rows and self.backfill_tenant is None:
            raise AdoptionRefusedError(
                f"AdoptTenantApp refuses to adopt table '{table}' (model '{label}'): it holds "
                f"{existing_rows} row(s) and no backfill_tenant was given. Adding the column "
                f"with its default would stamp every existing row with whichever tenant was "
                f"current at migration time, which during migrate is normally none. Pass "
                f"backfill_tenant=<tenant pk> to assign them deliberately, or exclude this "
                f"model."
            )

        # The unique forms the rewrite cannot express, refused before the
        # column is added rather than after (BR-RLS-012). The introspection is
        # unaffected by the pending ADD COLUMN: an added column carries no
        # unique constraint and no unique index of its own.
        rewrites = self._plan_unique_rewrites(cursor, table, label)

        return _AdoptionPlan(
            model=model,
            table=table,
            label=label,
            pg_type=pg_type,
            existing_rows=existing_rows,
            rewrites=rewrites,
        )

    def _adopt_table(self, schema_editor, plan) -> None:
        """Adopt one table, in exactly the DDL order BR-RLS-011 fixes.

        The helper function (step 1) is created once for the whole operation
        by :meth:`database_forwards`, and every refusal (steps 2 to 4, and
        the unique-form refusals inside step 6) has already been evaluated
        for every table in the derived set by :meth:`_validate_table`, so
        this method only writes.

        Step 6 (enabling RLS) must come after step 5 (the backfill). Under
        FORCE ROW LEVEL SECURITY with the isolation policy already in place,
        a migrating role that is not BYPASSRLS would match no rows and the
        backfill UPDATE would update zero rows with no error, leaving a
        table that then fails SET NOT NULL or, worse, passes it on rows the
        migrating role cannot see.
        """
        table = plan.table

        with schema_editor.connection.cursor() as cursor:
            # Step 5: add the column, backfilling when there are rows.
            self._add_tenant_column(
                schema_editor,
                cursor,
                table,
                plan.label,
                plan.pg_type,
                plan.existing_rows,
            )

        # Step 6: rewrite unique constraints to lead with tenant_id.
        self._apply_unique_rewrites(schema_editor, table, plan.rewrites)

        # Step 7: enable and force RLS, strictly after the backfill.
        schema_editor.execute(f'ALTER TABLE "{table}" ENABLE ROW LEVEL SECURITY')
        schema_editor.execute(f'ALTER TABLE "{table}" FORCE ROW LEVEL SECURITY')

        # Step 8: both policies, identical to a column-bearing table's.
        create_tenant_policies(schema_editor, table)

    def _add_tenant_column(self, schema_editor, cursor, table, label, pg_type, existing_rows):
        """Add ``tenant_id``, in the one-shot or three-step form (BR-RLS-014)."""
        from boundary.exceptions import AdoptionRefusedError

        if not existing_rows:
            # An empty table takes the one-shot form, which is equivalent on
            # zero rows and cheaper.
            schema_editor.execute(
                f'ALTER TABLE "{table}" ADD COLUMN tenant_id {pg_type} NOT NULL DEFAULT boundary_current_tenant_id()'
            )
            return

        schema_editor.execute(f'ALTER TABLE "{table}" ADD COLUMN tenant_id {pg_type} NULL')
        cursor.execute(f'UPDATE "{table}" SET tenant_id = %s', [self.backfill_tenant])
        updated = cursor.rowcount

        # The affected-row count must equal the count the refusal probe read.
        # A difference means either a concurrent write between the probe and
        # the update, or a partially applied earlier attempt, and in both
        # cases some rows' tenant assignment is not what the migration file
        # says it is. Proceeding would hit SET NOT NULL with an opaque
        # constraint error, or succeed with rows stamped by a default.
        if updated != existing_rows:
            raise AdoptionRefusedError(
                f"AdoptTenantApp aborted on table '{table}' (model '{label}'): the backfill UPDATE "
                f"affected {updated} row(s) but the row-count probe read {existing_rows}. Some "
                f"rows' tenant assignment would not be the one the migration declares, so the "
                f"operation refuses rather than proceed."
            )

        schema_editor.execute(f'ALTER TABLE "{table}" ALTER COLUMN tenant_id SET NOT NULL')
        schema_editor.execute(f'ALTER TABLE "{table}" ALTER COLUMN tenant_id SET DEFAULT boundary_current_tenant_id()')

    def _plan_unique_rewrites(self, cursor, table, label):
        """Return the ``(old_name, new_columns)`` rewrites for *table* (BR-RLS-012).

        The catalogue is introspected rather than the names being
        constructed, because Django generates the four unique sources into
        two different database forms under two different naming schemes: a
        field declared ``unique=True`` becomes an inline UNIQUE that
        PostgreSQL names ``"<table>_<column>_key"``, while
        ``Meta.unique_together`` and ``Meta.constraints`` become constraints
        the schema editor hashes. Constructing either name would miss the
        other.

        A form the operation cannot rewrite makes it refuse rather than skip.
        A silently skipped unique constraint stays globally unique, which is
        the exact cross-tenant collapse adoption exists to prevent. Every
        such refusal is raised here, in the read-only validation pass, so it
        reaches the consumer before any table has been altered.
        """
        from boundary import adoption
        from boundary.exceptions import AdoptionRefusedError

        # Refuse the index-only forms first, so an unrewritable one is
        # reported before any constraint has been dropped and recreated.
        for index in adoption.unique_indexes(cursor, table):
            if index["primary"] or index["constraint"] is not None:
                # The primary key is never touched, and an index backing a
                # constraint is handled through its constraint below.
                continue
            if index["partial"]:
                raise AdoptionRefusedError(
                    f"AdoptTenantApp cannot adopt table '{table}' (model '{label}'): unique index "
                    f"'{index['name']}' is partial (it carries a WHERE predicate), and boundary "
                    f"cannot safely re-express that predicate against the added tenant_id column. "
                    f"Exclude this model, or write the rewrite by hand in your own migration."
                )
            if index["expression"]:
                raise AdoptionRefusedError(
                    f"AdoptTenantApp cannot adopt table '{table}' (model '{label}'): unique index "
                    f"'{index['name']}' is an expression index, which boundary cannot rewrite. "
                    f"Exclude this model, or write the rewrite by hand in your own migration."
                )
            raise AdoptionRefusedError(
                f"AdoptTenantApp cannot adopt table '{table}' (model '{label}'): unique index "
                f"'{index['name']}' is a bare unique index backing no constraint, so replacing it "
                f"with a composite UNIQUE constraint would not be an equivalent rewrite. "
                f"Exclude this model, or write the rewrite by hand in your own migration."
            )

        rewrites = []
        for constraint in adoption.unique_constraints(cursor, table):
            name = constraint["name"]
            if constraint["deferrable"]:
                raise AdoptionRefusedError(
                    f"AdoptTenantApp cannot adopt table '{table}' (model '{label}'): unique "
                    f"constraint '{name}' is deferrable, and boundary cannot reproduce a "
                    f"deferrable composite replacement. Exclude this model, or write the rewrite "
                    f"by hand in your own migration."
                )
            if constraint["fk_targeted"]:
                raise AdoptionRefusedError(
                    f"AdoptTenantApp cannot adopt table '{table}' (model '{label}'): unique "
                    f"constraint '{name}' is targeted by a foreign key (a ForeignKey(to_field=...) "
                    f"elsewhere references it). Dropping it would invalidate that referencing "
                    f"constraint and the composite replacement cannot satisfy it. Exclude this "
                    f"model, or write the rewrite by hand in your own migration."
                )

            rewrites.append((name, ["tenant_id", *constraint["columns"]]))

        return rewrites

    def _apply_unique_rewrites(self, schema_editor, table, rewrites) -> None:
        """Drop each planned original and add its composite replacement.

        The replacement name is derived here rather than during planning
        because :func:`composite_constraint_name` falls through to the schema
        editor's own truncate-and-hash scheme, and the plan carries the
        original name it derives from instead, so the two halves cannot
        disagree about which constraint a name belongs to.
        """
        for name, columns in rewrites:
            new_name = composite_constraint_name(schema_editor, table, name, columns)
            quoted = ", ".join(f'"{column}"' for column in columns)
            schema_editor.execute(f'ALTER TABLE "{table}" DROP CONSTRAINT "{name}"')
            schema_editor.execute(f'ALTER TABLE "{table}" ADD CONSTRAINT "{new_name}" UNIQUE ({quoted})')

    # ── Backwards ────────────────────────────────────────────

    def database_backwards(self, app_label, schema_editor, from_state, to_state):
        """Undo adoption per table, in the order BR-RLS-020 fixes.

        Restoration re-derives rather than replays: each model's historical
        ``_meta`` is read from ``from_state.apps`` and the original unique
        constraints are recreated through the schema editor's own naming, so
        what is restored is what Django would generate for that model at
        that migration state rather than a definition snapshotted at
        adoption time. Nothing is stored in migration state or in the
        database to make reversal possible.

        ``boundary_current_tenant_id()`` is deliberately left in place, since
        other tables' policies may still depend on it, matching
        ``DropTenantPolicy``.

        The derived set the reverse iterates is not the set of tables this
        call actually altered: the forward skips a table an earlier migration
        had already adopted, and a reverse that has already run has left none
        of them adopted. So the reverse removes only what it can see, per
        table, and does nothing at all where there is nothing to see
        (BR-RLS-020).

        **This destroys the tenant assignment of every row on these tables**,
        because the assignment lives only in the dropped column and boundary
        snapshots nothing before dropping it.
        """
        from boundary import adoption

        models = adoption.adopted_models(self.app_label, apps=from_state.apps, exclude=self.exclude)

        # Both gates, in the forward's order (BR-RLS-013). A table the router
        # denies on this alias is skipped silently, and an alias holding none
        # of them is left entirely alone rather than refused on its vendor.
        models = self._router_allowed_models(schema_editor, models)
        if not models:
            return
        self._require_postgresql(schema_editor)

        for model in models:
            self._unadopt_table(schema_editor, model)

    def _unadopt_table(self, schema_editor, model) -> None:
        """Reverse one table's adoption, leaving the helper function alone."""
        from boundary import adoption

        table = model._meta.db_table

        with schema_editor.connection.cursor() as cursor:
            # A table with no tenant_id column was never adopted by this
            # operation (it was skipped, excluded, or the forward refused
            # before reaching it), so there is nothing to reverse.
            if adoption.column_type(cursor, table, "tenant_id") is None:
                return

        schema_editor.execute(f'DROP POLICY IF EXISTS boundary_tenant_isolation ON "{table}"')
        schema_editor.execute(f'DROP POLICY IF EXISTS boundary_admin_bypass ON "{table}"')
        schema_editor.execute(f'ALTER TABLE "{table}" DISABLE ROW LEVEL SECURITY')
        schema_editor.execute(f'ALTER TABLE "{table}" NO FORCE ROW LEVEL SECURITY')

        self._restore_unique_constraints(schema_editor, model, table)

        schema_editor.execute(f'ALTER TABLE "{table}" DROP COLUMN tenant_id')

    def _restore_unique_constraints(self, schema_editor, model, table) -> None:
        """Drop the composite constraints and recreate the originals (BR-RLS-020).

        The composite is found by the same deterministic ``_tenant`` name the
        forward derived, so no state needs carrying between the two
        directions; the originals are rebuilt from the historical ``_meta``
        through ``schema_editor._create_unique_sql(..., name=None)``, which
        is the schema editor's own naming.

        One consequence is visible and intended. A field declared
        ``unique=True`` was originally an inline UNIQUE that PostgreSQL named
        ``"<table>_<column>_key"``; the restored constraint instead carries
        the schema editor's hashed ``"<table>_<column>_<hash>_uniq"`` name,
        because that is the name Django's own schema editor generates for
        that model. BR-RLS-020 requires re-deriving rather than replaying, so
        the restored name is the derived one, and AC-RLS-013 asserts exactly
        that.

        **Deviation from BR-RLS-012's "reproducible from the original name
        alone".** The composite is located by introspecting the catalogue for
        a constraint leading with ``tenant_id`` on the historical column set,
        rather than by recomputing its ``_tenant`` name here. The forward
        derived that name from the name PostgreSQL had actually given the
        original, and for a ``unique=True`` field that is PostgreSQL's own
        ``"<table>_<column>_key"`` with PostgreSQL's own truncation and
        collision-suffix rules, which Django's schema editor does not
        generate and this operation therefore cannot recompute without
        reimplementing them. Matching on the column set is deterministic,
        carries no state between the directions, and cannot silently drop
        the wrong constraint: a composite unique on
        ``(tenant_id, <historical columns>)`` exists only because the forward
        created it.

        **An original is recreated only where a composite was actually found
        and dropped.** Recreating unconditionally would add a constraint the
        table never lost, which is a real defect rather than a cosmetic one:
        the forward refuses a unique form it cannot rewrite, so a model
        reaching the reverse may legitimately have had no composite for one of
        its historical column sets, and an unconditional CREATE would then
        either duplicate a constraint that is already there or invent one the
        schema never had. It also keeps the no-composite table untouched,
        which is what makes a second reverse a genuine no-op rather than a
        pass that quietly adds constraints (BR-RLS-020).
        """
        from boundary import adoption

        with schema_editor.connection.cursor() as cursor:
            live = adoption.unique_constraints(cursor, table)

        for columns in _historical_unique_column_sets(model):
            wanted = ["tenant_id", *[field.column for field in columns]]
            dropped = False
            for constraint in live:
                if constraint["columns"] == wanted:
                    schema_editor.execute(f'ALTER TABLE "{table}" DROP CONSTRAINT "{constraint["name"]}"')
                    dropped = True
                    break
            if not dropped:
                continue
            statement = schema_editor._create_unique_sql(model, columns)
            if statement is not None:
                schema_editor.execute(statement)

    def describe(self):
        return (
            f"Adopt every concrete table of '{self.app_label}' into tenant scope "
            f"(adds a tenant_id column, per-tenant unique constraints and RLS policies). "
            f"Reversing this operation drops the tenant_id column and therefore "
            f"destroys the tenant assignment of every row on those tables."
        )

    def deconstruct(self):
        kwargs = {"app_label": self.app_label}
        if self.exclude:
            kwargs["exclude"] = self.exclude
        if self.backfill_tenant is not None:
            kwargs["backfill_tenant"] = self.backfill_tenant
        return (
            self.__class__.__qualname__,
            [],
            kwargs,
        )
