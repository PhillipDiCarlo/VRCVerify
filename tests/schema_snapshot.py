"""Read schema/production.sql and compare it against the models in bot.py.

WHY THIS EXISTS. The suite runs on SQLite, and SQLite cannot reproduce a
disagreement about a column's type. It gives a VARCHAR column TEXT affinity, so
an integer written into one comes back as a string -- which means the
production behavior is not merely untested here, it is unreachable. #164
shipped on exactly that: `servers.server_id` is declared String while the
deployed column is `bigint`, so psycopg returned an int, a dict keyed on the
raw value matched nothing, and every server on the picker read as unconfigured.
Two regression tests written for that bug passed against the broken code.

WHY NOT JUST RUN THE SUITE ON POSTGRES. Because the schema would be built by
`Base.metadata.create_all()`, which creates the columns the MODELS describe.
That reproduces the models' opinion of the schema, and the models' opinion is
the half of the disagreement that was never in question. The divergence is
between the models and the DEPLOYED schema, so the deployed schema has to come
from the deployed database. schema/production.sql is that, captured by
scripts/refresh_schema_snapshot.sh.

This module does the reading and the comparing. The judgment about which
divergences are known lives in test_schema_snapshot.py, next to the reasons.
"""

import os
import re

from sqlalchemy.dialects import postgresql

SNAPSHOT_PATH = os.path.join(
    os.path.dirname(__file__), "..", "schema", "production.sql"
)

# CREATE TABLE public.<name> ( ...one column per line... );
#
# Deliberately not a SQL parser. pg_dump's output is machine-generated and
# rigidly formatted, so a pattern over it is stable in a way a pattern over
# hand-written SQL would not be -- and a real parser would be a dependency and
# a second thing that can be wrong.
_TABLE = re.compile(r"^CREATE TABLE public\.(\w+) \(\n(.*?)\n\);", re.S | re.M)

# Table-level constraints share the column list; they are not columns.
_NOT_A_COLUMN = ("CONSTRAINT", "PRIMARY", "UNIQUE", "FOREIGN", "CHECK")


class DeployedColumn:
    """One column as production actually has it."""

    def __init__(self, name, sql_type, nullable):
        self.name = name
        self.sql_type = sql_type
        self.nullable = nullable

    def __repr__(self):
        null = "" if self.nullable else " NOT NULL"
        return f"{self.sql_type}{null}"


def parse_snapshot(text=None):
    """{table: {column: DeployedColumn}} from a pg_dump --schema-only file."""
    if text is None:
        with open(SNAPSHOT_PATH, encoding="utf-8") as handle:
            text = handle.read()

    tables = {}
    for match in _TABLE.finditer(text):
        columns = {}
        for line in match.group(2).split("\n"):
            line = line.strip().rstrip(",")
            if not line or line.upper().startswith(_NOT_A_COLUMN):
                continue
            name, _, rest = line.partition(" ")
            # pg_dump quotes any identifier that is a reserved word, so a
            # column called "order" arrives as `"order" integer`. Left quoted
            # it would match no model column and be reported as absent, which
            # is a confusing failure for a column that is present and fine.
            name = name.strip('"')
            rest = rest.strip()
            nullable = "NOT NULL" not in rest.upper()
            # DEFAULT and NOT NULL are not part of the type. Cut at whichever
            # comes first: `timestamp with time zone DEFAULT now() NOT NULL`
            # has to end up as `timestamp with time zone`.
            sql_type = re.split(r"\s+DEFAULT\s+|\s+NOT NULL\b", rest, flags=re.I)[0]
            columns[name] = DeployedColumn(name, sql_type.strip(), nullable)
        tables[match.group(1)] = columns
    return tables


def model_tables(base):
    """{table: {column: (rendered Postgres type, nullable)}} from the models.

    Rendered through the Postgres dialect rather than compared as Python
    classes, so `String(30)` and `character varying(30)` are put in the same
    vocabulary before anything is decided about them.
    """
    tables = {}
    for table in base.metadata.sorted_tables:
        columns = {}
        for column in table.columns:
            rendered = column.type.compile(postgresql.dialect()).lower()
            # A primary key is NOT NULL whether or not the model says so.
            nullable = column.nullable and not column.primary_key
            columns[column.name] = (rendered, nullable)
        tables[table.name] = columns
    return tables


# Names for the same thing. pg_dump spells out what the dialect abbreviates.
_ALIASES = {
    "character varying": "varchar",
    "timestamp with time zone": "timestamptz",
    "timestamp without time zone": "timestamp",
    "double precision": "float",
    "boolean": "bool",
}


def _normalize(sql_type):
    """(family, parameters) -- 'character varying(30)' -> ('varchar', (30,)).

    Every number inside the parentheses is kept, not just the first. The first
    version took only the leading one, which is right for varchar(n) and wrong
    for numeric(p, s): it read numeric(10,2) and numeric(10,4) as the same
    thing and had nothing to say about the difference. A check whose failure
    mode is silence is the failure mode this whole file exists to remove.
    """
    sql_type = sql_type.strip().lower()
    parameters = None
    match = re.match(r"^(.*?)\((\d+(?:\s*,\s*\d+)*)\)$", sql_type)
    if match:
        sql_type = match.group(1).strip()
        parameters = tuple(int(part) for part in match.group(2).split(","))
    return _ALIASES.get(sql_type, sql_type), parameters


class Divergence:
    """One column the models and the deployed schema disagree about.

    `kind` is the thing that decides how much it matters, so it is carried
    rather than inferred by whoever reads the list:

    type        The families differ -- varchar against bigint. This is the one
                that shipped an outage, because the driver returns a different
                Python type than the code was written for and nothing raises.
    length      Same family, different cap. A write longer than the deployed
                cap fails on production and passes on SQLite, which ignores
                VARCHAR lengths entirely.
    timezone    One side is tz-aware and the other is not. Comparisons between
                them raise on Postgres; on SQLite everything is a string.
    nullable    The model permits NULL where the column does not, or the other
                way round. The first direction fails on write.
    absent      In the models, not in the deployed schema. create_all() adds
                missing tables but never missing columns, so this is a column
                every query against that table will fail on.
    """

    def __init__(self, table, column, kind, model, deployed):
        self.table = table
        self.column = column
        self.kind = kind
        self.model = model
        self.deployed = deployed

    @property
    def key(self):
        return f"{self.table}.{self.column}"

    def __repr__(self):
        return f"{self.key}: {self.kind} -- model {self.model}, deployed {self.deployed}"

    def __eq__(self, other):
        return repr(self) == repr(other)

    def __hash__(self):
        return hash(repr(self))


def compare(base, snapshot=None):
    """Every column the models and the snapshot disagree about.

    Tables in the snapshot but not in the models are ignored: the models are
    the authority on what this code uses, and a leftover table is somebody
    else's problem. Tables in the models but not in the snapshot are ignored
    too -- create_all() builds those complete, which is the one case where the
    two genuinely cannot drift.
    """
    if snapshot is None:
        snapshot = parse_snapshot()
    found = []
    for table, columns in model_tables(base).items():
        deployed_table = snapshot.get(table)
        if deployed_table is None:
            continue
        for name, (model_type, model_nullable) in columns.items():
            deployed = deployed_table.get(name)
            if deployed is None:
                found.append(Divergence(table, name, "absent", model_type, "--"))
                continue

            model_family, model_parameters = _normalize(model_type)
            deployed_family, deployed_parameters = _normalize(deployed.sql_type)

            if model_family != deployed_family:
                kind = (
                    "timezone"
                    if {model_family, deployed_family} == {"timestamp", "timestamptz"}
                    else "type"
                )
                found.append(
                    Divergence(table, name, kind, model_type, deployed.sql_type)
                )
            elif model_parameters != deployed_parameters:
                found.append(
                    Divergence(table, name, "length", model_type, deployed.sql_type)
                )

            if model_nullable != deployed.nullable:
                found.append(
                    Divergence(
                        table,
                        name,
                        "nullable",
                        "NULL allowed" if model_nullable else "NOT NULL",
                        "NULL allowed" if deployed.nullable else "NOT NULL",
                    )
                )
    return sorted(found, key=repr)
