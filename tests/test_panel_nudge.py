"""Unit tests for the missing-instructions-panel nudge (issue #51).

A server that ran /vrcverify_setup but never ran /vrcverify_instructions is
half-configured: members have no button to click, and nothing used to say so.
These tests pin the three things that changed:

- /vrcverify_setup nudges toward the panel when there isn't one yet
- an admin who still hasn't posted one 48h later gets exactly one DM, and the
  sweep that sends it can never turn into a DM blast
- /vrcverify_status reports what is actually wrong, including the quiet
  failure modes (revoked permissions, archived thread)

Scope note: the nudge is deliberately forward-looking. It hangs off the
guild_onboarding table, which only gets rows from this release onward, so
servers configured before it shipped are structurally unreachable.
"""

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import discord
import pytest

import bot
from locales import localizations, LANGUAGE_CODES

GUILD_ID = "123456789"
OWNER_ID = "42"


def run(coro):
    """Run an async bot helper from a sync test (no pytest-asyncio needed)."""
    return asyncio.run(coro)


def http_error(exc_type, status, code=0, message="test"):
    """Build a discord HTTPException subclass without a real aiohttp response."""
    return exc_type(SimpleNamespace(status=status, reason="test"), {"code": code, "message": message})


def archived_thread_error():
    return http_error(discord.HTTPException, 400, code=50083, message="Thread is archived")


def make_server(server_id=GUILD_ID, **overrides):
    fields = dict(
        server_id=server_id,
        owner_id=OWNER_ID,
        role_id="1",
        instructions_locale="en-US",
    )
    fields.update(overrides)
    with bot.session_scope() as session:
        session.add(bot.Server(**fields))


def make_onboarding(server_id=GUILD_ID, hours_ago=0, sent=False):
    with bot.session_scope() as session:
        session.add(
            bot.GuildOnboarding(
                server_id=server_id,
                setup_at=datetime.now(timezone.utc) - timedelta(hours=hours_ago),
                panel_nudge_dm_sent=sent,
            )
        )


def onboarding_row(server_id=GUILD_ID):
    with bot.session_scope() as session:
        row = session.query(bot.GuildOnboarding).filter_by(server_id=server_id).first()
        if row is None:
            return None
        return SimpleNamespace(
            server_id=row.server_id,
            setup_at=row.setup_at,
            sent=bool(row.panel_nudge_dm_sent),
        )


@pytest.fixture(autouse=True)
def clean_db():
    def wipe():
        with bot.session_scope() as session:
            session.query(bot.Server).delete()
            session.query(bot.GuildOnboarding).delete()
            session.query(bot.InstructionPanelView).delete()

    wipe()
    yield
    wipe()


@pytest.fixture
def dm_spy(monkeypatch):
    """Capture dm_localized calls instead of hitting Discord."""
    calls = []

    async def fake_dm(member, guild, key, instr_locale=None, **kwargs):
        calls.append(
            SimpleNamespace(
                member=member, guild=guild, key=key, locale=instr_locale, kwargs=kwargs
            )
        )

    monkeypatch.setattr(bot, "dm_localized", fake_dm)
    return calls


@pytest.fixture
def admin_member(monkeypatch):
    """fetch_member_cached resolves the configuring admin only."""
    member = SimpleNamespace(id=int(OWNER_ID), name="setup-admin")

    async def fake_fetch(guild, user_id):
        return member if user_id == int(OWNER_ID) else None

    monkeypatch.setattr(bot, "fetch_member_cached", fake_fetch)
    return member


@pytest.fixture
def guilds(monkeypatch):
    """Register fake guilds that bot.get_guild() can resolve by id."""
    registry = {}

    def add(server_id=GUILD_ID, name="Test Guild"):
        guild = SimpleNamespace(
            id=int(server_id), name=name, owner=None, owner_id=999, preferred_locale="en-US"
        )
        registry[int(server_id)] = guild
        return guild

    monkeypatch.setattr(bot.bot, "get_guild", lambda gid: registry.get(gid))
    return SimpleNamespace(add=add, registry=registry)


def setup_interaction(locale="en-US"):
    sent = []

    async def send_message(msg, ephemeral=False):
        sent.append(SimpleNamespace(msg=msg, ephemeral=ephemeral))

    interaction = SimpleNamespace(
        guild=SimpleNamespace(id=int(GUILD_ID)),
        user=SimpleNamespace(id=int(OWNER_ID)),
        locale=locale,
        response=SimpleNamespace(send_message=send_message),
    )
    return interaction, sent


# ---------------------------------------------------------------
# /vrcverify_setup nudges toward the panel
# ---------------------------------------------------------------
class TestSetupNudge:
    ROLE = SimpleNamespace(id=1, name="Verified")

    def test_nudge_included_when_no_panel_posted(self):
        interaction, sent = setup_interaction()
        run(bot.vrcverify_setup.callback(interaction, self.ROLE, None))
        assert localizations["en-US"]["setup_panel_nudge"] in sent[0].msg

    def test_no_nudge_when_panel_already_posted(self):
        make_server(instructions_channel_id="222", instructions_message_id="111")
        interaction, sent = setup_interaction()
        run(bot.vrcverify_setup.callback(interaction, self.ROLE, None))
        assert localizations["en-US"]["setup_panel_nudge"] not in sent[0].msg

    def test_donate_hint_stays_last(self):
        # The donate hint reads as a footer; the nudge must slot in above it.
        interaction, sent = setup_interaction()
        run(bot.vrcverify_setup.callback(interaction, self.ROLE, None))
        tail = localizations["en-US"]["setup_donate_hint"].format(kofi_link=bot.KOFI_URL)
        assert sent[0].msg.endswith(tail)

    def test_nudge_is_localized(self):
        interaction, sent = setup_interaction(locale="de")
        run(bot.vrcverify_setup.callback(interaction, self.ROLE, None))
        assert localizations["de"]["setup_panel_nudge"] in sent[0].msg

    def test_reply_stays_ephemeral(self):
        interaction, sent = setup_interaction()
        run(bot.vrcverify_setup.callback(interaction, self.ROLE, None))
        assert sent[0].ephemeral is True

    def test_setup_starts_the_nudge_clock(self):
        interaction, _ = setup_interaction()
        run(bot.vrcverify_setup.callback(interaction, self.ROLE, None))
        row = onboarding_row()
        assert row is not None
        assert row.sent is False

    def test_no_clock_started_when_panel_exists(self):
        make_server(instructions_channel_id="222", instructions_message_id="111")
        interaction, _ = setup_interaction()
        run(bot.vrcverify_setup.callback(interaction, self.ROLE, None))
        assert onboarding_row() is None

    def test_rerunning_setup_does_not_extend_the_deadline(self):
        # Otherwise an admin who tweaks roles daily is never nudged at all.
        make_onboarding(hours_ago=100)
        original = onboarding_row().setup_at
        interaction, _ = setup_interaction()
        run(bot.vrcverify_setup.callback(interaction, self.ROLE, None))
        assert onboarding_row().setup_at == original


# ---------------------------------------------------------------
# Onboarding helpers
# ---------------------------------------------------------------
class TestOnboardingHelpers:
    def test_record_is_idempotent(self):
        bot.record_guild_onboarding(GUILD_ID)
        first = onboarding_row().setup_at
        bot.record_guild_onboarding(GUILD_ID)
        with bot.session_scope() as session:
            assert session.query(bot.GuildOnboarding).count() == 1
        assert onboarding_row().setup_at == first

    def test_record_normalises_int_guild_ids(self):
        bot.record_guild_onboarding(int(GUILD_ID))
        assert onboarding_row(GUILD_ID) is not None

    def test_complete_marks_sent(self):
        make_onboarding()
        bot.complete_guild_onboarding(GUILD_ID)
        assert onboarding_row().sent is True

    def test_complete_on_missing_row_is_noop(self):
        bot.complete_guild_onboarding(GUILD_ID)  # must not raise
        assert onboarding_row() is None

    def test_forget_removes_the_row(self):
        make_onboarding()
        bot.forget_guild_onboarding(GUILD_ID)
        assert onboarding_row() is None

    def test_db_failure_is_swallowed(self, monkeypatch):
        from contextlib import contextmanager

        @contextmanager
        def broken_scope():
            raise RuntimeError("db down")
            yield  # pragma: no cover

        monkeypatch.setattr(bot, "session_scope", broken_scope)
        # Bookkeeping must never take down the command that triggered it.
        bot.record_guild_onboarding(GUILD_ID)
        bot.complete_guild_onboarding(GUILD_ID)
        bot.forget_guild_onboarding(GUILD_ID)


# ---------------------------------------------------------------
# Choosing who to nudge
# ---------------------------------------------------------------
class TestNudgeCandidates:
    def test_nothing_before_the_grace_period(self, monkeypatch):
        monkeypatch.setattr(bot, "PANEL_NUDGE_GRACE_HOURS", 48)
        make_server()
        make_onboarding(hours_ago=1)
        assert bot.load_panel_nudge_candidates(10) == []

    def test_eligible_after_the_grace_period(self, monkeypatch):
        monkeypatch.setattr(bot, "PANEL_NUDGE_GRACE_HOURS", 48)
        make_server()
        make_onboarding(hours_ago=49)
        candidates = bot.load_panel_nudge_candidates(10)
        assert [c["server_id"] for c in candidates] == [GUILD_ID]
        assert candidates[0]["owner_id"] == OWNER_ID

    def test_guild_with_a_panel_is_skipped(self, monkeypatch):
        monkeypatch.setattr(bot, "PANEL_NUDGE_GRACE_HOURS", 48)
        make_server(instructions_channel_id="222", instructions_message_id="111")
        make_onboarding(hours_ago=49)
        assert bot.load_panel_nudge_candidates(10) == []

    def test_already_nudged_guild_is_skipped(self, monkeypatch):
        monkeypatch.setattr(bot, "PANEL_NUDGE_GRACE_HOURS", 48)
        make_server()
        make_onboarding(hours_ago=49, sent=True)
        assert bot.load_panel_nudge_candidates(10) == []

    def test_guild_without_a_config_row_is_skipped(self, monkeypatch):
        monkeypatch.setattr(bot, "PANEL_NUDGE_GRACE_HOURS", 48)
        make_onboarding(hours_ago=49)
        assert bot.load_panel_nudge_candidates(10) == []

    def test_servers_configured_before_this_shipped_are_unreachable(self, monkeypatch):
        # No guild_onboarding row is exactly what a pre-existing server looks
        # like. This is the whole forward-only guarantee, so pin it.
        monkeypatch.setattr(bot, "PANEL_NUDGE_GRACE_HOURS", 0)
        make_server()
        assert bot.load_panel_nudge_candidates(10) == []

    def test_limit_is_honoured(self, monkeypatch):
        monkeypatch.setattr(bot, "PANEL_NUDGE_GRACE_HOURS", 0)
        for index in range(5):
            sid = str(1000 + index)
            make_server(server_id=sid)
            make_onboarding(server_id=sid, hours_ago=index + 1)
        assert len(bot.load_panel_nudge_candidates(2)) == 2

    def test_oldest_setups_come_first(self, monkeypatch):
        monkeypatch.setattr(bot, "PANEL_NUDGE_GRACE_HOURS", 0)
        for sid, age in (("1001", 5), ("1002", 90), ("1003", 40)):
            make_server(server_id=sid)
            make_onboarding(server_id=sid, hours_ago=age)
        order = [c["server_id"] for c in bot.load_panel_nudge_candidates(10)]
        assert order == ["1002", "1003", "1001"]


# ---------------------------------------------------------------
# Sending one nudge
# ---------------------------------------------------------------
class TestSendPanelNudge:
    def candidate(self, server_id=GUILD_ID):
        return {"server_id": server_id, "owner_id": OWNER_ID}

    def test_dms_the_configuring_admin(self, guilds, dm_spy, admin_member):
        make_server()
        make_onboarding()
        guild = guilds.add()
        assert run(bot.send_panel_nudge_dm(self.candidate())) is True
        assert len(dm_spy) == 1
        assert dm_spy[0].member is admin_member
        assert dm_spy[0].key == "panel_nudge_dm"
        assert dm_spy[0].kwargs == {"server": guild.name}

    def test_flag_is_set_before_sending(self, guilds, dm_spy, admin_member, monkeypatch):
        # A DM that raises must not leave the guild eligible for a repeat.
        make_server()
        make_onboarding()
        guilds.add()

        async def exploding_dm(*args, **kwargs):
            raise RuntimeError("discord down")

        monkeypatch.setattr(bot, "dm_localized", exploding_dm)
        with pytest.raises(RuntimeError):
            run(bot.send_panel_nudge_dm(self.candidate()))
        assert onboarding_row().sent is True

    def test_uses_the_server_locale(self, guilds, dm_spy, admin_member):
        make_server(instructions_locale="ja")
        make_onboarding()
        guilds.add()
        run(bot.send_panel_nudge_dm(self.candidate()))
        assert dm_spy[0].locale == "ja"

    def test_falls_back_to_guild_owner(self, guilds, dm_spy, monkeypatch):
        async def nobody(guild, user_id):
            return None

        monkeypatch.setattr(bot, "fetch_member_cached", nobody)
        make_server()
        make_onboarding()
        guild = guilds.add()
        guild.owner = SimpleNamespace(id=999, name="guild-owner")
        run(bot.send_panel_nudge_dm(self.candidate()))
        assert dm_spy[0].member is guild.owner

    def test_no_recipient_still_consumes_the_nudge(self, guilds, dm_spy, monkeypatch):
        async def nobody(guild, user_id):
            return None

        monkeypatch.setattr(bot, "fetch_member_cached", nobody)
        make_server()
        make_onboarding()
        guilds.add()
        assert run(bot.send_panel_nudge_dm(self.candidate())) is False
        assert dm_spy == []
        assert onboarding_row().sent is True

    def test_guild_we_left_is_forgotten_not_retried(self, guilds, dm_spy, admin_member):
        make_server()
        make_onboarding()
        # No guild registered, so get_guild() returns None.
        assert run(bot.send_panel_nudge_dm(self.candidate())) is False
        assert dm_spy == []
        assert onboarding_row() is None

    def test_malformed_guild_id_is_forgotten(self, guilds, dm_spy, admin_member):
        make_onboarding(server_id="not-a-number")
        assert run(bot.send_panel_nudge_dm(self.candidate("not-a-number"))) is False
        assert onboarding_row("not-a-number") is None


# ---------------------------------------------------------------
# The sweep loop
# ---------------------------------------------------------------
class TestPanelNudgeSweep:
    def sweep_once(self, seconds=0.2):
        """Let the loop complete at least one pass, then stop it."""

        async def scenario():
            task = asyncio.create_task(bot.panel_nudge_sweep_task(interval_seconds=60))
            await asyncio.sleep(seconds)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        run(scenario())

    def test_sweep_dms_eligible_servers_once(
        self, guilds, dm_spy, admin_member, monkeypatch
    ):
        monkeypatch.setattr(bot, "PANEL_NUDGE_GRACE_HOURS", 0)
        monkeypatch.setattr(bot, "PANEL_NUDGE_DM_SPACING", 0)
        make_server()
        make_onboarding(hours_ago=1)
        guilds.add()

        self.sweep_once()
        assert len(dm_spy) == 1
        assert onboarding_row().sent is True

        # A second sweep must find nothing left to do.
        self.sweep_once()
        assert len(dm_spy) == 1

    def test_sweep_respects_the_per_pass_cap(
        self, guilds, dm_spy, admin_member, monkeypatch
    ):
        # The cap is the anti-spam guarantee: a backlog trickles out instead of
        # going out as one burst.
        monkeypatch.setattr(bot, "PANEL_NUDGE_GRACE_HOURS", 0)
        monkeypatch.setattr(bot, "PANEL_NUDGE_DM_SPACING", 0)
        monkeypatch.setattr(bot, "PANEL_NUDGE_MAX_PER_SWEEP", 2)
        for index in range(5):
            sid = str(2000 + index)
            make_server(server_id=sid)
            make_onboarding(server_id=sid, hours_ago=index + 1)
            guilds.add(sid)

        self.sweep_once()
        assert len(dm_spy) == 2

    def test_sweep_spaces_out_its_dms(self, guilds, dm_spy, admin_member, monkeypatch):
        monkeypatch.setattr(bot, "PANEL_NUDGE_GRACE_HOURS", 0)
        monkeypatch.setattr(bot, "PANEL_NUDGE_DM_SPACING", 0.05)
        for index in range(3):
            sid = str(3000 + index)
            make_server(server_id=sid)
            make_onboarding(server_id=sid, hours_ago=index + 1)
            guilds.add(sid)

        # Three DMs at 50ms spacing cannot all land inside 60ms.
        self.sweep_once(seconds=0.06)
        assert len(dm_spy) < 3

    def test_one_bad_guild_does_not_stop_the_pass(
        self, guilds, dm_spy, admin_member, monkeypatch
    ):
        monkeypatch.setattr(bot, "PANEL_NUDGE_GRACE_HOURS", 0)
        monkeypatch.setattr(bot, "PANEL_NUDGE_DM_SPACING", 0)
        for sid in ("4001", "4002"):
            make_server(server_id=sid)
            make_onboarding(server_id=sid, hours_ago=1)
            guilds.add(sid)

        real_dm = bot.dm_localized

        async def fail_first(member, guild, key, instr_locale=None, **kwargs):
            if guild.id == 4001:
                raise RuntimeError("boom")
            await real_dm(member, guild, key, instr_locale, **kwargs)

        monkeypatch.setattr(bot, "dm_localized", fail_first)
        self.sweep_once()
        assert [c.guild.id for c in dm_spy] == [4002]

    def test_loop_survives_a_broken_query(self, monkeypatch):
        calls = []

        def boom(limit):
            calls.append(limit)
            raise RuntimeError("db down")

        monkeypatch.setattr(bot, "load_panel_nudge_candidates", boom)

        async def scenario():
            task = asyncio.create_task(bot.panel_nudge_sweep_task(interval_seconds=0.01))
            await asyncio.sleep(0.1)
            alive = not task.done()
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            return alive

        assert run(scenario()) is True
        assert len(calls) > 1  # it kept sweeping rather than dying


# ---------------------------------------------------------------
# /vrcverify_status
# ---------------------------------------------------------------
class TestStatusCommand:
    def interaction(self, role=None, locale="en-US"):
        sent = []

        async def defer(ephemeral=False):
            sent.append(SimpleNamespace(kind="defer", ephemeral=ephemeral))

        async def followup_send(msg, ephemeral=False):
            sent.append(SimpleNamespace(kind="send", msg=msg, ephemeral=ephemeral))

        interaction = SimpleNamespace(
            guild=SimpleNamespace(
                id=int(GUILD_ID), name="Test Guild", get_role=lambda rid: role
            ),
            user=SimpleNamespace(id=int(OWNER_ID)),
            locale=locale,
            response=SimpleNamespace(defer=defer),
            followup=SimpleNamespace(send=followup_send),
        )
        return interaction, sent

    def reply(self, sent):
        return next(s.msg for s in sent if s.kind == "send")

    def stub_probe(self, monkeypatch, outcome):
        seen = []

        async def fake_probe(entry, rebuild_embed):
            seen.append(SimpleNamespace(entry=entry, rebuild_embed=rebuild_embed))
            return outcome

        monkeypatch.setattr(bot, "probe_instruction_panel", fake_probe)
        return seen

    def test_unconfigured_server_reports_both_gaps(self):
        interaction, sent = self.interaction()
        run(bot.vrcverify_status.callback(interaction))
        msg = self.reply(sent)
        en = localizations["en-US"]
        assert en["status_role_missing"] in msg
        assert en["status_panel_missing"] in msg
        assert en["status_tips"] in msg

    def test_reply_is_ephemeral_and_deferred(self):
        interaction, sent = self.interaction()
        run(bot.vrcverify_status.callback(interaction))
        assert sent[0].kind == "defer" and sent[0].ephemeral is True
        assert sent[-1].kind == "send" and sent[-1].ephemeral is True

    def test_healthy_server_reports_ok_and_no_tips(self, monkeypatch):
        make_server(instructions_channel_id="222", instructions_message_id="111")
        self.stub_probe(monkeypatch, "ok")
        role = SimpleNamespace(id=1, name="Verified")
        interaction, sent = self.interaction(role=role)
        run(bot.vrcverify_status.callback(interaction))
        msg = self.reply(sent)
        en = localizations["en-US"]
        assert en["status_panel_ok"] in msg
        assert "Verified" in msg
        assert en["status_tips"] not in msg

    def test_deleted_role_is_reported(self, monkeypatch):
        make_server(instructions_channel_id="222", instructions_message_id="111")
        self.stub_probe(monkeypatch, "ok")
        interaction, sent = self.interaction(role=None)
        run(bot.vrcverify_status.callback(interaction))
        assert localizations["en-US"]["status_role_deleted"] in self.reply(sent)

    @pytest.mark.parametrize(
        "outcome,key",
        [
            ("forbidden", "status_panel_unreachable"),
            ("archived", "status_panel_archived"),
            ("gone", "status_panel_gone"),
            ("missing_ids", "status_panel_gone"),
            ("malformed", "status_panel_gone"),
            ("http_error", "status_panel_unreachable"),
            ("error", "status_panel_unreachable"),
        ],
    )
    def test_probe_outcomes_map_to_advice(self, monkeypatch, outcome, key):
        make_server(instructions_channel_id="222", instructions_message_id="111")
        self.stub_probe(monkeypatch, outcome)
        interaction, sent = self.interaction(role=SimpleNamespace(id=1, name="Verified"))
        run(bot.vrcverify_status.callback(interaction))
        msg = self.reply(sent)
        assert localizations["en-US"][key] in msg
        assert localizations["en-US"]["status_tips"] in msg

    def test_probe_gets_the_saved_panel_without_rebuilding_the_embed(self, monkeypatch):
        make_server(
            instructions_channel_id="222",
            instructions_message_id="111",
            instructions_locale="de",
        )
        seen = self.stub_probe(monkeypatch, "ok")
        interaction, _ = self.interaction(role=SimpleNamespace(id=1, name="Verified"))
        run(bot.vrcverify_status.callback(interaction))
        assert len(seen) == 1
        assert seen[0].entry["channel_id"] == "222"
        assert seen[0].entry["message_id"] == "111"
        assert seen[0].entry["locale"] == "de"
        # Rebuilding would rewrite a member-visible embed on a read-only check.
        assert seen[0].rebuild_embed is False

    def test_no_panel_means_no_api_call(self, monkeypatch):
        make_server()
        seen = self.stub_probe(monkeypatch, "ok")
        interaction, _ = self.interaction(role=SimpleNamespace(id=1, name="Verified"))
        run(bot.vrcverify_status.callback(interaction))
        assert seen == []

    def test_output_is_localized(self, monkeypatch):
        make_server()
        interaction, sent = self.interaction(locale="es-ES")
        run(bot.vrcverify_status.callback(interaction))
        assert localizations["es-ES"]["status_panel_missing"] in self.reply(sent)

    def test_status_is_admin_only(self):
        checks = getattr(bot.vrcverify_status, "checks", [])
        assert checks, "/vrcverify_status must keep its administrator check"


# ---------------------------------------------------------------
# Real probe outcomes (no stub) feeding the status command
# ---------------------------------------------------------------
class TestProbeOutcomes:
    def entry(self):
        return {
            "server_id": GUILD_ID,
            "channel_id": "222",
            "message_id": "111",
            "locale": "en-US",
            "view_version": bot.INSTRUCTIONS_VIEW_VERSION,
        }

    def install(self, monkeypatch, error=None):
        async def edit(**payload):
            if error:
                raise error

        monkeypatch.setattr(
            bot.bot,
            "get_partial_messageable",
            lambda cid, **kw: SimpleNamespace(get_partial_message=lambda mid: SimpleNamespace(edit=edit)),
        )

    def test_success_reports_ok(self, monkeypatch):
        self.install(monkeypatch)
        assert run(bot.probe_instruction_panel(self.entry(), False)) == "ok"

    def test_forbidden_reports_forbidden(self, monkeypatch):
        self.install(monkeypatch, http_error(discord.Forbidden, 403))
        assert run(bot.probe_instruction_panel(self.entry(), False)) == "forbidden"

    def test_archived_thread_is_distinguished(self, monkeypatch):
        # 50083 is the quiet failure issue #51 called out; it must not read as
        # a generic HTTP error, because the fix an admin needs is different.
        self.install(monkeypatch, archived_thread_error())
        assert run(bot.probe_instruction_panel(self.entry(), False)) == "archived"

    def test_other_http_errors_stay_generic(self, monkeypatch):
        self.install(monkeypatch, http_error(discord.HTTPException, 500, code=0))
        assert run(bot.probe_instruction_panel(self.entry(), False)) == "http_error"

    def test_not_found_reports_gone_and_clears_the_record(self, monkeypatch):
        make_server(instructions_channel_id="222", instructions_message_id="111")
        self.install(monkeypatch, http_error(discord.NotFound, 404))
        assert run(bot.probe_instruction_panel(self.entry(), False)) == "gone"
        with bot.session_scope() as session:
            srv = session.query(bot.Server).filter_by(server_id=GUILD_ID).first()
            assert srv.instructions_message_id is None

    def test_missing_ids_short_circuits(self, monkeypatch):
        entry = self.entry()
        entry["message_id"] = None
        assert run(bot.probe_instruction_panel(entry, False)) == "missing_ids"

    def test_malformed_ids_short_circuit(self, monkeypatch):
        monkeypatch.setattr(
            bot.bot, "get_partial_messageable", lambda cid, **kw: SimpleNamespace()
        )
        entry = self.entry()
        entry["channel_id"] = "not-an-int"
        assert run(bot.probe_instruction_panel(entry, False)) == "malformed"

    def test_refresh_wrapper_still_returns_a_bool(self, monkeypatch):
        self.install(monkeypatch)
        assert run(bot.refresh_instruction_panel(self.entry(), False)) is True
        self.install(monkeypatch, http_error(discord.Forbidden, 403))
        assert run(bot.refresh_instruction_panel(self.entry(), False)) is False


# ---------------------------------------------------------------
# /vrcverify_instructions retires a pending nudge
# ---------------------------------------------------------------
class TestInstructionsClearsNudge:
    def interaction(self):
        async def send_message(embed=None, view=None):
            pass

        async def original_response():
            return SimpleNamespace(id=111)

        async def followup_send(msg, ephemeral=False):
            pass

        return SimpleNamespace(
            guild=SimpleNamespace(id=int(GUILD_ID)),
            channel=SimpleNamespace(id=222),
            user=SimpleNamespace(id=int(OWNER_ID)),
            locale="en-US",
            response=SimpleNamespace(send_message=send_message),
            original_response=original_response,
            followup=SimpleNamespace(send=followup_send),
        )

    def test_posting_the_panel_retires_the_nudge(self):
        make_server()
        make_onboarding()
        run(bot.vrcverify_instructions.callback(self.interaction()))
        assert onboarding_row().sent is True

    def test_panel_is_tracked_even_without_a_setup_row(self):
        # Previously the ids were dropped when no Server row existed, leaving a
        # posted panel invisible to the refresh sweep and to /vrcverify_status.
        run(bot.vrcverify_instructions.callback(self.interaction()))
        with bot.session_scope() as session:
            srv = session.query(bot.Server).filter_by(server_id=GUILD_ID).first()
            assert srv is not None
            assert srv.instructions_channel_id == "222"
            assert srv.instructions_message_id == "111"


# ---------------------------------------------------------------
# Locale coverage for the new strings
# ---------------------------------------------------------------
NEW_KEYS = (
    "setup_panel_nudge",
    "panel_nudge_dm",
    "status_header",
    "status_role_ok",
    "status_role_missing",
    "status_role_deleted",
    "status_panel_ok",
    "status_panel_missing",
    "status_panel_unreachable",
    "status_panel_archived",
    "status_panel_gone",
    "status_tips",
)


class TestNudgeLocaleStrings:
    @pytest.mark.parametrize("locale", LANGUAGE_CODES)
    def test_all_keys_present(self, locale):
        for key in NEW_KEYS:
            assert key in localizations[locale], f"{locale} missing {key}"

    @pytest.mark.parametrize("locale", LANGUAGE_CODES)
    def test_setup_nudge_separates_itself(self, locale):
        # It is appended mid-message, so it must open its own paragraph.
        assert localizations[locale]["setup_panel_nudge"].startswith("\n\n")

    @pytest.mark.parametrize("locale", LANGUAGE_CODES)
    def test_templates_format_with_caller_kwargs(self, locale):
        strings = localizations[locale]
        assert "SrvName" in strings["panel_nudge_dm"].format(server="SrvName")
        assert "SrvName" in strings["status_header"].format(server="SrvName")
        assert "RoleName" in strings["status_role_ok"].format(role="RoleName")

    @pytest.mark.parametrize("locale", LANGUAGE_CODES)
    def test_plain_strings_take_no_placeholders(self, locale):
        for key in (
            "setup_panel_nudge",
            "status_role_missing",
            "status_role_deleted",
            "status_panel_ok",
            "status_panel_missing",
            "status_panel_unreachable",
            "status_panel_archived",
            "status_panel_gone",
            "status_tips",
        ):
            localizations[locale][key].format()  # must not raise

    @pytest.mark.parametrize("locale", LANGUAGE_CODES)
    def test_admin_facing_strings_name_the_command(self, locale):
        # The command name is the actionable part; it stays literal everywhere.
        assert "/vrcverify_instructions" in localizations[locale]["setup_panel_nudge"]
        assert "/vrcverify_instructions" in localizations[locale]["panel_nudge_dm"]

    def test_every_probe_outcome_has_a_message(self):
        outcomes = {"ok", "gone", "missing_ids", "malformed", "forbidden", "archived", "http_error", "error"}
        assert set(bot.PANEL_STATUS_MESSAGE_KEYS) == outcomes
        for key in bot.PANEL_STATUS_MESSAGE_KEYS.values():
            assert key in localizations["en-US"]
