"""Replacing the panels Discord will not let us edit (issue #320).

Every panel posted before 50e5317 (2026-08-11) went up as a slash-command
reply, which is webhook-owned. Discord takes an embed edit on one of those,
answers 200, and keeps the old embed -- so refreshing them is a silent no-op
and the only repair is to post a replacement and delete the original.

That last clause is why this file is careful. **This sweep deletes a message in
somebody else's server.** The tests below are mostly about the cases where it
must NOT do that, because those are the ones with a cost that cannot be undone:

- a panel that is editable is left completely alone
- a probe that could not read the message is skipped, never guessed
- a channel the bot cannot post in is not touched at all
- nothing happens without the trigger file
"""

import asyncio
import os
from types import SimpleNamespace

import pytest

import bot

GUILD_ID = "123456789"


def run(coro):
    return asyncio.run(coro)


def entry(server_id=GUILD_ID, channel_id="222", message_id="111", locale="en-US"):
    return {
        "server_id": server_id,
        "channel_id": channel_id,
        "message_id": message_id,
        "locale": locale,
    }


@pytest.fixture(autouse=True)
def fresh_lock(monkeypatch):
    """Each test gets its own loop via asyncio.run, so give it its own lock."""
    monkeypatch.setattr(bot, "instruction_refresh_lock", asyncio.Lock())


@pytest.fixture(autouse=True)
def unpaced(monkeypatch):
    monkeypatch.setattr(bot, "PANEL_REPLACE_SPACING", 0.0)


@pytest.fixture(autouse=True)
def messageable(monkeypatch):
    monkeypatch.setattr(
        bot.bot, "get_partial_messageable", lambda cid: SimpleNamespace(id=cid)
    )


def probe_answers(monkeypatch, answer):
    """Pin what `_panel_is_webhook_owned` says, and count the asks."""
    calls = []

    async def fake(channel, message_id):
        calls.append(message_id)
        return answer() if callable(answer) else answer

    monkeypatch.setattr(bot, "_panel_is_webhook_owned", fake)
    return calls


def replacement_records(monkeypatch, result=None, raises=None):
    """Stand in for post_dashboard_panel and record how it was called."""
    calls = []

    async def fake(guild_id, actor_id, channel_id):
        calls.append({"guild_id": guild_id, "actor_id": actor_id, "channel_id": channel_id})
        if raises is not None:
            raise raises
        return result if result is not None else {"action": "replaced"}

    monkeypatch.setattr(bot, "post_dashboard_panel", fake)
    return calls


class TestItOnlyTouchesWhatItMust:
    """The four answers, and only one of them changes anything."""

    def test_a_webhook_owned_panel_is_replaced(self, monkeypatch):
        probe_answers(monkeypatch, True)
        calls = replacement_records(monkeypatch)
        assert run(bot.replace_one_frozen_panel(entry())) == "replaced"
        assert len(calls) == 1
        assert calls[0]["guild_id"] == GUILD_ID
        assert calls[0]["channel_id"] == "222"

    def test_an_editable_panel_is_left_completely_alone(self, monkeypatch):
        """The ordinary refresh owns these and has already been here.

        Replacing one would delete a message that did not need deleting, and
        even refreshing it would cost an edit and an audit row saying nothing.
        """
        probe_answers(monkeypatch, False)
        calls = replacement_records(monkeypatch)
        assert run(bot.replace_one_frozen_panel(entry())) == "editable"
        assert calls == []

    def test_a_probe_that_could_not_tell_is_never_rounded_to_yes(self, monkeypatch):
        """`None` means the message could not be read, not that it is safe to
        replace. Guessing here is how a server ends up with two panels."""
        probe_answers(monkeypatch, None)
        calls = replacement_records(monkeypatch)
        assert run(bot.replace_one_frozen_panel(entry())) == "unreadable"
        assert calls == []

    def test_a_channel_we_cannot_post_in_is_not_touched(self, monkeypatch):
        """THE CASE THAT MUST NOT DESTROY ANYTHING.

        post_dashboard_panel checks send permissions before it posts and
        deletes the old message only after the new one is recorded, so a
        refusal here means nothing happened. This is also the set the follow-up
        DM is for: the only servers where a person has to act.
        """
        probe_answers(monkeypatch, True)
        calls = replacement_records(
            monkeypatch,
            raises=bot.SettingRejected("panel_channel", "channel_not_writable"),
        )
        assert run(bot.replace_one_frozen_panel(entry())) == "not_writable"
        assert len(calls) == 1

    def test_a_panel_in_a_thread_is_not_reported_as_a_permission_problem(
        self, monkeypatch
    ):
        """post_dashboard_panel resolves the channel out of
        `guild.text_channels`, which a thread is not in. That refusal has to
        stay distinct from a missing permission, because the follow-up DM would
        otherwise send an admin looking for a problem they do not have.
        """
        probe_answers(monkeypatch, True)
        calls = replacement_records(
            monkeypatch,
            raises=bot.SettingRejected("panel_channel", "channel_not_in_guild"),
        )
        assert run(bot.replace_one_frozen_panel(entry())) == "channel_unusable"
        assert len(calls) == 1

    @pytest.mark.parametrize(
        "bad,expected",
        [
            ({"channel_id": None}, "missing_ids"),
            ({"message_id": None}, "missing_ids"),
            ({"channel_id": "not-an-id"}, "malformed"),
        ],
    )
    def test_an_unusable_reference_is_reported_not_guessed(
        self, monkeypatch, bad, expected
    ):
        def boom(cid):
            raise ValueError("not an id")

        if bad.get("channel_id") == "not-an-id":
            monkeypatch.setattr(bot.bot, "get_partial_messageable", boom)
        probe = probe_answers(monkeypatch, True)
        calls = replacement_records(monkeypatch)
        assert run(bot.replace_one_frozen_panel(entry(**bad))) == expected
        assert calls == []
        assert probe == []

    def test_the_audit_names_something_that_is_not_a_person(self, monkeypatch):
        """An automated repair attributed to a real admin id would put words in
        somebody's mouth in their own server's audit trail."""
        probe_answers(monkeypatch, True)
        calls = replacement_records(monkeypatch)
        run(bot.replace_one_frozen_panel(entry()))
        actor = calls[0]["actor_id"]
        assert actor == bot.SYSTEM_ACTOR_ID
        assert not str(actor).isdigit(), "a numeric actor would resolve to a member"


class TestTheSweep:
    def _panels(self, monkeypatch, panels, departed=()):
        monkeypatch.setattr(bot, "load_instruction_panels", lambda *a, **k: list(panels))
        monkeypatch.setattr(
            bot, "partition_reachable_panels", lambda rows: (list(panels), list(departed))
        )

    def test_it_repairs_only_the_frozen_ones(self, monkeypatch):
        panels = [entry(server_id=str(i), message_id=str(i)) for i in range(4)]
        self._panels(monkeypatch, panels)
        # 0 and 2 are frozen, 1 and 3 are fine.
        answers = {"0": True, "1": False, "2": True, "3": False}

        async def probe(channel, message_id):
            return answers[str(message_id)]

        monkeypatch.setattr(bot, "_panel_is_webhook_owned", probe)
        calls = replacement_records(monkeypatch)
        tally = run(bot.replace_frozen_panels(reason="test"))
        assert tally == {"replaced": 2, "editable": 2}
        assert [c["guild_id"] for c in calls] == ["0", "2"]

    def test_the_cap_counts_repairs_rather_than_panels_examined(self, monkeypatch):
        """A cap eaten by panels that needed nothing would report twenty
        repairs having done none."""
        monkeypatch.setattr(bot, "PANEL_REPLACE_MAX_PER_SWEEP", 2)
        panels = [entry(server_id=str(i), message_id=str(i)) for i in range(6)]
        self._panels(monkeypatch, panels)
        # Every other one needs nothing, so a cap on panels seen would stop
        # after one repair rather than two.
        async def probe(channel, message_id):
            return int(message_id) % 2 == 0

        monkeypatch.setattr(bot, "_panel_is_webhook_owned", probe)
        calls = replacement_records(monkeypatch)
        tally = run(bot.replace_frozen_panels(reason="test"))
        assert tally["replaced"] == 2
        assert len(calls) == 2

    def test_a_rerun_finds_nothing_to_do(self, monkeypatch):
        """THE REASON THERE IS NO MARKER TABLE. A replaced panel is no longer
        webhook-owned, so the probe itself is the ledger and a second run is
        free rather than destructive."""
        panels = [entry()]
        self._panels(monkeypatch, panels)
        state = {"frozen": True}

        async def probe(channel, message_id):
            return state["frozen"]

        async def replace(guild_id, actor_id, channel_id):
            state["frozen"] = False
            return {"action": "replaced"}

        monkeypatch.setattr(bot, "_panel_is_webhook_owned", probe)
        monkeypatch.setattr(bot, "post_dashboard_panel", replace)
        assert run(bot.replace_frozen_panels(reason="first")) == {"replaced": 1}
        assert run(bot.replace_frozen_panels(reason="second")) == {"editable": 1}

    def test_one_crashing_guild_cannot_abort_the_pass(self, monkeypatch):
        panels = [entry(server_id=str(i), message_id=str(i)) for i in range(3)]
        self._panels(monkeypatch, panels)

        async def probe(channel, message_id):
            if str(message_id) == "1":
                raise RuntimeError("discord fell over")
            return True

        monkeypatch.setattr(bot, "_panel_is_webhook_owned", probe)
        calls = replacement_records(monkeypatch)
        tally = run(bot.replace_frozen_panels(reason="test"))
        assert tally["replaced"] == 2
        assert tally["error"] == 1
        assert [c["guild_id"] for c in calls] == ["0", "2"]

    def test_it_gives_up_rather_than_spinning_on_repeated_failure(self, monkeypatch):
        monkeypatch.setattr(bot, "PANEL_REPLACE_MAX_FAILURES", 2)
        panels = [entry(server_id=str(i), message_id=str(i)) for i in range(6)]
        self._panels(monkeypatch, panels)

        async def probe(channel, message_id):
            raise RuntimeError("database is gone")

        monkeypatch.setattr(bot, "_panel_is_webhook_owned", probe)
        tally = run(bot.replace_frozen_panels(reason="test"))
        assert tally["error"] == 2, "should stop at the failure ceiling, not run all six"

    def test_it_serializes_against_the_refresh_passes(self, monkeypatch):
        """A refresh editing a panel while this deletes it is a race over one
        message, and the refresh would log a success for an edit applied to
        something that is no longer there."""
        self._panels(monkeypatch, [])

        async def body():
            await bot.instruction_refresh_lock.acquire()
            try:
                task = asyncio.ensure_future(bot.replace_frozen_panels(reason="test"))
                await asyncio.sleep(0)
                assert not task.done(), "the sweep ran while the lock was held"
            finally:
                bot.instruction_refresh_lock.release()
            await task

        run(body())


class TestTheTrigger:
    def test_nothing_happens_without_the_file(self, monkeypatch, tmp_path):
        ran = []

        async def sweep(reason):
            ran.append(reason)

        monkeypatch.setattr(bot, "replace_frozen_panels", sweep)
        path = str(tmp_path / "absent.trigger")

        async def body():
            task = asyncio.ensure_future(
                bot.watch_panel_replace_trigger(path=path, poll_interval=0.01)
            )
            await asyncio.sleep(0.05)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        run(body())
        assert ran == [], "a fleet-wide delete must not start on its own"

    def test_the_file_starts_it_once_and_is_removed_first(self, monkeypatch, tmp_path):
        """Removed BEFORE the sweep, so a crash part-way leaves the rest
        unrepaired with nothing left to fire, rather than restarting it on
        every poll."""
        ran = []
        trigger = tmp_path / "go.trigger"
        trigger.write_text("")

        async def sweep(reason):
            ran.append(os.path.exists(str(trigger)))

        monkeypatch.setattr(bot, "replace_frozen_panels", sweep)

        async def body():
            task = asyncio.ensure_future(
                bot.watch_panel_replace_trigger(path=str(trigger), poll_interval=0.01)
            )
            await asyncio.sleep(0.08)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        run(body())
        assert ran == [False], "the file should be gone before the sweep starts"
        assert not trigger.exists()

    def test_it_does_not_share_a_switch_with_the_cosmetic_refresh(self):
        """Two different risks, two different files. Reaching for a refresh
        must not be able to start a fleet-wide delete."""
        assert bot.PANEL_REPLACE_TRIGGER_PATH != os.getenv(
            "INSTRUCTIONS_TRIGGER_PATH", "/tmp/update_instructions.trigger"
        )
