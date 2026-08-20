"""Storage, gate and API for the VRChat group invite (issue #49, phase 3).

Nothing here sends an invite or talks to VRChat. This is the layer underneath:
the table a guild's group lives in, the premium gate over it, and the two
settings fields the website will later render.

Two of these tests are about things that would be expensive to get wrong:

- **First claim wins.** Without it, guild B could type the id of a group guild
  A had already set up and start inviting its own members into a stranger's
  private group. The UNIQUE column is the mechanism; the query is what produces
  a message worth showing.
- **A changed group id resets everything cached about the old one.** A stale
  "ready" would let phase 5 send invites into a group nobody has checked.
"""

import asyncio
from datetime import datetime, timezone

import pytest

import bot
import vrc_group_inviter as inviter
from dashboard import settings_view


GUILD_ID = 987654321
OTHER_GUILD_ID = 123456789
ADMIN_ID = 4242
OWNER_ID = 77
SKU_ID = 555000111

GROUP_ID = "grp_0e1d4755-2f87-4129-a192-5587068cbf73"
OTHER_GROUP_ID = "grp_11111111-2222-3333-4444-555555555555"


def run(coro):
    return asyncio.run(coro)


def make_server(server_id=str(GUILD_ID), row_id=10, **overrides):
    fields = dict(
        id=row_id,
        server_id=server_id,
        owner_id=str(OWNER_ID),
        role_id="1",
        instructions_locale="en-US",
    )
    fields.update(overrides)
    with bot.session_scope() as session:
        session.add(bot.Server(**fields))


def write(changes, guild_id=GUILD_ID):
    return run(bot.write_dashboard_settings(guild_id, ADMIN_ID, changes))


def stored(guild_id=GUILD_ID):
    return bot.load_group_invite_config(guild_id)


class SummaryGuild:
    """Just enough guild for build_settings_summary to render."""

    def __init__(self, guild_id=GUILD_ID):
        self.id = guild_id
        self.roles = []
        self.text_channels = []

    def get_role(self, role_id):
        return None

    def get_channel(self, channel_id):
        return None


def audit_fields():
    with bot.session_scope() as session:
        return [row.field for row in session.query(bot.DashboardAudit).all()]


@pytest.fixture(autouse=True)
def clean_db():
    def wipe():
        with bot.session_scope() as session:
            session.query(bot.Server).delete()
            session.query(bot.GroupInviteConfig).delete()
            session.query(bot.GroupSeatLease).delete()
            session.query(bot.DashboardAudit).delete()
            session.query(bot.PremiumGrandfatherLine).delete()

    wipe()
    bot.premium_status_cache.clear()
    yield
    wipe()
    bot.premium_status_cache.clear()


@pytest.fixture
def enforced(monkeypatch):
    monkeypatch.setattr(bot, "PREMIUM_SKU_ID", SKU_ID)
    monkeypatch.setattr(bot, "PREMIUM_ENFORCED", True)
    bot.premium_status_cache.clear()


@pytest.fixture
def free(monkeypatch, enforced):
    async def no(guild_id):
        return False

    monkeypatch.setattr(bot, "guild_has_premium", no)
    with bot.session_scope() as session:
        session.add(bot.PremiumGrandfatherLine(id=1, max_server_id=1))
    bot.premium_status_cache.clear()


@pytest.fixture
def subscribed(monkeypatch, enforced):
    async def yes(guild_id):
        return True

    monkeypatch.setattr(bot, "guild_has_premium", yes)
    bot.premium_status_cache.clear()


# -------------------------------------------------------------------
# What an admin is allowed to paste into the box
# -------------------------------------------------------------------
class TestGroupIdParsing:
    def test_a_bare_id_is_accepted(self):
        assert bot.parse_vrchat_group_id(GROUP_ID) == GROUP_ID

    def test_surrounding_whitespace_is_ignored(self):
        assert bot.parse_vrchat_group_id(f"  {GROUP_ID}\n") == GROUP_ID

    @pytest.mark.parametrize(
        "url",
        [
            f"https://vrchat.com/home/group/{GROUP_ID}",
            f"http://vrchat.com/home/group/{GROUP_ID}",
            f"https://www.vrchat.com/home/group/{GROUP_ID}",
            f"https://vrchat.com/home/group/{GROUP_ID}/",
            f"https://vrchat.com/home/group/{GROUP_ID}/members",
            f"https://vrchat.com/home/group/{GROUP_ID}?tab=invites",
            f"https://vrchat.com/home/group/{GROUP_ID}#posts",
        ],
    )
    def test_the_website_url_is_accepted_whatever_tab_they_were_on(self, url):
        """What is in the address bar while they are looking at the group.

        Telling an admin to extract a substring from a URL they can already see
        is a step that exists only to create mistakes.
        """
        assert bot.parse_vrchat_group_id(url) == GROUP_ID

    def test_the_id_is_normalised_to_lower_case(self):
        """Case folding is what makes the UNIQUE column mean anything.

        Two guilds submitting one group in different cases would otherwise both
        be allowed to hold it, and first-claim-wins would quietly stop being
        true.
        """
        assert bot.parse_vrchat_group_id(GROUP_ID.upper()) == GROUP_ID

    def test_a_vrc_group_short_link_gets_its_own_refusal(self):
        """It is a real group link, and there is no endpoint that resolves it.

        "That is not a group id" would be a confusing thing to tell someone
        holding the link VRChat itself handed them.
        """
        for link in (
            "https://vrc.group/VERIFY.1234",
            "vrc.group/VERIFY.1234",
        ):
            with pytest.raises(bot.SettingRejected) as caught:
                bot.parse_vrchat_group_id(link)
            assert caught.value.reason == "group_shortlink_unsupported"

    @pytest.mark.parametrize(
        "value",
        [
            "not a group",
            "usr_0e59962a-3e0d-4303-802b-9314623027e5",
            "grp_",
            "grp_0e1d4755-2f87-4129-a192",
            f"{GROUP_ID}extra",
            f"https://evil.example.com/home/group/{GROUP_ID}",
            f"https://vrchat.com.evil.example/home/group/{GROUP_ID}",
            12345,
            True,
        ],
    )
    def test_anything_else_is_refused(self, value):
        with pytest.raises(bot.SettingRejected) as caught:
            bot.parse_vrchat_group_id(value)
        assert caught.value.reason == "not_a_group"

    @pytest.mark.parametrize("value", [None, "", "   "])
    def test_empty_means_no_group(self, value):
        """The same way leaving the argument off /vrcverify_logchannel clears it."""
        assert bot.parse_vrchat_group_id(value) is None


# -------------------------------------------------------------------
# The table
# -------------------------------------------------------------------
class TestStorage:
    def test_an_unconfigured_guild_has_no_row(self):
        assert bot.load_group_invite_config(GUILD_ID) is None

    def test_saving_a_group_stores_it_and_mints_a_claim_code(self):
        bot.save_group_invite_config(GUILD_ID, group_id=GROUP_ID, enabled=False)
        row = stored()
        assert row["group_id"] == GROUP_ID
        assert row["enabled"] is False
        assert row["claim_code"].startswith("VRCG-")
        assert row["claim_code_issued_at"] is not None
        assert row["verify_state"] == bot.GROUP_SETUP_UNVERIFIED

    def test_the_claim_code_survives_an_unrelated_save(self):
        """Toggling the switch must not invalidate a code already pasted.

        The admin may well have the code in their group description already and
        be a click away from verifying; reissuing it there would silently make
        what they pasted wrong.
        """
        bot.save_group_invite_config(GUILD_ID, group_id=GROUP_ID, enabled=False)
        first = stored()["claim_code"]
        bot.save_group_invite_config(GUILD_ID, group_id=GROUP_ID, enabled=True)
        assert stored()["claim_code"] == first
        assert stored()["enabled"] is True

    def test_changing_the_group_resets_everything_learned_about_the_old_one(self):
        bot.save_group_invite_config(GUILD_ID, group_id=GROUP_ID, enabled=True)
        first_code = stored()["claim_code"]
        # As the verify round trip will leave it once it succeeds.
        with bot.session_scope() as session:
            row = session.query(bot.GroupInviteConfig).first()
            row.verify_state = bot.GROUP_SETUP_READY
            row.group_name = "Old Group"
            row.can_invite = True
            row.can_see_members = True
            row.invite_account_id = "usr_old"
            row.verified_at = datetime.now(timezone.utc)
            row.verify_job_id = "job-1"

        bot.save_group_invite_config(GUILD_ID, group_id=OTHER_GROUP_ID, enabled=True)

        row = stored()
        assert row["group_id"] == OTHER_GROUP_ID
        assert row["verify_state"] == bot.GROUP_SETUP_UNVERIFIED
        assert row["group_name"] is None
        assert row["can_invite"] is False
        assert row["can_see_members"] is False
        assert row["invite_account_id"] is None
        assert row["verified_at"] is None
        assert row["verify_job_id"] is None
        # A new group needs proving again, so the old code must not carry over.
        assert row["claim_code"] != first_code
        assert row["claim_code"].startswith("VRCG-")

    def test_clearing_the_group_clears_the_claim_code(self):
        bot.save_group_invite_config(GUILD_ID, group_id=GROUP_ID, enabled=True)
        bot.save_group_invite_config(GUILD_ID, group_id=None, enabled=True)
        row = stored()
        assert row["group_id"] is None
        assert row["claim_code"] is None
        assert row["claim_code_issued_at"] is None

    def test_the_switch_is_independent_of_whether_a_group_is_set(self):
        """The dashboard offers it as its own control, so this state exists.

        Every reader has to check both -- which they would have to do anyway
        for "on, with a group that failed verification".
        """
        bot.save_group_invite_config(GUILD_ID, group_id=None, enabled=True)
        row = stored()
        assert row["enabled"] is True
        assert row["group_id"] is None

    def test_the_storage_layer_normalises_the_id_too(self):
        """The UNIQUE column's invariant belongs to the function that writes it.

        The settings coercer is one caller. A later phase storing what a worker
        echoed back would be another, and an unnormalised id reaching the table
        would put a second casing of an already-claimed group in it -- at which
        point first-claim-wins has quietly stopped being true.
        """
        bot.save_group_invite_config(
            GUILD_ID, group_id=GROUP_ID.upper(), enabled=False
        )
        assert stored()["group_id"] == GROUP_ID
        with pytest.raises(bot.SettingRejected) as caught:
            bot.save_group_invite_config(
                OTHER_GUILD_ID, group_id=GROUP_ID, enabled=False
            )
        assert caught.value.reason == "group_claimed_elsewhere"

    def test_the_storage_layer_refuses_a_value_that_is_not_a_group(self):
        with pytest.raises(bot.SettingRejected) as caught:
            bot.save_group_invite_config(
                GUILD_ID, group_id="not-a-group", enabled=False
            )
        assert caught.value.reason == "not_a_group"
        assert bot.load_group_invite_config(GUILD_ID) is None

    def test_claim_codes_are_not_predictable(self):
        codes = set()
        for _ in range(20):
            codes.add(bot.generate_group_claim_code())
        assert len(codes) == 20
        assert all(code.startswith("VRCG-") and len(code) == 11 for code in codes)


# -------------------------------------------------------------------
# One group, one guild
# -------------------------------------------------------------------
class TestFirstClaimWins:
    def test_a_second_guild_cannot_claim_a_held_group(self):
        bot.save_group_invite_config(GUILD_ID, group_id=GROUP_ID, enabled=True)
        with pytest.raises(bot.SettingRejected) as caught:
            bot.save_group_invite_config(
                OTHER_GUILD_ID, group_id=GROUP_ID, enabled=True
            )
        assert caught.value.reason == "group_claimed_elsewhere"
        assert bot.load_group_invite_config(OTHER_GUILD_ID) is None

    def test_the_refusal_does_not_name_the_other_server(self):
        """The holder is another customer's guild id, and none of this
        admin's business. The dashboard says to contact support."""
        bot.save_group_invite_config(GUILD_ID, group_id=GROUP_ID, enabled=True)
        with pytest.raises(bot.SettingRejected) as caught:
            bot.save_group_invite_config(
                OTHER_GUILD_ID, group_id=GROUP_ID, enabled=True
            )
        assert str(GUILD_ID) not in str(caught.value.reason)
        assert str(GUILD_ID) not in str(getattr(caught.value, "field", ""))

    def test_the_holder_can_save_its_own_group_again(self):
        bot.save_group_invite_config(GUILD_ID, group_id=GROUP_ID, enabled=False)
        bot.save_group_invite_config(GUILD_ID, group_id=GROUP_ID, enabled=True)
        assert stored()["enabled"] is True

    def test_releasing_a_group_frees_it_for_someone_else(self):
        bot.save_group_invite_config(GUILD_ID, group_id=GROUP_ID, enabled=True)
        bot.save_group_invite_config(GUILD_ID, group_id=None, enabled=False)
        bot.save_group_invite_config(OTHER_GUILD_ID, group_id=GROUP_ID, enabled=True)
        assert bot.load_group_invite_config(OTHER_GUILD_ID)["group_id"] == GROUP_ID

    def test_two_unconfigured_guilds_do_not_collide(self):
        """NULLs do not collide in a UNIQUE column, in Postgres or SQLite.

        If they did, the second guild to open the settings page and save
        anything at all would be refused.
        """
        bot.save_group_invite_config(GUILD_ID, group_id=None, enabled=False)
        bot.save_group_invite_config(OTHER_GUILD_ID, group_id=None, enabled=False)
        assert bot.load_group_invite_config(OTHER_GUILD_ID)["group_id"] is None

    def test_the_unique_column_settles_a_simultaneous_claim(self, monkeypatch):
        """The pre-check can only see committed rows, so it is not the guard.

        Simulated by blinding the pre-check, which is exactly what a race does:
        both writers look, both see nothing, and the database decides.
        """
        bot.save_group_invite_config(GUILD_ID, group_id=GROUP_ID, enabled=True)
        monkeypatch.setattr(bot, "group_claim_holder", lambda group_id: None)
        with pytest.raises(bot.SettingRejected) as caught:
            bot.save_group_invite_config(
                OTHER_GUILD_ID, group_id=GROUP_ID, enabled=True
            )
        assert caught.value.reason == "group_claimed_elsewhere"


# -------------------------------------------------------------------
# The plan gate
# -------------------------------------------------------------------
class TestThePlanGate:
    def test_a_free_server_is_refused_and_nothing_is_stored(self, free):
        make_server(row_id=9000)
        with pytest.raises(bot.SettingRejected) as caught:
            write({"vrchat_group_id": GROUP_ID})
        assert caught.value.reason == "requires_premium"
        assert caught.value.locked is True
        assert bot.load_group_invite_config(GUILD_ID) is None

    def test_a_grandfathered_server_is_refused_too(self, enforced, monkeypatch):
        """It shipped after the cutover, so nobody can be losing it."""
        async def no(guild_id):
            return False

        monkeypatch.setattr(bot, "guild_has_premium", no)
        make_server(row_id=1)
        with bot.session_scope() as session:
            session.add(bot.PremiumGrandfatherLine(id=1, max_server_id=1000))
        bot.premium_status_cache.clear()

        flags = run(bot.resolve_premium_flags(GUILD_ID))
        assert flags.grandfathered is True
        with pytest.raises(bot.SettingRejected) as caught:
            write({"vrchat_group_invite_enabled": True})
        assert caught.value.reason == "requires_premium"

    def test_a_subscribed_server_saves(self, subscribed):
        make_server()
        write({"vrchat_group_id": GROUP_ID, "vrchat_group_invite_enabled": True})
        row = stored()
        assert row["group_id"] == GROUP_ID
        assert row["enabled"] is True

    def test_the_feature_is_not_grandfathered(self):
        assert bot.FEATURE_GROUP_INVITE not in bot.GRANDFATHERED_FEATURES

    def test_the_feature_is_advertised_now_that_it_works(self):
        """It was in UNANNOUNCED_FEATURES for exactly as long as no admin could
        reach it. The settings page arrived, the name came out, and three tests
        that had been passing on the exception went back to being real:
        test_premium's "the pitch lists every gated feature",
        build_settings_summary, and the dashboard gap below.
        """
        assert bot.FEATURE_GROUP_INVITE not in bot.UNANNOUNCED_FEATURES
        assert bot.field_is_announced("vrchat_group_id") is True


# -------------------------------------------------------------------
# The write path
# -------------------------------------------------------------------
class TestTheWritePath:
    def test_both_fields_round_trip(self):
        make_server()
        write({"vrchat_group_id": f"https://vrchat.com/home/group/{GROUP_ID}"})
        assert stored()["group_id"] == GROUP_ID
        write({"vrchat_group_invite_enabled": True})
        assert stored()["enabled"] is True
        assert stored()["group_id"] == GROUP_ID

    def test_a_bad_group_id_is_refused_before_anything_is_written(self):
        make_server(instructions_locale="en-US")
        with pytest.raises(bot.SettingRejected):
            write({"instructions_locale": "de", "vrchat_group_id": "nonsense"})
        assert bot.load_group_invite_config(GUILD_ID) is None
        with bot.session_scope() as session:
            assert session.query(bot.Server).first().instructions_locale == "en-US"

    def test_a_claimed_group_leaves_the_rest_of_the_batch_alone(self):
        """The whole point of validating the batch before applying any of it.

        Checked with a claim rather than a malformed value because the claim is
        the one that needs a database round trip to discover, and so is the one
        most easily left until the write.
        """
        bot.save_group_invite_config(OTHER_GUILD_ID, group_id=GROUP_ID, enabled=True)
        make_server(instructions_locale="en-US")
        with pytest.raises(bot.SettingRejected) as caught:
            write({"instructions_locale": "de", "vrchat_group_id": GROUP_ID})
        assert caught.value.reason == "group_claimed_elsewhere"
        with bot.session_scope() as session:
            assert session.query(bot.Server).first().instructions_locale == "en-US"
        assert audit_fields() == []

    def test_the_website_may_write_both_fields(self):
        assert "vrchat_group_id" in bot.DASHBOARD_WRITABLE_FIELDS
        assert "vrchat_group_invite_enabled" in bot.DASHBOARD_WRITABLE_FIELDS

    def test_a_real_change_is_audited_and_a_no_op_is_not(self):
        make_server()
        write({"vrchat_group_id": GROUP_ID})
        assert audit_fields() == ["vrchat_group_id"]
        write({"vrchat_group_id": GROUP_ID})
        assert audit_fields() == ["vrchat_group_id"]
        write({"vrchat_group_invite_enabled": True})
        assert audit_fields() == ["vrchat_group_id", "vrchat_group_invite_enabled"]

    def test_saving_the_toggle_alone_does_not_clear_the_group(self):
        """Read-modify-write, not write-what-was-submitted.

        A form that posts one field must not blank the other -- the failure
        that made panel branding a read-modify-write too.
        """
        make_server()
        write({"vrchat_group_id": GROUP_ID})
        write({"vrchat_group_invite_enabled": True})
        assert stored()["group_id"] == GROUP_ID

    def test_an_unreadable_config_fails_the_save_rather_than_clearing_it(
        self, monkeypatch
    ):
        """The read feeds a write. "Could not read" must never mean "no group".

        Otherwise a connection blip during a save of the toggle would clear a
        verified group id and release its claim for anyone else to take.

        The stored row is checked through a reference taken before the patch,
        because asserting only that the save returned None would pass even if
        the group had been wiped -- the failing re-read at the end of
        write_dashboard_settings returns None too.
        """
        real_load = bot.load_group_invite_config
        make_server(instructions_locale="en-US")
        write({"vrchat_group_id": GROUP_ID})

        def boom(guild_id):
            raise RuntimeError("database is unhappy")

        monkeypatch.setattr(bot, "load_group_invite_config", boom)
        assert (
            write(
                {
                    "vrchat_group_invite_enabled": True,
                    "instructions_locale": "de",
                }
            )
            is None
        )

        assert real_load(GUILD_ID)["group_id"] == GROUP_ID
        assert real_load(GUILD_ID)["enabled"] is False
        # And nothing else in the batch was applied either: the group row is
        # read during validation, before any table has been touched.
        with bot.session_scope() as session:
            assert session.query(bot.Server).first().instructions_locale == "en-US"


# -------------------------------------------------------------------
# What the website is told
# -------------------------------------------------------------------
class TestTheSettingsPayload:
    def test_an_unconfigured_guild_reads_as_no_group(self):
        payload = run(bot.read_dashboard_settings(GUILD_ID))
        assert payload["fields"]["vrchat_group_id"]["value"] is None
        assert payload["fields"]["vrchat_group_invite_enabled"]["value"] is False
        assert payload["group_invite"] == {
            "state": bot.GROUP_SETUP_UNVERIFIED,
            "error": None,
            "group_name": None,
            "icon_url": None,
            "can_invite": False,
            "can_see_members": False,
            "claim_code": None,
            # Whoever this deployment has provisioned, which the admin has to
            # invite. None here only because the test env sets no account.
            "account_to_invite": bot.INVITE_VRCHAT_USER_ID,
            # ...as opposed to whoever actually joined, which is nobody yet.
            "joined_account": None,
            "verified_at": None,
            "requested_at": None,
        }

    def test_stored_values_come_back(self):
        make_server()
        bot.save_group_invite_config(GUILD_ID, group_id=GROUP_ID, enabled=True)
        payload = run(bot.read_dashboard_settings(GUILD_ID))
        assert payload["fields"]["vrchat_group_id"]["value"] == GROUP_ID
        assert payload["fields"]["vrchat_group_invite_enabled"]["value"] is True
        assert payload["group_invite"]["claim_code"].startswith("VRCG-")

    def test_the_verify_verdict_is_reported_as_the_worker_left_it(self):
        make_server()
        bot.save_group_invite_config(GUILD_ID, group_id=GROUP_ID, enabled=True)
        when = datetime.now(timezone.utc)
        with bot.session_scope() as session:
            row = session.query(bot.GroupInviteConfig).first()
            row.verify_state = bot.GROUP_SETUP_NO_INVITE_PERMISSION
            row.verify_error = "The bot is in the group but cannot invite"
            row.group_name = "Test Group"
            row.can_see_members = True
            row.invite_account_id = "usr_0e59962a-3e0d-4303-802b-9314623027e5"
            row.verified_at = when

        block = run(bot.read_dashboard_settings(GUILD_ID))["group_invite"]
        assert block["state"] == bot.GROUP_SETUP_NO_INVITE_PERMISSION
        assert block["error"] == "The bot is in the group but cannot invite"
        assert block["group_name"] == "Test Group"
        assert block["can_invite"] is False
        assert block["can_see_members"] is True
        assert block["joined_account"] == "usr_0e59962a-3e0d-4303-802b-9314623027e5"
        # Serialised, because this payload is JSON on the wire.
        assert block["verified_at"] == when.isoformat()

    def test_the_verdict_is_not_a_settings_field(self):
        """`fields` is the settings contract: everything in it has a plan gate
        and may be submitted back. None of the verify state is writable by
        anybody, so putting it there would invite exactly that."""
        fields = run(bot.read_dashboard_settings(GUILD_ID))["fields"]
        for name in ("state", "claim_code", "can_invite", "verify_state"):
            assert name not in fields

    def test_an_unreadable_config_refuses_the_whole_read(self, monkeypatch):
        """Rendering "no group configured" for a database blip would invite an
        admin to reconfigure a server that was fine -- and the step-6 write
        path would then store the lie. Same refusal as unreadable branding."""
        def boom(guild_id):
            raise RuntimeError("database is unhappy")

        monkeypatch.setattr(bot, "load_group_invite_config", boom)
        assert run(bot.read_dashboard_settings(GUILD_ID)) is None

    def test_the_retired_slash_command_summary_shows_them(self):
        """A setting an admin cannot discover from Discord is one they will ask
        support about. Now that the website has controls for these, the summary
        names them too."""
        make_server()
        bot.save_group_invite_config(GUILD_ID, group_id=GROUP_ID, enabled=True)
        embed = run(bot.build_settings_summary(SummaryGuild()))
        labels = {field.name for field in embed.fields}
        assert any("VRChat group" in label for label in labels)
        assert any("Group invites" in label for label in labels)
        assert any(GROUP_ID in (field.value or "") for field in embed.fields)


# -------------------------------------------------------------------
# The two processes have to agree
# -------------------------------------------------------------------
class TestTheVocabularyMatchesTheWorker:
    def test_every_worker_state_has_a_stored_counterpart(self):
        """bot.py duplicates the worker's STATE_* strings because the worker is
        a separate image with neither discord.py nor a database driver. This is
        the test that stops the copy drifting: a worker publishing a state the
        bot cannot store would be persisted as an unknown string and rendered
        as nothing at all.
        """
        worker_states = {
            value
            for name, value in vars(inviter).items()
            if name.startswith("STATE_") and isinstance(value, str)
        }
        assert worker_states <= bot.GROUP_SETUP_STATES

    def test_ready_means_the_same_thing_on_both_sides(self):
        assert bot.GROUP_SETUP_READY == inviter.STATE_READY

    def test_the_states_the_bot_adds_are_only_the_ones_the_worker_cannot_know(self):
        """"Never checked", "a job is in flight" and "nothing came back" are
        this side's business: the worker only ever reports what it found, and
        it cannot report never having been asked."""
        worker_states = {
            value
            for name, value in vars(inviter).items()
            if name.startswith("STATE_") and isinstance(value, str)
        }
        assert bot.GROUP_SETUP_STATES - worker_states == {
            bot.GROUP_SETUP_UNVERIFIED,
            bot.GROUP_SETUP_CHECKING,
            bot.GROUP_SETUP_TIMED_OUT,
            bot.GROUP_SETUP_WORKER_UNREACHABLE,
            bot.GROUP_SETUP_SEAT_RELEASED,
        }


# -------------------------------------------------------------------
# The website has not caught up yet, and says so out loud
# -------------------------------------------------------------------
class TestEverySettingReachesThePage:
    """Every field the bot declares is rendered by the settings page.

    test_dashboard.py asserts the same thing against a hand-written fixture,
    so a field added to the bot and not to the page slips straight past it.
    This one builds the payload from bot.SETTINGS_FIELDS itself, which is the
    version that notices.

    The gap below is normally empty. It held the two group fields for exactly
    as long as the page had no controls for them.
    """

    @staticmethod
    def not_yet_on_the_page():
        """Derived, not typed: one entry in UNANNOUNCED_FEATURES hides a field
        from the pitch, from /vrcverify_settings and from here alike."""
        return {
            field.name
            for field in bot.SETTINGS_FIELDS
            if field.feature in bot.UNANNOUNCED_FEATURES
        }

    def test_the_page_renders_every_field_except_the_declared_gap(self):
        payload = {
            "guild_id": str(GUILD_ID),
            "premium": {
                "enforced": True,
                "premium": True,
                "grandfathered": False,
                "sku_id": None,
            },
            "auto_verify_column_present": True,
            "choices": {"instructions_locale": list(bot.LANGUAGE_CODES)},
            "fields": {
                field.name: {
                    "value": None,
                    "feature": field.feature,
                    "active": True,
                    "locked": False,
                    "writable": True,
                }
                for field in bot.SETTINGS_FIELDS
            },
        }
        rendered = {
            field.name
            for group in settings_view.build_groups(payload, [], [])
            for field in group["fields"]
        }
        assert set(payload["fields"]) - rendered == self.not_yet_on_the_page()
        # ...and there is no gap any more.
        assert self.not_yet_on_the_page() == set()
