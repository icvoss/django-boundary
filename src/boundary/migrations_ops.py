"""Custom migration operations for PostgreSQL Row Level Security.

These operations are included in auto-generated migrations for TenantModel
subclasses, or can be added manually by developers.
"""

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
    REPLACE FUNCTION`` is idempotent, so emitting it repeatedly (once per
    adopted table) is safe and removes any ordering dependency between the
    operations.

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


def detect_tenant_pg_type(pk_field) -> str:
    """Map a tenant model's primary-key field to its PostgreSQL cast type.

    ``uuid`` for a UUID primary key, ``bigint`` for any integer one, and
    ``bigint`` as the fallback for a type the map does not cover, which is
    the same unrecognised-type fallback ``rls-policy.v1`` has always carried.
    """
    from django.db import connection

    return _PG_TYPE_MAP.get(pk_field.db_type(connection), "bigint")


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
        table = model._meta.db_table
        schema_editor.execute(f'ALTER TABLE "{table}" ENABLE ROW LEVEL SECURITY')
        schema_editor.execute(f'ALTER TABLE "{table}" FORCE ROW LEVEL SECURITY')

    def database_backwards(self, app_label, schema_editor, from_state, to_state):
        model = from_state.apps.get_model(self.app_label or app_label, self.model_name)
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
        table = model._meta.db_table
        schema_editor.execute(f'DROP POLICY IF EXISTS boundary_tenant_isolation ON "{table}"')
        schema_editor.execute(f'DROP POLICY IF EXISTS boundary_admin_bypass ON "{table}"')

    def database_backwards(self, app_label, schema_editor, from_state, to_state):
        # Re-create policies on reverse. The override is passed through so the
        # reverse targets the same table the forward dropped from.
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
