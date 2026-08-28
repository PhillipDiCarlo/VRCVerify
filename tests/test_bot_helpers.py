"""Unit tests for pure helper logic in src/bot.py."""

import string
from types import SimpleNamespace

import pytest

import bot
from locales import localizations


def ctx(locale="en-US"):
    """Minimal stand-in for a discord.Interaction as used by get_message."""
    return SimpleNamespace(locale=locale)


# ---------------------------------------------------------------
# Localization helpers
# ---------------------------------------------------------------
class TestGetLocale:
    def test_supported_locale_passthrough(self):
        assert bot.get_locale(ctx("es-ES")) == "es-ES"

    def test_unsupported_locale_falls_back_to_english(self):
        assert bot.get_locale(ctx("fr")) == "en-US"

    def test_missing_locale_attribute_falls_back(self):
        assert bot.get_locale(SimpleNamespace()) == "en-US"


class TestGetMessage:
    def test_known_key_formats_kwargs(self):
        msg = bot.get_message("dm_role_success", ctx(), role="18+", server="Test")
        assert "18+" in msg and "Test" in msg

    def test_unknown_key_returns_key_itself(self):
        assert bot.get_message("no_such_key_xyz", ctx()) == "no_such_key_xyz"

    def test_key_missing_from_locale_falls_back_to_english(self):
        # Even if a locale were missing a key, English must be served.
        msg = bot.get_message("already_verified", ctx("zh-CN"))
        assert msg  # non-empty localized or English string


# ---------------------------------------------------------------
# Verification codes
# ---------------------------------------------------------------
class TestGenerateVerificationCode:
    def test_format(self):
        code = bot.generate_verification_code()
        assert code.startswith("VRC-")
        suffix = code.removeprefix("VRC-")
        assert len(suffix) == 6
        assert all(c in string.ascii_uppercase + string.digits for c in suffix)

    def test_codes_vary(self):
        codes = {bot.generate_verification_code() for _ in range(50)}
        assert len(codes) > 1

    def test_excludes_ambiguous_letters(self):
        # O/I are visually indistinguishable from 0/1 in the fonts users read
        # their code in, which caused users to mistype the code into their bio.
        codes = {bot.generate_verification_code() for _ in range(200)}
        for c in codes:
            suffix = c.removeprefix("VRC-")
            assert "O" not in suffix
            assert "I" not in suffix


# ---------------------------------------------------------------
# Verification cooldown
# ---------------------------------------------------------------
class TestVerificationCooldown:
    def setup_method(self):
        bot._verification_cooldowns.clear()

    def test_first_attempt_allowed(self):
        assert bot.check_verification_cooldown("user1") == 0

    def test_second_attempt_blocked_with_remaining_seconds(self):
        bot.check_verification_cooldown("user1", window_seconds=30)
        remaining = bot.check_verification_cooldown("user1", window_seconds=30)
        assert 0 < remaining <= 31

    def test_blocked_attempt_does_not_extend_cooldown(self):
        bot.check_verification_cooldown("user1", window_seconds=30)
        first = bot.check_verification_cooldown("user1", window_seconds=30)
        second = bot.check_verification_cooldown("user1", window_seconds=30)
        assert second <= first

    def test_users_are_independent(self):
        bot.check_verification_cooldown("user1")
        assert bot.check_verification_cooldown("user2") == 0

    def test_expired_cooldown_allows_again(self):
        bot.check_verification_cooldown("user1", window_seconds=0)
        assert bot.check_verification_cooldown("user1", window_seconds=0) == 0

    def test_verify_button_never_blocked_by_recheck_cooldown(self):
        # Regression: user triggers a re-check (default scope), then presses
        # the green Verify button — that must always be allowed.
        assert bot.check_verification_cooldown("user1") == 0
        assert bot.check_verification_cooldown("user1", scope="verify") == 0

    def test_repeated_verify_presses_still_throttled(self):
        assert bot.check_verification_cooldown("user1", scope="verify") == 0
        assert bot.check_verification_cooldown("user1", scope="verify") > 0

    def test_verify_cooldown_does_not_block_recheck(self):
        assert bot.check_verification_cooldown("user1", scope="verify") == 0
        assert bot.check_verification_cooldown("user1") == 0

    def test_cooldown_message_localized_everywhere(self):
        from locales import localizations, LANGUAGE_CODES
        for code in LANGUAGE_CODES:
            msg = localizations[code]["cooldown_active"].format(seconds=30)
            assert "30" in msg


# ---------------------------------------------------------------
# VRChat user input parsing
# ---------------------------------------------------------------
class TestParseVrcUserInput:
    def test_full_profile_url(self):
        url = "https://vrchat.com/home/user/usr_1234d567-b12e-123d-a1c2-fd12345a67ea"
        assert bot.parse_vrc_user_input(url) == "usr_1234d567-b12e-123d-a1c2-fd12345a67ea"

    def test_http_url_accepted(self):
        url = "http://vrchat.com/home/user/usr_abc123"
        assert bot.parse_vrc_user_input(url) == "usr_abc123"

    def test_url_with_surrounding_whitespace(self):
        url = "  https://vrchat.com/home/user/usr_abc123  "
        assert bot.parse_vrc_user_input(url) == "usr_abc123"

    def test_raw_user_id(self):
        assert bot.parse_vrc_user_input("usr_abc123") == "usr_abc123"

    def test_display_name_rejected(self):
        assert bot.parse_vrc_user_input("CoolVRChatter99") is None

    def test_empty_input_rejected(self):
        assert bot.parse_vrc_user_input("   ") is None

    def test_unrelated_url_rejected(self):
        assert bot.parse_vrc_user_input("https://example.com/home/user/usr_abc") is None


# ---------------------------------------------------------------
# Custom success-message sanitizing
# ---------------------------------------------------------------
class TestSanitizeCustomMessage:
    def test_plain_message_untouched(self):
        clean, invalid = bot.sanitize_custom_message("Welcome to the server!")
        assert clean == "Welcome to the server!"
        assert invalid == []

    def test_zero_width_characters_stripped(self):
        clean, _ = bot.sanitize_custom_message("Hi​there﻿!")
        assert clean == "Hithere!"

    def test_mass_mentions_neutralized(self):
        clean, _ = bot.sanitize_custom_message("hello @everyone and @here")
        assert "@everyone" not in clean
        assert "@here" not in clean

    def test_allowed_links_pass(self):
        msg = "Join https://discord.com/invite/x and https://vrchat.com/home"
        _, invalid = bot.sanitize_custom_message(msg)
        assert invalid == []

    def test_disallowed_link_flagged(self):
        _, invalid = bot.sanitize_custom_message("visit https://evil.example.com/steal")
        assert invalid == ["https://evil.example.com/steal"]

    def test_lookalike_domain_flagged(self):
        _, invalid = bot.sanitize_custom_message("https://discord.com.evil.com/x")
        assert invalid == ["https://discord.com.evil.com/x"]

    def test_plain_http_flagged(self):
        _, invalid = bot.sanitize_custom_message("http://discord.com/invite/x")
        assert invalid == ["http://discord.com/invite/x"]


# ---------------------------------------------------------------
# VRChat issue / outage message mapping
# ---------------------------------------------------------------
class TestBuildVrchatIssueMessage:
    def test_user_not_found(self):
        msg = bot.build_vrchat_issue_message({"error_type": "vrchat_user_not_found"})
        assert msg == localizations["en-US"]["vrchat_issue_user_not_found"]

    def test_rate_limited(self):
        msg = bot.build_vrchat_issue_message({"error_type": "vrchat_rate_limited"})
        assert msg == localizations["en-US"]["vrchat_issue_rate_limited"]

    @pytest.mark.parametrize("etype", ["vrchat_auth_error", "vrchat_session_unavailable"])
    def test_temp_unavailable(self, etype):
        msg = bot.build_vrchat_issue_message({"error_type": etype})
        assert msg == localizations["en-US"]["vrchat_issue_temp_unavailable"]

    def test_confirmed_outage_without_status_message(self):
        msg = bot.build_vrchat_issue_message(
            {"error_type": "vrchat_upstream_error", "vrchat_outage_confirmed": True}
        )
        assert "status.vrchat.com" in msg

    def test_confirmed_outage_with_status_message(self):
        msg = bot.build_vrchat_issue_message(
            {
                "error_type": "vrchat_upstream_error",
                "vrchat_outage_confirmed": True,
                "vrchat_status_message": "API degraded",
            }
        )
        assert "API degraded" in msg

    def test_suspected_outage(self):
        msg = bot.build_vrchat_issue_message(
            {"error_type": "vrchat_timeout", "vrchat_outage": True}
        )
        assert msg == localizations["en-US"]["vrchat_issue_outage_suspected"].format(
            status_page="https://status.vrchat.com/"
        )

    def test_unknown_error_type_gets_generic_message(self):
        msg = bot.build_vrchat_issue_message({"error_type": "something_else"})
        assert msg == localizations["en-US"]["vrchat_issue_unexpected"]

    def test_localized_output(self):
        msg = bot.build_vrchat_issue_message(
            {"error_type": "vrchat_user_not_found"}, locale_code="es-ES"
        )
        assert msg == localizations["es-ES"]["vrchat_issue_user_not_found"]


class TestDiscordSafeNickname:
    """Discord caps nicknames at 32 chars and 400s on longer values.

    That surfaces as discord.HTTPException, NOT Forbidden, so an over-long
    VRChat display name used to escape the Forbidden-only handler in
    assign_role and skip the milestone bookkeeping that runs after it.
    """

    def test_short_name_passes_through(self):
        assert bot.discord_safe_nickname("Italiandogs") == "Italiandogs"

    def test_over_long_name_is_clamped(self):
        name = "A" * 100
        out = bot.discord_safe_nickname(name)
        assert len(out) == bot.DISCORD_NICK_MAX_LEN == 32

    def test_exactly_at_limit_is_untouched(self):
        name = "B" * 32
        assert bot.discord_safe_nickname(name) == name

    def test_whitespace_is_trimmed(self):
        assert bot.discord_safe_nickname("  Name  ") == "Name"

    def test_whitespace_only_becomes_none(self):
        # Discord rejects an empty nickname; None means "skip the edit".
        assert bot.discord_safe_nickname("   ") is None

    @pytest.mark.parametrize("bad", [None, 123, {"n": "x"}, ["n"], True])
    def test_non_string_becomes_none(self, bad):
        # display_name arrives from raw /profile JSON over RabbitMQ.
        assert bot.discord_safe_nickname(bad) is None


class TestVerificationCodeGeneration:
    def test_uses_cryptographic_randomness(self):
        # The code gates 18+ verification; random's Mersenne Twister is
        # predictable from observed output, so secrets must be used.
        import inspect

        src = inspect.getsource(bot.generate_verification_code)
        assert "secrets." in src
        assert "random." not in src

    def test_shape_and_charset(self):
        codes = {bot.generate_verification_code() for _ in range(200)}
        assert len(codes) > 190  # no obvious collisions
        for c in codes:
            assert c.startswith("VRC-")
            body = c[4:]
            assert len(body) == 6
            assert all(ch in string.ascii_uppercase + string.digits for ch in body)
            assert "O" not in body and "I" not in body


# ---------------------------------------------------------------
# The VRCVerify Discord invite (#138)
# ---------------------------------------------------------------
class TestTheSupportInviteIsOptional:
    """Unset means the feature is not provisioned, not broken.

    This is what lets #138 ship its plumbing before the announcement channel
    exists -- the same shape the invite worker uses for INVITE_VRCHAT_USERNAME.
    """

    def test_no_invite_configured_offers_none(self, monkeypatch):
        monkeypatch.setattr(bot, "SUPPORT_INVITE_URL", None)
        assert bot.support_invite_url() is None

    def test_an_empty_value_is_the_same_as_unset(self, monkeypatch):
        monkeypatch.setattr(bot, "SUPPORT_INVITE_URL", "")
        assert bot.support_invite_url() is None

    def test_a_configured_invite_is_returned(self, monkeypatch):
        monkeypatch.setattr(bot, "SUPPORT_INVITE_URL", "https://discord.gg/abc")
        assert bot.support_invite_url() == "https://discord.gg/abc"

    @pytest.mark.parametrize(
        "value", ["discord.gg/abc", "www.discord.gg/abc", "ftp://discord.gg/abc"]
    )
    def test_a_url_without_a_usable_scheme_is_refused(self, monkeypatch, value):
        """Discord renders a schemeless string as plain text, so the admin sees
        something they have to retype. Costing a sentence beats that."""
        monkeypatch.setattr(bot, "SUPPORT_INVITE_URL", value)
        assert bot.support_invite_url() is None

    def test_a_bad_value_says_so_in_the_log(self, monkeypatch, caplog):
        import logging

        monkeypatch.setattr(bot, "SUPPORT_INVITE_URL", "discord.gg/abc")
        with caplog.at_level(logging.WARNING):
            bot.support_invite_url()
        assert "SUPPORT_INVITE_URL" in caplog.text


class TestTheInviteSentenceIsLocalised:
    def test_every_locale_can_render_it(self):
        """The URL is language-neutral and comes from config, so an admin in
        any locale gets a working link on day one -- see UNTRANSLATED in
        tests/test_locales.py for why the carrier sentence is English."""
        for code in bot.LANGUAGE_CODES:
            rendered = bot.get_message(
                "support_invite_line", ctx(code), invite="https://discord.gg/abc"
            )
            assert "https://discord.gg/abc" in rendered
            assert "{invite}" not in rendered

    def test_the_placeholder_is_the_only_one(self):
        """A second placeholder would make every non-English table a KeyError
        waiting for the one caller that forgets it."""
        for code in bot.LANGUAGE_CODES:
            template = localizations[code]["support_invite_line"]
            names = {
                f for _, f, _, _ in string.Formatter().parse(template) if f
            }
            assert names == {"invite"}

    def test_the_url_is_not_baked_into_any_locale(self):
        """The whole reason for the placeholder: rotating the invite must be a
        config change, not a code change across twelve tables."""
        for code in bot.LANGUAGE_CODES:
            assert "discord.gg" not in localizations[code]["support_invite_line"]


class TestTheLogChannelRuleIsStillAboutContent:
    """#138 has admins follow OUR announcement channel while the log channel
    refuses to BE one. The two look contradictory and are not, so the comment
    explaining that is load-bearing -- it is the thing that stops somebody
    later "fixing" one of the two decisions to match the other.
    """

    def test_the_refusal_still_exists(self):
        """The acceptance criterion #138 must not weaken."""
        import inspect

        source = inspect.getsource(bot.write_dashboard_settings)
        assert "is_news()" in source
        assert "channel_is_announcement" in source

    def test_the_comment_no_longer_credits_a_command_that_stopped_checking(self):
        """It claimed "/vrcverify_logchannel refuses an announcement channel
        outright" long after that command went read-only, and claimed it
        confirms by posting into the channel long after it stopped writing at
        all."""
        import inspect

        source = inspect.getsource(bot)
        start = source.index("The log channel, which unlike a role")
        comment = source[start:start + 1400]
        assert "/vrcverify_logchannel refuses" not in comment
        assert "write_dashboard_settings" in comment

    def test_the_two_decisions_are_reconciled_in_writing(self):
        import inspect

        source = inspect.getsource(bot)
        start = source.index("The log channel, which unlike a role")
        comment = source[start:start + 1400]
        assert "#138" in comment
