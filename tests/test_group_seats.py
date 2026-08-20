"""Seats: which invite account serves which guild (issue #49, phase 6).

A VRChat account can be in 100 groups, 200 with VRC+. That cap is the scarcest
thing this feature has, and it is spent by servers rather than by members --
so it needs accounting a lapsed server cannot quietly hold forever.

Three properties carry the weight here, and each has a class below:

  * A job must reach the account that is actually in the group. Two workers on
    one queue split its messages round-robin, so routing is explicit and the
    queue is per account. Getting this wrong is not slow, it is wrong.

  * Reclaiming a seat must not cost a returning customer their setup.
    `verified_at` survives, which is what makes `requireCode` stay False -- so
    they re-invite the bot and press check, and never paste the claim code back
    into their group description.

  * Nothing is reclaimed on an unanswered question. A billing hiccup that read
    as "not paying" would evict paying customers from their own groups.
"""

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

import bot
import vrc_group_inviter as inviter


GUILD_ID = 987654321
OTHER_GUILD_ID = 123456789
OWNER_ID = 77
SKU_ID = 555000111
GROUP_ID = "grp_0e1d4755-2f87-4129-a192-5587068cbf73"
OTHER_GROUP_ID = "grp_11111111-2222-3333-4444-555555555555"
ACCOUNT_A = "usr_0e59962a-3e0d-4303-802b-9314623027e5"
ACCOUNT_B = "usr_11111111-2222-3333-4444-555555555555"


def run(coro):
    return asyncio.run(coro)


def account(user_id=ACCOUNT_A, queue="vrcverify_group_invites", seats=100):
    return bot.InviteAccount(user_id=user_id, queue=queue, seats=seats)


def make_server(guild_id=GUILD_ID, row_id=10):
    with bot.session_scope() as session:
        session.add(
            bot.Server(
                id=row_id,
                server_id=str(guild_id),
                owner_id=str(OWNER_ID),
                role_id="1",
                instructions_locale="en-US",
            )
        )


def configure_group(guild_id=GUILD_ID, group_id=GROUP_ID, verified=True):
    bot.save_group_invite_config(guild_id, group_id=group_id, enabled=True)
    if verified and group_id:
        with bot.session_scope() as session:
            row = (
                session.query(bot.GroupInviteConfig)
                .filter_by(server_id=str(guild_id))
                .first()
            )
            row.verify_state = bot.GROUP_SETUP_READY
            row.can_invite = True
            row.verified_at = datetime.now(timezone.utc)
            row.invite_account_id = ACCOUNT_A


def lease(guild_id=GUILD_ID):
    return bot.load_seat_lease(guild_id)


def set_lease(guild_id, account_id, *, reserved_days_ago=0, premium_days_ago=None):
    now = datetime.now(timezone.utc)
    with bot.session_scope() as session:
        row = (
            session.query(bot.GroupSeatLease)
            .filter_by(server_id=str(guild_id))
            .first()
        )
        if row is None:
            row = bot.GroupSeatLease(server_id=str(guild_id))
            session.add(row)
        row.invite_account_id = account_id
        row.reserved_at = now - timedelta(days=reserved_days_ago)
        row.released_at = None
        if premium_days_ago is not None:
            row.last_premium_at = now - timedelta(days=premium_days_ago)


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


@pytest.fixture(autouse=True)
def one_account(monkeypatch):
    """The single-account installation everyone is running today."""
    only = account()
    monkeypatch.setattr(bot, "INVITE_ACCOUNTS", (only,))
    monkeypatch.setattr(bot, "INVITE_ACCOUNTS_BY_ID", {only.user_id: only})
    monkeypatch.setattr(bot, "INVITE_VRCHAT_USER_ID", only.user_id)
    return only


@pytest.fixture
def two_accounts(monkeypatch):
    a = account(ACCOUNT_A, "vrcverify_group_invites", seats=2)
    b = account(ACCOUNT_B, "vrcverify_group_invites_2", seats=2)
    monkeypatch.setattr(bot, "INVITE_ACCOUNTS", (a, b))
    monkeypatch.setattr(bot, "INVITE_ACCOUNTS_BY_ID", {a.user_id: a, b.user_id: b})
    return a, b


@pytest.fixture
def published(monkeypatch):
    """Collect (job, queue) pairs that would have gone on a queue."""
    sent = []

    def fake_publish(job, queue=None):
        sent.append((job, queue))
        return True

    monkeypatch.setattr(bot, "publish_group_invite_job", fake_publish)
    return sent


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
# The roster
# -------------------------------------------------------------------
class TestParsingTheRoster:
    def parse(self, raw, **kw):
        kw.setdefault("default_queue", "q")
        kw.setdefault("default_seats", 100)
        return bot.parse_invite_accounts(raw, **kw)

    def test_unset_falls_back_to_the_single_account_settings(self):
        """The upgrade has to be a no-op for every existing deployment: they
        have one account and no INVITE_ACCOUNTS, and must keep working."""
        roster = self.parse(None, default_user_id=ACCOUNT_A)
        assert roster == (bot.InviteAccount(ACCOUNT_A, "q", 100),)

    def test_unset_and_unprovisioned_is_an_empty_roster(self):
        assert self.parse(None) == ()

    def test_two_accounts(self):
        roster = self.parse(f"{ACCOUNT_A}|q1|200,{ACCOUNT_B}|q2")
        assert roster == (
            bot.InviteAccount(ACCOUNT_A, "q1", 200),
            bot.InviteAccount(ACCOUNT_B, "q2", 100),
        )

    def test_whitespace_around_entries_is_tolerated(self):
        roster = self.parse(f"  {ACCOUNT_A} | q1 | 5 ,  {ACCOUNT_B}|q2  ")
        assert [a.user_id for a in roster] == [ACCOUNT_A, ACCOUNT_B]
        assert roster[0].seats == 5

    @pytest.mark.parametrize(
        "raw",
        [
            "not-a-user|q1",
            "usr_only",
            f"{ACCOUNT_A}|q1|q2|q3",
            f"{ACCOUNT_A}|",
        ],
    )
    def test_a_malformed_entry_is_skipped_not_raised(self, raw):
        """Refusing to start over one typo takes invites down for every guild,
        including the ones whose accounts parsed fine."""
        assert self.parse(raw) == ()

    def test_a_bad_entry_does_not_take_the_good_ones_with_it(self):
        roster = self.parse(f"nonsense,{ACCOUNT_B}|q2")
        assert [a.user_id for a in roster] == [ACCOUNT_B]

    def test_a_duplicate_account_keeps_the_first(self):
        roster = self.parse(f"{ACCOUNT_A}|q1|5,{ACCOUNT_A}|q2|9")
        assert roster == (bot.InviteAccount(ACCOUNT_A, "q1", 5),)

    def test_two_accounts_may_not_share_a_queue(self):
        """The round-robin bug this whole design exists to avoid, arriving by
        configuration rather than by code."""
        roster = self.parse(f"{ACCOUNT_A}|shared,{ACCOUNT_B}|shared")
        assert [a.user_id for a in roster] == [ACCOUNT_A]

    @pytest.mark.parametrize("seats", ["abc", "0", "-4"])
    def test_an_unusable_seat_count_falls_back_to_the_default(self, seats):
        roster = self.parse(f"{ACCOUNT_A}|q1|{seats}", default_seats=42)
        assert roster[0].seats == 42


# -------------------------------------------------------------------
# Counting and assigning
# -------------------------------------------------------------------
class TestSeatAccounting:
    def test_an_empty_installation_has_used_nothing(self):
        assert bot.seat_capacity() == {"used": 0, "total": 100, "free": 100}

    def test_a_lease_spends_a_seat(self):
        set_lease(GUILD_ID, ACCOUNT_A)
        assert bot.seat_usage() == {ACCOUNT_A: 1}
        assert bot.seat_capacity()["free"] == 99

    def test_a_released_lease_gives_it_back(self):
        set_lease(GUILD_ID, ACCOUNT_A)
        bot.release_seat(GUILD_ID)
        assert bot.seat_usage() == {}
        assert bot.seat_capacity()["free"] == 100

    def test_a_retired_accounts_leases_are_not_counted_as_capacity(
        self, monkeypatch
    ):
        """Understating free seats is the safe direction; overstating them
        sends admins to an account that cannot take them."""
        set_lease(GUILD_ID, "usr_retired-account")
        capacity = bot.seat_capacity()
        assert capacity["used"] == 0
        assert capacity["total"] == 100


class TestAssignment:
    def test_a_seat_is_reserved_when_it_is_assigned(self):
        """Not when the bot finally joins. Counting only joined groups would
        let fifty simultaneous setups all be sent to the same nearly-full
        account and push it past its cap."""
        chosen = bot.assign_invite_account(GUILD_ID)
        assert chosen.user_id == ACCOUNT_A
        assert lease()["invite_account_id"] == ACCOUNT_A
        assert lease()["reserved_at"] is not None

    def test_assignment_is_sticky(self, two_accounts):
        """The account an admin was told to invite is the account in their
        group. Reassigning would tell them to go and invite somebody else."""
        first = bot.assign_invite_account(GUILD_ID)
        set_lease(OTHER_GUILD_ID, first.user_id)
        set_lease(555, first.user_id)
        assert bot.assign_invite_account(GUILD_ID).user_id == first.user_id

    def test_re_assigning_does_not_reset_the_reservation_clock(self):
        """Otherwise a guild that merely re-saves its settings would keep
        postponing the expiry of a reservation it never used."""
        bot.assign_invite_account(GUILD_ID)
        set_lease(GUILD_ID, ACCOUNT_A, reserved_days_ago=20)
        before = lease()["reserved_at"]
        bot.assign_invite_account(GUILD_ID)
        assert lease()["reserved_at"] == before

    def test_the_least_loaded_account_wins(self, two_accounts):
        a, b = two_accounts
        set_lease(OTHER_GUILD_ID, a.user_id)
        assert bot.assign_invite_account(GUILD_ID).user_id == b.user_id

    def test_a_full_account_is_skipped(self, two_accounts):
        a, b = two_accounts
        set_lease(1, a.user_id)
        set_lease(2, a.user_id)
        set_lease(3, b.user_id)
        assert bot.assign_invite_account(GUILD_ID).user_id == b.user_id

    def test_a_full_installation_assigns_nobody(self, two_accounts):
        for index in range(4):
            set_lease(index, ACCOUNT_A if index < 2 else ACCOUNT_B)
        assert bot.assign_invite_account(GUILD_ID) is None
        assert lease() is None

    def test_no_roster_assigns_nobody(self, monkeypatch):
        monkeypatch.setattr(bot, "INVITE_ACCOUNTS", ())
        monkeypatch.setattr(bot, "INVITE_ACCOUNTS_BY_ID", {})
        assert bot.assign_invite_account(GUILD_ID) is None


class TestResolvingAGuildsAccount:
    def test_a_lease_names_the_account(self):
        set_lease(GUILD_ID, ACCOUNT_A)
        assert bot.invite_account_for_guild(GUILD_ID).user_id == ACCOUNT_A

    def test_no_lease_on_a_single_account_install_is_not_in_doubt(self):
        """Every guild configured before the lease table existed is in exactly
        this position, and telling them the feature is unavailable until a
        sweep materialises a row would be a self-inflicted outage."""
        assert bot.invite_account_for_guild(GUILD_ID).user_id == ACCOUNT_A

    def test_no_lease_with_several_accounts_says_so(self, two_accounts):
        assert bot.invite_account_for_guild(GUILD_ID) is None

    def test_a_released_lease_names_nobody(self):
        set_lease(GUILD_ID, ACCOUNT_A)
        bot.release_seat(GUILD_ID)
        assert bot.invite_account_for_guild(GUILD_ID) is None

    def test_an_account_no_longer_on_the_roster_names_nobody(self):
        set_lease(GUILD_ID, "usr_retired-account")
        assert bot.invite_account_for_guild(GUILD_ID) is None

    def test_resolving_never_reserves(self, two_accounts):
        """A member pressing a button must not spend capacity."""
        bot.invite_account_for_guild(GUILD_ID)
        assert lease() is None


# -------------------------------------------------------------------
# Routing
# -------------------------------------------------------------------
class TestJobsReachTheRightAccount:
    def test_a_verification_goes_to_the_assigned_accounts_queue(
        self, subscribed, published, two_accounts
    ):
        a, b = two_accounts
        set_lease(OTHER_GUILD_ID, a.user_id)  # so b is least loaded
        make_server()
        bot.save_group_invite_config(GUILD_ID, group_id=GROUP_ID, enabled=True)
        run(bot.request_group_verification(GUILD_ID, OWNER_ID))
        assert published, "nothing was published"
        job, queue = published[-1]
        assert queue == b.queue
        assert lease()["invite_account_id"] == b.user_id

    def test_a_leave_goes_to_the_account_that_is_in_the_group(
        self, published, two_accounts
    ):
        a, b = two_accounts
        assert run(bot.request_seat_release(GUILD_ID, GROUP_ID, b))
        job, queue = published[-1]
        assert queue == b.queue
        assert job["type"] == bot.JOB_LEAVE_GROUP
        assert job["groupID"] == GROUP_ID

    def test_a_leave_job_carries_nothing_a_caller_could_supply(self):
        """A leave whose group came from a request body would let anyone evict
        the bot from any group it is in."""
        job = bot.build_seat_release_job(GUILD_ID, GROUP_ID)
        assert set(job) == {"type", "jobID", "guildID", "groupID"}

    def test_a_full_installation_refuses_the_verification(
        self, subscribed, published, two_accounts
    ):
        for index in range(4):
            set_lease(index, ACCOUNT_A if index < 2 else ACCOUNT_B)
        make_server()
        bot.save_group_invite_config(GUILD_ID, group_id=GROUP_ID, enabled=True)
        assert run(bot.request_group_verification(GUILD_ID, OWNER_ID)) is None
        assert published == []


# -------------------------------------------------------------------
# Giving a seat back
# -------------------------------------------------------------------
class TestReleasingASeat:
    def test_a_returning_admin_does_not_need_a_new_claim_code(self):
        """The load-bearing property of the whole reclaim design.

        begin_group_verification computes `requireCode` as
        `verified_at is None`, so keeping verified_at is exactly what turns a
        return into "invite the bot again and press check". Clearing it would
        silently demand the code be pasted back into the group description.
        """
        make_server()
        configure_group()
        bot.mark_group_seat_released(GUILD_ID)
        job = bot.begin_group_verification(GUILD_ID)
        assert job["requireCode"] is False

    def test_the_group_and_its_claim_survive(self):
        make_server()
        configure_group()
        code = bot.load_group_invite_config(GUILD_ID)["claim_code"]
        bot.mark_group_seat_released(GUILD_ID)
        stored = bot.load_group_invite_config(GUILD_ID)
        assert stored["group_id"] == GROUP_ID
        assert stored["claim_code"] == code
        assert stored["verified_at"] is not None

    def test_nobody_can_take_the_group_while_its_server_is_away(self):
        make_server()
        configure_group()
        bot.mark_group_seat_released(GUILD_ID)
        make_server(OTHER_GUILD_ID, row_id=11)
        with pytest.raises(bot.SettingRejected):
            bot.save_group_invite_config(
                OTHER_GUILD_ID, group_id=GROUP_ID, enabled=True
            )

    def test_the_admins_own_switch_is_not_touched(self):
        """They never turned it off. We left."""
        make_server()
        configure_group()
        bot.mark_group_seat_released(GUILD_ID)
        assert bot.load_group_invite_config(GUILD_ID)["enabled"] is True

    def test_what_the_bot_learned_is_cleared(self):
        make_server()
        configure_group()
        bot.mark_group_seat_released(GUILD_ID)
        stored = bot.load_group_invite_config(GUILD_ID)
        assert stored["verify_state"] == bot.GROUP_SETUP_SEAT_RELEASED
        assert stored["can_invite"] is False
        assert stored["invite_account_id"] is None

    def test_the_released_state_has_dashboard_copy(self):
        from dashboard import settings_view

        assert bot.GROUP_SETUP_SEAT_RELEASED in settings_view.GROUP_SETUP_COPY

    def test_the_account_is_still_shown_so_they_can_re_invite(self):
        """seat_released is not an "already in the group" state, so the page
        must keep naming the account to invite."""
        from dashboard import settings_view

        assert (
            bot.GROUP_SETUP_SEAT_RELEASED
            not in settings_view.GROUP_STATES_ALREADY_IN
        )


class TestTheSeatIsFreedOnlyWhenTheBotIsActuallyOut:
    def result(self, state):
        return {
            "type": inviter.JOB_LEAVE_GROUP,
            "jobID": "job-1",
            "guildID": str(GUILD_ID),
            "groupID": GROUP_ID,
            "state": state,
        }

    def test_a_successful_leave_frees_the_seat(self):
        make_server()
        configure_group()
        set_lease(GUILD_ID, ACCOUNT_A)
        run(bot.handle_seat_release_result(self.result(bot.SEAT_LEAVE_DONE)))
        assert lease()["released_at"] is not None
        assert bot.seat_usage() == {}

    def test_a_failed_leave_keeps_the_seat_spoken_for(self):
        """Freeing it would advertise capacity the bot is still occupying."""
        set_lease(GUILD_ID, ACCOUNT_A)
        run(bot.handle_seat_release_result(self.result(bot.SEAT_LEAVE_FAILED)))
        assert lease()["released_at"] is None
        assert bot.seat_usage() == {ACCOUNT_A: 1}

    def test_an_unknown_state_changes_nothing(self):
        set_lease(GUILD_ID, ACCOUNT_A)
        run(bot.handle_seat_release_result(self.result("something_new")))
        assert lease()["released_at"] is None

    def test_the_result_router_sends_a_leave_verdict_here(self, monkeypatch):
        seen = []

        async def capture(data):
            seen.append(data)

        monkeypatch.setattr(bot, "handle_seat_release_result", capture)
        run(bot.handle_group_invite_result(self.result(bot.SEAT_LEAVE_DONE)))
        assert len(seen) == 1


# -------------------------------------------------------------------
# The sweep
# -------------------------------------------------------------------
class TestTheSweep:
    def test_a_paying_guild_has_its_clock_kept_current(self, subscribed):
        make_server()
        configure_group()
        set_lease(GUILD_ID, ACCOUNT_A, premium_days_ago=200)
        run(bot.seat_sweep_pass())
        seen = lease()["last_premium_at"]
        assert (datetime.now(timezone.utc) - seen).total_seconds() < 60

    def test_a_paying_guild_is_never_reclaimed(self, subscribed, published):
        make_server()
        configure_group()
        set_lease(GUILD_ID, ACCOUNT_A, premium_days_ago=500)
        run(bot.seat_sweep_pass())
        assert published == []

    def test_a_first_sighting_of_a_lapse_starts_the_clock(self, free, published):
        """NULL means "not yet observed", never "lapsed long ago". Reading it
        the other way would evict every lapsed guild the instant this shipped.
        """
        make_server()
        configure_group()
        set_lease(GUILD_ID, ACCOUNT_A)
        run(bot.seat_sweep_pass())
        assert published == []
        assert lease()["last_premium_at"] is not None

    def test_a_lapse_inside_the_grace_period_is_left_alone(self, free, published):
        make_server()
        configure_group()
        set_lease(GUILD_ID, ACCOUNT_A, premium_days_ago=30)
        run(bot.seat_sweep_pass())
        assert published == []

    def test_a_long_lapse_asks_the_worker_to_leave(self, free, published):
        make_server()
        configure_group()
        set_lease(GUILD_ID, ACCOUNT_A, premium_days_ago=120)
        run(bot.seat_sweep_pass())
        assert len(published) == 1
        job, queue = published[0]
        assert job["type"] == bot.JOB_LEAVE_GROUP
        assert job["groupID"] == GROUP_ID
        assert queue == "vrcverify_group_invites"

    def test_the_seat_is_not_freed_before_the_worker_answers(self, free, published):
        make_server()
        configure_group()
        set_lease(GUILD_ID, ACCOUNT_A, premium_days_ago=120)
        run(bot.seat_sweep_pass())
        assert lease()["released_at"] is None

    def test_an_unanswerable_plan_never_reclaims(self, monkeypatch, published):
        """A billing hiccup that read as "not paying" would evict paying
        customers from their own groups. The cost of waiting is one sweep."""

        async def boom(guild_id):
            raise RuntimeError("entitlements are down")

        monkeypatch.setattr(bot, "resolve_premium_flags", boom)
        make_server()
        configure_group()
        set_lease(GUILD_ID, ACCOUNT_A, premium_days_ago=500)
        run(bot.seat_sweep_pass())
        assert published == []
        assert lease()["released_at"] is None

    def test_a_lapsed_guild_with_no_account_is_freed_outright(
        self, free, published, monkeypatch
    ):
        """Nothing is holding a group, so there is nothing to ask anyone to
        leave -- but the lease must not pin capacity forever either."""
        make_server()
        configure_group()
        set_lease(GUILD_ID, "usr_retired-account", premium_days_ago=200)
        run(bot.seat_sweep_pass())
        assert published == []
        assert lease()["released_at"] is not None

    def test_the_pass_is_capped(self, free, published, monkeypatch):
        """A backlog or a clock jump must trickle out rather than becoming a
        burst of VRChat writes from one account."""
        monkeypatch.setattr(bot, "SEAT_SWEEP_MAX_PER_PASS", 2)
        monkeypatch.setattr(bot, "SEAT_SWEEP_SPACING", 0)
        for index in range(5):
            make_server(guild_id=index, row_id=100 + index)
            bot.save_group_invite_config(
                index,
                group_id="grp_00000000-0000-0000-0000-%012d" % index,
                enabled=True,
            )
            with bot.session_scope() as session:
                row = (
                    session.query(bot.GroupInviteConfig)
                    .filter_by(server_id=str(index))
                    .first()
                )
                row.verified_at = datetime.now(timezone.utc)
            set_lease(index, ACCOUNT_A, premium_days_ago=200)
        run(bot.seat_sweep_pass())
        assert len(published) == 2

    def test_a_guild_with_no_group_is_not_swept(self, free, published):
        make_server()
        set_lease(GUILD_ID, ACCOUNT_A, premium_days_ago=500)
        run(bot.seat_sweep_pass())
        assert published == []


class TestAbandonedReservations:
    def test_a_reservation_nobody_finished_is_released(self):
        """Otherwise the feature leaks capacity to everyone who tried it once
        and gave up."""
        make_server()
        bot.save_group_invite_config(GUILD_ID, group_id=GROUP_ID, enabled=True)
        set_lease(GUILD_ID, ACCOUNT_A, reserved_days_ago=60)
        assert bot.expire_stale_reservations() == 1
        assert lease()["released_at"] is not None

    def test_a_finished_setup_is_not_touched(self):
        """A guild whose setup succeeded holds a real seat; releasing it is
        the lapse path's business, not this one's."""
        make_server()
        configure_group()
        set_lease(GUILD_ID, ACCOUNT_A, reserved_days_ago=60)
        assert bot.expire_stale_reservations() == 0
        assert lease()["released_at"] is None

    def test_a_recent_reservation_is_not_touched(self):
        make_server()
        bot.save_group_invite_config(GUILD_ID, group_id=GROUP_ID, enabled=True)
        set_lease(GUILD_ID, ACCOUNT_A, reserved_days_ago=1)
        assert bot.expire_stale_reservations() == 0


class TestCapacityReporting:
    def test_a_full_installation_says_so_loudly(self, caplog, two_accounts):
        for index in range(4):
            set_lease(index, ACCOUNT_A if index < 2 else ACCOUNT_B)
        with caplog.at_level("ERROR"):
            bot.report_seat_capacity()
        assert any("Every invite account is full" in r.message for r in caplog.records)

    def test_running_low_warns_while_there_is_still_time(
        self, caplog, monkeypatch, two_accounts
    ):
        monkeypatch.setattr(bot, "SEAT_CAPACITY_WARN_FREE", 2)
        set_lease(1, ACCOUNT_A)
        set_lease(2, ACCOUNT_A)
        with caplog.at_level("WARNING"):
            bot.report_seat_capacity()
        assert any("nearly gone" in r.message for r in caplog.records)


# -------------------------------------------------------------------
# Changing the group
# -------------------------------------------------------------------
class TestChangingTheGroupLeavesTheOldOne:
    def test_the_displaced_group_is_reported(self):
        """The lapse sweep only ever looks at the group a guild CURRENTLY has,
        so without this the bot sits in the old one holding a seat nothing will
        ever reclaim."""
        make_server()
        configure_group()
        displaced = bot.save_group_invite_config(
            GUILD_ID, group_id=OTHER_GROUP_ID, enabled=True
        )
        assert displaced == GROUP_ID

    def test_clearing_the_group_displaces_it_too(self):
        make_server()
        configure_group()
        assert (
            bot.save_group_invite_config(GUILD_ID, group_id=None, enabled=True)
            == GROUP_ID
        )

    def test_a_group_the_bot_never_joined_is_not_worth_leaving(self):
        make_server()
        configure_group(verified=False)
        assert (
            bot.save_group_invite_config(
                GUILD_ID, group_id=OTHER_GROUP_ID, enabled=True
            )
            is None
        )

    def test_saving_the_same_group_displaces_nothing(self):
        make_server()
        configure_group()
        assert (
            bot.save_group_invite_config(GUILD_ID, group_id=GROUP_ID, enabled=False)
            is None
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


class FakeGroupsApi:
    def __init__(self):
        self.calls = []
        self.leave_error = None

    def leave_group(self, group_id, **kwargs):
        self.calls.append(("leave_group", group_id))
        if self.leave_error:
            raise self.leave_error
        return object()


LEAVE_JOB = {
    "type": inviter.JOB_LEAVE_GROUP,
    "jobID": "job-1",
    "guildID": str(GUILD_ID),
    "groupID": GROUP_ID,
}


@pytest.fixture
def api(monkeypatch):
    fake = FakeGroupsApi()
    monkeypatch.setattr(inviter, "GroupsApi", lambda client=None: fake)
    monkeypatch.setattr(inviter.vrchat_session, "get", lambda: (object(), None))
    monkeypatch.setattr(inviter, "ApiException", FakeApiException)
    monkeypatch.setattr(inviter, "request_timeout", lambda: (1, 1))
    monkeypatch.setattr(inviter.time, "sleep", lambda *_: None)
    return fake


class TestTheWorkerLeaves:
    def test_a_clean_leave(self, api):
        result = inviter.leave_group(LEAVE_JOB)
        assert result["state"] == inviter.LEAVE_DONE
        assert result["ok"] is True
        assert api.calls == [("leave_group", GROUP_ID)]

    def test_already_out_counts_as_done(self, api):
        """A 404 is "no such group" or "not a member of it", and in both cases
        the thing the caller wanted is already true. Reporting a failure would
        pin the seat behind a group that does not exist."""
        api.leave_error = FakeApiException(404, "Not Found")
        assert inviter.leave_group(LEAVE_JOB)["state"] == inviter.LEAVE_DONE

    def test_a_real_failure_is_reported(self, api):
        api.leave_error = FakeApiException(500, "Internal Server Error")
        assert inviter.leave_group(LEAVE_JOB)["state"] == inviter.LEAVE_FAILED

    def test_no_session_means_no_call(self, api, monkeypatch):
        monkeypatch.setattr(
            inviter.vrchat_session, "get", lambda: (None, {"error_message": "down"})
        )
        assert inviter.leave_group(LEAVE_JOB)["state"] == inviter.LEAVE_FAILED
        assert api.calls == []

    @pytest.mark.parametrize("group_id", [None, "usr_not-a-group", ""])
    def test_a_malformed_job_never_reaches_the_api(self, api, group_id):
        job = dict(LEAVE_JOB, groupID=group_id)
        assert inviter.leave_group(job)["state"] == inviter.LEAVE_FAILED
        assert api.calls == []

    def test_the_result_carries_no_discord_identity(self, api):
        result = inviter.leave_group(LEAVE_JOB)
        assert set(result) == {
            "type",
            "jobID",
            "guildID",
            "groupID",
            "ok",
            "state",
            "accountID",
            "error_message",
        }


class TestTheLeaveVocabularyMatches:
    def test_the_job_type_is_the_same_string_on_both_sides(self):
        assert bot.JOB_LEAVE_GROUP == inviter.JOB_LEAVE_GROUP

    def test_every_worker_leave_state_has_a_stored_counterpart(self):
        assert inviter.LEAVE_STATES == bot.SEAT_LEAVE_STATES

    def test_the_three_job_types_are_distinct(self):
        """All three share a request queue and a result queue."""
        assert (
            len(
                {
                    bot.JOB_LEAVE_GROUP,
                    bot.JOB_SEND_GROUP_INVITE,
                    bot.JOB_VERIFY_GROUP_SETUP,
                }
            )
            == 3
        )

    def test_the_worker_knows_how_to_handle_all_three(self):
        assert set(inviter.HANDLERS) == {
            inviter.JOB_VERIFY_SETUP,
            inviter.JOB_SEND_INVITE,
            inviter.JOB_LEAVE_GROUP,
        }
