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


class EnableRLS(migrations.operations.base.Operation):
    """Enable Row Level Security on a table.

    Reversible: disables RLS on the table.
    """

    reduces_to_sql = True
    reversible = True

    def __init__(self, model_name: str):
        self.model_name = model_name

    def state_forwards(self, app_label, state):
        pass

    def database_forwards(self, app_label, schema_editor, from_state, to_state):
        model = to_state.apps.get_model(app_label, self.model_name)
        table = model._meta.db_table
        schema_editor.execute(f'ALTER TABLE "{table}" ENABLE ROW LEVEL SECURITY')
        schema_editor.execute(f'ALTER TABLE "{table}" FORCE ROW LEVEL SECURITY')

    def database_backwards(self, app_label, schema_editor, from_state, to_state):
        model = from_state.apps.get_model(app_label, self.model_name)
        table = model._meta.db_table
        # Drop all boundary policies first
        schema_editor.execute(f'DROP POLICY IF EXISTS boundary_tenant_isolation ON "{table}"')
        schema_editor.execute(f'DROP POLICY IF EXISTS boundary_admin_bypass ON "{table}"')
        schema_editor.execute(f'ALTER TABLE "{table}" DISABLE ROW LEVEL SECURITY')
        schema_editor.execute(f'ALTER TABLE "{table}" NO FORCE ROW LEVEL SECURITY')

    def describe(self):
        return f"Enable Row Level Security on {self.model_name}"

    def deconstruct(self):
        return (
            self.__class__.__qualname__,
            [],
            {"model_name": self.model_name},
        )


class CreateTenantPolicy(migrations.operations.base.Operation):
    """Create tenant isolation and admin bypass RLS policies.

    Must be applied after EnableRLS. Creates the boundary_current_tenant_id()
    helper function if it does not already exist. The function is declared
    LEAKPROOF only when BOUNDARY_FUNCTION_LEAKPROOF is set (default off), since
    LEAKPROOF requires a superuser and managed Postgres grants none.

    Reversible: drops both policies.
    """

    reduces_to_sql = True
    reversible = True

    def __init__(self, model_name: str, tenant_column: str | None = None):
        self.model_name = model_name
        self.tenant_column = tenant_column or "tenant_id"

    def state_forwards(self, app_label, state):
        pass

    def database_forwards(self, app_label, schema_editor, from_state, to_state):
        model = to_state.apps.get_model(app_label, self.model_name)
        table = model._meta.db_table

        # Detect tenant column's database type for the helper function
        pg_type = self._detect_tenant_column_type(model)

        # Honour the configurable session-variable names so the database
        # policies test the same variables the ORM/context layer sets. These
        # default to "app.current_tenant_id" / "app.boundary_admin" but break
        # isolation silently if hardcoded when a project customises them.
        session_var = _sql_string_literal(boundary_settings.DB_SESSION_VAR)
        admin_flag = _sql_string_literal(boundary_settings.ADMIN_FLAG_VAR)

        # LEAKPROOF is a planner optimisation, not an isolation requirement, and
        # only a superuser may declare it. Managed Postgres (DigitalOcean, RDS,
        # Cloud SQL, Azure, Heroku, Supabase) grants no superuser role, so the
        # migration would abort with "only superuser can define a leakproof
        # function". Default off; opt in via BOUNDARY_FUNCTION_LEAKPROOF on a
        # self-managed cluster where the migrating role is a superuser.
        leakproof = " LEAKPROOF" if boundary_settings.FUNCTION_LEAKPROOF else ""

        # Create the helper function (idempotent via CREATE OR REPLACE). Uses the
        # detected column type so it works with both UUID and integer PKs.
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

        # Isolation policy with WITH CHECK for INSERT enforcement (BR-RLS-008)
        schema_editor.execute(
            f'CREATE POLICY boundary_tenant_isolation ON "{table}" '
            f"USING ({self.tenant_column} = boundary_current_tenant_id()) "
            f"WITH CHECK ({self.tenant_column} = boundary_current_tenant_id())"
        )

        # Admin bypass policy
        schema_editor.execute(
            f"CREATE POLICY boundary_admin_bypass ON \"{table}\" USING (current_setting({admin_flag}, TRUE) = 'true')"
        )

    def _detect_tenant_column_type(self, model):
        """Detect the PostgreSQL type of the tenant FK's target PK."""
        from django.db import connection

        field_name = self.tenant_column.removesuffix("_id")
        tenant_field = model._meta.get_field(field_name)
        target_pk = tenant_field.related_model._meta.pk
        db_type = target_pk.db_type(connection)
        # Map Django db types to PostgreSQL cast types
        type_map = {
            "uuid": "uuid",
            "integer": "bigint",
            "bigint": "bigint",
            "serial": "bigint",
            "bigserial": "bigint",
        }
        return type_map.get(db_type, "bigint")

    def database_backwards(self, app_label, schema_editor, from_state, to_state):
        model = from_state.apps.get_model(app_label, self.model_name)
        table = model._meta.db_table
        schema_editor.execute(f'DROP POLICY IF EXISTS boundary_tenant_isolation ON "{table}"')
        schema_editor.execute(f'DROP POLICY IF EXISTS boundary_admin_bypass ON "{table}"')

    def describe(self):
        return f"Create tenant RLS policies on {self.model_name}"

    def deconstruct(self):
        kwargs = {"model_name": self.model_name}
        if self.tenant_column != "tenant_id":
            kwargs["tenant_column"] = self.tenant_column
        return (
            self.__class__.__qualname__,
            [],
            kwargs,
        )


class DropTenantPolicy(migrations.operations.base.Operation):
    """Drop boundary RLS policies from a table.

    Reversible: re-creates the policies.
    """

    reduces_to_sql = True
    reversible = True

    def __init__(self, model_name: str, tenant_column: str | None = None):
        self.model_name = model_name
        self.tenant_column = tenant_column or "tenant_id"

    def state_forwards(self, app_label, state):
        pass

    def database_forwards(self, app_label, schema_editor, from_state, to_state):
        model = to_state.apps.get_model(app_label, self.model_name)
        table = model._meta.db_table
        schema_editor.execute(f'DROP POLICY IF EXISTS boundary_tenant_isolation ON "{table}"')
        schema_editor.execute(f'DROP POLICY IF EXISTS boundary_admin_bypass ON "{table}"')

    def database_backwards(self, app_label, schema_editor, from_state, to_state):
        # Re-create policies on reverse
        create_op = CreateTenantPolicy(self.model_name, self.tenant_column)
        create_op.database_forwards(app_label, schema_editor, from_state, to_state)

    def describe(self):
        return f"Drop tenant RLS policies from {self.model_name}"

    def deconstruct(self):
        kwargs = {"model_name": self.model_name}
        if self.tenant_column != "tenant_id":
            kwargs["tenant_column"] = self.tenant_column
        return (
            self.__class__.__qualname__,
            [],
            kwargs,
        )
