"""Unit tests for src/vrc_online_checker.py logic.

All network paths (VRChat login, status page, RabbitMQ) are monkeypatched;
these tests exercise pure logic: bio code matching, result payload shape,
the /profile-vs-/users lookup preference, and verify_and_build_result with
a faked VRChat session.

Login, 2FA, cookie persistence and error/outage classification moved to
test_vrc_session.py when that machinery was extracted for the second
VRChat account.
"""

import json
from types import SimpleNamespace

import pytest

import vrc_online_checker as checker
import vrc_session as vrcs


NO_OUTAGE_STATUS = {
    "vrchat_outage": False,
    "vrchat_outage_confirmed": False,
    "vrchat_status_message": None,
    "vrchat_status_indicator": None,
}


class FakeApiException(checker.ApiException):
    def __init__(self, status=None, reason="", body=""):
        self.status = status
        self.reason = reason
        self.body = body


@pytest.fixture(autouse=True)
def no_status_page(monkeypatch):
    """Never hit status.vrchat.com from tests."""
    monkeypatch.setattr(vrcs, "fetch_status_summary", lambda force_refresh=False: None)


# ---------------------------------------------------------------
# Bio code matching
# ---------------------------------------------------------------
class TestBioContainsCode:
    def test_code_on_own_line(self):
        assert checker.bio_contains_code("hello\nVRC-ABC123\nworld", "VRC-ABC123")

    def test_code_with_surrounding_whitespace(self):
        assert checker.bio_contains_code("  VRC-ABC123  ", " VRC-ABC123 ")

    def test_code_embedded_in_line_not_matched(self):
        assert not checker.bio_contains_code("my code is VRC-ABC123 ok", "VRC-ABC123")

    def test_wrong_code(self):
        assert not checker.bio_contains_code("VRC-XXXXXX", "VRC-ABC123")

    def test_empty_bio(self):
        assert not checker.bio_contains_code("", "VRC-ABC123")

    def test_none_bio_does_not_crash(self):
        assert not checker.bio_contains_code(None, "VRC-ABC123")

    @pytest.mark.parametrize("bad", [{"t": "x"}, 123, ["VRC-ABC123"], True, object()])
    def test_non_string_bio_is_false_not_an_exception(self, bad):
        # Callers turn exceptions into requeued RabbitMQ messages, so this
        # must fail closed rather than raise.
        assert checker.bio_contains_code(bad, "VRC-ABC123") is False

    @pytest.mark.parametrize("bad", [None, 123, {"c": "x"}])
    def test_non_string_code_is_false_not_an_exception(self, bad):
        # The code arrives over RabbitMQ and is not guaranteed to be a string.
        assert checker.bio_contains_code("VRC-ABC123", bad) is False


# ---------------------------------------------------------------
# Result payload shape
# ---------------------------------------------------------------
class TestResultPayload:
    def test_defaults(self):
        p = checker._result_payload("d1", "usr_1", "g1", "VRC-ABC123")
        assert p["discordID"] == "d1"
        assert p["vrcUserID"] == "usr_1"
        assert p["guildID"] == "g1"
        assert p["verificationCode"] == "VRC-ABC123"
        assert p["is_18_plus"] is False
        assert p["code_found"] is False
        assert p["lookup_ok"] is True
        assert p["error_type"] is None

    def test_extra_overrides(self):
        p = checker._result_payload("d1", "usr_1", "g1", None, is_18_plus=True, lookup_ok=False)
        assert p["is_18_plus"] is True
        assert p["lookup_ok"] is False


# ---------------------------------------------------------------
# API error classification
# ---------------------------------------------------------------
# ---------------------------------------------------------------
# verify_and_build_result (VRChat session faked)
# ---------------------------------------------------------------
def fake_users_api(user):
    class FakeUsersApi:
        def __init__(self, client):
            pass

        def get_user(self, vrc_user_id, _request_timeout=None):
            return user

    return FakeUsersApi


# The tests below pass a bare object() as the VRChat client, which has no
# call_api, so the /profile lookup fails and verify_and_build_result falls
# back to /users/ -- i.e. they exercise the fallback path on purpose.


class TestFetchProfileSnapshot:
    """/profile/{id} is preferred; /users/{id} is the safety net.

    Motivation: VRChat's /users/{id} can return a bio hours out of date,
    which silently fails verification for users whose code is really there.
    """

    def _client(self, payload=None, exc=None):
        class FakeClient:
            def call_api(self, *a, **k):
                if exc is not None:
                    raise exc
                return SimpleNamespace(data=json.dumps(payload).encode("utf-8"))

        return FakeClient()

    def test_prefers_profile_endpoint(self, monkeypatch):
        client = self._client(
            {
                "bio": "hi\nVRC-ABC123",
                "ageVerificationStatus": "18+",
                "displayName": "FromProfile",
            }
        )
        bio, age, name, source = checker.fetch_profile_snapshot(client, "usr_1")
        assert source == "profile"
        assert bio == "hi\nVRC-ABC123"
        assert age == "18+"
        assert name == "FromProfile"

    def test_falls_back_to_users_when_profile_errors(self, monkeypatch):
        client = self._client(exc=RuntimeError("boom"))
        user = SimpleNamespace(
            age_verification_status="18+", bio="from users", display_name="FromUsers"
        )
        monkeypatch.setattr(checker.users_api, "UsersApi", fake_users_api(user))
        bio, age, name, source = checker.fetch_profile_snapshot(client, "usr_1")
        assert source == "users"
        assert bio == "from users"
        assert name == "FromUsers"

    def test_falls_back_when_profile_missing_age(self, monkeypatch):
        # Age gating is security-critical: an incomplete profile response
        # must not be trusted just because it parsed.
        client = self._client({"bio": "has bio but no age"})
        user = SimpleNamespace(
            age_verification_status="18+", bio="from users", display_name="FromUsers"
        )
        monkeypatch.setattr(checker.users_api, "UsersApi", fake_users_api(user))
        _, _, _, source = checker.fetch_profile_snapshot(client, "usr_1")
        assert source == "users"

    def test_empty_bio_from_profile_is_still_trusted(self):
        # An empty bio is a legitimate value and must not trigger fallback.
        client = self._client({"bio": "", "ageVerificationStatus": "none"})
        bio, age, _, source = checker.fetch_profile_snapshot(client, "usr_1")
        assert source == "profile"
        assert bio == ""
        assert age == "none"

    @pytest.mark.parametrize(
        "bad_bio", [{"text": "VRC-ABC123"}, 12345, ["VRC-ABC123"], True]
    )
    def test_non_string_bio_falls_back_instead_of_raising(self, monkeypatch, bad_bio):
        """A type change on the undocumented endpoint must not wedge the queue.

        Regression: the guard used to check presence, not type, so a non-str
        bio reached bio_contains_code and raised AttributeError. That escapes
        verify_and_build_result into process_verification_request, which does
        nack(requeue=True) -- with prefetch_count=1 that redelivers forever
        and stops verification for every server.
        """
        client = self._client({"bio": bad_bio, "ageVerificationStatus": "18+"})
        user = SimpleNamespace(
            age_verification_status="18+", bio="from users", display_name="U"
        )
        monkeypatch.setattr(checker.users_api, "UsersApi", fake_users_api(user))
        bio, _, _, source = checker.fetch_profile_snapshot(client, "usr_1")
        assert source == "users"
        assert bio == "from users"

    def test_non_string_age_status_falls_back(self, monkeypatch):
        client = self._client({"bio": "ok", "ageVerificationStatus": {"v": "18+"}})
        user = SimpleNamespace(
            age_verification_status="18+", bio="from users", display_name="U"
        )
        monkeypatch.setattr(checker.users_api, "UsersApi", fake_users_api(user))
        _, _, _, source = checker.fetch_profile_snapshot(client, "usr_1")
        assert source == "users"

    def test_non_string_display_name_becomes_none(self):
        """displayName crosses RabbitMQ into Discord nickname handling."""
        client = self._client(
            {"bio": "b", "ageVerificationStatus": "18+", "displayName": {"n": "x"}}
        )
        _, _, display_name, source = checker.fetch_profile_snapshot(client, "usr_1")
        assert source == "profile"
        assert display_name is None

    def test_verify_survives_malformed_profile_bio(self, monkeypatch):
        """End-to-end: a bad payload degrades, it does not raise."""
        client = self._client({"bio": {"t": "x"}, "ageVerificationStatus": "18+"})
        user = SimpleNamespace(
            age_verification_status="18+", bio="my bio", display_name="U"
        )
        monkeypatch.setattr(checker, "get_vrchat_session", lambda: (client, None))
        monkeypatch.setattr(checker.users_api, "UsersApi", fake_users_api(user))
        result = checker.verify_and_build_result("d1", "usr_1", "g1", "VRC-ABC123")
        assert result["lookup_ok"] is True
        assert result["code_found"] is False

    def test_unauthorized_propagates(self, monkeypatch):
        # Must not be swallowed: the caller invalidates the session on this.
        client = self._client(exc=checker.UnauthorizedException(status=401))
        with pytest.raises(checker.UnauthorizedException):
            checker.fetch_profile_snapshot(client, "usr_1")

    def test_disabled_flag_skips_profile_entirely(self, monkeypatch):
        monkeypatch.setattr(checker, "VRCHAT_USE_PROFILE_ENDPOINT", False)

        class Boom:
            def call_api(self, *a, **k):
                raise AssertionError("/profile must not be called when disabled")

        user = SimpleNamespace(
            age_verification_status="18+", bio="from users", display_name="U"
        )
        monkeypatch.setattr(checker.users_api, "UsersApi", fake_users_api(user))
        _, _, _, source = checker.fetch_profile_snapshot(Boom(), "usr_1")
        assert source == "users"

    def test_verify_uses_fresh_profile_bio(self, monkeypatch):
        """End-to-end: a code only present in /profile still verifies.

        This is the exact regression that broke verification -- /users/
        returns the pre-edit bio while /profile/ has the code.
        """
        client = self._client(
            {
                "bio": "my bio\nVRC-FRESH1",
                "ageVerificationStatus": "18+",
                "displayName": "Fresh",
            }
        )
        stale = SimpleNamespace(
            age_verification_status="18+", bio="my bio", display_name="Stale"
        )
        monkeypatch.setattr(checker, "get_vrchat_session", lambda: (client, None))
        monkeypatch.setattr(checker.users_api, "UsersApi", fake_users_api(stale))
        result = checker.verify_and_build_result("d1", "usr_1", "g1", "VRC-FRESH1")
        assert result["code_found"] is True
        assert result["is_18_plus"] is True
        assert result["display_name"] == "Fresh"


class TestRetryCountFloor:
    """VRCHAT_LOOKUP_RETRIES counts total attempts, not extra retries.

    Regression: at 0 both retry loops fell through without ever calling
    VRChat. The profile loop hit a bogus "unreachable" AssertionError and
    get_user returned None, which became bio="" / age="unknown" -- every
    user reported "code not found" with lookup_ok=True, i.e. a silent lie.
    """

    def test_module_config_is_floored(self):
        # Asserts the FLOOR IN THE MODULE, not a restatement of max(). An
        # earlier version of this test asserted `max(1, 0) == 1`, which passed
        # even with the floor deleted from the source.
        assert checker.VRCHAT_LOOKUP_RETRIES >= 1

    def test_get_user_never_returns_none(self, monkeypatch):
        monkeypatch.setattr(checker, "VRCHAT_LOOKUP_RETRIES", 0)
        calls = []

        class Api:
            def get_user(self, uid, _request_timeout=None):
                calls.append(uid)
                return SimpleNamespace(
                    age_verification_status="18+", bio="b", display_name="U"
                )

        result = checker._get_vrchat_user_with_retry(Api(), "usr_1")
        assert calls == ["usr_1"]  # the API was actually called
        assert result is not None

    def test_profile_lookup_still_runs(self, monkeypatch):
        monkeypatch.setattr(checker, "VRCHAT_LOOKUP_RETRIES", 0)
        calls = []

        def fake_fetch(client, uid):
            calls.append(uid)
            return {"bio": "b", "ageVerificationStatus": "18+"}

        monkeypatch.setattr(checker, "_fetch_vrchat_profile", fake_fetch)
        assert checker._get_vrchat_profile_with_retry(object(), "usr_1")
        assert calls == ["usr_1"]


class TestVerifyAndBuildResult:
    def test_no_session_returns_unavailable(self, monkeypatch):
        monkeypatch.setattr(checker, "get_vrchat_session", lambda: (None, None))
        result = checker.verify_and_build_result("d1", "usr_nosession", "g1", "VRC-ABC123")
        assert result["lookup_ok"] is False
        assert result["error_type"] == "vrchat_session_unavailable"

    def test_verified_user_with_code(self, monkeypatch):
        user = SimpleNamespace(
            age_verification_status="18+",
            bio="hello\nVRC-ABC123",
            display_name="Tester",
        )
        monkeypatch.setattr(checker, "get_vrchat_session", lambda: (object(), None))
        monkeypatch.setattr(checker.users_api, "UsersApi", fake_users_api(user))
        result = checker.verify_and_build_result("d1", "usr_ok_code", "g1", "VRC-ABC123")
        assert result["lookup_ok"] is True
        assert result["is_18_plus"] is True
        assert result["code_found"] is True
        assert result["display_name"] == "Tester"

    def test_unverified_age_status(self, monkeypatch):
        user = SimpleNamespace(age_verification_status="none", bio="VRC-ABC123", display_name="T")
        monkeypatch.setattr(checker, "get_vrchat_session", lambda: (object(), None))
        monkeypatch.setattr(checker.users_api, "UsersApi", fake_users_api(user))
        result = checker.verify_and_build_result("d1", "usr_not18", "g1", "VRC-ABC123")
        assert result["is_18_plus"] is False
        assert result["code_found"] is True

    def test_recheck_without_code(self, monkeypatch):
        user = SimpleNamespace(age_verification_status="18+", bio="anything", display_name="T")
        monkeypatch.setattr(checker, "get_vrchat_session", lambda: (object(), None))
        monkeypatch.setattr(checker.users_api, "UsersApi", fake_users_api(user))
        result = checker.verify_and_build_result("d1", "usr_recheck", "g1", None)
        assert result["is_18_plus"] is True
        assert result["code_found"] is False
        assert result["verificationCode"] is None

    def test_user_with_none_bio_does_not_crash(self, monkeypatch):
        user = SimpleNamespace(age_verification_status="18+", bio=None, display_name="T")
        monkeypatch.setattr(checker, "get_vrchat_session", lambda: (object(), None))
        monkeypatch.setattr(checker.users_api, "UsersApi", fake_users_api(user))
        result = checker.verify_and_build_result("d1", "usr_nonebio", "g1", "VRC-ABC123")
        assert result["is_18_plus"] is True
        assert result["code_found"] is False
