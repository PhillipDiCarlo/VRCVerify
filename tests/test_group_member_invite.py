"""The member-facing group invite (issue #49, phase 5).

Phases 3 and 4 built everything an admin touches: the group is stored, claimed,
and proven. This is the half that acts on it, and it is the only part of the
feature that sends anything to a third party on a member's behalf.

Two properties are worth stating plainly, because both are load-bearing and
neither is obvious from reading a diff:

  * Nothing reaches VRChat until the member presses a button in their own DM.
    The offer costs no API call, the press costs one or two, and a member who
    ignores the DM costs nothing at all. This is a compliance argument as much
    as a privacy one -- the Creator Guidelines treat unsolicited automation as
    abuse, and an invite nobody asked for is exactly that.

  * A "no" is permanent. A member who has said no, is already in the group, or
    already has an invite waiting is never offered one again, and
    confirm_override_block -- the parameter that exists to push an invite past
    someone who blocked the group -- is passed as False every time.

    That last one is explicit rather than omitted because vrchatapi 1.0.0
    defaults it to True. Leaving it out opts in to overriding blocks, in the
    one call where doing so would undo the entire argument for the feature.
    Two tests below hold that line: one on the request we build, one on the
    client default that makes the explicitness necessary.
"""

import asyncio
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

import bot
import vrc_group_inviter as inviter


GUILD_ID = 987654321
MEMBER_ID = 24680
OWNER_ID = 77
SKU_ID = 555000111
GROUP_ID = "grp_0e1d4755-2f87-4129-a192-5587068cbf73"
OTHER_GROUP_ID = "grp_11111111-2222-3333-4444-555555555555"
VRC_USER_ID = "usr_9f8e7d6c-5b4a-3928-1706-abcdef012345"
ACCOUNT_ID = "usr_0e59962a-3e0d-4303-802b-9314623027e5"
CHANNEL_ID = 111222333
MESSAGE_ID = 444555666


def run(coro):
    return asyncio.run(coro)


def make_server(**overrides):
    fields = dict(
        id=10,
        server_id=str(GUILD_ID),
        owner_id=str(OWNER_ID),
        role_id="1",
        instructions_locale="en-US",
    )
    fields.update(overrides)
    with bot.session_scope() as session:
        session.add(bot.Server(**fields))


def make_user(vrc_user_id=VRC_USER_ID, discord_id=str(MEMBER_ID)):
    with bot.session_scope() as session:
        session.add(
            bot.User(
                discord_id=discord_id,
                vrc_user_id=vrc_user_id,
                verification_status=True,
            )
        )


def ready_group(group_id=GROUP_ID, enabled=True, can_invite=True):
    """A group that has been configured, verified, and is ready to invite."""
    bot.save_group_invite_config(GUILD_ID, group_id=group_id, enabled=enabled)
    if group_id is None:
        return bot.load_group_invite_config(GUILD_ID)
    with bot.session_scope() as session:
        row = (
            session.query(bot.GroupInviteConfig)
            .filter_by(server_id=str(GUILD_ID))
            .first()
        )
        row.verify_state = bot.GROUP_SETUP_READY
        row.can_invite = can_invite
        row.can_see_members = True
        row.group_name = "Club LA"
        row.verified_at = datetime.now(timezone.utc)
    return bot.load_group_invite_config(GUILD_ID)


def standing():
    return bot.load_group_invite_request(GUILD_ID, MEMBER_ID)


def set_standing(state, *, group_id=GROUP_ID, age_seconds=0, job_id="job-1"):
    """Put a member's row straight into one state, as if it had settled there."""
    when = datetime.now(timezone.utc) - timedelta(seconds=age_seconds)
    with bot.session_scope() as session:
        session.add(
            bot.GroupInviteRequest(
                server_id=str(GUILD_ID),
                discord_id=str(MEMBER_ID),
                group_id=group_id,
                vrc_user_id=VRC_USER_ID,
                state=state,
                job_id=job_id,
                requested_at=when,
                channel_id=str(CHANNEL_ID),
                message_id=str(MESSAGE_ID),
            )
        )


@pytest.fixture(autouse=True)
def clean_db():
    def wipe():
        with bot.session_scope() as session:
            session.query(bot.Server).delete()
            session.query(bot.User).delete()
            session.query(bot.GroupInviteConfig).delete()
            session.query(bot.GroupInviteRequest).delete()
            session.query(bot.DashboardAudit).delete()
            session.query(bot.PremiumGrandfatherLine).delete()

    wipe()
    bot.premium_status_cache.clear()
    yield
    wipe()
    bot.premium_status_cache.clear()


@pytest.fixture(autouse=True)
def account_configured(monkeypatch):
    monkeypatch.setattr(bot, "INVITE_VRCHAT_USER_ID", ACCOUNT_ID)


@pytest.fixture
def enforced(monkeypatch):
    monkeypatch.setattr(bot, "PREMIUM_SKU_ID", SKU_ID)
    monkeypatch.setattr(bot, "PREMIUM_ENFORCED", True)
    bot.premium_status_cache.clear()


@pytest.fixture
def subscribed(monkeypatch, enforced):
    async def yes(guild_id):
        return True

    monkeypatch.setattr(bot, "guild_has_premium", yes)
    bot.premium_status_cache.clear()


@pytest.fixture
def free(monkeypatch, enforced):
    async def no(guild_id):
        return False

    monkeypatch.setattr(bot, "guild_has_premium", no)
    with bot.session_scope() as session:
        session.add(bot.PremiumGrandfatherLine(id=1, max_server_id=1))
    bot.premium_status_cache.clear()


# -------------------------------------------------------------------
# The two modules must agree about what an outcome is called
# -------------------------------------------------------------------
class TestTheVocabularyMatchesTheWorker:
    """The worker is a separate image with neither discord.py nor a DB driver,
    so its constants are copied into bot.py rather than imported. That is only
    safe while something checks they still agree."""

    def test_every_worker_outcome_has_a_stored_counterpart(self):
        assert inviter.INVITE_STATES <= bot.GROUP_INVITE_STATES

    def test_the_states_the_bot_adds_are_only_the_ones_the_worker_cannot_know(self):
        assert bot.GROUP_INVITE_STATES - inviter.INVITE_STATES == {
            bot.GROUP_INVITE_PENDING,
            bot.GROUP_INVITE_TIMED_OUT,
            bot.GROUP_INVITE_WORKER_UNREACHABLE,
        }

    def test_the_job_type_is_the_same_string_on_both_sides(self):
        assert bot.JOB_SEND_GROUP_INVITE == inviter.JOB_SEND_INVITE

    def test_the_two_job_types_are_distinct(self):
        """They share a request queue and a result queue; a collision would
        have the worker run a setup check for an invite and vice versa."""
        assert bot.JOB_SEND_GROUP_INVITE != bot.JOB_VERIFY_GROUP_SETUP

    def test_every_outcome_a_member_can_reach_has_a_sentence(self):
        """A state with no message key renders as the generic failure, which
        would quietly turn "you are already in the group" into "VRChat did not
        answer" the moment somebody added a state and forgot the copy."""
        assert set(bot.GROUP_INVITE_MESSAGE_KEYS) == bot.GROUP_INVITE_STATES

    def test_every_message_key_exists_in_english(self):
        from locales import localizations

        for key in bot.GROUP_INVITE_MESSAGE_KEYS.values():
            assert key in localizations["en-US"], key


# -------------------------------------------------------------------
# Who may be offered an invite
# -------------------------------------------------------------------
class TestTheGate:
    def test_a_member_who_has_never_asked_may_ask(self):
        assert bot.group_invite_refusal(None, GROUP_ID) is None

    @pytest.mark.parametrize("state", sorted(bot.GROUP_INVITE_SETTLED_STATES))
    def test_a_settled_outcome_is_never_offered_again(self, state):
        set_standing(state)
        assert (
            bot.group_invite_refusal(standing(), GROUP_ID)
            == bot.INVITE_REFUSED_SETTLED
        )

    def test_saying_no_is_permanent(self):
        """The one that matters most. A member with group invites switched off
        has answered the question, and re-asking on every future verification
        is the unsolicited-invite pattern this whole design exists to avoid."""
        set_standing(bot.GROUP_INVITE_BLOCKED, age_seconds=400 * 24 * 3600)
        assert (
            bot.group_invite_refusal(standing(), GROUP_ID)
            == bot.INVITE_REFUSED_SETTLED
        )

    def test_a_request_in_flight_blocks_another(self):
        set_standing(bot.GROUP_INVITE_PENDING)
        assert (
            bot.group_invite_refusal(standing(), GROUP_ID)
            == bot.INVITE_REFUSED_PENDING
        )

    def test_a_transient_failure_may_be_retried_after_the_cooldown(self):
        set_standing(
            bot.GROUP_INVITE_VRCHAT_UNAVAILABLE,
            age_seconds=bot.GROUP_INVITE_COOLDOWN_SECONDS + 60,
        )
        assert bot.group_invite_refusal(standing(), GROUP_ID) is None

    def test_a_transient_failure_may_not_be_retried_immediately(self):
        set_standing(bot.GROUP_INVITE_VRCHAT_UNAVAILABLE, age_seconds=1)
        assert (
            bot.group_invite_refusal(standing(), GROUP_ID)
            == bot.INVITE_REFUSED_COOLDOWN
        )

    def test_a_verdict_about_a_different_group_does_not_apply(self):
        """An admin who changes the server's group has invalidated everything
        learned about the old one. Without this, a member who already belonged
        to the group the server USED to use would be silently locked out of
        the one it uses now, for ever."""
        set_standing(bot.GROUP_INVITE_ALREADY_MEMBER, group_id=OTHER_GROUP_ID)
        assert bot.group_invite_refusal(standing(), GROUP_ID) is None


class TestPendingExpiresOnRead:
    def test_a_fresh_request_is_still_pending(self):
        set_standing(bot.GROUP_INVITE_PENDING, age_seconds=1)
        assert (
            bot.effective_group_invite_state(standing()) == bot.GROUP_INVITE_PENDING
        )

    def test_an_unanswered_request_expires(self):
        set_standing(
            bot.GROUP_INVITE_PENDING,
            age_seconds=bot.GROUP_INVITE_TIMEOUT_SECONDS + 5,
        )
        assert (
            bot.effective_group_invite_state(standing()) == bot.GROUP_INVITE_TIMED_OUT
        )

    def test_pending_with_no_timestamp_is_already_lost(self):
        """Nothing could ever expire it, so it would lock the member out for
        good -- the failure this read-side expiry exists to make impossible."""
        set_standing(bot.GROUP_INVITE_PENDING)
        with bot.session_scope() as session:
            session.query(bot.GroupInviteRequest).first().requested_at = None
        assert (
            bot.effective_group_invite_state(standing()) == bot.GROUP_INVITE_TIMED_OUT
        )

    def test_an_expired_request_stops_blocking_a_new_one(self):
        set_standing(
            bot.GROUP_INVITE_PENDING,
            age_seconds=bot.GROUP_INVITE_COOLDOWN_SECONDS + 60,
        )
        assert bot.group_invite_refusal(standing(), GROUP_ID) is None


# -------------------------------------------------------------------
# Everything that has to be true before any invite exists
# -------------------------------------------------------------------
class TestWhetherTheServerCanInviteAtAll:
    def test_a_ready_paid_enabled_group_is_a_target(self, subscribed):
        make_server()
        ready_group()
        assert run(bot.group_invite_target(GUILD_ID))["group_id"] == GROUP_ID

    def test_no_group_configured(self, subscribed):
        make_server()
        assert run(bot.group_invite_target(GUILD_ID)) is None

    def test_the_toggle_is_off(self, subscribed):
        make_server()
        ready_group(enabled=False)
        assert run(bot.group_invite_target(GUILD_ID)) is None

    def test_setup_was_never_verified(self, subscribed):
        make_server()
        bot.save_group_invite_config(GUILD_ID, group_id=GROUP_ID, enabled=True)
        assert run(bot.group_invite_target(GUILD_ID)) is None

    def test_the_account_lost_its_invite_permission(self, subscribed):
        make_server()
        ready_group(can_invite=False)
        assert run(bot.group_invite_target(GUILD_ID)) is None

    def test_the_subscription_lapsed(self, free):
        """`enabled` stays True on a lapsed server -- the settings field is
        write_locked precisely so the admin's configuration survives. So the
        toggle can never be the only gate, and this is the test that says so."""
        make_server()
        ready_group()
        assert bot.load_group_invite_config(GUILD_ID)["enabled"] is True
        assert run(bot.group_invite_target(GUILD_ID)) is None


# -------------------------------------------------------------------
# Claiming the row and building the job
# -------------------------------------------------------------------
class TestBeginningARequest:
    def build(self):
        return bot.begin_group_invite(
            GUILD_ID,
            MEMBER_ID,
            group_id=GROUP_ID,
            vrc_user_id=VRC_USER_ID,
            channel_id=CHANNEL_ID,
            message_id=MESSAGE_ID,
        )

    def test_the_job_carries_only_what_the_worker_needs(self):
        """No Discord identity crosses to the process holding the VRChat
        session. guildID and jobID are enough for the answer to find its way
        home, and the Discord-to-VRChat mapping is the thing the verification
        log already refuses to publish."""
        job = self.build()
        assert set(job) == {"type", "jobID", "guildID", "groupID", "vrcUserID"}
        assert str(MEMBER_ID) not in str(job)

    def test_the_row_records_where_to_answer(self):
        self.build()
        row = standing()
        assert row["state"] == bot.GROUP_INVITE_PENDING
        assert row["channel_id"] == str(CHANNEL_ID)
        assert row["message_id"] == str(MESSAGE_ID)
        assert row["vrc_user_id"] == VRC_USER_ID

    def test_a_second_press_is_refused(self):
        """The button is removed the moment it is pressed, but two clicks can
        race that edit. The claim re-checks inside the write, which is the only
        place that can settle it."""
        assert self.build() is not None
        assert self.build() is None

    def test_a_settled_member_cannot_start_another(self):
        set_standing(bot.GROUP_INVITE_SENT)
        assert self.build() is None

    def test_a_retry_after_a_transient_failure_gets_a_new_job_id(self):
        set_standing(
            bot.GROUP_INVITE_VRCHAT_UNAVAILABLE,
            age_seconds=bot.GROUP_INVITE_COOLDOWN_SECONDS + 60,
            job_id="old-job",
        )
        job = self.build()
        assert job is not None and job["jobID"] != "old-job"
        assert standing()["job_id"] == job["jobID"]

    def test_a_failed_publish_frees_the_member_to_try_again(self):
        job = self.build()
        bot.abandon_group_invite(GUILD_ID, MEMBER_ID, job["jobID"])
        assert standing()["state"] == bot.GROUP_INVITE_WORKER_UNREACHABLE

    def test_abandoning_an_older_job_cannot_clobber_a_newer_one(self):
        self.build()
        bot.abandon_group_invite(GUILD_ID, MEMBER_ID, "some-older-job")
        assert standing()["state"] == bot.GROUP_INVITE_PENDING


# -------------------------------------------------------------------
# Storing the answer
# -------------------------------------------------------------------
class TestRecordingTheResult:
    def start(self):
        return bot.begin_group_invite(
            GUILD_ID,
            MEMBER_ID,
            group_id=GROUP_ID,
            vrc_user_id=VRC_USER_ID,
            channel_id=CHANNEL_ID,
            message_id=MESSAGE_ID,
        )

    def payload(self, job_id, state=bot.GROUP_INVITE_SENT, **overrides):
        data = {
            "type": inviter.JOB_SEND_INVITE,
            "jobID": job_id,
            "guildID": str(GUILD_ID),
            "groupID": GROUP_ID,
            "ok": state == bot.GROUP_INVITE_SENT,
            "state": state,
            "accountID": ACCOUNT_ID,
        }
        data.update(overrides)
        return data

    def test_an_answer_is_stored_and_carries_the_dm_back(self):
        job = self.start()
        outcome, row = bot.record_group_invite_result(self.payload(job["jobID"]))
        assert outcome == "applied"
        assert row["channel_id"] == str(CHANNEL_ID)
        assert row["message_id"] == str(MESSAGE_ID)
        assert row["discord_id"] == str(MEMBER_ID)
        assert standing()["state"] == bot.GROUP_INVITE_SENT

    def test_a_late_answer_to_an_old_question_is_ignored(self):
        """The round trip is asynchronous, so a slow answer can land after a
        fast one. Letting the straggler win would show the member a verdict
        about a request they have already seen the result of."""
        job = self.start()
        bot.record_group_invite_result(self.payload(job["jobID"]))
        outcome, row = bot.record_group_invite_result(
            self.payload(job["jobID"], state=bot.GROUP_INVITE_BLOCKED)
        )
        assert outcome == "stale"
        assert row is None
        assert standing()["state"] == bot.GROUP_INVITE_SENT

    def test_an_answer_to_a_request_nobody_has_is_ignored(self):
        assert bot.record_group_invite_result(self.payload("nonesuch")) == (
            "unknown_request",
            None,
        )

    def test_a_state_this_bot_does_not_know_is_refused(self):
        job = self.start()
        outcome, _ = bot.record_group_invite_result(
            self.payload(job["jobID"], state="something_new")
        )
        assert outcome == "unknown_state"
        assert standing()["state"] == bot.GROUP_INVITE_PENDING

    @pytest.mark.parametrize("data", [None, {}, {"jobID": "x"}, {"guildID": "1"}])
    def test_a_payload_missing_its_routing_is_refused(self, data):
        assert bot.record_group_invite_result(data)[0] == "bad_payload"


class TestTheResultRouter:
    def test_an_invite_verdict_does_not_reach_the_setup_row(self, monkeypatch):
        """Both types share one result queue. Routing on `type` rather than on
        which fields happen to be present is what keeps an invite verdict from
        being filed against the guild's setup."""
        seen = []
        monkeypatch.setattr(
            bot, "record_group_verification_result", lambda d: seen.append(("setup", d))
        )

        async def capture(data):
            seen.append(("invite", data))

        monkeypatch.setattr(bot, "handle_member_invite_result", capture)
        run(
            bot.handle_group_invite_result(
                {"type": inviter.JOB_SEND_INVITE, "guildID": str(GUILD_ID)}
            )
        )
        assert [kind for kind, _ in seen] == ["invite"]

    def test_a_payload_with_no_type_is_still_a_setup_verdict(self, monkeypatch):
        """One may be in the queue across the upgrade that added invites."""
        seen = []
        monkeypatch.setattr(
            bot,
            "record_group_verification_result",
            lambda d: (seen.append(d), "applied")[1],
        )
        run(
            bot.handle_group_invite_result(
                {"guildID": str(GUILD_ID), "state": "ready"}
            )
        )
        assert len(seen) == 1


# -------------------------------------------------------------------
# The offer DM
# -------------------------------------------------------------------
class FakeMember:
    def __init__(self, member_id=MEMBER_ID):
        self.id = member_id
        self.sent = []

    async def send(self, content=None, **kwargs):
        self.sent.append((content, kwargs))


class FakeGuild:
    def __init__(self):
        self.id = GUILD_ID
        self.name = "Club LA Discord"
        self.preferred_locale = "en-US"


class TestTheOffer:
    def offer(self, premium=None):
        member, guild = FakeMember(), FakeGuild()
        run(bot.offer_group_invite(member, guild, "en-US", premium))
        return member.sent

    def test_a_verified_member_is_offered_a_button(self, subscribed):
        make_server()
        make_user()
        ready_group()
        sent = self.offer()
        assert len(sent) == 1
        content, kwargs = sent[0]
        assert "Club LA" in content
        assert isinstance(kwargs["view"], bot.GroupInviteOfferView)

    def test_the_offer_touches_nothing_outside_the_database(self, subscribed):
        """The reason there is no membership pre-check: an offer costs no
        VRChat call, so the members who never press cost nothing."""
        make_server()
        make_user()
        ready_group()
        assert standing() is None
        self.offer()
        assert standing() is None

    def test_a_member_with_no_vrchat_account_is_not_offered(self, subscribed):
        make_server()
        make_user(vrc_user_id=None)
        ready_group()
        assert self.offer() == []

    def test_a_member_with_no_record_at_all_is_not_offered(self, subscribed):
        make_server()
        ready_group()
        assert self.offer() == []

    @pytest.mark.parametrize("state", sorted(bot.GROUP_INVITE_SETTLED_STATES))
    def test_a_member_who_has_settled_is_never_offered_again(self, subscribed, state):
        make_server()
        make_user()
        ready_group()
        set_standing(state)
        assert self.offer() == []

    def test_a_lapsed_server_offers_nothing(self, free):
        make_server()
        make_user()
        ready_group()
        assert self.offer() == []

    def test_the_callers_resolved_flags_are_believed(self, subscribed):
        """assign_role has already resolved the plan for three other features.
        Passing it in is what keeps the offer from costing a second read."""
        make_server()
        make_user()
        ready_group()
        assert self.offer(premium=bot.PremiumFlags(False, False)) == []

    def test_a_transient_failure_leaves_the_offer_available(self, subscribed):
        make_server()
        make_user()
        ready_group()
        set_standing(
            bot.GROUP_INVITE_VRCHAT_UNAVAILABLE,
            age_seconds=bot.GROUP_INVITE_COOLDOWN_SECONDS + 60,
        )
        assert len(self.offer()) == 1


class TestOnlyMembersOfTheServerMayPress:
    """The offer DM never expires and the button routes for ever.

    So membership has to be re-checked at the press, not merely implied by the
    offer having been made. Without it, somebody who left the server -- or was
    kicked or banned from it -- could press a months-old button and be invited
    into that server's private VRChat group. The whole feature is "members of
    this Discord get into this group", and leaving is the most obvious way to
    stop being one.
    """

    def test_the_press_path_confirms_membership(self):
        import inspect

        source = inspect.getsource(bot.handle_group_invite_press)
        code = "\n".join(
            line for line in source.splitlines() if not line.strip().startswith("#")
        )
        assert "fetch_member_cached" in code
        assert "group_invite_not_a_member" in code

    def test_a_non_member_is_refused_before_anything_is_claimed(self):
        """It must sit ahead of begin_group_invite: a refusal that had already
        claimed the row would burn the cooldown of somebody who did nothing."""
        import inspect

        source = inspect.getsource(bot.handle_group_invite_press)
        assert source.index("fetch_member_cached") < source.index(
            "begin_group_invite"
        )

    def test_an_unconfirmable_membership_is_refused_not_assumed(self):
        """A wrong "yes" puts a stranger in somebody's private group. A wrong
        "no" costs one retry."""
        import inspect

        source = inspect.getsource(bot.handle_group_invite_press)
        after = source[source.index("fetch_member_cached"):]
        assert "except discord.HTTPException" in after
        assert (
            after.index("group_invite_unavailable")
            < after.index("group_invite_not_a_member")
        )

    def test_the_refusal_has_its_own_sentence(self):
        """Reusing the setup-problem copy would tell an ex-member their old
        server is broken, which is both wrong and unactionable."""
        from locales import localizations

        assert "group_invite_not_a_member" in localizations["en-US"]
        assert (
            localizations["en-US"]["group_invite_not_a_member"]
            != localizations["en-US"]["group_invite_setup_problem"]
        )


class TestTheWireCannotSetTheBotsOwnStates:
    """GROUP_INVITE_WORKER_STATES is deliberately narrower than
    GROUP_INVITE_STATES."""

    @pytest.mark.parametrize(
        "state",
        [
            bot.GROUP_INVITE_PENDING,
            bot.GROUP_INVITE_TIMED_OUT,
            bot.GROUP_INVITE_WORKER_UNREACHABLE,
        ],
    )
    def test_a_result_carrying_a_bot_state_is_refused(self, state):
        """"pending" is the dangerous one: accepted, it would push a settled row
        back into flight and rewrite the member's DM to "asking VRChat..."
        permanently, with nothing left to answer it."""
        job = bot.begin_group_invite(
            GUILD_ID,
            MEMBER_ID,
            group_id=GROUP_ID,
            vrc_user_id=VRC_USER_ID,
            channel_id=CHANNEL_ID,
            message_id=MESSAGE_ID,
        )
        outcome, row = bot.record_group_invite_result(
            {
                "type": inviter.JOB_SEND_INVITE,
                "jobID": job["jobID"],
                "guildID": str(GUILD_ID),
                "state": state,
            }
        )
        assert outcome == "unknown_state"
        assert row is None
        assert standing()["state"] == bot.GROUP_INVITE_PENDING

    def test_the_worker_set_is_exactly_what_the_worker_can_produce(self):
        assert bot.GROUP_INVITE_WORKER_STATES == inviter.INVITE_STATES

    def test_the_bots_own_states_are_the_difference(self):
        assert bot.GROUP_INVITE_STATES - bot.GROUP_INVITE_WORKER_STATES == {
            bot.GROUP_INVITE_PENDING,
            bot.GROUP_INVITE_TIMED_OUT,
            bot.GROUP_INVITE_WORKER_UNREACHABLE,
        }


class TestTheButtonIdentifiesItsServer:
    def test_the_press_handler_never_reads_interaction_guild(self):
        """This button only ever lives in a DM, where interaction.guild is
        always None. Reading it for the server's name would render every
        outcome message as "this server", and reading it for the locale would
        put every one of them in English."""
        import inspect

        source = inspect.getsource(bot.handle_group_invite_press)
        # Comments stripped: the rule is explained in one up there, and a test
        # that read it would pass on the explanation alone.
        code = "\n".join(
            line for line in source.splitlines() if not line.strip().startswith("#")
        )
        assert "interaction.guild" not in code
        assert "bot.get_guild(guild_id)" in code

    def test_the_custom_id_carries_the_guild(self):
        view = bot.GroupInviteOfferView(GUILD_ID)
        assert view.children[0].custom_id.endswith(str(GUILD_ID))

    def test_the_template_matches_what_the_view_posts(self):
        """If these two disagree, every button ever posted answers "this
        interaction failed" and nothing in the logs says why."""
        pattern = bot.GroupInviteButton.__discord_ui_compiled_template__
        custom_id = bot.GroupInviteOfferView(GUILD_ID).children[0].custom_id
        match = pattern.fullmatch(custom_id)
        assert match and match["guild_id"] == str(GUILD_ID)

    def test_the_view_never_expires(self):
        """A DM sits in someone's inbox indefinitely; a timeout would leave a
        button that looks live and is not."""
        assert bot.GroupInviteOfferView(GUILD_ID).timeout is None

    def test_the_label_follows_the_servers_language(self):
        from locales import localizations

        view = bot.GroupInviteOfferView(GUILD_ID, "de")
        assert view.children[0].item.label == localizations["de"]["btn_group_invite"]

    def test_an_unknown_locale_falls_back_to_english(self):
        from locales import localizations

        view = bot.GroupInviteOfferView(GUILD_ID, "fr")
        assert (
            view.children[0].item.label
            == localizations["en-US"]["btn_group_invite"]
        )


# -------------------------------------------------------------------
# The worker
# -------------------------------------------------------------------
class FakeApiException(Exception):
    def __init__(self, status=None, body=""):
        super().__init__(f"{status}: {body}")
        self.status = status
        self.body = body
        self.reason = body


def member_record(status):
    return SimpleNamespace(membership_status=status)


class FakeGroupsApi:
    def __init__(self):
        self.calls = []
        self.member = None
        self.get_member_error = FakeApiException(404, "Not Found")
        self.invite_error = None
        # The group exists unless a test says otherwise. Only consulted to read
        # a 404 from create_group_invite, which covers both "no such group" and
        # "no such user".
        self.get_group_error = None

    def get_group(self, group_id, **kwargs):
        self.calls.append(("get_group", group_id))
        if self.get_group_error:
            raise self.get_group_error
        return SimpleNamespace(name="Club LA")

    def get_group_member(self, group_id, user_id, **kwargs):
        self.calls.append(("get_group_member", group_id, user_id))
        if self.member is not None:
            return self.member
        if self.get_member_error:
            raise self.get_member_error
        return None

    def create_group_invite(self, group_id, request, **kwargs):
        self.calls.append(("create_group_invite", group_id, request))
        if self.invite_error:
            raise self.invite_error
        return SimpleNamespace()

    def invites(self):
        return [c for c in self.calls if c[0] == "create_group_invite"]


INVITE_JOB = {
    "type": inviter.JOB_SEND_INVITE,
    "jobID": "job-1",
    "guildID": str(GUILD_ID),
    "groupID": GROUP_ID,
    "vrcUserID": VRC_USER_ID,
}


@pytest.fixture
def api(monkeypatch):
    fake = FakeGroupsApi()
    monkeypatch.setattr(inviter, "GroupsApi", lambda client=None: fake)
    monkeypatch.setattr(inviter.vrchat_session, "get", lambda: (object(), None))
    monkeypatch.setattr(inviter, "ApiException", FakeApiException)
    monkeypatch.setattr(inviter, "request_timeout", lambda: (1, 1))
    monkeypatch.setattr(inviter.time, "sleep", lambda *_: None)
    monkeypatch.setattr(inviter, "_space_invite_calls", lambda: None)
    return fake


class TestSendingOneInvite:
    def test_a_stranger_gets_an_invite(self, api):
        result = inviter.send_group_invite(INVITE_JOB)
        assert result["state"] == inviter.INVITE_SENT
        assert result["ok"] is True
        assert len(api.invites()) == 1

    def test_membership_is_checked_before_anything_is_sent(self, api):
        inviter.send_group_invite(INVITE_JOB)
        assert api.calls[0][0] == "get_group_member"

    def test_a_block_is_never_overridden(self, api):
        """confirm_override_block exists to push an invite past someone who
        blocked the group. Setting it would throw away the entire compliance
        argument for this feature."""
        inviter.send_group_invite(INVITE_JOB)
        request = api.invites()[0][2]
        # False, not "unset". vrchatapi 1.0.0 defaults this field to True, so
        # an invite built without naming it opts in to overriding blocks.
        assert request.confirm_override_block is False
        assert request.user_id == VRC_USER_ID

    def test_the_client_default_is_the_dangerous_one(self):
        """Pinned because it is the reason the line above is explicit. If a
        future vrchatapi flips this to False, the explicit argument becomes
        redundant rather than wrong -- but nobody should discover the current
        default by shipping an override nobody asked for."""
        from vrchatapi.models.create_group_invite_request import (
            CreateGroupInviteRequest,
        )

        assert CreateGroupInviteRequest(user_id=VRC_USER_ID).confirm_override_block

    def test_an_existing_member_is_not_invited(self, api):
        api.member = member_record("member")
        result = inviter.send_group_invite(INVITE_JOB)
        assert result["state"] == inviter.INVITE_ALREADY_MEMBER
        assert api.invites() == []

    def test_a_waiting_invite_is_not_duplicated(self, api):
        api.member = member_record("invited")
        result = inviter.send_group_invite(INVITE_JOB)
        assert result["state"] == inviter.INVITE_ALREADY_INVITED
        assert api.invites() == []

    def test_a_pending_join_request_is_left_alone(self, api):
        """They asked to join under their own steam. An invite would cut across
        a moderator's pending decision."""
        api.member = member_record("requested")
        result = inviter.send_group_invite(INVITE_JOB)
        assert result["state"] == inviter.INVITE_ALREADY_INVITED
        assert api.invites() == []

    @pytest.mark.parametrize("status", ["banned", "userblocked"])
    def test_a_banned_member_is_not_invited(self, api, status):
        api.member = member_record(status)
        result = inviter.send_group_invite(INVITE_JOB)
        assert result["state"] == inviter.INVITE_BANNED
        assert api.invites() == []

    def test_a_member_who_switched_group_invites_off(self, api):
        api.invite_error = FakeApiException(403, "You can't invite that user")
        result = inviter.send_group_invite(INVITE_JOB)
        assert result["state"] == inviter.INVITE_BLOCKED

    def test_joining_between_the_check_and_the_invite(self, api):
        api.invite_error = FakeApiException(400, "already a member of this group")
        result = inviter.send_group_invite(INVITE_JOB)
        assert result["state"] == inviter.INVITE_ALREADY_MEMBER

    def test_a_group_that_has_gone(self, api):
        api.invite_error = FakeApiException(404, "Can't find group!")
        api.get_group_error = FakeApiException(404, "Can't find group!")
        result = inviter.send_group_invite(INVITE_JOB)
        assert result["state"] == inviter.INVITE_GROUP_NOT_FOUND

    def test_a_403_on_the_precheck_still_sends_the_invite(self, api):
        """What this 403 means is genuinely unknown, and that is the point.

        Two fixes in a row assigned it a precise meaning from one observation
        each -- "the target is not a member", then "the bot is not a member" --
        and both were wrong; in the incident it came from a userId that
        resolved to no VRChat account at all. The invite attempt is what
        produces an authoritative answer, so it always happens.
        """
        api.get_member_error = FakeApiException(403, "You're not a member.")
        result = inviter.send_group_invite(INVITE_JOB)
        assert result["state"] == inviter.INVITE_SENT
        assert len(api.invites()) == 1

    def test_vrchat_being_down_on_the_precheck_still_tries(self, api):
        """The precheck buys a better sentence, nothing more. Its failure must
        never cost the member their invite."""
        api.get_member_error = FakeApiException(500, "Internal Server Error")
        result = inviter.send_group_invite(INVITE_JOB)
        assert result["state"] == inviter.INVITE_SENT

    def test_a_dead_session_is_the_one_precheck_failure_that_stops(
        self, api, monkeypatch
    ):
        """Not best effort: the invite cannot work either, and pressing on
        would spend a second call finding that out."""

        class FakeUnauthorized(Exception):
            status = 401

        monkeypatch.setattr(inviter, "UnauthorizedException", FakeUnauthorized)
        api.get_member_error = FakeUnauthorized()
        result = inviter.send_group_invite(INVITE_JOB)
        assert result["state"] == inviter.INVITE_VRCHAT_UNAVAILABLE
        assert api.invites() == []

    def test_vrchat_being_down_on_the_invite_is_reported(self, api):
        api.invite_error = FakeApiException(500, "Internal Server Error")
        result = inviter.send_group_invite(INVITE_JOB)
        assert result["state"] == inviter.INVITE_VRCHAT_UNAVAILABLE

    def test_no_session_means_no_call(self, api, monkeypatch):
        monkeypatch.setattr(
            inviter.vrchat_session, "get", lambda: (None, {"error_message": "down"})
        )
        result = inviter.send_group_invite(INVITE_JOB)
        assert result["state"] == inviter.INVITE_VRCHAT_UNAVAILABLE
        assert api.calls == []

    @pytest.mark.parametrize(
        "job",
        [
            dict(INVITE_JOB, groupID=None),
            dict(INVITE_JOB, groupID="usr_not-a-group"),
            dict(INVITE_JOB, vrcUserID=None),
            dict(INVITE_JOB, vrcUserID="grp_not-a-user"),
        ],
    )
    def test_a_malformed_job_never_reaches_the_api(self, api, job):
        """Passing None to the client raises ApiValueError, which subclasses
        ValueError rather than ApiException -- it would sail past every handler
        and leave the member watching a message that never updates."""
        assert inviter.send_group_invite(job)["state"] == inviter.INVITE_BAD_JOB
        assert api.calls == []

    def test_the_result_names_the_account_that_acted(self, api, monkeypatch):
        monkeypatch.setattr(inviter, "INVITE_ACCOUNT_USER_ID", ACCOUNT_ID)
        assert inviter.send_group_invite(INVITE_JOB)["accountID"] == ACCOUNT_ID

    def test_the_result_carries_no_discord_identity(self, api):
        result = inviter.send_group_invite(INVITE_JOB)
        assert set(result) == {
            "type",
            "jobID",
            "guildID",
            "groupID",
            "ok",
            "state",
            "accountID",
        }


class TestThePrecheckCanNeverBlockAnInvite:
    """The invariant both production failures violated, in one place.

    The membership check exists to word a message better. Whatever it does,
    an invite must still be attempted -- because create_group_invite is where
    every authoritative answer comes from.
    """

    # 401 is deliberately absent: vrchatapi raises UnauthorizedException for
    # it, not a bare ApiException, and that is the one precheck failure that
    # does stop -- see the test above. Listing it here would pin behaviour the
    # real client cannot produce.
    @pytest.mark.parametrize("status", [400, 403, 404, 429, 500, 502, 503])
    def test_no_precheck_status_prevents_the_invite(self, api, status):
        api.get_member_error = FakeApiException(status, "whatever")
        inviter.send_group_invite(INVITE_JOB)
        assert len(api.invites()) == 1

    def test_the_session_exception_really_is_a_subclass(self):
        """The two except clauses in send_group_invite are ordered on this. If
        UnauthorizedException stopped subclassing ApiException, the dead-session
        branch would still work; if the order were swapped, it never would."""
        from vrchatapi.exceptions import ApiException, UnauthorizedException

        assert issubclass(UnauthorizedException, ApiException)

    def test_a_precheck_returning_nothing_useful_still_invites(self, api):
        api.member = SimpleNamespace(membership_status=None)
        api.get_member_error = None
        assert (
            inviter.send_group_invite(INVITE_JOB)["state"] == inviter.INVITE_SENT
        )

    def test_only_a_recognised_standing_stops_it(self, api):
        """The two cases the check is FOR, and nothing else."""
        stopped = []
        for status in ["member", "invited", "requested", "banned", "userblocked"]:
            api.calls.clear()
            api.member = member_record(status)
            inviter.send_group_invite(INVITE_JOB)
            if not api.invites():
                stopped.append(status)
        assert stopped == ["member", "invited", "requested", "banned", "userblocked"]


class TestTellingTwoKindsOf404Apart:
    """The incident of 2026-08-20, as a test.

    A member whose linked VRChat account did not resolve was told their
    server's group "isn't set up correctly" and sent to find an admin. The
    group was fine; the account was not, and only they could fix it.

    create_group_invite answers 404 for both, and the body does not reliably
    say which. Rather than matching on wording -- the habit behind two wrong
    fixes already -- the worker asks whether the group still resolves, which
    has an unambiguous answer.
    """

    def test_a_member_whose_account_does_not_resolve(self, api):
        api.invite_error = FakeApiException(404, "Not Found")
        result = inviter.send_group_invite(INVITE_JOB)
        assert result["state"] == inviter.INVITE_USER_NOT_FOUND

    def test_a_group_that_really_has_gone(self, api):
        api.invite_error = FakeApiException(404, "Can't find group!")
        api.get_group_error = FakeApiException(404, "Can't find group!")
        result = inviter.send_group_invite(INVITE_JOB)
        assert result["state"] == inviter.INVITE_GROUP_NOT_FOUND

    def test_the_group_is_only_consulted_on_a_404(self, api):
        """One extra call, and only on a path that has already failed."""
        inviter.send_group_invite(INVITE_JOB)
        assert [c[0] for c in api.calls if c[0] == "get_group"] == []

    def test_an_unrelated_failure_does_not_invent_a_missing_group(self, api):
        """A 500 while asking is not evidence the group has gone. Falling back
        to blaming the member's account is the recoverable half -- they have
        something to try, and a group that really has gone shows up plainly on
        the dashboard's own setup check."""
        api.invite_error = FakeApiException(404, "Not Found")
        api.get_group_error = FakeApiException(500, "Internal Server Error")
        result = inviter.send_group_invite(INVITE_JOB)
        assert result["state"] == inviter.INVITE_USER_NOT_FOUND

    def test_the_member_is_not_sent_to_blame_their_admin(self):
        """The whole point: these two must not share a sentence."""
        assert (
            bot.GROUP_INVITE_MESSAGE_KEYS[bot.GROUP_INVITE_USER_NOT_FOUND]
            != bot.GROUP_INVITE_MESSAGE_KEYS[bot.GROUP_INVITE_GROUP_NOT_FOUND]
        )

    def test_re_verifying_gets_them_another_chance(self):
        """Their account resolving again is exactly the fix, so this must not
        be one of the outcomes that is never offered again."""
        assert bot.GROUP_INVITE_USER_NOT_FOUND not in bot.GROUP_INVITE_SETTLED_STATES


class TestTellingTwoKindsOf403Apart:
    """403 from create_group_invite is ambiguous, and only one side of it is
    permanent. Getting this backwards records a refusal the member never made
    and locks them out of the group for good."""

    def test_the_recipient_refusing_is_the_members_answer(self, api):
        api.invite_error = FakeApiException(403, "You can't invite that user")
        assert (
            inviter.send_group_invite(INVITE_JOB)["state"] == inviter.INVITE_BLOCKED
        )

    def test_a_json_envelope_around_the_same_wording_still_counts(self, api):
        api.invite_error = FakeApiException(
            403, '{"error":{"message":"You can\'t invite that user","status_code":403}}'
        )
        assert (
            inviter.send_group_invite(INVITE_JOB)["state"] == inviter.INVITE_BLOCKED
        )

    def test_our_own_permission_being_revoked_is_not(self, api):
        api.invite_error = FakeApiException(403, "Insufficient permissions")
        assert (
            inviter.send_group_invite(INVITE_JOB)["state"]
            == inviter.INVITE_NO_PERMISSION
        )

    def test_an_unrecognised_403_fails_to_the_retryable_side(self, api):
        """Guessing wrong this way costs a retry. Guessing wrong the other way
        costs somebody their place in the group permanently."""
        api.invite_error = FakeApiException(403, "something nobody has seen yet")
        state = inviter.send_group_invite(INVITE_JOB)["state"]
        assert state == inviter.INVITE_NO_PERMISSION
        assert state not in bot.GROUP_INVITE_SETTLED_STATES

    def test_the_members_refusal_is_permanent(self, api):
        api.invite_error = FakeApiException(403, "You can't invite that user")
        state = inviter.send_group_invite(INVITE_JOB)["state"]
        assert state in bot.GROUP_INVITE_SETTLED_STATES


class TestThroughputIsSpaced:
    def test_consecutive_invites_are_spaced_apart(self, monkeypatch):
        """One account issues invites for every guild, so a verification rush
        must not be able to spend the whole shared budget in a burst."""
        slept = []
        monkeypatch.setattr(inviter.time, "sleep", slept.append)
        monkeypatch.setattr(inviter, "INVITE_MIN_SPACING_SECONDS", 3.0)
        monkeypatch.setattr(inviter, "_last_invite_call", 0.0)
        clock = iter([1000.0, 1000.0, 1000.1, 1000.1])
        monkeypatch.setattr(inviter.time, "monotonic", lambda: next(clock))
        inviter._space_invite_calls()
        inviter._space_invite_calls()
        assert slept and slept[-1] >= 2.5

    def test_the_wait_is_jittered(self):
        """A fixed interval is exactly what makes many callers synchronise into
        a spike, which VRChat's guidelines call out by name."""
        seen = {round(inviter.random.uniform(0.0, 0.5), 6) for _ in range(50)}
        assert len(seen) > 1


class TestTheWorkerSurvivesItsOwnConfiguration:
    """Found by pinning these in conftest: an empty value crashed the import.

    A half-written line in a .env file -- `INVITE_MIN_SPACING_SECONDS=` with
    nothing after it -- used to raise ValueError at module import, before
    logging was configured, taking the worker down with a traceback naming
    neither the variable nor the file.
    """

    @pytest.mark.parametrize("raw", ["", "   ", "three", None])
    def test_a_blank_or_malformed_spacing_falls_back(self, monkeypatch, raw):
        if raw is None:
            monkeypatch.delenv("INVITE_MIN_SPACING_SECONDS", raising=False)
        else:
            monkeypatch.setenv("INVITE_MIN_SPACING_SECONDS", raw)
        assert inviter._float_env("INVITE_MIN_SPACING_SECONDS", 3.0) == 3.0

    @pytest.mark.parametrize("raw", ["", "   ", "lots", None])
    def test_a_blank_or_malformed_retry_count_falls_back(self, monkeypatch, raw):
        if raw is None:
            monkeypatch.delenv("INVITE_CALL_RETRIES", raising=False)
        else:
            monkeypatch.setenv("INVITE_CALL_RETRIES", raw)
        assert inviter._int_env("INVITE_CALL_RETRIES", 3) == 3

    def test_a_real_value_is_still_honoured(self, monkeypatch):
        monkeypatch.setenv("INVITE_MIN_SPACING_SECONDS", "7.5")
        assert inviter._float_env("INVITE_MIN_SPACING_SECONDS", 3.0) == 7.5

    def test_spacing_cannot_be_negative(self, monkeypatch):
        """A negative gap would make _space_invite_calls sleep on a wait that
        has always already elapsed, which is a throttle that does nothing."""
        monkeypatch.setenv("INVITE_MIN_SPACING_SECONDS", "-10")
        assert inviter._float_env("INVITE_MIN_SPACING_SECONDS", 3.0) == 0.0


class TestTheExpiryTaskIsOwned:
    def test_the_bot_keeps_a_reference_to_it(self):
        """asyncio holds only a weak reference to a running task, and this one
        sleeps for GROUP_INVITE_TIMEOUT_SECONDS before doing anything. Collected
        mid-await, the member is left watching "asking VRChat" for ever with
        nothing in the log to say why."""
        assert isinstance(bot._group_invite_expiry_tasks, set)

    def test_it_is_not_in_the_background_task_registry(self):
        """That dict is the registry of the singletons on_ready starts, and its
        contents are asserted against a fixed list in test_background_tasks."""
        assert "group_invite_expiry" not in bot.background_tasks


class TestTheCrashFallbackAnswersTheRightQuestion:
    def test_an_invite_that_dies_twice_reports_an_invite_failure(self, monkeypatch):
        """Both job types share a result queue and the bot routes on `type`. A
        setup-shaped apology for a failed invite would be filed against the
        guild's setup row, and the member's DM would hang for ever."""
        published = []
        monkeypatch.setattr(inviter, "publish_result", published.append)

        def boom(job):
            raise RuntimeError("nope")

        monkeypatch.setitem(inviter.HANDLERS, inviter.JOB_SEND_INVITE, boom)

        acks = []
        channel = SimpleNamespace(
            basic_ack=lambda **kw: acks.append(("ack", kw)),
            basic_nack=lambda **kw: acks.append(("nack", kw)),
        )
        method = SimpleNamespace(delivery_tag=1, redelivered=True)
        inviter.process_job(channel, method, None, json.dumps(INVITE_JOB))

        assert len(published) == 1
        assert published[0]["type"] == inviter.JOB_SEND_INVITE
        assert published[0]["state"] == inviter.INVITE_VRCHAT_UNAVAILABLE


# -------------------------------------------------------------------
# The seam into the verification flow
# -------------------------------------------------------------------
class TestAssignRoleOffersTheInvite:
    def test_every_verification_path_goes_through_one_function(self):
        """A fresh verification, a re-check, pressing Begin Verification while
        already verified, and auto-verify on join all call assign_role. That is
        why the offer lives there and not in any one of them."""
        import inspect

        source = inspect.getsource(bot.assign_role)
        assert "offer_group_invite" in source

    def test_a_broken_offer_cannot_undo_a_successful_verification(self):
        """The role is already on and the success DM has gone out. Milestone
        bookkeeping still has to run after this."""
        import inspect

        source = inspect.getsource(bot.assign_role)
        offer_at = source.index("offer_group_invite")
        assert "try:" in source[:offer_at]
        assert "except Exception:" in source[offer_at:]
