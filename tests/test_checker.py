"""Unit tests for src/vrc_online_checker.py logic.

All network paths (VRChat login, status page, RabbitMQ) are monkeypatched;
these tests exercise pure logic: bio code matching, error classification,
outage detection from status-page summaries, result payload shape, the TTL
cache, and verify_and_build_result with a faked VRChat session.
"""

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
# TTL cache
# ---------------------------------------------------------------
class TestTTLCache:
    def test_set_and_get(self):
        cache = checker._TTLCache(maxsize=10, ttl_seconds=1000)
        cache.set("k", "v")
        assert cache.get("k") == "v"

    def test_expired_entry_returns_none(self):
        cache = checker._TTLCache(maxsize=10, ttl_seconds=-1)
        cache.set("k", "v")
        assert cache.get("k") is None

    def test_missing_key(self):
        cache = checker._TTLCache(maxsize=10, ttl_seconds=1000)
        assert cache.get("nope") is None

    def test_eviction_keeps_size_bounded(self):
        cache = checker._TTLCache(maxsize=2, ttl_seconds=1000)
        for i in range(5):
            cache.set(f"k{i}", i)
        assert len(cache._store) <= 2


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
