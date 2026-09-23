"""AC-RLS-020 (BR-RLS-022): the private schema-editor adapter pins its
dependency on Django private API, and nothing outside it touches that API.

Two halves, both load-bearing.

The **containment** half scans the package source and asserts that
``schema_compat.py`` is the only module naming a private schema-editor
attribute. Without it, the adapter is a convention a later change can quietly
step around by calling the private method directly again, which is exactly the
state issue #83 found.

The **version-assertion** half asserts, by name and by parameter name, that
each wrapped attribute exists on the schema editor of the Django under test and
still accepts what the adapter passes it. Both attributes are private Django
API with no deprecation cycle, so Django may rename or re-signature either in
any feature release. This test runs on every Django leg of the support matrix
(``.github/workflows/ci.yml``: 5.2, 6.0, 6.1), so adding a version to the
matrix runs the assertion against it, and an upstream rename fails here by name
rather than as an ``AttributeError`` inside a consumer's ``migrate``.

The module is unmarked and runs on every leg, SQLite included (BR-ENV-006).
The adapter's dependency is on Django's private API rather than on PostgreSQL,
so the question is worth asking of whichever schema editor the leg provides.
Nothing here executes DDL: the ``schema_editor`` fixture hands over an
UNENTERED editor, because entering one is what begins a DDL transaction and on
SQLite that raises inside ``django_db``'s atomic block.

The controls matter as much as the assertions. ``inspect.signature`` is checked
against expected PARAMETER NAMES rather than merely arity, because the adapter
passes ``suffix=`` and ``name=`` as keywords: a Django release that reordered
or renamed a parameter while keeping the count would break the adapter and pass
an arity check. And a monkeypatch control removes each attribute and asserts the
adapter raises, proving the adapter has no silent fallback, which is the
specific failure BR-RLS-022 exists to prevent.
"""

import inspect
import io
import tokenize
from pathlib import Path

import pytest
from django.db import connection
from django.db.backends.base.schema import BaseDatabaseSchemaEditor

from boundary import schema_compat

#: The private attributes the adapter wraps, mapped to the parameter names the
#: adapter relies on. Positional names are asserted in order; keyword names are
#: asserted to be present and accepted as keywords.
WRAPPED_ATTRIBUTES = {
    "_create_index_name": ("table_name", "column_names", "suffix"),
    "_create_unique_sql": ("model", "fields", "name"),
}


@pytest.fixture
def schema_editor():
    """An unentered schema editor, so this module runs on every backend leg.

    The adapter's dependency is on Django's private API, not on PostgreSQL:
    BR-RLS-022 asks whether the Django under test still has these attributes
    and still accepts what the adapter passes them, which is a question about
    the Django version and is worth asking on every leg of the matrix
    (BR-ENV-006). These tests are therefore unmarked rather than ``rls``.

    The editor is deliberately NOT entered as a context manager. Entering it
    is what begins the DDL transaction, and on SQLite that raises
    ``NotSupportedError: SQLite schema editor cannot be used while foreign key
    constraint checks are enabled`` whenever a test is already inside
    ``django_db``'s atomic block, which every test here is. Nothing in this
    module executes DDL: the three adapter helpers only READ attributes off
    the editor and build SQL strings from its metadata, so an unentered
    instance answers every question asked of it on both backends. ``__exit__``
    is likewise skipped, since it is only there to run the collected DDL.
    """
    return connection.schema_editor()


def _boundary_source_files():
    """Every ``.py`` file shipped under ``src/boundary/``."""
    package_root = Path(schema_compat.__file__).parent
    return sorted(path for path in package_root.rglob("*.py") if "__pycache__" not in path.parts)


def _code_lines(source):
    """Return *source*'s lines with string literals and comments blanked out.

    BR-RLS-022 forbids a private schema-editor CALL SITE outside the adapter,
    not a mention of one in prose: ``models.py`` names
    ``BaseDatabaseSchemaEditor._create_index_name`` in a docstring explaining
    which Django helper a digest is computed through, and a raw line scan
    cannot tell that apart from a call. Tokenising and blanking every STRING
    and COMMENT token leaves executable code only, so the scan reads what the
    rule is about. The positive control below is unaffected: the adapter's own
    matches at its two ``return`` statements are code.
    """
    lines = source.splitlines()
    blanked = list(lines)
    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(source).readline))
    except tokenize.TokenError:  # pragma: no cover - a syntax error fails elsewhere
        return lines
    for token in tokens:
        if token.type not in (tokenize.STRING, tokenize.COMMENT):
            continue
        (start_row, _), (end_row, _) = token.start, token.end
        for row in range(start_row, end_row + 1):
            blanked[row - 1] = ""
    return blanked


class TestOnlyTheAdapterNamesPrivateSchemaEditorApi:
    """BR-RLS-022: every private schema-editor attribute access lives in
    exactly one module.
    """

    def test_no_module_outside_the_adapter_names_a_private_schema_editor_attribute(self):
        """Given the package source, when every module is scanned for an
        attribute access matching ``schema_editor._`` or ``SchemaEditor._``,
        then the only matches are inside ``schema_compat.py``.

        A source scan rather than an import-time check, because the thing being
        prevented is a future call site being written, not a runtime state. The
        scan is positively controlled by the next test, which asserts the
        pattern matches the adapter itself: a scan whose pattern had stopped
        matching anything would otherwise pass while proving nothing.
        """
        import re

        pattern = re.compile(r"schema_editor\._|SchemaEditor\._")
        offenders = {}
        for path in _boundary_source_files():
            if path.name == "schema_compat.py":
                continue
            matching = [
                f"{path.name}:{number}"
                for number, line in enumerate(_code_lines(path.read_text()), 1)
                if pattern.search(line)
            ]
            if matching:
                offenders[path.name] = matching

        assert offenders == {}, (
            "private schema-editor API must be reached only through boundary.schema_compat "
            f"(BR-RLS-022); found: {offenders}"
        )

    def test_the_scan_pattern_does_match_the_adapter_itself(self):
        """And the scan's pattern matches ``schema_compat.py``, so the clean
        result above is the containment holding rather than a pattern that has
        stopped matching anything at all.
        """
        import re

        pattern = re.compile(r"schema_editor\._|SchemaEditor\._")
        adapter = _code_lines(Path(schema_compat.__file__).read_text())
        assert any(pattern.search(line) for line in adapter), (
            "the scan pattern no longer matches the adapter's own CODE; the scan above proves nothing"
        )

    def test_migrations_ops_matches_nothing(self):
        """And ``migrations_ops.py`` specifically matches nothing, named
        separately because it held both call sites before this rule.
        """
        import re

        pattern = re.compile(r"schema_editor\._|SchemaEditor\._")
        path = Path(schema_compat.__file__).parent / "migrations_ops.py"
        assert not any(pattern.search(line) for line in _code_lines(path.read_text()))


class TestTheAdapterAssertsItsDjangoVersion:
    """BR-RLS-022: a version-assertion test that fails when a wrapped private
    attribute is absent from the Django under test, or no longer accepts what
    the adapter passes it.
    """

    @pytest.mark.django_db
    @pytest.mark.parametrize("attribute", sorted(WRAPPED_ATTRIBUTES))
    def test_the_wrapped_attribute_exists_on_the_schema_editor_class(self, attribute, schema_editor):
        """Given the Django version under test, when the adapter's wrapped
        attributes are looked up, then both are present on
        ``BaseDatabaseSchemaEditor`` and on the live connection's own schema
        editor class.

        Both lookups, because the base class is what the adapter documents
        while the live class is what it actually calls: a backend overriding
        one of these without the base declaring it, or vice versa, is a
        difference the adapter would meet at runtime. Running on both backends
        is the point: the live class differs per backend, so the SQLite leg
        asks the question of the SQLite editor.
        """
        assert hasattr(BaseDatabaseSchemaEditor, attribute), (
            f"Django {_django_version()} has no BaseDatabaseSchemaEditor.{attribute}; "
            "boundary.schema_compat wraps it (BR-RLS-022) and must be updated"
        )
        assert hasattr(type(schema_editor), attribute), (
            f"the {connection.vendor} schema editor has no {attribute}; boundary.schema_compat wraps it"
        )

    @pytest.mark.django_db
    @pytest.mark.parametrize(("attribute", "expected_parameters"), sorted(WRAPPED_ATTRIBUTES.items()))
    def test_the_wrapped_attribute_still_accepts_the_parameters_the_adapter_passes(
        self, attribute, expected_parameters, schema_editor
    ):
        """And each still declares the parameter names the adapter passes, so a
        Django rename fails by name.

        Parameter NAMES rather than arity: the adapter passes ``suffix=`` and
        ``name=`` as keywords, so a release that renamed either while keeping
        the signature's shape would break the adapter at runtime and pass an
        arity-only check. The first two of each are asserted in order as well
        as by name, since the adapter passes those positionally.
        """
        signature = inspect.signature(getattr(type(schema_editor), attribute))

        names = [name for name in signature.parameters if name != "self"]
        for expected in expected_parameters:
            assert expected in names, (
                f"Django {_django_version()}'s {attribute} no longer declares {expected!r} "
                f"(has {names}); boundary.schema_compat passes it (BR-RLS-022)"
            )
        assert names[:2] == list(expected_parameters[:2]), (
            f"{attribute}'s first two parameters changed order or name: expected "
            f"{list(expected_parameters[:2])}, got {names[:2]}; the adapter passes them positionally"
        )

    @pytest.mark.django_db
    def test_index_name_returns_a_string_within_the_identifier_limit(self, schema_editor):
        """And calling ``index_name()`` through the adapter returns the
        documented shape: a ``str`` carrying the requested suffix, within
        whatever identifier limit the backend declares.

        The limit is read from ``connection.ops.max_name_length()`` rather
        than written as 63, because the truncation is Django's and is driven
        by that value: PostgreSQL declares 63 and Django truncates to it,
        while SQLite declares ``None`` (no limit) and Django truncates
        nothing, so the same call returns 54 characters on PostgreSQL and 71
        on SQLite. Asserting a literal 63 would assert a PostgreSQL property
        of a backend that does not have it and fail on the SQLite leg for a
        correct result.

        The suffix and the type ARE the adapter's own contract on every
        backend, and BR-RLS-012's naming path depends on them, so those two
        assertions are unconditional. The 63-character case is covered on the
        PostgreSQL legs, where it is a real constraint.
        """
        name = schema_compat.index_name(schema_editor, "a" * 40, ["tenant_id", "code"], "_tenant")

        assert isinstance(name, str)
        assert name.endswith("_tenant")

        max_length = connection.ops.max_name_length()
        if max_length is not None:
            assert len(name) <= max_length, (
                f"{connection.vendor} declares a {max_length}-character identifier limit "
                f"and the adapter returned {len(name)} characters: {name}"
            )

    @pytest.mark.django_db
    def test_unique_sql_returns_an_object_carrying_the_composite_unique_sql(self, schema_editor):
        """And calling ``unique_sql()`` through the adapter returns an object
        whose string form is the composite ``UNIQUE`` statement, naming the
        table and every column asked for.

        Asserted on the rendered SQL rather than the object's type, because the
        contract the caller depends on is that the value can be handed to
        ``schema_editor.execute()`` and will constrain those columns. A type
        assertion on ``Statement`` would pass for a statement over the wrong
        columns.

        The assertions are deliberately about UNIQUE, the table and the
        columns, and not about the statement's full text, because the text
        legitimately differs per backend: Django's SQLite editor renders this
        as ``CREATE UNIQUE INDEX`` while PostgreSQL's renders an ``ALTER
        TABLE ... ADD CONSTRAINT ... UNIQUE``. What the adapter's caller
        depends on is the same on both, so this runs on both legs rather than
        pinning one backend's spelling.
        """
        from django.apps import apps

        model = apps.get_model("boundary_testapp", "Booking")
        fields = [model._meta.get_field("court"), model._meta.get_field("is_paid")]

        statement = schema_compat.unique_sql(schema_editor, model, fields)

        assert statement is not None, "a plain unconditional unique is supported on every backend in the matrix"
        sql = str(statement)
        assert "UNIQUE" in sql.upper()
        assert model._meta.db_table in sql
        for field in fields:
            assert field.column in sql, f"the statement must cover {field.column}; got {sql}"

    @pytest.mark.django_db
    @pytest.mark.parametrize("attribute", sorted(WRAPPED_ATTRIBUTES))
    def test_the_adapter_raises_rather_than_falling_back_when_the_attribute_is_gone(
        self, attribute, monkeypatch, schema_editor
    ):
        """And the assertion fails, rather than skipping or passing vacuously,
        when a wrapped attribute is absent: proven by removing it from the
        schema editor class and asserting the adapter raises.

        This is the control that makes the whole file mean something. BR-RLS-022
        forbids a ``getattr(..., None)`` fallback because a quietly different
        constraint name would make BR-RLS-012's forward naming diverge from
        BR-RLS-020's reverse lookup, so the reverse would fail to find the
        constraint the forward created and leave it in place while reporting
        success. An adapter that swallowed the missing attribute would pass
        every other test in this file.
        """
        editor_class = type(schema_editor)
        # Delete the attribute from every class in the MRO that DEFINES it,
        # which is what makes the lookup genuinely fail. ``__dict__`` rather
        # than ``hasattr``, because monkeypatch.delattr calls ``delattr`` on
        # the object it is given even under ``raising=False``: handed a
        # subclass that merely INHERITS the attribute, ``hasattr`` is true,
        # the delete raises ``AttributeError`` from the patch call itself,
        # and undo then fails in teardown as well.
        for klass in (*editor_class.__mro__, BaseDatabaseSchemaEditor):
            if attribute in vars(klass):
                monkeypatch.delattr(klass, attribute)

        with pytest.raises(AttributeError):
            if attribute == "_create_index_name":
                schema_compat.index_name(schema_editor, "table", ["tenant_id"], "_tenant")
            else:
                from django.apps import apps

                model = apps.get_model("boundary_testapp", "Booking")
                schema_compat.unique_sql(schema_editor, model, [model._meta.get_field("court")])


class TestTheAdapterPreservesTheNameTheForwardAlreadyGenerated:
    """BR-RLS-020's reverse lookup depends on the adapter deriving the same
    name the direct private call derived before the adapter existed.
    """

    @pytest.mark.django_db
    def test_the_composite_name_for_the_adopted_widget_table_is_unchanged(self, schema_editor):
        """And the composite constraint name the adapter produces for the
        suite's adopted ``thirdparty_widget`` table is the exact name
        BR-RLS-012 generates, so BR-RLS-020's reverse lookup is unaffected.

        The exact literal is the assertion, deliberately: a name recomputed
        from the same code path it is checking would hold by construction
        whatever that code did, which is the defect issue #83 filed against the
        previous coverage of this helper.
        """
        from boundary.migrations_ops import composite_constraint_name

        name = composite_constraint_name(
            schema_editor,
            "thirdparty_widget",
            "thirdparty_widget_code_key",
            ["tenant_id", "code"],
        )

        assert name == "thirdparty_widget_code_key_tenant"


def _django_version() -> str:
    import django

    return django.get_version()
