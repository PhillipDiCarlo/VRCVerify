"""Verifying a server's existing members (#292).

Every Discord call is faked. What these pin down are the decisions recorded on
#292:

* Members are listed with fetch_members, in id order, because guild.members is
  empty on this bot. A restart carries on from the saved cursor.
* Only members already verified 18+ in our database get the role. Linked but
  not 18+ and unknown members are counted, never rechecked, so a run makes no
  VRChat calls.
* No member is DMed and nothing is logged per member. An applied run writes one
  activity-log line and one audit row.
* Counting is free; applying needs premium, a finished count, and no cooldown.
* A server whose roles the bot cannot manage gets one reason up front, and a
  refusal from Discord partway through stops the run instead of repeating.
"""

import asyncio
from types import SimpleNamespace

import discord
import pytest

import bot


GUILD_ID = 987654321
OTHER_GUILD_ID = 123
OWNER_ID = 77
ADMIN_ID = 5001
VERIFIED_ROLE_ID = 1
UNVERIFIED_ROLE_ID = 2
LOG_CHANNEL_ID = "7001"


def run(coro):
    return asyncio.run(coro)


def http_error(cls, status):
    return cls(SimpleNamespace(status=status, reason="test"), "test")


# -------------------------------------------------------------------
# A Discord guild that records what the sweep did to it
# -------------------------------------------------------------------
class FakeRole:
    def __init__(self, role_id, position=1, managed=False):
        self.id = role_id
        self.position = position
        self.managed = managed

    def __gt__(self, other):
        return self.position > other.position


class FakeMember:
    def __init__(self, member_id, roles=(), is_bot=False):
        self.id = member_id
        self.bot = is_bot
        self._roles = {role.id: role for role in roles}
        self.added = []
        self.removed = []
        self.dms = []
        self.add_error = None
        self.remove_error = None

    def get_role(self, role_id):
        return self._roles.get(role_id)

    async def add_roles(self, role, reason=None):
        if self.add_error is not None:
            raise self.add_error
        self._roles[role.id] = role
        self.added.append((role.id, reason))

    async def remove_roles(self, role, reason=None):
        if self.remove_error is not None:
            raise self.remove_error
        self._roles.pop(role.id, None)
        self.removed.append((role.id, reason))

    async def send(self, *args, **kwargs):
        self.dms.append((args, kwargs))


class FakeGuild:
    def __init__(self, members=()):
        self.id = GUILD_ID
        self.verified = FakeRole(VERIFIED_ROLE_ID, position=1)
        self.unverified = FakeRole(UNVERIFIED_ROLE_ID, position=1)
        self._roles = {VERIFIED_ROLE_ID: self.verified, UNVERIFIED_ROLE_ID: self.unverified}
        self.me = SimpleNamespace(
            top_role=FakeRole(99, position=10),
            guild_permissions=SimpleNamespace(manage_roles=True),
        )
        self.members = list(members)
        self.member_count = len(self.members)
        self.fetch_calls = []
        # Raise this after yielding this many members, to fake a page failing.
        self.fail_after = None

    def get_role(self, role_id):
        return self._roles.get(role_id)

    async def fetch_members(self, *, limit=1000, after=None):
        self.fetch_calls.append({"limit": limit, "after": getattr(after, "id", None)})
        start = getattr(after, "id", 0) or 0
        yielded = 0
        for member in sorted(self.members, key=lambda m: m.id):
            if member.id <= start:
                continue
            if self.fail_after is not None and yielded >= self.fail_after:
                raise http_error(discord.HTTPException, 500)
            yielded += 1
            yield member


# -------------------------------------------------------------------
# Fixtures
# -------------------------------------------------------------------
@pytest.fixture(autouse=True)
def clean_db():
    def wipe():
        with bot.session_scope() as session:
            for model in (
                bot.Server,
                bot.User,
                bot.MemberBackfill,
                bot.VerificationLogChannel,
                bot.DashboardAudit,
            ):
                session.query(model).delete()
        bot.verification_log_buffer.clear()

    wipe()
    yield
    wipe()


@pytest.fixture(autouse=True)
def setting(monkeypatch):
    """A premium server in the preview allowlist, and no pauses."""
    monkeypatch.setitem(
        bot.FEATURE_PREVIEW_GUILDS, bot.FEATURE_MEMBER_BACKFILL, frozenset({str(GUILD_ID)})
    )
    monkeypatch.setattr(bot, "MEMBER_BACKFILL_EDIT_SPACING", 0)
    monkeypatch.setattr(bot, "MEMBER_BACKFILL_BATCH", 2)
    set_premium(monkeypatch, True)

    started = []
    monkeypatch.setattr(bot, "_start_member_backfill_task", started.append)
    return started


def set_premium(monkeypatch, premium):
    # With the tier switched off, PremiumFlags allows everything.
    monkeypatch.setattr(bot, "PREMIUM_ENFORCED", True)

    async def flags(guild_id):
        return bot.PremiumFlags(premium=premium, grandfathered=False)

    monkeypatch.setattr(bot, "resolve_premium_flags", flags)


@pytest.fixture
def guild(monkeypatch):
    guild = FakeGuild()
    monkeypatch.setattr(
        bot.bot, "get_guild", lambda gid: guild if int(gid) == GUILD_ID else None
    )
    add_server()
    return guild


def add_server(role_id=VERIFIED_ROLE_ID, unverified_role_id=None):
    with bot.session_scope() as session:
        session.add(
            bot.Server(
                server_id=GUILD_ID,
                owner_id=OWNER_ID,
                role_id=role_id,
                unverified_role_id=str(unverified_role_id) if unverified_role_id else None,
            )
        )


def add_user(discord_id, verified, vrc_user_id=None):
    # Unique per user: users.vrc_user_id is UNIQUE in production, which the
    # Postgres run enforces and SQLite does not.
    if vrc_user_id is None:
        vrc_user_id = f"usr_{discord_id}"
    with bot.session_scope() as session:
        session.add(
            bot.User(
                discord_id=discord_id,
                vrc_user_id=vrc_user_id,
                verification_status=verified,
            )
        )


def populate(guild):
    """One of each: verified, already has the role, linked not 18+, unknown,
    a verified member whose record has no VRChat link, and a bot."""
    verified = FakeMember(10)
    holder = FakeMember(20, roles=[guild.verified])
    linked = FakeMember(30)
    stranger = FakeMember(40)
    unlinked_verified = FakeMember(50)
    a_bot = FakeMember(60, is_bot=True)
    guild.members = [verified, holder, linked, stranger, unlinked_verified, a_bot]
    guild.member_count = len(guild.members)
    add_user(10, True)
    add_user(20, True)
    add_user(30, False)
    add_user(50, True, vrc_user_id="")
    return SimpleNamespace(
        verified=verified,
        holder=holder,
        linked=linked,
        stranger=stranger,
        unlinked_verified=unlinked_verified,
        bot=a_bot,
    )


def row():
    with bot.session_scope() as session:
        found = session.get(bot.MemberBackfill, str(GUILD_ID))
        if found is None:
            return None
        return SimpleNamespace(
            **{
                column.name: getattr(found, column.name)
                for column in bot.MemberBackfill.__table__.columns
            }
        )


def count():
    return run(bot.request_member_backfill_count(GUILD_ID, ADMIN_ID))


def apply():
    return run(bot.request_member_backfill_apply(GUILD_ID, ADMIN_ID))


def sweep():
    run(bot.run_member_backfill(str(GUILD_ID)))


def counted():
    count()
    sweep()


def rejected(call):
    with pytest.raises(bot.SettingRejected) as caught:
        call()
    return caught.value


def audit_rows():
    with bot.session_scope() as session:
        return [
            (r.actor_id, r.field, r.old_value, r.new_value)
            for r in session.query(bot.DashboardAudit).all()
        ]


# -------------------------------------------------------------------
# Who may see it
# -------------------------------------------------------------------
class TestItIsInPreview:
    def test_it_is_hidden_from_servers_outside_the_allowlist(self, monkeypatch):
        assert bot.FEATURE_MEMBER_BACKFILL in bot.UNANNOUNCED_FEATURES
        assert run(bot.read_member_backfill(OTHER_GUILD_ID)) == {"available": False}
        with pytest.raises(bot.SettingRejected) as caught:
            run(bot.request_member_backfill_count(OTHER_GUILD_ID, ADMIN_ID))
        assert caught.value.reason == "not_available"

    def test_the_allowlist_is_read_from_its_own_env_var(self):
        assert "MEMBER_BACKFILL_PREVIEW_GUILDS" in open(bot.__file__).read()

    def test_it_is_not_grandfathered(self):
        assert bot.FEATURE_MEMBER_BACKFILL not in bot.GRANDFATHERED_FEATURES


# -------------------------------------------------------------------
# Counting
# -------------------------------------------------------------------
class TestCounting:
    def test_every_member_lands_in_exactly_one_bucket(self, guild):
        people = populate(guild)
        counted()
        result = row()
        assert result.state == "done"
        assert result.kind == "count"
        assert result.eligible == 2  # verified, and verified with no link
        assert result.has_role == 1
        assert result.linked_unverified == 1
        assert result.unknown == 1
        # Bots are paged through, and counted for progress only.
        assert result.scanned == 6
        assert result.granted == 0
        assert people.verified.added == []

    def test_a_count_changes_nothing_and_messages_nobody(self, guild):
        people = populate(guild)
        counted()
        for member in vars(people).values():
            assert member.added == [] and member.removed == [] and member.dms == []
        assert bot.verification_log_buffer.pending(str(GUILD_ID)) == []
        assert audit_rows() == []

    def test_it_is_free(self, guild, monkeypatch):
        set_premium(monkeypatch, False)
        populate(guild)
        counted()
        assert row().state == "done"

    def test_it_pages_with_the_rest_api_not_the_member_cache(self, guild):
        populate(guild)
        counted()
        assert guild.fetch_calls == [{"limit": None, "after": None}]

    def test_members_verified_in_another_server_are_found(self, guild):
        """users has no guild column, so a verification anywhere counts."""
        guild.members = [FakeMember(10)]
        add_user(10, True)
        counted()
        assert row().eligible == 1

    def test_it_needs_a_verified_role(self, guild):
        with bot.session_scope() as session:
            session.query(bot.Server).delete()
        add_server(role_id=None)
        assert rejected(count).reason == "no_role"

    def test_it_cannot_be_repeated_straight_away(self, guild):
        populate(guild)
        counted()
        assert rejected(count).reason == "cooldown"

    def test_it_can_be_repeated_once_the_wait_is_over(self, guild, monkeypatch):
        populate(guild)
        counted()
        monkeypatch.setattr(bot, "MEMBER_BACKFILL_RECOUNT_SECONDS", 0)
        count()
        assert row().state == "running"


# -------------------------------------------------------------------
# Applying
# -------------------------------------------------------------------
class TestApplying:
    def test_only_members_verified_in_our_database_get_the_role(self, guild):
        people = populate(guild)
        counted()
        apply()
        sweep()
        result = row()
        assert result.state == "done"
        assert result.granted == 2
        assert people.verified.added == [(VERIFIED_ROLE_ID, bot.MEMBER_BACKFILL_REASON)]
        assert people.unlinked_verified.added == [(VERIFIED_ROLE_ID, bot.MEMBER_BACKFILL_REASON)]
        for member in (people.holder, people.linked, people.stranger, people.bot):
            assert member.added == []

    def test_nobody_is_dmed(self, guild):
        people = populate(guild)
        counted()
        apply()
        sweep()
        for member in vars(people).values():
            assert member.dms == []

    def test_it_removes_the_unverified_role(self, guild):
        with bot.session_scope() as session:
            session.query(bot.Server).delete()
        add_server(unverified_role_id=UNVERIFIED_ROLE_ID)
        waiting = FakeMember(10, roles=[guild.unverified])
        # Already verified, but still carrying the unverified role.
        leftover = FakeMember(20, roles=[guild.verified, guild.unverified])
        untouched = FakeMember(30, roles=[guild.unverified])
        guild.members = [waiting, leftover, untouched]
        add_user(10, True)
        add_user(20, True)
        counted()
        apply()
        sweep()
        assert waiting.removed == [(UNVERIFIED_ROLE_ID, bot.MEMBER_BACKFILL_REASON)]
        assert leftover.removed == [(UNVERIFIED_ROLE_ID, bot.MEMBER_BACKFILL_REASON)]
        assert untouched.removed == []

    def test_it_writes_one_log_line_and_one_audit_row(self, guild):
        populate(guild)
        with bot.session_scope() as session:
            session.add(
                bot.VerificationLogChannel(server_id=str(GUILD_ID), channel_id=LOG_CHANNEL_ID)
            )
        counted()
        apply()
        sweep()
        lines = bot.verification_log_buffer.pending(str(GUILD_ID))
        assert len(lines) == 1
        assert f"<@{ADMIN_ID}>" in lines[0] and "2" in lines[0]
        assert audit_rows() == [(str(ADMIN_ID), "member_backfill", "done", "2")]

    def test_it_needs_premium(self, guild, monkeypatch):
        populate(guild)
        counted()
        set_premium(monkeypatch, False)
        refusal = rejected(apply)
        assert refusal.reason == "requires_premium"
        assert refusal.locked

    def test_it_needs_a_finished_count_first(self, guild):
        populate(guild)
        assert rejected(apply).reason == "count_first"
        count()
        # Still running, so there is no confirmed number yet.
        assert rejected(apply).reason == "already_running"

    def test_it_refuses_when_there_is_nobody_to_verify(self, guild):
        guild.members = [FakeMember(40)]
        counted()
        assert rejected(apply).reason == "nothing_to_do"

    def test_a_second_apply_waits_for_the_cooldown(self, guild, monkeypatch):
        populate(guild)
        counted()
        apply()
        sweep()
        monkeypatch.setattr(bot, "MEMBER_BACKFILL_RECOUNT_SECONDS", 0)
        guild.members.append(FakeMember(70))
        add_user(70, True)
        counted()
        assert rejected(apply).reason == "cooldown"
        monkeypatch.setattr(bot, "MEMBER_BACKFILL_COOLDOWN_HOURS", 0)
        apply()
        assert row().state == "running"

    def test_the_cooldown_survives_a_later_count(self, guild, monkeypatch):
        populate(guild)
        counted()
        apply()
        sweep()
        applied_at = row().last_applied_at
        monkeypatch.setattr(bot, "MEMBER_BACKFILL_RECOUNT_SECONDS", 0)
        counted()
        assert row().last_applied_at == applied_at

    def test_a_member_who_left_is_skipped(self, guild):
        people = populate(guild)
        people.verified.add_error = http_error(discord.NotFound, 404)
        counted()
        apply()
        sweep()
        result = row()
        assert result.state == "done"
        assert result.granted == 1
        assert result.failed == 0

    def test_a_transient_failure_is_counted_and_the_run_goes_on(self, guild):
        people = populate(guild)
        people.verified.add_error = http_error(discord.HTTPException, 500)
        counted()
        apply()
        sweep()
        result = row()
        assert result.state == "done"
        assert (result.granted, result.failed) == (1, 1)

    def test_a_refusal_from_discord_stops_the_run(self, guild):
        people = populate(guild)
        people.verified.add_error = http_error(discord.Forbidden, 403)
        counted()
        apply()
        sweep()
        result = row()
        assert result.state == "failed"
        assert result.error == "forbidden"
        assert people.unlinked_verified.added == []
        assert result.last_applied_at is None
        assert audit_rows() == [(str(ADMIN_ID), "member_backfill", "failed", "0")]


# -------------------------------------------------------------------
# Checked before anything starts
# -------------------------------------------------------------------
class TestPreflight:
    def test_without_manage_roles(self, guild):
        populate(guild)
        counted()
        guild.me.guild_permissions.manage_roles = False
        assert rejected(apply).reason == "cannot_manage"

    def test_with_the_role_above_the_bot(self, guild):
        populate(guild)
        counted()
        guild.verified.position = 50
        assert rejected(apply).reason == "role_too_high"

    def test_with_a_managed_role(self, guild):
        populate(guild)
        counted()
        guild.verified.managed = True
        assert rejected(apply).reason == "role_too_high"

    def test_with_the_unverified_role_above_the_bot(self, guild):
        with bot.session_scope() as session:
            session.query(bot.Server).delete()
        add_server(unverified_role_id=UNVERIFIED_ROLE_ID)
        populate(guild)
        counted()
        guild.unverified.position = 50
        assert rejected(apply).reason == "unverified_role_too_high"

    def test_a_deleted_unverified_role_is_not_a_blocker(self, guild):
        with bot.session_scope() as session:
            session.query(bot.Server).delete()
        add_server(unverified_role_id=4242)
        populate(guild)
        counted()
        apply()
        assert row().state == "running"

    def test_a_deleted_verified_role(self, guild):
        guild._roles.pop(VERIFIED_ROLE_ID)
        assert rejected(count).reason == "role_missing"

    def test_counting_does_not_need_manage_roles(self, guild):
        populate(guild)
        guild.me.guild_permissions.manage_roles = False
        counted()
        assert row().state == "done"

    def test_the_card_reports_the_blocker_before_anyone_clicks(self, guild):
        guild.me.guild_permissions.manage_roles = False
        card = run(bot.read_member_backfill(GUILD_ID))
        assert card["count_blocker"] is None
        assert card["apply_blocker"] == "cannot_manage"


# -------------------------------------------------------------------
# Interrupted runs
# -------------------------------------------------------------------
class TestResuming:
    def test_a_restart_carries_on_from_the_cursor(self, guild, setting):
        people = populate(guild)
        counted()
        apply()
        # Pretend the process died after the first batch of two.
        run(
            bot._member_backfill_batch(
                str(GUILD_ID),
                [people.verified, people.holder],
                guild.verified,
                None,
                True,
            )
        )
        assert row().cursor == str(people.holder.id)

        setting.clear()
        run(bot.resume_member_backfills())
        assert setting == [str(GUILD_ID)]
        sweep()
        result = row()
        assert guild.fetch_calls[-1]["after"] == people.holder.id
        assert result.state == "done"
        assert result.granted == 2
        # Nobody was given the role twice.
        assert len(people.verified.added) == 1

    def test_a_finished_run_is_not_resumed(self, guild, setting):
        populate(guild)
        counted()
        setting.clear()
        run(bot.resume_member_backfills())
        assert setting == []

    def test_a_lapsed_plan_stops_a_resumed_apply(self, guild, monkeypatch):
        populate(guild)
        counted()
        apply()
        set_premium(monkeypatch, False)
        sweep()
        assert (row().state, row().error) == ("failed", "not_premium")

    def test_a_failed_page_fails_the_run(self, guild):
        populate(guild)
        guild.fail_after = 3
        counted()
        result = row()
        assert (result.state, result.error) == ("failed", "fetch_failed")
        # The pages before the failure were saved.
        assert result.scanned == 2

    def test_a_server_the_bot_left(self, guild, monkeypatch):
        populate(guild)
        count()
        monkeypatch.setattr(bot.bot, "get_guild", lambda gid: None)
        sweep()
        assert (row().state, row().error) == ("failed", "guild_unavailable")


# -------------------------------------------------------------------
# What the Overview is sent
# -------------------------------------------------------------------
class TestTheCard:
    def test_before_any_run(self, guild):
        card = run(bot.read_member_backfill(GUILD_ID))
        assert card["available"] is True
        assert card["can_apply"] is True
        assert card["run"] is None
        assert card["count_blocker"] is None
        assert card["count_available_at"] is None

    def test_after_a_count(self, guild):
        populate(guild)
        counted()
        card = run(bot.read_member_backfill(GUILD_ID))
        assert card["run"]["eligible"] == 2
        assert card["run"]["state"] == "done"
        assert card["member_count"] == 6
        assert card["count_available_at"] is not None
        assert card["apply_available_at"] is None

    def test_a_free_server_sees_it_cannot_apply(self, guild, monkeypatch):
        set_premium(monkeypatch, False)
        card = run(bot.read_member_backfill(GUILD_ID))
        assert card["can_apply"] is False

    def test_the_overview_carries_it(self, guild):
        overview = run(bot.read_dashboard_overview(GUILD_ID))
        assert overview["backfill"]["available"] is True

    def test_it_sends_no_member_ids(self, guild):
        populate(guild)
        counted()
        card = run(bot.read_member_backfill(GUILD_ID))
        text = repr(card)
        for member_id in ("10", "20", "30", "40", "50"):
            assert f"'{member_id}'" not in text
        assert "cursor" not in card["run"]


# -------------------------------------------------------------------
# Adversarial
# -------------------------------------------------------------------
class TestUnderPressure:
    def test_two_clicks_at_once_start_one_run(self, guild, monkeypatch, setting):
        """Two admins, or one double-click through two dashboard threads. Both
        requests await the premium check before claiming, so the claim itself
        is what has to hold."""
        populate(guild)
        counted()
        setting.clear()

        async def both():
            return await asyncio.gather(
                bot.request_member_backfill_apply(GUILD_ID, ADMIN_ID),
                bot.request_member_backfill_apply(GUILD_ID, ADMIN_ID + 1),
                return_exceptions=True,
            )

        results = run(both())
        refused = [r for r in results if isinstance(r, bot.SettingRejected)]
        assert len(refused) == 1 and refused[0].reason == "already_running"
        assert [str(started) for started in setting] == [str(GUILD_ID)]

    def test_the_real_task_path_finishes_the_run(self, guild, monkeypatch):
        """Everything else here stubs _start_member_backfill_task. This does
        not: the request starts a real background task, which runs to done."""
        monkeypatch.undo()
        monkeypatch.setitem(
            bot.FEATURE_PREVIEW_GUILDS, bot.FEATURE_MEMBER_BACKFILL, frozenset({str(GUILD_ID)})
        )
        monkeypatch.setattr(bot, "MEMBER_BACKFILL_EDIT_SPACING", 0)
        monkeypatch.setattr(
            bot.bot, "get_guild", lambda gid: guild if int(gid) == GUILD_ID else None
        )
        set_premium(monkeypatch, True)
        populate(guild)

        async def scenario():
            await bot.request_member_backfill_count(GUILD_ID, ADMIN_ID)
            await bot.background_tasks[f"member_backfill:{GUILD_ID}"]

        run(scenario())
        assert row().state == "done"
        assert row().eligible == 2
