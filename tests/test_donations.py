"""Unit tests for the donation features.

Covers:
- the ☕ Donate link button on the instruction panel view
- the donate hint appended to the /vrcverify_setup confirmation
- record_guild_verification(): per-guild counting, the one-time milestone
  DM to the configuring admin (with guild-owner fallback), and the
  pre-migration column guard
- the milestone wiring inside handle_verification_result
- locale coverage/formatting of the three new strings in all languages
- the GitHub FUNDING.yml sponsor declaration
"""

import asyncio
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import discord
import pytest

import bot
from locales import localizations, LANGUAGE_CODES

REPO_ROOT = Path(__file__).resolve().parents[1]
GUILD_ID = "123456789"
OWNER_ID = "42"


def run(coro):
    """Run an async bot helper from a sync test (no pytest-asyncio needed)."""
    return asyncio.run(coro)


def fake_guild(owner=None):
    return SimpleNamespace(name="Test Guild", owner=owner, owner_id=999)


def make_server(**overrides):
    fields = dict(
        server_id=GUILD_ID,
        owner_id=OWNER_ID,
        role_id="1",
        instructions_locale="en-US",
    )
    fields.update(overrides)
    with bot.session_scope() as session:
        session.add(bot.Server(**fields))


def get_counts():
    with bot.session_scope() as session:
        srv = session.query(bot.Server).filter_by(server_id=GUILD_ID).first()
        return srv.verification_count, bool(srv.milestone_dm_sent)


@pytest.fixture
def clean_servers():
    with bot.session_scope() as session:
        session.query(bot.Server).delete()
    yield
    with bot.session_scope() as session:
        session.query(bot.Server).delete()


@pytest.fixture
def dm_spy(monkeypatch):
    """Capture dm_localized calls instead of hitting Discord."""
    calls = []

    async def fake_dm(member, guild, key, instr_locale=None, **kwargs):
        calls.append(
            SimpleNamespace(
                member=member, guild=guild, key=key, locale=instr_locale, kwargs=kwargs
            )
        )

    monkeypatch.setattr(bot, "dm_localized", fake_dm)
    return calls


@pytest.fixture
def owner_member(monkeypatch):
    """fetch_member_cached resolves the configuring admin (id 42) only."""
    member = SimpleNamespace(id=int(OWNER_ID), name="setup-admin")

    async def fake_fetch(guild, user_id):
        return member if user_id == int(OWNER_ID) else None

    monkeypatch.setattr(bot, "fetch_member_cached", fake_fetch)
    return member


# ---------------------------------------------------------------
# Instruction panel: Donate button
# ---------------------------------------------------------------
class TestDonateButton:
    def _view(self, locale="en-US"):
        return bot.VRCVerifyInstructionView(locale=locale)

    def test_panel_has_three_buttons_in_order(self):
        en = localizations["en-US"]
        labels = [c.label for c in self._view().children]
        assert labels == [
            en["btn_begin_verification"],
            en["btn_update_nickname"],
            en["btn_donate"],
        ]

    def test_donate_is_link_button_to_kofi(self):
        donate = self._view().children[-1]
        assert donate.style is discord.ButtonStyle.link
        assert donate.url == bot.KOFI_URL

    def test_donate_has_coffee_emoji(self):
        assert str(self._view().children[-1].emoji) == "☕"

    def test_donate_label_localized(self):
        donate = self._view("de").children[-1]
        assert donate.label == localizations["de"]["btn_donate"]

    def test_unknown_locale_falls_back_to_english(self):
        donate = self._view("fr").children[-1]
        assert donate.label == localizations["en-US"]["btn_donate"]

    def test_view_stays_non_expiring(self):
        # Panels are re-attached on startup and must never time out.
        assert self._view().timeout is None

    def test_kofi_url_is_https(self):
        assert bot.KOFI_URL.startswith("https://ko-fi.com/")


# ---------------------------------------------------------------
# New locale strings
# ---------------------------------------------------------------
class TestDonationLocaleStrings:
    @pytest.mark.parametrize("locale", LANGUAGE_CODES)
    def test_new_keys_exist_everywhere(self, locale):
        for key in ("btn_donate", "setup_donate_hint", "milestone_owner_dm"):
            assert key in localizations[locale], f"{locale} missing {key}"

    @pytest.mark.parametrize("locale", LANGUAGE_CODES)
    def test_setup_hint_embeds_the_link(self, locale):
        msg = localizations[locale]["setup_donate_hint"].format(kofi_link=bot.KOFI_URL)
        assert bot.KOFI_URL in msg

    @pytest.mark.parametrize("locale", LANGUAGE_CODES)
    def test_setup_hint_separates_itself(self, locale):
        # The hint is appended to the setup confirmation, so it must start
        # on its own paragraph in every language.
        assert localizations[locale]["setup_donate_hint"].startswith("\n\n")

    @pytest.mark.parametrize("locale", LANGUAGE_CODES)
    def test_milestone_dm_embeds_all_fields(self, locale):
        msg = localizations[locale]["milestone_owner_dm"].format(
            server="SrvName", count=100, kofi_link=bot.KOFI_URL
        )
        assert "SrvName" in msg
        assert "100" in msg
        assert bot.KOFI_URL in msg


# ---------------------------------------------------------------
# /vrcverify_setup confirmation carries the donate hint
# ---------------------------------------------------------------
class TestSetupConfirmationHint:
    def test_setup_reply_ends_with_donate_hint(self, clean_servers):
        sent = []

        async def send_message(msg, ephemeral=False):
            sent.append(msg)

        interaction = SimpleNamespace(
            guild=SimpleNamespace(id=int(GUILD_ID)),
            user=SimpleNamespace(id=int(OWNER_ID)),
            locale="en-US",
            response=SimpleNamespace(send_message=send_message),
        )
        role = SimpleNamespace(id=1, name="Verified")

        run(bot.vrcverify_setup.callback(interaction, role, None))

        assert len(sent) == 1
        expected_tail = localizations["en-US"]["setup_donate_hint"].format(
            kofi_link=bot.KOFI_URL
        )
        assert sent[0].endswith(expected_tail)


# ---------------------------------------------------------------
# record_guild_verification: counting + one-time milestone DM
# ---------------------------------------------------------------
class TestRecordGuildVerification:
    def test_counts_each_verification_below_threshold(
        self, clean_servers, dm_spy, owner_member, monkeypatch
    ):
        monkeypatch.setattr(bot, "MILESTONE_VERIFICATION_COUNT", 100)
        make_server()
        guild = fake_guild()
        run(bot.record_guild_verification(GUILD_ID, guild))
        run(bot.record_guild_verification(GUILD_ID, guild))
        count, sent = get_counts()
        assert count == 2
        assert sent is False
        assert dm_spy == []

    def test_milestone_dms_admin_exactly_once(
        self, clean_servers, dm_spy, owner_member, monkeypatch
    ):
        monkeypatch.setattr(bot, "MILESTONE_VERIFICATION_COUNT", 3)
        make_server()
        guild = fake_guild()
        for _ in range(5):
            run(bot.record_guild_verification(GUILD_ID, guild))

        count, sent = get_counts()
        assert count == 5
        assert sent is True
        assert len(dm_spy) == 1
        call = dm_spy[0]
        assert call.member is owner_member
        assert call.key == "milestone_owner_dm"
        assert call.kwargs == {
            "server": guild.name,
            "count": 3,
            "kofi_link": bot.KOFI_URL,
        }

    def test_milestone_dm_uses_server_locale(
        self, clean_servers, dm_spy, owner_member, monkeypatch
    ):
        monkeypatch.setattr(bot, "MILESTONE_VERIFICATION_COUNT", 1)
        make_server(instructions_locale="de")
        run(bot.record_guild_verification(GUILD_ID, fake_guild()))
        assert dm_spy[0].locale == "de"

    def test_falls_back_to_guild_owner_when_admin_left(
        self, clean_servers, dm_spy, monkeypatch
    ):
        monkeypatch.setattr(bot, "MILESTONE_VERIFICATION_COUNT", 1)

        async def nobody(guild, user_id):
            return None

        monkeypatch.setattr(bot, "fetch_member_cached", nobody)
        make_server()
        guild_owner = SimpleNamespace(id=999, name="guild-owner")
        run(bot.record_guild_verification(GUILD_ID, fake_guild(owner=guild_owner)))
        assert len(dm_spy) == 1
        assert dm_spy[0].member is guild_owner

    def test_no_dm_when_nobody_resolvable_but_flag_still_set(
        self, clean_servers, dm_spy, monkeypatch
    ):
        monkeypatch.setattr(bot, "MILESTONE_VERIFICATION_COUNT", 1)

        async def nobody(guild, user_id):
            return None

        monkeypatch.setattr(bot, "fetch_member_cached", nobody)
        make_server()
        run(bot.record_guild_verification(GUILD_ID, fake_guild(owner=None)))
        _, sent = get_counts()
        assert sent is True
        assert dm_spy == []

    def test_uncached_guild_counts_but_skips_dm(
        self, clean_servers, dm_spy, owner_member, monkeypatch
    ):
        monkeypatch.setattr(bot, "MILESTONE_VERIFICATION_COUNT", 1)
        make_server()
        run(bot.record_guild_verification(GUILD_ID, None))
        count, sent = get_counts()
        assert count == 1
        assert sent is True  # milestone consumed; never re-fires later
        assert dm_spy == []

    def test_preexisting_flag_blocks_repeat_dm(
        self, clean_servers, dm_spy, owner_member, monkeypatch
    ):
        monkeypatch.setattr(bot, "MILESTONE_VERIFICATION_COUNT", 3)
        make_server(verification_count=10, milestone_dm_sent=True)
        run(bot.record_guild_verification(GUILD_ID, fake_guild()))
        count, _ = get_counts()
        assert count == 11
        assert dm_spy == []

    def test_unknown_guild_row_is_noop(self, clean_servers, dm_spy, owner_member):
        run(bot.record_guild_verification(GUILD_ID, fake_guild()))
        assert dm_spy == []

    def test_empty_guild_id_is_noop(self, clean_servers, dm_spy, owner_member):
        run(bot.record_guild_verification("", fake_guild()))
        run(bot.record_guild_verification(None, fake_guild()))
        assert dm_spy == []

    def test_missing_columns_skips_counting(
        self, clean_servers, dm_spy, owner_member, monkeypatch
    ):
        # Pre-migration databases must not error or count.
        monkeypatch.setattr(bot, "server_has_column", lambda name: False)
        make_server()
        run(bot.record_guild_verification(GUILD_ID, fake_guild()))
        count, sent = get_counts()
        assert count == 0
        assert sent is False
        assert dm_spy == []

    def test_db_failure_is_swallowed(self, dm_spy, owner_member, monkeypatch):
        # A broken DB must never crash the verification result handler.
        from contextlib import contextmanager

        @contextmanager
        def broken_scope():
            raise RuntimeError("db down")
            yield  # pragma: no cover

        monkeypatch.setattr(bot, "session_scope", broken_scope)
        run(bot.record_guild_verification(GUILD_ID, fake_guild()))  # must not raise
        assert dm_spy == []


# ---------------------------------------------------------------
# handle_verification_result triggers milestone counting
# ---------------------------------------------------------------
class NaiveNowDatetime(datetime):
    """SQLite returns naive datetimes, so pending-expiry comparisons need a
    naive 'now' inside handle_verification_result during these tests."""

    @classmethod
    def now(cls, tz=None):
        return datetime.now()


class TestVerificationResultCountsMilestone:
    CODE = "VRC-TEST01"

    @pytest.fixture
    def spies(self, monkeypatch):
        calls = SimpleNamespace(assign=[], record=[])

        async def fake_assign(*args, **kwargs):
            calls.assign.append((args, kwargs))

        async def fake_record(*args, **kwargs):
            calls.record.append(args)

        monkeypatch.setattr(bot, "assign_role", fake_assign)
        monkeypatch.setattr(bot, "record_guild_verification", fake_record)
        return calls

    @pytest.fixture
    def user_row(self):
        with bot.session_scope() as session:
            session.query(bot.User).delete()
            session.add(bot.User(discord_id="555", verification_status=False))
        yield
        with bot.session_scope() as session:
            session.query(bot.User).delete()

    @pytest.fixture
    def naive_now(self, monkeypatch):
        monkeypatch.setattr(bot, "datetime", NaiveNowDatetime)

    def _make_pending(self, expires_in_minutes=5):
        with bot.session_scope() as session:
            session.query(bot.PendingVerification).delete()
            session.add(
                bot.PendingVerification(
                    discord_id="555",
                    guild_id=GUILD_ID,
                    vrc_user_id="usr_x",
                    verification_code=self.CODE,
                    expires_at=datetime.now() + timedelta(minutes=expires_in_minutes),
                )
            )

    def _pending_count(self):
        with bot.session_scope() as session:
            return session.query(bot.PendingVerification).count()

    def _recheck_data(self, is_18_plus):
        return {
            "discordID": "555",
            "guildID": GUILD_ID,
            "is_18_plus": is_18_plus,
            "verificationCode": None,
        }

    def _code_data(self, is_18_plus=True, code_found=True):
        return {
            "discordID": "555",
            "guildID": GUILD_ID,
            "is_18_plus": is_18_plus,
            "verificationCode": self.CODE,
            "code_found": code_found,
            "vrcUserID": "usr_x",
        }

    # -- re-check (no-code) flow --
    def test_recheck_success_counts_toward_milestone(self, spies, user_row):
        run(bot.handle_verification_result(self._recheck_data(True)))
        assert len(spies.assign) == 1  # flow completed
        assert spies.record == [(GUILD_ID, None)]

    def test_not_18_plus_never_counts(self, spies, user_row):
        run(bot.handle_verification_result(self._recheck_data(False)))
        assert len(spies.assign) == 1  # flow completed
        assert spies.record == []

    # -- code-based (bio) flow --
    def test_code_flow_success_counts_and_updates_user(
        self, spies, user_row, naive_now
    ):
        self._make_pending()
        run(bot.handle_verification_result(self._code_data()))
        assert len(spies.assign) == 1
        assert spies.record == [(GUILD_ID, None)]
        assert self._pending_count() == 0
        with bot.session_scope() as session:
            user = session.query(bot.User).filter_by(discord_id="555").first()
            assert bool(user.verification_status) is True
            assert user.vrc_user_id == "usr_x"

    def test_code_flow_not_18_plus_never_counts(self, spies, user_row, naive_now):
        self._make_pending()
        run(bot.handle_verification_result(self._code_data(is_18_plus=False)))
        assert len(spies.assign) == 1
        assert spies.record == []

    def test_expired_code_never_counts(self, spies, user_row, naive_now):
        self._make_pending(expires_in_minutes=-1)
        run(bot.handle_verification_result(self._code_data()))
        assert spies.assign == []
        assert spies.record == []
        assert self._pending_count() == 0  # expired pending is removed

    def test_code_not_found_in_bio_never_counts(self, spies, user_row, naive_now):
        self._make_pending()
        run(bot.handle_verification_result(self._code_data(code_found=False)))
        assert spies.assign == []
        assert spies.record == []
        # Pending row must survive so the "try again" DM's Verify button
        # (which re-sends the same code) can still find a match.
        assert self._pending_count() == 1

    def test_unknown_code_never_counts(self, spies, user_row, naive_now):
        # No pending row at all
        with bot.session_scope() as session:
            session.query(bot.PendingVerification).delete()
        run(bot.handle_verification_result(self._code_data()))
        assert spies.assign == []
        assert spies.record == []

    # -- flows that must never count --
    def test_nickname_update_flow_never_counts(self, spies, user_row):
        data = {
            "discordID": "555",
            "guildID": GUILD_ID,
            "is_18_plus": True,
            "verificationCode": None,
            "updateNickname": True,
            "display_name": "SomeName",
        }
        run(bot.handle_verification_result(data))
        assert spies.assign == []
        assert spies.record == []

    def test_vrchat_lookup_failure_never_counts(self, spies, user_row):
        data = {
            "discordID": "555",
            "guildID": GUILD_ID,
            "is_18_plus": True,
            "verificationCode": None,
            "lookup_ok": False,
            "error_type": "vrchat_timeout",
        }
        run(bot.handle_verification_result(data))
        assert spies.assign == []
        assert spies.record == []


# ---------------------------------------------------------------
# /vrcverify_instructions posts the panel, then nudges the admin
# ---------------------------------------------------------------
class TestInstructionsFollowupHint:
    def test_admin_gets_ephemeral_donate_followup(self, clean_servers):
        panel = []
        followups = []
        message = SimpleNamespace(id=111)

        async def send_message(embed=None, view=None):
            panel.append(SimpleNamespace(embed=embed, view=view))

        async def original_response():
            return message

        async def followup_send(msg, ephemeral=False):
            followups.append(SimpleNamespace(msg=msg, ephemeral=ephemeral))

        interaction = SimpleNamespace(
            guild=SimpleNamespace(id=int(GUILD_ID)),
            channel=SimpleNamespace(id=222),
            locale="en-US",
            response=SimpleNamespace(send_message=send_message),
            original_response=original_response,
            followup=SimpleNamespace(send=followup_send),
        )

        make_server()
        run(bot.vrcverify_instructions.callback(interaction))

        # public panel went out with the Donate button attached
        assert len(panel) == 1
        assert panel[0].view.children[-1].url == bot.KOFI_URL
        # panel location saved for reinitialization on restart
        with bot.session_scope() as session:
            srv = session.query(bot.Server).filter_by(server_id=GUILD_ID).first()
            assert srv.instructions_channel_id == "222"
            assert srv.instructions_message_id == "111"
        # admin-only follow-up carries the donate hint
        assert len(followups) == 1
        assert followups[0].ephemeral is True
        assert bot.KOFI_URL in followups[0].msg
        assert not followups[0].msg.startswith("\n")  # stripped for standalone use


# ---------------------------------------------------------------
# /vrcverify_subscription still hands out the Ko-fi link
# ---------------------------------------------------------------
class TestSubscriptionCommand:
    def test_reply_contains_kofi_link(self):
        sent = []

        async def send_message(msg, ephemeral=False):
            sent.append(msg)

        interaction = SimpleNamespace(
            locale="en-US",
            response=SimpleNamespace(send_message=send_message),
        )
        run(bot.vrcverify_subscription.callback(interaction))
        assert len(sent) == 1
        assert bot.KOFI_URL in sent[0]


# ---------------------------------------------------------------
# GitHub sponsor button
# ---------------------------------------------------------------
class TestFundingFile:
    def test_funding_yml_declares_kofi(self):
        funding = (REPO_ROOT / ".github" / "FUNDING.yml").read_text(encoding="utf-8")
        assert "ko_fi: italiandogs" in funding
