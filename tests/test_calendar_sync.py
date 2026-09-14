"""Calendar sync: a VRChat group's calendar into Discord Scheduled Events (#289, PR 1b).

Every VRChat call and every Discord call is faked. What these pin down is what
was MEASURED before any of this was written, because each measurement is a
rule the sync has to keep:

* VRChat expands a recurring event into independent occurrences, each with a
  `seriesId`; a one-off event is `occurrenceKind: "single"`.
* `roleIds` comes back as [] or null on public events, and both mean
  unrestricted.
* The invite account is a member of some groups, so a read can include
  group-scoped events. Mode 1 must filter them out itself.
* Discord refuses a start in the past, caps location at 100 characters, counts
  every scheduled or active event toward 100, and completes external events on
  its own.
"""

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import discord
import pytest

import bot


GUILD_ID = 987654321
ADMIN_ID = 4242
OWNER_ID = 77
GROUP_ID = "grp_0e1d4755-2f87-4129-a192-5587068cbf73"
OTHER_GROUP_ID = "grp_11111111-2222-3333-4444-555555555555"
NOW = datetime(2026, 9, 14, 12, 0, tzinfo=timezone.utc)


def run(coro):
    return asyncio.run(coro)


def iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def occurrence(event_id, starts, *, hours=2, series="cal_series", **overrides):
    """One trimmed event, shaped exactly as the worker's _trim_calendar_event."""
    event = {
        "id": event_id,
        "series_id": series,
        "kind": "occurrence" if series else "single",
        "access": "public",
        "role_ids": [],
        "draft": False,
        "deleted": False,
        "title": "Weekly meetup",
        "description": "Come hang out",
        "starts_at": iso(starts),
        "ends_at": iso(starts + timedelta(hours=hours)),
    }
    event.update(overrides)
    return event


def weekly(series, first, count):
    return [
        occurrence(f"cal_{series}_{i}", first + timedelta(weeks=i), series=series)
        for i in range(count)
    ]


@pytest.fixture(autouse=True)
def clean_db():
    def wipe():
        with bot.session_scope() as session:
            for model in (
                bot.Server,
                bot.GroupInviteConfig,
                bot.GroupOwnershipProof,
                bot.GroupCalendarLink,
                bot.CalendarEventSync,
                bot.DashboardAudit,
            ):
                session.query(model).delete()

    wipe()
    bot._calendar_polls.clear()
    bot._calendar_locks.clear()
    yield
    wipe()
    bot._calendar_polls.clear()
    bot._calendar_locks.clear()


@pytest.fixture(autouse=True)
def fast(monkeypatch):
    monkeypatch.setattr(bot, "CALENDAR_WRITE_SPACING_SECONDS", 0)
    monkeypatch.setattr(bot, "CALENDAR_POLL_START_SPACING_SECONDS", 0)


@pytest.fixture
def preview(monkeypatch):
    monkeypatch.setattr(bot, "CALENDAR_SYNC_PREVIEW_GUILDS", frozenset({str(GUILD_ID)}))


@pytest.fixture
def premium(monkeypatch):
    async def flags(guild_id):
        return bot.PremiumFlags(premium=True, grandfathered=False)

    monkeypatch.setattr(bot, "resolve_premium_flags", flags)


@pytest.fixture
def lapsed(monkeypatch):
    monkeypatch.setattr(bot, "PREMIUM_ENFORCED", True)

    async def flags(guild_id):
        return bot.PremiumFlags(premium=False, grandfathered=False)

    monkeypatch.setattr(bot, "resolve_premium_flags", flags)


@pytest.fixture
def published(monkeypatch):
    jobs = []

    def fake_publish(job, queue=None):
        jobs.append(dict(job))
        return True

    monkeypatch.setattr(bot, "publish_group_invite_job", fake_publish)
    return jobs


# -------------------------------------------------------------------
# A Discord guild that records what the sync did to it
# -------------------------------------------------------------------
def http_error(cls, status):
    return cls(SimpleNamespace(status=status, reason="test"), "test")


class FakeEvent:
    _next_id = 1_000

    def __init__(self, guild, **fields):
        FakeEvent._next_id += 1
        self.id = FakeEvent._next_id
        self.guild = guild
        self.status = fields.pop("status", discord.EventStatus.scheduled)
        self.fields = fields

    async def edit(self, **fields):
        if self.guild.forbid:
            raise http_error(discord.Forbidden, 403)
        self.guild.edits.append((self.id, fields))
        self.fields.update(fields)

    async def delete(self, reason=None):
        if self.guild.forbid:
            raise http_error(discord.Forbidden, 403)
        self.guild.deleted.append(self.id)
        self.guild.events.pop(self.id, None)


class FakeGuild:
    def __init__(self, can_create=True):
        self.id = GUILD_ID
        self.me = SimpleNamespace(
            guild_permissions=SimpleNamespace(create_events=can_create, manage_events=False)
        )
        self.events = {}
        self.created, self.edits, self.deleted = [], [], []
        self.forbid = False

    @property
    def scheduled_events(self):
        return list(self.events.values())

    def get_scheduled_event(self, event_id):
        return self.events.get(int(event_id))

    async def fetch_scheduled_event(self, event_id):
        event = self.events.get(int(event_id))
        if event is None:
            raise http_error(discord.NotFound, 404)
        return event

    async def create_scheduled_event(self, **fields):
        if self.forbid:
            raise http_error(discord.Forbidden, 403)
        event = FakeEvent(self, **fields)
        self.events[event.id] = event
        self.created.append(fields)
        return event

    def foreign_event(self, status=discord.EventStatus.scheduled):
        event = FakeEvent(self, name="Staff made this", status=status)
        self.events[event.id] = event
        return event


@pytest.fixture
def guild(monkeypatch):
    fake = FakeGuild()
    monkeypatch.setattr(bot.bot, "get_guild", lambda gid: fake if int(gid) == GUILD_ID else None)
    return fake


@pytest.fixture
def clock(monkeypatch):
    """Pin 'now' for the code under test without touching datetime globally."""

    class Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW if tz is None else NOW.astimezone(tz)

    monkeypatch.setattr(bot, "datetime", Frozen)
    return NOW


def make_server():
    with bot.session_scope() as session:
        session.add(
            bot.Server(
                id=10,
                server_id=str(GUILD_ID),
                owner_id=str(OWNER_ID),
                role_id="1",
                instructions_locale="en-US",
            )
        )


def connect_group(group_id=GROUP_ID, proven=True, name="Club LA"):
    bot.save_group_invite_config(GUILD_ID, group_id=group_id, enabled=False)
    if proven:
        job = bot.begin_group_claim_check(GUILD_ID)
        bot.record_group_claim_result(
            {
                "type": "verify_group_claim",
                "jobID": job["jobID"],
                "guildID": str(GUILD_ID),
                "groupID": group_id,
                "state": "proven",
                "group_name": name,
            }
        )


def enable():
    bot.save_calendar_enabled(GUILD_ID, True)


def link():
    return bot.load_calendar_link(GUILD_ID)


def rows():
    return bot.load_calendar_event_rows(GUILD_ID)


# -------------------------------------------------------------------
# Which occurrences may be mirrored at all
# -------------------------------------------------------------------
class TestEligibility:
    def event(self, **overrides):
        return occurrence("cal_1", NOW + timedelta(days=2), **overrides)

    def test_a_public_future_occurrence_is_eligible(self):
        assert bot.calendar_event_is_eligible(self.event(), NOW)

    def test_a_one_off_event_is_eligible(self):
        assert bot.calendar_event_is_eligible(self.event(series=None, kind="single"), NOW)

    @pytest.mark.parametrize("role_ids", [[], None])
    def test_no_roles_in_either_shape_is_unrestricted(self, role_ids):
        """Measured on public events: [] on one group, null on another."""
        assert bot.calendar_event_is_eligible(self.event(role_ids=role_ids), NOW)

    def test_a_role_restricted_event_never_is(self):
        """Staff meetings in a channel members can read is the leak the issue
        names. Deferred mapping, so for now: never."""
        assert not bot.calendar_event_is_eligible(self.event(role_ids=["grol_x"]), NOW)

    def test_a_group_scoped_event_is_not_mode_1(self):
        """The invite account is a member of some groups, and a member's read
        includes these. Mode 1 must not rely on them being invisible."""
        assert not bot.calendar_event_is_eligible(self.event(access="group"), NOW)

    def test_the_series_parent_is_never_an_event(self):
        """The discovery endpoints return it; syncing it would add a phantom
        event on the series' first date."""
        assert not bot.calendar_event_is_eligible(self.event(kind="series"), NOW)

    @pytest.mark.parametrize("flag", ["draft", "deleted"])
    def test_drafts_and_deleted_events_are_not(self, flag):
        assert not bot.calendar_event_is_eligible(self.event(**{flag: True}), NOW)

    def test_an_occurrence_that_has_started_is_not(self):
        """Discord refuses a start in the past (measured 2026-09-14)."""
        started = occurrence("cal_1", NOW - timedelta(minutes=10))
        assert not bot.calendar_event_is_eligible(started, NOW)

    def test_one_about_to_start_is_not_sent_to_be_refused(self):
        soon = occurrence("cal_1", NOW + timedelta(seconds=bot.CALENDAR_START_MARGIN_SECONDS - 1))
        assert not bot.calendar_event_is_eligible(soon, NOW)

    @pytest.mark.parametrize("bad", [{"starts_at": "not a date"}, {"ends_at": None}, {"id": None}])
    def test_an_unreadable_event_is_not(self, bad):
        assert not bot.calendar_event_is_eligible(self.event(**bad), NOW)


class TestTheCaps:
    def test_soonest_ten_per_series(self):
        chosen, stats = bot.select_calendar_events(weekly("a", NOW + timedelta(days=1), 52), NOW, 80)
        assert len(chosen) == bot.CALENDAR_SERIES_CAP
        assert [e["id"] for e in chosen] == [f"cal_a_{i}" for i in range(10)]
        assert stats["eligible"] == 52
        assert stats["over_cap"] == 0, "the series cap is policy, not a warning"

    def test_a_daily_series_cannot_crowd_out_a_weekly_one(self):
        daily = [
            occurrence(f"cal_d_{i}", NOW + timedelta(days=1 + i), series="d") for i in range(365)
        ]
        chosen, _ = bot.select_calendar_events(
            daily + weekly("w", NOW + timedelta(days=3), 52), NOW, 80
        )
        assert sum(1 for e in chosen if e["series_id"] == "w") == 10

    def test_the_guild_budget_keeps_the_soonest_and_counts_the_rest(self):
        """Measured: a live group had 15 series. Fifteen times ten is past 80."""
        events = []
        for series in range(15):
            events += weekly(f"s{series}", NOW + timedelta(hours=1 + series), 52)
        chosen, stats = bot.select_calendar_events(events, NOW, 80)
        assert len(chosen) == 80
        assert stats["over_cap"] == 70
        starts = [e["starts_at"] for e in chosen]
        assert starts == sorted(starts)

    def test_a_one_off_event_is_a_series_of_one(self):
        singles = [occurrence(f"cal_s{i}", NOW + timedelta(days=1 + i), series=None) for i in range(12)]
        chosen, _ = bot.select_calendar_events(singles, NOW, 80)
        assert len(chosen) == 12

    def test_an_id_returned_twice_is_synced_once(self):
        event = occurrence("cal_1", NOW + timedelta(days=1))
        chosen, stats = bot.select_calendar_events([event, dict(event)], NOW, 80)
        assert len(chosen) == 1 and stats["eligible"] == 1

    @pytest.mark.parametrize(
        "foreign, budget",
        [(0, 80), (15, 80), (20, 80), (30, 70), (100, 0), (150, 0)],
    )
    def test_the_servers_own_events_come_out_of_discords_hundred(self, foreign, budget):
        assert bot.calendar_event_budget(foreign) == budget


# -------------------------------------------------------------------
# What a Discord event says
# -------------------------------------------------------------------
class TestTheDiscordEvent:
    def test_location_never_passes_discords_limit(self):
        """Measured: 100 accepted, 101 refused."""
        fields = bot.build_discord_event_fields(
            occurrence("cal_1", NOW + timedelta(days=1)), GROUP_ID, "x" * 300
        )
        assert len(fields["location"]) <= 100

    def test_the_group_link_survives_a_long_description(self):
        event = occurrence("cal_1", NOW + timedelta(days=1), description="y" * 5000)
        fields = bot.build_discord_event_fields(event, GROUP_ID, "Club LA")
        assert len(fields["description"]) <= 1000
        assert fields["description"].endswith(f"https://vrchat.com/home/group/{GROUP_ID}")

    def test_a_long_title_is_clipped_and_an_empty_one_named(self):
        long = bot.build_discord_event_fields(
            occurrence("cal_1", NOW + timedelta(days=1), title="z" * 400), GROUP_ID, None
        )
        empty = bot.build_discord_event_fields(
            occurrence("cal_1", NOW + timedelta(days=1), title="  "), GROUP_ID, None
        )
        assert len(long["name"]) <= 100
        assert empty["name"] == "VRChat event"

    def test_the_times_are_the_occurrences_own(self):
        start = NOW + timedelta(days=1)
        fields = bot.build_discord_event_fields(occurrence("cal_1", start), GROUP_ID, None)
        assert fields["start_time"] == start
        assert fields["end_time"] == start + timedelta(hours=2)

    def test_the_same_content_hashes_the_same(self):
        event = occurrence("cal_1", NOW + timedelta(days=1))
        one = bot.build_discord_event_fields(event, GROUP_ID, "Club LA")
        two = bot.build_discord_event_fields(dict(event), GROUP_ID, "Club LA")
        assert bot.calendar_content_hash(one) == bot.calendar_content_hash(two)

    def test_a_moved_start_is_an_edit(self):
        event = occurrence("cal_1", NOW + timedelta(days=1))
        moved = dict(event, starts_at=iso(NOW + timedelta(days=1, hours=1)))
        a = bot.calendar_content_hash(bot.build_discord_event_fields(event, GROUP_ID, None))
        b = bot.calendar_content_hash(bot.build_discord_event_fields(moved, GROUP_ID, None))
        assert a != b


# -------------------------------------------------------------------
# The plan: create, update, delete
# -------------------------------------------------------------------
class TestThePlan:
    def row_for(self, event, **overrides):
        fields = bot.build_discord_event_fields(event, GROUP_ID, "Club LA")
        row = {
            "vrc_event_id": event["id"],
            "discord_event_id": "555",
            "starts_at": fields["start_time"],
            "ends_at": fields["end_time"],
            "content_hash": bot.calendar_content_hash(fields),
            "state": bot.CALENDAR_EVENT_SYNCED,
        }
        row.update(overrides)
        return row

    def plan(self, rows, chosen):
        return bot.plan_calendar_changes(rows, chosen, GROUP_ID, "Club LA", NOW)

    def test_a_new_occurrence_is_created(self):
        event = occurrence("cal_1", NOW + timedelta(days=1))
        assert len(self.plan({}, [event])["create"]) == 1

    def test_an_unchanged_one_is_left_alone(self):
        event = occurrence("cal_1", NOW + timedelta(days=1))
        plan = self.plan({"cal_1": self.row_for(event)}, [event])
        assert not any(plan.values())

    def test_a_changed_one_is_updated(self):
        event = occurrence("cal_1", NOW + timedelta(days=1))
        row = self.row_for(event)
        plan = self.plan({"cal_1": row}, [dict(event, title="Moved to Thursdays")])
        assert len(plan["update"]) == 1

    def test_one_a_moderator_deleted_is_not_recreated(self):
        event = occurrence("cal_1", NOW + timedelta(days=1))
        row = self.row_for(event, state=bot.CALENDAR_EVENT_REMOVED_BY_ADMIN, content_hash="old")
        assert not any(self.plan({"cal_1": row}, [event]).values())

    def test_an_occurrence_gone_from_vrchat_is_only_marked_the_first_time(self):
        event = occurrence("cal_1", NOW + timedelta(days=1))
        plan = self.plan({"cal_1": self.row_for(event)}, [])
        assert plan["delete"] == [] and len(plan["missing"]) == 1

    def test_and_deleted_when_the_next_poll_still_does_not_see_it(self):
        event = occurrence("cal_1", NOW + timedelta(days=1))
        row = self.row_for(event, missing_since=NOW - timedelta(hours=1))
        assert len(self.plan({"cal_1": row}, [])["delete"]) == 1

    def test_one_seen_again_is_no_longer_missing(self):
        event = occurrence("cal_1", NOW + timedelta(days=1))
        row = self.row_for(event, missing_since=NOW - timedelta(hours=1))
        assert self.plan({"cal_1": row}, [event])["seen_again"] == ["cal_1"]

    def test_one_that_has_started_is_never_deleted(self):
        """Discord completes it; deleting it would pull it mid-event."""
        event = occurrence("cal_1", NOW - timedelta(minutes=30))
        plan = self.plan({"cal_1": self.row_for(event, missing_since=NOW)}, [])
        assert plan["delete"] == [] and plan["missing"] == []

    def test_one_about_to_start_is_not_deleted_for_dropping_out_of_the_margin(self):
        """It leaves `chosen` because of the start margin, not because it went
        away. Found reading the first cut, before any test existed."""
        event = occurrence("cal_1", NOW + timedelta(seconds=60))
        plan = self.plan({"cal_1": self.row_for(event, missing_since=NOW)}, [])
        assert plan["delete"] == [] and plan["missing"] == []

    def test_an_occurrence_moved_after_it_already_ran_is_a_new_event(self):
        """A finished Discord event cannot be edited (error 180000)."""
        ran = occurrence("cal_1", NOW - timedelta(days=1))
        moved = dict(ran, starts_at=iso(NOW + timedelta(days=3)), ends_at=iso(NOW + timedelta(days=3, hours=2)))
        plan = self.plan({"cal_1": self.row_for(ran)}, [moved])
        assert len(plan["create"]) == 1 and plan["update"] == []


# -------------------------------------------------------------------
# Storage and the poll's lifecycle
# -------------------------------------------------------------------
class TestThePollLifecycle:
    def test_turning_it_on_asks_for_a_poll_now(self):
        enable()
        assert link()["enabled"] is True
        assert bot.calendar_poll_is_due(link(), datetime.now(timezone.utc))

    def test_a_poll_in_flight_is_not_started_twice(self):
        enable()
        bot.begin_calendar_poll(GUILD_ID, GROUP_ID)
        assert not bot.calendar_poll_is_due(link(), datetime.now(timezone.utc))

    def test_a_lost_poll_is_replaced_after_its_timeout(self):
        enable()
        bot.begin_calendar_poll(GUILD_ID, GROUP_ID)
        later = datetime.now(timezone.utc) + timedelta(seconds=bot.CALENDAR_POLL_TIMEOUT_SECONDS + 5)
        assert bot.calendar_poll_is_due(link(), later)

    def test_ending_a_poll_schedules_the_next_with_jitter(self):
        enable()
        job = bot.begin_calendar_poll(GUILD_ID, GROUP_ID)
        assert bot.end_calendar_poll(GUILD_ID, job["jobID"], bot.CALENDAR_SYNCED)
        wait = (link()["next_poll_at"] - datetime.now(timezone.utc)).total_seconds()
        interval = bot.CALENDAR_POLL_INTERVAL_SECONDS
        assert 0.8 * interval - 5 <= wait <= 1.2 * interval + 5

    def test_a_failed_poll_waits_the_interval_too(self):
        """VRChat being down is not a reason to ask it more often."""
        enable()
        job = bot.begin_calendar_poll(GUILD_ID, GROUP_ID)
        bot.end_calendar_poll(GUILD_ID, job["jobID"], bot.CALENDAR_PAGE_VRCHAT_UNAVAILABLE)
        assert not bot.calendar_poll_is_due(link(), datetime.now(timezone.utc))

    def test_ending_someone_elses_poll_is_refused(self):
        enable()
        bot.begin_calendar_poll(GUILD_ID, GROUP_ID)
        assert bot.end_calendar_poll(GUILD_ID, "not-this-one", bot.CALENDAR_SYNCED) is False


# -------------------------------------------------------------------
# Pages coming back from the worker
# -------------------------------------------------------------------
class TestPages:
    def start(self, published):
        enable()
        job = bot.begin_calendar_poll(GUILD_ID, GROUP_ID)
        bot._calendar_polls[job["jobID"]] = {
            "guild_id": str(GUILD_ID),
            "group_id": GROUP_ID,
            "next_offset": 0,
            "pages": 0,
            "events": [],
            "started": datetime.now(timezone.utc),
        }
        return job

    def page(self, job, offset=0, count=100, events=None, **overrides):
        payload = {
            "type": bot.JOB_FETCH_CALENDAR_PAGE,
            "jobID": job["jobID"],
            "guildID": str(GUILD_ID),
            "groupID": GROUP_ID,
            "offset": offset,
            "state": "ok",
            "events": events if events is not None else [],
            "count": count,
            "n": 100,
        }
        payload.update(overrides)
        return payload

    def test_a_full_page_asks_for_the_next(self, published):
        job = self.start(published)
        assert run(bot.handle_calendar_page_result(self.page(job, count=100))) == "next_page"
        assert published[-1]["offset"] == 100
        assert published[-1]["jobID"] == job["jobID"]

    def test_a_short_page_ends_the_read_and_syncs(self, published, monkeypatch):
        job = self.start(published)
        seen = {}

        async def fake_sync(guild_id, group_id, events, job_id):
            seen.update(events=events, job_id=job_id)
            return "synced"

        monkeypatch.setattr(bot, "sync_calendar_to_discord", fake_sync)
        run(bot.handle_calendar_page_result(self.page(job, count=100, events=[{"id": "a"}])))
        run(bot.handle_calendar_page_result(self.page(job, offset=100, count=3, events=[{"id": "b"}])))
        assert [e["id"] for e in seen["events"]] == ["a", "b"]
        assert job["jobID"] not in bot._calendar_polls

    def test_the_page_cap_ends_even_a_calendar_that_never_runs_out(self, published, monkeypatch):
        monkeypatch.setattr(bot, "CALENDAR_MAX_PAGES", 2)
        synced = []

        async def fake_sync(*args):
            synced.append(args)
            return "synced"

        monkeypatch.setattr(bot, "sync_calendar_to_discord", fake_sync)
        job = self.start(published)
        run(bot.handle_calendar_page_result(self.page(job, count=100)))
        run(bot.handle_calendar_page_result(self.page(job, offset=100, count=100)))
        assert len(synced) == 1

    def test_a_redelivered_page_is_ignored_rather_than_ending_the_poll(self, published):
        """Found attacking the branch. RabbitMQ redelivers a result when an ack
        is lost, so the same page can arrive twice. The copy is behind the
        poll, not out of order, and must not throw the poll away."""
        job = self.start(published)
        run(bot.handle_calendar_page_result(self.page(job, count=100)))
        assert run(bot.handle_calendar_page_result(self.page(job, count=100))) == "duplicate"
        assert link()["poll_job_id"] == job["jobID"]
        assert len(published) == 1, "the next page is asked for once"

    def test_an_answer_for_another_poll_is_stale(self, published):
        job = self.start(published)
        assert run(bot.handle_calendar_page_result(self.page(dict(job, jobID="old")))) == "stale"

    def test_a_page_out_of_order_loses_the_poll_rather_than_syncing_half(self, published):
        job = self.start(published)
        assert run(bot.handle_calendar_page_result(self.page(job, offset=300))) == "lost"
        assert link()["poll_job_id"] is None
        assert link()["last_state"] == bot.CALENDAR_TIMED_OUT

    def test_pages_lost_to_a_restart_end_the_poll(self, published):
        job = self.start(published)
        bot._calendar_polls.clear()
        assert run(bot.handle_calendar_page_result(self.page(job, offset=100))) == "lost"

    def test_a_vrchat_failure_is_recorded_and_the_poll_ends(self, published):
        job = self.start(published)
        run(bot.handle_calendar_page_result(
            self.page(job, state="vrchat_unavailable", error_message="VRChat said no")
        ))
        assert link()["last_state"] == "vrchat_unavailable"
        assert link()["last_error"] == "VRChat said no"
        assert link()["poll_job_id"] is None

    def test_a_state_nobody_defined_is_not_stored_verbatim(self, published):
        job = self.start(published)
        run(bot.handle_calendar_page_result(self.page(job, state="surprise")))
        assert link()["last_state"] == bot.CALENDAR_PAGE_VRCHAT_UNAVAILABLE

    def test_it_is_routed_from_the_shared_result_queue(self, published, monkeypatch):
        handled = []

        async def fake(data):
            handled.append(data)
            return "ok"

        monkeypatch.setattr(bot, "handle_calendar_page_result", fake)
        run(bot.handle_group_invite_result({"type": bot.JOB_FETCH_CALENDAR_PAGE, "jobID": "x"}))
        assert len(handled) == 1


# -------------------------------------------------------------------
# Writing Discord
# -------------------------------------------------------------------
class TestTheSync:
    def begin(self):
        make_server()
        connect_group()
        enable()
        return bot.begin_calendar_poll(GUILD_ID, GROUP_ID)

    def sync(self, events):
        job = self.begin() if link() is None or link()["poll_job_id"] is None and not rows() else None
        if job is None:
            job = bot.begin_calendar_poll(GUILD_ID, GROUP_ID)
        return run(bot.sync_calendar_to_discord(str(GUILD_ID), GROUP_ID, events, job["jobID"]))

    def test_it_creates_external_guild_only_events(self, guild, clock):
        state = self.sync(weekly("a", NOW + timedelta(days=1), 3))
        assert state == bot.CALENDAR_SYNCED
        assert len(guild.created) == 3
        made = guild.created[0]
        assert made["entity_type"] == discord.EntityType.external
        assert made["privacy_level"] == discord.PrivacyLevel.guild_only
        assert made["location"] == "VRChat: Club LA"
        assert set(rows()) == {"cal_a_0", "cal_a_1", "cal_a_2"}
        assert link()["synced_count"] == 3

    def test_a_second_poll_with_nothing_new_writes_nothing(self, guild, clock):
        events = weekly("a", NOW + timedelta(days=1), 3)
        self.sync(events)
        self.sync(events)
        assert len(guild.created) == 3 and guild.edits == [] and guild.deleted == []

    def test_an_edit_in_vrchat_is_patched(self, guild, clock):
        events = weekly("a", NOW + timedelta(days=1), 2)
        self.sync(events)
        events[0] = dict(events[0], title="Moved to Thursdays")
        self.sync(events)
        assert len(guild.edits) == 1
        assert guild.edits[0][1]["name"] == "Moved to Thursdays"

    def test_an_occurrence_removed_in_vrchat_is_deleted_on_the_second_poll(self, guild, clock):
        events = weekly("a", NOW + timedelta(days=1), 2)
        self.sync(events)
        self.sync(events[:1])
        assert guild.deleted == [], "one poll not seeing it is not enough"
        self.sync(events[:1])
        assert len(guild.deleted) == 1
        assert set(rows()) == {"cal_a_0"}

    def test_one_empty_answer_from_vrchat_deletes_nothing(self, guild, clock):
        """Recreating an event loses every member's Interested. A single empty
        page, which VRChat could return by mistake, must not cost that."""
        events = weekly("a", NOW + timedelta(days=1), 3)
        self.sync(events)
        self.sync([])
        self.sync(events)
        assert guild.deleted == [] and len(guild.created) == 3
        assert all(r["missing_since"] is None for r in rows().values())

    def test_a_moderators_deletion_sticks(self, guild, clock):
        events = weekly("a", NOW + timedelta(days=1), 1)
        self.sync(events)
        guild.events.clear()  # deleted by hand in Discord
        self.sync([dict(events[0], title="Edited in VRChat")])
        assert rows()["cal_a_0"]["state"] == bot.CALENDAR_EVENT_REMOVED_BY_ADMIN
        self.sync([dict(events[0], title="Edited again")])
        assert len(guild.created) == 1, "recreating it would be arguing with a moderator"

    def test_an_event_the_database_could_not_record_is_taken_back_down(self, guild, clock, monkeypatch):
        """Found attacking the branch. Discord made the event, then storing its
        row failed: nothing remembers it, so the next poll would make it again
        and members would see it twice."""
        def broken(*args, **kwargs):
            raise RuntimeError("database went away")

        self.begin()
        job = bot.begin_calendar_poll(GUILD_ID, GROUP_ID)
        monkeypatch.setattr(bot, "store_calendar_event_row", broken)
        state = run(bot.sync_calendar_to_discord(
            str(GUILD_ID), GROUP_ID, weekly("a", NOW + timedelta(days=1), 2), job["jobID"]
        ))
        assert guild.events == {}, "no orphan left behind"
        assert state == bot.CALENDAR_DISCORD_ERROR
        assert link()["poll_job_id"] is None, "the poll is ended, not left hanging"

    def test_without_create_events_nothing_is_written(self, guild, clock):
        guild.me.guild_permissions.create_events = False
        state = self.sync(weekly("a", NOW + timedelta(days=1), 3))
        assert state == bot.CALENDAR_MISSING_PERMISSION
        assert guild.created == []
        assert link()["last_state"] == bot.CALENDAR_MISSING_PERMISSION

    def test_a_permission_pulled_mid_sync_stops_it(self, guild, clock):
        self.sync(weekly("a", NOW + timedelta(days=1), 1))
        guild.forbid = True
        state = self.sync(weekly("a", NOW + timedelta(days=1), 3))
        assert state == bot.CALENDAR_MISSING_PERMISSION
        assert len(guild.created) == 1

    def test_the_servers_own_events_shrink_the_budget(self, guild, clock, monkeypatch):
        monkeypatch.setattr(bot, "CALENDAR_GUILD_CEILING", 5)
        monkeypatch.setattr(bot, "DISCORD_SCHEDULED_EVENT_LIMIT", 6)
        guild.foreign_event()
        guild.foreign_event()
        guild.foreign_event(status=discord.EventStatus.completed)  # does not count
        self.sync([occurrence(f"cal_{i}", NOW + timedelta(days=1 + i), series=None) for i in range(10)])
        assert len(guild.created) == 4
        assert link()["over_cap_count"] == 6

    def test_a_sync_for_a_poll_that_was_replaced_writes_nothing(self, guild, clock):
        job = self.begin()
        bot.begin_calendar_poll(GUILD_ID, GROUP_ID)  # a newer poll
        state = run(bot.sync_calendar_to_discord(str(GUILD_ID), GROUP_ID, weekly("a", NOW + timedelta(days=1), 2), job["jobID"]))
        assert state == "stale" and guild.created == []

    def test_a_group_changed_mid_poll_writes_nothing(self, guild, clock):
        job = self.begin()
        bot.save_group_invite_config(GUILD_ID, group_id=OTHER_GROUP_ID, enabled=False)
        state = run(bot.sync_calendar_to_discord(str(GUILD_ID), GROUP_ID, weekly("a", NOW + timedelta(days=1), 2), job["jobID"]))
        assert state == "stale" and guild.created == []


# -------------------------------------------------------------------
# The pass: who is polled, and what gets cleaned up
# -------------------------------------------------------------------
class TestThePass:
    def synced_guild(self, guild):
        make_server()
        connect_group()
        enable()
        job = bot.begin_calendar_poll(GUILD_ID, GROUP_ID)
        run(bot.sync_calendar_to_discord(
            str(GUILD_ID), GROUP_ID, weekly("a", NOW + timedelta(days=1), 3), job["jobID"]
        ))
        assert len(guild.events) == 3

    def test_a_due_preview_guild_is_polled(self, guild, preview, premium, published):
        make_server()
        connect_group()
        enable()
        outcome = run(bot.calendar_sync_pass())
        assert outcome["started"] == 1
        assert published[0]["type"] == bot.JOB_FETCH_CALENDAR_PAGE
        assert published[0]["groupID"] == GROUP_ID and published[0]["offset"] == 0

    def test_an_unannounced_feature_is_not_polled_for_anyone_else(self, guild, premium, published):
        make_server()
        connect_group()
        enable()
        assert run(bot.calendar_sync_pass())["started"] == 0
        assert published == []

    def test_an_unproven_group_is_not_polled(self, guild, preview, premium, published):
        make_server()
        connect_group(proven=False)
        enable()
        assert run(bot.calendar_sync_pass())["started"] == 0

    def test_a_publish_failure_is_recorded(self, guild, preview, premium, monkeypatch):
        monkeypatch.setattr(bot, "publish_group_invite_job", lambda job, queue=None: False)
        make_server()
        connect_group()
        enable()
        run(bot.calendar_sync_pass())
        assert link()["last_state"] == bot.CALENDAR_WORKER_UNREACHABLE
        assert link()["poll_job_id"] is None

    def test_turning_it_off_deletes_the_upcoming_events(self, guild, clock, preview, premium, published):
        self.synced_guild(guild)
        bot.save_calendar_enabled(GUILD_ID, False)
        run(bot.calendar_sync_pass())
        assert guild.events == {}
        assert rows() == {}

    def test_changing_the_group_deletes_the_old_groups_events(self, guild, clock, preview, premium, published):
        self.synced_guild(guild)
        bot.save_group_invite_config(GUILD_ID, group_id=OTHER_GROUP_ID, enabled=False)
        run(bot.calendar_sync_pass())
        assert guild.events == {}
        assert rows() == {}
        assert link()["group_id"] is None

    def test_a_lapse_leaves_the_events_alone(self, guild, clock, preview, lapsed, published):
        """Decided on #289: syncing stops, and the events run out on their own."""
        self.synced_guild(guild)
        # Due now, so the only thing that can stop a poll is the lapse itself.
        with bot.session_scope() as session:
            session.query(bot.GroupCalendarLink).first().next_poll_at = None
        run(bot.calendar_sync_pass())
        assert len(guild.events) == 3
        assert published == []

    def test_turning_it_off_never_deletes_one_that_is_running(self, guild, clock, preview, premium, published):
        self.synced_guild(guild)
        with bot.session_scope() as session:
            row = session.query(bot.CalendarEventSync).filter_by(vrc_event_id="cal_a_0").first()
            row.starts_at = NOW - timedelta(minutes=5)
        bot.save_calendar_enabled(GUILD_ID, False)
        run(bot.calendar_sync_pass())
        assert len(guild.events) == 1

    def test_one_guilds_failure_does_not_stop_the_pass(self, guild, preview, published, monkeypatch):
        async def boom(guild_id):
            raise RuntimeError("entitlements down")

        monkeypatch.setattr(bot, "resolve_premium_flags", boom)
        make_server()
        connect_group()
        enable()
        assert run(bot.calendar_sync_pass()) == {"started": 0, "cleared": 0}


# -------------------------------------------------------------------
# The settings gate
# -------------------------------------------------------------------
class TestTheSwitch:
    def test_a_guild_outside_the_preview_cannot_set_it(self, premium):
        make_server()
        with pytest.raises(bot.SettingRejected) as caught:
            run(bot.write_dashboard_settings(GUILD_ID, ADMIN_ID, {"calendar_sync_enabled": True}))
        assert caught.value.reason == "not_writable_yet"
        assert link() is None

    def test_a_preview_guild_can_and_it_is_audited(self, preview, premium, monkeypatch):
        make_server()
        run(bot.write_dashboard_settings(GUILD_ID, ADMIN_ID, {"calendar_sync_enabled": True}))
        assert link()["enabled"] is True
        with bot.session_scope() as session:
            fields = [r.field for r in session.query(bot.DashboardAudit).all()]
        assert fields == ["calendar_sync_enabled"]

    def test_it_is_not_reachable_by_default(self):
        assert bot.FEATURE_CALENDAR_SYNC in bot.UNANNOUNCED_FEATURES
        assert not bot.feature_is_reachable(bot.FEATURE_CALENDAR_SYNC, GUILD_ID)
        assert bot.feature_is_reachable(bot.FEATURE_GROUP_INVITE, GUILD_ID)

    def test_the_preview_list_is_read_strictly(self):
        assert bot._guild_id_set(" 1, 2 ,abc,,3 ") == frozenset({"1", "2", "3"})
        assert bot._guild_id_set(None) == frozenset()


class TestTheContractWithTheWorker:
    def test_the_job_type_matches(self):
        import vrc_group_inviter as inviter

        assert bot.JOB_FETCH_CALENDAR_PAGE == inviter.JOB_FETCH_CALENDAR_PAGE

    def test_the_vocabularies_match(self):
        import vrc_group_inviter as inviter

        assert bot.CALENDAR_PAGE_STATES == inviter.CALENDAR_PAGE_STATES

    def test_the_page_size_matches(self):
        import vrc_group_inviter as inviter

        assert bot.CALENDAR_PAGE_SIZE == inviter.CALENDAR_PAGE_MAX

    def test_a_trimmed_worker_event_is_eligible(self):
        """Built by the worker's own trimmer from raw JSON shaped like the
        measured one, not by a fixture that agrees with the bot by construction."""
        import vrc_group_inviter as inviter

        raw = {
            "id": "cal_162c3909-7f81-4975-85a0-a66c67237eca",
            "occurrenceKind": "single",
            "seriesId": None,
            "accessType": "public",
            "roleIds": None,
            "isDraft": False,
            "deletedAt": None,
            "title": "Hangout",
            "description": "",
            "startsAt": iso(NOW + timedelta(days=4)),
            "endsAt": iso(NOW + timedelta(days=4, hours=1)),
        }
        assert bot.calendar_event_is_eligible(inviter._trim_calendar_event(raw), NOW)
