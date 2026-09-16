"""Tests for the daily registered-versus-accessible server snapshot."""

import asyncio
from types import SimpleNamespace

import pytest

import bot


TODAY = bot.datetime.now(bot.timezone.utc).date()


def add_server(server_id):
    with bot.session_scope() as session:
        session.add(
            bot.Server(
                server_id=str(server_id),
                owner_id="1",
                role_id="2",
                instructions_locale="en-US",
            )
        )


@pytest.fixture(autouse=True)
def clean_db():
    with bot.session_scope() as session:
        session.query(bot.ServerMembershipDaily).delete()
        session.query(bot.PremiumSubscriptionDaily).delete()
        session.query(bot.StripeSubscription).delete()
        session.query(bot.Server).delete()
    yield
    with bot.session_scope() as session:
        session.query(bot.ServerMembershipDaily).delete()
        session.query(bot.PremiumSubscriptionDaily).delete()
        session.query(bot.StripeSubscription).delete()
        session.query(bot.Server).delete()


def snapshot():
    with bot.session_scope() as session:
        row = session.get(bot.ServerMembershipDaily, TODAY)
        return (
            row.registered_count,
            row.active_count,
            row.inaccessible_count,
        ) if row else None


def test_snapshot_counts_all_active_guilds_and_removed_registrations(monkeypatch):
    add_server(100)
    add_server(200)
    add_server(300)
    monkeypatch.setattr(
        bot,
        "bot",
        SimpleNamespace(
            guilds=[
                SimpleNamespace(id=100),
                SimpleNamespace(id=200),
                SimpleNamespace(id=400),
            ]
        ),
    )

    bot._record_server_membership_day()

    assert snapshot() == (3, 3, 1)


def test_snapshot_refreshes_the_existing_day(monkeypatch):
    add_server(100)
    add_server(200)
    guilds = [SimpleNamespace(id=100), SimpleNamespace(id=200)]
    monkeypatch.setattr(bot, "bot", SimpleNamespace(guilds=guilds))

    bot._record_server_membership_day()
    guilds.pop()
    bot._record_server_membership_day()

    assert snapshot() == (2, 1, 1)


def test_snapshot_task_records_after_waiting_for_the_next_utc_day(monkeypatch):
    recorded = []
    sleeps = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) > 1:
            raise asyncio.CancelledError

    monkeypatch.setattr(bot, "_record_server_membership_day", lambda: recorded.append(True))

    async def fake_premium_record():
        recorded.append("premium")

    monkeypatch.setattr(bot, "_record_premium_subscription_day", fake_premium_record)
    monkeypatch.setattr(bot.asyncio, "sleep", fake_sleep)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(bot.server_membership_snapshot_task())

    assert recorded == ["premium", True, "premium"]
    assert sleeps[0] > 0


def add_stripe_subscription(sub_id, server_id, status, days_left):
    with bot.session_scope() as session:
        session.add(
            bot.StripeSubscription(
                stripe_subscription_id=sub_id,
                server_id=str(server_id),
                stripe_customer_id="cus_1",
                price_id="price_1",
                status=status,
                current_period_end=bot.datetime.now(bot.timezone.utc)
                + bot.timedelta(days=days_left),
                cancel_at_period_end=False,
                last_event_created=bot.datetime.now(bot.timezone.utc),
            )
        )


def premium_snapshot():
    with bot.session_scope() as session:
        row = session.get(bot.PremiumSubscriptionDaily, TODAY)
        return (row.discord_count, row.stripe_count, row.total_count) if row else None


def use_entitled_guilds(monkeypatch, ids):
    async def fake_entitled_guild_ids():
        return ids

    monkeypatch.setattr(bot, "entitled_guild_ids", fake_entitled_guild_ids)
    monkeypatch.setattr(bot, "STRIPE_ENABLED", True)


def test_premium_snapshot_counts_both_sources_including_canceled_but_running(monkeypatch):
    use_entitled_guilds(monkeypatch, frozenset({"100", "200"}))
    add_stripe_subscription("sub_active", 200, "active", 10)
    add_stripe_subscription("sub_canceled", 300, "canceled", 5)
    add_stripe_subscription("sub_expired", 400, "canceled", -1)
    add_stripe_subscription("sub_unpaid", 500, "unpaid", 10)

    asyncio.run(bot._record_premium_subscription_day())

    assert premium_snapshot() == (2, 2, 3)


def test_premium_snapshot_skips_the_day_when_discord_cannot_answer(monkeypatch):
    use_entitled_guilds(monkeypatch, None)
    add_stripe_subscription("sub_active", 200, "active", 10)

    asyncio.run(bot._record_premium_subscription_day())

    assert premium_snapshot() is None


def test_premium_snapshot_refreshes_the_existing_day(monkeypatch):
    use_entitled_guilds(monkeypatch, frozenset({"100"}))
    asyncio.run(bot._record_premium_subscription_day())
    use_entitled_guilds(monkeypatch, frozenset({"100", "200"}))
    asyncio.run(bot._record_premium_subscription_day())

    assert premium_snapshot() == (2, 0, 2)


def test_snapshot_task_records_premium_before_waiting_not_in_on_ready(monkeypatch):
    # on_ready runs on every reconnect and starts every other background task;
    # the premium snapshot's Discord listing must not hold those up.
    events = []

    async def fake_premium_record():
        events.append("premium")

    async def fake_sleep(seconds):
        events.append("sleep")
        raise asyncio.CancelledError

    monkeypatch.setattr(bot, "_record_premium_subscription_day", fake_premium_record)
    monkeypatch.setattr(bot.asyncio, "sleep", fake_sleep)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(bot.server_membership_snapshot_task())

    assert events == ["premium", "sleep"]
