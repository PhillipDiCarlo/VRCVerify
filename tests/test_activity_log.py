"""Unit tests for the premium verification activity log (issue #55).

The log sits directly in the verification path, so most of what matters here is
what it must *not* do: never break a verification, never ping the member it is
reporting on, never leak the VRChat identity behind a Discord account, and
never grow without bound when a channel is unreachable.
"""

import asyncio
from types import SimpleNamespace

import discord
import pytest

import bot

GUILD_ID = "987654321"
OWNER_ID = "77"
CHANNEL_ID = "555000222"
SKU_ID = 555000111
OLD_ID = 100
NEW_ID = 5000


# Captured before any test can monkeypatch asyncio.sleep out from under us.
REAL_SLEEP = asyncio.sleep


def run(coro):
    return asyncio.run(coro)


def run_and_drain(coro):
    """Run `coro`, then cancel anything it left behind (e.g. _delayed_cleanup)."""

    async def scenario():
        result = await coro
        await asyncio.sleep(0)
        pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        return result

    return asyncio.run(scenario())


class FakeEntitlement:
    def __init__(self, sku_id=SKU_ID, expired=False, deleted=False):
        self.sku_id = sku_id
        self.deleted = deleted
        self.guild_id = int(GUILD_ID)
        self._expired = expired

    def is_expired(self):
        return self._expired


def entitlements_api(*items):
    def _entitlements(**kwargs):
        async def generate():
            for item in items:
                yield item

        return generate()

    return _entitlements


def counting_entitlements_api(*items):
    """Records calls, so a test can assert the lookup never happened."""
    calls = []

    def _entitlements(**kwargs):
        calls.append(kwargs)
        return entitlements_api(*items)(**kwargs)

    return _entitlements, calls


def make_server(server_id=GUILD_ID, row_id=OLD_ID, **overrides):
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


@pytest.fixture(autouse=True)
def clean_db():
    def wipe():
        with bot.session_scope() as session:
            session.query(bot.Server).delete()
            session.query(bot.User).delete()
            session.query(bot.VerificationLogChannel).delete()

    wipe()
    bot.verification_log_buffer.clear()
    bot.premium_status_cache.clear()
    yield
    wipe()
    bot.verification_log_buffer.clear()
    bot.premium_status_cache.clear()


@pytest.fixture
def enforced(monkeypatch):
    monkeypatch.setattr(bot, "PREMIUM_SKU_ID", SKU_ID)
    monkeypatch.setattr(bot, "PREMIUM_ENFORCED", True)
    bot.premium_status_cache.clear()


# ---------------------------------------------------------------
# Buffer
# ---------------------------------------------------------------
class TestVerificationLogBuffer:
    def test_add_and_drain(self):
        buf = bot.VerificationLogBuffer(max_per_guild=10)
        buf.add("g1", "a")
        buf.add("g1", "b")
        buf.add("g2", "c")
        assert buf.drain() == {"g1": (["a", "b"], 0), "g2": (["c"], 0)}

    def test_drain_empties(self):
        buf = bot.VerificationLogBuffer(max_per_guild=10)
        buf.add("g1", "a")
        buf.drain()
        assert buf.drain() == {}

    def test_overflow_drops_oldest_and_counts(self):
        # If entries must be lost, the recent ones are what an admin is looking
        # at, so the old ones go.
        buf = bot.VerificationLogBuffer(max_per_guild=3)
        for i in range(6):
            buf.add("g1", f"line{i}")
        lines, dropped = buf.drain()["g1"]
        assert lines == ["line3", "line4", "line5"]
        assert dropped == 3

    def test_guilds_are_independent(self):
        buf = bot.VerificationLogBuffer(max_per_guild=1)
        buf.add("g1", "a")
        buf.add("g1", "b")
        buf.add("g2", "c")
        drained = buf.drain()
        assert drained["g2"] == (["c"], 0)


# ---------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------
class TestBuildLogLine:
    def test_contains_a_suppressed_mention_and_a_discord_timestamp(self):
        line = bot.build_log_line(bot.LOG_OUTCOME_VERIFIED, 42, "en-US")
        assert "<@42>" in line
        # <t:...:f> renders in each reader's own timezone, so no date format
        # needs translating twelve times.
        assert "<t:" in line and ":f>" in line

    def test_localized(self):
        english = bot.build_log_line(bot.LOG_OUTCOME_NOT_18, 42, "en-US")
        german = bot.build_log_line(bot.LOG_OUTCOME_NOT_18, 42, "de")
        assert english != german

    @pytest.mark.parametrize(
        "outcome",
        [bot.LOG_OUTCOME_VERIFIED, bot.LOG_OUTCOME_ROLE_FAILED, bot.LOG_OUTCOME_NOT_18],
    )
    def test_never_contains_vrchat_identity(self, outcome):
        """The privacy boundary, asserted rather than trusted to review.

        The bot knows the Discord-to-VRChat link because it must. Writing it
        into a server channel publishes it to everyone who can read there.
        """
        for locale in bot.LANGUAGE_CODES:
            line = bot.build_log_line(outcome, 42, locale)
            assert "usr_" not in line
            assert "{display_name}" not in line


class TestChunkLogLines:
    def test_short_lines_become_one_message(self):
        assert bot.chunk_log_lines(["a", "b", "c"]) == ["a\nb\nc"]

    def test_splits_at_the_discord_limit(self):
        messages = bot.chunk_log_lines(["x" * 700] * 5)
        assert len(messages) > 1
        assert all(len(m) <= bot.DISCORD_MESSAGE_MAX_LEN for m in messages)

    def test_a_single_oversized_line_is_truncated_not_looped(self):
        messages = bot.chunk_log_lines(["y" * 5000])
        assert len(messages) == 1
        assert len(messages[0]) == bot.DISCORD_MESSAGE_MAX_LEN

    def test_empty(self):
        assert bot.chunk_log_lines([]) == []


# ---------------------------------------------------------------
# assign_role hooks
# ---------------------------------------------------------------
@pytest.fixture
def harness(monkeypatch):
    """Minimal guild/member so assign_role can run end to end."""
    verified = SimpleNamespace(id=1, name="Verified")
    events = SimpleNamespace(added=[], forbidden_on_add=False)

    class FakeMember:
        id = 42
        roles = []

        async def add_roles(self, role):
            if events.forbidden_on_add:
                raise discord.Forbidden(
                    SimpleNamespace(status=403, reason="x"), {"code": 50013}
                )
            events.added.append(role.name)

        async def remove_roles(self, role):
            pass

        async def edit(self, nick=None):
            pass

        async def send(self, content):
            pass

    guild = SimpleNamespace(id=int(GUILD_ID), name="Test Server", roles=[verified])

    async def fake_fetch(g, user_id):
        return FakeMember()

    async def noop_dm(*args, **kwargs):
        pass

    monkeypatch.setattr(bot.bot, "get_guild", lambda gid: guild)
    monkeypatch.setattr(bot, "fetch_member_cached", fake_fetch)
    monkeypatch.setattr(bot, "dm_localized", noop_dm)
    monkeypatch.setattr(bot, "dm_role_assignment_failure", noop_dm)
    return events


def set_log_channel(server_id=GUILD_ID, channel_id=CHANNEL_ID):
    bot.set_log_channel(server_id, channel_id)


def buffered():
    return bot.verification_log_buffer.pending(GUILD_ID)


class TestAssignRoleLogging:
    def test_success_is_logged(self, enforced, monkeypatch, harness):
        make_server(row_id=NEW_ID)
        set_log_channel()
        monkeypatch.setattr(bot.bot, "entitlements", entitlements_api(FakeEntitlement()))

        run_and_drain(bot.assign_role("42", True, GUILD_ID))

        assert harness.added == ["Verified"]
        assert len(buffered()) == 1
        assert "<@42>" in buffered()[0]

    def test_role_assignment_failure_is_logged(self, enforced, monkeypatch, harness):
        """The silent failure this feature exists to surface.

        A bot whose role sits below the verified role fails today with the
        member getting a DM and the server seeing nothing at all.
        """
        make_server(row_id=NEW_ID)
        set_log_channel()
        harness.forbidden_on_add = True
        monkeypatch.setattr(bot.bot, "entitlements", entitlements_api(FakeEntitlement()))

        run_and_drain(bot.assign_role("42", True, GUILD_ID))

        # Asserted against the rendered template for this exact outcome, so a
        # copy edit cannot quietly turn this into a check that passes for any
        # of the three outcomes.
        expected = bot.build_log_line(bot.LOG_OUTCOME_ROLE_FAILED, 42, "en-US")
        assert len(buffered()) == 1
        # Timestamps differ by the second, so compare everything before them.
        assert buffered()[0].split("<t:")[0] == expected.split("<t:")[0]

    def test_not_18_is_logged(self, enforced, monkeypatch, harness):
        make_server(row_id=NEW_ID)
        set_log_channel()
        monkeypatch.setattr(bot.bot, "entitlements", entitlements_api(FakeEntitlement()))

        run_and_drain(bot.assign_role("42", False, GUILD_ID))

        assert harness.added == []
        assert len(buffered()) == 1

    def test_free_server_logs_nothing(self, enforced, monkeypatch, harness):
        make_server(row_id=NEW_ID)
        set_log_channel()
        # No entitlement: configured, but not entitled.
        monkeypatch.setattr(bot.bot, "entitlements", entitlements_api())

        run_and_drain(bot.assign_role("42", True, GUILD_ID))

        assert harness.added == ["Verified"]  # verification itself is unaffected
        assert buffered() == []

    def test_free_server_logs_nothing_on_the_not_18_path_either(
        self, enforced, monkeypatch, harness
    ):
        # The not-18+ branch resolves premium separately from the branch above,
        # so it needs its own coverage or the gate can rot on one side only.
        make_server(row_id=NEW_ID)
        set_log_channel()
        monkeypatch.setattr(bot.bot, "entitlements", entitlements_api())

        run_and_drain(bot.assign_role("42", False, GUILD_ID))
        assert buffered() == []

    def test_grandfathering_does_not_unlock_it(self, enforced, monkeypatch, harness):
        # Brand new feature, so an old server has nothing to preserve.
        make_server(row_id=OLD_ID)
        set_log_channel()
        monkeypatch.setattr(bot.bot, "entitlements", entitlements_api())

        run_and_drain(bot.assign_role("42", True, GUILD_ID))
        assert buffered() == []

    def test_no_channel_configured_skips_the_entitlement_lookup(
        self, enforced, monkeypatch, harness
    ):
        """The hot path for the overwhelming majority of guilds.

        Counted rather than raised from the stub: guild_has_premium catches
        Exception and fails open, so a raise would be swallowed.
        """
        make_server(row_id=NEW_ID)
        api, calls = counting_entitlements_api(FakeEntitlement())
        monkeypatch.setattr(bot.bot, "entitlements", api)

        run_and_drain(bot.assign_role("42", False, GUILD_ID))

        assert buffered() == []
        assert calls == []

    def test_the_locale_is_reused_not_re_queried(self, enforced, monkeypatch, harness):
        """assign_role already read instructions_locale off the server row.

        Re-deriving it here meant a second database query for every logged
        verification, on the hot path.
        """
        make_server(row_id=NEW_ID, instructions_locale="de")
        set_log_channel()
        monkeypatch.setattr(bot.bot, "entitlements", entitlements_api(FakeEntitlement()))

        def boom(*args, **kwargs):
            raise AssertionError("locale should come from the caller")

        monkeypatch.setattr(bot, "get_server_locale_code", boom)

        run_and_drain(bot.assign_role("42", True, GUILD_ID))
        assert buffered()[0].split("<t:")[0] == bot.build_log_line(
            bot.LOG_OUTCOME_VERIFIED, 42, "de"
        ).split("<t:")[0]

    def test_a_broken_logger_never_breaks_verification(
        self, enforced, monkeypatch, harness
    ):
        make_server(row_id=NEW_ID)
        set_log_channel()
        monkeypatch.setattr(bot.bot, "entitlements", entitlements_api(FakeEntitlement()))

        def boom(*args, **kwargs):
            raise RuntimeError("locale table on fire")

        monkeypatch.setattr(bot, "build_log_line", boom)

        run_and_drain(bot.assign_role("42", True, GUILD_ID))
        assert harness.added == ["Verified"]


# ---------------------------------------------------------------
# Flushing
# ---------------------------------------------------------------
@pytest.fixture
def channel_spy(monkeypatch):
    sent = []

    class FakeChannel:
        def __init__(self, error=None):
            self.error = error

        async def send(self, content, allowed_mentions=None):
            if self.error:
                raise self.error
            sent.append(SimpleNamespace(content=content, mentions=allowed_mentions))

    state = SimpleNamespace(sent=sent, channel=FakeChannel())
    monkeypatch.setattr(bot.bot, "get_partial_messageable", lambda cid: state.channel)
    monkeypatch.setattr(bot.bot, "get_guild", lambda gid: SimpleNamespace(
        id=int(GUILD_ID), name="Test Server", preferred_locale="en-US"
    ))
    return state


def http_error(exc_type, status, code=0):
    return exc_type(SimpleNamespace(status=status, reason="test"), {"code": code})


class TestFlushGuildLog:
    def test_posts_with_mentions_suppressed(self, channel_spy):
        """Without this every verification pings the member being reported."""
        make_server()
        set_log_channel()
        assert run(bot.flush_guild_log(GUILD_ID, ["<@42> verified"], 0)) == "ok"
        assert len(channel_spy.sent) == 1
        mentions = channel_spy.sent[0].mentions
        assert mentions is not None
        assert mentions.users is False and mentions.roles is False
        assert mentions.everyone is False

    def test_dropped_entries_are_admitted(self, channel_spy):
        make_server()
        set_log_channel()
        run(bot.flush_guild_log(GUILD_ID, ["a"], 7))
        assert "7" in channel_spy.sent[0].content

    def test_the_dropped_notice_comes_first(self, channel_spy):
        """The dropped entries are the OLDEST, so the note belongs at the top.

        Appended, it reads as though the gap came after everything above it,
        which puts an admin reconstructing a timeline exactly backwards.
        """
        make_server()
        set_log_channel()
        run(bot.flush_guild_log(GUILD_ID, ["first", "second"], 3))
        body = channel_spy.sent[0].content
        assert body.index("3") < body.index("first")

    def test_no_channel_configured_is_a_no_op(self, channel_spy):
        make_server()
        assert run(bot.flush_guild_log(GUILD_ID, ["a"], 0)) == "no_channel"
        assert channel_spy.sent == []

    def test_deleted_channel_clears_the_reference(self, channel_spy):
        make_server()
        set_log_channel()
        channel_spy.channel.error = http_error(discord.NotFound, 404)
        assert run(bot.flush_guild_log(GUILD_ID, ["a"], 0)) == "gone"
        # Retrying a channel that no longer exists forever is pointless.
        assert bot.load_log_channel_id(GUILD_ID) is None

    def test_forbidden_keeps_the_reference(self, channel_spy):
        # Permissions get restored; deleted channels do not come back.
        make_server()
        set_log_channel()
        channel_spy.channel.error = http_error(discord.Forbidden, 403)
        assert run(bot.flush_guild_log(GUILD_ID, ["a"], 0)) == "forbidden"
        assert bot.load_log_channel_id(GUILD_ID) == CHANNEL_ID

    def test_other_http_errors_are_swallowed(self, channel_spy):
        make_server()
        set_log_channel()
        channel_spy.channel.error = http_error(discord.HTTPException, 500)
        assert run(bot.flush_guild_log(GUILD_ID, ["a"], 0)) == "http_error"
        assert bot.load_log_channel_id(GUILD_ID) == CHANNEL_ID

    def test_long_batches_are_split(self, channel_spy):
        make_server()
        set_log_channel()
        run(bot.flush_guild_log(GUILD_ID, ["x" * 700] * 5, 0))
        assert len(channel_spy.sent) > 1
        assert all(
            len(m.content) <= bot.DISCORD_MESSAGE_MAX_LEN for m in channel_spy.sent
        )

    def test_a_supplied_channel_id_skips_the_query(self, channel_spy, monkeypatch):
        # The flush task batches the lookup, so per-guild reads are wasteful.
        make_server()

        def boom(_):
            raise AssertionError("should not query per guild")

        monkeypatch.setattr(bot, "load_log_channel_id", boom)
        assert (
            run(bot.flush_guild_log(GUILD_ID, ["a"], 0, channel_id=CHANNEL_ID)) == "ok"
        )


class TestLoadLogChannels:
    def test_batches_several_guilds(self):
        for gid in ("1", "2", "3"):
            bot.set_log_channel(gid, "chan" + gid)
        found = bot.load_log_channels(["1", "2", "3", "404"])
        assert found == {"1": "chan1", "2": "chan2", "3": "chan3"}

    def test_empty_input(self):
        assert bot.load_log_channels([]) == {}

    def test_db_failure_is_swallowed(self, monkeypatch):
        def boom():
            raise RuntimeError("db down")

        monkeypatch.setattr(bot, "session_scope", boom)
        assert bot.load_log_channels(["1"]) == {}


class TestFlushTask:
    """The loop around flush_guild_log, which previously had no coverage."""

    # Distinctive so the stub can tell the task's own loop sleep apart from
    # RequestPacer's inter-guild spacing. Counting both would cancel the cycle
    # partway through the guild list, which silently made a multi-guild test
    # pass for the wrong reason.
    INTERVAL = 3600

    def run_one_cycle(self, monkeypatch):
        """Run exactly one iteration of the flush task, then stop it.

        REAL_SLEEP is captured at import, not here: a test that calls this
        twice would otherwise have the second call capture the first call's
        stub as its "real" sleep, inherit its exhausted counter, and cancel
        before draining anything.
        """
        calls = {"n": 0}

        async def fake_sleep(seconds):
            if seconds == self.INTERVAL:
                calls["n"] += 1
                if calls["n"] > 1:
                    raise asyncio.CancelledError()
            # Pacer spacing passes straight through so tests don't really wait.
            await REAL_SLEEP(0)

        monkeypatch.setattr(bot.asyncio, "sleep", fake_sleep)
        try:
            run(bot.verification_log_flush_task(interval_seconds=self.INTERVAL))
        except asyncio.CancelledError:
            pass

    def test_drains_and_posts(self, channel_spy, monkeypatch):
        make_server()
        set_log_channel()
        bot.verification_log_buffer.add(GUILD_ID, "entry one")
        self.run_one_cycle(monkeypatch)
        assert len(channel_spy.sent) == 1
        assert "entry one" in channel_spy.sent[0].content
        assert bot.verification_log_buffer.pending(GUILD_ID) == []

    def test_a_failed_send_is_reported_in_the_next_batch(
        self, channel_spy, monkeypatch
    ):
        """The silent-gap failure: the batch left the buffer before we tried.

        Without carrying the count forward, a revoked permission produces an
        unexplained hole in the log and nothing ever says so.
        """
        make_server()
        set_log_channel()
        bot.verification_log_buffer.add(GUILD_ID, "lost one")
        bot.verification_log_buffer.add(GUILD_ID, "lost two")
        channel_spy.channel.error = http_error(discord.Forbidden, 403)
        self.run_one_cycle(monkeypatch)
        assert channel_spy.sent == []

        # Permission restored; the next batch has to account for the gap.
        channel_spy.channel.error = None
        bot.verification_log_buffer.add(GUILD_ID, "later entry")
        self.run_one_cycle(monkeypatch)
        assert "2" in channel_spy.sent[0].content

    def test_a_deleted_channel_is_not_reported_as_lost_entries(
        self, channel_spy, monkeypatch
    ):
        # There is no working log for entries to be missing from.
        make_server()
        set_log_channel()
        bot.verification_log_buffer.add(GUILD_ID, "entry")
        channel_spy.channel.error = http_error(discord.NotFound, 404)
        self.run_one_cycle(monkeypatch)
        assert bot.load_log_channel_id(GUILD_ID) is None

    def test_one_guild_failing_does_not_stop_the_others(
        self, channel_spy, monkeypatch
    ):
        make_server("1", row_id=1)
        make_server("2", row_id=2)
        # Numeric: flush_guild_log does int(channel_id) and treats a
        # non-numeric value as a malformed reference to be cleared.
        bot.set_log_channel("1", "111")
        bot.set_log_channel("2", "222")
        bot.verification_log_buffer.add("1", "from one")
        bot.verification_log_buffer.add("2", "from two")

        original = bot.flush_guild_log

        async def flaky(guild_id, lines, dropped, channel_id=None, locale=None):
            if guild_id == "1":
                raise RuntimeError("boom")
            return await original(guild_id, lines, dropped, channel_id, locale)

        monkeypatch.setattr(bot, "flush_guild_log", flaky)
        self.run_one_cycle(monkeypatch)
        assert len(channel_spy.sent) == 1
        assert "from two" in channel_spy.sent[0].content

    def test_the_task_uses_the_batched_lookup(self, channel_spy, monkeypatch):
        """The task must hand the channel id down, not let each flush re-query.

        Asserted here rather than on flush_guild_log alone: that function
        happily falls back to a per-guild read, so testing it in isolation
        cannot tell whether the task is actually passing anything.
        """
        make_server()
        set_log_channel()
        bot.verification_log_buffer.add(GUILD_ID, "entry")

        def boom(_):
            raise AssertionError("per-guild query inside the flush loop")

        monkeypatch.setattr(bot, "load_log_channel_id", boom)
        self.run_one_cycle(monkeypatch)
        assert len(channel_spy.sent) == 1

    def test_nothing_buffered_posts_nothing(self, channel_spy, monkeypatch):
        make_server()
        set_log_channel()
        self.run_one_cycle(monkeypatch)
        assert channel_spy.sent == []


# ---------------------------------------------------------------
# Storage
# ---------------------------------------------------------------
class TestLogChannelStorage:
    def test_set_and_read(self):
        bot.set_log_channel(GUILD_ID, CHANNEL_ID)
        assert bot.load_log_channel_id(GUILD_ID) == CHANNEL_ID

    def test_update_replaces(self):
        bot.set_log_channel(GUILD_ID, CHANNEL_ID)
        bot.set_log_channel(GUILD_ID, "999")
        assert bot.load_log_channel_id(GUILD_ID) == "999"

    def test_clear(self):
        bot.set_log_channel(GUILD_ID, CHANNEL_ID)
        bot.set_log_channel(GUILD_ID, None)
        assert bot.load_log_channel_id(GUILD_ID) is None

    def test_clearing_when_unset_is_fine(self):
        bot.set_log_channel(GUILD_ID, None)
        assert bot.load_log_channel_id(GUILD_ID) is None

    def test_unknown_guild(self):
        assert bot.load_log_channel_id("404") is None

    def test_db_failure_is_swallowed(self, monkeypatch):
        def boom():
            raise RuntimeError("db down")

        monkeypatch.setattr(bot, "session_scope", boom)
        assert bot.load_log_channel_id(GUILD_ID) is None
