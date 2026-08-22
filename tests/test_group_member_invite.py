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
# The account half of the button's custom_id, for VRC_USER_ID.
FINGERPRINT = bot.group_invite_account_fingerprint(VRC_USER_ID)
OTHER_VRC_USER_ID = "usr_deadbeef-0000-1111-2222-333344445555"


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


def make_user(vrc_user_id=VRC_USER_ID, discord_id=str(MEMBER_ID), verified=True):
    with bot.session_scope() as session:
        session.add(
            bot.User(
                discord_id=discord_id,
                vrc_user_id=vrc_user_id,
                verification_status=verified,
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


def localized(key, **kwargs):
    """What the member is shown for one message key, in the test server's
    locale -- so an assertion names the key rather than pasting the sentence,
    which would then have to be edited every time the copy is reworded."""
    kwargs.setdefault("server", "Club LA Discord")
    kwargs.setdefault("group", "Club LA")
    return bot.get_message(key, SimpleNamespace(locale="en-US"), **kwargs)


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
            session.query(bot.GroupSeatLease).delete()
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

    def test_a_guild_the_bot_cannot_see_is_refused_not_skipped(self):
        """get_guild returns None when the bot has been REMOVED from a server,
        as well as when the gateway has not delivered it yet. Skipping the
        membership check in that case let an ex-member of a server the bot had
        been kicked out of press a months-old button and be invited into that
        server's private group."""
        import inspect

        source = inspect.getsource(bot.handle_group_invite_press)
        guard = source.index("if guild is None")
        assert guard < source.index("fetch_member_cached")
        # It must RETURN there, not fall through to the check it cannot do.
        assert "return" in source[guard : source.index("fetch_member_cached")]

    def test_the_refusal_has_its_own_sentence(self):
        """Reusing the setup-problem copy would tell an ex-member their old
        server is broken, which is both wrong and unactionable."""
        from locales import localizations

        assert "group_invite_not_a_member" in localizations["en-US"]
        assert (
            localizations["en-US"]["group_invite_not_a_member"]
            != localizations["en-US"]["group_invite_setup_problem"]
        )


class FakeInteractionResponse:
    def __init__(self, outer):
        self.outer = outer

    async def edit_message(self, content=None, view=None):
        self.outer.edits.append((content, view))


class FakeInteraction:
    """Just enough of an Interaction for the press handler to run end to end."""

    def __init__(self, user_id=MEMBER_ID):
        self.edits = []
        self.user = SimpleNamespace(id=user_id)
        self.channel_id = CHANNEL_ID
        self.message = SimpleNamespace(id=MESSAGE_ID)
        self.response = FakeInteractionResponse(self)

    async def edit_original_response(self, content=None, view=None):
        self.edits.append((content, view))

    @property
    def settled(self):
        """What the member is left looking at."""
        return self.edits[-1]


@pytest.fixture
def pressable(monkeypatch):
    """A server, a ready group, a seat, and a queue that records instead of
    publishing -- everything a press needs except the member's own record."""
    make_server()
    ready_group()
    only = bot.InviteAccount(
        user_id=ACCOUNT_ID, queue="vrcverify_group_invites", seats=100
    )
    monkeypatch.setattr(bot, "INVITE_ACCOUNTS", (only,))
    monkeypatch.setattr(bot, "INVITE_ACCOUNTS_BY_ID", {only.user_id: only})

    guild = FakeGuild()
    monkeypatch.setattr(bot.bot, "get_guild", lambda gid: guild)

    async def still_here(g, uid):
        return SimpleNamespace(id=uid)

    monkeypatch.setattr(bot, "fetch_member_cached", still_here)

    published = []
    monkeypatch.setattr(
        bot,
        "publish_group_invite_job",
        lambda job, queue=None: (published.append((job, queue)), True)[1],
    )
    return published


class TestOnlyTheVerifiedAccountMayBeInvited:
    """The hole this closes, found live on 2026-08-22.

    A Discord account verified with an 18+ VRChat account, was offered the
    button, then re-verified with a VRChat account that was NOT 18+. It was
    told "you're not 18+" and lost its role -- and the original button was
    still sitting in the DM. Pressing it invited the new, unverified account
    into the server's private group.

    Two things were wrong. The press re-checked the config, the seat, and guild
    membership, but never the one thing the feature exists to enforce. And the
    VRChat account was resolved at press time from whatever was linked THEN,
    so the button was not an offer to one account -- it was a standing
    capability that followed the member's current link wherever it went.
    """

    def press(self, interaction=None, account=FINGERPRINT):
        interaction = interaction or FakeInteraction()
        run(bot.handle_group_invite_press(interaction, GUILD_ID, account))
        return interaction

    def test_the_verified_account_still_gets_its_invite(self, subscribed, pressable):
        """The control: nothing below is refusing everybody."""
        make_user()
        self.press()
        assert len(pressable) == 1
        job, queue = pressable[0]
        assert job["vrcUserID"] == VRC_USER_ID
        assert standing()["state"] == bot.GROUP_INVITE_PENDING

    def test_a_member_who_is_no_longer_18_plus_is_refused(self, subscribed, pressable):
        """The exact live failure."""
        make_user(verified=False)
        interaction = self.press()
        content, view = interaction.settled
        assert content == localized("group_invite_not_verified")
        assert pressable == []

    def test_the_refusal_claims_nothing(self, subscribed, pressable):
        """It has to sit ahead of begin_group_invite, for the same reason the
        membership check does: a refusal that claimed the row first would burn
        the cooldown of somebody who was told no."""
        make_user(verified=False)
        self.press()
        assert standing() is None

    def test_being_told_no_is_not_retryable(self, subscribed, pressable):
        """Their verification is what is wrong. Handing back a button invites
        them to press it again, and it will refuse again every time."""
        make_user(verified=False)
        _, view = self.press().settled
        assert view is None

    def test_a_relinked_account_cannot_use_the_old_button(
        self, subscribed, pressable
    ):
        """Even when the new account IS 18+. The offer was made to one account
        and stays made to that one; re-verifying issues a fresh button."""
        make_user(vrc_user_id=OTHER_VRC_USER_ID, verified=True)
        interaction = self.press()
        content, view = interaction.settled
        assert content == localized("group_invite_account_changed")
        assert pressable == []
        assert standing() is None

    def test_the_invite_goes_to_the_account_the_offer_named(
        self, subscribed, pressable
    ):
        """A press must never invite an account that was not the one checked."""
        make_user()
        self.press()
        job, _ = pressable[0]
        assert bot.group_invite_account_fingerprint(job["vrcUserID"]) == FINGERPRINT

    def test_a_button_stamped_for_someone_else_is_refused(
        self, subscribed, pressable
    ):
        """The custom_id is not evidence of anything. A button carrying another
        member's fingerprint, pressed by this one, is a mismatch like any
        other -- it is compared against the presser's own record, never
        trusted to name the account to invite."""
        make_user()
        interaction = self.press(
            account=bot.group_invite_account_fingerprint(OTHER_VRC_USER_ID)
        )
        assert interaction.settled[0] == localized("group_invite_account_changed")
        assert pressable == []

    def test_the_two_refusals_read_differently(self):
        """"You are not 18+" and "that was a different account" need different
        actions from the member. Collapsing them into the generic failure
        would tell someone whose verification lapsed that VRChat is down."""
        from locales import localizations

        en = localizations["en-US"]
        assert (
            en["group_invite_not_verified"]
            != en["group_invite_account_changed"]
            != en["group_invite_unavailable"]
        )

    @pytest.mark.parametrize(
        "key", ["group_invite_not_verified", "group_invite_account_changed"]
    )
    def test_every_locale_can_say_it(self, key):
        from locales import localizations

        assert all(key in strings for strings in localizations.values())


class TestTheOfferIsStampedForOneAccount:
    def test_the_button_carries_the_members_own_account(self, subscribed):
        make_server()
        make_user()
        ready_group()
        member, guild = FakeMember(), FakeGuild()
        run(bot.offer_group_invite(member, guild, "en-US", None))
        view = member.sent[0][1]["view"]
        pattern = bot.GroupInviteButton.__discord_ui_compiled_template__
        match = pattern.fullmatch(view.children[0].custom_id)
        assert match["account"] == FINGERPRINT

    def test_a_member_the_database_says_is_not_18_is_never_offered(self, subscribed):
        """offer_group_invite is only called from assign_role's 18+ branch, so
        this can only fire if the stored verdict disagrees with the one that
        just ran. Offering anyway would put a live button in the DMs of
        somebody the database says is not 18+."""
        make_server()
        make_user(verified=False)
        ready_group()
        member, guild = FakeMember(), FakeGuild()
        run(bot.offer_group_invite(member, guild, "en-US", None))
        assert member.sent == []

    def test_the_fingerprint_distinguishes_accounts(self):
        assert bot.group_invite_account_fingerprint(
            VRC_USER_ID
        ) != bot.group_invite_account_fingerprint(OTHER_VRC_USER_ID)

    def test_the_fingerprint_fits_the_template(self):
        """A custom_id is capped at 100 characters and the template pins this
        field's shape. A fingerprint that did not match would route nothing."""
        import re

        for uid in (VRC_USER_ID, OTHER_VRC_USER_ID, "8JoV9XEdKs", "usr_" + "z" * 60):
            fp = bot.group_invite_account_fingerprint(uid)
            assert re.fullmatch(r"[0-9a-f]{16}", fp)

    def test_the_whole_custom_id_fits_discords_limit(self):
        custom_id = (
            bot.GroupInviteOfferView(2**63 - 1, FINGERPRINT).children[0].custom_id
        )
        assert len(custom_id) <= 100

    def test_v1_buttons_no_longer_route(self):
        """Every button posted before this fix carries no account fingerprint,
        so there is no way to honour one safely. They were the vulnerable
        population; retiring them IS the fix, not a side effect."""
        pattern = bot.GroupInviteButton.__discord_ui_compiled_template__
        assert pattern.fullmatch(f"vrcverify:groupinvite:v1:{GUILD_ID}") is None


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


class TestACooldownDoesNotStrandTheMember:
    """A transient failure gives the button back; the cooldown then refuses a
    press for a few minutes. Removing the button on that refusal left the
    member holding "wait a few minutes" with nothing to wait for -- short of
    verifying all over again."""

    def test_a_cooldown_refusal_keeps_the_button(self):
        import inspect

        source = inspect.getsource(bot.handle_group_invite_press)
        branch = source[source.index("group_invite_too_soon"):]
        assert "INVITE_REFUSED_COOLDOWN" in branch[:400]

    def test_the_two_refusals_are_not_treated_alike(self):
        """An in-flight request needs no button: its result is already on its
        way to rewrite the message. A cooldown has nothing coming."""
        assert bot.INVITE_REFUSED_COOLDOWN != bot.INVITE_REFUSED_PENDING

    def test_a_transient_failure_is_retryable_after_the_wait(self):
        """The path the button is being kept for, end to end."""
        set_standing(
            bot.GROUP_INVITE_VRCHAT_UNAVAILABLE,
            age_seconds=1,
        )
        assert (
            bot.group_invite_refusal(standing(), GROUP_ID)
            == bot.INVITE_REFUSED_COOLDOWN
        )
        with bot.session_scope() as session:
            row = session.query(bot.GroupInviteRequest).first()
            row.requested_at = datetime.now(timezone.utc) - timedelta(
                seconds=bot.GROUP_INVITE_COOLDOWN_SECONDS + 5
            )
        assert bot.group_invite_refusal(standing(), GROUP_ID) is None


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
        view = bot.GroupInviteOfferView(GUILD_ID, FINGERPRINT)
        assert str(GUILD_ID) in view.children[0].custom_id

    def test_the_template_matches_what_the_view_posts(self):
        """If these two disagree, every button ever posted answers "this
        interaction failed" and nothing in the logs says why."""
        pattern = bot.GroupInviteButton.__discord_ui_compiled_template__
        custom_id = (
            bot.GroupInviteOfferView(GUILD_ID, FINGERPRINT).children[0].custom_id
        )
        match = pattern.fullmatch(custom_id)
        assert match and match["guild_id"] == str(GUILD_ID)
        assert match["account"] == FINGERPRINT

    def test_the_view_never_expires(self):
        """A DM sits in someone's inbox indefinitely; a timeout would leave a
        button that looks live and is not."""
        assert bot.GroupInviteOfferView(GUILD_ID, FINGERPRINT).timeout is None

    def test_the_label_follows_the_servers_language(self):
        from locales import localizations

        view = bot.GroupInviteOfferView(GUILD_ID, FINGERPRINT, "de")
        assert view.children[0].item.label == localizations["de"]["btn_group_invite"]

    def test_an_unknown_locale_falls_back_to_english(self):
        from locales import localizations

        view = bot.GroupInviteOfferView(GUILD_ID, FINGERPRINT, "fr")
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
        # The group exists, and we can still invite into it, unless a test says
        # otherwise. Consulted only to read a 403 or 404 from
        # create_group_invite -- both of which are ambiguous on their own.
        self.get_group_error = None
        self.group_permissions = ["group-invites-manage", "group-members-viewall"]

    def get_group(self, group_id, **kwargs):
        self.calls.append(("get_group", group_id))
        if self.get_group_error:
            raise self.get_group_error
        return SimpleNamespace(
            name="Club LA",
            my_member=SimpleNamespace(permissions=list(self.group_permissions)),
        )

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


# Captured before any fixture replaces it. The `api` fixture stubs the throttle
# out so the other tests do not sleep, so the throttle's own tests need a way
# back to the real one.
REAL_SPACE_INVITE_CALLS = inviter._space_invite_calls

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

    def test_a_banned_member_is_not_invited(self, api):
        api.member = member_record("banned")
        result = inviter.send_group_invite(INVITE_JOB)
        assert result["state"] == inviter.INVITE_BANNED
        assert api.invites() == []

    def test_a_member_who_blocked_the_group_is_not_invited(self, api):
        """userblocked is its own GroupMemberStatus and is NOT a moderator ban.
        Folding it into banned told the member "only a group moderator can
        change that" about a block they placed themselves."""
        api.member = member_record("userblocked")
        result = inviter.send_group_invite(INVITE_JOB)
        assert result["state"] == inviter.INVITE_BLOCKED
        assert api.invites() == []

    def test_a_member_who_switched_group_invites_off(self, api):
        """We still hold the invite permission, so the refusal is theirs."""
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

    def test_an_unrelated_failure_blames_nobody(self, api):
        """A 500 while asking is not evidence about the group OR the member.
        Every other answer here blames somebody and two of them stick, so not
        knowing is reported as not knowing."""
        api.invite_error = FakeApiException(404, "Not Found")
        api.get_group_error = FakeApiException(500, "Internal Server Error")
        result = inviter.send_group_invite(INVITE_JOB)
        assert result["state"] == inviter.INVITE_VRCHAT_UNAVAILABLE

    def test_a_dead_session_during_the_probe_blames_nobody(self, api, monkeypatch):
        """UnauthorizedException subclasses ApiException, so the probe used to
        read a dead session as "not a 404, therefore the group is present,
        therefore the member's account is at fault" -- and told them to
        re-verify an account that was never the problem."""

        class FakeUnauthorized(Exception):
            status = 401

        monkeypatch.setattr(inviter, "UnauthorizedException", FakeUnauthorized)
        api.invite_error = FakeApiException(404, "Not Found")
        api.get_group_error = FakeUnauthorized()
        result = inviter.send_group_invite(INVITE_JOB)
        assert result["state"] == inviter.INVITE_VRCHAT_UNAVAILABLE

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
    and locks them out of the group for good.

    Resolved by asking the group what our permissions are, rather than by
    matching English in the error body -- the earlier approach, which broke on
    rewording and broke asymmetrically, since a permission error mentioning
    "...invite that user" would have been filed as the MEMBER's refusal.
    """

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
        """Read from the group itself, not from the error's wording."""
        api.invite_error = FakeApiException(403, "Insufficient permissions")
        api.group_permissions = ["group-members-viewall"]
        state = inviter.send_group_invite(INVITE_JOB)["state"]
        assert state == inviter.INVITE_NO_PERMISSION
        assert state not in bot.GROUP_INVITE_SETTLED_STATES

    def test_the_wording_of_the_403_is_never_consulted(self, api):
        """The point of the rewrite. An error phrased any way at all is read
        the same, because the reading comes from a question with an answer --
        which survives VRChat rewording or localising its messages."""
        outcomes = set()
        for body in ["You can't invite that user", "nonsense", "", "Verboten"]:
            api.calls.clear()
            api.invite_error = FakeApiException(403, body)
            outcomes.add(inviter.send_group_invite(INVITE_JOB)["state"])
        assert outcomes == {inviter.INVITE_BLOCKED}

    def test_the_members_refusal_is_permanent(self, api):
        api.invite_error = FakeApiException(403, "You can't invite that user")
        state = inviter.send_group_invite(INVITE_JOB)["state"]
        assert state in bot.GROUP_INVITE_SETTLED_STATES


class TestThroughputIsSpaced:
    def test_consecutive_invites_are_spaced_apart(self, api, monkeypatch):
        """One account issues invites for every guild, so a verification rush
        must not be able to spend the whole shared budget in a burst."""
        slept = []
        monkeypatch.setattr(inviter.time, "sleep", slept.append)
        monkeypatch.setattr(inviter, "INVITE_MIN_SPACING_SECONDS", 3.0)
        monkeypatch.setattr(inviter, "_last_invite_call", 0.0)
        monkeypatch.setattr(inviter, "_space_invite_calls", REAL_SPACE_INVITE_CALLS)
        monkeypatch.setattr(inviter.time, "monotonic", lambda: 1000.0)
        inviter.send_group_invite(INVITE_JOB)
        inviter.send_group_invite(INVITE_JOB)
        assert slept and slept[-1] >= 3.0

    def test_the_gap_is_measured_from_when_the_call_FINISHED(self, monkeypatch, api):
        """Stamped before the call instead, a job whose retries took twelve
        seconds left the next one measuring its gap from the start of that
        chain -- and firing immediately."""
        monkeypatch.setattr(inviter.time, "sleep", lambda *_: None)
        monkeypatch.setattr(inviter, "_space_invite_calls", REAL_SPACE_INVITE_CALLS)
        ticks = iter([100.0, 175.0])  # measuring the gap, then stamping
        monkeypatch.setattr(inviter.time, "monotonic", lambda: next(ticks))
        monkeypatch.setattr(inviter, "_last_invite_call", 0.0)
        inviter.send_group_invite(INVITE_JOB)
        assert inviter._last_invite_call == 175.0

    def test_the_throttle_survives_a_failed_call(self, monkeypatch, api):
        """The timestamp is stamped in a finally. Without it, a 429 -- VRChat
        telling us to slow down -- would leave the next invite unthrottled."""
        monkeypatch.setattr(inviter.time, "sleep", lambda *_: None)
        monkeypatch.setattr(inviter.time, "monotonic", lambda: 500.0)
        monkeypatch.setattr(inviter, "_last_invite_call", 0.0)
        api.invite_error = FakeApiException(500, "boom")
        inviter.send_group_invite(INVITE_JOB)
        assert inviter._last_invite_call == 500.0

    def test_a_mistyped_spacing_cannot_stop_the_worker(self):
        """time.sleep(inf) raises OverflowError inside the consumer callback,
        and anything past the broker heartbeat drops the connection mid-job. A
        bad setting must degrade to "slow", never to "stops answering"."""
        assert inviter.INVITE_MIN_SPACING_SECONDS <= 60.0

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


class TestOneButtonPressCannotSendManyInvites:
    """Redelivery is what bounds this, and nothing else.

    process_job runs handler(job) BEFORE publish_result, so every redelivery
    re-runs the handler. For verify_group_setup that is harmless -- it re-reads
    state it already established. send_group_invite is a WRITE to somebody
    else's platform, and an uncapped requeue turns one press into an invite per
    redelivery, for ever. That is precisely the spam pattern the opt-in design
    exists not to produce.
    """

    def channel(self, acks):
        return SimpleNamespace(
            basic_ack=lambda **kw: acks.append(("ack", kw)),
            basic_nack=lambda **kw: acks.append(("nack", kw)),
        )

    def test_a_publish_failure_is_requeued_at_most_once(self, monkeypatch):
        sent = []
        monkeypatch.setitem(
            inviter.HANDLERS, inviter.JOB_SEND_INVITE, lambda job: sent.append(job)
        )

        def refuse(result):
            raise inviter.AMQPError("broker is unhappy")

        monkeypatch.setattr(inviter, "publish_result", refuse)

        acks = []
        first = SimpleNamespace(delivery_tag=1, redelivered=False)
        inviter.process_job(self.channel(acks), first, None, json.dumps(INVITE_JOB))
        assert acks[-1] == ("nack", {"delivery_tag": 1, "requeue": True})

        again = SimpleNamespace(delivery_tag=2, redelivered=True)
        inviter.process_job(self.channel(acks), again, None, json.dumps(INVITE_JOB))
        assert acks[-1] == ("nack", {"delivery_tag": 2, "requeue": False})

    def test_a_dropped_job_leaves_the_member_to_the_read_side_timeout(self):
        """Dropping is the right trade only because the bot expires the row on
        read and tells them. Nothing else would."""
        assert bot.GROUP_INVITE_TIMED_OUT in bot.GROUP_INVITE_MESSAGE_KEYS

    @pytest.mark.parametrize("body", ["5", '"text"', "[1, 2]", "null"])
    def test_a_json_body_that_is_not_an_object_is_dropped(self, body):
        """All valid JSON, none of it has .get(). The AttributeError used to
        escape process_job entirely -- outside both try blocks -- so the message
        was neither acked nor nacked, start_consuming unwound, and the broker
        redelivered it for ever with every other job stuck behind it."""
        acks = []
        method = SimpleNamespace(delivery_tag=9, redelivered=False)
        inviter.process_job(self.channel(acks), method, None, body)
        assert acks == [("nack", {"delivery_tag": 9, "requeue": False})]


class TestALateAnswerIsStillAnAnswer:
    """The read-side timeout is wall-clock and knows nothing about queue depth.

    Behind INVITE_MIN_SPACING_SECONDS a verification rush expires rows whose
    invites are queued and about to succeed. Refusing the late answer left
    those members reading "VRChat didn't answer" about an invite that had in
    fact been sent -- and their row un-settled, so the next verification
    offered them a second one.
    """

    def start(self):
        return bot.begin_group_invite(
            GUILD_ID,
            MEMBER_ID,
            group_id=GROUP_ID,
            vrc_user_id=VRC_USER_ID,
            channel_id=CHANNEL_ID,
            message_id=MESSAGE_ID,
        )

    def payload(self, job_id, state):
        return {
            "type": inviter.JOB_SEND_INVITE,
            "jobID": job_id,
            "guildID": str(GUILD_ID),
            "groupID": GROUP_ID,
            "state": state,
        }

    def expire(self):
        with bot.session_scope() as session:
            session.query(bot.GroupInviteRequest).first().state = (
                bot.GROUP_INVITE_TIMED_OUT
            )

    def test_a_timed_out_row_still_accepts_the_verdict(self):
        job = self.start()
        self.expire()
        outcome, row = bot.record_group_invite_result(
            self.payload(job["jobID"], bot.GROUP_INVITE_SENT)
        )
        assert outcome == "applied"
        assert standing()["state"] == bot.GROUP_INVITE_SENT

    def test_and_is_then_not_offered_again(self):
        job = self.start()
        self.expire()
        bot.record_group_invite_result(
            self.payload(job["jobID"], bot.GROUP_INVITE_SENT)
        )
        assert (
            bot.group_invite_refusal(standing(), GROUP_ID)
            == bot.INVITE_REFUSED_SETTLED
        )

    def test_a_row_settled_for_good_still_refuses_a_late_answer(self):
        job = self.start()
        bot.record_group_invite_result(
            self.payload(job["jobID"], bot.GROUP_INVITE_SENT)
        )
        outcome, _ = bot.record_group_invite_result(
            self.payload(job["jobID"], bot.GROUP_INVITE_BLOCKED)
        )
        assert outcome == "stale"

    def test_an_abandoned_row_is_not_reopened(self):
        """abandon_group_invite means the job provably never left the process.
        An answer to it cannot exist, so one arriving is not to be believed."""
        job = self.start()
        bot.abandon_group_invite(GUILD_ID, MEMBER_ID, job["jobID"])
        outcome, _ = bot.record_group_invite_result(
            self.payload(job["jobID"], bot.GROUP_INVITE_SENT)
        )
        assert outcome == "stale"


class TestTheOutcomeDmIsActuallyEditable:
    """Every one of these ran the real function, because the one bug this
    class exists to catch was invisible to source inspection.

    `tell_member_about_invite` rebuilds the offer button for outcomes that are
    not the member's own answer, and it was the one call site missed when the
    view grew its account argument. `locale_code` landed in the account slot,
    DynamicItem raised ValueError on a custom_id that cannot match the
    template, and the callers' catch-all swallowed it -- so on every retryable
    outcome the member's DM was never edited and they sat on "asking VRChat"
    for ever, on a row already settled and so never offered again.
    """

    @pytest.fixture
    def dm(self, monkeypatch):
        """Capture the edit that replaces the member's offer DM."""
        edits = []

        class FakeMessage:
            async def edit(self, content=None, view=None):
                edits.append((content, view))

        class FakePartialChannel:
            def get_partial_message(self, mid):
                return FakeMessage()

        monkeypatch.setattr(
            bot.bot, "get_partial_messageable", lambda cid, type=None: FakePartialChannel()
        )
        monkeypatch.setattr(bot.bot, "get_guild", lambda gid: FakeGuild())
        return edits

    def row(self, **overrides):
        fields = dict(
            discord_id=str(MEMBER_ID),
            vrc_user_id=VRC_USER_ID,
            channel_id=str(CHANNEL_ID),
            message_id=str(MESSAGE_ID),
        )
        fields.update(overrides)
        return fields

    @pytest.mark.parametrize(
        "state",
        sorted(bot.GROUP_INVITE_STATES - bot.GROUP_INVITE_SETTLED_STATES),
    )
    def test_every_retryable_outcome_can_be_told(self, dm, state):
        """Parametrised over the whole set rather than one example: the bug was
        in the branch shared by all of them."""
        run(bot.tell_member_about_invite(GUILD_ID, self.row(), state))
        assert len(dm) == 1
        content, view = dm[0]
        assert content
        assert isinstance(view, bot.GroupInviteOfferView)

    @pytest.mark.parametrize("state", sorted(bot.GROUP_INVITE_SETTLED_STATES))
    def test_every_settled_outcome_can_be_told(self, dm, state):
        run(bot.tell_member_about_invite(GUILD_ID, self.row(), state))
        assert len(dm) == 1
        content, view = dm[0]
        assert content
        assert view is None

    def test_the_returned_button_is_stamped_for_the_same_account(self, dm):
        """A retry is the same offer to the same account. Rebuilding it from
        whatever is linked now would let a VRChat hiccup launder a stale
        button into a valid one for a different account."""
        run(
            bot.tell_member_about_invite(
                GUILD_ID, self.row(), bot.GROUP_INVITE_VRCHAT_UNAVAILABLE
            )
        )
        _, view = dm[0]
        pattern = bot.GroupInviteButton.__discord_ui_compiled_template__
        match = pattern.fullmatch(view.children[0].custom_id)
        assert match["account"] == FINGERPRINT

    def test_a_row_with_no_account_still_delivers_the_answer(self, dm):
        """Should not happen -- begin_group_invite writes one every time. But
        the alternative to degrading here is throwing while reporting an
        outcome on a settled row, which is the failure mode above."""
        run(
            bot.tell_member_about_invite(
                GUILD_ID,
                self.row(vrc_user_id=None),
                bot.GROUP_INVITE_VRCHAT_UNAVAILABLE,
            )
        )
        assert len(dm) == 1
        content, view = dm[0]
        assert content
        assert view is None

    def test_the_rows_that_reach_here_carry_an_account(self):
        """Both builders that feed this function, checked at the source: a row
        missing vrc_user_id silently downgrades to a buttonless retry, which
        strands the member exactly as before, only quietly."""
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
                "state": bot.GROUP_INVITE_VRCHAT_UNAVAILABLE,
            }
        )
        assert outcome == "applied"
        assert row["vrc_user_id"] == VRC_USER_ID

    def test_the_timeout_path_carries_an_account_too(self, dm, monkeypatch):
        """The other builder. It expires a pending row and reports it, and
        "timed_out" is retryable -- so it hits the same branch."""
        monkeypatch.setattr(bot, "GROUP_INVITE_TIMEOUT_SECONDS", 0)
        job = bot.begin_group_invite(
            GUILD_ID,
            MEMBER_ID,
            group_id=GROUP_ID,
            vrc_user_id=VRC_USER_ID,
            channel_id=CHANNEL_ID,
            message_id=MESSAGE_ID,
        )
        run(
            bot.expire_group_invite_if_unanswered(GUILD_ID, MEMBER_ID, job["jobID"])
        )
        assert standing()["state"] == bot.GROUP_INVITE_TIMED_OUT
        assert len(dm) == 1
        _, view = dm[0]
        assert isinstance(view, bot.GroupInviteOfferView)


class TestAStoredVerdictAlwaysReachesSomeone:
    def test_a_failed_edit_still_delivers_the_answer(self):
        """The verdict is stored BEFORE this runs, so the row is settled: there
        is no retry and no future offer. Staying quiet on a transient 429 means
        the member never learns the outcome and is never offered again. A
        confusing pair of messages is recoverable; silence is not."""
        import inspect

        source = inspect.getsource(bot.tell_member_about_invite)
        http = source.index("except discord.HTTPException")
        forbidden = source.index("except discord.Forbidden")
        # No early return between the failed edit and the fresh-DM path.
        assert "return" not in source[http:forbidden].split("logger.warning")[0]

    def test_a_failure_to_tell_the_member_is_logged_not_swallowed(self):
        """handle_member_invite_result runs under run_coroutine_threadsafe, and
        the consumer discards the Future it returns -- so an exception escaping
        here logs NOTHING. The member would sit on "asking VRChat..." for ever,
        on a row that is settled and so never offered again, with no trace of
        why anywhere."""
        import inspect

        source = inspect.getsource(bot.handle_member_invite_result)
        tell_at = source.index("tell_member_about_invite")
        assert "try:" in source[:tell_at]
        assert "logger.exception" in source[tell_at:]

    def test_the_bad_job_state_tells_them_something_they_can_act_on(self):
        """A job the worker calls malformed is a stored VRChat id that will be
        just as malformed next time, so "try again in a few minutes" is advice
        that cannot work."""
        assert (
            bot.GROUP_INVITE_MESSAGE_KEYS[bot.GROUP_INVITE_BAD_JOB]
            == "group_invite_account_missing"
        )


class TestTheWorkerLogDoesNotPairTheTwoIdentities:
    def test_the_result_payload_is_summarised_not_dumped(self):
        """VRChat names the user in its own error prose -- "User usr_... is
        already a member" -- and the payload carries guildID. Logging the whole
        body put a VRChat id and a Discord server on one line, which is most of
        the mapping this project deliberately refuses to store."""
        import inspect

        source = inspect.getsource(inviter.publish_result)
        logged = [l for l in source.splitlines() if "logging.info" in l]
        assert logged, "publish_result no longer logs at all"
        after = source[source.index("logging.info"):]
        assert "body" not in after.split(")")[0]


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
