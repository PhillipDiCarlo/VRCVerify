"""Unit tests for src/vrc_online_checker.py logic.

All network paths (VRChat login, status page, RabbitMQ) are monkeypatched;
these tests exercise pure logic: bio code matching, error classification,
outage detection from status-page summaries, result payload shape, the
/profile-vs-/users lookup preference, and verify_and_build_result with a
faked VRChat session.
"""

import json
import os
from http.cookiejar import Cookie, MozillaCookieJar
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


class TestLoginRobustness:
    """Nothing may escape login_to_vrchat into the relogin thread.

    _vrchat_relogin_loop is the ONLY thing that can recover a lost session
    (the main thread is blocked consuming RabbitMQ). If an exception kills
    that thread, every verification fails until the container is restarted.
    """

    def test_none_reason_on_2fa_challenge_does_not_raise(self, monkeypatch):
        """`"..." in e.reason` raised TypeError when reason was None.

        It raised from INSIDE the except UnauthorizedException handler, so it
        escaped login_to_vrchat and would have killed the relogin thread.
        """
        monkeypatch.setattr(checker, "VRCHAT_SESSION_FILE", "")
        monkeypatch.setattr(
            checker.vrchatapi, "Configuration",
            lambda **k: SimpleNamespace(host="https://api.vrchat.cloud/api/1"),
        )
        monkeypatch.setattr(
            checker.vrchatapi, "ApiClient",
            lambda cfg: SimpleNamespace(
                user_agent="", rest_client=SimpleNamespace(cookie_jar=None)
            ),
        )
        exc = checker.UnauthorizedException(status=200)
        exc.reason = None  # VRChat gave us no reason string

        def get_current_user(**kwargs):
            raise exc

        monkeypatch.setattr(
            checker.authentication_api, "AuthenticationApi",
            lambda c: SimpleNamespace(get_current_user=get_current_user),
        )
        monkeypatch.setattr(checker, "fetch_latest_2fa_code", lambda: None)

        # Must return a structured error, not raise.
        client, err = checker.login_to_vrchat()
        assert client is None
        assert err["error_type"] == "vrchat_auth_error"

    def test_relogin_loop_body_guards_exceptions(self):
        """The retry call must be wrapped so the thread cannot die."""
        import inspect

        src = inspect.getsource(checker._vrchat_relogin_loop)
        assert "try:" in src and "except Exception" in src, (
            "attempt_vrchat_login must be guarded; an unguarded raise kills "
            "the only thread that can recover a VRChat session"
        )


class TestSessionPersistence:
    """Storing the auth cookie keeps restarts from re-authenticating.

    Every fresh login burns a 2FA email and VRChat rate-limits that endpoint,
    so a few quick redeploys can lock the account out and take verification
    down. None of this may ever block a login, hence the tolerance tests.
    """

    HOST = "https://api.vrchat.cloud/api/1"

    def _client(self):
        return SimpleNamespace(rest_client=SimpleNamespace(cookie_jar=object()))

    @staticmethod
    def _cookie(name="auth", value="authcookie_" + "A" * 40, expires=2000000000):
        return Cookie(
            0, name, value, None, False, "api.vrchat.cloud", True, False,
            "/", True, True, expires, False, None, None, {},
        )

    def test_disabled_when_unset(self, monkeypatch):
        monkeypatch.setattr(checker, "VRCHAT_SESSION_FILE", "")
        assert checker._attach_session_store(self._client()) is None

    def test_real_cookie_roundtrips_and_would_be_sent(self, monkeypatch, tmp_path):
        """A REAL cookie must survive save+load and still be sendable.

        The previous version of this test persisted an EMPTY jar, so it passed
        on nothing but the Netscape header -- it stayed green even if
        ignore_discard was dropped, which silently discards the session
        cookie that makes 2FA-skipping work.
        """
        path = tmp_path / "session.txt"
        monkeypatch.setattr(checker, "VRCHAT_SESSION_FILE", str(path))

        client = self._client()
        jar = checker._attach_session_store(client)
        assert client.rest_client.cookie_jar is jar
        jar.set_cookie(self._cookie())
        # session cookie (expires=None) -- only kept if ignore_discard is used
        jar.set_cookie(self._cookie("twoFactorAuth", "tfa_" + "B" * 40, None))
        checker._persist_session(jar)

        reloaded = checker._attach_session_store(self._client())
        names = sorted(c.name for c in reloaded)
        assert names == ["auth", "twoFactorAuth"], f"lost cookies: {names}"
        assert checker._jar_sends_cookies(reloaded, self.HOST) is True

    def test_torn_file_yields_no_cookies(self, monkeypatch, tmp_path):
        """A half-written file must not leave a TRUNCATED cookie behind.

        MozillaCookieJar.save truncates in place and load() inserts every row
        it parsed before raising, so a torn file used to come back holding a
        partial auth cookie while the log claimed it was ignored.
        """
        path = tmp_path / "session.txt"
        monkeypatch.setattr(checker, "VRCHAT_SESSION_FILE", str(path))
        jar = checker._attach_session_store(self._client())
        jar.set_cookie(self._cookie())
        jar.set_cookie(self._cookie("twoFactorAuth", "tfa_" + "B" * 40, 2000000000))
        checker._persist_session(jar)

        raw = path.read_bytes()
        path.write_bytes(raw[: int(len(raw) * 0.8)])  # simulate a torn write

        reloaded = checker._attach_session_store(self._client())
        assert reloaded is not None
        assert len(reloaded) == 0, "partial cookies survived a torn file"
        assert checker._jar_sends_cookies(reloaded, self.HOST) is False

    def test_persist_is_atomic_no_tmp_left_behind(self, monkeypatch, tmp_path):
        path = tmp_path / "session.txt"
        monkeypatch.setattr(checker, "VRCHAT_SESSION_FILE", str(path))
        jar = checker._attach_session_store(self._client())
        jar.set_cookie(self._cookie())
        checker._persist_session(jar)
        assert path.exists()
        assert not (tmp_path / "session.txt.tmp").exists()

    def test_expired_only_jar_is_not_a_live_session(self, monkeypatch, tmp_path):
        """len(jar) counts expired cookies; they are never actually sent.

        Treating that as a live session made the ordinary "2FA required"
        response look like a rejected cookie.
        """
        path = tmp_path / "session.txt"
        monkeypatch.setattr(checker, "VRCHAT_SESSION_FILE", str(path))
        jar = checker._attach_session_store(self._client())
        jar.set_cookie(self._cookie(expires=1))  # long expired
        checker._persist_session(jar)

        reloaded = checker._attach_session_store(self._client())
        assert len(reloaded) == 1  # loaded with ignore_expires
        assert checker._jar_sends_cookies(reloaded, self.HOST) is False

    def test_jar_sends_cookies_handles_none_and_empty(self):
        assert checker._jar_sends_cookies(None, self.HOST) is False
        assert checker._jar_sends_cookies(MozillaCookieJar(), self.HOST) is False

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

    def _patch_login_stack(self, monkeypatch, get_current_user, cookies_on_auth=None):
        """Fake ApiClient/AuthenticationApi but keep the REAL jar and file.

        Deliberately does NOT stub _attach_session_store/_persist_session --
        the earlier version did, so the real jar and file were never exercised
        and the tests proved nothing about persistence.
        """
        monkeypatch.setattr(
            checker.vrchatapi,
            "Configuration",
            lambda **k: SimpleNamespace(host=self.HOST),
        )
        created = []

        def make_client(cfg):
            c = SimpleNamespace(
                user_agent="", rest_client=SimpleNamespace(cookie_jar=None)
            )
            created.append(c)
            return c

        monkeypatch.setattr(checker.vrchatapi, "ApiClient", make_client)

        def make_auth(client):
            def _get(**kwargs):
                result = get_current_user(**kwargs)
                # Simulate VRChat issuing fresh cookies on a successful login.
                if cookies_on_auth:
                    for c in cookies_on_auth:
                        client.rest_client.cookie_jar.set_cookie(c)
                return result

            return SimpleNamespace(get_current_user=_get)

        monkeypatch.setattr(checker.authentication_api, "AuthenticationApi", make_auth)
        monkeypatch.setattr(
            checker, "fetch_latest_2fa_code",
            lambda: (_ for _ in ()).throw(AssertionError("unexpected 2FA")),
        )
        return created

    def test_rejected_session_still_persists_the_new_one(self, monkeypatch, tmp_path):
        """The retry after a rejected cookie MUST still store its session.

        Regression: the retry passed use_stored_session=False, which also
        skipped attaching the jar, so _persist_session got None. The stale
        file had already been deleted -- so a rejected session left NOTHING
        on disk and the next restart burned a 2FA email, defeating the whole
        feature in exactly the case it exists for. The previous test actually
        asserted the broken behaviour (`attach == 1`).
        """
        path = tmp_path / "session.txt"
        monkeypatch.setattr(checker, "VRCHAT_SESSION_FILE", str(path))

        # Seed a stored session that VRChat will reject.
        seed = MozillaCookieJar(str(path))
        seed.set_cookie(self._cookie("auth", "stale_" + "C" * 40))
        seed.save(ignore_discard=True, ignore_expires=True)
        assert path.exists()

        n = {"calls": 0}

        def get_current_user(**kwargs):
            n["calls"] += 1
            if n["calls"] == 1:
                raise checker.UnauthorizedException(status=401)
            return SimpleNamespace(display_name="ClubLA Bot")

        fresh = self._cookie("auth", "fresh_" + "D" * 40)
        self._patch_login_stack(monkeypatch, get_current_user, cookies_on_auth=[fresh])

        client, err = checker.login_to_vrchat()
        assert err is None and client is not None
        assert n["calls"] == 2  # retried exactly once, no loop

        assert path.exists(), "retry login left no stored session"
        after = MozillaCookieJar(str(path))
        after.load(ignore_discard=True, ignore_expires=True)
        values = [c.value for c in after]
        assert any(v.startswith("fresh_") for v in values), values
        assert not any(v.startswith("stale_") for v in values), values

    def test_2fa_required_is_handled_in_place_not_treated_as_bad_cookie(
        self, monkeypatch, tmp_path
    ):
        """status==200 means "do 2FA", not "your stored cookie is bad".

        With an expired-only store, reused_session used to be True (len counts
        expired cookies), so the ordinary 2FA challenge was misread as a
        rejected cookie: the file got deleted and a second round trip spent.
        """
        path = tmp_path / "session.txt"
        monkeypatch.setattr(checker, "VRCHAT_SESSION_FILE", str(path))
        seed = MozillaCookieJar(str(path))
        seed.set_cookie(self._cookie("auth", "expired_" + "E" * 40, expires=1))
        seed.save(ignore_discard=True, ignore_expires=True)

        n = {"calls": 0, "discards": 0}
        monkeypatch.setattr(
            checker, "_discard_stored_session",
            lambda: n.__setitem__("discards", n["discards"] + 1),
        )

        def get_current_user(**kwargs):
            n["calls"] += 1
            if n["calls"] == 1:
                raise checker.UnauthorizedException(status=200)
            return SimpleNamespace(display_name="ClubLA Bot")

        fresh = self._cookie("auth", "fresh_" + "F" * 40)
        self._patch_login_stack(monkeypatch, get_current_user, cookies_on_auth=[fresh])
        monkeypatch.setattr(checker, "fetch_latest_2fa_code", lambda: "123456")
        monkeypatch.setattr(
            checker.authentication_api, "AuthenticationApi",
            lambda c: SimpleNamespace(
                get_current_user=lambda **k: (
                    get_current_user(**k),
                    c.rest_client.cookie_jar.set_cookie(fresh),
                )[0],
                verify2_fa_email_code=lambda code, **k: None,
                verify2_fa=lambda code, **k: None,
            ),
        )

        client, err = checker.login_to_vrchat()
        assert err is None and client is not None
        assert n["discards"] == 0, "2FA challenge was misread as a bad cookie"

    def test_stored_session_skips_2fa_entirely(self, monkeypatch, tmp_path):
        """A live stored cookie must authenticate with no 2FA at all."""
        path = tmp_path / "session.txt"
        monkeypatch.setattr(checker, "VRCHAT_SESSION_FILE", str(path))
        seed = MozillaCookieJar(str(path))
        seed.set_cookie(self._cookie())
        seed.save(ignore_discard=True, ignore_expires=True)

        # _patch_login_stack makes fetch_latest_2fa_code raise if called.
        self._patch_login_stack(
            monkeypatch, lambda **k: SimpleNamespace(display_name="ClubLA Bot")
        )
        client, err = checker.login_to_vrchat()
        assert err is None and client is not None
        assert path.exists()  # session refreshed on disk


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
