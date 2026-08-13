"""Unit tests for the Overview page's data (issue #86).

Two halves, and the interesting one is the second.

The rollup is ordinary bookkeeping: one row per guild per day, incremented on
each completed verification, and never allowed to break a verification if the
write fails.

The reading is where the care goes, because this page has three ways to show a
non-number and they mean different things:

- **blank** -- the window reaches back further than the rollup has been
  collecting, so no figure would be true
- **0** -- the window is fully covered and nothing happened in it, which is a
  real answer and often the useful one
- **unknown** -- the bot could not answer at all

Flattening any pair of those tells an admin something untrue. A blank shown as
0 invents a quiet week; a 0 shown as blank hides a server where the panel is up
and nobody is using it. Most of what follows pins those apart.

The privacy ceiling is tested too: the table stores counts, and a test here
fails if it ever grows a column that could name a member.
"""

from datetime import date, timedelta

import pytest

import bot


GUILD_ID = "987654321"
OTHER_GUILD = "123456789"
OWNER_ID = "77"


def today():
    return bot.datetime.now(bot.timezone.utc).date()


def make_server(server_id=GUILD_ID, row_id=1, **overrides):
    fields = dict(
        id=row_id,
        server_id=server_id,
        owner_id=OWNER_ID,
        role_id="1",
        instructions_locale="en-US",
    )
    fields.update(overrides)
    with bot.session_scope() as session:
        session.add(bot.Server(**fields))


def add_day(day, count, server_id=GUILD_ID):
    with bot.session_scope() as session:
        session.add(
            bot.VerificationDaily(server_id=server_id, day=day, count=count)
        )


def counts_for(server_id=GUILD_ID):
    with bot.session_scope() as session:
        return {
            row.day: row.count
            for row in session.query(bot.VerificationDaily).filter_by(
                server_id=server_id
            )
        }


@pytest.fixture(autouse=True)
def clean_db():
    def wipe():
        with bot.session_scope() as session:
            session.query(bot.Server).delete()
            session.query(bot.VerificationDaily).delete()

    wipe()
    # Memoised across calls, and the tests move the table under it constantly.
    bot._collecting_since = None
    yield
    wipe()
    bot._collecting_since = None


# -------------------------------------------------------------------
# Writing
# -------------------------------------------------------------------
class TestTheRollupWrite:
    def test_the_first_verification_of_the_day_creates_the_row(self):
        bot._record_verification_day(GUILD_ID)
        assert counts_for() == {today(): 1}

    def test_later_ones_increment_the_same_row(self):
        for _ in range(3):
            bot._record_verification_day(GUILD_ID)
        assert counts_for() == {today(): 3}

    def test_guilds_are_counted_separately(self):
        bot._record_verification_day(GUILD_ID)
        bot._record_verification_day(OTHER_GUILD)
        bot._record_verification_day(OTHER_GUILD)
        assert counts_for(GUILD_ID) == {today(): 1}
        assert counts_for(OTHER_GUILD) == {today(): 2}

    def test_an_existing_row_from_an_earlier_day_is_left_alone(self):
        yesterday = today() - timedelta(days=1)
        add_day(yesterday, 5)
        bot._record_verification_day(GUILD_ID)
        assert counts_for() == {yesterday: 5, today(): 1}

    def test_a_database_failure_is_swallowed(self, monkeypatch):
        """A verification has already happened by the time this runs.

        The member is verified, their role is assigned, and the only thing at
        stake is a number on a page. Raising here would turn a lost count into
        a failed verification.
        """

        def boom():
            raise RuntimeError("database is on fire")

        monkeypatch.setattr(bot, "session_scope", boom)
        bot._record_verification_day(GUILD_ID)  # must not raise

    def test_no_guild_id_writes_nothing(self):
        bot._record_verification_day("")
        assert counts_for() == {}


class TestTheRollupIsNotAffectedByTheMilestoneColumns:
    """The two counters share a call site and nothing else.

    `record_guild_verification` gives up early when `servers` is missing the
    columns the milestone DM needs. A deployment that never ran that ALTER must
    still collect history, or the Overview would show empty windows forever
    with nothing to say why.
    """

    def test_a_missing_milestone_column_still_records_the_day(self, monkeypatch):
        make_server()
        monkeypatch.setattr(bot, "server_has_column", lambda name: False)
        bot.asyncio.run(bot.record_guild_verification(GUILD_ID, None))
        assert counts_for() == {today(): 1}

    def test_a_guild_with_no_server_row_still_records_the_day(self, monkeypatch):
        """`record_guild_verification` returns early for an unknown server.

        The rollup runs before that check, which is deliberate: a verification
        happened, and the count is about the verification rather than about the
        row's existence.
        """
        monkeypatch.setattr(bot, "server_has_column", lambda name: True)
        bot.asyncio.run(bot.record_guild_verification(GUILD_ID, None))
        assert counts_for() == {today(): 1}


# -------------------------------------------------------------------
# Reading
# -------------------------------------------------------------------
class TestTheWindows:
    """Blank, zero and a count are three different answers."""

    def test_a_covered_window_sums_its_days(self):
        add_day(today(), 2)
        add_day(today() - timedelta(days=3), 5)
        add_day(today() - timedelta(days=20), 9)
        # Collection started well before any of them.
        add_day(today() - timedelta(days=100), 1, server_id=OTHER_GUILD)

        windows = bot._verification_windows(GUILD_ID)
        assert windows["today"] == 2
        assert windows["last_7_days"] == 7
        assert windows["last_30_days"] == 16

    def test_a_covered_window_with_nothing_in_it_is_zero_not_blank(self):
        """The single most important assertion in this file.

        A server whose panel is up and whose members are not using it shows 0.
        Rendering that as blank would hide the exact problem an admin opened
        this page to find.
        """
        add_day(today() - timedelta(days=200), 1, server_id=OTHER_GUILD)

        windows = bot._verification_windows(GUILD_ID)
        assert windows == {"today": 0, "last_7_days": 0, "last_30_days": 0}

    def test_a_window_reaching_before_collection_started_is_blank(self):
        """Not zero. Nothing was measured, so no number would be true."""
        add_day(today(), 4)  # collection starts today

        windows = bot._verification_windows(GUILD_ID)
        assert windows["today"] == 4
        assert windows["last_7_days"] is None
        assert windows["last_30_days"] is None

    def test_an_empty_table_blanks_every_window_including_today(self):
        assert bot._verification_windows(GUILD_ID) == {
            "today": None,
            "last_7_days": None,
            "last_30_days": None,
        }

    def test_the_boundary_day_counts_as_covered(self):
        """A 7-day window starting exactly on the first collected day is real."""
        first = today() - timedelta(days=6)
        add_day(first, 3)

        windows = bot._verification_windows(GUILD_ID)
        assert windows["last_7_days"] == 3
        assert windows["last_30_days"] is None

    def test_days_outside_the_window_are_not_counted(self):
        add_day(today() - timedelta(days=100), 50)
        add_day(today(), 1)

        windows = bot._verification_windows(GUILD_ID)
        assert windows["today"] == 1
        assert windows["last_7_days"] == 1
        assert windows["last_30_days"] == 1

    def test_another_guilds_rows_are_never_counted(self):
        add_day(today() - timedelta(days=200), 1, server_id=OTHER_GUILD)
        add_day(today(), 99, server_id=OTHER_GUILD)
        add_day(today(), 2)

        assert bot._verification_windows(GUILD_ID)["today"] == 2


class TestCollectionStart:
    def test_it_is_global_not_per_guild(self):
        """A guild's own first row is the wrong question.

        Per guild, `MIN(day)` is the day that server first verified somebody --
        so a guild whose first verification was yesterday would have its 30-day
        window reported as unknowable, when the truthful answer is that the
        window is covered and the count is small.
        """
        add_day(today() - timedelta(days=90), 1, server_id=OTHER_GUILD)
        add_day(today(), 2)

        assert bot._collection_started() == today() - timedelta(days=90)
        # Covered, because collection has been running for 90 days -- even
        # though this guild only appeared today.
        assert bot._verification_windows(GUILD_ID)["last_30_days"] == 2

    def test_an_empty_table_has_no_start(self):
        assert bot._collection_started() is None

    def test_it_is_not_cached_as_none(self):
        """An empty table must not freeze the answer at "never".

        Memoising None would mean the first verification after a deploy never
        starts the clock, and every window stays blank until a restart.
        """
        assert bot._collection_started() is None
        add_day(today(), 1)
        assert bot._collection_started() == today()


# -------------------------------------------------------------------
# The payload
# -------------------------------------------------------------------
class FakeGuild:
    def __init__(self, guild_id=GUILD_ID, member_count=42):
        self.id = int(guild_id)
        self.member_count = member_count
        self.me = None
        self.owner_id = int(OWNER_ID)

    def get_channel_or_thread(self, _id):
        return None


@pytest.fixture
def in_guild(monkeypatch):
    guild = FakeGuild()
    monkeypatch.setattr(
        bot.bot, "get_guild", lambda gid: guild if int(gid) == int(GUILD_ID) else None
    )
    return guild


def read_overview(guild_id=GUILD_ID):
    return bot.asyncio.run(bot.read_dashboard_overview(guild_id))


class TestTheOverviewPayload:
    def test_it_reports_the_member_count_from_the_gateway(self, in_guild):
        make_server()
        assert read_overview()["member_count"] == 42

    def test_a_guild_the_bot_cannot_see_is_unanswerable(self, in_guild):
        """None becomes a 503, rather than a page of zeroes."""
        assert read_overview(OTHER_GUILD) is None

    def test_the_windows_travel_with_a_known_flag(self, in_guild):
        make_server()
        add_day(today() - timedelta(days=90), 1, server_id=OTHER_GUILD)
        add_day(today(), 3)

        counts = read_overview()["verifications"]
        assert counts["known"] is True
        assert counts["today"] == 3
        assert counts["last_30_days"] == 3
        assert counts["collecting_since"] == (today() - timedelta(days=90)).isoformat()

    def test_an_unreadable_rollup_is_unknown_not_zero(self, in_guild, monkeypatch):
        """`known: false` is the page's "Couldn't check".

        Distinct from a blank window, which is a successful read of a question
        the data cannot answer. This is the read itself failing.
        """
        make_server()

        def boom(_guild_id):
            raise RuntimeError("no database")

        monkeypatch.setattr(bot, "_verification_windows", boom)

        counts = read_overview()["verifications"]
        assert counts["known"] is False
        assert counts["today"] is None
        assert counts["last_7_days"] is None
        assert counts["last_30_days"] is None

    def test_a_missing_verification_count_column_omits_the_total(
        self, in_guild, monkeypatch
    ):
        """Not zero: a server with thousands of verifications would be lied to."""
        make_server()
        monkeypatch.setattr(bot, "server_has_column", lambda name: False)
        assert read_overview()["verifications"]["total"] is None

    def test_the_total_comes_from_the_running_counter(self, in_guild, monkeypatch):
        make_server(verification_count=417)
        monkeypatch.setattr(bot, "server_has_column", lambda name: True)
        assert read_overview()["verifications"]["total"] == 417

    def test_configuration_is_booleans_never_ids(self, in_guild):
        """The Overview says whether things are set, not what they are set to.

        Ids belong on Settings. Repeating them here would be a second place for
        them to be wrong, and this page has no control to change them.
        """
        make_server(role_id="900000000001")

        configured = read_overview()["configured"]
        assert configured["verified_role"] is True
        assert configured["unverified_role"] is False
        for value in configured.values():
            assert isinstance(value, bool)

    def test_no_member_identifier_appears_anywhere_in_the_payload(self, in_guild):
        """The privacy ceiling, pinned end to end.

        Nothing in this payload can name a member, because nothing behind it
        stores one. If a future change adds a per-person field to the rollup,
        this is where it should hurt.
        """
        make_server()
        add_day(today(), 2)

        payload = read_overview()
        rendered = repr(payload)
        for forbidden in ("discord_id", "vrc_user_id", "user_id", "member_id"):
            assert forbidden not in rendered


class TestTheRollupStoresNoPeople:
    def test_the_table_has_exactly_three_columns(self):
        """A guild, a day, and a count.

        This test exists to be annoying. Adding a column here is adding a
        record of who verified when to a product whose entire job is to answer
        "is this person over 18" and then forget -- so it should require
        deleting an assertion that says so out loud.
        """
        columns = {column.name for column in bot.VerificationDaily.__table__.columns}
        assert columns == {"server_id", "day", "count"}

    def test_one_row_per_guild_per_day(self):
        primary = {
            column.name for column in bot.VerificationDaily.__table__.primary_key
        }
        assert primary == {"server_id", "day"}
