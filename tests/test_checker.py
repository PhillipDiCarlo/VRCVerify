"""Unit tests for src/vrc_online_checker.py logic.

All network paths (VRChat login, status page, RabbitMQ) are monkeypatched;
these tests exercise pure logic: bio code matching, error classification,
outage detection from status-page summaries, result payload shape, the
/profile-vs-/users lookup preference, and verify_and_build_result with a
faked VRChat session.
"""

import json
from types import SimpleNamespace

import pytest

import vrc_online_checker as checker


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
    monkeypatch.setattr(checker, "_fetch_vrchat_status_summary", lambda force_refresh=False: None)


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
class TestClassifyVrchatApiError:
    @pytest.mark.parametrize("status", [500, 502, 503, 504])
    def test_upstream_errors(self, status):
        meta = checker._classify_vrchat_api_error(FakeApiException(status=status, reason="Server Error"))
        assert meta["error_type"] == "vrchat_upstream_error"
        assert meta["vrchat_outage"] is True
        assert meta["lookup_ok"] is False

    def test_rate_limited(self):
        meta = checker._classify_vrchat_api_error(FakeApiException(status=429, reason="Too Many Requests"))
        assert meta["error_type"] == "vrchat_rate_limited"
        assert meta["vrchat_outage"] is False

    @pytest.mark.parametrize("status", [401, 403])
    def test_auth_errors(self, status):
        meta = checker._classify_vrchat_api_error(FakeApiException(status=status, reason="Unauthorized"))
        assert meta["error_type"] == "vrchat_auth_error"

    def test_user_not_found(self):
        meta = checker._classify_vrchat_api_error(FakeApiException(status=404, reason="Not Found"))
        assert meta["error_type"] == "vrchat_user_not_found"

    def test_timeout_from_message(self):
        meta = checker._classify_vrchat_api_error(Exception("connection timed out"))
        assert meta["error_type"] == "vrchat_timeout"
        assert meta["vrchat_outage"] is True

    def test_unknown_error(self):
        meta = checker._classify_vrchat_api_error(Exception("weird failure"))
        assert meta["error_type"] == "vrchat_error"
        assert meta["vrchat_outage"] is False

    def test_long_reason_truncated(self):
        meta = checker._classify_vrchat_api_error(Exception("x" * 2000))
        assert len(meta["error_message"]) <= 500


# ---------------------------------------------------------------
# Status page parsing
# ---------------------------------------------------------------
class TestExtractRelevantVrchatStatus:
    def test_no_summary_available(self):
        status = checker._extract_relevant_vrchat_status()
        assert status == NO_OUTAGE_STATUS

    def test_all_operational(self, monkeypatch):
        summary = {
            "status": {"indicator": "none"},
            "incidents": [],
            "components": [{"name": "API", "status": "operational"}],
        }
        monkeypatch.setattr(checker, "_fetch_vrchat_status_summary", lambda force_refresh=False: summary)
        status = checker._extract_relevant_vrchat_status()
        assert status["vrchat_outage_confirmed"] is False
        assert status["vrchat_status_message"] is None

    def test_api_component_degraded(self, monkeypatch):
        summary = {
            "status": {"indicator": "major"},
            "incidents": [],
            "components": [
                {"name": "API", "status": "major_outage"},
                {"name": "Website", "status": "operational"},
            ],
        }
        monkeypatch.setattr(checker, "_fetch_vrchat_status_summary", lambda force_refresh=False: summary)
        status = checker._extract_relevant_vrchat_status()
        assert status["vrchat_outage_confirmed"] is True
        assert "API" in status["vrchat_status_message"]

    def test_active_login_incident_confirms_outage(self, monkeypatch):
        summary = {
            "status": {"indicator": "minor"},
            "incidents": [
                {
                    "name": "Login issues",
                    "status": "investigating",
                    "incident_updates": [{"body": "We are investigating login failures."}],
                }
            ],
            "components": [],
        }
        monkeypatch.setattr(checker, "_fetch_vrchat_status_summary", lambda force_refresh=False: summary)
        status = checker._extract_relevant_vrchat_status()
        assert status["vrchat_outage_confirmed"] is True
        assert "Login issues" in status["vrchat_status_message"]

    def test_resolved_incident_ignored(self, monkeypatch):
        summary = {
            "status": {"indicator": "none"},
            "incidents": [
                {
                    "name": "Old login incident",
                    "status": "resolved",
                    "incident_updates": [{"body": "Fixed."}],
                }
            ],
            "components": [],
        }
        monkeypatch.setattr(checker, "_fetch_vrchat_status_summary", lambda force_refresh=False: summary)
        status = checker._extract_relevant_vrchat_status()
        assert status["vrchat_outage_confirmed"] is False


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


class TestSessionPersistence:
    """Storing the auth cookie keeps restarts from re-authenticating.

    Every fresh login burns a 2FA email and VRChat rate-limits that endpoint,
    so a few quick redeploys can lock the account out and take verification
    down. None of this may ever block a login, hence the tolerance tests.
    """

    def _client(self):
        return SimpleNamespace(rest_client=SimpleNamespace(cookie_jar=object()))

    def test_disabled_when_unset(self, monkeypatch):
        monkeypatch.setattr(checker, "VRCHAT_SESSION_FILE", "")
        assert checker._attach_session_store(self._client()) is None

    def test_roundtrip_persists_and_reloads(self, monkeypatch, tmp_path):
        path = tmp_path / "session.txt"
        monkeypatch.setattr(checker, "VRCHAT_SESSION_FILE", str(path))

        client = self._client()
        jar = checker._attach_session_store(client)
        assert jar is not None
        assert client.rest_client.cookie_jar is jar

        checker._persist_session(jar)
        assert path.exists()

        # A second client picks the stored session back up.
        assert checker._attach_session_store(self._client()) is not None

    def test_corrupt_session_file_is_ignored(self, monkeypatch, tmp_path):
        path = tmp_path / "session.txt"
        path.write_text("this is not a cookie jar", encoding="utf-8")
        monkeypatch.setattr(checker, "VRCHAT_SESSION_FILE", str(path))
        # Must not raise: a bad file falls back to a normal login.
        assert checker._attach_session_store(self._client()) is not None

    def test_persist_failure_is_swallowed(self, monkeypatch, tmp_path):
        monkeypatch.setattr(checker, "VRCHAT_SESSION_FILE", str(tmp_path / "s.txt"))
        jar = checker._attach_session_store(self._client())

        def boom(*a, **k):
            raise OSError("read-only filesystem")

        monkeypatch.setattr(jar, "save", boom)
        checker._persist_session(jar)  # must not raise

    def test_persist_noop_when_disabled(self):
        checker._persist_session(None)  # must not raise

    def test_discard_removes_file(self, monkeypatch, tmp_path):
        path = tmp_path / "session.txt"
        path.write_text("x", encoding="utf-8")
        monkeypatch.setattr(checker, "VRCHAT_SESSION_FILE", str(path))
        checker._discard_stored_session()
        assert not path.exists()
        checker._discard_stored_session()  # already gone: still must not raise

    def _patch_login_stack(self, monkeypatch, get_current_user):
        """Fake out ApiClient/AuthenticationApi for login_to_vrchat."""
        monkeypatch.setattr(
            checker.vrchatapi, "Configuration", lambda **k: SimpleNamespace()
        )
        monkeypatch.setattr(
            checker.vrchatapi,
            "ApiClient",
            lambda cfg: SimpleNamespace(
                user_agent="", rest_client=SimpleNamespace(cookie_jar=None)
            ),
        )
        monkeypatch.setattr(
            checker.authentication_api,
            "AuthenticationApi",
            lambda c: SimpleNamespace(get_current_user=get_current_user),
        )

    def test_rejected_stored_session_retries_clean_without_looping(self, monkeypatch):
        """A stale cookie must not strand us or recurse forever.

        Without this, a rejected stored session would be sent through the 2FA
        path -- the exact endpoint VRChat rate-limits.
        """
        calls = {"get_current_user": 0, "discarded": 0, "attach": 0}

        def get_current_user(**kwargs):
            calls["get_current_user"] += 1
            if calls["get_current_user"] == 1:
                raise checker.UnauthorizedException(status=401)
            return SimpleNamespace(display_name="ClubLA Bot")

        self._patch_login_stack(monkeypatch, get_current_user)

        def fake_attach(client):
            calls["attach"] += 1
            # Non-empty jar on the first pass => a stored session was reused.
            return ["cookie"] if calls["attach"] == 1 else None

        monkeypatch.setattr(checker, "_attach_session_store", fake_attach)
        monkeypatch.setattr(checker, "_persist_session", lambda jar: None)
        monkeypatch.setattr(
            checker,
            "_discard_stored_session",
            lambda: calls.__setitem__("discarded", calls["discarded"] + 1),
        )

        client, err = checker.login_to_vrchat()

        assert err is None and client is not None
        assert calls["discarded"] == 1
        assert calls["get_current_user"] == 2  # retried exactly once
        assert calls["attach"] == 1  # retry did not re-load the stored session

    def test_stored_session_skips_2fa_entirely(self, monkeypatch):
        def get_current_user(**kwargs):
            return SimpleNamespace(display_name="ClubLA Bot")

        self._patch_login_stack(monkeypatch, get_current_user)
        monkeypatch.setattr(checker, "_attach_session_store", lambda c: ["cookie"])
        saved = []
        monkeypatch.setattr(checker, "_persist_session", lambda jar: saved.append(jar))

        def no_2fa():
            raise AssertionError("2FA must not be used when a session is reused")

        monkeypatch.setattr(checker, "fetch_latest_2fa_code", no_2fa)

        client, err = checker.login_to_vrchat()
        assert err is None and client is not None
        assert saved  # session refreshed on disk


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
