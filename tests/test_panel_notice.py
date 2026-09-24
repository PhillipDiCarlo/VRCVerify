"""The DM to owners whose frozen panel the sweep could not replace (issue #320).

A message to a person cannot be unsent, and this one tells its reader two
specific things about their server. So most of this file is about when it must
NOT go out:

- a panel the sweep can still repair is not their problem to fix
- a panel that cannot be proven frozen gets no message claiming it is
- a server that has been told is never told twice, even across runs and crashes
- the record is written before the message, and a failed write sends nothing
"""

import asyncio
import os
from datetime import datetime, timezone
from types import SimpleNamespace

import discord
import pytest

import bot

GUILD_ID = "123456789"
BEFORE = datetime(2026, 8, 5, 12, 0, tzinfo=timezone.utc)   # frozen by date alone
AFTER = datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc)   # only the probe can say


def run(coro):
    return asyncio.run(coro)


def snowflake(when):
    return str(discord.utils.time_snowflake(when))


def perms(view=True, history=True, send=True, embed=True):
    return SimpleNamespace(
        view_channel=view, read_message_history=history, send_messages=send, embed_links=embed
    )


def guild_with(channel_perms, name="Test Server", channel_name="verify"):
    channel = SimpleNamespace(id=222, name=channel_name, permissions_for=lambda me: channel_perms)
    return SimpleNamespace(
        id=int(GUILD_ID),
        name=name,
        me=object(),
        get_channel_or_thread=lambda cid: channel if cid == 222 else None,
    ), channel


def entry(server_id=GUILD_ID, posted=BEFORE, channel_id="222"):
    return {"server_id": server_id, "channel_id": channel_id, "message_id": snowflake(posted), "locale": "en-US"}


class Member:
    def __init__(self, raises=None):
        self.sent = []
        self.raises = raises

    async def send(self, text):
        if self.raises is not None:
            raise self.raises
        self.sent.append(text)


def closed_dms():
    return discord.Forbidden(
        SimpleNamespace(status=403, reason="Forbidden"),
        {"code": 50007, "message": "Cannot send messages to this user"},
    )


def notified_rows():
    with bot.session_scope() as session:
        return [
            (row.server_id, row.old_value)
            for row in session.query(bot.DashboardAudit)
            .filter_by(actor_id=bot.SYSTEM_ACTOR_ID, field="instructions_panel")
            .all()
        ]


@pytest.fixture(autouse=True)
def clean_audit():
    def wipe():
        with bot.session_scope() as session:
            session.query(bot.DashboardAudit).delete()

    wipe()
    yield
    wipe()


@pytest.fixture(autouse=True)
def fresh_lock(monkeypatch):
    monkeypatch.setattr(bot, "instruction_refresh_lock", asyncio.Lock())


@pytest.fixture(autouse=True)
def unpaced(monkeypatch):
    monkeypatch.setattr(bot, "PANEL_NOTICE_SPACING", 0.0)


@pytest.fixture
def dashboard(monkeypatch):
    monkeypatch.setattr(bot, "DASHBOARD_URL", "https://dashboard.vrcverify.com")
    monkeypatch.setattr(
        bot, "dashboard_guild_url", lambda gid: f"https://dashboard.vrcverify.com/guild/{gid}/settings"
    )


def probe_answers(monkeypatch, answer):
    calls = []

    async def fake(channel, message_id):
        calls.append(message_id)
        return answer

    monkeypatch.setattr(bot, "_panel_is_webhook_owned", fake)
    return calls


class TestTheMessage:
    def _text(self, **overrides):
        values = dict(
            server="Test Server",
            channel="verify",
            posted=BEFORE,
            permissions=["Send Messages", "Embed Links"],
            link="https://dashboard.vrcverify.com/guild/1/settings",
            dashboard="https://dashboard.vrcverify.com",
        )
        values.update(overrides)
        return bot.frozen_panel_notice_text(**values)

    def test_it_says_what_was_approved(self):
        text = self._text()
        assert text.startswith("**Your VRCVerify panel in Test Server is out of date**")
        assert "Members can still use it to verify." in text
        assert "it's missing **Send Messages and Embed Links** in **#verify**" in text
        assert "1. Open your server's settings on the dashboard: https://dashboard.vrcverify.com/guild/1/settings" in text
        assert "3. Once the new panel is up, delete the old one if it's still there." in text
        assert "the VRCVerify dashboard at https://dashboard.vrcverify.com lets you manage" in text

    def test_it_names_the_button_the_admin_will_actually_see(self):
        """settings.html labels it this for any server with a panel recorded,
        and every server this goes to has one."""
        assert "Press **Repost or move panel**" in self._text()

    def test_it_states_the_panels_own_date_rather_than_before_august(self):
        """The fix reached main on August 11, so a panel posted on the 5th is
        frozen too, and "before August" would be false for its owner."""
        text = self._text(posted=BEFORE)
        assert "It was posted on August 5, 2026," in text
        assert "before August" not in text

    @pytest.mark.parametrize(
        "names,expected",
        [
            (["Embed Links"], "**Embed Links**"),
            (["Send Messages", "Embed Links"], "**Send Messages and Embed Links**"),
            (
                ["View Channel", "Read Message History", "Send Messages", "Embed Links"],
                "**View Channel, Read Message History, Send Messages and Embed Links**",
            ),
        ],
    )
    def test_it_names_exactly_the_missing_permissions(self, names, expected):
        assert expected in self._text(permissions=names)

    def test_a_server_name_cannot_rewrite_the_formatting(self):
        text = self._text(server="**Loud** _Server_")
        assert "**Loud** _Server_ is out of date" not in text
        assert "\\*\\*Loud\\*\\*" in text

    def test_it_fits_in_one_message_at_the_longest_names_discord_allows(self):
        text = self._text(
            server="S" * 100,
            channel="c" * 100,
            permissions=[label for _, label in bot.PANEL_REPAIR_PERMISSIONS],
        )
        assert len(text) <= 2000


class TestWhoGetsIt:
    def test_a_panel_the_sweep_can_repair_is_nobodys_chore(self, monkeypatch):
        guild, _ = guild_with(perms())
        monkeypatch.setattr(bot.bot, "get_guild", lambda gid: guild)
        probe = probe_answers(monkeypatch, True)
        outcome, context = run(bot.assess_frozen_panel_notice(entry()))
        assert outcome == "sweep_can_replace"
        assert context is None
        assert probe == []

    def test_a_post_cutoff_panel_with_every_permission_is_not_called_repairable(
        self, monkeypatch
    ):
        """#328: lumped in with the frozen ones, these read as 221 servers
        waiting for a repair when most of them had nothing wrong."""
        guild, _ = guild_with(perms())
        monkeypatch.setattr(bot.bot, "get_guild", lambda gid: guild)
        probe = probe_answers(monkeypatch, True)
        outcome, context = run(bot.assess_frozen_panel_notice(entry(posted=AFTER)))
        assert outcome == "has_permissions"
        assert context is None
        # No probe is needed to say "probably healthy", so none is spent.
        assert probe == []

    def test_a_panel_posted_before_the_cutoff_needs_no_probe_to_prove_it(self, monkeypatch):
        """The servers this exists for are the ones where the bot cannot read
        the channel, so it cannot fetch the panel to ask. The message id is the
        proof instead."""
        guild, _ = guild_with(perms(view=False, history=False))
        monkeypatch.setattr(bot.bot, "get_guild", lambda gid: guild)
        probe = probe_answers(monkeypatch, None)
        outcome, context = run(bot.assess_frozen_panel_notice(entry(posted=BEFORE)))
        assert outcome == "notify"
        assert context["missing"] == ["View Channel", "Read Message History"]
        assert probe == []

    def test_after_the_cutoff_an_unreadable_panel_gets_no_claim_about_it(self, monkeypatch):
        """Posted after the change that made panels editable, so it may be
        editable, and the bot cannot read it to find out. Telling that owner
        their panel is frozen would be a guess."""
        guild, _ = guild_with(perms(history=False))
        monkeypatch.setattr(bot.bot, "get_guild", lambda gid: guild)
        probe = probe_answers(monkeypatch, True)
        outcome, _ = run(bot.assess_frozen_panel_notice(entry(posted=AFTER)))
        assert outcome == "unprovable"
        assert probe == []

    @pytest.mark.parametrize(
        "answer,expected",
        [(True, "notify"), (False, "editable"), (None, "unreadable")],
    )
    def test_after_the_cutoff_a_readable_panel_is_asked(self, monkeypatch, answer, expected):
        guild, _ = guild_with(perms(send=False))
        monkeypatch.setattr(bot.bot, "get_guild", lambda gid: guild)
        probe = probe_answers(monkeypatch, answer)
        outcome, _ = run(bot.assess_frozen_panel_notice(entry(posted=AFTER)))
        assert outcome == expected
        assert len(probe) == 1

    def test_read_message_history_alone_counts_as_missing(self, monkeypatch):
        """The one an admin told only about sending would never grant, and
        without it the dashboard's button does nothing."""
        guild, _ = guild_with(perms(history=False))
        monkeypatch.setattr(bot.bot, "get_guild", lambda gid: guild)
        outcome, context = run(bot.assess_frozen_panel_notice(entry(posted=BEFORE)))
        assert outcome == "notify"
        assert context["missing"] == ["Read Message History"]

    def test_a_channel_missing_from_the_cache_is_not_guessed_about(self, monkeypatch):
        guild, _ = guild_with(perms(send=False))
        monkeypatch.setattr(bot.bot, "get_guild", lambda gid: guild)
        outcome, _ = run(bot.assess_frozen_panel_notice(entry(channel_id="999")))
        assert outcome == "channel_unknown"


class TestSendingIt:
    def _context(self, missing=("Send Messages",)):
        guild, channel = guild_with(perms(send=False))
        return {"guild": guild, "channel": channel, "posted": BEFORE, "missing": list(missing)}

    def test_the_record_is_written_before_the_message(self, monkeypatch, dashboard):
        """If the order were the other way, a crash between the two would send
        a message nothing remembers, and the next run would send it again."""
        seen_at_send = []

        class Recording(Member):
            async def send(self, text):
                seen_at_send.append(notified_rows())
                await super().send(text)

        member = Recording()

        async def resolve(guild, owner_id):
            return member

        monkeypatch.setattr(bot, "resolve_config_admin", resolve)
        assert run(bot.send_frozen_panel_notice(entry(), self._context(), "42")) == "notified"
        assert seen_at_send == [[(GUILD_ID, "notified")]]
        assert len(member.sent) == 1

    def test_a_record_that_cannot_be_written_sends_nothing(self, monkeypatch, dashboard):
        member = Member()

        async def resolve(guild, owner_id):
            return member

        def broken(server_id, channel_id):
            raise RuntimeError("database is gone")

        monkeypatch.setattr(bot, "resolve_config_admin", resolve)
        monkeypatch.setattr(bot, "_record_panel_notice", broken)
        assert run(bot.send_frozen_panel_notice(entry(), self._context(), "42")) == "record_failed"
        assert member.sent == []

    def test_an_admin_who_cannot_be_found_is_not_marked_as_told(self, monkeypatch, dashboard):
        async def resolve(guild, owner_id):
            return None

        monkeypatch.setattr(bot, "resolve_config_admin", resolve)
        assert run(bot.send_frozen_panel_notice(entry(), self._context(), "42")) == "no_recipient"
        assert notified_rows() == [], "a later run should still be able to reach them"

    def test_a_closed_inbox_still_spends_the_one_message(self, monkeypatch, dashboard):
        member = Member(raises=closed_dms())

        async def resolve(guild, owner_id):
            return member

        monkeypatch.setattr(bot, "resolve_config_admin", resolve)
        assert run(bot.send_frozen_panel_notice(entry(), self._context(), "42")) == "dm_closed"
        assert notified_rows() == [(GUILD_ID, "notified")]

    def test_without_a_dashboard_there_is_nothing_to_send(self, monkeypatch):
        monkeypatch.setattr(bot, "DASHBOARD_URL", None)
        monkeypatch.setattr(bot, "dashboard_guild_url", lambda gid: None)
        member = Member()

        async def resolve(guild, owner_id):
            return member

        monkeypatch.setattr(bot, "resolve_config_admin", resolve)
        assert run(bot.send_frozen_panel_notice(entry(), self._context(), "42")) == "no_dashboard"
        assert member.sent == []
        assert notified_rows() == []


class TestTheBatch:
    def _fleet(self, monkeypatch, panels, guilds):
        monkeypatch.setattr(bot, "load_instruction_panels", lambda *a, **k: list(panels))
        monkeypatch.setattr(bot, "partition_reachable_panels", lambda rows: (list(panels), []))
        monkeypatch.setattr(bot, "_panel_owner_ids", lambda: {p["server_id"]: "42" for p in panels})
        monkeypatch.setattr(bot.bot, "get_guild", lambda gid: guilds[str(gid)])

    def test_a_rerun_never_messages_the_same_server_twice(self, monkeypatch, dashboard):
        guild, _ = guild_with(perms(send=False))
        self._fleet(monkeypatch, [entry()], {GUILD_ID: guild})
        member = Member()

        async def resolve(g, owner_id):
            return member

        monkeypatch.setattr(bot, "resolve_config_admin", resolve)
        assert run(bot.notify_frozen_panels(reason="first")) == {"notified": 1}
        assert run(bot.notify_frozen_panels(reason="second")) == {"already_notified": 1}
        assert len(member.sent) == 1

    def test_it_only_messages_the_servers_that_need_it(self, monkeypatch, dashboard):
        panels = [entry(server_id=str(i)) for i in range(3)]
        ok, _ = guild_with(perms())
        broken, _ = guild_with(perms(send=False))
        self._fleet(monkeypatch, panels, {"0": ok, "1": broken, "2": ok})
        recipients = []

        async def resolve(g, owner_id):
            recipients.append(g)
            return Member()

        monkeypatch.setattr(bot, "resolve_config_admin", resolve)
        tally = run(bot.notify_frozen_panels(reason="test"))
        assert tally == {"sweep_can_replace": 2, "notified": 1}
        assert recipients == [broken]

    def test_neither_permissions_label_sends_a_message(self, monkeypatch, dashboard):
        """#328 acceptance, asserted directly: both labels are skips."""
        panels = [entry(server_id="0"), entry(server_id="1", posted=AFTER)]
        ok, _ = guild_with(perms())
        self._fleet(monkeypatch, panels, {"0": ok, "1": ok})
        probe_answers(monkeypatch, True)
        member = Member()

        async def resolve(g, owner_id):
            return member

        monkeypatch.setattr(bot, "resolve_config_admin", resolve)
        tally = run(bot.notify_frozen_panels(reason="test"))
        assert tally == {"sweep_can_replace": 1, "has_permissions": 1}
        assert member.sent == []

    def test_a_panel_the_sweep_could_replace_says_to_rerun_it(
        self, monkeypatch, dashboard, caplog
    ):
        ok, _ = guild_with(perms())
        self._fleet(monkeypatch, [entry()], {GUILD_ID: ok})
        with caplog.at_level("INFO"):
            run(bot.notify_frozen_panels(reason="test"))
        assert bot.PANEL_REPLACE_TRIGGER_PATH in caplog.text

    def test_no_rerun_advice_when_there_is_nothing_to_replace(
        self, monkeypatch, dashboard, caplog
    ):
        ok, _ = guild_with(perms())
        self._fleet(monkeypatch, [entry(posted=AFTER)], {GUILD_ID: ok})
        with caplog.at_level("INFO"):
            run(bot.notify_frozen_panels(reason="test"))
        assert bot.PANEL_REPLACE_TRIGGER_PATH not in caplog.text

    def test_the_cap_counts_messages_rather_than_servers_examined(self, monkeypatch, dashboard):
        monkeypatch.setattr(bot, "PANEL_NOTICE_MAX_PER_SWEEP", 2)
        panels = [entry(server_id=str(i)) for i in range(6)]
        ok, _ = guild_with(perms())
        broken, _ = guild_with(perms(send=False))
        # Alternating, so a cap on servers seen would stop after one message.
        self._fleet(monkeypatch, panels, {str(i): (broken if i % 2 == 0 else ok) for i in range(6)})
        sent = []

        async def resolve(g, owner_id):
            member = Member()
            sent.append(member)
            return member

        monkeypatch.setattr(bot, "resolve_config_admin", resolve)
        tally = run(bot.notify_frozen_panels(reason="test"))
        assert tally["notified"] == 2
        assert len(sent) == 2


class TestTheTrigger:
    def test_nothing_is_sent_without_the_file(self, monkeypatch, tmp_path):
        ran = []

        async def batch(reason):
            ran.append(reason)

        monkeypatch.setattr(bot, "notify_frozen_panels", batch)

        async def body():
            task = asyncio.ensure_future(
                bot.watch_panel_notice_trigger(path=str(tmp_path / "absent"), poll_interval=0.01)
            )
            await asyncio.sleep(0.05)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        run(body())
        assert ran == []

    def test_the_file_is_removed_before_the_batch(self, monkeypatch, tmp_path):
        trigger = tmp_path / "go"
        trigger.write_text("")
        seen = []

        async def batch(reason):
            seen.append(os.path.exists(str(trigger)))

        monkeypatch.setattr(bot, "notify_frozen_panels", batch)

        async def body():
            task = asyncio.ensure_future(
                bot.watch_panel_notice_trigger(path=str(trigger), poll_interval=0.01)
            )
            await asyncio.sleep(0.08)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        run(body())
        assert seen == [False]

    def test_it_shares_no_switch_with_the_other_two(self):
        paths = {
            bot.PANEL_NOTICE_TRIGGER_PATH,
            bot.PANEL_REPLACE_TRIGGER_PATH,
            os.getenv("INSTRUCTIONS_TRIGGER_PATH", "/tmp/update_instructions.trigger"),
        }
        assert len(paths) == 3
