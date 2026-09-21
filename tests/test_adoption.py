"""Acceptance tests for third-party app adoption (BR-RLS-010 to BR-RLS-020).

One test per Given/When/Then of AC-RLS-008 to 016, named after the
acceptance criterion and carrying its text in the docstring. AC-RLS-012 and
AC-RLS-015 cover ``boundary.E007``, and sit at the end of the module behind
their own section marker, because they are the only tests here driven by
``BOUNDARY_TENANT_APPS`` rather than by the operation directly.

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


#: The adopted tables whose tenant_id column the E007 damage tests drop.
_ADOPTED_TABLES = (
    "thirdparty_widget",
    "thirdparty_widget_tags",
    "thirdparty_tag",
    "thirdparty_gadget",
)


def _restore_after_dropped_column():
    """Restore the baseline adopted state after a DROP COLUMN tenant_id.

    Reversing and re-applying the migration is not enough on its own.
    ``AdoptTenantApp._unadopt_table`` skips a table with no tenant_id column,
    and that early return is correct: from the operation's side a
    column-less table was never adopted, so there is nothing to reverse.
    Dropping the column by hand therefore puts the table in a state no
    forward path produces, and the reversal steps straight over it. Two
    things are left stranded, and both cascade into every later test in the
    module:

    * the RLS flags and both policies survive, so the re-apply dies on
      ``policy "boundary_admin_bypass" ... already exists``;
    * ``DROP COLUMN ... CASCADE`` took the composite unique constraints with
      it, and since ``_restore_unique_constraints`` is behind the same early
      return, the originals are never rebuilt. The re-apply then finds no
      unique constraint to rewrite and creates none, leaving the table
      permanently without them.

    Putting the column back before reversing is what avoids both. The
    reversal then takes its full real path over a table it recognises as
    adopted: it drops the policies, clears the flags, and rebuilds the
    original unique constraints through the operation's own code, so the
    re-apply rewrites them exactly as the baseline has them. The restore
    leans on the production path rather than reimplementing it.
    """
    with connection.cursor() as cursor:
        for table in _ADOPTED_TABLES:
            if column_type(cursor, table, "tenant_id") is not None:
                continue
            # Nullable and defaultless: this column exists only so the
            # reversal recognises the table, and the reversal drops it again
            # a moment later. The forward re-apply is what builds the real
            # NOT NULL column with its boundary_current_tenant_id() default.
            cursor.execute(f'ALTER TABLE "{table}" ADD COLUMN tenant_id bigint')

    _executor().migrate([PRE_ADOPTION_MIGRATION])
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

        The unique=True field is matched by prefix and suffix rather than by
        the one literal ``thirdparty_widget_code_key_tenant``, because either
        of two original names is legitimate here and which one is present
        depends on whether this table has been through a reversal. Reversal
        re-derives the original through the schema editor's own naming
        (BR-RLS-020), so PostgreSQL's inline
        ``thirdparty_widget_code_key`` becomes Django's hashed
        ``thirdparty_widget_code_<hash>_uniq`` and never reverts; a later
        re-application derives its composite from that. Both satisfy the rule
        this test states, which is that the composite carries the original's
        name plus ``_tenant``, so pinning one spelling pins reversal history
        rather than the naming derivation.
        """
        names = unique_constraints_by_name("thirdparty_widget")

        code = [name for name in names if name.startswith("thirdparty_widget_code_") and name.endswith("_tenant")]
        assert len(code) == 1, names
        assert names[code[0]] == ["tenant_id", "code"]

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

    def test_ac_rls_010_a_refusal_in_the_set_issues_no_ddl_for_any_table(self, unadopted):
        """And a derived set holding one refusable table and several clean
        ones issues no DDL at all, rather than adopting the clean ones and
        relying on the transaction to take them back.

        The set here is Widget, Tag, the Widget_tags through table and Gadget,
        all clean, plus Seat, whose unique constraint a ForeignKey targets and
        which the operation refuses. The refusal must be reached before a
        single statement is offered for any of them.

        Two instruments, because either alone is weak. ``collect_sql=True``
        makes the schema editor append every statement to a list instead of
        executing it, so an empty list is direct evidence that no DDL was
        EMITTED rather than evidence that it was emitted and rolled back;
        introspection still runs, because it goes through a raw cursor. The
        second run then goes through a real ``atomic=False`` schema editor
        outside any atomic block, where each statement would commit as it ran,
        so a clean table still carrying no column is evidence no rollback was
        involved. The clean tables coming first is what makes the test
        falsifiable: the derived set follows the app registry's own order, in
        which Tag, Widget_tags, Widget and Gadget all precede Seat, so a
        per-table validate-then-write loop would have adopted four tables
        before reaching the refusal.
        """
        state = _fake_state()
        operation = AdoptTenantApp("thirdparty", exclude=("thirdparty.Coupon",))

        with connection.schema_editor(collect_sql=True) as editor:
            with pytest.raises(AdoptionRefusedError) as caught:
                operation.database_forwards("boundary_consumer", editor, state, state)
            collected = list(editor.collected_sql)
        assert "thirdparty.Seat" in str(caught.value)
        assert collected == [], collected

        # The same refusal through a schema editor that cannot roll back.
        with (
            pytest.raises(AdoptionRefusedError),
            connection.schema_editor(atomic=False) as editor,
        ):
            operation.database_forwards("boundary_consumer", editor, state, state)

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


# ── boundary.E007 (Phase C) ──────────────────────────────────
#
# E007 derives its expected set from BOUNDARY_TENANT_APPS and
# BOUNDARY_ADOPT_EXCLUDE, neither of which tests/settings.py sets: the whole
# check is inert by default, which is why no existing test in this suite
# changes behaviour. Each test below turns it on with override_settings, and
# must mirror the consumer migration's own exclude= in ADOPT_EXCLUDE, because
# boundary_consumer/0002 excluded Seat, SeatBooking and Coupon per-migration
# while E007 reads only the setting.

E007_TENANT_APPS = ["thirdparty"]
E007_ADOPT_EXCLUDE = list(ORDINARY_EXCLUDE)


def _e007_errors():
    """Run boundary.E007 and return only its errors, dropping W007.

    Calling the check function directly rather than through
    ``call_command("check")`` so the assertions read the Error objects
    themselves (id, msg, hint) rather than parsing formatted console output,
    and so a W007 could-not-determine warning is visibly a different thing
    from a real E007 rather than both being lines of text.
    """
    from boundary.checks import _check_adopted_tables

    return [e for e in _check_adopted_tables() if e.id == "boundary.E007"]


def _e007_for(label):
    """Return the E007 errors naming *label*, in ``app_label.ModelName`` form."""
    return [e for e in _e007_errors() if label in e.msg]


@pytest.mark.django_db(transaction=True)
class TestAcRls012E007ReportsAdoptedTableDrift:
    """AC-RLS-012 (BR-RLS-010, BR-RLS-017): boundary.E007 reports a missing
    column, missing RLS, a missing policy and a decomposed index.

    transaction=True throughout: every test here damages the schema with DDL
    and restores it in a finally block, and a non-transactional django_db
    would roll the damage back before the check's own connection could see
    a consistent picture of it.

    Each damage state is produced in turn against the real adopted
    thirdparty_widget table and then restored, rather than against a
    purpose-built fixture table, so what E007 is proven to catch is drift in
    exactly the schema AdoptTenantApp produces.
    """

    @pytest.fixture(autouse=True)
    def _e007_settings(self, settings, adopted):
        """Turn E007 on for every test in this class.

        Depends on ``adopted`` so the baseline is the correct adopted state:
        a test that asserts a specific damage state is reported would
        otherwise be reporting whatever a previous test left behind.
        """
        settings.BOUNDARY_TENANT_APPS = E007_TENANT_APPS
        settings.BOUNDARY_ADOPT_EXCLUDE = E007_ADOPT_EXCLUDE

    def test_ac_rls_012_the_correct_state_reports_nothing(self):
        """Then a run against the fully correct state reports no
        boundary.E007.

        The positive control for every damage test below. Without it, a
        check that reported nothing under any condition would pass all four
        of them vacuously.
        """
        assert _e007_errors() == [], f"expected no E007 against the correct adopted state; got {_e007_errors()}"

    def test_ac_rls_012_a_dropped_tenant_id_column_is_reported(self):
        """When the tenant_id column is dropped, then the run reports a
        boundary.E007 naming thirdparty.Widget, its table, and the missing
        column, hinting at a new AdoptTenantApp migration.
        """
        with connection.cursor() as cursor:
            cursor.execute('ALTER TABLE "thirdparty_widget" DROP COLUMN tenant_id CASCADE')
        try:
            errors = _e007_for("thirdparty.Widget")
            missing = [e for e in errors if "no tenant_id column" in e.msg]
            assert missing, f"expected E007 for the dropped column; got {[e.msg for e in errors]}"
            assert "thirdparty_widget" in missing[0].msg
            assert "AdoptTenantApp('thirdparty')" in missing[0].hint
        finally:
            _restore_after_dropped_column()

    def test_ac_rls_012_an_unforced_table_is_reported(self):
        """When NO FORCE ROW LEVEL SECURITY is applied, then the run reports
        a boundary.E007 naming thirdparty.Widget, its table, and that RLS is
        not both enabled and forced.

        NO FORCE rather than DISABLE, because it is the subtler half: with
        RLS enabled but not forced, PostgreSQL exempts the table OWNER from
        every policy on it, which is exactly the role a Django deployment
        usually connects as.
        """
        with connection.cursor() as cursor:
            cursor.execute('ALTER TABLE "thirdparty_widget" NO FORCE ROW LEVEL SECURITY')
        try:
            errors = _e007_for("thirdparty.Widget")
            unforced = [e for e in errors if "Row Level Security enabled and forced" in e.msg]
            assert unforced, f"expected E007 for the unforced table; got {[e.msg for e in errors]}"
            assert "thirdparty_widget" in unforced[0].msg
            assert "relforcerowsecurity=False" in unforced[0].msg
            assert "AdoptTenantApp('thirdparty')" in unforced[0].hint
        finally:
            with connection.cursor() as cursor:
                cursor.execute('ALTER TABLE "thirdparty_widget" FORCE ROW LEVEL SECURITY')

    def test_ac_rls_012_a_dropped_isolation_policy_is_reported(self):
        """When boundary_tenant_isolation is dropped, then the run reports a
        boundary.E007 naming thirdparty.Widget, its table, and the missing
        policy.

        Asserts the admin_bypass policy is NOT also reported, which is what
        proves the check distinguishes the two policies rather than
        reporting "some policy is missing" for either.
        """
        with connection.cursor() as cursor:
            cursor.execute('DROP POLICY boundary_tenant_isolation ON "thirdparty_widget"')
        try:
            errors = _e007_for("thirdparty.Widget")
            missing = [e for e in errors if "boundary_tenant_isolation" in e.msg]
            assert missing, f"expected E007 for the dropped policy; got {[e.msg for e in errors]}"
            assert "thirdparty_widget" in missing[0].msg
            assert not [e for e in errors if "boundary_admin_bypass" in e.msg], (
                "the admin_bypass policy is still present and must not be reported"
            )
        finally:
            from boundary.migrations_ops import create_tenant_policies

            with connection.schema_editor() as editor:
                editor.execute('DROP POLICY IF EXISTS boundary_admin_bypass ON "thirdparty_widget"')
                create_tenant_policies(editor, "thirdparty_widget")

    def test_ac_rls_012_a_dropped_admin_bypass_policy_is_reported(self):
        """When boundary_admin_bypass is dropped, then the run reports a
        boundary.E007 naming it, and does not report the isolation policy
        that is still there.

        The counterpart direction of the test above: together they prove
        conditions 3 and 4 are two conditions rather than one.
        """
        with connection.cursor() as cursor:
            cursor.execute('DROP POLICY boundary_admin_bypass ON "thirdparty_widget"')
        try:
            errors = _e007_for("thirdparty.Widget")
            missing = [e for e in errors if "boundary_admin_bypass" in e.msg]
            assert missing, f"expected E007 for the dropped policy; got {[e.msg for e in errors]}"
            assert not [e for e in errors if "boundary_tenant_isolation" in e.msg], (
                "the isolation policy is still present and must not be reported"
            )
        finally:
            from boundary.migrations_ops import create_tenant_policies

            with connection.schema_editor() as editor:
                editor.execute('DROP POLICY IF EXISTS boundary_tenant_isolation ON "thirdparty_widget"')
                create_tenant_policies(editor, "thirdparty_widget")

    def test_ac_rls_012_a_composite_unique_index_decomposed_to_a_global_one_is_reported(self):
        """When the composite unique constraint on (tenant_id, code) is
        replaced by a global unique one on (code), then the run reports a
        boundary.E007 naming thirdparty.Widget, its table, and that the
        unique form does not lead with tenant_id.

        This is BR-RLS-017's load-bearing condition 5, and the state an
        upstream package upgrade produces when its own migration drops and
        recreates the index: cross-tenant uniqueness is silently restored
        with no other signal anywhere in the system.
        """
        original = {name: columns for name, columns in unique_constraints_by_name("thirdparty_widget").items()}
        target = [name for name, columns in original.items() if columns == ["tenant_id", "code"]]
        assert target, f"expected a composite (tenant_id, code) constraint to damage; got {original}"
        name = target[0]

        with connection.cursor() as cursor:
            cursor.execute(f'ALTER TABLE "thirdparty_widget" DROP CONSTRAINT "{name}"')
            cursor.execute(f'ALTER TABLE "thirdparty_widget" ADD CONSTRAINT "{name}" UNIQUE (code)')
        try:
            errors = _e007_for("thirdparty.Widget")
            decomposed = [e for e in errors if "does not lead with tenant_id" in e.msg]
            assert decomposed, f"expected E007 for the decomposed constraint; got {[e.msg for e in errors]}"
            assert "thirdparty_widget" in decomposed[0].msg
            assert name in decomposed[0].msg
            assert "unique across every tenant" in decomposed[0].msg
        finally:
            with connection.cursor() as cursor:
                cursor.execute(f'ALTER TABLE "thirdparty_widget" DROP CONSTRAINT "{name}"')
                cursor.execute(f'ALTER TABLE "thirdparty_widget" ADD CONSTRAINT "{name}" UNIQUE (tenant_id, code)')

    def test_ac_rls_012_a_bare_global_unique_index_is_reported(self):
        """A unique INDEX backing no constraint is reported too.

        Django emits the four unique sources into two different catalogue
        forms, and an upstream migration writing a bare
        ``CREATE UNIQUE INDEX`` produces one that no pg_constraint row
        covers. A check reading only pg_constraint would miss it entirely,
        which is why condition 5 inspects both.
        """
        with connection.cursor() as cursor:
            cursor.execute('CREATE UNIQUE INDEX thirdparty_widget_bare_name_uniq ON "thirdparty_widget" (name)')
        try:
            errors = _e007_for("thirdparty.Widget")
            bare = [e for e in errors if "thirdparty_widget_bare_name_uniq" in e.msg]
            assert bare, f"expected E007 for the bare unique index; got {[e.msg for e in errors]}"
            assert "does not lead with tenant_id" in bare[0].msg
        finally:
            with connection.cursor() as cursor:
                cursor.execute("DROP INDEX thirdparty_widget_bare_name_uniq")

    def test_ac_rls_012_the_primary_key_is_not_reported(self):
        """The primary key index leads with id, not tenant_id, and must not
        be reported.

        BR-RLS-017 excludes it by name: adoption deliberately leaves the
        primary key alone, so a primary key that does not lead with
        tenant_id is the expected state rather than drift. Without this
        exclusion, condition 5 would fire on every adopted table forever.
        """
        assert _e007_errors() == [], (
            f"the primary key must not be reported against a correct adopted state; got {_e007_errors()}"
        )

    def test_ac_rls_012_a_mixin_model_in_a_listed_app_is_not_reported(self, settings):
        """And a model in the listed app that already carries TenantMixin is
        not reported.

        Adoption is the third of three mutually exclusive categories
        (BR-RLS-010), so a model that is already column-bearing is skipped
        from the derived set rather than expected to carry an adopted
        column. Proven against boundary_testapp, none of whose tables is
        adopted: Booking carries TenantMixin and its table has no tenant_id
        column of adoption's kind, so a check that did not skip it would
        report it under condition 1 immediately.

        The positive control cannot be another model in the SAME app,
        because every boundary_testapp model except Tenant is mixin-scoped
        and the whole derived set is therefore empty. It is instead the same
        check, in the same call, reporting the genuinely unadopted
        thirdparty.Gadget: E007 is demonstrably running and finding a
        missing column elsewhere in the very run that stays silent about
        Booking, so the silence is the skip and not the check failing to
        run.
        """
        with connection.cursor() as cursor:
            cursor.execute('ALTER TABLE "thirdparty_gadget" DROP COLUMN tenant_id CASCADE')
        try:
            settings.BOUNDARY_TENANT_APPS = ["boundary_testapp", "thirdparty"]
            settings.BOUNDARY_ADOPT_EXCLUDE = E007_ADOPT_EXCLUDE

            errors = _e007_errors()
            assert not [e for e in errors if "boundary_testapp.Booking" in e.msg], (
                f"a mixin-scoped model must not be reported as expected-adopted; got {[e.msg for e in errors]}"
            )
            assert _e007_for("thirdparty.Gadget"), (
                "positive control: this same run must report the genuinely unadopted "
                "table, or the silence about Booking proves nothing"
            )
        finally:
            _restore_after_dropped_column()

    def test_ac_rls_012_an_excluded_model_is_not_reported(self, settings):
        """And a model listed in BOUNDARY_ADOPT_EXCLUDE is not reported.

        Widget is dropped from the derived set by naming it in the setting,
        so the missing-column damage below goes unreported. The same damage
        WITHOUT the exclusion is asserted to be reported, which is what
        makes the silence attributable to the exclusion rather than to the
        check not running.
        """
        with connection.cursor() as cursor:
            cursor.execute('ALTER TABLE "thirdparty_widget" DROP COLUMN tenant_id CASCADE')
        try:
            assert _e007_for("thirdparty.Widget"), (
                "positive control: the dropped column must be reported when Widget is not excluded"
            )

            settings.BOUNDARY_ADOPT_EXCLUDE = [*E007_ADOPT_EXCLUDE, "thirdparty.Widget"]
            assert not _e007_for("thirdparty.Widget"), "an excluded model must not be reported"
        finally:
            _restore_after_dropped_column()

    def test_ac_rls_012_the_auto_created_through_model_is_reported(self):
        """And the auto-created many-to-many through model of a
        ManyToManyField declared in thirdparty is reported when its table
        lacks the column, proving the adopted set is derived with
        include_auto_created=True (BR-RLS-010).

        thirdparty.Widget_tags is a class Django generated, which appears in
        no models.py and which a hand-written list of models to check would
        never contain. It is reported by name, in the generated class's own
        app_label.ModelName form, which is also the form that makes
        excluding one of them possible.
        """
        with connection.cursor() as cursor:
            cursor.execute('ALTER TABLE "thirdparty_widget_tags" DROP COLUMN tenant_id CASCADE')
        try:
            errors = _e007_for("thirdparty.Widget_tags")
            missing = [e for e in errors if "no tenant_id column" in e.msg]
            assert missing, f"expected E007 for the through table; got {[e.msg for e in _e007_errors()]}"
            assert "thirdparty_widget_tags" in missing[0].msg
        finally:
            _restore_after_dropped_column()

    def test_ac_rls_012_a_model_added_after_the_adoption_migration_is_reported(self):
        """And a model added to the live thirdparty app registry after the
        adoption migration was written is reported as a missing column,
        proving E007 derives its expected set from the LIVE registry rather
        than the historical migration state (BR-RLS-017).

        Gadget is that model here: thirdparty/0001_initial created its
        table, and the adoption migration's own derivation from the
        historical state covers it, so the standing state has it adopted.
        The live-registry claim is proven by removing its column WITHOUT
        touching any migration state at all: the historical state still says
        it is adopted, the database says it is not, and E007 must side with
        the database and the live registry.

        The complementary half, a model in the live registry that the
        historical state never had, cannot be produced here without editing
        thirdparty's migrations, which BR-RLS-011 forbids. Removing the
        column is the same observable condition from E007's side: the live
        registry names a model whose table has no column.
        """
        with connection.cursor() as cursor:
            cursor.execute('ALTER TABLE "thirdparty_gadget" DROP COLUMN tenant_id CASCADE')
        try:
            errors = _e007_for("thirdparty.Gadget")
            missing = [e for e in errors if "no tenant_id column" in e.msg]
            assert missing, f"expected E007 for the new model's table; got {[e.msg for e in _e007_errors()]}"
            assert "thirdparty_gadget" in missing[0].msg
        finally:
            _restore_after_dropped_column()

    def test_ac_rls_012_the_expected_set_comes_from_the_live_registry(self, settings):
        """The derivation itself, asserted directly rather than only through
        its symptoms.

        ``adoption.expected_adopted_models()`` is what E007, E006, W009,
        boundary_deprovision and assert_rls_enforced all select from, so
        pinning it here pins all five. It must return the LIVE registry's
        models for the listed app, including the auto-created through model,
        minus what the setting excludes, and it must never consult a
        migration state.
        """
        from boundary import adoption

        settings.BOUNDARY_TENANT_APPS = E007_TENANT_APPS
        settings.BOUNDARY_ADOPT_EXCLUDE = E007_ADOPT_EXCLUDE

        labels = {adoption.model_label(m) for m in adoption.expected_adopted_models()}
        assert labels == {
            "thirdparty.Widget",
            "thirdparty.Widget_tags",
            "thirdparty.Tag",
            "thirdparty.Gadget",
        }, f"unexpected expected-adopted set: {sorted(labels)}"

        # Every one of them is the LIVE class, not a state-rendered stand-in.
        from django.apps import apps as live_apps

        for model in adoption.expected_adopted_models():
            assert model is live_apps.get_model(model._meta.app_label, model.__name__)

    def test_ac_rls_012_each_failing_condition_is_a_separate_error(self):
        """One Error per failing condition per table, not one per table.

        An operator fixing a table needs to see everything wrong with it,
        not only whichever condition the check happened to test first. Three
        conditions are damaged at once and all three must come back.
        """
        with connection.cursor() as cursor:
            cursor.execute('ALTER TABLE "thirdparty_widget" NO FORCE ROW LEVEL SECURITY')
            cursor.execute('DROP POLICY boundary_tenant_isolation ON "thirdparty_widget"')
            cursor.execute('DROP POLICY boundary_admin_bypass ON "thirdparty_widget"')
        try:
            errors = _e007_for("thirdparty.Widget")
            assert len(errors) == 3, f"expected three separate errors; got {[e.msg for e in errors]}"
            assert {"Row Level Security enabled and forced" in e.msg for e in errors} == {True, False}, (
                "expected exactly one of the three to be the RLS condition"
            )
        finally:
            from boundary.migrations_ops import create_tenant_policies

            with connection.schema_editor() as editor:
                editor.execute('ALTER TABLE "thirdparty_widget" FORCE ROW LEVEL SECURITY')
                create_tenant_policies(editor, "thirdparty_widget")


@pytest.mark.django_db(transaction=True)
class TestAcRls015AdoptionRefusesDeniedAppsButNotTheTenantModelsApp:
    """AC-RLS-015 (BR-RLS-016): adoption refuses a deny-listed app, the
    tenant model itself and an uninstalled app, but not the tenant model's
    own app.

    The acceptance criterion is written against a ``clubs`` app holding
    ``Club`` (the tenant model), ``Membership`` and ``ClubSettings``. This
    suite's ``boundary_testapp`` is that app: ``Tenant`` is the configured
    tenant model and its siblings live beside it, which is the exact shape
    BR-RLS-016 refuses to refuse.

    Refusal is asserted at operation-apply time, per the rule's "at
    operation-apply time and again at check time" split; the check-time half
    is the last two tests, which need no migration at all.
    """

    def test_ac_rls_015_a_deny_listed_app_is_refused_naming_the_reason(self):
        """When a migration applies AdoptTenantApp("contenttypes"), then it
        fails with an error naming the app and the reason (global by
        construction).

        contenttypes rather than sessions here because it is the sharper
        case: scoping it breaks Django's own bootstrap, since every
        ContentType row is looked up before any tenant could be resolved.
        """
        with pytest.raises(AdoptionRefusedError) as exc_info:
            _adopt(exclude=(), app_label="contenttypes")

        message = str(exc_info.value)
        assert "contenttypes" in message
        assert "global by construction" in message

    def test_ac_rls_015_an_uninstalled_app_is_refused_naming_the_reason(self):
        """When a migration applies AdoptTenantApp("not_installed"), then it
        fails with an error naming the app and the reason (not installed).
        """
        with pytest.raises(AdoptionRefusedError) as exc_info:
            _adopt(exclude=(), app_label="not_installed")

        message = str(exc_info.value)
        assert "not_installed" in message
        assert "is not installed" in message

    def test_ac_rls_015_the_tenant_model_itself_is_refused_naming_the_reason(self):
        """When AdoptTenantApp is applied to an app whose only adoptable
        model is the configured tenant model, then it fails with an error
        naming the model and the reason (it is the tenant model).

        The criterion's "``AdoptTenantApp("clubs", exclude=())`` in a
        configuration where ``Club`` is the only model" is reproduced by
        excluding every sibling, which leaves exactly that configuration:
        the derived set is empty, and BR-RLS-016 requires naming the model
        rather than leaving the consumer to discover nothing was adopted.
        """
        from django.apps import apps as live_apps

        from boundary import adoption

        siblings = tuple(
            adoption.model_label(model)
            for model in live_apps.get_app_config("boundary_testapp").get_models(include_auto_created=True)
            if adoption.model_label(model) != "boundary_testapp.Tenant"
        )

        with pytest.raises(AdoptionRefusedError) as exc_info:
            _adopt(exclude=siblings, app_label="boundary_testapp")

        message = str(exc_info.value)
        assert "boundary_testapp.Tenant" in message
        assert "configured tenant model" in message

    def test_ac_rls_015_the_tenant_models_own_app_skips_only_the_tenant_model(self):
        """When the tenant model's own app is derived, then the tenant model
        is SKIPPED from the set rather than refusing the whole app, and the
        tenant table carries no tenant_id column and no boundary policy.

        The criterion's ``clubs_membership`` and ``clubs_clubsettings`` are
        ``boundary_testapp``'s siblings here, and this suite's version of
        that app is the stronger case rather than the weaker one: every one
        of Tenant's siblings already carries a boundary mixin, so the
        derived set is empty for a reason that has nothing to do with the
        tenant model. What BR-RLS-016 turns on is WHY each model is absent,
        so each is asserted against its own reason rather than inferred from
        the empty result:

        - the tenant model is absent because it is the tenant model, which
          ``refusal_reason_for_model`` says in those words;
        - every sibling is absent because it is already scoped, which
          ``_is_already_scoped`` says;
        - the APP is not refused at all, which is the whole point: its
          ``refusal_reason`` is None, unlike contenttypes' above.

        The adopt-the-siblings half of the criterion is proven by the
        thirdparty adoption the rest of this module exercises, which is the
        same derivation applied to an app with unscoped models in it.
        """
        from django.apps import apps as live_apps

        from boundary import adoption

        # The app itself is NOT refused: that is the distinction BR-RLS-016
        # draws between the tenant model's app and a deny-listed one.
        assert adoption.refusal_reason("boundary_testapp") is None

        tenant_model = live_apps.get_model("boundary_testapp", "Tenant")
        reason = adoption.refusal_reason_for_model(tenant_model)
        assert reason is not None and "configured tenant model" in reason

        for model in live_apps.get_app_config("boundary_testapp").get_models(include_auto_created=True):
            if model is tenant_model:
                continue
            assert adoption._is_already_scoped(model), (
                f"{adoption.model_label(model)} is neither the tenant model nor "
                f"mixin-scoped, so this test's reasoning about why the set is "
                f"empty no longer holds"
            )

        derived = {adoption.model_label(m) for m in adoption.adopted_models("boundary_testapp")}
        assert "boundary_testapp.Tenant" not in derived, (
            "the tenant model must be SKIPPED from its own app's derived set, not refuse the whole app"
        )

        # And the tenant table is untouched: no adopted column, no policy.
        with connection.cursor() as cursor:
            assert column_type(cursor, "boundary_testapp_tenant", "tenant_id") is None
        assert _policies("boundary_testapp_tenant") == set()

    def test_ac_rls_015_e007_reports_a_deny_listed_app_before_any_migration(self, db, settings):
        """And with BOUNDARY_TENANT_APPS = ["sessions"], manage.py check
        reports boundary.E007 for the deny-listed app before any migration
        is run.

        Condition 6 is settings-only by BR-RLS-017's own division: it reads
        the setting against the app registry and the deny-list, so it fires
        with no adoption migration anywhere and nothing in the database to
        look at. sessions is not even in INSTALLED_APPS here, so the error
        must be the deny-list one rather than the not-installed one: the
        deny-list is checked first precisely so an operator is told the app
        can never be adopted rather than to go install it.
        """
        settings.BOUNDARY_TENANT_APPS = ["sessions"]
        settings.BOUNDARY_ADOPT_EXCLUDE = []

        errors = _e007_errors()
        assert len(errors) == 1, f"expected exactly one E007 for the deny-listed app; got {[e.msg for e in errors]}"
        assert "sessions" in errors[0].msg
        assert "global by construction" in errors[0].msg
        assert "BOUNDARY_TENANT_APPS" in errors[0].hint

    def test_ac_rls_015_e007_reports_nothing_for_the_tenant_model_itself(self, db, settings):
        """And with BOUNDARY_TENANT_APPS = ["boundary_testapp"], manage.py
        check reports no boundary.E007 for the tenant model itself.

        The tenant model is skipped from the derived set the way an excluded
        model is, so it is never expected-adopted and its table's total
        absence of a tenant_id column is the correct state rather than
        condition 1 drift. Without this skip, listing the tenant model's own
        app would report the tenant table forever, which is what BR-RLS-016
        means by not refusing the app.
        """
        settings.BOUNDARY_TENANT_APPS = ["boundary_testapp"]
        settings.BOUNDARY_ADOPT_EXCLUDE = []

        errors = _e007_errors()
        assert not [e for e in errors if "boundary_testapp.Tenant" in e.msg], (
            f"the tenant model must never be expected-adopted; got {[e.msg for e in errors]}"
        )
        assert not [e for e in errors if "boundary_testapp_tenant" in e.msg], (
            f"the tenant TABLE must never be reported either; got {[e.msg for e in errors]}"
        )
