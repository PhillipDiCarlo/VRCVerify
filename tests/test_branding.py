"""Unit tests for the premium branded instructions panel (issue #58).

Two properties carry most of the weight here.

The first is that the posted panel and the refreshed panel must never disagree.
They are built by the same function from the same resolved style, and the embed
builder is a pure function of its arguments so that stays provable rather than
merely intended.

The second is that a lapsed subscription reverts to the default look. The panel
is a persisted Discord message, so "revert" means something has to re-edit it —
the styling is resolved at edit time, never cached alongside the panel.
"""

import asyncio
from types import SimpleNamespace

import discord
import pytest

import bot

GUILD_ID = "987654321"
OWNER_ID = "77"
CHANNEL_ID = "555000222"
MESSAGE_ID = "555000333"
SKU_ID = 555000111
OLD_ID = 100
NEW_ID = 5000
LINE = 820

BRAND = 0x5865F2
ICON_URL = "https://cdn.discordapp.com/icons/987654321/abc.png"


def run(coro):
    return asyncio.run(coro)


def make_server(server_id=GUILD_ID, row_id=OLD_ID, with_panel=True, **overrides):
    fields = dict(
        id=row_id,
        server_id=server_id,
        owner_id=OWNER_ID,
        role_id="1",
        instructions_locale="en-US",
    )
    if with_panel:
        fields["instructions_channel_id"] = CHANNEL_ID
        fields["instructions_message_id"] = MESSAGE_ID
    fields.update(overrides)
    with bot.session_scope() as session:
        session.add(bot.Server(**fields))


def set_branding(server_id=GUILD_ID, embed_color=BRAND, show_icon=True):
    bot.save_panel_branding(server_id, embed_color, show_icon)


def draw_line(max_server_id=LINE):
    with bot.session_scope() as session:
        session.query(bot.PremiumGrandfatherLine).delete()
        session.add(bot.PremiumGrandfatherLine(id=1, max_server_id=max_server_id))


class FakeGuild:
    """Just enough guild for the thumbnail lookup."""

    def __init__(self, icon_url=ICON_URL):
        self.id = int(GUILD_ID)
        self.icon = SimpleNamespace(url=icon_url) if icon_url else None


@pytest.fixture(autouse=True)
def clean_db():
    def wipe():
        with bot.session_scope() as session:
            session.query(bot.Server).delete()
            session.query(bot.User).delete()
            session.query(bot.InstructionPanelBranding).delete()
            session.query(bot.PremiumGrandfatherLine).delete()

    wipe()
    bot.premium_status_cache.clear()
    draw_line()
    yield
    wipe()
    bot.premium_status_cache.clear()


@pytest.fixture
def enforced(monkeypatch):
    monkeypatch.setattr(bot, "PREMIUM_SKU_ID", SKU_ID)
    monkeypatch.setattr(bot, "PREMIUM_ENFORCED", True)
    bot.premium_status_cache.clear()


@pytest.fixture
def premium(monkeypatch, enforced):
    """Guild resolves as subscribed without any Discord round trip."""

    async def yes(guild_id):
        return True

    monkeypatch.setattr(bot, "guild_has_premium", yes)
    bot.premium_status_cache.clear()


# ---------------------------------------------------------------
# Hex parsing
# ---------------------------------------------------------------
class TestParseHexColor:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("#5865F2", 0x5865F2),
            ("5865F2", 0x5865F2),
            ("0x5865F2", 0x5865F2),
            ("#5865f2", 0x5865F2),
            ("  #5865F2  ", 0x5865F2),
            ("#58F", 0x5588FF),
            ("#FFFFFF", 0xFFFFFF),
        ],
    )
    def test_accepted_forms(self, raw, expected):
        assert bot.parse_hex_color(raw) == expected

    @pytest.mark.parametrize(
        "raw",
        ["", "   ", "#12345", "#1234567", "nope", "#GGGGGG", "12 34 56", None],
    )
    def test_rejected_forms(self, raw):
        assert bot.parse_hex_color(raw) is None

    def test_black_is_nudged_so_discord_renders_it(self):
        """Discord treats 0 as "no colour" and shows the default grey sidebar.

        A server asking for black would look like it had been ignored, so the
        value is moved to the darkest colour Discord will actually render.
        """
        assert bot.parse_hex_color("#000000") == bot.NEAREST_RENDERABLE_BLACK
        assert bot.parse_hex_color("#000000") != 0


# ---------------------------------------------------------------
# The embed builder stays a pure function
# ---------------------------------------------------------------
class TestBuildInstructionsEmbed:
    def test_defaults_to_blue_with_no_thumbnail(self):
        embed = bot.build_instructions_embed("en-US")
        assert embed.color == bot.DEFAULT_PANEL_COLOR
        assert embed.thumbnail.url is None

    def test_applies_colour_and_thumbnail(self):
        embed = bot.build_instructions_embed(
            "en-US", discord.Color(BRAND), ICON_URL
        )
        assert embed.color == discord.Color(BRAND)
        assert embed.thumbnail.url == ICON_URL

    def test_the_instruction_copy_never_changes(self):
        """Styling is customisable; the wording is not, deliberately."""
        plain = bot.build_instructions_embed("en-US")
        styled = bot.build_instructions_embed(
            "en-US", discord.Color(BRAND), ICON_URL
        )
        assert styled.title == plain.title
        assert styled.description == plain.description
        assert [(f.name, f.value) for f in styled.fields] == [
            (f.name, f.value) for f in plain.fields
        ]

    def test_it_reads_no_database(self, monkeypatch):
        """A pure builder is what keeps the two call sites honest."""

        def boom():
            raise AssertionError("build_instructions_embed touched the database")

        monkeypatch.setattr(bot, "session_scope", boom)
        bot.build_instructions_embed("en-US", discord.Color(BRAND), ICON_URL)


# ---------------------------------------------------------------
# Storage
# ---------------------------------------------------------------
class TestStorage:
    def test_styling_that_asks_for_nothing_stores_no_row(self):
        """The settings view saves every page at once.

        A premium server that only changed its nickname setting must not end up
        with a row, because a row's existence costs an entitlement lookup on
        every fleet refresh for styling that is the default anyway.
        """
        make_server()
        set_branding(embed_color=None, show_icon=False)
        assert bot.load_panel_branding(GUILD_ID) is None
        with bot.session_scope() as session:
            assert session.query(bot.InstructionPanelBranding).count() == 0

    def test_clearing_a_colour_removes_the_row(self):
        make_server()
        set_branding(embed_color=BRAND, show_icon=False)
        assert bot.load_panel_branding(GUILD_ID) == (BRAND, False)
        set_branding(embed_color=None, show_icon=False)
        assert bot.load_panel_branding(GUILD_ID) is None

    def test_icon_alone_is_still_worth_a_row(self):
        make_server()
        set_branding(embed_color=None, show_icon=True)
        assert bot.load_panel_branding(GUILD_ID) == (None, True)

    def test_saving_twice_updates_rather_than_duplicates(self):
        make_server()
        set_branding(embed_color=BRAND, show_icon=True)
        set_branding(embed_color=0x00FF00, show_icon=False)
        assert bot.load_panel_branding(GUILD_ID) == (0x00FF00, False)
        with bot.session_scope() as session:
            assert session.query(bot.InstructionPanelBranding).count() == 1

    def test_a_read_failure_is_not_reported_as_no_branding(self, monkeypatch):
        """"Couldn't tell" has to stay distinct from "definitely nothing"."""

        def boom():
            raise RuntimeError("db down")

        monkeypatch.setattr(bot, "session_scope", boom)
        assert bot.load_panel_branding(GUILD_ID) is bot.BRANDING_UNREADABLE
        assert bot.load_panel_branding(GUILD_ID) is not None


# ---------------------------------------------------------------
# Style resolution and gating
# ---------------------------------------------------------------
class TestPanelStyle:
    def test_premium_gets_its_colour_and_icon(self):
        style = bot.panel_style((BRAND, True), FakeGuild(), allowed=True)
        assert style == (discord.Color(BRAND), ICON_URL)

    def test_not_allowed_reverts_to_the_default_look(self):
        style = bot.panel_style((BRAND, True), FakeGuild(), allowed=False)
        assert style == (bot.DEFAULT_PANEL_COLOR, None)

    def test_no_branding_row_is_the_default_look(self):
        style = bot.panel_style(None, FakeGuild(), allowed=True)
        assert style == (bot.DEFAULT_PANEL_COLOR, None)

    def test_icon_off_keeps_the_colour(self):
        style = bot.panel_style((BRAND, False), FakeGuild(), allowed=True)
        assert style == (discord.Color(BRAND), None)

    def test_a_guild_with_no_icon_yields_no_thumbnail(self):
        """Discord rejects an empty thumbnail url, so this must not be set."""
        style = bot.panel_style((BRAND, True), FakeGuild(icon_url=None), allowed=True)
        assert style == (discord.Color(BRAND), None)

    def test_a_missing_guild_yields_no_thumbnail(self):
        style = bot.panel_style((BRAND, True), None, allowed=True)
        assert style == (discord.Color(BRAND), None)

    def test_no_stored_colour_keeps_the_default_blue(self):
        style = bot.panel_style((None, True), FakeGuild(), allowed=True)
        assert style == (bot.DEFAULT_PANEL_COLOR, ICON_URL)


class TestResolvePanelStyle:
    def test_ungated_while_the_tier_is_off(self):
        make_server()
        set_branding()
        style = run(bot.resolve_panel_style(GUILD_ID, FakeGuild()))
        assert style == (discord.Color(BRAND), ICON_URL)

    def test_premium_guild_keeps_its_branding(self, premium):
        make_server(row_id=NEW_ID)
        set_branding()
        style = run(bot.resolve_panel_style(GUILD_ID, FakeGuild()))
        assert style == (discord.Color(BRAND), ICON_URL)

    def test_free_guild_reverts(self, enforced, monkeypatch):
        async def no(guild_id):
            return False

        monkeypatch.setattr(bot, "guild_has_premium", no)
        make_server(row_id=NEW_ID)
        set_branding()
        style = run(bot.resolve_panel_style(GUILD_ID, FakeGuild()))
        assert style == (bot.DEFAULT_PANEL_COLOR, None)

    def test_grandfathered_does_not_get_it(self, enforced, monkeypatch):
        """New feature, so nobody is losing anything by not being included."""

        async def no(guild_id):
            return False

        monkeypatch.setattr(bot, "guild_has_premium", no)
        make_server(row_id=OLD_ID)  # inside the grandfather line
        set_branding()
        assert bot.is_grandfathered(GUILD_ID) is True
        style = run(bot.resolve_panel_style(GUILD_ID, FakeGuild()))
        assert style == (bot.DEFAULT_PANEL_COLOR, None)

    def test_it_is_not_a_grandfathered_feature(self):
        assert bot.FEATURE_BRANDED_PANEL not in bot.GRANDFATHERED_FEATURES

    def test_unreadable_branding_declines_to_answer(self, enforced, monkeypatch):
        monkeypatch.setattr(
            bot, "load_panel_branding", lambda gid: bot.BRANDING_UNREADABLE
        )
        assert run(bot.resolve_panel_style(GUILD_ID, FakeGuild())) is None

    def test_no_branding_row_skips_the_entitlement_read(self, enforced, monkeypatch):
        """The fleet refresh would otherwise be one lookup per panel."""

        async def boom(guild_id):
            raise AssertionError("resolved entitlements for an unbranded guild")

        monkeypatch.setattr(bot, "guild_has_premium", boom)
        make_server(row_id=NEW_ID)
        style = run(bot.resolve_panel_style(GUILD_ID, FakeGuild()))
        assert style == (bot.DEFAULT_PANEL_COLOR, None)


# ---------------------------------------------------------------
# The panel is a persisted message, so styling has to be re-applied
# ---------------------------------------------------------------
class PanelRecorder:
    """Stands in for the Discord HTTP layer and records panel edits."""

    def __init__(self):
        self.edits = []

    def install(self, monkeypatch):
        monkeypatch.setattr(
            bot.bot, "get_partial_messageable", self._messageable
        )
        monkeypatch.setattr(bot.bot, "get_guild", lambda gid: FakeGuild())
        return self

    def _messageable(self, channel_id, **kwargs):
        return SimpleNamespace(
            get_partial_message=lambda message_id: SimpleNamespace(edit=self._edit)
        )

    async def _edit(self, **payload):
        self.edits.append(payload)

    @property
    def last_embed(self):
        return self.edits[-1]["embed"]


class TestRestylePanel:
    def test_it_re_edits_the_panel_with_current_styling(self, monkeypatch, premium):
        make_server(row_id=NEW_ID)
        set_branding()
        rec = PanelRecorder().install(monkeypatch)

        assert run(bot.restyle_instruction_panel(GUILD_ID)) == "ok"

        assert len(rec.edits) == 1
        assert rec.last_embed.color == discord.Color(BRAND)
        assert rec.last_embed.thumbnail.url == ICON_URL

    def test_a_lapse_reverts_the_live_panel(self, monkeypatch, enforced):
        """The whole point of resolving style at edit time."""

        async def no(guild_id):
            return False

        monkeypatch.setattr(bot, "guild_has_premium", no)
        make_server(row_id=NEW_ID)
        set_branding()
        rec = PanelRecorder().install(monkeypatch)

        run(bot.restyle_instruction_panel(GUILD_ID))

        assert rec.last_embed.color == bot.DEFAULT_PANEL_COLOR
        assert rec.last_embed.thumbnail.url is None

    def test_the_stored_choice_survives_the_lapse(self, monkeypatch, enforced):
        """Reverting the look must not discard what the admin picked."""

        async def no(guild_id):
            return False

        monkeypatch.setattr(bot, "guild_has_premium", no)
        make_server(row_id=NEW_ID)
        set_branding()
        PanelRecorder().install(monkeypatch)

        run(bot.restyle_instruction_panel(GUILD_ID))

        assert bot.load_panel_branding(GUILD_ID) == (BRAND, True)

    def test_an_unreadable_table_leaves_the_panel_untouched(
        self, monkeypatch, premium
    ):
        """A database blip must not restyle a paying server back to default.

        The view is still refreshed — that is what the pass is for — but no
        embed goes in the payload, so the panel keeps the look it has.
        """
        make_server(row_id=NEW_ID)
        set_branding()
        rec = PanelRecorder().install(monkeypatch)
        monkeypatch.setattr(
            bot, "load_panel_branding", lambda gid: bot.BRANDING_UNREADABLE
        )

        run(bot.restyle_instruction_panel(GUILD_ID))

        assert len(rec.edits) == 1
        assert "embed" not in rec.edits[0]
        assert isinstance(rec.edits[0]["view"], bot.VRCVerifyInstructionView)

    def test_a_guild_with_no_panel_is_a_no_op(self, monkeypatch, premium):
        make_server(row_id=NEW_ID, with_panel=False)
        set_branding()
        rec = PanelRecorder().install(monkeypatch)

        assert run(bot.restyle_instruction_panel(GUILD_ID)) == "no_panel"
        assert rec.edits == []

    def test_it_never_raises(self, monkeypatch, premium):
        """It runs after a save that already succeeded; it must not undo that."""
        make_server(row_id=NEW_ID)
        set_branding()

        async def boom(entry, rebuild_embed):
            raise RuntimeError("discord is down")

        monkeypatch.setattr(bot, "probe_instruction_panel", boom)
        assert run(bot.restyle_instruction_panel(GUILD_ID)) == "error"


class TestBothCallSitesAgree:
    def test_posted_and_refreshed_panels_match(self, monkeypatch, premium):
        """A refreshed panel must look exactly like a freshly posted one.

        They go through one builder from one resolved style, so this is the
        test that the issue's "keep it that way" requirement still holds.
        """
        make_server(row_id=NEW_ID)
        set_branding()
        rec = PanelRecorder().install(monkeypatch)

        run(bot.restyle_instruction_panel(GUILD_ID))
        refreshed = rec.last_embed

        # What /vrcverify_instructions builds, with the same resolved style.
        branding = bot.load_panel_branding(GUILD_ID)
        color, icon = bot.panel_style(branding, FakeGuild(), allowed=True)
        posted = bot.build_instructions_embed("en-US", color, icon)

        assert refreshed.to_dict() == posted.to_dict()


# ---------------------------------------------------------------
# Settings page
# ---------------------------------------------------------------
class TestSettingsBrandingPage:
    PAGE = bot.SETTINGS_LAST_PAGE

    def build(self, premium_flag, grandfathered, page=None, **kwargs):
        return bot.PagedSettingsView(
            True,
            "en-US",
            True,
            auto_verify_available=True,
            page_index=self.PAGE if page is None else page,
            premium=bot.PremiumFlags(
                premium=premium_flag, grandfathered=grandfathered
            ),
            **kwargs,
        )

    def test_free_server_sees_it_locked_with_an_upgrade_button(self, enforced):
        view = self.build(False, False)
        assert view._page_locked() is True
        select = next(i for i in view.children if isinstance(i, discord.ui.Select))
        assert select.disabled is True
        assert any(getattr(i, "sku_id", None) == SKU_ID for i in view.children)

    def test_grandfathered_does_not_unlock_it(self, enforced):
        """Not in GRANDFATHERED_FEATURES, so being old is not enough."""
        assert self.build(False, True)._page_locked() is True

    def test_premium_unlocks_it(self, enforced):
        view = self.build(True, False)
        assert view._page_locked() is False
        select = next(i for i in view.children if isinstance(i, discord.ui.Select))
        assert select.disabled is False

    def test_the_colour_button_is_disabled_when_locked(self, enforced):
        view = self.build(False, False)
        buttons = [
            i
            for i in view.children
            if isinstance(i, discord.ui.Button) and i.label == "Set colour"
        ]
        assert len(buttons) == 1
        assert buttons[0].disabled is True

    def test_next_is_disabled_on_the_last_page(self, enforced):
        view = self.build(True, False)
        nxt = next(
            i
            for i in view.children
            if isinstance(i, discord.ui.Button) and i.label == "Next"
        )
        assert nxt.disabled is True

    def test_earlier_pages_can_still_page_forward(self, enforced):
        """Adding a page must not leave Next dead on the old last page."""
        view = self.build(True, False, page=2)
        nxt = next(
            i
            for i in view.children
            if isinstance(i, discord.ui.Button) and i.label == "Next"
        )
        assert nxt.disabled is False

    def test_it_reports_the_current_values(self, enforced):
        content = self.build(True, False, embed_color=BRAND, show_icon=True).render_content()
        assert "#5865F2" in content
        assert "Show server icon: Yes" in content

    def test_it_reports_the_default_when_no_colour_is_set(self, enforced):
        content = self.build(True, False).render_content()
        assert "Default blue" in content

    def test_paging_carries_the_branding_values(self, enforced):
        view = self.build(True, False, embed_color=BRAND, show_icon=True)
        moved = view._rebuilt(0)
        assert moved.embed_color == BRAND
        assert moved.show_icon is True

    def test_the_clear_button_only_appears_when_a_colour_is_set(self, enforced):
        def labels(view):
            return [
                i.label for i in view.children if isinstance(i, discord.ui.Button)
            ]

        assert "Use default colour" not in labels(self.build(True, False))
        assert "Use default colour" in labels(
            self.build(True, False, embed_color=BRAND)
        )

    def test_the_page_is_registered_as_premium_gated(self):
        assert bot.SETTINGS_PAGE_FEATURE[self.PAGE] == bot.FEATURE_BRANDED_PANEL


class TestSaveOrdering:
    """Discord gives three seconds to acknowledge an interaction.

    Editing the panel is a real HTTP call and message edits are rate limited
    per channel, so it has to happen after the reply or a 429 turns a
    successful save into "This interaction failed" for the admin.
    """

    def build_and_save(self, monkeypatch, premium_flag=True):
        order = []

        async def fake_restyle(guild_id):
            order.append("panel_edit")
            return "ok"

        monkeypatch.setattr(bot, "restyle_instruction_panel", fake_restyle)

        async def fake_response(**kwargs):
            order.append("reply")

        interaction = SimpleNamespace(
            guild=SimpleNamespace(id=int(GUILD_ID)),
            user=SimpleNamespace(id=int(OWNER_ID)),
            locale="en-US",
            response=SimpleNamespace(edit_message=fake_response),
        )

        view = bot.PagedSettingsView(
            True,
            "en-US",
            True,
            auto_verify_available=True,
            page_index=bot.SETTINGS_LAST_PAGE,
            premium=bot.PremiumFlags(premium=premium_flag, grandfathered=False),
            embed_color=BRAND,
            show_icon=True,
        )
        save = next(
            i
            for i in view.children
            if isinstance(i, discord.ui.Button) and i.label == "Save"
        )
        run(save.callback(interaction))
        return order

    def test_the_reply_comes_before_the_panel_edit(self, monkeypatch, enforced):
        make_server(row_id=NEW_ID)
        assert self.build_and_save(monkeypatch) == ["reply", "panel_edit"]

    def test_a_free_server_edits_no_panel(self, monkeypatch, enforced):
        make_server(row_id=NEW_ID)
        assert self.build_and_save(monkeypatch, premium_flag=False) == ["reply"]


# ---------------------------------------------------------------
# Entitlement events keep the live panel honest
# ---------------------------------------------------------------
class TestEntitlementEvents:
    def test_a_branded_guild_gets_its_panel_restyled(self, monkeypatch):
        make_server(row_id=NEW_ID)
        set_branding()
        called = []
        monkeypatch.setattr(
            bot, "restyle_instruction_panel", lambda gid: called.append(gid)
        )
        monkeypatch.setattr(
            bot.asyncio, "create_task", lambda coro: coro
        )

        bot._note_entitlement_change(
            SimpleNamespace(guild_id=int(GUILD_ID)), "updated"
        )
        assert called == [int(GUILD_ID)]

    def test_an_unbranded_guild_is_left_alone(self, monkeypatch):
        """Nothing to change, so there is no reason to spend an edit."""
        make_server(row_id=NEW_ID)
        called = []
        monkeypatch.setattr(
            bot, "restyle_instruction_panel", lambda gid: called.append(gid)
        )
        monkeypatch.setattr(bot.asyncio, "create_task", lambda coro: coro)

        bot._note_entitlement_change(
            SimpleNamespace(guild_id=int(GUILD_ID)), "updated"
        )
        assert called == []

    def test_an_unreadable_table_buys_no_edit(self, monkeypatch):
        make_server(row_id=NEW_ID)
        set_branding()
        called = []
        monkeypatch.setattr(
            bot, "load_panel_branding", lambda gid: bot.BRANDING_UNREADABLE
        )
        monkeypatch.setattr(
            bot, "restyle_instruction_panel", lambda gid: called.append(gid)
        )
        monkeypatch.setattr(bot.asyncio, "create_task", lambda coro: coro)

        bot._note_entitlement_change(
            SimpleNamespace(guild_id=int(GUILD_ID)), "updated"
        )
        assert called == []

    def test_a_user_scoped_entitlement_is_ignored(self, monkeypatch):
        make_server(row_id=NEW_ID)
        set_branding()
        called = []
        monkeypatch.setattr(
            bot, "restyle_instruction_panel", lambda gid: called.append(gid)
        )
        monkeypatch.setattr(bot.asyncio, "create_task", lambda coro: coro)

        bot._note_entitlement_change(SimpleNamespace(guild_id=None), "created")
        assert called == []
