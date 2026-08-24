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


class TestTheDailySeries:
    """#135 phase 1. Thirty days of counts, for a chart that cannot lie.

    The window totals beside this can only ever be one number, so a window
    straddling the collection floor has to blank entirely. A series does not
    have that problem -- it can carry real counts for the covered days and
    None for the rest -- which is the whole reason it is worth adding rather
    than deriving the chart from the three totals already there.

    Everything below is about the same three-way distinction the windows have:
    a count, a measured zero, and a day nobody measured.
    """

    def series(self, guild_id=GUILD_ID, days=30):
        return {
            entry["day"]: entry["count"]
            for entry in bot._verification_daily(guild_id, days)
        }

    def test_it_covers_the_window_one_entry_per_day(self):
        add_day(today() - timedelta(days=40), 1)  # collection well underway
        entries = bot._verification_daily(GUILD_ID, 30)

        assert len(entries) == 30
        days = [entry["day"] for entry in entries]
        assert days == sorted(days), "oldest first, so a chart reads left to right"
        assert days[-1] == today().isoformat()
        assert days[0] == (today() - timedelta(days=29)).isoformat()

    def test_a_measured_day_with_nothing_on_it_is_zero_not_absent(self):
        """The distinction the whole feature turns on. The table writes no row
        for a quiet day, so the calendar has to be rebuilt here -- and above
        the floor, a missing row means nothing happened."""
        add_day(today() - timedelta(days=10), 5)
        counts = self.series()

        assert counts[(today() - timedelta(days=10)).isoformat()] == 5
        assert counts[(today() - timedelta(days=9)).isoformat()] == 0
        assert counts[today().isoformat()] == 0

    def test_days_before_collection_started_are_not_zero(self):
        """They are None. Nothing was measured, so no number would be true --
        and a chart that drew them as zero would invent a quiet fortnight."""
        first = today() - timedelta(days=6)
        add_day(first, 3)
        counts = self.series()

        assert counts[(first - timedelta(days=1)).isoformat()] is None
        assert counts[(today() - timedelta(days=29)).isoformat()] is None
        # And the floor day itself is measured.
        assert counts[first.isoformat()] == 3

    def test_an_empty_table_measures_nothing_at_all(self):
        """Not thirty zeroes. Nothing has ever been counted anywhere."""
        counts = self.series()
        assert len(counts) == 30
        assert set(counts.values()) == {None}

    def test_a_single_day_of_data_leaves_the_rest_unmeasured(self):
        add_day(today(), 4)
        counts = self.series()

        assert counts[today().isoformat()] == 4
        assert all(
            value is None for day, value in counts.items()
            if day != today().isoformat()
        )

    def test_another_guilds_rows_are_not_counted(self):
        add_day(today(), 9, server_id=OTHER_GUILD)
        add_day(today(), 2)

        assert self.series()[today().isoformat()] == 2
        assert self.series(OTHER_GUILD)[today().isoformat()] == 9

    def test_a_quiet_guild_still_gets_zeroes_once_anyone_is_collecting(self):
        """`_collection_started()` is global on purpose. A guild that has never
        verified anybody, on a fleet that has been collecting for months, has a
        fully measured window of zeroes -- not thirty unmeasured days."""
        add_day(today() - timedelta(days=40), 1, server_id=OTHER_GUILD)
        counts = self.series()

        assert set(counts.values()) == {0}

    def test_the_series_agrees_with_the_window_beside_it(self):
        """They are read from the same table in the same request and appear in
        the same payload, so a chart summing to something other than the tile
        above it would be the page contradicting itself on screen."""
        add_day(today() - timedelta(days=40), 100)  # floor well back
        add_day(today() - timedelta(days=5), 7)
        add_day(today() - timedelta(days=1), 2)
        add_day(today(), 1)

        total = sum(v for v in self.series().values() if v is not None)
        assert total == bot._verification_windows(GUILD_ID)["last_30_days"] == 10

    def test_the_series_says_more_than_the_window_can(self):
        """The reason this exists at all.

        A window straddling the floor must blank -- one number cannot be
        part-measured. The series carries the covered days as real counts and
        the rest as None, so a server collecting for a week gets six real bars
        where the tile can only show a dash.
        """
        first = today() - timedelta(days=5)
        add_day(first, 4)

        assert bot._verification_windows(GUILD_ID)["last_30_days"] is None
        counts = self.series()
        measured = [v for v in counts.values() if v is not None]
        assert len(measured) == 6 and sum(measured) == 4

    def test_it_asks_the_database_once(self, monkeypatch):
        """Thirty days must not be thirty queries. This runs on every Overview
        render, for every admin, on a page whose whole point is loading fast
        enough to answer "is it working"."""
        calls = []
        real = bot.session_scope

        def counting_scope(*args, **kwargs):
            calls.append(1)
            return real(*args, **kwargs)

        add_day(today() - timedelta(days=40), 1)
        # Warm `_collecting_since` first. It is memoised globally and every
        # Overview render before this one has already paid for it, so counting
        # it here would measure a cold process rather than the series -- and
        # the claim being made is about the series.
        bot._collection_started()

        monkeypatch.setattr(bot, "session_scope", counting_scope)
        bot._verification_daily(GUILD_ID, 30)
        assert len(calls) == 1, f"{len(calls)} database sessions for one series"

    def test_it_stores_no_more_than_a_day_and_a_number(self):
        """The privacy ceiling, restated at the payload boundary.

        `VerificationDaily` is counts-only by design, and this is the first
        thing to expose its rows one at a time rather than summed. A series is
        finer-grained than a total, so it is worth pinning that finer does not
        mean more identifying: an entry is a date and a count, and nothing a
        date and a count can be joined to names a member.
        """
        add_day(today(), 1)
        for entry in bot._verification_daily(GUILD_ID, 3):
            assert set(entry) == {"day", "count"}


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
class FakeRole:
    """Enough of `discord.Role` for the hierarchy check: a position to compare
    on and a `managed` flag. `>`/`<` mirror the ordering `top_role > role` in
    `_overview_configuration` relies on -- higher position outranks lower."""

    def __init__(self, role_id, position=1, managed=False):
        self.id = role_id
        self.position = position
        self.managed = managed

    def __gt__(self, other):
        return self.position > other.position

    def __lt__(self, other):
        return self.position < other.position


class FakeMember:
    def __init__(self, top_role):
        self.top_role = top_role


class FakeGuild:
    def __init__(self, guild_id=GUILD_ID, member_count=42):
        self.id = int(guild_id)
        self.member_count = member_count
        self.me = None
        self.owner_id = int(OWNER_ID)
        self._roles = {}

    def get_channel_or_thread(self, _id):
        return None

    def get_role(self, role_id):
        return self._roles.get(role_id)


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
        # None, not []. The read failed, and an empty list is a chart's way of
        # saying thirty days of confidently reported nothing.
        assert counts["daily"] is None

    def test_the_payload_carries_the_daily_series(self, in_guild):
        """The series travels with the totals it has to agree with. Splitting
        it into a second call would let a chart and the tile above it be read
        a moment apart and disagree on screen."""
        make_server()
        add_day(today() - timedelta(days=40), 1)  # floor well back
        add_day(today(), 3)

        counts = read_overview()["verifications"]
        assert len(counts["daily"]) == 30
        assert counts["daily"][-1] == {"day": today().isoformat(), "count": 3}
        assert counts["known"] is True

    def test_the_series_and_the_thirty_day_tile_cannot_disagree(self, in_guild):
        """Both are read in one request from one table, and the page shows them
        within an inch of each other."""
        make_server()
        add_day(today() - timedelta(days=40), 99)
        add_day(today() - timedelta(days=3), 5)
        add_day(today(), 2)

        counts = read_overview()["verifications"]
        measured = [e["count"] for e in counts["daily"] if e["count"] is not None]
        assert sum(measured) == counts["last_30_days"] == 7

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

        The two health fields are the exception to "always a bool" rather than
        to "never an id" -- they are None when the health question does not
        apply yet (no role_id, or nothing to compare it against), which is
        also this test's job to pin, not just describe.
        """
        make_server(role_id="900000000001")

        configured = read_overview()["configured"]
        assert configured["verified_role"] is True
        assert configured["unverified_role"] is False
        health_only = {"verified_role_exists", "verified_role_assignable"}
        for key, value in configured.items():
            if key in health_only:
                assert value is None or isinstance(value, bool)
            else:
                assert isinstance(value, bool)


class TestVerifiedRoleHealth:
    """The two facts `configured` adds beyond "is a role_id stored": does that
    role still exist, and could the bot actually grant it. Three answers each
    -- yes, no, and "cannot tell" -- and the None case matters as much as the
    booleans: a role that was never set has nothing to check, and that must
    not print the same as a role that was checked and found missing."""

    def test_no_role_set_answers_neither_question(self, in_guild):
        make_server(role_id=None)
        configured = read_overview()["configured"]
        assert configured["verified_role"] is False
        assert configured["verified_role_exists"] is None
        assert configured["verified_role_assignable"] is None

    def test_a_deleted_role_exists_is_false_and_assignable_is_moot(self, in_guild):
        make_server(role_id="900000000001")
        # in_guild's FakeGuild.get_role returns nothing for an id it was never
        # given -- exactly what a deleted role looks like to the real guild.
        configured = read_overview()["configured"]
        assert configured["verified_role_exists"] is False
        assert configured["verified_role_assignable"] is None

    def test_role_exists_but_no_guild_me_cannot_answer_assignable(self, in_guild):
        make_server(role_id="900000000001")
        in_guild._roles[900000000001] = FakeRole(900000000001, position=3)
        # in_guild.me is None by default -- the bot's own member object was
        # unavailable, so hierarchy is genuinely unknown, not "no".
        configured = read_overview()["configured"]
        assert configured["verified_role_exists"] is True
        assert configured["verified_role_assignable"] is None

    def test_bots_role_above_the_verified_role_is_assignable(self, in_guild):
        make_server(role_id="900000000001")
        verified = FakeRole(900000000001, position=3)
        in_guild._roles[900000000001] = verified
        in_guild.me = FakeMember(top_role=FakeRole(0, position=10))
        assert read_overview()["configured"]["verified_role_assignable"] is True

    def test_bots_role_below_the_verified_role_is_not_assignable(self, in_guild):
        """The silent-failure case the issue calls out: the bot can see the
        role and even offer it in the picker, and still cannot grant it."""
        make_server(role_id="900000000001")
        verified = FakeRole(900000000001, position=8)
        in_guild._roles[900000000001] = verified
        in_guild.me = FakeMember(top_role=FakeRole(0, position=2))
        assert read_overview()["configured"]["verified_role_assignable"] is False

    def test_a_managed_role_is_never_assignable_even_from_above(self, in_guild):
        """An integration's own role -- a booster role, a bot role. Nobody can
        grant those by hand, whatever the hierarchy says."""
        make_server(role_id="900000000001")
        verified = FakeRole(900000000001, position=1, managed=True)
        in_guild._roles[900000000001] = verified
        in_guild.me = FakeMember(top_role=FakeRole(0, position=10))
        assert read_overview()["configured"]["verified_role_assignable"] is False

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


class TestTheFakePayloadMatchesTheRealOne:
    """`make_overview()` in test_dashboard.py says it is "shaped exactly like
    read_dashboard_overview returns". Until now that was a sentence in a
    docstring with nothing behind it.

    It matters more than an ordinary fixture, because two things build on that
    claim: every dashboard test that renders the Overview, and
    `scripts/preview_bot.py`, which imports the same helper so the local
    preview cannot drift from the bot. A field added here and forgotten there
    means a page built and reviewed against a payload production never sends.

    Phase 1 of #135 is exactly that kind of change -- it adds `daily` -- so
    this lands with it rather than after the first thing it would have caught.

    KEYS, NOT VALUES. The fake's job is to produce plausible contents, not the
    same contents; asserting equality would make it a copy of the bot rather
    than a stand-in for it.
    """

    def _real(self, in_guild):
        make_server()
        add_day(today(), 1)
        return read_overview()

    def _fake(self):
        pytest.importorskip("flask")
        # Imported here rather than at module scope on purpose: test_dashboard
        # imports the Flask app, and this module is the bot's. Keeping the
        # dependency one-way and lazy also keeps `bot` out of test_dashboard's
        # import graph, which is what stops `scripts/preview_bot.py` from
        # pulling `load_dotenv()` into the local preview -- see #162.
        from test_dashboard import make_overview

        return make_overview()

    def test_the_top_level_keys_match(self, in_guild):
        assert set(self._fake()) == set(self._real(in_guild))

    def test_the_verifications_block_matches(self, in_guild):
        """Where phase 1 added a field, and where the next phases will add
        more."""
        real = self._real(in_guild)["verifications"]
        fake = self._fake()["verifications"]
        assert set(fake) == set(real), (
            f"fake has {sorted(set(fake) - set(real))} extra, "
            f"missing {sorted(set(real) - set(fake))}"
        )

    def test_the_configured_block_matches(self, in_guild):
        """Phase 3 added `verified_role_exists` and `verified_role_assignable`
        -- the same drift this class exists to catch, one field earlier."""
        real = self._real(in_guild)["configured"]
        fake = self._fake()["configured"]
        assert set(fake) == set(real), (
            f"fake has {sorted(set(fake) - set(real))} extra, "
            f"missing {sorted(set(real) - set(fake))}"
        )

    def test_a_daily_entry_has_the_same_shape_in_both(self, in_guild):
        real = self._real(in_guild)["verifications"]["daily"]
        fake = self._fake()["verifications"]["daily"]
        assert real and fake
        assert set(real[0]) == set(fake[0]) == {"day", "count"}
        # And both are ordered oldest-first, which the chart depends on.
        for series in (real, fake):
            days = [entry["day"] for entry in series]
            assert days == sorted(days)


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
