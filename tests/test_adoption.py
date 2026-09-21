"""Acceptance tests for third-party app adoption (BR-RLS-010 to BR-RLS-020).

One test per Given/When/Then of AC-RLS-008, 009, 010, 011, 013, 014 and 016,
named after the acceptance criterion and carrying its text in the docstring.
AC-RLS-012 and AC-RLS-015 cover ``boundary.E007``, which is Phase C, and are
not implemented here.

Most tests apply the operation through ``connection.schema_editor()`` with
the ``FakeState`` pattern ``tests/test_rls.py`` established, because the
live app registry and a historical state agree for a model that has not
changed. AC-RLS-011 and AC-RLS-013 instead go through
``MigrationExecutor`` against the real migrations in ``boundary_consumer``,
because what they assert IS the migration machinery: a refusal leaving the
migration unapplied and the transaction rolled back, and a reversal
re-deriving constraints from ``from_state.apps``' historical ``_meta``.

The isolation assertions run through the ``app_conn`` fixture as the
non-superuser ``icv_app`` role. PostgreSQL exempts superuser and BYPASSRLS
roles from every policy even on a FORCE ROW LEVEL SECURITY table, so the
suite's default ``icv_test`` superuser would pass these against a broken
policy as readily as a correct one.
"""

import pytest
from django.db import IntegrityError, connection, transaction

from boundary.adoption import column_type, unique_constraints, unique_indexes
from boundary.exceptions import AdoptionRefusedError
from boundary.migrations_ops import AdoptTenantApp

# The models the ordinary adoption path covers. Seat, SeatBooking and Coupon
# are excluded because their unique forms are the refusal fixtures, asserted
# directly in TestAcRls010.
ORDINARY_EXCLUDE = (
    "thirdparty.Seat",
    "thirdparty.SeatBooking",
    "thirdparty.Coupon",
)


def _fake_state():
    """Stand the live app registry in for a migration state.

    The two agree for any model whose recorded fields match its class, which
    is every model here: nothing in these tests changes a model between
    states. AC-RLS-011 and AC-RLS-013, which need genuine historical state,
    use MigrationExecutor instead.
    """
    from django.apps import apps

    return type("FakeState", (), {"apps": apps})()


def _adopt(exclude=ORDINARY_EXCLUDE, backfill_tenant=None, app_label="thirdparty"):
    """Apply AdoptTenantApp forwards, from the consumer's app."""
    state = _fake_state()
    operation = AdoptTenantApp(app_label, exclude=exclude, backfill_tenant=backfill_tenant)
    with connection.schema_editor() as editor:
        operation.database_forwards("boundary_consumer", editor, state, state)
    return operation


def _unadopt(operation):
    """Reverse an applied AdoptTenantApp, for test teardown."""
    state = _fake_state()
    with connection.schema_editor() as editor:
        operation.database_backwards("boundary_consumer", editor, state, state)


def _rls_state(table):
    """Return (enabled, forced) for *table* from pg_class."""
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT relrowsecurity, relforcerowsecurity FROM pg_class WHERE oid = to_regclass(%s)::oid",
            [table],
        )
        row = cursor.fetchone()
        return (False, False) if row is None else (row[0], row[1])


def _policies(table):
    """Return the set of policy names on *table*."""
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT polname FROM pg_policy WHERE polrelid = to_regclass(%s)::oid",
            [table],
        )
        return {row[0] for row in cursor.fetchall()}


def _column_default(table, column):
    """Return the column DEFAULT expression, or None."""
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT pg_get_expr(d.adbin, d.adrelid), a.attnotnull
            FROM pg_attribute a
            LEFT JOIN pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum
            WHERE a.attrelid = to_regclass(%s)::oid AND a.attname = %s
            """,
            [table, column],
        )
        row = cursor.fetchone()
        return (None, None) if row is None else (row[0], row[1])


#: The consumer's real adoption migration, and the state before it. The test
#: database is BUILT with the adoption applied, because
#: boundary_consumer/0002 is an ordinary migration in the graph, which is
#: exactly the shape a consumer's deployment is in. Tests that need the
#: unadopted state reverse to PREVIOUS and restore afterwards.
ADOPTION_MIGRATION = ("boundary_consumer", "0002_adopt_thirdparty")
PRE_ADOPTION_MIGRATION = ("boundary_consumer", "0001_initial")


def _executor():
    """Return a MigrationExecutor with a freshly built graph.

    Rebuilt per call because applying or unapplying a migration changes the
    recorded state the executor plans from, and a stale graph would plan
    against the state before the previous call.
    """
    from django.db.migrations.executor import MigrationExecutor

    executor = MigrationExecutor(connection)
    executor.loader.build_graph()
    return executor


def _clear_thirdparty_rows():
    """Empty every adopted table, as the superuser so RLS does not hide rows.

    Needed because the transaction=True tests commit, and an adopted table's
    rows survive between tests unless they are removed deliberately.
    """
    with connection.cursor() as cursor:
        cursor.execute(
            'TRUNCATE "thirdparty_widget_tags", "thirdparty_widget", "thirdparty_tag", '
            '"thirdparty_gadget", "thirdparty_seatbooking", "thirdparty_seat", '
            '"thirdparty_coupon" CASCADE'
        )


@pytest.fixture
def adopted(db):
    """Guarantee the baseline adopted state, and leave it that way.

    Applies no DDL of its own in the ordinary case: the test database was
    built with boundary_consumer/0002 applied, so the state is already
    there. The migration is re-applied only if a previous test left it
    reversed, which keeps one test's teardown failure from cascading into
    every later test in the module.
    """
    if ADOPTION_MIGRATION not in _recorded_migrations():
        _executor().migrate([ADOPTION_MIGRATION])
    _clear_thirdparty_rows()
    yield
    _clear_thirdparty_rows()


@pytest.fixture
def unadopted(db):
    """Reverse the baseline adoption, restoring it on teardown.

    For the tests that assert what happens when adoption is APPLIED, which
    need the table to start without the column.
    """
    _clear_thirdparty_rows()
    _executor().migrate([PRE_ADOPTION_MIGRATION])
    yield
    _clear_thirdparty_rows()
    _executor().migrate([ADOPTION_MIGRATION])


def _recorded_migrations():
    """Return the set of applied (app_label, name) pairs from django_migrations."""
    from django.db.migrations.recorder import MigrationRecorder

    return set(MigrationRecorder(connection).applied_migrations())


@pytest.mark.django_db(transaction=True)
class TestAcRls008AdoptionAddsColumnRlsAndPolicies:
    """AC-RLS-008 (BR-RLS-010, BR-RLS-011, BR-RLS-013): AdoptTenantApp adds
    the column, RLS and policies to a third-party table.
    """

    def test_ac_rls_008_the_adopted_table_carries_the_column_rls_and_both_policies(self, adopted):
        """Given thirdparty.Widget is an empty concrete model with no boundary
        mixin, when a migration in the consumer's own app applies
        AdoptTenantApp("thirdparty"), then thirdparty_widget has a tenant_id
        column of the type the tenant model's primary key derives, NOT NULL
        with default boundary_current_tenant_id(), and pg_class shows RLS
        enabled and forced, and pg_policy holds both boundary_tenant_isolation
        and boundary_admin_bypass for it.
        """
        with connection.cursor() as cursor:
            assert column_type(cursor, "thirdparty_widget", "tenant_id") == "bigint"

        default, not_null = _column_default("thirdparty_widget", "tenant_id")
        assert not_null is True
        assert "boundary_current_tenant_id()" in default

        assert _rls_state("thirdparty_widget") == (True, True)
        assert _policies("thirdparty_widget") == {
            "boundary_tenant_isolation",
            "boundary_admin_bypass",
        }

    def test_ac_rls_008_the_auto_created_through_table_is_adopted_too(self, adopted):
        """And the adopted set is derived with include_auto_created=True, so
        the ManyToManyField's through table is adopted alongside the models
        the app declares (BR-RLS-010).
        """
        with connection.cursor() as cursor:
            assert column_type(cursor, "thirdparty_widget_tags", "tenant_id") == "bigint"
        assert _rls_state("thirdparty_widget_tags") == (True, True)
        assert _policies("thirdparty_widget_tags") == {
            "boundary_tenant_isolation",
            "boundary_admin_bypass",
        }

    def test_ac_rls_008_the_adopted_model_gains_no_django_field(self, adopted):
        """And Widget._meta.get_fields() contains no field named tenant, and
        the thirdparty app's own migration directory is unchanged.

        The column is deliberately invisible to the ORM: Django emits
        explicit column lists on INSERT and SELECT, so a column the model
        never declares is never named, which is what lets the adopted package
        stay passive and unaware (BR-RLS-011).
        """
        from thirdparty.models import Widget

        names = {field.name for field in Widget._meta.get_fields()}
        assert "tenant" not in names
        assert "tenant_id" not in names

        # The adopted app's own migration state records no tenant column
        # either: recording it would put it back into makemigrations'
        # field-level view of the third-party app, which is what BR-RLS-011
        # avoids.
        from django.db.migrations.loader import MigrationLoader

        loader = MigrationLoader(connection)
        state = loader.project_state(("thirdparty", "0001_initial"))
        historical = state.apps.get_model("thirdparty", "Widget")
        assert "tenant_id" not in {field.column for field in historical._meta.local_fields}

    def test_ac_rls_008_a_second_application_skips_adopted_tables_and_adopts_only_new_ones(self, unadopted):
        """When a second migration applies AdoptTenantApp("thirdparty") again
        after a package upgrade added thirdparty.Gadget, then it succeeds,
        thirdparty_widget is skipped unchanged, and thirdparty_gadget gains
        the column, RLS and both policies (BR-RLS-013).

        The upgrade is simulated by adopting everything except Gadget first,
        which is the state a consumer is in when an upstream package adds a
        model after their adoption migration was written.
        """
        first = _adopt(exclude=(*ORDINARY_EXCLUDE, "thirdparty.Gadget"))
        try:
            with connection.cursor() as cursor:
                assert column_type(cursor, "thirdparty_widget", "tenant_id") == "bigint"
                assert column_type(cursor, "thirdparty_gadget", "tenant_id") is None
            widget_constraints_before = unique_constraints_by_name("thirdparty_widget")

            second = _adopt()
            try:
                with connection.cursor() as cursor:
                    assert column_type(cursor, "thirdparty_gadget", "tenant_id") == "bigint"
                assert _rls_state("thirdparty_gadget") == (True, True)
                assert _policies("thirdparty_gadget") == {
                    "boundary_tenant_isolation",
                    "boundary_admin_bypass",
                }

                # Widget was skipped unchanged: no second column, and above
                # all no second rewrite of its unique constraints, which
                # would have produced a "_tenant_tenant" name.
                assert unique_constraints_by_name("thirdparty_widget") == widget_constraints_before
            finally:
                _unadopt(second)
        finally:
            _unadopt(first)

    def test_ac_rls_008_a_tenant_id_column_of_a_different_type_is_refused(self, unadopted):
        """And applying it to a table that already carries a tenant_id column
        of a different type fails with an error naming both types.

        Boundary cannot tell its own column from a column the adopted app
        genuinely declares, so it refuses rather than guess (BR-RLS-013).
        """
        with connection.cursor() as cursor:
            cursor.execute('ALTER TABLE "thirdparty_gadget" ADD COLUMN tenant_id uuid NULL')
        try:
            with pytest.raises(AdoptionRefusedError) as caught:
                _adopt()
            message = str(caught.value)
            assert "thirdparty_gadget" in message
            assert "uuid" in message
            assert "bigint" in message
        finally:
            with connection.cursor() as cursor:
                cursor.execute('ALTER TABLE "thirdparty_gadget" DROP COLUMN tenant_id')


def unique_constraints_by_name(table):
    """Return {name: columns} for every non-PK unique constraint on *table*."""
    with connection.cursor() as cursor:
        return {c["name"]: c["columns"] for c in unique_constraints(cursor, table)}


@pytest.mark.django_db(transaction=True)
class TestAcRls009AdoptedTableIsolatesThirdPartyOrmCode:
    """AC-RLS-009 (BR-RLS-011, BR-RLS-015): an adopted table isolates
    unmodified third-party ORM code.

    Every assertion here runs through app_conn as the non-superuser icv_app
    role, because the suite's default role bypasses RLS and would pass these
    against a broken policy as readily as a correct one.
    """

    def _insert(self, app_conn, tenant_pk, name, code):
        """Insert a widget the way the third-party app's ORM would: naming
        every column it declares and none it does not, so tenant_id reaches
        the row through the column DEFAULT alone.
        """
        with app_conn.cursor() as cursor:
            cursor.execute("BEGIN")
            cursor.execute("SELECT set_config('app.current_tenant_id', %s, true)", [str(tenant_pk)])
            cursor.execute(
                'INSERT INTO "thirdparty_widget" (name, code, owner, label) '
                "VALUES (%s, %s, 'o', %s) RETURNING tenant_id",
                [name, code, code],
            )
            stamped = cursor.fetchone()[0]
            cursor.execute("COMMIT")
        return stamped

    def _count(self, app_conn, tenant_pk):
        with app_conn.cursor() as cursor:
            cursor.execute("BEGIN")
            if tenant_pk is not None:
                cursor.execute("SELECT set_config('app.current_tenant_id', %s, true)", [str(tenant_pk)])
            cursor.execute('SELECT count(*) FROM "thirdparty_widget"')
            count = cursor.fetchone()[0]
            cursor.execute("COMMIT")
        return count

    def test_ac_rls_009_the_create_is_stamped_by_the_column_default(self, adopted, tenant_a, app_conn):
        """Given thirdparty.Widget has been adopted and tenant A is active,
        when Widget.objects.create(name="w") is called, then the create
        succeeds with tenant_id equal to tenant A's primary key, stamped by
        the column default rather than by any boundary ORM code.
        """
        stamped = self._insert(app_conn, tenant_a.pk, "w", "w")
        assert stamped == tenant_a.pk

    def test_ac_rls_009_the_query_under_another_tenant_returns_zero_rows(self, adopted, tenant_a, tenant_b, app_conn):
        """When tenant B is made active and Widget.objects.all() is
        evaluated, then the query under tenant B returns zero rows.
        """
        self._insert(app_conn, tenant_a.pk, "w", "w")
        assert self._count(app_conn, tenant_a.pk) == 1
        assert self._count(app_conn, tenant_b.pk) == 0

    def test_ac_rls_009_a_query_with_no_tenant_returns_zero_rows_and_raises_nothing(
        self, adopted, tenant_a, app_conn, settings
    ):
        """And re-running the same query with no tenant active returns zero
        rows and raises no TenantNotSetError, even with
        BOUNDARY_STRICT_MODE = True.

        An adopted table has no ORM layer, so STRICT_MODE has nothing to fire
        on: a missing tenant surfaces as an empty result, never as an
        exception (BR-RLS-015).
        """
        settings.BOUNDARY_STRICT_MODE = True
        self._insert(app_conn, tenant_a.pk, "w", "w")
        assert self._count(app_conn, None) == 0


@pytest.mark.django_db(transaction=True)
class TestAcRls010UniqueConstraintsBecomePerTenant:
    """AC-RLS-010 (BR-RLS-012): adoption rewrites unique constraints to
    composite per-tenant ones, and refuses what it cannot rewrite.
    """

    def _insert(self, app_conn, tenant_pk, code, owner="o", label=None):
        with app_conn.cursor() as cursor:
            cursor.execute("BEGIN")
            cursor.execute("SELECT set_config('app.current_tenant_id', %s, true)", [str(tenant_pk)])
            cursor.execute(
                'INSERT INTO "thirdparty_widget" (name, code, owner, label) VALUES (%s, %s, %s, %s)',
                ["w", code, owner, label if label is not None else code],
            )
            cursor.execute("COMMIT")

    def test_ac_rls_010_two_tenants_may_hold_the_same_unique_value(self, adopted, tenant_a, tenant_b, app_conn):
        """Given Widget declares code = CharField(unique=True) and
        Meta.unique_together = [("owner", "label")], and has been adopted,
        when tenant A creates a widget with code="X" and tenant B creates a
        widget with code="X", then both creates succeed.
        """
        self._insert(app_conn, tenant_a.pk, "X")
        self._insert(app_conn, tenant_b.pk, "X")

        # Both rows exist, which is what "both creates succeed" means. Counted
        # through the Django superuser connection, which bypasses RLS, because
        # the point here is that the WRITES succeeded, not what either tenant
        # can see; the visibility half is AC-RLS-009's.
        with connection.cursor() as cursor:
            cursor.execute(
                'SELECT tenant_id FROM "thirdparty_widget" WHERE code = %s ORDER BY tenant_id',
                ["X"],
            )
            assert cursor.fetchall() == sorted([(tenant_a.pk,), (tenant_b.pk,)])

    def test_ac_rls_010_a_duplicate_within_one_tenant_still_raises(self, adopted, tenant_a, app_conn):
        """And a second code="X" create under tenant A raises IntegrityError.

        Uniqueness became per-tenant, not absent: this is what makes the
        rewrite a rewrite rather than a removal.
        """
        import psycopg

        self._insert(app_conn, tenant_a.pk, "X")
        with pytest.raises(psycopg.errors.UniqueViolation):
            self._insert(app_conn, tenant_a.pk, "X", owner="other", label="other")
        app_conn.rollback()

    def test_ac_rls_010_every_unique_index_leads_with_tenant_id(self, adopted):
        """And pg_index shows no unique index on that table whose leading
        column is anything other than tenant_id, excluding the primary key
        index.
        """
        with connection.cursor() as cursor:
            indexes = unique_indexes(cursor, "thirdparty_widget")

        non_primary = [index for index in indexes if not index["primary"]]
        assert non_primary, "the rewrite must leave the unique indexes in place, not drop them"
        for index in non_primary:
            assert index["columns"][0] == "tenant_id", index

    def test_ac_rls_010_each_replacement_is_a_constraint_named_from_the_original(self, adopted):
        """And each replacement appears in pg_constraint with contype = 'u'
        under the original name plus _tenant, so it is a constraint rather
        than a bare index.

        The two originals exercise both of Django's naming schemes: the
        unique=True field is an inline UNIQUE PostgreSQL named
        thirdparty_widget_code_key, while unique_together is a constraint the
        schema editor hashed. Only catalogue introspection finds both.
        """
        names = unique_constraints_by_name("thirdparty_widget")

        assert names["thirdparty_widget_code_key_tenant"] == ["tenant_id", "code"]

        together = [
            name
            for name in names
            if name.startswith("thirdparty_widget_owner_label_") and name.endswith("_uniq_tenant")
        ]
        assert len(together) == 1, names
        assert names[together[0]] == ["tenant_id", "owner", "label"]

    def test_ac_rls_010_an_fk_targeted_unique_constraint_is_refused(self, unadopted):
        """When the same operation is applied to a model whose unique
        constraint is targeted by a ForeignKey(to_field=...) from another
        table, then it fails with an error naming the constraint and the
        model.
        """
        with pytest.raises(AdoptionRefusedError) as caught:
            _adopt(exclude=("thirdparty.Coupon",))
        message = str(caught.value)
        assert "thirdparty.Seat" in message
        assert "thirdparty_seat_code_key" in message
        assert "foreign key" in message.lower()

    def test_ac_rls_010_a_conditional_unique_constraint_is_refused(self, unadopted):
        """And separately, to one declaring a UniqueConstraint with a
        condition, then it fails with an error naming the index and the
        model.

        A conditional UniqueConstraint becomes a PARTIAL unique index rather
        than a constraint, and boundary cannot safely re-express its
        predicate against the added column.
        """
        with pytest.raises(AdoptionRefusedError) as caught:
            _adopt(exclude=("thirdparty.Seat", "thirdparty.SeatBooking"))
        message = str(caught.value)
        assert "thirdparty.Coupon" in message
        assert "thirdparty_coupon_active_code_uniq" in message
        assert "partial" in message.lower()

    def test_ac_rls_010_a_deferrable_unique_constraint_is_refused(self, unadopted):
        """And separately, to one with a deferrable constraint, then it fails
        with an error naming the constraint and the model.

        Django has no Meta form that produces one, so the deferrable
        constraint is created directly on the adopted app's table, which is
        exactly how a third-party package's own hand-written migration would
        introduce one.
        """
        with connection.cursor() as cursor:
            cursor.execute(
                'ALTER TABLE "thirdparty_gadget" ADD CONSTRAINT thirdparty_gadget_name_def UNIQUE (name) DEFERRABLE'
            )
        try:
            with pytest.raises(AdoptionRefusedError) as caught:
                _adopt()
            message = str(caught.value)
            assert "thirdparty.Gadget" in message
            assert "thirdparty_gadget_name_def" in message
            assert "deferrable" in message.lower()
        finally:
            with connection.cursor() as cursor:
                cursor.execute('ALTER TABLE "thirdparty_gadget" DROP CONSTRAINT thirdparty_gadget_name_def')

    def test_ac_rls_010_an_expression_unique_index_is_refused(self, unadopted):
        """And separately, to one with an expression index, then it fails
        with an error naming the index and the model.
        """
        with connection.cursor() as cursor:
            cursor.execute('CREATE UNIQUE INDEX thirdparty_gadget_lower_name ON "thirdparty_gadget" (lower(name))')
        try:
            with pytest.raises(AdoptionRefusedError) as caught:
                _adopt()
            message = str(caught.value)
            assert "thirdparty.Gadget" in message
            assert "thirdparty_gadget_lower_name" in message
            assert "expression" in message.lower()
        finally:
            with connection.cursor() as cursor:
                cursor.execute("DROP INDEX thirdparty_gadget_lower_name")

    def test_ac_rls_010_a_refusal_leaves_no_table_partially_adopted(self, unadopted):
        """And no table the operation touched retains any partial adoption.

        The operation takes Django's default atomic = True, and PostgreSQL's
        DDL is transactional, so a refusal on any one table rolls back every
        table the same call had already altered (BR-RLS-013). The refusal is
        raised inside an atomic block here, because a bare schema_editor()
        outside one would have committed each statement as it ran, which is
        the migration runner's job to provide rather than the operation's.
        """
        with pytest.raises(AdoptionRefusedError), transaction.atomic():
            _adopt(exclude=("thirdparty.Coupon",))

        with connection.cursor() as cursor:
            for table in (
                "thirdparty_widget",
                "thirdparty_widget_tags",
                "thirdparty_tag",
                "thirdparty_gadget",
                "thirdparty_seat",
            ):
                assert column_type(cursor, table, "tenant_id") is None, table


@pytest.mark.django_db(transaction=True)
class TestAcRls011PopulatedTableIsRefusedWithoutBackfill:
    """AC-RLS-011 (BR-RLS-014): a populated table is refused without a
    backfill tenant.

    Driven through MigrationExecutor rather than a bare schema_editor,
    because what this criterion asserts IS the migration machinery: the
    migration must be left unapplied in django_migrations and its
    transaction rolled back, which a direct operation call cannot show.
    """

    @pytest.fixture(autouse=True)
    def _populated(self, unadopted):
        """Put three rows on the unadopted table, so the forward meets a
        populated one.

        Three rows rather than one, so the refusal's count and the backfill's
        affected-row equality assertion each name a number that could only
        have come from a real probe.
        """
        with connection.cursor() as cursor:
            for index in range(3):
                cursor.execute(
                    'INSERT INTO "thirdparty_widget" (name, code, owner, label) VALUES (%s, %s, %s, %s)',
                    [f"w{index}", f"c{index}", "o", f"l{index}"],
                )
        yield

    def test_ac_rls_011_the_migration_fails_naming_the_model_count_and_argument(self):
        """Given thirdparty_widget already holds three rows and no tenant
        column, when a migration applies AdoptTenantApp("thirdparty") with no
        backfill_tenant, then the migration fails with an error naming
        thirdparty.Widget, its row count, and the backfill_tenant argument.
        """
        with pytest.raises(AdoptionRefusedError) as caught:
            _executor().migrate([ADOPTION_MIGRATION])
        message = str(caught.value)
        assert "thirdparty_widget" in message
        assert "thirdparty.Widget" in message
        assert "3 row" in message
        assert "backfill_tenant" in message

    def test_ac_rls_011_the_refused_migration_leaves_no_trace(self):
        """And the table still has no tenant_id column, the migration is
        recorded as unapplied in django_migrations, and the transaction was
        rolled back, so no other table the same operation would have adopted
        carries any partial state (BR-RLS-013).
        """
        with pytest.raises(AdoptionRefusedError):
            _executor().migrate([ADOPTION_MIGRATION])

        assert ADOPTION_MIGRATION not in _recorded_migrations()

        with connection.cursor() as cursor:
            for table in (
                "thirdparty_widget",
                "thirdparty_widget_tags",
                "thirdparty_tag",
                "thirdparty_gadget",
            ):
                assert column_type(cursor, table, "tenant_id") is None, table

    def test_ac_rls_011_the_same_migration_succeeds_with_a_backfill_tenant(self, tenant_a):
        """When the migration is re-run as AdoptTenantApp("thirdparty",
        backfill_tenant=<tenant A pk>), then it succeeds, all three
        pre-existing rows carry tenant A's primary key, and the column is NOT
        NULL with the boundary_current_tenant_id() default, and no row on the
        table has a NULL tenant_id (BR-RLS-014).

        The migration file itself is not edited; the operation is applied
        with the backfill argument the consumer would have written into it,
        which is the same code path the migration runner takes.
        """
        operation = _adopt(backfill_tenant=tenant_a.pk)
        try:
            with connection.cursor() as cursor:
                assert column_type(cursor, "thirdparty_widget", "tenant_id") == "bigint"
                cursor.execute('SELECT DISTINCT tenant_id FROM "thirdparty_widget"')
                assert cursor.fetchall() == [(tenant_a.pk,)]
                cursor.execute('SELECT count(*) FROM "thirdparty_widget" WHERE tenant_id IS NULL')
                assert cursor.fetchone()[0] == 0

            default, not_null = _column_default("thirdparty_widget", "tenant_id")
            assert not_null is True
            assert "boundary_current_tenant_id()" in default
        finally:
            _unadopt(operation)


@pytest.mark.django_db(transaction=True)
class TestAcRls013AdoptionReverses:
    """AC-RLS-013 (BR-RLS-013, BR-RLS-020): AdoptTenantApp reverses to the
    original schema.

    Driven through MigrationExecutor, because BR-RLS-020's re-derivation
    reads from_state.apps' historical _meta, and only a real reversal
    supplies a real from_state.
    """

    @pytest.fixture
    def reversed_adoption(self, unadopted):
        """Name the shared unadopted fixture in this criterion's own terms.

        The test database is built with boundary_consumer/0002 applied, so
        ``unadopted`` reverses a genuinely applied migration through
        MigrationExecutor rather than setting up an artificial state, which
        is what supplies the real ``from_state`` BR-RLS-020's re-derivation
        reads.
        """
        yield

    def test_ac_rls_013_the_policies_rls_and_column_are_gone(self, reversed_adoption):
        """Given thirdparty.Widget has been adopted by a migration in the
        consumer's app, when that migration is reversed with
        migrate <app> <previous>, then both policies are gone, RLS is
        disabled and un-forced, and the tenant_id column is dropped.
        """
        assert _policies("thirdparty_widget") == set()
        assert _rls_state("thirdparty_widget") == (False, False)
        with connection.cursor() as cursor:
            assert column_type(cursor, "thirdparty_widget", "tenant_id") is None

    def test_ac_rls_013_the_unique_constraint_is_global_again_under_a_derived_name(self, reversed_adoption):
        """And the unique constraint on code is global again, carrying the
        name Django's own schema editor generates for it rather than a name
        recorded at adoption time.

        The original inline UNIQUE was named thirdparty_widget_code_key by
        PostgreSQL, not by Django. BR-RLS-020 requires re-deriving rather
        than replaying, so the restored constraint carries the schema
        editor's own hashed <table>_<column>_<hash>_uniq name. That the name
        CHANGED is the evidence of re-derivation: a replayed definition would
        have restored the _key name.
        """
        names = unique_constraints_by_name("thirdparty_widget")

        code_constraints = {name: columns for name, columns in names.items() if columns == ["code"]}
        assert len(code_constraints) == 1, names
        restored = next(iter(code_constraints))
        assert restored.startswith("thirdparty_widget_code_")
        assert restored.endswith("_uniq")
        assert restored != "thirdparty_widget_code_key"

        # No composite survives the reverse.
        for columns in names.values():
            assert "tenant_id" not in columns, names

    def test_ac_rls_013_the_helper_function_survives_the_reverse(self, reversed_adoption):
        """And boundary_current_tenant_id() still exists in the database.

        Other tables' policies may still depend on it, so the reverse leaves
        it alone, matching DropTenantPolicy's existing behaviour.
        """
        with connection.cursor() as cursor:
            cursor.execute("SELECT count(*) FROM pg_proc WHERE proname = 'boundary_current_tenant_id'")
            assert cursor.fetchone()[0] == 1

    def test_ac_rls_013_deconstruct_round_trips_all_three_arguments(self):
        """And AdoptTenantApp("thirdparty", exclude=("thirdparty.Tag",),
        backfill_tenant=<pk>).deconstruct() round-trips all three arguments,
        while AdoptTenantApp("thirdparty").deconstruct() emits only
        app_label.
        """
        name, args, kwargs = AdoptTenantApp("thirdparty", exclude=("thirdparty.Tag",), backfill_tenant=7).deconstruct()
        assert name == "AdoptTenantApp"
        assert args == []
        assert kwargs == {
            "app_label": "thirdparty",
            "exclude": ("thirdparty.Tag",),
            "backfill_tenant": 7,
        }

        rebuilt = AdoptTenantApp(**kwargs)
        assert rebuilt.deconstruct() == (name, args, kwargs)

        _, _, plain = AdoptTenantApp("thirdparty").deconstruct()
        assert plain == {"app_label": "thirdparty"}

    def test_ac_rls_013_describe_states_that_reversal_loses_tenant_assignment(self):
        """BR-RLS-020 requires describe() to state that reversing destroys
        the tenant assignment of every row, because the assignment lives only
        in the dropped column and boundary snapshots nothing before dropping
        it.
        """
        described = AdoptTenantApp("thirdparty").describe()
        assert "thirdparty" in described
        assert "tenant assignment" in described
        assert "Revers" in described


@pytest.mark.django_db
class TestAcRls014AdoptedModelIsNotATenantModelToTheOrm:
    """AC-RLS-014 (BR-RLS-015): an adopted model is not a tenant model to the
    ORM helpers.

    Adoption is not a fourth answer from these helpers. It registers nothing
    in _tenant_model_registry and adds no Django field, so the helpers
    continue to answer for an adopted model exactly as they did before it was
    adopted, which is what makes the RLS-only asymmetry permanent.
    """

    def test_ac_rls_014_the_tenant_helpers_all_answer_negative(self, adopted):
        """Given thirdparty.Widget has been adopted, when is_tenant_model,
        has_tenant_column, get_tenant_lookup and get_tenant_fk_field are
        called, then they return False, False, None and None respectively.
        """
        from thirdparty.models import Widget

        from boundary.models import (
            get_tenant_fk_field,
            get_tenant_lookup,
            has_tenant_column,
            is_tenant_model,
        )

        assert is_tenant_model(Widget) is False
        assert has_tenant_column(Widget) is False
        assert get_tenant_lookup(Widget) is None
        assert get_tenant_fk_field(Widget) is None

    def test_ac_rls_014_the_managers_are_the_third_party_apps_own(self, adopted):
        """And Widget has no unscoped manager and Widget.objects is the
        third-party app's own manager, unreplaced.
        """
        from django.db.models import Manager
        from thirdparty.models import Widget

        from boundary.models import TenantManager

        assert not hasattr(Widget, "unscoped")
        assert type(Widget.objects) is Manager
        assert not isinstance(Widget.objects, TenantManager)

    def test_ac_rls_014_cross_tenant_fk_validation_raises_nothing(self, adopted, tenant_a):
        """And validate_cross_tenant_fks(widget_instance) raises nothing.

        It walks declared ForeignKey fields and compares against a declared
        local tenant column; an adopted model has neither, so there is
        nothing for it to inspect (BR-ORM-013 is unchanged).
        """
        from thirdparty.models import Widget

        from boundary.models import validate_cross_tenant_fks
        from boundary.testing import set_tenant

        with set_tenant(tenant_a):
            widget = Widget.objects.create(name="w", code="c", owner="o", label="l")
            validate_cross_tenant_fks(widget)

    def test_ac_rls_014_the_positive_control_still_answers_yes(self):
        """A control: the same four helpers answer positively for a genuinely
        column-bearing model, so the assertions above pin adoption's
        asymmetry rather than four helpers that answer negative for
        everything.
        """
        from boundary_testapp.models import Booking

        from boundary.models import (
            get_tenant_fk_field,
            get_tenant_lookup,
            has_tenant_column,
            is_tenant_model,
        )

        assert is_tenant_model(Booking) is True
        assert has_tenant_column(Booking) is True
        assert get_tenant_lookup(Booking) == "tenant"
        assert get_tenant_fk_field(Booking) == "tenant"


@pytest.mark.django_db(transaction=True)
class TestAcRls016WriteWithNoTenantFailsClosed:
    """AC-RLS-016 (BR-RLS-018): a write to an adopted table with no tenant
    fails closed, and a tenant context is what resolves it.

    The point of the criterion is the discrimination between the two
    remedies. admin_bypass() changes which rows the boundary_admin_bypass
    policy admits; it has no bearing on what boundary_current_tenant_id()
    returns, and the NOT NULL constraint is evaluated before any policy is
    consulted. So the bypass does not fix a write, and TenantContext does.
    """

    def test_ac_rls_016_a_write_with_no_tenant_raises_integrity_error(self, adopted):
        """Given thirdparty.Widget has been adopted and no tenant is active,
        when Widget.objects.create(name="w") is called, then IntegrityError
        is raised on the tenant_id NOT NULL constraint and no row is written.
        """
        from thirdparty.models import Widget

        with pytest.raises(IntegrityError) as caught, transaction.atomic():
            Widget.objects.create(name="w", code="c", owner="o", label="l")
        assert "tenant_id" in str(caught.value)

        with connection.cursor() as cursor:
            cursor.execute('SELECT count(*) FROM "thirdparty_widget"')
            assert cursor.fetchone()[0] == 0

    def test_ac_rls_016_a_tenant_context_resolves_it(self, adopted, tenant_a):
        """When the same create is run inside
        with TenantContext.using(tenant_a):, then it succeeds and the row
        carries tenant A's primary key, stamped by the column default.
        """
        from thirdparty.models import Widget

        from boundary.context import TenantContext

        with TenantContext.using(tenant_a):
            Widget.objects.create(name="w", code="c", owner="o", label="l")

        with connection.cursor() as cursor:
            cursor.execute('SELECT tenant_id FROM "thirdparty_widget"')
            assert cursor.fetchall() == [(tenant_a.pk,)]

    def test_ac_rls_016_admin_bypass_does_not_resolve_a_write(self, adopted):
        """When the same create is run inside with admin_bypass(): with no
        tenant active, then IntegrityError is raised again on the same NOT
        NULL constraint, proving the bypass flag has no bearing on the column
        default.

        This is the discriminating half: without it, the test above would
        pass equally if the column were simply nullable.
        """
        from thirdparty.models import Widget

        from boundary.context import admin_bypass

        with pytest.raises(IntegrityError) as caught, transaction.atomic(), admin_bypass():
            Widget.objects.create(name="w", code="c", owner="o", label="l")
        assert "tenant_id" in str(caught.value)

    def test_ac_rls_016_a_raw_insert_naming_tenant_id_succeeds_under_the_bypass(self, adopted, tenant_a, app_conn):
        """When a raw INSERT naming tenant_id explicitly is run inside
        with admin_bypass():, then the row is written and is visible to that
        tenant afterwards.

        Raw SQL that supplies tenant_id explicitly is one of the two cases
        admin_bypass() remains the remedy for, and is what
        boundary_deprovision does (BR-PRV-009).

        Run through app_conn as the non-superuser role, so the visibility
        half is a real RLS read rather than a bypassing one.
        """
        with app_conn.cursor() as cursor:
            cursor.execute("BEGIN")
            cursor.execute("SELECT set_config('app.boundary_admin', 'true', true)")
            cursor.execute(
                'INSERT INTO "thirdparty_widget" (name, code, owner, label, tenant_id) '
                "VALUES ('w', 'c', 'o', 'l', %s)",
                [tenant_a.pk],
            )
            cursor.execute("COMMIT")

            cursor.execute("BEGIN")
            cursor.execute("SELECT set_config('app.current_tenant_id', %s, true)", [str(tenant_a.pk)])
            cursor.execute('SELECT count(*) FROM "thirdparty_widget"')
            visible = cursor.fetchone()[0]
            cursor.execute("COMMIT")

        assert visible == 1
