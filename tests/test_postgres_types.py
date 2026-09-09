"""The facts that still need a real Postgres after #275.

Skipped unless VRCVERIFY_TEST_DATABASE_URL points at one, which
scripts/test_postgres.sh sets up.

This file used to carry the int round-trip for servers.server_id as well. It
does not any more, and the reason is the point of #275: the model now declares
BigInteger, SQLite gives a BIGINT column INTEGER affinity, and the production
type is reachable from the ordinary fast suite. That assertion moved to
test_schema_snapshot.py, where it runs on every commit rather than only when
somebody remembers to start a container.

What is left here is what SQLite genuinely cannot answer, because it is loosely
typed and Postgres is not.
"""

import os

import pytest
from sqlalchemy import inspect

import bot

pytestmark = pytest.mark.skipif(
    not os.environ.get("VRCVERIFY_TEST_DATABASE_URL", "").strip(),
    reason="needs Postgres: ./scripts/test_postgres.sh",
)


@pytest.fixture(autouse=True)
def clean_servers():
    def wipe():
        with bot.session_scope() as session:
            session.query(bot.Server).delete()

    wipe()
    yield
    wipe()


class TestTheSchemaCameFromTheSnapshot:
    """If this fails, the container was built by create_all() rather than
    loaded from schema/production.sql, and the whole mode is measuring the
    models against themselves."""

    def test_the_column_is_bigint(self):
        columns = {c["name"]: c for c in inspect(bot.engine).get_columns("servers")}
        assert "BIGINT" in str(columns["server_id"]["type"]).upper()

    def test_a_genuinely_textual_id_column_is_still_text(self):
        """instruction_panel_views.server_id is varchar and stays varchar --
        which is why panel_view_key() still has a job after the reconciliation.
        A snapshot that made every id column bigint would hide that."""
        columns = {
            c["name"]: c
            for c in inspect(bot.engine).get_columns("instruction_panel_views")
        }
        assert "VARCHAR" in str(columns["server_id"]["type"]).upper()


class TestStrictTypingSqliteCannotShow:
    def test_a_non_numeric_id_is_refused(self):
        """SQLite stores 'a' in an INTEGER column without complaint; Postgres
        raises.

        THIS IS THE ONE THAT DOES NOT TRANSFER. Declaring BigInteger buys the
        fast suite the production *type* on the way out -- reads come back as
        ints -- but not strict typing on the way in: SQLite's INTEGER affinity
        converts a numeric string and shrugs at anything else. So a fixture
        holding a non-numeric id is still invisible to `pytest` and still
        raises here, which is the gap #281 emptied the fixtures to close.

        Kept as the standing proof that the Postgres mode can still see
        something the default run cannot.
        """
        from sqlalchemy.exc import DataError

        with pytest.raises(DataError):
            with bot.session_scope() as session:
                session.add(bot.Server(server_id="a", owner_id="1", role_id="2"))

    def test_a_non_numeric_owner_id_is_refused_too(self):
        """owner_id and role_id are bigint as well, reconciled in #281.

        Named separately because "the id columns" is three columns, and a
        fixture is as likely to put a label in one as in another -- several
        did.
        """
        from sqlalchemy.exc import DataError

        with pytest.raises(DataError):
            with bot.session_scope() as session:
                session.add(
                    bot.Server(server_id="1", owner_id="nobody", role_id="2")
                )
