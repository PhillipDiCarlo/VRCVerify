"""create_all() adds missing tables but never missing columns.

So a column added to a model after its table shipped exists only in the code
until someone runs an ALTER by hand. Until then every query against that table
raises, and the driver error names a column and says nothing about migrations
-- which is several steps from the answer for whoever is reading the log.

This is not hypothetical. group_seat_lease.release_job_id was added in
"Fix six seat bugs found by adversarial review", never made it into the
hand-kept list this check used to use, and the first anyone heard of it was a
live ProgrammingError on a member pressing the invite button.
"""

import logging

import pytest
from sqlalchemy import Column, Integer, String, inspect

import bot


class TestTheCheckIsDerivedNotRemembered:
    def test_every_model_table_is_covered(self):
        """The property the old hand-kept list could not have: nothing to
        forget, because nothing is written down twice."""
        import inspect as _inspect

        source = _inspect.getsource(bot._warn_about_missing_columns)
        assert "Base.metadata.sorted_tables" in source

    def test_the_column_that_was_missed_is_now_in_scope(self):
        """The specific regression, named."""
        columns = {c.name for c in bot.Base.metadata.tables["group_seat_lease"].columns}
        assert "release_job_id" in columns

    def test_a_missing_column_is_reported_with_a_runnable_statement(
        self, caplog, monkeypatch
    ):
        """The log line has to be pasteable. Whoever reads it is mid-incident."""
        real_inspect = inspect

        class Pretend:
            def get_table_names(self):
                return ["group_seat_lease"]

            def get_columns(self, table):
                real = real_inspect(bot.engine)
                try:
                    cols = real.get_columns(table)
                except Exception:
                    cols = [
                        {"name": c.name}
                        for c in bot.Base.metadata.tables[table].columns
                    ]
                return [c for c in cols if c["name"] != "release_job_id"]

        monkeypatch.setattr(bot, "inspect", lambda engine: Pretend())
        with caplog.at_level(logging.ERROR):
            bot._warn_about_missing_columns()
        message = caplog.text
        assert "group_seat_lease.release_job_id is missing" in message
        assert "ALTER TABLE group_seat_lease ADD COLUMN release_job_id" in message

    def test_a_complete_schema_says_nothing(self, caplog):
        """It runs on every boot. A check that cries wolf gets filtered out of
        the log and stops being a check."""
        with caplog.at_level(logging.ERROR):
            bot._warn_about_missing_columns()
        assert "is missing" not in caplog.text


class TestTheStatementCarriesTheRightType:
    def test_a_string_column_is_not_guessed(self):
        rendered = bot._ddl_type_for("group_seat_lease", "release_job_id")
        assert "VARCHAR" in rendered.upper()

    def test_a_timestamp_column_is_not_called_varchar(self):
        """The bug the hardcoded VARCHAR was one timestamp away from: an ALTER
        that runs and creates the wrong type is worse than one that does not
        run at all."""
        rendered = bot._ddl_type_for("group_seat_lease", "reserved_at")
        assert "VARCHAR" not in rendered.upper()
        assert "TIMESTAMP" in rendered.upper() or "DATETIME" in rendered.upper()

    def test_an_unknown_column_degrades_instead_of_raising(self):
        """Formatting the advice must never swallow the warning it belongs to."""
        rendered = bot._ddl_type_for("group_seat_lease", "no_such_column")
        assert rendered
        assert "ALTER" not in rendered.upper()
