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
