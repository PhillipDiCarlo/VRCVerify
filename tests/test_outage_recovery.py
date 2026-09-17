"""Coming back on its own after a long network outage (issue #325).

The bot sat "Up" in docker ps and offline in Discord until somebody restarted
it by hand. Four pieces: discord.py's reconnect wait is capped, a watchdog
exits a process stuck on something a restart can fix, VRChat logins that never
reached VRChat retry sooner, and database connections fail fast.
"""

import inspect
import threading
import time
from types import SimpleNamespace

import discord
import pytest
import urllib3

import bot
import heartbeat
import vrc_session as vrcs


# -------------------------------------------------------------------
# A. discord.py's reconnect wait
# -------------------------------------------------------------------
class TestTheReconnectWait:
    def test_it_never_waits_longer_than_the_cap(self):
        backoff = discord.client.ExponentialBackoff()
        delays = [backoff.delay() for _ in range(40)]
        assert max(delays) <= bot.DISCORD_RECONNECT_MAX_DELAY
        assert len(set(delays)) > 1, "the jitter stays, so reconnects do not march in step"

    def test_the_patch_still_reaches_discord_py(self):
        """A private detail of discord.py. If an upgrade stops building the
        backoff from this module-level name, the cap silently stops working,
        so this fails instead."""
        assert discord.client.ExponentialBackoff is bot._CappedBackoff
        assert "ExponentialBackoff()" in inspect.getsource(discord.Client.connect)


# -------------------------------------------------------------------
# B. The watchdog
# -------------------------------------------------------------------
UP = (True, None)
DOWN = (False, "gone")


class TestStallTracking:
    def test_a_service_that_never_comes_up_is_restarted_after_the_stall_time(self):
        """The grace at boot: counted from the start, not immediate, not never."""
        tracker = heartbeat.StallTracker(["gateway"], 900, started=0)
        assert tracker.observe({"gateway": DOWN}, now=899) is None
        part, down_for, detail = tracker.observe({"gateway": DOWN}, now=900)
        assert (part, down_for, detail) == ("gateway", 900, "gone")

    def test_one_healthy_reading_starts_the_count_again(self):
        tracker = heartbeat.StallTracker(["gateway"], 900, started=0)
        tracker.observe({"gateway": DOWN}, now=800)
        tracker.observe({"gateway": UP}, now=850)
        # Down again from 1600, so the count runs from there, not from boot.
        assert tracker.observe({"gateway": DOWN}, now=1600) is None
        assert tracker.observe({"gateway": DOWN}, now=2499) is None
        assert tracker.observe({"gateway": DOWN}, now=2500) is not None

    def test_a_part_that_flaps_is_not_a_stall(self):
        tracker = heartbeat.StallTracker(["queue"], 900, started=0)
        for now in range(0, 5000, 30):
            reading = UP if (now // 30) % 2 else DOWN
            assert tracker.observe({"queue": reading}, now=now) is None

    def test_a_part_the_probe_stopped_reporting_counts_as_down(self):
        tracker = heartbeat.StallTracker(["queue"], 60, started=0)
        assert tracker.observe({}, now=60)[0] == "queue"

    def test_parts_that_are_not_watched_never_restart_anything(self):
        """The database being down is not something a restart fixes."""
        tracker = heartbeat.StallTracker(["gateway"], 60, started=0)
        assert tracker.observe({"gateway": UP, "database": DOWN}, now=10_000) is None


def run_watchdog(probe, parts, stall, ticks, monkeypatch=None):
    """Run the real watchdog thread against a fake clock for `ticks` probes."""
    clock = {"now": 0.0}
    stalls = []

    class Done(BaseException):
        pass

    def fake_sleep(seconds):
        clock["now"] += seconds
        if clock["now"] >= ticks * heartbeat.WATCHDOG_INTERVAL:
            raise Done()

    errors = []
    original_hook = threading.excepthook
    threading.excepthook = lambda args: errors.append(args.exc_type)
    try:
        thread = heartbeat.start_watchdog(
            "svc", probe, parts, stall_seconds=stall,
            clock=lambda: clock["now"], sleep=fake_sleep,
            on_stall=lambda *found: stalls.append(found),
        )
        if thread is not None:
            thread.join(timeout=5)
    finally:
        threading.excepthook = original_hook
    return thread, stalls


class TestTheWatchdogThread:
    def test_it_reports_a_stall_once_the_time_has_passed(self):
        _, stalls = run_watchdog(lambda: {"gateway": DOWN}, ["gateway"], stall=90, ticks=5)
        assert stalls and stalls[0][:2] == ("svc", "gateway") and stalls[0][2] >= 90

    def test_a_healthy_service_is_left_alone(self):
        _, stalls = run_watchdog(lambda: {"gateway": UP}, ["gateway"], stall=90, ticks=20)
        assert stalls == []

    def test_a_probe_that_raises_counts_as_down(self):
        def probe():
            raise RuntimeError("wedged")

        _, stalls = run_watchdog(probe, ["gateway"], stall=60, ticks=4)
        assert stalls and "RuntimeError" in stalls[0][3]

    def test_zero_turns_it_off(self, monkeypatch):
        monkeypatch.setenv("WATCHDOG_STALL_SECONDS", "0")
        assert heartbeat.start_watchdog("svc", lambda: {}, ["gateway"]) is None

    def test_the_default_is_fifteen_minutes(self, monkeypatch):
        monkeypatch.delenv("WATCHDOG_STALL_SECONDS", raising=False)
        assert heartbeat.watchdog_stall_seconds() == 900
        monkeypatch.setenv("WATCHDOG_STALL_SECONDS", "nonsense")
        assert heartbeat.watchdog_stall_seconds() == 900

    def test_it_does_not_need_the_status_page(self, monkeypatch):
        """The heartbeat only runs with HEARTBEAT_DIR; the watchdog must not."""
        monkeypatch.delenv("HEARTBEAT_DIR", raising=False)
        thread, _ = run_watchdog(lambda: {"gateway": UP}, ["gateway"], stall=90, ticks=1)
        assert thread is not None


class TestLeaving:
    def test_it_exits_hard_with_code_1(self, monkeypatch):
        """os._exit, because a normal exit hangs joining the consumer threads."""
        codes = []
        monkeypatch.setattr(heartbeat.os, "_exit", lambda code: codes.append(code))
        heartbeat.exit_for_restart("discord-bot", "discord-bot", 912.4, "gateway not ready")
        assert codes == [1]

    def test_the_log_names_the_part_and_how_long(self, monkeypatch, caplog):
        monkeypatch.setattr(heartbeat.os, "_exit", lambda code: None)
        with caplog.at_level("ERROR"):
            heartbeat.exit_for_restart("discord-bot", "results-queue", 912.4, "no broker connection")
        assert "'results-queue' has been down for 912s" in caplog.text

    def test_the_bot_exits_if_bot_run_ever_returns(self):
        source = open(bot.__file__, encoding="utf-8").read()
        main = source[source.index('if __name__ == "__main__":'):]
        run_at = main.index("bot.run(DISCORD_BOT_TOKEN")
        assert main.index("finally:", run_at) < main.index("os._exit(1)", run_at)
        assert "sys.exit" not in main


class TestTheBotsWatchedParts:
    @pytest.fixture(autouse=True)
    def clean(self, monkeypatch):
        monkeypatch.setattr(bot, "_consumer_connections", {})
        monkeypatch.setattr(bot, "_bot_loop", None)

    def test_only_what_a_restart_can_fix(self):
        assert set(bot.BOT_WATCHED_PARTS) == {
            "discord-bot", "event-loop", "results-queue", "group-invite-results-queue",
        }

    def test_the_probe_never_touches_the_database(self, monkeypatch):
        """A dead connection could hold the probe for minutes, and the
        database is not something a restart fixes."""
        def boom(*args, **kwargs):
            raise AssertionError("the watchdog probe must not use the database")

        monkeypatch.setattr(bot.engine, "connect", boom)
        parts = bot._watchdog_probe()
        assert set(parts) == set(bot.BOT_WATCHED_PARTS)

    def test_a_dropped_gateway_is_down_even_though_is_ready_still_says_true(self, monkeypatch):
        """discord.py never clears is_ready() while it retries, which is why an
        outage looked like a healthy bot from inside."""
        import asyncio

        monkeypatch.setattr(bot.bot, "is_ready", lambda: True)
        monkeypatch.setattr(bot, "_gateway_connected", True)
        asyncio.run(bot.on_disconnect())
        assert bot._watchdog_probe()["discord-bot"][0] is False
        asyncio.run(bot.on_resumed())
        assert bot._watchdog_probe()["discord-bot"] == (True, None)
        asyncio.run(bot.on_disconnect())
        asyncio.run(bot.on_connect())
        assert bot._watchdog_probe()["discord-bot"] == (True, None)
        assert bot.bot.on_disconnect is bot.on_disconnect, "registered with discord.py"

    def test_discord_py_still_clears_ready_only_on_close(self):
        """The reason the listeners exist. If discord.py starts clearing it on
        a drop, is_ready() alone would do, and this says so."""
        source = inspect.getsource(discord.Client.connect)
        assert "_ready.clear" not in source

    def test_a_frozen_event_loop_is_down(self, monkeypatch):
        loop = SimpleNamespace(is_closed=lambda: False, call_soon_threadsafe=lambda cb: None)
        monkeypatch.setattr(bot, "_bot_loop", loop)
        monkeypatch.setattr(bot, "_bot_loop_seen_at", time.monotonic() - bot.BOT_LOOP_UNRESPONSIVE_SECONDS - 5)
        assert bot._watchdog_probe()["event-loop"][0] is False

    def test_a_loop_that_answers_is_up(self, monkeypatch):
        calls = []
        loop = SimpleNamespace(is_closed=lambda: False, call_soon_threadsafe=lambda cb: calls.append(cb))
        monkeypatch.setattr(bot, "_bot_loop", loop)
        monkeypatch.setattr(bot, "_bot_loop_seen_at", time.monotonic())
        assert bot._watchdog_probe()["event-loop"] == (True, None)
        assert calls == [bot._mark_bot_loop_alive], "it pings the loop every probe"

    def test_the_queue_consumers_are_watched_by_their_connection(self, monkeypatch):
        monkeypatch.setattr(bot, "_consumer_connections", {
            "results-queue": SimpleNamespace(is_open=True),
            "group-invite-results-queue": SimpleNamespace(is_open=False),
        })
        parts = bot._watchdog_probe()
        assert parts["results-queue"] == (True, None)
        assert parts["group-invite-results-queue"][0] is False


# -------------------------------------------------------------------
# C. VRChat logins that never reached VRChat
# -------------------------------------------------------------------
def account():
    return vrcs.VRChatAccount(
        username="bot", password="hunter2", user_agent="VRCVerifyTest/1.0 x@example.com",
        session_file="", gmail_user="bot@example.com", gmail_app_password="x", label="test",
    )


class TestTheLoginRetry:
    def attempt(self, monkeypatch, error):
        def failing_login(acct, load_stored_session=True):
            return None, vrcs._mark_network_failure(vrcs.classify_api_error(error), error)

        monkeypatch.setattr(vrcs, "login", failing_login)
        monkeypatch.setattr(vrcs, "fetch_status_summary", lambda force_refresh=False: None)
        session = vrcs.VRChatSession(account())
        before = time.monotonic()
        _, meta = session.attempt_login(force=True)
        return session._next_login_attempt_at - before, meta, session

    @pytest.mark.parametrize("error", [
        urllib3.exceptions.NewConnectionError(None, "Failed to establish a new connection"),
        urllib3.exceptions.MaxRetryError(None, "/auth/user"),
        ConnectionRefusedError("refused"),
        vrcs.ApiException(status=0, reason="SSL error"),
    ])
    def test_a_network_failure_retries_within_a_minute(self, monkeypatch, error):
        wait, _, _ = self.attempt(monkeypatch, error)
        assert wait <= vrcs.VRCHAT_NETWORK_RETRY_SECONDS + 1

    @pytest.mark.parametrize("error", [
        vrcs.ApiException(status=401, reason="Unauthorized"),
        vrcs.ApiException(status=429, reason="Too Many Requests"),
        vrcs.ApiException(status=503, reason="Service Unavailable"),
        ValueError("Invalid value for `bio`, must not be `None`"),
    ])
    def test_an_answer_from_vrchat_keeps_the_long_wait(self, monkeypatch, error):
        """A rejected password must not pound VRChat's login every minute."""
        wait, _, _ = self.attempt(monkeypatch, error)
        assert wait >= vrcs.VRCHAT_RELOGIN_INTERVAL_SECONDS - 1

    def test_the_marker_never_travels_in_a_result(self, monkeypatch):
        error = ConnectionRefusedError("refused")
        _, meta, session = self.attempt(monkeypatch, error)
        assert vrcs.NETWORK_FAILURE_KEY not in meta
        assert vrcs.NETWORK_FAILURE_KEY not in session.get()[1]


# -------------------------------------------------------------------
# D. Database connections that fail fast
# -------------------------------------------------------------------
class TestTheDatabaseConnection:
    def test_postgres_connections_are_checked_and_time_out(self):
        options = bot._engine_options("postgresql://user:pw@127.0.0.1/vrcverify")
        assert options["pool_pre_ping"] is True
        args = options["connect_args"]
        assert args["connect_timeout"] == 10 and args["keepalives"] == 1

    def test_sqlite_gets_none_of_it(self):
        assert bot._engine_options("sqlite:///:memory:") == {}
