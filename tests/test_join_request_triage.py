"""Join-request triage: a VRChat group's join requests, posted in Discord (#291).

Every VRChat call and every Discord call is faked. What these pin down are the
decisions recorded on #291 and the measurements behind them:

* Only members of THIS server are named as the applicant's Discord identity.
* Only the roles the admin chose may approve or deny.
* Deny is confirmed first; a decision records the moderator who made it.
* The first poll posts at most JOIN_REQUEST_POST_CAP and sums up the rest,
  which are never posted. Later polls post only new requests.
* A request that is already gone answers "not pending", and that is shown as
  handled elsewhere, not as a failure (measured 2026-09-17).
"""

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import discord
import pytest

import bot
import vrc_group_inviter as inviter


GUILD_ID = 987654321
OWNER_ID = 77
MOD_ID = 5001
OUTSIDER_ID = 5002
MOD_ROLE_ID = 6001
VERIFIED_ROLE_ID = 1
CHANNEL_ID = 7001
GROUP_ID = "grp_0e1d4755-2f87-4129-a192-5587068cbf73"
OTHER_GROUP_ID = "grp_11111111-2222-3333-4444-555555555555"
ACCOUNT_ID = "usr_0e59962a-3e0d-4303-802b-9314623027e5"
QUEUE = "vrcverify_group_invites_test"
APPLICANT = "usr_656fd5b8-a5fa-4b26-9f9f-03b85e6e4375"


def run(coro):
    return asyncio.run(coro)


def request(user_id=APPLICANT, name="ClubLA Bot"):
    """One trimmed request, shaped exactly as the worker's _trim_join_request."""
    return {"user_id": user_id, "display_name": name, "icon_url": "https://example.com/i.png"}


def applicants(count):
    return [request(f"usr_00000000-0000-0000-0000-{i:012d}", f"Applicant {i}") for i in range(count)]


def http_error(cls, status):
    return cls(SimpleNamespace(status=status, reason="test"), "test")


# -------------------------------------------------------------------
# A Discord guild that records what triage did to it
# -------------------------------------------------------------------
class FakeMessage:
    _next_id = 9_000

    def __init__(self, channel, content=None, embed=None, view=None):
        FakeMessage._next_id += 1
        self.id = FakeMessage._next_id
        self.channel = channel
        self.content = content
        self.embeds = [embed] if embed else []
        self.view = view
        self.edits = []

    async def edit(self, embed=None, view=None):
        self.edits.append((embed, view))
        self.embeds = [embed] if embed else self.embeds
        self.view = view


class FakeChannel:
    def __init__(self, can_post=True):
        self.id = CHANNEL_ID
        self.messages = {}
        self.can_post = can_post
        self.forbid = False

    def permissions_for(self, me):
        return SimpleNamespace(view_channel=True, send_messages=self.can_post, embed_links=self.can_post)

    async def send(self, content=None, embed=None, view=None, allowed_mentions=None):
        if self.forbid:
            raise http_error(discord.Forbidden, 403)
        message = FakeMessage(self, content, embed, view)
        self.messages[message.id] = message
        return message

    async def fetch_message(self, message_id):
        if getattr(self, "no_history", False):
            raise http_error(discord.Forbidden, 403)
        message = self.messages.get(int(message_id))
        if message is None:
            raise http_error(discord.NotFound, 404)
        return message

    def get_partial_message(self, message_id):
        # Editing by id needs no Read Message History; fetching does.
        return self.messages.get(int(message_id))

    @property
    def posts(self):
        return [m for m in self.messages.values() if m.embeds]

    @property
    def notes(self):
        return [m for m in self.messages.values() if not m.embeds]


class FakeMember:
    def __init__(self, member_id, role_ids=(), joined_at=datetime(2026, 3, 1, tzinfo=timezone.utc)):
        self.id = member_id
        self.roles = [SimpleNamespace(id=r) for r in role_ids]
        self.joined_at = joined_at
        self.mention = f"<@{member_id}>"

    def get_role(self, role_id):
        return next((r for r in self.roles if r.id == role_id), None)


class FakeGuild:
    def __init__(self):
        self.id = GUILD_ID
        self.me = SimpleNamespace(id=1)
        self.channel = FakeChannel()
        self.members = {}
        self.fetched = []

    def get_channel(self, channel_id):
        return self.channel if int(channel_id) == CHANNEL_ID else None

    def get_member(self, member_id):
        # Nothing cached, as the bot runs with MemberCacheFlags.none().
        return None

    async def fetch_member(self, member_id):
        self.fetched.append(member_id)
        member = self.members.get(int(member_id))
        if member is None:
            raise http_error(discord.NotFound, 404)
        return member


@pytest.fixture(autouse=True)
def clean_db():
    def wipe():
        with bot.session_scope() as session:
            for model in (
                bot.Server,
                bot.User,
                bot.GroupInviteConfig,
                bot.GroupSeatLease,
                bot.JoinRequestTriage,
                bot.JoinRequestTriageRole,
                bot.JoinRequestPost,
                bot.VerificationLogChannel,
                bot.DashboardAudit,
            ):
                session.query(model).delete()

    wipe()
    bot._triage_polls.clear()
    bot._triage_locks.clear()
    bot.verification_log_buffer.clear()
    yield
    wipe()
    bot._triage_polls.clear()
    bot._triage_locks.clear()
    bot.verification_log_buffer.clear()


@pytest.fixture(autouse=True)
def setting(monkeypatch):
    """A preview guild with premium, one invite account, and no pauses."""
    monkeypatch.setitem(bot.FEATURE_PREVIEW_GUILDS, bot.FEATURE_JOIN_REQUEST_TRIAGE, frozenset({str(GUILD_ID)}))
    account = bot.InviteAccount(user_id=ACCOUNT_ID, queue=QUEUE, seats=100)
    monkeypatch.setattr(bot, "INVITE_ACCOUNTS", (account,))
    monkeypatch.setattr(bot, "INVITE_ACCOUNTS_BY_ID", {ACCOUNT_ID: account})
    monkeypatch.setattr(bot, "JOIN_REQUEST_POST_SPACING_SECONDS", 0)
    monkeypatch.setattr(bot, "JOIN_REQUEST_POLL_START_SPACING_SECONDS", 0)

    async def flags(guild_id):
        return bot.PremiumFlags(premium=True, grandfathered=False)

    monkeypatch.setattr(bot, "resolve_premium_flags", flags)


@pytest.fixture
def guild(monkeypatch):
    fake = FakeGuild()
    monkeypatch.setattr(bot.bot, "get_guild", lambda gid: fake if int(gid) == GUILD_ID else None)
    return fake


@pytest.fixture
def published(monkeypatch):
    jobs = []

    def fake_publish(job, queue=None):
        jobs.append((dict(job), queue))
        return True

    monkeypatch.setattr(bot, "publish_group_invite_job", fake_publish)
    return jobs


def make_server():
    with bot.session_scope() as session:
        session.add(
            bot.Server(
                id=10,
                server_id=str(GUILD_ID),
                owner_id=str(OWNER_ID),
                role_id=str(VERIFIED_ROLE_ID),
                instructions_locale="en-US",
            )
        )


def ready_group(group_id=GROUP_ID, state=None, verified=True):
    bot.save_group_invite_config(GUILD_ID, group_id=group_id, enabled=True)
    with bot.session_scope() as session:
        row = session.query(bot.GroupInviteConfig).filter_by(server_id=str(GUILD_ID)).first()
        row.verify_state = state or bot.GROUP_SETUP_READY
        row.can_invite = True
        row.verified_at = datetime.now(timezone.utc) if verified else None


def link_user(vrc_user_id=APPLICANT, discord_id=MOD_ID + 100, verified=True):
    with bot.session_scope() as session:
        session.add(bot.User(discord_id=str(discord_id), vrc_user_id=vrc_user_id, verification_status=verified))


@pytest.fixture
def ready(guild):
    make_server()
    ready_group()
    bot.save_triage_settings(GUILD_ID, enabled=True, channel_id=CHANNEL_ID, role_ids=[MOD_ROLE_ID])
    return guild


def poll_once(requests, *, complete=True):
    """Start a poll the way the pass does and hand it these requests."""
    job = bot.begin_triage_poll(GUILD_ID, GROUP_ID)
    started = datetime.now(timezone.utc)
    return run(bot.sync_join_requests(str(GUILD_ID), GROUP_ID, requests, job["jobID"], started, complete=complete))


def posts():
    return bot.load_join_request_posts(GUILD_ID)


# -------------------------------------------------------------------
# The two ends agree
# -------------------------------------------------------------------
class TestTheVocabularyMatchesTheWorker:
    def test_page_states(self):
        assert bot.JOIN_REQUESTS_STATES == inviter.JOIN_REQUESTS_STATES

    def test_respond_states(self):
        assert bot.RESPOND_STATES == inviter.RESPOND_STATES

    def test_job_names(self):
        assert bot.JOB_FETCH_JOIN_REQUESTS == inviter.JOB_FETCH_JOIN_REQUESTS
        assert bot.JOB_RESPOND_JOIN_REQUEST == inviter.JOB_RESPOND_JOIN_REQUEST

    def test_the_actions_are_the_ones_vrchat_accepts(self):
        assert set(bot.JOIN_REQUEST_ACTIONS.values()) == inviter.RESPOND_ACTIONS


# -------------------------------------------------------------------
# Who may use it at all
# -------------------------------------------------------------------
class TestItIsHiddenUntilAnnounced:
    def test_nobody_outside_the_preview_can_reach_it(self, monkeypatch):
        monkeypatch.setitem(bot.FEATURE_PREVIEW_GUILDS, bot.FEATURE_JOIN_REQUEST_TRIAGE, frozenset())
        assert bot.FEATURE_JOIN_REQUEST_TRIAGE in bot.UNANNOUNCED_FEATURES
        assert not bot.feature_is_reachable(bot.FEATURE_JOIN_REQUEST_TRIAGE, GUILD_ID)

    def test_a_preview_guild_can(self):
        assert bot.feature_is_reachable(bot.FEATURE_JOIN_REQUEST_TRIAGE, GUILD_ID)
        assert not bot.feature_is_reachable(bot.FEATURE_JOIN_REQUEST_TRIAGE, 1234)

    def test_announced_features_are_untouched(self):
        assert bot.feature_is_reachable(bot.FEATURE_CALENDAR_SYNC, 1234)


class TestTheGroupItRunsIn:
    def test_a_ready_invite_group_has_an_account(self, ready):
        assert run(bot.triage_account(GUILD_ID)).queue == QUEUE

    def test_an_unproven_group_does_not(self, guild):
        ready_group(verified=False)
        assert run(bot.triage_account(GUILD_ID)) is None

    def test_a_group_without_the_invite_permission_does_not(self, guild):
        """Responding needs group-invites-manage (measured 2026-09-17)."""
        ready_group(state=bot.GROUP_SETUP_NO_INVITE_PERMISSION)
        assert run(bot.triage_account(GUILD_ID)) is None

    def test_a_lapsed_server_does_not(self, ready, monkeypatch):
        monkeypatch.setattr(bot, "PREMIUM_ENFORCED", True)

        async def flags(guild_id):
            return bot.PremiumFlags(premium=False, grandfathered=True)

        monkeypatch.setattr(bot, "resolve_premium_flags", flags)
        assert run(bot.triage_account(GUILD_ID)) is None


# -------------------------------------------------------------------
# Polling
# -------------------------------------------------------------------
class TestThePass:
    def test_a_due_link_starts_a_poll_on_the_seats_queue(self, ready, published):
        outcome = run(bot.join_request_triage_pass())
        assert outcome["started"] == 1
        job, queue = published[0]
        assert queue == QUEUE
        assert job["type"] == bot.JOB_FETCH_JOIN_REQUESTS and job["groupID"] == GROUP_ID and job["offset"] == 0
        assert bot.load_triage_link(GUILD_ID)["last_state"] == bot.JOIN_REQUEST_TRIAGE_POLLING

    def test_a_poll_in_flight_is_not_started_twice(self, ready, published):
        run(bot.join_request_triage_pass())
        run(bot.join_request_triage_pass())
        assert len(published) == 1

    @pytest.mark.parametrize("settings", [{"enabled": False}, {"channel_id": None}])
    def test_nothing_is_read_while_off_or_without_a_channel(self, ready, published, settings):
        bot.save_triage_settings(GUILD_ID, **settings)
        run(bot.join_request_triage_pass())
        assert published == []

    def test_an_unreachable_worker_is_recorded_and_retried_later(self, ready, monkeypatch):
        monkeypatch.setattr(bot, "publish_group_invite_job", lambda job, queue=None: False)
        run(bot.join_request_triage_pass())
        link = bot.load_triage_link(GUILD_ID)
        assert link["last_state"] == bot.JOIN_REQUEST_TRIAGE_WORKER_UNREACHABLE
        assert link["next_poll_at"] is not None and link["poll_job_id"] is None

    def test_switching_on_again_polls_at_once_and_counts_a_backlog(self, ready):
        poll_once([])
        assert bot.load_triage_link(GUILD_ID)["seeded_at"] is not None
        bot.save_triage_settings(GUILD_ID, enabled=False)
        bot.save_triage_settings(GUILD_ID, enabled=True)
        link = bot.load_triage_link(GUILD_ID)
        assert link["seeded_at"] is None and link["next_poll_at"] is None


class TestPages:
    def start(self, published):
        run(bot.join_request_triage_pass())
        return published[-1][0]

    def page(self, job, requests, *, count=None, state="ok", offset=0):
        return {
            "type": bot.JOB_FETCH_JOIN_REQUESTS,
            "jobID": job["jobID"],
            "guildID": str(GUILD_ID),
            "groupID": GROUP_ID,
            "offset": offset,
            "state": state,
            "requests": requests,
            "count": len(requests) if count is None else count,
            "n": 100,
        }

    def test_a_full_page_asks_for_the_next_on_the_same_queue(self, ready, published):
        job = self.start(published)
        assert run(bot.handle_join_requests_page_result(self.page(job, applicants(100)))) == "next_page"
        next_job, queue = published[-1]
        assert next_job["offset"] == 100 and queue == QUEUE

    def test_a_short_page_ends_the_poll_and_posts(self, ready, published):
        job = self.start(published)
        assert run(bot.handle_join_requests_page_result(self.page(job, [request()]))) == bot.JOIN_REQUEST_TRIAGE_SYNCED
        assert len(ready.channel.posts) == 1
        assert bot.load_triage_link(GUILD_ID)["pending_count"] == 1

    def test_a_failed_page_ends_the_poll_with_the_workers_verdict(self, ready, published):
        job = self.start(published)
        state = run(bot.handle_join_requests_page_result(self.page(job, [], state=bot.GROUP_SETUP_NO_INVITE_PERMISSION)))
        assert state == bot.GROUP_SETUP_NO_INVITE_PERMISSION
        assert bot.load_triage_link(GUILD_ID)["last_state"] == bot.GROUP_SETUP_NO_INVITE_PERMISSION
        assert ready.channel.messages == {}

    def test_an_answer_for_another_poll_is_stale(self, ready, published):
        job = self.start(published)
        assert run(bot.handle_join_requests_page_result(self.page(dict(job, jobID="old"), [request()]))) == "stale"

    def test_the_page_cap_makes_the_poll_incomplete(self, ready, published, monkeypatch):
        """A poll stopped by the cap never closes a post it did not see."""
        monkeypatch.setattr(bot, "JOIN_REQUEST_MAX_PAGES", 1)
        poll_once([request()])
        poll_once([request()])
        bot.update_join_request_post(GUILD_ID, APPLICANT, missing_since=datetime.now(timezone.utc))
        with bot.session_scope() as session:
            session.query(bot.JoinRequestTriage).update({bot.JoinRequestTriage.next_poll_at: None})
        job = self.start(published)
        run(bot.handle_join_requests_page_result(self.page(job, applicants(100))))
        assert posts()[APPLICANT]["state"] == bot.JOIN_REQUEST_PENDING


# -------------------------------------------------------------------
# What gets posted
# -------------------------------------------------------------------
class TestPosting:
    def test_a_request_is_posted_with_buttons(self, ready):
        assert poll_once([request()]) == bot.JOIN_REQUEST_TRIAGE_SYNCED
        [message] = ready.channel.posts
        assert "ClubLA Bot" in message.embeds[0].title
        assert APPLICANT in message.embeds[0].description
        custom_ids = {item.custom_id for item in message.view.children}
        assert custom_ids == {
            f"vrcverify:joinreq:v1:approve:{GUILD_ID}",
            f"vrcverify:joinreq:v1:deny:{GUILD_ID}",
        }
        row = posts()[APPLICANT]
        assert row["state"] == bot.JOIN_REQUEST_PENDING and row["message_id"] == str(message.id)

    def test_the_same_request_is_posted_once(self, ready):
        poll_once([request()])
        poll_once([request()])
        assert len(ready.channel.posts) == 1

    def test_the_first_poll_posts_the_cap_and_sums_up_the_rest(self, ready, monkeypatch):
        monkeypatch.setattr(bot, "JOIN_REQUEST_POST_CAP", 3)
        poll_once(applicants(10))
        assert len(ready.channel.posts) == 3
        [note] = ready.channel.notes
        assert "Not shown: 7" in note.content
        states = [row["state"] for row in posts().values()]
        assert states.count(bot.JOIN_REQUEST_BACKLOG) == 7

    def test_the_backlog_is_never_posted_later(self, ready, monkeypatch):
        monkeypatch.setattr(bot, "JOIN_REQUEST_POST_CAP", 3)
        poll_once(applicants(10))
        poll_once(applicants(10) + [request()])
        assert len(ready.channel.posts) == 4
        assert len(ready.channel.notes) == 1

    def test_after_the_first_poll_the_cap_only_delays(self, ready, monkeypatch):
        monkeypatch.setattr(bot, "JOIN_REQUEST_POST_CAP", 3)
        poll_once([])
        poll_once(applicants(5))
        assert len(ready.channel.posts) == 3 and ready.channel.notes == []
        poll_once(applicants(5))
        assert len(ready.channel.posts) == 5

    def test_a_channel_the_bot_cannot_post_in_is_reported(self, ready):
        ready.channel.can_post = False
        assert poll_once([request()]) == bot.JOIN_REQUEST_TRIAGE_CHANNEL_UNUSABLE
        assert posts() == {}

    def test_a_refused_post_stops_the_poll_without_recording_it(self, ready):
        ready.channel.forbid = True
        assert poll_once([request()]) == bot.JOIN_REQUEST_TRIAGE_CHANNEL_UNUSABLE
        assert posts() == {}


class TestTheDiscordIdentity:
    def field(self, guild):
        return guild.channel.posts[-1].embeds[0].fields[0].value

    def test_a_linked_member_of_this_server_is_named(self, ready):
        link_user(discord_id=4242)
        ready.members[4242] = FakeMember(4242, role_ids=[VERIFIED_ROLE_ID])
        poll_once([request()])
        value = self.field(ready)
        assert "<@4242>" in value and "verified 18+" in value and f"<@&{VERIFIED_ROLE_ID}>" in value
        assert "<t:" in value

    def test_an_account_linked_to_someone_outside_this_server_is_not(self, ready):
        """Decided on #291: naming them would reveal an identity from another
        community to this server's moderators."""
        link_user(discord_id=4242)
        poll_once([request()])
        value = self.field(ready)
        assert "4242" not in value
        assert value == "No linked Discord account in this server."

    def test_an_unverified_link_says_so(self, ready):
        link_user(discord_id=4242, verified=False)
        ready.members[4242] = FakeMember(4242)
        poll_once([request()])
        assert "not verified 18+" in self.field(ready)

    def test_every_linked_member_here_is_shown(self, ready):
        """users.vrc_user_id is not unique."""
        for discord_id in (4242, 4343):
            link_user(discord_id=discord_id)
            ready.members[discord_id] = FakeMember(discord_id)
        poll_once([request()])
        value = self.field(ready)
        assert "<@4242>" in value and "<@4343>" in value


class TestRequestsThatLeaveTheQueue:
    def test_one_missed_poll_is_not_enough(self, ready):
        poll_once([request()])
        poll_once([])
        assert posts()[APPLICANT]["state"] == bot.JOIN_REQUEST_PENDING

    def test_two_missed_polls_close_the_post(self, ready):
        poll_once([request()])
        poll_once([])
        poll_once([])
        assert posts()[APPLICANT]["state"] == bot.JOIN_REQUEST_ELSEWHERE
        [message] = ready.channel.posts
        embed, view = message.edits[-1]
        assert view is None
        assert "handled in VRChat" in embed.fields[-1].value

    def test_coming_back_resets_the_count(self, ready):
        poll_once([request()])
        poll_once([])
        poll_once([request()])
        poll_once([])
        assert posts()[APPLICANT]["state"] == bot.JOIN_REQUEST_PENDING

    def test_an_incomplete_poll_closes_nothing(self, ready):
        poll_once([request()])
        poll_once([], complete=False)
        poll_once([], complete=False)
        assert posts()[APPLICANT]["missing_since"] is None

    def test_a_backlog_entry_that_leaves_is_forgotten(self, ready, monkeypatch):
        monkeypatch.setattr(bot, "JOIN_REQUEST_POST_CAP", 1)
        first, second = applicants(2)
        poll_once([first, second])
        poll_once([first])
        assert second["user_id"] not in posts()

    def test_asking_again_after_a_decision_is_a_new_post(self, ready):
        poll_once([request()])
        bot.update_join_request_post(
            GUILD_ID, APPLICANT, state=bot.JOIN_REQUEST_DENIED, decided_at=datetime.now(timezone.utc) - timedelta(hours=1)
        )
        poll_once([request()])
        assert len(ready.channel.posts) == 2
        assert posts()[APPLICANT]["state"] == bot.JOIN_REQUEST_PENDING

    def test_a_read_older_than_the_decision_is_not_a_new_request(self, ready):
        """The poll started before the moderator pressed Approve, so VRChat
        still listed the request it has since settled."""
        poll_once([request()])
        job = bot.begin_triage_poll(GUILD_ID, GROUP_ID)
        started = datetime.now(timezone.utc) - timedelta(minutes=1)
        bot.update_join_request_post(
            GUILD_ID, APPLICANT, state=bot.JOIN_REQUEST_APPROVED, decided_at=datetime.now(timezone.utc)
        )
        run(bot.sync_join_requests(str(GUILD_ID), GROUP_ID, [request()], job["jobID"], started))
        assert len(ready.channel.posts) == 1


class TestAGroupChange:
    def test_open_posts_for_the_old_group_are_closed(self, ready, published):
        poll_once([request()])
        ready_group(group_id=OTHER_GROUP_ID)
        outcome = run(bot.join_request_triage_pass())
        assert outcome["closed"] == 1
        assert posts()[APPLICANT]["state"] == bot.JOIN_REQUEST_CLOSED
        embed, view = ready.channel.posts[0].edits[-1]
        assert view is None and "different VRChat group" in embed.fields[-1].value
        # And the new group is polled as a fresh backlog.
        assert published[-1][0]["groupID"] == OTHER_GROUP_ID
        assert bot.load_triage_link(GUILD_ID)["seeded_at"] is None


# -------------------------------------------------------------------
# Pressing the buttons
# -------------------------------------------------------------------
class FakeResponse:
    def __init__(self, outer):
        self.outer = outer

    async def send_message(self, content=None, view=None, ephemeral=False):
        self.outer.sent.append((content, view, ephemeral))

    async def defer(self, ephemeral=False, thinking=False):
        self.outer.deferred = True

    async def edit_message(self, content=None, view=None):
        self.outer.edited.append((content, view))


class FakeFollowup:
    def __init__(self, outer):
        self.outer = outer

    async def send(self, content=None, ephemeral=False):
        self.outer.followups.append(content)


class FakeInteraction:
    def __init__(self, guild, message_id, user=None):
        self.guild = guild
        self.guild_id = GUILD_ID
        self.user = user or FakeMember(MOD_ID, role_ids=[MOD_ROLE_ID])
        self.message = SimpleNamespace(id=message_id)
        self.locale = "en-US"
        self.sent, self.edited, self.followups = [], [], []
        self.deferred = False
        self.response = FakeResponse(self)
        self.followup = FakeFollowup(self)

    @property
    def said(self):
        return [c for c, _, _ in self.sent] + [c for c, _ in self.edited if c] + self.followups


@pytest.fixture
def posted(ready):
    poll_once([request()])
    return ready.channel.posts[0]


def press(guild, message, choice, user=None):
    interaction = FakeInteraction(guild, message.id, user)
    run(bot.handle_join_request_press(interaction, GUILD_ID, choice))
    return interaction


class TestPressing:
    def test_approve_sends_the_decision_and_names_the_moderator(self, ready, posted, published):
        interaction = press(ready, posted, "approve")
        job, queue = published[-1]
        assert queue == QUEUE
        assert job == {
            "type": bot.JOB_RESPOND_JOIN_REQUEST,
            "jobID": job["jobID"],
            "guildID": str(GUILD_ID),
            "groupID": GROUP_ID,
            "userID": APPLICANT,
            "action": "accept",
        }
        row = posts()[APPLICANT]
        assert row["state"] == bot.JOIN_REQUEST_RESPONDING and row["decided_by"] == str(MOD_ID)
        embed, view = posted.edits[-1]
        assert view is None and f"<@{MOD_ID}>" in embed.fields[-1].value
        assert interaction.followups == ["Sent to VRChat. The post will update when VRChat answers."]

    def test_someone_without_a_chosen_role_is_refused(self, ready, posted, published):
        interaction = press(ready, posted, "approve", user=FakeMember(OUTSIDER_ID, role_ids=[123]))
        assert published == []
        assert "Only the roles" in interaction.said[0]
        assert posts()[APPLICANT]["state"] == bot.JOIN_REQUEST_PENDING

    def test_no_roles_chosen_means_nobody(self, ready, posted, published):
        bot.save_triage_settings(GUILD_ID, role_ids=[])
        press(ready, posted, "approve")
        assert published == []

    def test_deny_asks_first_and_does_nothing_until_confirmed(self, ready, posted, published):
        interaction = press(ready, posted, "deny")
        [(content, view, ephemeral)] = interaction.sent
        assert ephemeral and "ClubLA Bot" in content
        assert isinstance(view, bot.JoinRequestDenyConfirmView)
        assert published == []

        confirming = FakeInteraction(ready, posted.id)
        run(view.confirm(confirming))
        assert published[-1][0]["action"] == "reject"

    def test_cancelling_a_deny_changes_nothing(self, ready, posted, published):
        view = press(ready, posted, "deny").sent[0][1]
        cancelling = FakeInteraction(ready, posted.id)
        run(view.cancel(cancelling))
        assert published == [] and cancelling.edited[0][0] == "Nothing was changed."

    def test_a_second_press_is_told_it_is_handled(self, ready, posted, published):
        press(ready, posted, "approve")
        second = press(ready, posted, "approve")
        assert len(published) == 1
        assert second.said == ["This request has already been handled."]

    def test_a_press_on_an_unknown_post_is_handled(self, ready, posted, published):
        interaction = press(ready, SimpleNamespace(id=1), "approve")
        assert published == [] and interaction.said == ["This request has already been handled."]

    def test_an_unreachable_worker_gives_the_buttons_back(self, ready, posted, monkeypatch):
        monkeypatch.setattr(bot, "publish_group_invite_job", lambda job, queue=None: False)
        interaction = press(ready, posted, "approve")
        assert posts()[APPLICANT]["state"] == bot.JOIN_REQUEST_PENDING
        assert "couldn't reach VRChat" in interaction.followups[0]

    def test_a_group_changed_since_the_post_refuses(self, ready, posted, published):
        ready_group(group_id=OTHER_GROUP_ID)
        interaction = press(ready, posted, "approve")
        assert published == [] and "isn't available" in interaction.followups[0]


class TestTheWorkersAnswer:
    def decide(self, ready, posted, published, choice="approve"):
        if choice == "deny":
            view = press(ready, posted, "deny").sent[0][1]
            run(view.confirm(FakeInteraction(ready, posted.id)))
        else:
            press(ready, posted, choice)
        return published[-1][0]

    def answer(self, job, state):
        return run(
            bot.handle_join_request_response_result(
                {
                    "type": bot.JOB_RESPOND_JOIN_REQUEST,
                    "jobID": job["jobID"],
                    "guildID": str(GUILD_ID),
                    "groupID": GROUP_ID,
                    "action": job["action"],
                    "state": state,
                }
            )
        )

    @pytest.fixture
    def log_channel(self, ready):
        bot.set_log_channel(GUILD_ID, "8001")

    @pytest.mark.parametrize(
        "choice, state, words",
        [("approve", bot.JOIN_REQUEST_APPROVED, "Approved by"), ("deny", bot.JOIN_REQUEST_DENIED, "Denied by")],
    )
    def test_done_settles_the_post_and_logs_it(self, ready, posted, published, log_channel, choice, state, words):
        job = self.decide(ready, posted, published, choice)
        assert self.answer(job, bot.RESPOND_DONE) == state
        embed, view = posted.edits[-1]
        assert view is None and words in embed.fields[-1].value and f"<@{MOD_ID}>" in embed.fields[-1].value
        [line] = bot.verification_log_buffer.pending(str(GUILD_ID))
        assert f"<@{MOD_ID}>" in line and "ClubLA Bot" in line

    def test_no_log_channel_means_no_log_line(self, ready, posted, published):
        self.answer(self.decide(ready, posted, published), bot.RESPOND_DONE)
        assert bot.verification_log_buffer.pending(str(GUILD_ID)) == []

    def test_already_gone_is_shown_as_handled_elsewhere(self, ready, posted, published, log_channel):
        job = self.decide(ready, posted, published)
        assert self.answer(job, bot.RESPOND_NOT_PENDING) == bot.JOIN_REQUEST_ELSEWHERE
        row = posts()[APPLICANT]
        assert row["decided_by"] is None
        assert bot.verification_log_buffer.pending(str(GUILD_ID)) == []

    @pytest.mark.parametrize(
        "state, words",
        [
            (bot.GROUP_SETUP_NO_INVITE_PERMISSION, "no longer has permission"),
            (bot.GROUP_SETUP_GROUP_NOT_FOUND, "couldn't find this group"),
            (bot.GROUP_SETUP_VRCHAT_UNAVAILABLE, "didn't answer"),
            ("something_new", "didn't answer"),
        ],
    )
    def test_a_failure_gives_the_buttons_back_and_says_why(self, ready, posted, published, state, words):
        job = self.decide(ready, posted, published)
        assert self.answer(job, state) == bot.JOIN_REQUEST_PENDING
        embed, view = posted.edits[-1]
        assert isinstance(view, bot.JoinRequestView)
        assert words in embed.fields[-1].value
        # And the next decision goes through as normal.
        press(ready, posted, "approve")
        assert posts()[APPLICANT]["state"] == bot.JOIN_REQUEST_RESPONDING

    def test_an_answer_to_a_superseded_job_is_stale(self, ready, posted, published):
        job = self.decide(ready, posted, published)
        assert self.answer(dict(job, jobID="nope"), bot.RESPOND_DONE) == "stale"
        assert posts()[APPLICANT]["state"] == bot.JOIN_REQUEST_RESPONDING

    def test_a_decision_nobody_answers_is_given_back(self, ready, posted, published, monkeypatch):
        self.decide(ready, posted, published)
        bot.update_join_request_post(
            GUILD_ID, APPLICANT, decided_at=datetime.now(timezone.utc) - timedelta(seconds=bot.JOIN_REQUEST_RESPOND_TIMEOUT_SECONDS + 5)
        )
        run(bot.join_request_triage_pass())
        assert posts()[APPLICANT]["state"] == bot.JOIN_REQUEST_PENDING
        assert isinstance(posted.edits[-1][1], bot.JoinRequestView)


class TestTheRouting:
    @pytest.mark.parametrize(
        "job_type, handler",
        [
            (bot.JOB_FETCH_JOIN_REQUESTS, "handle_join_requests_page_result"),
            (bot.JOB_RESPOND_JOIN_REQUEST, "handle_join_request_response_result"),
        ],
    )
    def test_each_result_reaches_its_handler(self, monkeypatch, job_type, handler):
        seen = []

        async def record(data):
            seen.append(data["type"])
            return "ok"

        monkeypatch.setattr(bot, handler, record)
        run(bot.handle_group_invite_result({"type": job_type, "guildID": str(GUILD_ID)}))
        assert seen == [job_type]

    def test_the_buttons_survive_a_restart(self):
        """Registered as a dynamic item, so a post made before a restart still
        routes its clicks."""
        import re

        template = bot.JoinRequestButton.__discord_ui_compiled_template__
        assert re.fullmatch(template, f"vrcverify:joinreq:v1:deny:{GUILD_ID}")
        assert not re.fullmatch(template, f"vrcverify:joinreq:v1:ban:{GUILD_ID}")


# -------------------------------------------------------------------
# The dashboard's settings (#291, PR 2)
# -------------------------------------------------------------------
class SettingsGuild(FakeGuild):
    """A guild with the roles and channels write_dashboard_settings checks."""

    def __init__(self):
        super().__init__()
        self.roles = [
            SimpleNamespace(id=MOD_ROLE_ID, is_default=lambda: False),
            SimpleNamespace(id=MOD_ROLE_ID + 1, is_default=lambda: False),
        ]
        self.text_channels = []

    def add_channel(self, *, news=False, send=True, embed=True):
        perms = SimpleNamespace(view_channel=True, send_messages=send, embed_links=embed)
        self.text_channels = [
            SimpleNamespace(id=CHANNEL_ID, is_news=lambda: news, permissions_for=lambda me: perms)
        ]


@pytest.fixture
def settings_guild(monkeypatch):
    fake = SettingsGuild()
    monkeypatch.setattr(bot.bot, "get_guild", lambda gid: fake if int(gid) == GUILD_ID else None)
    make_server()
    ready_group()
    return fake


def save(changes):
    return run(bot.write_dashboard_settings(GUILD_ID, OWNER_ID, changes))


def refused(changes):
    with pytest.raises(bot.SettingRejected) as caught:
        save(changes)
    return caught.value


class TestTheSettings:
    def test_all_three_are_saved_and_audited(self, settings_guild):
        settings_guild.add_channel()
        save({
            "join_request_triage_enabled": True,
            "join_request_channel_id": str(CHANNEL_ID),
            "join_request_mod_role_ids": [str(MOD_ROLE_ID + 1), MOD_ROLE_ID, str(MOD_ROLE_ID)],
        })
        link = bot.load_triage_link(GUILD_ID)
        assert link["enabled"] and link["channel_id"] == str(CHANNEL_ID)
        assert bot.load_triage_role_ids(GUILD_ID) == {str(MOD_ROLE_ID), str(MOD_ROLE_ID + 1)}
        with bot.session_scope() as session:
            audited = {row.field: row.new_value for row in session.query(bot.DashboardAudit).filter_by(server_id=str(GUILD_ID))}
        assert audited["join_request_mod_role_ids"] == f"{MOD_ROLE_ID},{MOD_ROLE_ID + 1}"
        assert audited["join_request_triage_enabled"] == "True"

    def test_they_read_back_for_the_page(self, settings_guild):
        settings_guild.add_channel()
        save({"join_request_channel_id": str(CHANNEL_ID), "join_request_mod_role_ids": [str(MOD_ROLE_ID)]})
        settings = run(bot.read_dashboard_settings(GUILD_ID))
        assert settings["fields"]["join_request_channel_id"]["value"] == str(CHANNEL_ID)
        assert settings["fields"]["join_request_mod_role_ids"]["value"] == [str(MOD_ROLE_ID)]
        block = settings["join_request_triage"]
        assert block["available"] and block["group_ready"]
        assert block["poll_interval_minutes"] == 10

    def test_an_unready_group_is_reported_as_such(self, settings_guild):
        ready_group(state=bot.GROUP_SETUP_NO_INVITE_PERMISSION)
        assert run(bot.read_dashboard_settings(GUILD_ID))["join_request_triage"]["group_ready"] is False

    def test_saving_the_same_roles_again_audits_nothing(self, settings_guild):
        save({"join_request_mod_role_ids": [str(MOD_ROLE_ID)]})
        save({"join_request_mod_role_ids": [str(MOD_ROLE_ID)]})
        with bot.session_scope() as session:
            assert session.query(bot.DashboardAudit).filter_by(field="join_request_mod_role_ids").count() == 1

    def test_clearing_the_roles_is_a_real_choice(self, settings_guild):
        save({"join_request_mod_role_ids": [str(MOD_ROLE_ID)]})
        save({"join_request_mod_role_ids": []})
        assert bot.load_triage_role_ids(GUILD_ID) == frozenset()

    def test_an_announcement_channel_is_refused(self, settings_guild):
        """A followed channel would republish who is 18+ into other servers."""
        settings_guild.add_channel(news=True)
        assert refused({"join_request_channel_id": str(CHANNEL_ID)}).reason == "channel_is_announcement"

    @pytest.mark.parametrize("perms", [{"send": False}, {"embed": False}])
    def test_a_channel_the_bot_cannot_post_embeds_in_is_refused(self, settings_guild, perms):
        settings_guild.add_channel(**perms)
        assert refused({"join_request_channel_id": str(CHANNEL_ID)}).reason == "channel_not_writable"

    def test_a_channel_from_elsewhere_is_refused(self, settings_guild):
        assert refused({"join_request_channel_id": "123"}).reason == "channel_not_in_guild"

    def test_a_role_from_elsewhere_is_refused(self, settings_guild):
        assert refused({"join_request_mod_role_ids": ["999"]}).reason == "role_not_in_guild"
        assert bot.load_triage_role_ids(GUILD_ID) == frozenset()

    @pytest.mark.parametrize("value", ["6001", [True], ["not-a-role"], [None], [""]])
    def test_a_malformed_role_list_is_refused(self, settings_guild, value):
        assert refused({"join_request_mod_role_ids": value}).reason == "not_a_role"

    def test_more_than_ten_roles_is_refused(self, settings_guild):
        many = [str(1000 + i) for i in range(11)]
        assert refused({"join_request_mod_role_ids": many}).reason == "too_many_roles"

    def test_a_guild_outside_the_preview_cannot_write_them(self, settings_guild, monkeypatch):
        monkeypatch.setitem(bot.FEATURE_PREVIEW_GUILDS, bot.FEATURE_JOIN_REQUEST_TRIAGE, frozenset())
        assert refused({"join_request_triage_enabled": True}).reason == "not_writable_yet"

    def test_a_lapsed_server_cannot_change_them(self, settings_guild, monkeypatch):
        monkeypatch.setattr(bot, "PREMIUM_ENFORCED", True)

        async def flags(guild_id):
            return bot.PremiumFlags(premium=False, grandfathered=False)

        monkeypatch.setattr(bot, "resolve_premium_flags", flags)
        assert refused({"join_request_triage_enabled": True}).reason == "requires_premium"


# -------------------------------------------------------------------
# The adversarial pass (#291)
# -------------------------------------------------------------------
class TestTheAdversarialPass:
    @pytest.mark.parametrize("value", [["²"], ["9" * 5000], ["١٢"]])
    def test_a_role_id_python_calls_a_digit_but_int_refuses_is_refused_cleanly(self, settings_guild, value):
        """str.isdigit() accepts superscripts, other scripts' digits and numbers
        too long for int(). Sorting by int() turned a crafted save into an
        unhandled ValueError: a 500 from the bot API, not a refusal."""
        assert refused({"join_request_mod_role_ids": value}).reason == "not_a_role"

    def test_a_poll_closing_a_post_does_not_overwrite_a_decision_made_meanwhile(self, ready, published, monkeypatch):
        """The sync reads its rows once, then awaits Discord between them. A
        moderator's press can land in between, and the close used to overwrite
        it, so an approval VRChat accepted was never recorded."""
        poll_once([request()])
        poll_once([])  # missing once
        message = ready.channel.posts[0]

        pressed = []
        real_load = bot.load_join_request_posts
        monkeypatch.setattr(bot, "load_join_request_posts", lambda gid: rows_then_press(gid))

        def rows_then_press(guild_id):
            rows = real_load(guild_id)
            if not pressed:
                pressed.append(True)
                # Claimed after the sync has its snapshot, as a press would be.
                bot.claim_join_request_post(guild_id, message.id, "accept", MOD_ID, "job-meanwhile")
            return rows

        poll_once([])  # missing twice: the close
        row = posts()[APPLICANT]
        assert row["state"] == bot.JOIN_REQUEST_RESPONDING
        settled = run(bot.handle_join_request_response_result({
            "type": bot.JOB_RESPOND_JOIN_REQUEST, "jobID": "job-meanwhile", "guildID": str(GUILD_ID),
            "groupID": GROUP_ID, "action": "accept", "state": bot.RESPOND_DONE,
        }))
        assert settled == bot.JOIN_REQUEST_APPROVED
        assert posts()[APPLICANT]["decided_by"] == str(MOD_ID)

    def test_a_late_answer_after_the_buttons_came_back_is_still_recorded(self, ready, published):
        """The worker takes one job at a time, so a backlog can outlast the
        timeout. VRChat did approve; the post must say so and name who did."""
        poll_once([request()])
        message = ready.channel.posts[0]
        press(ready, message, "approve")
        job = published[-1][0]
        bot.update_join_request_post(
            GUILD_ID, APPLICANT,
            decided_at=datetime.now(timezone.utc) - timedelta(seconds=bot.JOIN_REQUEST_RESPOND_TIMEOUT_SECONDS + 5),
        )
        run(bot.expire_join_request_decisions(ready, str(GUILD_ID), datetime.now(timezone.utc)))
        assert posts()[APPLICANT]["state"] == bot.JOIN_REQUEST_PENDING

        state = run(bot.handle_join_request_response_result({
            "type": bot.JOB_RESPOND_JOIN_REQUEST, "jobID": job["jobID"], "guildID": str(GUILD_ID),
            "groupID": GROUP_ID, "action": "accept", "state": bot.RESPOND_DONE,
        }))
        assert state == bot.JOIN_REQUEST_APPROVED
        row = posts()[APPLICANT]
        assert row["decided_by"] == str(MOD_ID)
        embed, view = message.edits[-1]
        assert view is None and "Approved by" in embed.fields[-1].value

    def test_a_late_answer_does_not_override_a_newer_decision(self, ready, published):
        poll_once([request()])
        message = ready.channel.posts[0]
        press(ready, message, "approve")
        first = published[-1][0]
        bot.update_join_request_post(
            GUILD_ID, APPLICANT,
            decided_at=datetime.now(timezone.utc) - timedelta(seconds=bot.JOIN_REQUEST_RESPOND_TIMEOUT_SECONDS + 5),
        )
        run(bot.expire_join_request_decisions(ready, str(GUILD_ID), datetime.now(timezone.utc)))
        view = press(ready, message, "deny").sent[0][1]
        run(view.confirm(FakeInteraction(ready, message.id)))
        assert run(bot.handle_join_request_response_result({
            "type": bot.JOB_RESPOND_JOIN_REQUEST, "jobID": first["jobID"], "guildID": str(GUILD_ID),
            "groupID": GROUP_ID, "action": "accept", "state": bot.RESPOND_DONE,
        })) == "stale"
        assert posts()[APPLICANT]["state"] == bot.JOIN_REQUEST_RESPONDING

    def test_a_late_failure_after_the_timeout_changes_nothing(self, ready, published):
        poll_once([request()])
        message = ready.channel.posts[0]
        press(ready, message, "approve")
        job = published[-1][0]
        bot.update_join_request_post(
            GUILD_ID, APPLICANT,
            decided_at=datetime.now(timezone.utc) - timedelta(seconds=bot.JOIN_REQUEST_RESPOND_TIMEOUT_SECONDS + 5),
        )
        run(bot.expire_join_request_decisions(ready, str(GUILD_ID), datetime.now(timezone.utc)))
        assert run(bot.handle_join_request_response_result({
            "type": bot.JOB_RESPOND_JOIN_REQUEST, "jobID": job["jobID"], "guildID": str(GUILD_ID),
            "groupID": GROUP_ID, "action": "accept", "state": bot.GROUP_SETUP_VRCHAT_UNAVAILABLE,
        })) == "stale"

    def test_a_channel_without_read_message_history_still_gets_its_posts_updated(self, ready, published):
        """Fetching a message needs Read Message History, which the channel check
        does not require. Without a fallback the post kept its buttons and its
        "pending" look after the decision went through."""
        link_user(discord_id=4242)
        ready.members[4242] = FakeMember(4242)
        poll_once([request()])
        message = ready.channel.posts[0]
        ready.channel.no_history = True
        press(ready, message, "approve")
        job = published[-1][0]
        run(bot.handle_join_request_response_result({
            "type": bot.JOB_RESPOND_JOIN_REQUEST, "jobID": job["jobID"], "guildID": str(GUILD_ID),
            "groupID": GROUP_ID, "action": "accept", "state": bot.RESPOND_DONE,
        }))
        embed, view = message.edits[-1]
        assert view is None
        assert "Approved by" in embed.fields[-1].value
        # Rebuilt, so the identity the moderator decided on is still there.
        assert "<@4242>" in embed.fields[0].value
