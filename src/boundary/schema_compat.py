"""The one module allowed to name a private Django schema-editor attribute.

Every use of a schema-editor attribute whose name begins with an underscore
lives here and is reached from the rest of the package only through the public
functions below (BR-RLS-022). No other module under ``src/boundary/`` may name
such an attribute, which AC-RLS-020 asserts by scanning the package source.

**Why an adapter rather than the call sites.** Both wrapped attributes are
private Django API: neither appears in Django's documented schema-editor
interface, and Django may rename, re-signature or remove either in any feature
release without a deprecation cycle. With the calls spread across
``migrations_ops.py``, an upstream rename surfaced as an ``AttributeError``
inside a consumer's ``migrate``, or as whichever adoption test happened to
rebuild a composite unique constraint, with nothing naming the cause
(icvoss/django-boundary#83). Concentrating them here gives the dependency one
home and one version-assertion test (``tests/test_schema_adapter.py``) that
fails by name on every Django leg of the support matrix, turning a silent
runtime break in a consumer's migration into a red CI leg before release.

**No silent fallback.** Neither function uses ``getattr(..., None)`` or catches
``AttributeError``. A missing or renamed attribute raises, because a fallback
that quietly produced a different constraint name would make BR-RLS-012's
forward naming diverge from BR-RLS-020's reverse lookup, which is the exact
failure mode this rule exists to prevent: the reverse would then fail to find
the constraint the forward created, and would leave it in place while
reporting success.

**Verified Django versions.** The signatures and return shapes documented on
each function were read from the installed Django 5.2.17 schema editor during
this change. Django 6.0 and 6.1 are in the support matrix
(``.github/workflows/ci.yml``, ``pyproject.toml``) and are covered by the
version-assertion test on their own CI legs; they were NOT verified locally,
because no Django 6.x environment was available in this worktree. That is what
the test exists for: it runs on every matrix leg, so a 6.x rename fails there
by name rather than reaching a consumer.

This module is internal. It is absent from the exported-boundaries inventory,
it carries no contract, and a consumer importing it has no compatibility
promise (``CONTRACTS.md``, Stability).
"""


def index_name(schema_editor, table: str, columns, suffix: str) -> str:
    """Derive Django's own truncate-and-hash index name for *table*/*columns*.

    Wraps ``BaseDatabaseSchemaEditor._create_index_name(table_name,
    column_names, suffix="")``, verified against Django 5.2.17.

    Used for exactly one thing: the composite constraint name BR-RLS-012
    generates when the plain ``_tenant``-suffixed form exceeds PostgreSQL's
    63-character identifier limit. Django's scheme truncates the parts and
    appends a hash of the full input, so the result stays inside the limit and
    is collision-resistant against a sibling constraint whose name shares the
    surviving prefix, which a plain cut at 63 characters would not be.

    Returns a ``str`` of at most 30 characters plus the suffix (Django caps on
    the connection's ``max_name_length``), already unquoted.

    Raises ``AttributeError`` if the private attribute is absent, deliberately
    rather than falling back: see the module docstring.

    :param schema_editor: an open schema editor for the target connection.
    :param table: the table name the constraint is on.
    :param columns: the column names the constraint covers, in order.
    :param suffix: appended to the derived name, ``"_tenant"`` for BR-RLS-012.
    """
    return schema_editor._create_index_name(table, list(columns), suffix=suffix)


def unique_sql(schema_editor, model, fields, name: str | None = None):
    """Build the ``UNIQUE`` statement Django's own schema editor would build.

    Wraps ``BaseDatabaseSchemaEditor._create_unique_sql(model, fields,
    name=None, condition=None, deferrable=None, include=None, opclasses=None,
    expressions=None, nulls_distinct=None)``, verified against Django 5.2.17.
    Only the first three parameters are exposed, because BR-RLS-020 restores
    plain unconditional uniques only: the forward refuses a unique form
    carrying any of the other keywords rather than rewriting it, so there is
    nothing for the reverse to restore in those cases and no reason for this
    adapter to offer them.

    Used to rebuild an original unique constraint from the historical ``_meta``
    on the reverse path (BR-RLS-020), so the restored name is the one Django's
    own schema editor derives rather than one boundary invents.

    Returns a ``django.db.backends.ddl_references.Statement``, which stringifies
    to the ``ALTER TABLE ... ADD CONSTRAINT ... UNIQUE (...)`` SQL and can be
    passed straight to ``schema_editor.execute()``.

    **Returns ``None`` where the backend does not support the requested unique
    form**, which is upstream's own documented outcome for that case and not a
    fallback this adapter introduces: ``_create_unique_sql`` short-circuits on
    ``_unique_supported()`` before touching anything. The caller must handle
    ``None`` rather than executing it. With *name* left at ``None`` and no
    other keyword available here, the supported check passes on every backend
    in the matrix, so ``None`` is a defensive path rather than an expected one.

    Raises ``AttributeError`` if the private attribute is absent, deliberately
    rather than falling back: see the module docstring.

    :param schema_editor: an open schema editor for the target connection.
    :param model: the model the constraint belongs to, historical state on the
        reverse path rather than the live class.
    :param fields: the field objects the constraint covers, in order.
    :param name: an explicit constraint name, or ``None`` to let Django derive
        its own hashed one, which is what BR-RLS-020 requires.
    """
    return schema_editor._create_unique_sql(model, fields, name=name)
