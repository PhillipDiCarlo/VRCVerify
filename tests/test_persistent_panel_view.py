"""Unit tests for the persistent instruction panel view (issue #14, tier 2).

Panel buttons used to get a random custom_id per process, so every posted panel
had to be re-edited on boot just to hand out ids the new process recognised.
They now carry fixed, versioned ids and are registered once via add_view(), so
a restart only has to touch panels that predate the current version.
"""

import asyncio
from types import SimpleNamespace

import discord
import pytest

import bot

GUILD_ID = "123456789"


def run(coro):
    return asyncio.run(coro)


def interactive_buttons(view):
    """The two dispatchable buttons (the donate button is a link, so it isn't)."""
    return [c for c in view.children if c.style is not discord.ButtonStyle.link]


def make_server(server_id=GUILD_ID, channel_id="222", message_id="111", **overrides):
    fields = dict(
        server_id=server_id,
        owner_id="42",
        role_id="1",
        instructions_locale="en-US",
        instructions_channel_id=channel_id,
        instructions_message_id=message_id,
    )
    fields.update(overrides)
    with bot.session_scope() as session:
        session.add(bot.Server(**fields))


def mark_migrated(server_id, version=None):
    bot.record_panel_view_version(
        server_id, bot.INSTRUCTIONS_VIEW_VERSION if version is None else version
    )


@pytest.fixture
def clean_db():
    def wipe():
        with bot.session_scope() as session:
            session.query(bot.Server).delete()
            session.query(bot.InstructionPanelView).delete()

    wipe()
    yield
    wipe()


# ---------------------------------------------------------------
# The view itself
# ---------------------------------------------------------------
class TestPersistentView:
    def test_view_is_persistent(self):
        assert bot.VRCVerifyInstructionView(locale="en-US").is_persistent()

    def test_buttons_carry_the_versioned_custom_ids(self):
        ids = [b.custom_id for b in interactive_buttons(bot.VRCVerifyInstructionView("en-US"))]
        assert ids == [
            bot.BEGIN_VERIFICATION_CUSTOM_ID,
            bot.UPDATE_NICKNAME_CUSTOM_ID,
        ]

    def test_custom_ids_embed_the_version(self):
        suffix = f"v{bot.INSTRUCTIONS_VIEW_VERSION}"
        assert bot.BEGIN_VERIFICATION_CUSTOM_ID.endswith(suffix)
        assert bot.UPDATE_NICKNAME_CUSTOM_ID.endswith(suffix)

    def test_ids_are_identical_across_instances(self):
        # This is the whole point: a new process must produce the same ids as
        # the one that originally posted the panel.
        first = [b.custom_id for b in interactive_buttons(bot.VRCVerifyInstructionView("en-US"))]
        second = [b.custom_id for b in interactive_buttons(bot.VRCVerifyInstructionView("en-US"))]
        assert first == second

    def test_ids_do_not_vary_by_locale(self):
        en = [b.custom_id for b in interactive_buttons(bot.VRCVerifyInstructionView("en-US"))]
        de = [b.custom_id for b in interactive_buttons(bot.VRCVerifyInstructionView("de"))]
        assert en == de

    def test_labels_still_localize(self):
        en = [b.label for b in interactive_buttons(bot.VRCVerifyInstructionView("en-US"))]
        de = [b.label for b in interactive_buttons(bot.VRCVerifyInstructionView("de"))]
        assert en != de

    def test_donate_link_button_has_no_custom_id(self):
        # Discord rejects custom_id on link buttons; it must stay unset.
        link = [c for c in bot.VRCVerifyInstructionView("en-US").children
                if c.style is discord.ButtonStyle.link]
        assert len(link) == 1
        assert link[0].custom_id is None

    def test_add_view_accepts_it(self):
        client = discord.Client(intents=discord.Intents.none())
        client.add_view(bot.VRCVerifyInstructionView(locale="en-US"))
        store = client._connection._view_store._views
        # Registered under the None key, which dispatch falls back to for any
        # message id — that is what makes one registration cover every panel.
        assert (2, bot.BEGIN_VERIFICATION_CUSTOM_ID) in store[None]
        assert (2, bot.UPDATE_NICKNAME_CUSTOM_ID) in store[None]

    def test_a_never_seen_message_resolves_to_the_view(self):
        client = discord.Client(intents=discord.Intents.none())
        client.add_view(bot.VRCVerifyInstructionView(locale="en-US"))
        store = client._connection._view_store
        assert store._views.get(999999999) is None  # no per-message registration
        item = store._views[None][(2, bot.BEGIN_VERIFICATION_CUSTOM_ID)]
        assert item.view is not None


# ---------------------------------------------------------------
# Version bookkeeping
# ---------------------------------------------------------------
class TestVersionRecording:
    def test_unrecorded_panel_is_version_zero(self, clean_db):
        make_server()
        panel = bot.load_instruction_panels()[0]
        assert panel["view_version"] == 0

    def test_recording_marks_the_current_version(self, clean_db):
        make_server()
        mark_migrated(GUILD_ID)
        assert bot.load_instruction_panels()[0]["view_version"] == bot.INSTRUCTIONS_VIEW_VERSION

    def test_recording_is_idempotent(self, clean_db):
        make_server()
        mark_migrated(GUILD_ID)
        mark_migrated(GUILD_ID)
        with bot.session_scope() as session:
            assert session.query(bot.InstructionPanelView).count() == 1

    def test_forgetting_a_panel_clears_its_version(self, clean_db):
        make_server()
        mark_migrated(GUILD_ID)

        bot.forget_instruction_panel(GUILD_ID)

        with bot.session_scope() as session:
            assert session.query(bot.InstructionPanelView).count() == 0

    def test_stale_only_skips_current_panels(self, clean_db):
        make_server("a", channel_id="1", message_id="10")
        make_server("b", channel_id="2", message_id="20")
        mark_migrated("a")

        stale = bot.load_instruction_panels(stale_only=True)

        assert [p["server_id"] for p in stale] == ["b"]

    def test_stale_only_includes_older_versions(self, clean_db):
        make_server()
        mark_migrated(GUILD_ID, version=bot.INSTRUCTIONS_VIEW_VERSION - 1)

        stale = bot.load_instruction_panels(stale_only=True)

        assert [p["server_id"] for p in stale] == [GUILD_ID]

    def test_full_load_ignores_version(self, clean_db):
        make_server("a", channel_id="1", message_id="10")
        make_server("b", channel_id="2", message_id="20")
        mark_migrated("a")

        assert len(bot.load_instruction_panels()) == 2


# ---------------------------------------------------------------
# Guild id type coercion
# ---------------------------------------------------------------
class TestServerIdCoercion:
    """`servers.server_id` is declared String but the deployed Postgres column
    is an integer type, so SQLAlchemy hands back ints. Against the text column
    in instruction_panel_views that made Postgres reject `varchar = bigint` on
    write, and made the version lookup silently miss on read. Everything
    touching that table has to normalise first.
    """

    def test_key_normalises_ints(self):
        assert bot.panel_view_key(123) == "123"
        assert bot.panel_view_key("123") == "123"

    def test_recording_with_an_int_is_found_by_a_str_lookup(self, clean_db):
        make_server(GUILD_ID)  # stored as str, as the model declares

        bot.record_panel_view_version(int(GUILD_ID))

        # The read side must match despite the write coming in as an int.
        assert bot.load_instruction_panels(stale_only=True) == []

    def test_recording_with_a_str_is_forgotten_by_an_int(self, clean_db):
        make_server(GUILD_ID)
        bot.record_panel_view_version(GUILD_ID)

        bot.forget_panel_view_version(int(GUILD_ID))

        with bot.session_scope() as session:
            assert session.query(bot.InstructionPanelView).count() == 0

    def test_int_and_str_never_create_two_rows(self, clean_db):
        bot.record_panel_view_version(int(GUILD_ID))
        bot.record_panel_view_version(GUILD_ID)

        with bot.session_scope() as session:
            assert session.query(bot.InstructionPanelView).count() == 1

    def test_version_is_visible_when_ids_differ_in_type(self, clean_db):
        make_server(GUILD_ID)
        bot.record_panel_view_version(int(GUILD_ID))

        panel = bot.load_instruction_panels()[0]

        assert panel["view_version"] == bot.INSTRUCTIONS_VIEW_VERSION

    @pytest.mark.parametrize(
        "call",
        [
            pytest.param(lambda: bot.record_panel_view_version(GUILD_ID), id="record"),
            pytest.param(lambda: bot.forget_panel_view_version(GUILD_ID), id="forget"),
            pytest.param(lambda: bot.load_instruction_panels(), id="load"),
        ],
    )
    def test_every_table_access_normalises_first(self, clean_db, monkeypatch, call):
        # The round-trip tests above cannot fail on SQLite, whose TEXT affinity
        # quietly coerces ints on the way in. Postgres does not, so the real
        # invariant to protect is that nothing reaches this table un-normalised.
        make_server(GUILD_ID)
        seen = []
        real = bot.panel_view_key
        monkeypatch.setattr(bot, "panel_view_key", lambda v: (seen.append(v), real(v))[1])

        call()

        assert seen, "guild id reached instruction_panel_views without normalising"
