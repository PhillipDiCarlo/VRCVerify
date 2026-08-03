"""Unit tests for instruction panel refresh (issue #14).

Startup used to re-post every guild's panel with two sequential API calls each
(fetch_message + edit), which scaled badly past ~1000 servers. These tests pin
the replacement behaviour:
- one edit per panel, via a partial message (no channel/message fetch, no cache)
- panels refresh concurrently, bounded by INSTRUCTIONS_REFRESH_CONCURRENCY
- a 404 forgets the saved reference; a 403 leaves it alone
- overlapping refreshes are serialized by the module lock
"""

import asyncio
from types import SimpleNamespace

import discord
import pytest

import bot

GUILD_ID = "123456789"


def run(coro):
    """Run an async bot helper from a sync test (no pytest-asyncio needed)."""
    return asyncio.run(coro)


def http_error(exc_type, status, code=0, message="test"):
    """Build a discord HTTPException subclass without a real aiohttp response."""
    payload = {"code": code, "message": message}
    return exc_type(SimpleNamespace(status=status, reason="test"), payload)


def archived_thread_error():
    """The 400 Discord returns when a panel's thread has been archived."""
    return http_error(discord.HTTPException, 400, code=50083, message="Thread is archived")


def make_server(server_id, channel_id="222", message_id="111", **overrides):
    fields = dict(
        server_id=server_id,
        owner_id="42",
        role_id="1",
        instructions_locale="en-US",
        instructions_channel_id=channel_id,
        instructions_message_id=message_id,
    )
    fields.update(overrides)
    with bot.session_scope() as session:
        session.add(bot.Server(**fields))


def saved_panel(server_id):
    with bot.session_scope() as session:
        srv = session.query(bot.Server).filter_by(server_id=server_id).first()
        return srv.instructions_channel_id, srv.instructions_message_id


def entry(server_id=GUILD_ID, channel_id="222", message_id="111", locale="en-US"):
    return {
        "server_id": server_id,
        "channel_id": channel_id,
        "message_id": message_id,
        "locale": locale,
    }


@pytest.fixture
def clean_servers():
    def wipe():
        with bot.session_scope() as session:
            session.query(bot.Server).delete()
            # Successful refreshes now record a view version; clear it too so
            # stale_only tests can't inherit another test's bookkeeping.
            session.query(bot.InstructionPanelView).delete()

    wipe()
    yield
    wipe()


@pytest.fixture(autouse=True)
def fresh_lock(monkeypatch):
    """Each test gets its own loop via asyncio.run, so give it its own lock."""
    monkeypatch.setattr(bot, "instruction_refresh_lock", asyncio.Lock())


@pytest.fixture(autouse=True)
def unpaced(monkeypatch):
    """Default to effectively no pacing; TestRequestPacing opts back in."""
    monkeypatch.setattr(bot, "INSTRUCTIONS_REFRESH_RATE", 1_000_000)


@pytest.fixture(autouse=True)
def still_in_every_guild(monkeypatch):
    """Default to the bot still being a member of each guild.

    Every test in this file predates the departed-guild filter and implicitly
    assumed it; without a guild in the cache they would all now be skipped
    before any edit is attempted. TestDepartedGuilds opts back out.
    """
    monkeypatch.setattr(
        bot.bot, "get_guild", lambda gid: SimpleNamespace(id=gid, name="Test Guild")
    )


class Recorder:
    """Stands in for the Discord HTTP layer and records every edit."""

    def __init__(self, error_for=None, delay=0.0):
        self.edits = []
        self.error_for = error_for or {}
        self.delay = delay
        self.in_flight = 0
        self.peak_in_flight = 0

    def install(self, monkeypatch):
        monkeypatch.setattr(bot.bot, "get_partial_messageable", self._messageable)
        # The refresh path must not touch these; blow up loudly if it does.
        #
        # get_guild used to be in this list and deliberately is not any more.
        # The invariant worth protecting is "no API call and no dependency on
        # a cached channel or message" — that is what makes one edit per panel
        # possible. get_guild is neither: it is a dict lookup on a cache that
        # is fully populated before on_ready fires, and the refresh now needs
        # it to avoid retrying panels in guilds the bot has been removed from.
        for name in ("get_channel", "fetch_channel"):
            monkeypatch.setattr(bot.bot, name, self._forbidden_call(name))
        return self

    def _forbidden_call(self, name):
        def _fail(*args, **kwargs):
            raise AssertionError(f"refresh must not call bot.{name}()")

        return _fail

    def _messageable(self, channel_id, **kwargs):
        return SimpleNamespace(
            get_partial_message=lambda message_id: self._message(channel_id, message_id)
        )

    def _message(self, channel_id, message_id):
        async def edit(**payload):
            self.in_flight += 1
            self.peak_in_flight = max(self.peak_in_flight, self.in_flight)
            try:
                if self.delay:
                    await asyncio.sleep(self.delay)
                error = self.error_for.get(message_id)
                if error:
                    raise error
                self.edits.append((channel_id, message_id, payload))
            finally:
                self.in_flight -= 1

        return SimpleNamespace(edit=edit)


# ---------------------------------------------------------------
# Single panel: one edit, no fetches
# ---------------------------------------------------------------
class TestRefreshSinglePanel:
    def test_edits_via_partial_message_without_fetching(self, monkeypatch):
        rec = Recorder().install(monkeypatch)

        assert run(bot.refresh_instruction_panel(entry(), rebuild_embed=False)) is True

        assert len(rec.edits) == 1
        channel_id, message_id, payload = rec.edits[0]
        assert (channel_id, message_id) == (222, 111)
        assert isinstance(payload["view"], bot.VRCVerifyInstructionView)

    def test_startup_refresh_leaves_embed_untouched(self, monkeypatch):
        rec = Recorder().install(monkeypatch)

        run(bot.refresh_instruction_panel(entry(), rebuild_embed=False))

        assert "embed" not in rec.edits[0][2]

    def test_manual_refresh_rebuilds_localized_embed(self, monkeypatch):
        rec = Recorder().install(monkeypatch)

        run(bot.refresh_instruction_panel(entry(locale="de"), rebuild_embed=True))

        embed = rec.edits[0][2]["embed"]
        assert embed.title == bot.build_instructions_embed("de").title

    def test_view_uses_stored_locale(self, monkeypatch):
        rec = Recorder().install(monkeypatch)

        run(bot.refresh_instruction_panel(entry(locale="de"), rebuild_embed=False))

        assert rec.edits[0][2]["view"].locale == "de"

    @pytest.mark.parametrize(
        "bad", [{"channel_id": None}, {"message_id": None}, {"channel_id": ""}]
    )
    def test_incomplete_reference_is_skipped(self, monkeypatch, bad):
        rec = Recorder().install(monkeypatch)

        assert run(bot.refresh_instruction_panel(entry(**bad), rebuild_embed=False)) is False
        assert rec.edits == []

    def test_malformed_ids_do_not_raise(self, monkeypatch):
        rec = Recorder().install(monkeypatch)

        result = run(
            bot.refresh_instruction_panel(entry(channel_id="not-an-id"), rebuild_embed=False)
        )

        assert result is False
        assert rec.edits == []


# ---------------------------------------------------------------
# Failure handling
# ---------------------------------------------------------------
class TestRefreshFailures:
    def test_404_forgets_saved_panel(self, monkeypatch, clean_servers):
        make_server(GUILD_ID)
        Recorder(error_for={111: http_error(discord.NotFound, 404)}).install(monkeypatch)

        assert run(bot.refresh_instruction_panel(entry(), rebuild_embed=False)) is False
        assert saved_panel(GUILD_ID) == (None, None)

    def test_403_keeps_saved_panel(self, monkeypatch, clean_servers):
        make_server(GUILD_ID)
        Recorder(error_for={111: http_error(discord.Forbidden, 403)}).install(monkeypatch)

        assert run(bot.refresh_instruction_panel(entry(), rebuild_embed=False)) is False
        assert saved_panel(GUILD_ID) == ("222", "111")

    def test_unexpected_error_keeps_saved_panel(self, monkeypatch, clean_servers):
        make_server(GUILD_ID)
        Recorder(error_for={111: RuntimeError("boom")}).install(monkeypatch)

        assert run(bot.refresh_instruction_panel(entry(), rebuild_embed=False)) is False
        assert saved_panel(GUILD_ID) == ("222", "111")

    def test_archived_thread_keeps_saved_panel(self, monkeypatch, clean_servers):
        # Unarchiving the thread revives the panel, so the reference is still good.
        make_server(GUILD_ID)
        Recorder(error_for={111: archived_thread_error()}).install(monkeypatch)

        assert run(bot.refresh_instruction_panel(entry(), rebuild_embed=False)) is False
        assert saved_panel(GUILD_ID) == ("222", "111")

    def test_archived_thread_logs_one_line_not_a_traceback(
        self, monkeypatch, clean_servers, caplog
    ):
        make_server(GUILD_ID)
        Recorder(error_for={111: archived_thread_error()}).install(monkeypatch)

        with caplog.at_level("WARNING"):
            run(bot.refresh_instruction_panel(entry(), rebuild_embed=False))

        assert "50083" in caplog.text
        assert "Thread is archived" in caplog.text
        assert "Traceback" not in caplog.text

    def test_rejected_edit_does_not_stop_the_fleet(self, monkeypatch, clean_servers):
        for i in range(4):
            make_server(str(1000 + i), channel_id=str(2000 + i), message_id=str(3000 + i))
        rec = Recorder(error_for={3001: archived_thread_error()})
        rec.install(monkeypatch)

        run(bot.refresh_all_instruction_panels(rebuild_embed=False, reason="test"))

        assert len(rec.edits) == 3
        assert saved_panel("1001") == ("2001", "3001")


# ---------------------------------------------------------------
# Fleet-wide refresh
# ---------------------------------------------------------------
class TestRefreshAllPanels:
    def test_refreshes_every_saved_panel(self, monkeypatch, clean_servers):
        for i in range(25):
            make_server(str(1000 + i), channel_id=str(2000 + i), message_id=str(3000 + i))
        rec = Recorder().install(monkeypatch)

        run(bot.refresh_all_instruction_panels(rebuild_embed=False, reason="test"))

        assert len(rec.edits) == 25

    def test_servers_without_a_panel_are_ignored(self, monkeypatch, clean_servers):
        make_server("1", channel_id="10", message_id="20")
        make_server("2", channel_id=None, message_id=None)
        rec = Recorder().install(monkeypatch)

        run(bot.refresh_all_instruction_panels(rebuild_embed=False, reason="test"))

        assert [e[1] for e in rec.edits] == [20]

    def test_edits_run_concurrently_up_to_the_cap(self, monkeypatch, clean_servers):
        monkeypatch.setattr(bot, "INSTRUCTIONS_REFRESH_CONCURRENCY", 5)
        for i in range(20):
            make_server(str(1000 + i), channel_id=str(2000 + i), message_id=str(3000 + i))
        rec = Recorder(delay=0.01).install(monkeypatch)

        run(bot.refresh_all_instruction_panels(rebuild_embed=False, reason="test"))

        assert len(rec.edits) == 20
        # The whole point of the change: more than one edit in flight at a time,
        # but never more than the configured cap.
        assert rec.peak_in_flight == 5

    def test_one_bad_panel_does_not_stop_the_rest(self, monkeypatch, clean_servers):
        for i in range(5):
            make_server(str(1000 + i), channel_id=str(2000 + i), message_id=str(3000 + i))
        rec = Recorder(error_for={3002: http_error(discord.NotFound, 404)})
        rec.install(monkeypatch)

        run(bot.refresh_all_instruction_panels(rebuild_embed=False, reason="test"))

        assert len(rec.edits) == 4
        assert saved_panel("1002") == (None, None)

    def test_empty_fleet_is_a_no_op(self, monkeypatch, clean_servers):
        rec = Recorder().install(monkeypatch)

        run(bot.refresh_all_instruction_panels(rebuild_embed=False, reason="test"))

        assert rec.edits == []

    def test_overlapping_refreshes_are_serialized(self, monkeypatch, clean_servers):
        monkeypatch.setattr(bot, "INSTRUCTIONS_REFRESH_CONCURRENCY", 10)
        for i in range(4):
            make_server(str(1000 + i), channel_id=str(2000 + i), message_id=str(3000 + i))
        rec = Recorder(delay=0.01).install(monkeypatch)

        async def both():
            await asyncio.gather(
                bot.refresh_all_instruction_panels(rebuild_embed=False, reason="a"),
                bot.refresh_all_instruction_panels(rebuild_embed=True, reason="b"),
            )

        run(both())

        assert len(rec.edits) == 8
        # Serialized: the second pass never overlaps the first, so in-flight
        # edits stay within a single pass's worth of work.
        assert rec.peak_in_flight == 4

    def test_manual_trigger_rebuilds_embeds(self, monkeypatch, clean_servers):
        make_server(GUILD_ID)
        rec = Recorder().install(monkeypatch)

        run(bot.update_all_instruction_messages())

        assert "embed" in rec.edits[0][2]


# ---------------------------------------------------------------
# Startup migration to the persistent view (tier 2)
# ---------------------------------------------------------------
class TestStartupMigration:
    @staticmethod
    def startup():
        return bot.refresh_all_instruction_panels(
            rebuild_embed=False, reason="startup", stale_only=True
        )

    def test_first_boot_migrates_and_records_every_panel(self, monkeypatch, clean_servers):
        for i in range(5):
            make_server(str(1000 + i), channel_id=str(2000 + i), message_id=str(3000 + i))
        rec = Recorder().install(monkeypatch)

        run(self.startup())

        assert len(rec.edits) == 5
        with bot.session_scope() as session:
            assert session.query(bot.InstructionPanelView).count() == 5

    def test_second_boot_touches_nothing(self, monkeypatch, clean_servers):
        # This is the tier 2 payoff: a fully migrated fleet costs zero API calls.
        for i in range(5):
            make_server(str(1000 + i), channel_id=str(2000 + i), message_id=str(3000 + i))
        rec = Recorder().install(monkeypatch)
        run(self.startup())
        assert len(rec.edits) == 5

        second = Recorder().install(monkeypatch)
        run(self.startup())

        assert second.edits == []

    def test_only_failed_panels_are_retried(self, monkeypatch, clean_servers):
        for i in range(5):
            make_server(str(1000 + i), channel_id=str(2000 + i), message_id=str(3000 + i))
        # 3002 is forbidden, 3004 sits in an archived thread; neither migrates.
        first = Recorder(
            error_for={
                3002: http_error(discord.Forbidden, 403),
                3004: archived_thread_error(),
            }
        )
        first.install(monkeypatch)
        run(self.startup())
        assert len(first.edits) == 3

        second = Recorder().install(monkeypatch)
        run(self.startup())

        assert sorted(e[1] for e in second.edits) == [3002, 3004]

    def test_a_recovered_panel_stops_being_retried(self, monkeypatch, clean_servers):
        make_server(GUILD_ID)
        Recorder(error_for={111: http_error(discord.Forbidden, 403)}).install(monkeypatch)
        run(self.startup())

        # Admin restores permissions; next boot succeeds and records the version.
        recovered = Recorder().install(monkeypatch)
        run(self.startup())
        assert len(recovered.edits) == 1

        quiet = Recorder().install(monkeypatch)
        run(self.startup())
        assert quiet.edits == []

    def test_a_bumped_version_re_migrates_the_fleet(self, monkeypatch, clean_servers):
        for i in range(3):
            make_server(str(1000 + i), channel_id=str(2000 + i), message_id=str(3000 + i))
        Recorder().install(monkeypatch)
        run(self.startup())

        monkeypatch.setattr(bot, "INSTRUCTIONS_VIEW_VERSION", bot.INSTRUCTIONS_VIEW_VERSION + 1)
        rec = Recorder().install(monkeypatch)
        run(self.startup())

        assert len(rec.edits) == 3

    def test_manual_trigger_still_refreshes_migrated_panels(self, monkeypatch, clean_servers):
        for i in range(4):
            make_server(str(1000 + i), channel_id=str(2000 + i), message_id=str(3000 + i))
        Recorder().install(monkeypatch)
        run(self.startup())

        rec = Recorder().install(monkeypatch)
        run(bot.update_all_instruction_messages())

        # The trigger path exists to push copy/locale changes, so it must not
        # skip panels merely because their buttons are current.
        assert len(rec.edits) == 4

    def test_404_during_migration_clears_both_records(self, monkeypatch, clean_servers):
        make_server(GUILD_ID)
        Recorder(error_for={111: http_error(discord.NotFound, 404)}).install(monkeypatch)

        run(self.startup())

        assert saved_panel(GUILD_ID) == (None, None)
        with bot.session_scope() as session:
            assert session.query(bot.InstructionPanelView).count() == 0


# ---------------------------------------------------------------
# Blast radius: one bad guild must not take down the pass
# ---------------------------------------------------------------
class TestFleetIsolation:
    """The startup pass is run_once, so anything escaping a single panel would
    strand every remaining guild until the process restarts. Nothing may
    propagate out of a worker.
    """

    @staticmethod
    def exploding_view(fail_on_locale, monkeypatch):
        real = bot.VRCVerifyInstructionView

        def build(locale):
            if locale == fail_on_locale:
                raise RuntimeError("locale table is corrupt")
            return real(locale)

        monkeypatch.setattr(bot, "VRCVerifyInstructionView", build)

    def test_view_construction_failure_does_not_abort_the_fleet(
        self, monkeypatch, clean_servers
    ):
        make_server("a", channel_id="1", message_id="10")
        make_server("b", channel_id="2", message_id="20", instructions_locale="de")
        make_server("c", channel_id="3", message_id="30")
        rec = Recorder().install(monkeypatch)
        self.exploding_view("de", monkeypatch)

        run(bot.refresh_all_instruction_panels(rebuild_embed=False, reason="test"))

        assert sorted(e[1] for e in rec.edits) == [10, 30]

    def test_embed_construction_failure_is_contained(self, monkeypatch, clean_servers):
        make_server("a", channel_id="1", message_id="10")
        make_server("b", channel_id="2", message_id="20")
        rec = Recorder().install(monkeypatch)
        real = bot.build_instructions_embed
        seen = []

        def build(locale):
            seen.append(locale)
            if len(seen) == 1:
                raise ValueError("bad embed")
            return real(locale)

        monkeypatch.setattr(bot, "build_instructions_embed", build)

        run(bot.refresh_all_instruction_panels(rebuild_embed=True, reason="test"))

        assert len(rec.edits) == 1

    def test_summary_still_logs_when_a_worker_crashes(
        self, monkeypatch, clean_servers, caplog
    ):
        make_server("a", channel_id="1", message_id="10")
        make_server("b", channel_id="2", message_id="20", instructions_locale="de")
        Recorder().install(monkeypatch)
        self.exploding_view("de", monkeypatch)

        with caplog.at_level("INFO"):
            run(bot.refresh_all_instruction_panels(rebuild_embed=False, reason="test"))

        assert "Instruction panel refresh (test): 1/2" in caplog.text

    def test_an_escaped_error_is_reported_not_silently_counted(
        self, monkeypatch, clean_servers, caplog
    ):
        make_server("a", channel_id="1", message_id="10", instructions_locale="de")
        Recorder().install(monkeypatch)
        self.exploding_view("de", monkeypatch)

        with caplog.at_level("ERROR"):
            run(bot.refresh_all_instruction_panels(rebuild_embed=False, reason="test"))

        assert "locale table is corrupt" in caplog.text

    @staticmethod
    def escaping_worker(monkeypatch, bad_server):
        """Simulate an error raised outside refresh_instruction_panel's own guard.

        Its internal try only wraps the edit; anything before that (id parsing,
        pacing) escapes to the gather, which is what return_exceptions defends.
        """
        real = bot.refresh_instruction_panel

        async def flaky(entry, rebuild_embed):
            if entry["server_id"] == bad_server:
                raise RuntimeError("escaped the worker guard")
            return await real(entry, rebuild_embed)

        monkeypatch.setattr(bot, "refresh_instruction_panel", flaky)

    def test_an_escape_from_the_worker_does_not_abort_the_pass(
        self, monkeypatch, clean_servers
    ):
        make_server("a", channel_id="1", message_id="10")
        make_server("b", channel_id="2", message_id="20")
        make_server("c", channel_id="3", message_id="30")
        rec = Recorder().install(monkeypatch)
        self.escaping_worker(monkeypatch, "a")

        run(bot.refresh_all_instruction_panels(rebuild_embed=False, reason="test"))

        assert sorted(e[1] for e in rec.edits) == [20, 30]

    def test_summary_survives_an_escaped_error(self, monkeypatch, clean_servers, caplog):
        make_server("a", channel_id="1", message_id="10")
        make_server("b", channel_id="2", message_id="20")
        Recorder().install(monkeypatch)
        self.escaping_worker(monkeypatch, "a")

        with caplog.at_level("INFO"):
            run(bot.refresh_all_instruction_panels(rebuild_embed=False, reason="test"))

        assert "1/2 updated" in caplog.text
        assert "1 crashed" in caplog.text

    def test_cancellation_is_not_swallowed(self, monkeypatch, clean_servers):
        # return_exceptions=True captures CancelledError too; a shutdown must
        # still propagate rather than be logged as a per-panel failure.
        make_server("a", channel_id="1", message_id="10")
        Recorder().install(monkeypatch)

        async def cancel_one(entry, rebuild_embed):
            raise asyncio.CancelledError()

        monkeypatch.setattr(bot, "refresh_instruction_panel", cancel_one)

        with pytest.raises(asyncio.CancelledError):
            run(bot.refresh_all_instruction_panels(rebuild_embed=False, reason="test"))


# ---------------------------------------------------------------
# Release rollback
# ---------------------------------------------------------------
class TestVersionRollback:
    def test_a_newer_recorded_panel_is_re_migrated(self, monkeypatch, clean_servers):
        # Deploy v2, migrate, then roll back to v1. The panel carries v2
        # custom_ids this process never registered, so skipping it would leave
        # the buttons permanently dead.
        make_server(GUILD_ID)
        bot.record_panel_view_version(GUILD_ID, bot.INSTRUCTIONS_VIEW_VERSION + 1)
        rec = Recorder().install(monkeypatch)

        run(
            bot.refresh_all_instruction_panels(
                rebuild_embed=False, reason="rollback", stale_only=True
            )
        )

        assert len(rec.edits) == 1

    def test_rollback_rewrites_the_version_downwards(self, monkeypatch, clean_servers):
        make_server(GUILD_ID)
        bot.record_panel_view_version(GUILD_ID, bot.INSTRUCTIONS_VIEW_VERSION + 5)
        Recorder().install(monkeypatch)

        run(
            bot.refresh_all_instruction_panels(
                rebuild_embed=False, reason="rollback", stale_only=True
            )
        )

        assert bot.load_instruction_panels()[0]["view_version"] == bot.INSTRUCTIONS_VIEW_VERSION

    def test_exact_match_is_still_skipped(self, monkeypatch, clean_servers):
        make_server(GUILD_ID)
        bot.record_panel_view_version(GUILD_ID)
        rec = Recorder().install(monkeypatch)

        run(
            bot.refresh_all_instruction_panels(
                rebuild_embed=False, reason="test", stale_only=True
            )
        )

        assert rec.edits == []


# ---------------------------------------------------------------
# Request pacing (keeps us off Discord's global 50 req/s ceiling)
# ---------------------------------------------------------------
class TestRequestPacing:
    """Slot arithmetic is asserted against recorded sleeps rather than a wall
    clock: asyncio timers can fire up to one clock tick early (~15.6ms on
    Windows), which makes direct elapsed-time thresholds flaky.
    """

    @staticmethod
    def recorded_delays(monkeypatch, per_second, calls):
        delays = []

        async def fake_sleep(delay):
            delays.append(delay)

        monkeypatch.setattr(asyncio, "sleep", fake_sleep)

        async def scenario():
            pacer = bot.RequestPacer(per_second=per_second)
            for _ in range(calls):
                await pacer.wait()

        run(scenario())
        return delays

    def test_first_call_does_not_wait(self, monkeypatch):
        assert self.recorded_delays(monkeypatch, per_second=10, calls=1) == []

    def test_each_further_call_waits_one_more_slot(self, monkeypatch):
        delays = self.recorded_delays(monkeypatch, per_second=10, calls=5)

        # 10/s == a 100ms slot; the clock barely moves because sleeps are faked,
        # so the Nth caller waits roughly N slots.
        assert len(delays) == 4
        for i, delay in enumerate(delays, start=1):
            assert abs(delay - i * 0.1) < 0.02, delays

    def test_rate_sets_the_slot_width(self, monkeypatch):
        assert bot.RequestPacer(per_second=25).min_interval == pytest.approx(0.04)
        assert bot.RequestPacer(per_second=50).min_interval == pytest.approx(0.02)

    def test_concurrent_waiters_each_get_their_own_slot(self, monkeypatch):
        delays = []

        async def fake_sleep(delay):
            delays.append(delay)

        monkeypatch.setattr(asyncio, "sleep", fake_sleep)

        async def scenario():
            pacer = bot.RequestPacer(per_second=100)
            await asyncio.gather(*(pacer.wait() for _ in range(10)))

        run(scenario())

        # No two callers were handed the same slot.
        assert len(delays) == 9
        assert len(set(delays)) == 9

    def test_fleet_refresh_is_paced(self, monkeypatch, clean_servers):
        monkeypatch.setattr(bot, "INSTRUCTIONS_REFRESH_RATE", 100)
        monkeypatch.setattr(bot, "INSTRUCTIONS_REFRESH_CONCURRENCY", 50)
        for i in range(20):
            make_server(str(1000 + i), channel_id=str(2000 + i), message_id=str(3000 + i))
        rec = Recorder().install(monkeypatch)

        async def scenario():
            began = asyncio.get_running_loop().time()
            await bot.refresh_all_instruction_panels(rebuild_embed=False, reason="test")
            return asyncio.get_running_loop().time() - began

        elapsed = run(scenario())

        assert len(rec.edits) == 20
        # Unpaced, 20 instant edits finish in ~1ms. At 100/s they need ~190ms;
        # the floor here is loose enough to absorb early-firing timers.
        assert elapsed >= 0.1


# ---------------------------------------------------------------
# Guilds the bot has been removed from (issue #62)
# ---------------------------------------------------------------
class TestDepartedGuilds:
    """Panels in guilds we've left can never be edited.

    probe_instruction_panel deliberately keeps the saved reference on 403,
    because a revoked permission can be restored. A kick cannot, so without a
    filter those rows are retried on every boot forever and the count only
    grows. In production this was 114 of them, and the noise buried the real
    permission failures the refresh exists to surface.
    """

    def only_in(self, monkeypatch, *guild_ids):
        present = {str(g) for g in guild_ids}

        def get_guild(gid):
            if str(gid) in present:
                return SimpleNamespace(id=gid, name="Test Guild")
            return None

        monkeypatch.setattr(bot.bot, "get_guild", get_guild)

    def test_departed_guilds_are_never_edited(self, monkeypatch, clean_servers):
        make_server("111", channel_id="1", message_id="10")
        make_server("222", channel_id="2", message_id="20")
        rec = Recorder().install(monkeypatch)
        self.only_in(monkeypatch, "111")

        run(bot.refresh_all_instruction_panels(rebuild_embed=False, reason="test"))

        # Not merely "failed gracefully" — no API call was made at all.
        assert [e[1] for e in rec.edits] == [10]

    def test_the_skipped_count_is_reported(self, monkeypatch, clean_servers, caplog):
        make_server("111", channel_id="1", message_id="10")
        make_server("222", channel_id="2", message_id="20")
        Recorder().install(monkeypatch)
        self.only_in(monkeypatch, "111")

        with caplog.at_level("INFO"):
            run(bot.refresh_all_instruction_panels(rebuild_embed=False, reason="test"))

        assert "1/1" in caplog.text
        assert "1 skipped" in caplog.text

    def test_all_departed_still_logs_the_count(
        self, monkeypatch, clean_servers, caplog
    ):
        # The 0/114 case: nothing to refresh, but the number must stay visible
        # rather than vanishing into "nothing needs refreshing".
        make_server("111", channel_id="1", message_id="10")
        Recorder().install(monkeypatch)
        self.only_in(monkeypatch)

        with caplog.at_level("INFO"):
            run(bot.refresh_all_instruction_panels(rebuild_embed=False, reason="test"))

        assert "1 skipped" in caplog.text

    def test_saved_reference_is_left_alone(self, monkeypatch, clean_servers):
        # Skipping is not deleting: a re-invite should find its panel intact,
        # and a cold cache must not cost anyone their configuration.
        make_server("111", channel_id="1", message_id="10")
        Recorder().install(monkeypatch)
        self.only_in(monkeypatch)

        run(bot.refresh_all_instruction_panels(rebuild_embed=False, reason="test"))

        assert saved_panel("111") == ("1", "10")

    def test_unparseable_ids_are_not_assumed_departed(self, monkeypatch, clean_servers):
        # "I couldn't tell" is not "we left". Let it through so the existing
        # malformed-id handling reports it instead of skipping it silently.
        make_server("not-a-guild-id", channel_id="1", message_id="10")
        rec = Recorder().install(monkeypatch)
        self.only_in(monkeypatch)

        run(bot.refresh_all_instruction_panels(rebuild_embed=False, reason="test"))

        assert [e[1] for e in rec.edits] == [10]


class TestGuildRemoveCleanup:
    def test_panel_and_onboarding_are_cleared(self, clean_servers):
        make_server("111", channel_id="1", message_id="10")
        bot.record_panel_view_version("111")
        bot.record_guild_onboarding("111")

        run(bot.on_guild_remove(SimpleNamespace(id=111, name="Gone")))

        assert saved_panel("111") == (None, None)
        with bot.session_scope() as session:
            assert (
                session.query(bot.GuildOnboarding).filter_by(server_id="111").first()
                is None
            )

    def test_the_server_row_and_role_config_survive(self, clean_servers):
        """Being removed is often temporary.

        Forcing an admin back through /vrcverify_setup on re-invite costs them
        far more than a stale row costs us.
        """
        make_server("111", channel_id="1", message_id="10", role_id="999")

        run(bot.on_guild_remove(SimpleNamespace(id=111, name="Gone")))

        with bot.session_scope() as session:
            srv = session.query(bot.Server).filter_by(server_id="111").first()
            assert srv is not None
            assert srv.role_id == "999"

    def test_failure_is_swallowed(self, monkeypatch, clean_servers):
        def boom(_):
            raise RuntimeError("db down")

        monkeypatch.setattr(bot, "forget_instruction_panel", boom)
        run(bot.on_guild_remove(SimpleNamespace(id=111, name="Gone")))  # must not raise
