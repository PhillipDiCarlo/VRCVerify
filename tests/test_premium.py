"""Unit tests for the Discord App Subscriptions premium tier (issue #46).

The premium tier gates automation that used to be unconditional, so the tests
here are mostly about the ways that gating can go wrong rather than the happy
path:

- with no SKU configured the whole system must be inert, so shipping this code
  before the tier launches changes nothing
- an entitlement lookup that fails must never quietly switch a paying server's
  automation off
- grandfathered servers keep exactly the three features they were promised, and
  auto-verify-on-join is pointedly not one of them
- the cutover announcement can never DM the same server twice, and can never
  turn into a burst
"""

import asyncio
import logging
from contextlib import contextmanager
from types import SimpleNamespace

import discord
import pytest

import bot

GUILD_ID = "987654321"
OWNER_ID = "77"
SKU_ID = 555000111


def run(coro):
    """Run an async bot helper from a sync test (no pytest-asyncio needed)."""
    return asyncio.run(coro)


def run_and_drain(coro):
    """Run `coro`, then cancel anything it left behind (e.g. _delayed_cleanup)."""

    async def scenario():
        result = await coro
        await asyncio.sleep(0)
        pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        return result

    return asyncio.run(scenario())


class FakeEntitlement:
    """Enough of discord.Entitlement for the resolution helpers."""

    def __init__(self, sku_id=SKU_ID, expired=False, deleted=False, guild_id=GUILD_ID):
        self.sku_id = sku_id
        self.deleted = deleted
        self.guild_id = int(guild_id) if guild_id is not None else None
        self._expired = expired

    def is_expired(self) -> bool:
        return self._expired


def fake_entitlements_api(*items, error: Exception | None = None):
    """Stand-in for Client.entitlements(): an async iterator, or a raiser."""

    def _entitlements(**kwargs):
        async def generate():
            if error is not None:
                raise error
            for item in items:
                yield item

        return generate()

    return _entitlements


def counting_entitlements_api(*items, error: Exception | None = None):
    """Same, but records every call so a test can assert it was never made.

    Asserting "must not be called" by raising from the stub does not work here:
    guild_has_premium catches Exception and fails open, so the raise would be
    swallowed and the test would pass while the regression it guards against
    was live. Count the calls instead.
    """
    calls = []

    def _entitlements(**kwargs):
        calls.append(kwargs)
        return fake_entitlements_api(*items, error=error)(**kwargs)

    return _entitlements, calls


def make_interaction(entitlements=(), guild_id=GUILD_ID, locale="en-US"):
    return SimpleNamespace(
        guild_id=int(guild_id) if guild_id is not None else None,
        entitlements=list(entitlements),
        locale=locale,
        guild=SimpleNamespace(id=int(guild_id or 0), name="Test Server"),
        user=SimpleNamespace(id=1),
    )


# Grandfathering is `servers.id <= the captured line`, so tests pick a row id
# on the side of the line they mean. The line itself is drawn by the autouse
# fixture below.
LINE = 820
OLD_ID = 100  # comfortably before the cutover
NEW_ID = 5000  # comfortably after it


def draw_line(max_server_id=LINE):
    """Capture the grandfather line, as the first enforced startup would."""
    with bot.session_scope() as session:
        session.query(bot.PremiumGrandfatherLine).delete()
        session.add(
            bot.PremiumGrandfatherLine(id=1, max_server_id=max_server_id)
        )


def make_server(server_id=GUILD_ID, row_id=OLD_ID, **overrides):
    fields = dict(
        id=row_id,
        server_id=server_id,
        owner_id=OWNER_ID,
        role_id="1",
        instructions_locale="en-US",
    )
    fields.update(overrides)
    with bot.session_scope() as session:
        session.add(bot.Server(**fields))


def mark_notified(server_id=GUILD_ID):
    with bot.session_scope() as session:
        session.add(bot.PremiumCutoverNotice(server_id=server_id))


@pytest.fixture(autouse=True)
def clean_db():
    def wipe():
        with bot.session_scope() as session:
            session.query(bot.Server).delete()
            session.query(bot.User).delete()
            session.query(bot.PremiumCutoverNotice).delete()
            session.query(bot.PremiumGrandfatherLine).delete()

    wipe()
    bot.premium_status_cache.clear()
    bot._cutover_reminder_logged = False
    # Most tests care about which side of the line a server falls on, not about
    # the capture itself, so start with the line already drawn. The tests that
    # exercise capture clear it first.
    draw_line()
    yield
    wipe()
    bot.premium_status_cache.clear()
    bot._cutover_reminder_logged = False


@pytest.fixture
def enforced(monkeypatch):
    """Turn the tier on, as if PREMIUM_SKU_ID were configured."""
    monkeypatch.setattr(bot, "PREMIUM_SKU_ID", SKU_ID)
    monkeypatch.setattr(bot, "PREMIUM_ENFORCED", True)
    bot.premium_status_cache.clear()


# ---------------------------------------------------------------
# The kill switch
# ---------------------------------------------------------------
class TestNotEnforced:
    """With no SKU configured nothing may be gated. This is what lets the
    code deploy before the tier exists."""

    def test_every_feature_is_allowed(self):
        flags = run(bot.resolve_premium_flags(GUILD_ID))
        for feature in (
            bot.FEATURE_UNVERIFIED_ROLE_REMOVAL,
            bot.FEATURE_NICKNAME_SYNC,
            bot.FEATURE_CUSTOM_DM,
            bot.FEATURE_REDUCED_COOLDOWN,
        ):
            assert flags.allows(feature)

    def test_guild_has_premium_is_true_without_asking_discord(self, monkeypatch):
        monkeypatch.setattr(
            bot.bot,
            "entitlements",
            fake_entitlements_api(error=AssertionError("must not be called")),
        )
        assert run(bot.guild_has_premium(GUILD_ID)) is True

    def test_cooldown_window_is_the_premium_one(self):
        # Not enforced means "behave as if subscribed", including the shorter
        # throttle — there is no tier to withhold it for yet.
        flags = run(bot.resolve_premium_flags(GUILD_ID))
        assert flags.cooldown_window() == bot.PREMIUM_VERIFICATION_COOLDOWN_SECONDS


# ---------------------------------------------------------------
# Reading entitlements
# ---------------------------------------------------------------
class TestEntitlementsGrantPremium:
    def test_live_entitlement_for_our_sku(self, enforced):
        assert bot.entitlements_grant_premium([FakeEntitlement()]) is True

    def test_other_sku_is_ignored(self, enforced):
        assert bot.entitlements_grant_premium([FakeEntitlement(sku_id=1)]) is False

    def test_expired_entitlement_does_not_count(self, enforced):
        assert bot.entitlements_grant_premium([FakeEntitlement(expired=True)]) is False

    def test_refunded_entitlement_does_not_count(self, enforced):
        assert bot.entitlements_grant_premium([FakeEntitlement(deleted=True)]) is False

    def test_empty_and_none(self, enforced):
        assert bot.entitlements_grant_premium([]) is False
        assert bot.entitlements_grant_premium(None) is False

    def test_one_live_among_dead_ones(self, enforced):
        entitlements = [
            FakeEntitlement(expired=True),
            FakeEntitlement(sku_id=42),
            FakeEntitlement(),
        ]
        assert bot.entitlements_grant_premium(entitlements) is True


class TestPremiumFromInteraction:
    def test_reads_the_payload_and_seeds_the_cache(self, enforced):
        interaction = make_interaction([FakeEntitlement()])
        assert bot.premium_from_interaction(interaction) is True
        # Seeded, so a later background lookup costs no API call.
        assert bot.premium_status_cache.get_fresh(GUILD_ID) is True

    def test_absence_means_not_entitled(self, enforced):
        interaction = make_interaction([])
        assert bot.premium_from_interaction(interaction) is False
        assert bot.premium_status_cache.get_fresh(GUILD_ID) is False

    def test_outside_a_guild_is_never_premium(self, enforced):
        assert bot.premium_from_interaction(make_interaction(guild_id=None)) is False

    def test_a_positive_read_becomes_the_fallback(self, enforced):
        # Finding our SKU in the payload is proof, so it is safe to promote.
        bot.premium_from_interaction(make_interaction([FakeEntitlement()]))
        assert bot.premium_status_cache.get_last_known(GUILD_ID) is True

    def test_a_negative_read_never_poisons_the_fallback(self, enforced, monkeypatch):
        """The riskiest failure mode in the whole cache.

        discord.py builds entitlements from data.get('entitlements', []), so an
        absent field is indistinguishable from a genuinely empty one. If a
        negative read were promoted to the last-known value, a paying guild
        would fail *closed* on the next outage — the exact inverse of what the
        fail-open design promises, and silent.
        """
        assert bot.premium_from_interaction(make_interaction([])) is False
        assert bot.premium_status_cache.get_last_known(GUILD_ID) is None

        bot.premium_status_cache.invalidate(GUILD_ID)
        monkeypatch.setattr(
            bot.bot, "entitlements", fake_entitlements_api(error=RuntimeError("boom"))
        )
        assert run(bot.guild_has_premium(GUILD_ID)) is True


# ---------------------------------------------------------------
# The cache, and what happens when Discord is unreachable
# ---------------------------------------------------------------
class TestPremiumStatusCache:
    def test_value_expires_but_last_known_does_not(self, monkeypatch):
        cache = bot.PremiumStatusCache(ttl=60)
        cache.set(GUILD_ID, True)
        assert cache.get_fresh(GUILD_ID) is True

        clock = [0.0]
        monkeypatch.setattr(bot.time, "monotonic", lambda: clock[0])
        cache = bot.PremiumStatusCache(ttl=60)
        cache.set(GUILD_ID, True)
        clock[0] = 61.0
        assert cache.get_fresh(GUILD_ID) is None
        assert cache.get_last_known(GUILD_ID) is True

    def test_invalidate_keeps_the_fallback(self):
        cache = bot.PremiumStatusCache(ttl=60)
        cache.set(GUILD_ID, True)
        cache.invalidate(GUILD_ID)
        assert cache.get_fresh(GUILD_ID) is None
        assert cache.get_last_known(GUILD_ID) is True


class TestGuildHasPremium:
    def test_live_entitlement_resolves_true_and_caches(self, enforced, monkeypatch):
        calls = []

        def counting(**kwargs):
            calls.append(kwargs)
            return fake_entitlements_api(FakeEntitlement())(**kwargs)

        monkeypatch.setattr(bot.bot, "entitlements", counting)
        assert run(bot.guild_has_premium(GUILD_ID)) is True
        # Second read is served from cache.
        assert run(bot.guild_has_premium(GUILD_ID)) is True
        assert len(calls) == 1

    def test_no_entitlement_resolves_false(self, enforced, monkeypatch):
        monkeypatch.setattr(bot.bot, "entitlements", fake_entitlements_api())
        assert run(bot.guild_has_premium(GUILD_ID)) is False

    def test_lookup_failure_serves_the_last_known_value(self, enforced, monkeypatch):
        # A server we know is NOT subscribed must not be handed premium just
        # because a later lookup failed.
        bot.premium_status_cache.set(GUILD_ID, False)
        bot.premium_status_cache.invalidate(GUILD_ID)
        monkeypatch.setattr(
            bot.bot, "entitlements", fake_entitlements_api(error=RuntimeError("boom"))
        )
        assert run(bot.guild_has_premium(GUILD_ID)) is False

    def test_lookup_failure_with_nothing_known_fails_open(self, enforced, monkeypatch):
        monkeypatch.setattr(
            bot.bot, "entitlements", fake_entitlements_api(error=RuntimeError("boom"))
        )
        # Cold cache during a Discord incident: a paying server keeps working.
        assert run(bot.guild_has_premium(GUILD_ID)) is True

    def test_a_sustained_outage_does_not_amplify_into_a_call_per_check(
        self, enforced, monkeypatch
    ):
        """Failures must be cached too, or an outage becomes a request storm.

        Without this, every verification result retries the failing endpoint
        with no backoff, and discord.py's 429 handling then throttles the whole
        bot — verification DMs included — exactly when Discord is struggling.
        """
        api, calls = counting_entitlements_api(error=RuntimeError("boom"))
        monkeypatch.setattr(bot.bot, "entitlements", api)

        for _ in range(5):
            assert run(bot.guild_has_premium(GUILD_ID)) is True
        assert len(calls) == 1

    def test_a_guess_never_becomes_the_permanent_fallback(self, enforced, monkeypatch):
        monkeypatch.setattr(
            bot.bot, "entitlements", fake_entitlements_api(error=RuntimeError("boom"))
        )
        assert run(bot.guild_has_premium(GUILD_ID)) is True
        # Cached, so the outage is throttled — but not promoted to knowledge,
        # or the fail-open value would end up deriving from itself.
        assert bot.premium_status_cache.get_last_known(GUILD_ID) is None

    def test_entitlement_event_forces_a_re_read(self, enforced, monkeypatch):
        monkeypatch.setattr(bot.bot, "entitlements", fake_entitlements_api())
        assert run(bot.guild_has_premium(GUILD_ID)) is False

        # They just bought it; the gateway event must not leave them waiting
        # out the TTL.
        monkeypatch.setattr(
            bot.bot, "entitlements", fake_entitlements_api(FakeEntitlement())
        )
        run(bot.on_entitlement_create(FakeEntitlement()))
        assert run(bot.guild_has_premium(GUILD_ID)) is True

    def test_user_scoped_entitlement_event_is_ignored(self, enforced):
        bot.premium_status_cache.set(GUILD_ID, True)
        run(bot.on_entitlement_delete(FakeEntitlement(guild_id=None)))
        assert bot.premium_status_cache.get_fresh(GUILD_ID) is True


# ---------------------------------------------------------------
# Grandfathering
# ---------------------------------------------------------------
class TestPremiumFlags:
    GRANDFATHERED = (
        bot.FEATURE_UNVERIFIED_ROLE_REMOVAL,
        bot.FEATURE_NICKNAME_SYNC,
        bot.FEATURE_CUSTOM_DM,
    )
    PREMIUM_ONLY = (bot.FEATURE_REDUCED_COOLDOWN,)

    def test_premium_allows_everything(self, enforced):
        flags = bot.PremiumFlags(premium=True, grandfathered=False)
        for feature in self.GRANDFATHERED + self.PREMIUM_ONLY:
            assert flags.allows(feature)

    def test_free_server_allows_nothing(self, enforced):
        flags = bot.PremiumFlags(premium=False, grandfathered=False)
        for feature in self.GRANDFATHERED + self.PREMIUM_ONLY:
            assert not flags.allows(feature)

    def test_grandfathered_keeps_exactly_the_promised_three(self, enforced):
        flags = bot.PremiumFlags(premium=False, grandfathered=True)
        for feature in self.GRANDFATHERED:
            assert flags.allows(feature), feature
        for feature in self.PREMIUM_ONLY:
            # Auto-verify-on-join is deliberately not grandfathered.
            assert not flags.allows(feature), feature

    def test_cooldown_window_only_for_premium(self, enforced):
        assert bot.PremiumFlags(True, False).cooldown_window() is not None
        assert bot.PremiumFlags(False, True).cooldown_window() is None


class TestIsGrandfathered:
    def test_server_from_before_the_cutover(self):
        make_server(row_id=OLD_ID)
        assert bot.is_grandfathered(GUILD_ID) is True

    def test_server_added_after_the_cutover(self):
        make_server(row_id=NEW_ID)
        assert bot.is_grandfathered(GUILD_ID) is False

    def test_the_boundary_id_is_included(self):
        make_server(row_id=LINE)
        assert bot.is_grandfathered(GUILD_ID) is True

    def test_one_past_the_boundary_is_not(self):
        make_server(row_id=LINE + 1)
        assert bot.is_grandfathered(GUILD_ID) is False

    def test_no_line_yet_means_grandfathered(self):
        """Fail open: never take automation away because we couldn't tell."""
        with bot.session_scope() as session:
            session.query(bot.PremiumGrandfatherLine).delete()
        make_server(row_id=NEW_ID)
        assert bot.is_grandfathered(GUILD_ID) is True

    def test_an_unreadable_line_also_fails_open(self, monkeypatch):
        make_server(row_id=NEW_ID)
        monkeypatch.setattr(bot, "grandfather_line", lambda: None)
        assert bot.is_grandfathered(GUILD_ID) is True

    def test_unconfigured_guild(self):
        # No servers row means nothing was ever configured here, so there is no
        # automation to preserve.
        assert bot.is_grandfathered("404") is False

    def test_db_failure_assumes_yes(self, monkeypatch):
        # Failing closed here would strip automation from an existing server
        # over a transient DB blip.
        def boom():
            raise RuntimeError("db down")

        monkeypatch.setattr(bot, "session_scope", boom)
        assert bot.is_grandfathered(GUILD_ID) is True


# ---------------------------------------------------------------
# Gated behaviour
# ---------------------------------------------------------------
@pytest.fixture
def assign_role_harness(monkeypatch):
    """Minimal guild/member so assign_role can be exercised end to end."""
    verified = SimpleNamespace(id=1, name="Verified")
    unverified = SimpleNamespace(id=2, name="Unverified")
    events = SimpleNamespace(added=[], removed=[], nicks=[], dms=[], localized=[])

    class FakeMember:
        id = 42
        roles = [unverified]

        async def add_roles(self, role):
            events.added.append(role.name)

        async def remove_roles(self, role):
            events.removed.append(role.name)

        async def edit(self, nick=None):
            events.nicks.append(nick)

        async def send(self, content):
            events.dms.append(content)

    member = FakeMember()
    guild = SimpleNamespace(
        id=int(GUILD_ID), name="Test Server", roles=[verified, unverified]
    )

    async def fake_fetch(g, user_id):
        return member

    async def fake_dm(m, g, key, instr_locale=None, **kwargs):
        events.localized.append(key)

    monkeypatch.setattr(bot.bot, "get_guild", lambda gid: guild)
    monkeypatch.setattr(bot, "fetch_member_cached", fake_fetch)
    monkeypatch.setattr(bot, "dm_localized", fake_dm)
    return events


class TestAssignRoleGating:
    def setup_server(self, row_id=OLD_ID):
        make_server(
            row_id=row_id,
            role_id="1",
            unverified_role_id="2",
            auto_nickname_change=True,
            custom_verification_requested_message="Welcome aboard!",
        )

    def test_free_server_loses_all_three(self, enforced, monkeypatch, assign_role_harness):
        self.setup_server(row_id=NEW_ID)
        monkeypatch.setattr(bot.bot, "entitlements", fake_entitlements_api())

        run_and_drain(bot.assign_role("42", True, GUILD_ID, display_name="VRCName"))

        events = assign_role_harness
        assert events.added == ["Verified"]  # core verification is untouched
        assert events.removed == []  # unverified-role removal is gated
        assert events.nicks == []  # nickname sync is gated
        assert events.dms == []  # no custom DM...
        assert "dm_role_success" in events.localized  # ...but still told they passed

    def test_grandfathered_server_keeps_all_three(
        self, enforced, monkeypatch, assign_role_harness
    ):
        self.setup_server(row_id=OLD_ID)
        monkeypatch.setattr(bot.bot, "entitlements", fake_entitlements_api())

        run_and_drain(bot.assign_role("42", True, GUILD_ID, display_name="VRCName"))

        events = assign_role_harness
        assert events.added == ["Verified"]
        assert events.removed == ["Unverified"]
        assert events.nicks == ["VRCName"]
        assert events.dms == ["Welcome aboard!"]

    def test_premium_server_keeps_all_three(
        self, enforced, monkeypatch, assign_role_harness
    ):
        self.setup_server(row_id=NEW_ID)
        monkeypatch.setattr(
            bot.bot, "entitlements", fake_entitlements_api(FakeEntitlement())
        )

        run_and_drain(bot.assign_role("42", True, GUILD_ID, display_name="VRCName"))

        events = assign_role_harness
        assert events.removed == ["Unverified"]
        assert events.nicks == ["VRCName"]
        assert events.dms == ["Welcome aboard!"]


class TestAssignRoleSkipsNeedlessLookups:
    """Premium is resolved inside the 18+ branch, after the early returns.

    Resolving it at the top of assign_role cost a REST round-trip on paths that
    cannot use the answer — including every failed verification, which is not a
    rare case.
    """

    def setup_server(self):
        make_server(row_id=NEW_ID, role_id="1", unverified_role_id="2")

    def test_a_failed_verification_never_consults_entitlements(
        self, enforced, monkeypatch, assign_role_harness
    ):
        self.setup_server()
        api, calls = counting_entitlements_api()
        monkeypatch.setattr(bot.bot, "entitlements", api)

        run_and_drain(bot.assign_role("42", False, GUILD_ID))

        assert calls == []
        assert assign_role_harness.localized == ["not_18_plus"]

    def test_a_departed_member_never_consults_entitlements(
        self, enforced, monkeypatch, assign_role_harness
    ):
        self.setup_server()
        api, calls = counting_entitlements_api()
        monkeypatch.setattr(bot.bot, "entitlements", api)

        async def gone(guild, user_id):
            return None

        monkeypatch.setattr(bot, "fetch_member_cached", gone)
        run_and_drain(bot.assign_role("42", True, GUILD_ID))
        assert calls == []

    def test_an_unconfigured_role_never_consults_entitlements(
        self, enforced, monkeypatch, assign_role_harness
    ):
        make_server(row_id=NEW_ID, role_id=None)
        api, calls = counting_entitlements_api()
        monkeypatch.setattr(bot.bot, "entitlements", api)

        run_and_drain(bot.assign_role("42", True, GUILD_ID))
        assert calls == []


class TestAutoVerifyOnJoinIsFree:
    """Auto-verify-on-join must never become a paid feature.

    Users read "the bot recognises me and gives me the role" as simply how a
    verification bot works, so gating it reads as the bot being broken rather
    than as an upsell. These tests exist to stop it drifting behind the paywall
    the next time someone reorganises the bundle.
    """

    def make_member(self):
        return SimpleNamespace(
            id=42, guild=SimpleNamespace(id=int(GUILD_ID), name="Test Server")
        )

    def prepare(self, row_id=OLD_ID):
        make_server(row_id=row_id, auto_verify_new_members=True)
        with bot.session_scope() as session:
            session.add(
                bot.User(discord_id="42", verification_status=True, vrc_user_id="usr_x")
            )

    def assigned_roles(self, monkeypatch):
        assigned = []

        async def fake_assign(*args, **kwargs):
            assigned.append(args)

        monkeypatch.setattr(bot, "assign_role", fake_assign)
        return assigned

    def test_unsubscribed_server_still_auto_verifies(self, enforced, monkeypatch):
        # Newest possible server, no entitlement, tier fully enforced.
        self.prepare(row_id=NEW_ID)
        monkeypatch.setattr(bot.bot, "entitlements", fake_entitlements_api())
        assigned = self.assigned_roles(monkeypatch)
        run(bot.on_member_join(self.make_member()))
        assert assigned == [("42", True, GUILD_ID)]

    def test_premium_server_auto_verifies(self, enforced, monkeypatch):
        self.prepare()
        monkeypatch.setattr(
            bot.bot, "entitlements", fake_entitlements_api(FakeEntitlement())
        )
        assigned = self.assigned_roles(monkeypatch)
        run(bot.on_member_join(self.make_member()))
        assert assigned == [("42", True, GUILD_ID)]

    def test_it_never_consults_entitlements_at_all(self, enforced, monkeypatch):
        # Not merely allowed — the join path must not even ask, so a Discord
        # outage can't slow down or break member joins.
        #
        # Counted rather than raised from the stub: guild_has_premium catches
        # Exception and fails open, so a raise would be swallowed and this
        # would pass even if the gate came back.
        self.prepare(row_id=NEW_ID)
        api, calls = counting_entitlements_api()
        monkeypatch.setattr(bot.bot, "entitlements", api)
        assigned = self.assigned_roles(monkeypatch)
        run(bot.on_member_join(self.make_member()))
        assert assigned == [("42", True, GUILD_ID)]
        assert calls == []

    def test_the_server_setting_still_turns_it_off(self, enforced, monkeypatch):
        # Free, but still the admin's choice to make.
        make_server(row_id=NEW_ID, auto_verify_new_members=False)
        with bot.session_scope() as session:
            session.add(
                bot.User(discord_id="42", verification_status=True, vrc_user_id="usr_x")
            )
        assigned = self.assigned_roles(monkeypatch)
        run(bot.on_member_join(self.make_member()))
        assert assigned == []


# ---------------------------------------------------------------
# Settings view
# ---------------------------------------------------------------
class TestSettingsViewLocking:
    def build(self, premium, grandfathered, page):
        return bot.PagedSettingsView(
            True,
            "en-US",
            True,
            auto_verify_available=True,
            page_index=page,
            premium=bot.PremiumFlags(premium=premium, grandfathered=grandfathered),
        )

    def test_free_server_locks_the_nickname_page(self, enforced):
        view = self.build(False, False, 0)
        assert view._page_locked() is True
        select = next(i for i in view.children if isinstance(i, discord.ui.Select))
        assert select.disabled is True
        assert any(getattr(i, "sku_id", None) == SKU_ID for i in view.children)

    def test_free_pages_are_never_locked(self, enforced):
        # Page 1 is auto-verify and page 2 is language; both are free for
        # everyone, so neither may ever render locked.
        for page in (1, 2):
            view = self.build(False, False, page)
            assert view._page_locked() is False, page
            select = next(i for i in view.children if isinstance(i, discord.ui.Select))
            assert select.disabled is False, page
            assert not any(getattr(i, "sku_id", None) for i in view.children), page

    def test_auto_verify_page_reports_the_saved_value(self, enforced):
        content = self.build(False, False, 1).render_content()
        assert "Current: Yes" in content

    def test_grandfathered_keeps_the_nickname_page_open(self, enforced):
        assert self.build(False, True, 0)._page_locked() is False

    def test_premium_server_locks_nothing(self, enforced):
        for page in (0, 1, 2):
            assert self.build(True, False, page)._page_locked() is False

    def test_paging_carries_the_flags(self, enforced):
        view = self.build(False, False, 1)
        assert view._rebuilt(0).premium.premium is False
        assert view._rebuilt(0)._page_locked() is True


# ---------------------------------------------------------------
# Cutover DM campaign
# ---------------------------------------------------------------
@pytest.fixture
def cutover_harness(monkeypatch):
    events = SimpleNamespace(dms=[], sleeps=[])
    guild = SimpleNamespace(id=int(GUILD_ID), name="Test Server", owner=None)

    async def fake_dm(member, g, key, instr_locale=None, **kwargs):
        events.dms.append(kwargs.get("server"))

    async def fake_resolve(g, owner_id):
        return SimpleNamespace(id=int(OWNER_ID))

    async def fake_sleep(seconds):
        events.sleeps.append(seconds)

    monkeypatch.setattr(bot.bot, "get_guild", lambda gid: guild)
    monkeypatch.setattr(bot, "dm_localized", fake_dm)
    monkeypatch.setattr(bot, "resolve_config_admin", fake_resolve)
    monkeypatch.setattr(bot.asyncio, "sleep", fake_sleep)
    return events


class TestCutoverCampaign:
    def test_sends_one_dm_and_marks_it(self, cutover_harness):
        make_server(row_id=OLD_ID)
        run(bot.premium_cutover_sweep_task())
        assert cutover_harness.dms == ["Test Server"]
        assert bot.load_premium_cutover_candidates(10) == []

    def test_already_sent_is_skipped(self, cutover_harness):
        make_server(row_id=OLD_ID)
        mark_notified()
        run(bot.premium_cutover_sweep_task())
        assert cutover_harness.dms == []

    def test_no_line_means_nothing_to_announce(self, cutover_harness):
        """No captured line means the tier has never been switched on.

        The DM says the tier launched and nothing changed for you; sending it
        before either is true would be a lie to every server we have.
        """
        with bot.session_scope() as session:
            session.query(bot.PremiumGrandfatherLine).delete()
        make_server(row_id=OLD_ID)
        assert bot.load_premium_cutover_candidates(10) == []
        run(bot.premium_cutover_sweep_task())
        assert cutover_harness.dms == []

    def test_servers_added_after_the_cutover_are_not_told(self, cutover_harness):
        # They never had the grandfathered features, so there is nothing to
        # announce to them.
        make_server(row_id=NEW_ID)
        run(bot.premium_cutover_sweep_task())
        assert cutover_harness.dms == []

    def test_marks_before_sending(self, monkeypatch, cutover_harness):
        """A DM that raises must not leave the row eligible for a second try."""
        make_server(row_id=OLD_ID)

        async def exploding_dm(*args, **kwargs):
            raise RuntimeError("DM blocked")

        monkeypatch.setattr(bot, "dm_localized", exploding_dm)
        run(bot.premium_cutover_sweep_task())
        assert bot.load_premium_cutover_candidates(10) == []

    def test_guild_we_left_is_retired_without_a_dm(self, monkeypatch, cutover_harness):
        make_server(row_id=OLD_ID)
        monkeypatch.setattr(bot.bot, "get_guild", lambda gid: None)
        run(bot.premium_cutover_sweep_task())
        assert cutover_harness.dms == []
        assert bot.load_premium_cutover_candidates(10) == []

    def test_respects_the_per_sweep_cap(self, cutover_harness):
        for index in range(5):
            make_server(str(index), row_id=index + 1)
        assert len(bot.load_premium_cutover_candidates(2)) == 2

    def test_spaces_sends_apart(self, monkeypatch, cutover_harness):
        for index in range(3):
            make_server(str(index), row_id=index + 1)
        monkeypatch.setattr(bot, "PREMIUM_CUTOVER_MAX_PER_SWEEP", 3)
        run(bot.premium_cutover_sweep_task())
        # One gap between each pair of sends, so a backlog trickles rather than
        # looking like a DM blast to Discord's anti-spam heuristics.
        assert (
            cutover_harness.sleeps.count(bot.PREMIUM_CUTOVER_DM_SPACING) == 2
        )

    def test_campaign_exits_when_there_is_nothing_left(self, cutover_harness):
        # No rows at all: the loop must return rather than idling forever.
        run(bot.premium_cutover_sweep_task())
        assert cutover_harness.dms == []

    def test_it_gives_up_after_repeated_failures(self, monkeypatch, cutover_harness):
        """A persistent error must not spin forever.

        The trigger watcher awaits this task, so it cannot get back to polling
        while the sweep is still running. An unbounded retry on something like
        a DB outage would wedge the watcher for the life of the process.
        """
        calls = []

        def boom(limit):
            calls.append(limit)
            raise RuntimeError("db down")

        monkeypatch.setattr(bot, "load_premium_cutover_candidates", boom)
        monkeypatch.setattr(bot, "PREMIUM_CUTOVER_MAX_FAILURES", 3)

        run(bot.premium_cutover_sweep_task())  # must return, not hang
        assert len(calls) == 3

    def test_a_recovered_sweep_resets_the_failure_count(
        self, monkeypatch, cutover_harness
    ):
        # One blip must not count toward the give-up threshold forever.
        make_server(row_id=OLD_ID)
        real = bot.load_premium_cutover_candidates
        state = {"first": True}

        def flaky(limit):
            if state["first"]:
                state["first"] = False
                raise RuntimeError("transient")
            return real(limit)

        monkeypatch.setattr(bot, "load_premium_cutover_candidates", flaky)
        monkeypatch.setattr(bot, "PREMIUM_CUTOVER_MAX_FAILURES", 2)

        run(bot.premium_cutover_sweep_task())
        assert cutover_harness.dms == ["Test Server"]


class TestGrandfatherLineCapture:
    """Issue #59: nobody may lose a feature they already had.

    The line is captured at the moment the tier goes live, so every server
    installed before then is grandfathered by construction. These pin the
    properties that guarantee it.
    """

    @pytest.fixture(autouse=True)
    def no_line_yet(self):
        with bot.session_scope() as session:
            session.query(bot.PremiumGrandfatherLine).delete()

    def test_does_nothing_while_the_tier_is_off(self):
        make_server(row_id=NEW_ID)
        assert bot.capture_grandfather_line() is None
        assert bot.grandfather_line() is None

    def test_captures_the_highest_server_id(self, enforced):
        make_server("a", row_id=10)
        make_server("b", row_id=NEW_ID)
        make_server("c", row_id=50)
        assert bot.capture_grandfather_line() == NEW_ID
        assert bot.grandfather_line() == NEW_ID

    def test_every_existing_server_ends_up_grandfathered(self, enforced):
        """The whole point: switching the tier on takes nothing from anyone."""
        for index in range(1, 6):
            make_server(f"s{index}", row_id=index * 37)
        bot.capture_grandfather_line()
        assert all(bot.is_grandfathered(f"s{i}") for i in range(1, 6))

    def test_a_server_added_later_is_not_grandfathered(self, enforced):
        make_server("early", row_id=10)
        bot.capture_grandfather_line()
        make_server("late", row_id=11)
        assert bot.is_grandfathered("early") is True
        assert bot.is_grandfathered("late") is False

    def test_the_line_never_moves_once_drawn(self, enforced):
        make_server("early", row_id=10)
        assert bot.capture_grandfather_line() == 10
        # A later boot, with more servers, must not redraw it -- otherwise
        # every restart would retroactively grandfather everyone since.
        make_server("late", row_id=999)
        assert bot.capture_grandfather_line() == 10
        assert bot.grandfather_line() == 10

    def test_no_servers_at_all_draws_the_line_at_zero(self, enforced):
        assert bot.capture_grandfather_line() == 0
        assert bot.grandfather_line() == 0

    def test_a_failed_capture_leaves_no_line(self, enforced, monkeypatch):
        def boom():
            raise RuntimeError("db down")

        monkeypatch.setattr(bot, "session_scope", boom)
        assert bot.capture_grandfather_line() is None

    def test_the_override_wins_when_set(self, enforced, monkeypatch):
        make_server(row_id=NEW_ID)
        bot.capture_grandfather_line()
        monkeypatch.setattr(bot, "PREMIUM_GRANDFATHER_MAX_ID_OVERRIDE", 5)
        assert bot.grandfather_line() == 5


class TestCutoverCompletionWarning:
    """Issue #59: the tier must not go live before everyone has been told.

    The audience for the DM and the set of servers that keep their features are
    the same predicate. These pin that they stay the same, and that flipping
    the switch early is at least loud.
    """

    def test_counts_only_the_untold_inside_the_line(self):
        make_server("told", row_id=OLD_ID)
        make_server("untold", row_id=OLD_ID + 1)
        mark_notified("told")
        assert bot.count_pending_cutover_notices() == 1

    def test_servers_past_the_line_are_not_counted(self):
        # They never had the grandfathered features, so there is nothing to
        # warn about — otherwise this would grow forever after launch and
        # become noise instead of signal.
        make_server("new", row_id=NEW_ID)
        assert bot.count_pending_cutover_notices() == 0

    def test_it_matches_the_campaign_audience(self):
        """The count and the campaign must never disagree about who is owed."""
        for index in range(4):
            make_server(str(index), row_id=index + 1)
        make_server("past", row_id=NEW_ID)
        mark_notified("0")
        assert bot.count_pending_cutover_notices() == len(
            bot.load_premium_cutover_candidates(100)
        )

    def test_silent_while_the_tier_is_off(self, caplog):
        make_server(row_id=OLD_ID)
        with caplog.at_level(logging.WARNING):
            assert bot.warn_if_cutover_incomplete() == 0
        assert "cutover DM" not in caplog.text

    def test_warns_when_enforced_with_sends_outstanding(self, enforced, caplog):
        make_server(row_id=OLD_ID)
        with caplog.at_level(logging.WARNING):
            assert bot.warn_if_cutover_incomplete() == 1
        assert "cutover DM" in caplog.text
        # The fix has to be in the message, not just the complaint.
        assert bot.PREMIUM_CUTOVER_TRIGGER_PATH in caplog.text

    def test_it_only_speaks_once_per_process(self, enforced, caplog):
        """on_ready fires on every reconnect; this must not follow it."""
        make_server(row_id=OLD_ID)
        with caplog.at_level(logging.WARNING):
            assert bot.warn_if_cutover_incomplete() == 1
            caplog.clear()
            # A reconnect, with the campaign still outstanding.
            assert bot.warn_if_cutover_incomplete() == 0
        assert caplog.text == ""

    def test_the_repeat_guard_skips_the_query_entirely(self, enforced, monkeypatch):
        make_server(row_id=OLD_ID)
        bot.warn_if_cutover_incomplete()

        def boom():
            raise AssertionError("queried the database on a reconnect")

        monkeypatch.setattr(bot, "count_pending_cutover_notices", boom)
        assert bot.warn_if_cutover_incomplete() == 0

    def test_a_finished_campaign_also_stops_re_querying(self, enforced, monkeypatch):
        """Nothing to report is still a reason not to look again.

        The line never moves, so no server can later fall inside it — a
        completed campaign stays completed, and re-checking it on every
        reconnect is pure waste on the event loop.
        """
        make_server(row_id=OLD_ID)
        mark_notified()
        assert bot.warn_if_cutover_incomplete() == 0

        def boom():
            raise AssertionError("queried the database on a reconnect")

        monkeypatch.setattr(bot, "count_pending_cutover_notices", boom)
        assert bot.warn_if_cutover_incomplete() == 0

    def test_silent_once_the_campaign_has_finished(self, enforced, caplog):
        make_server(row_id=OLD_ID)
        mark_notified()
        with caplog.at_level(logging.WARNING):
            assert bot.warn_if_cutover_incomplete() == 0
        assert "cutover DM" not in caplog.text

    def test_an_integer_server_id_still_matches_its_notice(self, monkeypatch):
        """servers.server_id comes back as an int on the deployed database.

        The notice table's column is genuinely text, so the two halves must be
        normalised in Python before they are compared. This cannot be set up
        through the real session: SQLite coerces an int written to a String
        column back to str, which is exactly why the mismatch is invisible
        locally and only bites on Postgres (`bigint = character varying`).
        So the row types are forced directly. See panel_view_key.
        """

        class FakeQuery:
            def __init__(self, rows):
                self.rows = rows

            def filter(self, *args, **kwargs):
                return self

            def all(self):
                return self.rows

        class FakeSession:
            def query(self, column):
                if column is bot.PremiumCutoverNotice.server_id:
                    # As complete_premium_cutover wrote it: text.
                    return FakeQuery([SimpleNamespace(server_id="123456789")])
                # As the deployed integer column returns it.
                return FakeQuery([SimpleNamespace(server_id=123456789)])

        @contextmanager
        def fake_scope():
            yield FakeSession()

        # Via the override, so resolving the line does not go through the fake
        # session -- otherwise it fails, the count short-circuits to 0, and
        # this test passes without ever comparing an id.
        monkeypatch.setattr(bot, "PREMIUM_GRANDFATHER_MAX_ID_OVERRIDE", 10**9)
        monkeypatch.setattr(bot, "session_scope", fake_scope)
        assert bot.count_pending_cutover_notices() == 0

    def test_a_database_failure_does_not_invent_a_number(self, monkeypatch):
        def boom():
            raise RuntimeError("db down")

        monkeypatch.setattr(bot, "session_scope", boom)
        assert bot.count_pending_cutover_notices() == 0
