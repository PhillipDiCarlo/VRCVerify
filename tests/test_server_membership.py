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
        session.query(bot.Server).delete()
    yield
    with bot.session_scope() as session:
        session.query(bot.ServerMembershipDaily).delete()
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
    monkeypatch.setattr(bot.asyncio, "sleep", fake_sleep)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(bot.server_membership_snapshot_task())

    assert recorded == [True]
    assert sleeps[0] > 0
