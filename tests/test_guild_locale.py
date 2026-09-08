"""Tests for the per-guild locale record (issue #132)."""

from types import SimpleNamespace

import discord
import pytest

import bot


TODAY = bot.datetime.now(bot.timezone.utc).date()


@pytest.fixture(autouse=True)
def clean_db():
    with bot.session_scope() as session:
        session.query(bot.GuildLocale).delete()
        session.query(bot.ServerMembershipDaily).delete()
        session.query(bot.Server).delete()
    yield
    with bot.session_scope() as session:
        session.query(bot.GuildLocale).delete()
        session.query(bot.ServerMembershipDaily).delete()
        session.query(bot.Server).delete()


def set_guilds(monkeypatch, *guilds):
    monkeypatch.setattr(bot, "bot", SimpleNamespace(guilds=list(guilds)))


def guild(guild_id, locale="en-US"):
    return SimpleNamespace(id=guild_id, preferred_locale=locale)


def locales():
    with bot.session_scope() as session:
        return {
            row.server_id: (row.preferred_locale, row.last_seen)
            for row in session.query(bot.GuildLocale)
        }


def test_records_the_locale_of_every_live_guild(monkeypatch):
    set_guilds(monkeypatch, guild(100, "en-US"), guild(200, "pt-BR"))

    bot._record_guild_locales()

    assert locales() == {
        "100": ("en-US", TODAY),
        "200": ("pt-BR", TODAY),
    }


def test_a_changed_locale_overwrites_rather_than_appends(monkeypatch):
    set_guilds(monkeypatch, guild(100, "en-US"))
    bot._record_guild_locales()

    set_guilds(monkeypatch, guild(100, "ja"))
    bot._record_guild_locales()

    assert locales() == {"100": ("ja", TODAY)}


def test_a_departed_guild_keeps_its_last_seen_row(monkeypatch):
    """Rows survive removal, as `servers` rows do.

    A breakdown that wants only live guilds filters on `last_seen`; it must
    not be handed a table that silently forgot the guild instead.
    """
    set_guilds(monkeypatch, guild(100, "de"), guild(200, "fr"))
    bot._record_guild_locales()

    set_guilds(monkeypatch, guild(100, "de"))
    bot._record_guild_locales()

    assert locales()["200"] == ("fr", TODAY)


def test_a_discord_locale_enum_is_stored_as_its_string_form(monkeypatch):
    """Regression: `preferred_locale` is an enum, not a string.

    Storing the enum's repr would give a table full of `Locale.german` that no
    locale filter matches. `dm_localized` was bitten by the same thing.
    """
    set_guilds(monkeypatch, guild(100, discord.Locale.german))

    bot._record_guild_locales()

    assert locales() == {"100": ("de", TODAY)}


def test_a_guild_with_no_locale_is_recorded_as_absent(monkeypatch):
    """Never invent en-US for a guild we did not observe one on."""
    set_guilds(monkeypatch, SimpleNamespace(id=100), guild(200, "ko"))

    bot._record_guild_locales()

    assert locales() == {"200": ("ko", TODAY)}


def test_an_unavailable_guild_does_not_overwrite_a_known_locale(monkeypatch):
    """Discord outage regression.

    discord.py fills `preferred_locale` with a default `en-US` whenever the
    guild payload omits it, and an unavailable guild's payload carries nothing
    but an id and the unavailable flag. That default is a fabrication, not an
    observation, and trusting it lets an outage quietly rewrite the whole
    table to English -- the precise skew this table exists not to have.
    """
    set_guilds(monkeypatch, guild(100, "pt-BR"))
    bot._record_guild_locales()

    set_guilds(
        monkeypatch,
        SimpleNamespace(
            id=100,
            preferred_locale=discord.Locale.american_english,
            unavailable=True,
        ),
    )
    bot._record_guild_locales()

    assert locales() == {"100": ("pt-BR", TODAY)}


def test_an_unavailable_guild_is_not_recorded_at_all(monkeypatch):
    """A guild we have never actually seen must not enter as an en-US row."""
    set_guilds(
        monkeypatch,
        SimpleNamespace(
            id=100,
            preferred_locale=discord.Locale.american_english,
            unavailable=True,
        ),
        guild(200, "fi"),
    )

    bot._record_guild_locales()

    assert locales() == {"200": ("fi", TODAY)}


def test_the_daily_sweep_records_locales_too(monkeypatch):
    """The point of riding the existing sweep: no second task to schedule."""
    set_guilds(monkeypatch, guild(100, "es-ES"))

    bot._record_server_membership_day()

    assert locales() == {"100": ("es-ES", TODAY)}


def test_a_locale_failure_does_not_lose_the_membership_snapshot(monkeypatch):
    set_guilds(monkeypatch, guild(100, "en-US"))

    def boom():
        raise RuntimeError("locale table is on fire")

    monkeypatch.setattr(bot, "_record_guild_locales", boom)

    with pytest.raises(RuntimeError):
        bot._record_server_membership_day()

    with bot.session_scope() as session:
        assert session.get(bot.ServerMembershipDaily, TODAY) is not None


def test_a_broken_query_is_swallowed_rather_than_killing_the_sweep(monkeypatch):
    set_guilds(monkeypatch, guild(100, "en-US"))
    monkeypatch.setattr(
        bot, "session_scope", lambda: (_ for _ in ()).throw(RuntimeError("no db"))
    )

    bot._record_guild_locales()  # must not raise


class TestTheColumnsStayGuildLevel:
    def test_a_guild_id_a_language_and_a_date(self):
        """This test exists to be annoying.

        `guild_locale` is a table about servers, not about people, and that is
        what makes it safe to keep indefinitely for a product whose job is to
        answer "is this person over 18" and then forget. Adding a member id or
        a per-person timestamp here should require deleting an assertion that
        says so out loud.
        """
        columns = {column.name for column in bot.GuildLocale.__table__.columns}
        assert columns == {"server_id", "preferred_locale", "last_seen"}

    def test_one_row_per_guild(self):
        primary = {column.name for column in bot.GuildLocale.__table__.primary_key}
        assert primary == {"server_id"}
