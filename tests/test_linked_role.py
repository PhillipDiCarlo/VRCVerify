"""The Linked role (#359, PR 1): the bot side, before any admin can set one.

The rule these tests hold: a guild with no Linked role behaves exactly as it
did before the table existed. Everything a Linked role changes is asserted
beside a guild without one doing the old thing.
"""

from types import SimpleNamespace

import discord
import pytest

import bot
import locales
from test_premium import (  # noqa: F401 -- `enforced` is a fixture
    GUILD_ID,
    NEW_ID,
    OLD_ID,
    counting_entitlements_api,
    draw_line,
    enforced,
    fake_entitlements_api,
    make_server,
    run,
    run_and_drain,
)

VERIFIED_ID, UNVERIFIED_ID, LINKED_ID = 1, 2, 3


@pytest.fixture(autouse=True)
def clean_db():
    def wipe():
        with bot.session_scope() as session:
            session.query(bot.Server).delete()
            session.query(bot.User).delete()
            session.query(bot.LinkedRole).delete()
            session.query(bot.PremiumGrandfatherLine).delete()

    wipe()
    bot.premium_status_cache.clear()
    draw_line()
    yield
    wipe()
    bot.premium_status_cache.clear()


def set_linked_role(role_id=LINKED_ID):
    with bot.session_scope() as session:
        session.add(bot.LinkedRole(server_id=GUILD_ID, role_id=str(role_id)))


@pytest.fixture
def harness(monkeypatch):
    """A guild holding all three roles and a member holding Unverified."""
    verified = SimpleNamespace(id=VERIFIED_ID, name="Verified")
    unverified = SimpleNamespace(id=UNVERIFIED_ID, name="Unverified")
    linked = SimpleNamespace(id=LINKED_ID, name="Linked")
    events = SimpleNamespace(
        added=[], removed=[], nicks=[], dms=[], localized=[], logged=[],
        offers=[], refused=[],
    )

    class FakeMember:
        id = 42
        roles = [unverified]

        async def add_roles(self, role):
            if role.name in events.refused:
                raise discord.Forbidden(SimpleNamespace(status=403, reason="x"), "no")
            events.added.append(role.name)

        async def remove_roles(self, role):
            events.removed.append(role.name)

        async def edit(self, nick=None):
            events.nicks.append(nick)

        async def send(self, content):
            events.dms.append(content)

    member = FakeMember()
    guild = SimpleNamespace(
        id=int(GUILD_ID), name="Test Server", roles=[verified, unverified, linked]
    )

    async def fake_fetch(g, user_id):
        return member

    async def fake_dm(m, g, key, instr_locale=None, **kwargs):
        events.localized.append((key, kwargs))

    async def fake_failure(m, role, g, instr_locale=None):
        events.localized.append(("role_failed", {"role": role.name}))

    async def fake_offer(m, g, instr_locale=None, premium=None):
        events.offers.append(m.id)

    def fake_log(guild_id, user_id, outcome, channel, locale=None):
        events.logged.append(outcome)

    monkeypatch.setattr(bot.bot, "get_guild", lambda gid: guild)
    monkeypatch.setattr(bot, "fetch_member_cached", fake_fetch)
    monkeypatch.setattr(bot, "dm_localized", fake_dm)
    monkeypatch.setattr(bot, "dm_role_assignment_failure", fake_failure)
    monkeypatch.setattr(bot, "offer_group_invite", fake_offer)
    monkeypatch.setattr(bot, "queue_verification_log", fake_log)
    return events


def keys(events):
    return [key for key, _ in events.localized]


class TestAGuildWithoutALinkedRoleIsUnchanged:
    def test_not_18_is_told_so_and_given_nothing(self, harness):
        make_server(unverified_role_id=str(UNVERIFIED_ID))
        run_and_drain(bot.assign_role("42", False, GUILD_ID))
        assert harness.added == []
        assert harness.removed == []
        assert keys(harness) == [locales.NOT_18_PLUS]
        assert harness.logged == [bot.LOG_OUTCOME_NOT_18]
        assert harness.offers == []

    def test_18_gets_only_the_18_role(self, harness):
        make_server(unverified_role_id=str(UNVERIFIED_ID))
        run_and_drain(bot.assign_role("42", True, GUILD_ID))
        assert harness.added == ["Verified"]
        assert harness.removed == ["Unverified"]
        assert keys(harness) == [locales.DM_ROLE_SUCCESS]
        assert harness.logged == [bot.LOG_OUTCOME_VERIFIED]
        assert harness.offers == [42]

    def test_not_18_never_consults_entitlements(self, enforced, monkeypatch, harness):
        """The existing guarantee, still true when the table exists but is empty."""
        make_server(row_id=NEW_ID, unverified_role_id=str(UNVERIFIED_ID))
        api, calls = counting_entitlements_api()
        monkeypatch.setattr(bot.bot, "entitlements", api)
        run_and_drain(bot.assign_role("42", False, GUILD_ID))
        assert calls == []


class TestAGuildWithBothRoles:
    def setup_method(self):
        make_server(unverified_role_id=str(UNVERIFIED_ID))
        set_linked_role()

    def test_18_gets_both_and_one_dm_naming_both(self, harness):
        run_and_drain(bot.assign_role("42", True, GUILD_ID))
        assert harness.added == ["Verified", "Linked"]
        assert harness.localized == [
            (locales.DM_ROLES_SUCCESS,
             {"role": "Verified", "linked_role": "Linked", "server": "Test Server"}),
        ]
        assert harness.logged == [bot.LOG_OUTCOME_VERIFIED]

    def test_not_18_gets_linked_and_is_told_why_not_18(self, harness):
        run_and_drain(bot.assign_role("42", False, GUILD_ID))
        assert harness.added == ["Linked"]
        assert harness.localized == [
            (locales.DM_LINKED_NOT_18,
             {"linked_role": "Linked", "role": "Verified", "server": "Test Server"}),
        ]
        assert harness.logged == [bot.LOG_OUTCOME_LINKED]

    def test_unverified_comes_off_at_link_not_at_18(self, harness):
        """The lowest role the guild set up is the Linked role, so linking is
        what removes Unverified, even though an 18+ role also exists."""
        run_and_drain(bot.assign_role("42", False, GUILD_ID))
        assert harness.removed == ["Unverified"]

    def test_no_group_invite_offer_for_a_linked_member_yet(self, harness):
        """18+ only until the invite audience lands (#359 PR 2)."""
        run_and_drain(bot.assign_role("42", False, GUILD_ID))
        assert harness.offers == []

    def test_a_refused_linked_role_is_logged_as_a_link_failure(self, harness):
        harness.refused.append("Linked")
        run_and_drain(bot.assign_role("42", False, GUILD_ID))
        assert harness.added == []
        assert harness.logged == [bot.LOG_OUTCOME_LINKED_ROLE_FAILED]
        assert ("role_failed", {"role": "Linked"}) in harness.localized
        assert locales.DM_LINKED_NOT_18 not in keys(harness)

    def test_a_refused_18_role_still_gives_linked_and_says_so(self, harness):
        harness.refused.append("Verified")
        run_and_drain(bot.assign_role("42", True, GUILD_ID))
        assert harness.added == ["Linked"]
        assert harness.logged == [bot.LOG_OUTCOME_ROLE_FAILED]
        assert harness.localized == [
            ("role_failed", {"role": "Verified"}),
            (locales.DM_ROLE_SUCCESS, {"role": "Linked", "server": "Test Server"}),
        ]

    def test_a_refused_linked_role_is_not_logged_as_an_18_failure(self, harness):
        """The 18+ role went on; the log must not tell the admin to check it."""
        harness.refused.append("Linked")
        run_and_drain(bot.assign_role("42", True, GUILD_ID))
        assert harness.added == ["Verified"]
        assert harness.logged == [bot.LOG_OUTCOME_LINKED_ROLE_FAILED]
        assert harness.localized == [
            ("role_failed", {"role": "Linked"}),
            (locales.DM_ROLE_SUCCESS, {"role": "Verified", "server": "Test Server"}),
        ]


class TestAGuildWithOnlyALinkedRole:
    def setup_method(self):
        make_server(role_id=None, unverified_role_id=str(UNVERIFIED_ID))
        set_linked_role()

    def test_not_18_gets_linked_with_no_mention_of_an_18_role(self, harness):
        run_and_drain(bot.assign_role("42", False, GUILD_ID))
        assert harness.added == ["Linked"]
        assert harness.localized == [
            (locales.DM_LINKED_SUCCESS, {"role": "Linked", "server": "Test Server"}),
        ]
        assert harness.removed == ["Unverified"]

    def test_18_gets_linked_and_the_ordinary_success_dm(self, harness):
        run_and_drain(bot.assign_role("42", True, GUILD_ID))
        assert harness.added == ["Linked"]
        assert harness.localized == [
            (locales.DM_ROLE_SUCCESS, {"role": "Linked", "server": "Test Server"}),
        ]


class TestADeletedLinkedRole:
    def test_falls_back_to_the_old_behavior(self, harness):
        """The row points at a role that is gone: nothing to give for linking,
        so a not-18+ member is told so, exactly as without a Linked role."""
        make_server(unverified_role_id=str(UNVERIFIED_ID))
        set_linked_role(role_id=999)
        run_and_drain(bot.assign_role("42", False, GUILD_ID))
        assert harness.added == []
        assert harness.removed == []
        assert keys(harness) == [locales.NOT_18_PLUS]

    def test_a_deleted_18_role_does_not_stop_the_linked_one(self, harness):
        make_server(role_id="999")
        set_linked_role()
        run_and_drain(bot.assign_role("42", True, GUILD_ID))
        assert harness.added == ["Linked"]


class TestPremiumFeaturesFollowTheLink:
    """Nickname sync, the custom DM and Unverified removal need only the link."""

    def setup_server(self, row_id):
        make_server(
            row_id=row_id,
            unverified_role_id=str(UNVERIFIED_ID),
            auto_nickname_change=True,
            custom_verification_requested_message="Welcome aboard!",
        )
        set_linked_role()

    def test_a_premium_guild_runs_all_three_for_a_linked_member(self, harness):
        self.setup_server(OLD_ID)
        run_and_drain(bot.assign_role("42", False, GUILD_ID, display_name="VRCName"))
        assert harness.removed == ["Unverified"]
        assert harness.nicks == ["VRCName"]
        assert harness.dms == ["Welcome aboard!"]

    def test_the_custom_dm_does_not_hide_why_there_is_no_18_role(self, harness):
        """Custom text is likely written for 18+ members, so the reason the
        18+ role is missing still goes out beside it."""
        self.setup_server(OLD_ID)
        run_and_drain(bot.assign_role("42", False, GUILD_ID))
        assert harness.dms == ["Welcome aboard!"]
        assert keys(harness) == [locales.DM_LINKED_NOT_18]

    def test_an_18_member_gets_only_the_custom_dm(self, harness):
        self.setup_server(OLD_ID)
        run_and_drain(bot.assign_role("42", True, GUILD_ID))
        assert harness.dms == ["Welcome aboard!"]
        assert locales.DM_LINKED_NOT_18 not in keys(harness)

    def test_a_free_guild_still_gets_none_of_them(self, enforced, monkeypatch, harness):
        self.setup_server(NEW_ID)
        monkeypatch.setattr(bot.bot, "entitlements", fake_entitlements_api())
        run_and_drain(bot.assign_role("42", False, GUILD_ID, display_name="VRCName"))
        assert harness.added == ["Linked"]  # the Linked role itself is free
        assert harness.removed == []
        assert harness.nicks == []
        assert harness.dms == []
        assert keys(harness) == [locales.DM_LINKED_NOT_18]


class TestAutoVerifyOnJoin:
    def member(self, roles=(LINKED_ID,)):
        live = set(roles)
        return SimpleNamespace(
            id=42,
            guild=SimpleNamespace(
                id=int(GUILD_ID),
                name="Test Server",
                get_role=lambda rid: SimpleNamespace(id=rid) if rid in live else None,
            ),
        )

    def add_user(self, verified, vrc_user_id="usr_x"):
        with bot.session_scope() as session:
            session.add(bot.User(
                discord_id="42", verification_status=verified, vrc_user_id=vrc_user_id,
            ))

    def assigned(self, monkeypatch):
        calls = []

        async def fake_assign(*args, **kwargs):
            calls.append(args)

        monkeypatch.setattr(bot, "assign_role", fake_assign)
        return calls

    def test_a_linked_member_gets_linked_on_join(self, monkeypatch):
        make_server(auto_verify_new_members=True)
        set_linked_role()
        self.add_user(verified=False)
        calls = self.assigned(monkeypatch)
        run(bot.on_member_join(self.member()))
        assert calls == [("42", False, GUILD_ID)]

    def test_without_a_linked_role_a_linked_member_is_left_alone(self, monkeypatch):
        """Otherwise joining a server would DM them "you are not 18+"."""
        make_server(auto_verify_new_members=True)
        self.add_user(verified=False)
        calls = self.assigned(monkeypatch)
        run(bot.on_member_join(self.member()))
        assert calls == []

    def test_a_linked_only_guild_still_auto_verifies_18_members(self, monkeypatch):
        make_server(role_id=None, auto_verify_new_members=True)
        set_linked_role()
        self.add_user(verified=True)
        calls = self.assigned(monkeypatch)
        run(bot.on_member_join(self.member()))
        assert calls == [("42", True, GUILD_ID)]

    def test_a_legacy_row_with_no_vrchat_id_is_not_a_link(self, monkeypatch):
        make_server(auto_verify_new_members=True)
        set_linked_role()
        self.add_user(verified=False, vrc_user_id="")
        calls = self.assigned(monkeypatch)
        run(bot.on_member_join(self.member()))
        assert calls == []

    def test_a_deleted_linked_role_sends_nothing_on_join(self, monkeypatch):
        """Found by the adversarial pass: the row alone would send assign_role
        down the not-18+ path and DM the member for joining."""
        make_server(auto_verify_new_members=True)
        set_linked_role()
        self.add_user(verified=False)
        calls = self.assigned(monkeypatch)
        run(bot.on_member_join(self.member(roles=())))
        assert calls == []

    def test_the_switch_still_turns_it_off(self, monkeypatch):
        make_server(auto_verify_new_members=False)
        set_linked_role()
        self.add_user(verified=False)
        calls = self.assigned(monkeypatch)
        run(bot.on_member_join(self.member()))
        assert calls == []


class TestSetupIsSatisfiedByEitherRole:
    def interaction(self):
        replies = []

        class Response:
            async def send_message(self, *a, **kw):
                replies.append(a)

            async def send_modal(self, modal):
                replies.append(("modal",))

            async def defer(self, **kw):
                replies.append(("defer",))

        return SimpleNamespace(
            guild_id=int(GUILD_ID),
            user=SimpleNamespace(id=42),
            locale="en-US",
            entitlements=[],
            response=Response(),
            followup=SimpleNamespace(send=lambda *a, **k: None),
        ), replies

    def test_a_linked_only_guild_is_set_up(self, monkeypatch):
        make_server(role_id=None)
        set_linked_role()
        monkeypatch.setattr(bot, "_verification_cooldowns", {})
        interaction, replies = self.interaction()
        run(bot.process_verification(interaction))
        # A new member is sent to the link form, not told setup is missing.
        assert replies == [("modal",)]

    def test_a_guild_with_neither_role_is_not(self, monkeypatch):
        make_server(role_id=None)
        monkeypatch.setattr(bot, "_verification_cooldowns", {})
        interaction, replies = self.interaction()
        run(bot.process_verification(interaction))
        assert replies == [(bot.get_message(locales.SETUP_MISSING, interaction),)]


class TestStatus:
    def status(self, role):
        sent = []

        async def defer(ephemeral=False):
            pass

        async def send(msg, ephemeral=False, view=None):
            sent.append(msg)

        interaction = SimpleNamespace(
            guild=SimpleNamespace(
                id=int(GUILD_ID), name="Test Guild", get_role=lambda rid: role
            ),
            user=SimpleNamespace(id=77),
            locale="en-US",
            response=SimpleNamespace(defer=defer, send_message=send),
            followup=SimpleNamespace(send=send),
        )
        run(bot.vrcverify_status.callback(interaction))
        return sent[0]

    def test_a_linked_only_guild_is_not_reported_as_missing_a_role(self, monkeypatch):
        monkeypatch.setattr(bot, "_verification_cooldowns", {})
        make_server(role_id=None)
        set_linked_role()
        msg = self.status(SimpleNamespace(name="Linked"))
        assert locales.STATUS_ROLE_MISSING not in msg
        assert "Linked role: **Linked**" in msg

    def test_a_deleted_linked_role_is_reported(self, monkeypatch):
        monkeypatch.setattr(bot, "_verification_cooldowns", {})
        make_server(role_id=None)
        set_linked_role()
        msg = self.status(None)
        assert locales.STATUS_LINKED_ROLE_DELETED in msg
