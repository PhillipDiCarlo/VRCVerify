"""The models and the deployed schema, held against each other.

THE POINT OF THIS FILE. Everything else in the suite runs on SQLite, where a
column's type is nearly an opinion: VARCHAR gets TEXT affinity, so an integer
put into one comes back as a string, and code written for a string passes. On
production that same column is `bigint` and the driver hands back an int.
That gap is not a coverage hole that a better-written test could close from
inside SQLite -- the behavior is unreachable there. #164 shipped through it,
and the two regression tests written for #164 passed against the broken code.

So this file does not test behavior at all. It compares the column types the
models declare against schema/production.sql, a pg_dump --schema-only of the
real database, and fails when a disagreement appears that is not written down
below. The parsing and comparing is in schema_snapshot.py; what lives here is
the judgment about which disagreements are known, and why each one is tolerable
or is not.

WHEN THIS FAILS. Either a model changed and the database has not, or the
database changed and nobody wrote it down. Both are worth a stop. Refresh the
snapshot with scripts/refresh_schema_snapshot.sh, look at the diff, and then
either fix the divergence or add it below with a reason.
"""

import re

import pytest

import bot
from schema_snapshot import compare, parse_snapshot

# Every divergence that exists today, keyed by exactly what the comparison
# prints, so an entry cannot quietly go on covering a different fact than the
# one it was written for. If a type moves on either side, its entry goes stale
# and test_nothing_here_is_stale says so.
#
# An entry is a record that somebody looked, not a permission slip. The reason
# is the whole value of the line.
KNOWN = {
    # ---- The type-class divergences. These are the dangerous family: the
    # driver returns a different PYTHON type than the code was written for, and
    # nothing raises. #164 is what that looks like in production.
    "servers.owner_id: type -- model varchar, deployed bigint": (
        "Same shape as server_id and found the same way, but never keyed on:"
        " the only consumer is resolve_config_admin(), which does int(owner_id)"
        " before use, so it is indifferent to which type it is handed. Writes"
        " send a str into a bigint column, which Postgres casts."
    ),
    "servers.role_id: type -- model varchar, deployed bigint": (
        "As owner_id. Read straight into int() for a Discord API call, never"
        " compared against a string and never used as a dict key."
    ),
    "users.discord_id: type -- model varchar(30), deployed bigint": (
        "Only ever reaches the database through filter_by(discord_id=...),"
        " where Postgres casts the parameter. No dict is keyed on it, which is"
        " the thing that would break -- see _rows_by_server_id's docstring."
    ),
    "users.vrc_user_id: type -- model varchar(50), deployed text": (
        "text is wider than varchar(50), so nothing that fits the model fails"
        " on production. The divergence runs in the safe direction; the one to"
        " watch is the nullable entry below, which does not."
    ),
    # ---- Timezone. Comparing an aware datetime against a naive one raises
    # TypeError in Python and Postgres refuses the mixed comparison too. On
    # SQLite both are strings, so neither complains.
    "users.last_verification_attempt: timezone -- model timestamp with time zone, deployed timestamp without time zone": (
        "WRITE-ONLY TODAY, which is the only reason it is quiet. All three"
        " writers store datetime.now(timezone.utc); Postgres drops the offset"
        " and reads would come back naive. Nothing reads it. The first code"
        " that compares it against an aware now() raises on production and"
        " passes here."
    ),
    "servers.subscription_start_date: timezone -- model timestamp without time zone, deployed timestamp with time zone": (
        "A dead column. bot.py records at two places that the subscription"
        " columns on `servers` were never able to hold the Stripe state and"
        " are unused; stripe_subscription carries it instead."
    ),
    "servers.last_renewal_date: timezone -- model timestamp without time zone, deployed timestamp with time zone": (
        "Dead alongside subscription_start_date, and named in the same two"
        " comments."
    ),
    # ---- Length. SQLite ignores VARCHAR lengths entirely, so an over-long
    # write passes here and raises StringDataRightTruncation on production.
    "servers.instructions_locale: length -- model varchar, deployed character varying(10)": (
        "Written only from the locale table's own keys, the longest of which is"
        " well inside 10 characters. A locale code longer than that would be a"
        " new locale, and adding one is not a silent act."
    ),
    "servers.unverified_role_id: length -- model varchar, deployed character varying(64)": (
        "A Discord snowflake as a string is 17-20 characters, so the cap is"
        " roughly three times the longest value that can reach it. The model"
        " being the unbounded side is the safe direction."
    ),
    "servers.custom_verification_requested_message: length -- model varchar, deployed character varying(1000)": (
        "GUARDED IN CODE, and exactly: CUSTOM_MESSAGE_MAX_LEN is 1000 and"
        " _coerce_custom_message rejects anything longer before it can reach"
        " the column. If that constant is ever raised, this column has to be"
        " widened in the same change or the setting starts failing on save."
    ),
    # ---- Nullability. The model permitting NULL where the column does not is
    # the direction that fails, on write, at flush time.
    "users.vrc_user_id: nullable -- model NULL allowed, deployed NOT NULL": (
        "THE NARROW ONE. The model says a User may have no VRChat id; the"
        " column says otherwise. The single place that builds a User without"
        " one (handle_verification_result) assigns vrc_user_id before the"
        " session flushes, so it survives on a technicality. A second"
        " construction site that does not would raise NotNullViolation on"
        " production and pass every test here."
    ),
    "servers.auto_nickname_change: nullable -- model NULL allowed, deployed NOT NULL": (
        "Column(Boolean, default=False) leaves nullable at its default of True."
        " The default fills the value on every insert the code makes, so the"
        " permission is never exercised."
    ),
    "pending_verifications.created_at: nullable -- model NULL allowed, deployed NOT NULL": (
        "The deployed column carries DEFAULT now(), so an insert that omits it"
        " gets a value rather than an error."
    ),
}


@pytest.fixture(scope="module")
def divergences():
    return compare(bot.Base)


class TestTheModelsAgainstTheDeployedSchema:
    def test_no_divergence_that_nobody_has_looked_at(self, divergences):
        found = {repr(d) for d in divergences}
        unknown = sorted(found - set(KNOWN))
        assert not unknown, (
            "The models and schema/production.sql disagree about a column that"
            " is not written down in KNOWN.\n\n"
            + "\n".join(f"    {line}" for line in unknown)
            + "\n\nEither a model moved and the database did not, or somebody"
            " ran an ALTER by hand. Refresh the snapshot"
            " (./scripts/refresh_schema_snapshot.sh), read the diff, then"
            " either reconcile the column or add it to KNOWN with the reason"
            " it is safe. Do not add it without looking: SQLite cannot"
            " reproduce any of these, so this check is the only place they"
            " are visible."
        )

    def test_nothing_here_is_stale(self, divergences):
        """An entry that no longer describes anything has to go.

        Otherwise reconciling a column leaves its excuse behind, and the next
        reader takes a fixed divergence for a live one.
        """
        found = {repr(d) for d in divergences}
        stale = sorted(set(KNOWN) - found)
        assert not stale, (
            "KNOWN describes divergences that no longer exist. If the column"
            " was reconciled, delete the entry.\n\n"
            + "\n".join(f"    {line}" for line in stale)
        )

    def test_every_entry_carries_a_reason(self):
        """A registry of bare keys would be a mute list, which is what the
        hand-kept column list in test_schema_drift.py failed as."""
        for key, reason in KNOWN.items():
            assert len(reason) > 60, f"{key} needs a real reason, not a label."


class TestTheOutageIsCovered:
    """#164, named. The acceptance criterion for #275 is that this mismatch is
    found by a test rather than by users."""

    def test_the_column_that_took_the_picker_down_agrees_now(self, divergences):
        """#275 reconciled it: the model says BigInteger and the column is
        bigint, so there is nothing left to report.

        Kept as an assertion rather than deleted, because "no divergence" is a
        property worth defending. A change that puts String back would fail
        here with the outage named, which is a better error than a bare entry
        reappearing in the unknown list.
        """
        assert not [d for d in divergences if d.key == "servers.server_id"], (
            "servers.server_id diverges again. This is the column behind #164:"
            " declared String while deployed bigint made the driver return an"
            " int, a dict keyed on it matched nothing, and every picker card"
            " read as unconfigured. It is BigInteger for that reason."
        )

    def test_the_old_declaration_would_still_be_caught(self):
        """The mutation that matters now that the column is reconciled.

        Hand the comparison the schema the model USED to claim -- varchar,
        which is what #164 shipped -- and it has to report a type divergence.
        Without this, a revert of the model to String would be invisible here,
        since the KNOWN entry that used to excuse it is gone.

        This is also why the snapshot cannot be built by create_all(): a
        Postgres database created from the models would produce whatever the
        models say, and the check would have nothing to compare against.
        """
        as_it_was_declared = parse_snapshot(
            "CREATE TABLE public.servers (\n"
            "    server_id character varying NOT NULL\n"
            ");\n"
        )
        found = [
            d
            for d in compare(bot.Base, {"servers": as_it_was_declared["servers"]})
            if d.key == "servers.server_id" and d.kind == "type"
        ]
        assert found, "A model declaring server_id as text has to be reported."
        assert found[0].model == "bigint"


class TestTheReconciliationHoldsAtRuntime:
    """The declaration is only half of it. This is the round trip.

    It runs under SQLite, which is the whole win from #275: declaring
    BigInteger gives the column INTEGER affinity there too, so the type
    production returns is now reachable in the fast suite. Under String this
    assertion was impossible to write honestly -- SQLite handed back the string
    it was given, and the two regression tests written for #164 passed against
    the broken code because of it.
    """

    @pytest.fixture(autouse=True)
    def clean_servers(self):
        def wipe():
            with bot.session_scope() as session:
                session.query(bot.Server).delete()

        wipe()
        yield
        wipe()

    def test_a_string_written_to_server_id_reads_back_as_an_int(self):
        with bot.session_scope() as session:
            session.add(bot.Server(server_id="123456789", owner_id="1", role_id="2"))
        with bot.session_scope() as session:
            row = session.query(bot.Server).first()
            assert isinstance(row.server_id, int)

    def test_a_dict_keyed_on_the_raw_value_is_what_broke(self):
        """#164 reproduced rather than described -- the assertion the original
        regression tests could not make."""
        with bot.session_scope() as session:
            session.add(bot.Server(server_id="123456789", owner_id="1", role_id="2"))
        with bot.session_scope() as session:
            rows = session.query(bot.Server).all()
            assert {row.server_id: row for row in rows}.get("123456789") is None
            normalized = bot._rows_by_server_id(rows)
            assert normalized.get(bot.panel_view_key("123456789")) is not None


class TestTheSnapshotItself:
    def test_it_holds_no_row_data(self):
        """--schema-only, enforced rather than trusted. A full dump committed
        here would put every server's configuration into git history, and the
        mistake looks identical until you open the file."""
        with open(
            __import__("schema_snapshot").SNAPSHOT_PATH, encoding="utf-8"
        ) as handle:
            text = handle.read()
        assert not re.search(r"^(COPY|INSERT INTO) ", text, re.M)

    def test_it_covers_the_tables_the_models_use(self):
        deployed = set(parse_snapshot())
        modelled = {t.name for t in bot.Base.metadata.sorted_tables}
        missing = sorted(modelled - deployed)
        assert not missing, (
            f"Tables in the models but not in the snapshot: {missing}. Either"
            " they have never been deployed, or the snapshot is stale."
        )

    def test_it_keeps_the_sentinel_the_refresh_script_reads(self):
        """refresh_schema_snapshot.sh carries the note above this line across a
        refresh and regenerates nothing else. Without the sentinel it has to
        guess where the note ends, and the version that guessed -- first blank
        line -- appended three lines of pg_dump's own header to the note on
        every run, because $(...) had already eaten the blank line it was
        looking for. The script refuses to run if this is missing; this says
        why it is here so nobody tidies it away.
        """
        with open(
            __import__("schema_snapshot").SNAPSHOT_PATH, encoding="utf-8"
        ) as handle:
            text = handle.read()
        assert "-- (end of note; everything below is pg_dump output)" in text
        assert text.count("-- PostgreSQL database dump\n") == 1, (
            "The snapshot has more than one pg_dump header. That is the growth"
            " bug above: the note swallowed part of the dump on a refresh."
        )

    def test_it_carries_no_per_dump_token(self):
        """pg_dump 17 emits \\restrict with a token that is random per run.
        Left in, every refresh diffs dirty and the real changes hide in the
        noise. refresh_schema_snapshot.sh strips them; this says so."""
        with open(
            __import__("schema_snapshot").SNAPSHOT_PATH, encoding="utf-8"
        ) as handle:
            text = handle.read()
        assert not re.search(r"^\\restrict ", text, re.M)


class TestTheParser:
    """pg_dump's output is machine-generated, but the reading of it is not."""

    def test_not_null_is_not_read_as_part_of_the_type(self):
        parsed = parse_snapshot(
            "CREATE TABLE public.t (\n    c character varying(30) NOT NULL\n);\n"
        )
        assert parsed["t"]["c"].sql_type == "character varying(30)"
        assert parsed["t"]["c"].nullable is False

    def test_a_default_is_not_read_as_part_of_the_type(self):
        parsed = parse_snapshot(
            "CREATE TABLE public.t (\n"
            "    c timestamp with time zone DEFAULT now() NOT NULL\n"
            ");\n"
        )
        assert parsed["t"]["c"].sql_type == "timestamp with time zone"

    def test_a_default_containing_a_cast_survives_it(self):
        """`DEFAULT 'en-US'::character varying` is the shape that would make a
        naive split on 'character varying' produce nonsense."""
        parsed = parse_snapshot(
            "CREATE TABLE public.t (\n"
            "    c character varying(10) DEFAULT 'en-US'::character varying NOT NULL\n"
            ");\n"
        )
        assert parsed["t"]["c"].sql_type == "character varying(10)"

    def test_a_table_level_constraint_is_not_read_as_a_column(self):
        parsed = parse_snapshot(
            "CREATE TABLE public.t (\n"
            "    c integer NOT NULL,\n"
            "    CONSTRAINT t_c_check CHECK ((c > 0))\n"
            ");\n"
        )
        assert set(parsed["t"]) == {"c"}

    def test_a_column_with_no_qualifiers_is_nullable(self):
        parsed = parse_snapshot("CREATE TABLE public.t (\n    c bigint\n);\n")
        assert parsed["t"]["c"].nullable is True
