"""Unit tests for src/vrc_session.py.

These moved here wholesale from test_checker.py when the session machinery
was extracted so a second VRChat account could hold its own session. The
assertions are unchanged; what changed is that the account -- credentials,
cookie file, 2FA mailbox -- is now passed in rather than read from module
globals, which is also why these tests no longer monkeypatch configuration.

The tests at the end are new, and exist because "two accounts cannot see each
other's state" is the entire reason this module was split out.
"""

from http.cookiejar import Cookie, MozillaCookieJar
from types import SimpleNamespace

import pytest

import vrc_session as vrcs


NO_OUTAGE_STATUS = {
    "vrchat_outage": False,
    "vrchat_outage_confirmed": False,
    "vrchat_status_message": None,
    "vrchat_status_indicator": None,
}

USER_AGENT = "VRCVerifyTest/1.0 contact@esattotech.com"


class FakeApiException(vrcs.ApiException):
    def __init__(self, status=None, reason="", body=""):
        self.status = status
        self.reason = reason
        self.body = body


def account(session_file="", label="test") -> vrcs.VRChatAccount:
    return vrcs.VRChatAccount(
        username="bot",
        password="hunter2",
        user_agent=USER_AGENT,
        session_file=str(session_file),
        gmail_user="bot@example.com",
        gmail_app_password="app-password",
        label=label,
    )


@pytest.fixture(autouse=True)
def no_status_page(monkeypatch):
    """Never hit status.vrchat.com from tests."""
    monkeypatch.setattr(vrcs, "fetch_status_summary", lambda force_refresh=False: None)


# ---------------------------------------------------------------
# Error classification
# ---------------------------------------------------------------
class TestClassifyVrchatApiError:
    @pytest.mark.parametrize("status", [500, 502, 503, 504])
    def test_upstream_errors(self, status):
        meta = vrcs.classify_api_error(FakeApiException(status=status, reason="Server Error"))
        assert meta["error_type"] == "vrchat_upstream_error"
        assert meta["vrchat_outage"] is True
        assert meta["lookup_ok"] is False

    def test_rate_limited(self):
        meta = vrcs.classify_api_error(FakeApiException(status=429, reason="Too Many Requests"))
        assert meta["error_type"] == "vrchat_rate_limited"
        assert meta["vrchat_outage"] is False

    @pytest.mark.parametrize("status", [401, 403])
    def test_auth_errors(self, status):
        meta = vrcs.classify_api_error(FakeApiException(status=status, reason="Unauthorized"))
        assert meta["error_type"] == "vrchat_auth_error"

    def test_user_not_found(self):
        meta = vrcs.classify_api_error(FakeApiException(status=404, reason="Not Found"))
        assert meta["error_type"] == "vrchat_user_not_found"

    def test_timeout_from_message(self):
        meta = vrcs.classify_api_error(Exception("connection timed out"))
        assert meta["error_type"] == "vrchat_timeout"
        assert meta["vrchat_outage"] is True

    def test_unknown_error(self):
        meta = vrcs.classify_api_error(Exception("weird failure"))
        assert meta["error_type"] == "vrchat_error"
        assert meta["vrchat_outage"] is False

    def test_long_reason_truncated(self):
        meta = vrcs.classify_api_error(Exception("x" * 2000))
        assert len(meta["error_message"]) <= 500


# ---------------------------------------------------------------
# Status page parsing
# ---------------------------------------------------------------
class TestExtractRelevantVrchatStatus:
    def test_no_summary_available(self):
        status = vrcs.extract_relevant_status()
        assert status == NO_OUTAGE_STATUS

    def test_all_operational(self, monkeypatch):
        summary = {
            "status": {"indicator": "none"},
            "incidents": [],
            "components": [{"name": "API", "status": "operational"}],
        }
        monkeypatch.setattr(vrcs, "fetch_status_summary", lambda force_refresh=False: summary)
        status = vrcs.extract_relevant_status()
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
        monkeypatch.setattr(vrcs, "fetch_status_summary", lambda force_refresh=False: summary)
        status = vrcs.extract_relevant_status()
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
        monkeypatch.setattr(vrcs, "fetch_status_summary", lambda force_refresh=False: summary)
        status = vrcs.extract_relevant_status()
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
        monkeypatch.setattr(vrcs, "fetch_status_summary", lambda force_refresh=False: summary)
        status = vrcs.extract_relevant_status()
        assert status["vrchat_outage_confirmed"] is False


# ---------------------------------------------------------------
# Login robustness
# ---------------------------------------------------------------
class TestLoginRobustness:
    """Nothing may escape login() into the relogin thread.

    VRChatSession.relogin_loop is the ONLY thing that can recover a lost
    session (the main thread is blocked consuming RabbitMQ). If an exception
    kills that thread, every request fails until the container is restarted.
    """

    def test_none_reason_on_2fa_challenge_does_not_raise(self, monkeypatch):
        """`"..." in e.reason` raised TypeError when reason was None.

        It raised from INSIDE the except UnauthorizedException handler, so it
        escaped login() and would have killed the relogin thread.
        """
        monkeypatch.setattr(
            vrcs.vrchatapi, "Configuration",
            lambda **k: SimpleNamespace(host="https://api.vrchat.cloud/api/1"),
        )
        monkeypatch.setattr(
            vrcs.vrchatapi, "ApiClient",
            lambda cfg: SimpleNamespace(
                user_agent="", rest_client=SimpleNamespace(cookie_jar=None)
            ),
        )
        exc = vrcs.UnauthorizedException(status=200)
        exc.reason = None  # VRChat gave us no reason string

        def get_current_user(**kwargs):
            raise exc

        monkeypatch.setattr(
            vrcs.authentication_api, "AuthenticationApi",
            lambda c: SimpleNamespace(get_current_user=get_current_user),
        )
        monkeypatch.setattr(vrcs, "fetch_latest_2fa_code", lambda acct: None)

        # Must return a structured error, not raise.
        client, err = vrcs.login(account())
        assert client is None
        assert err["error_type"] == "vrchat_auth_error"

    def test_relogin_loop_body_guards_exceptions(self):
        """The retry call must be wrapped so the thread cannot die."""
        import inspect

        src = inspect.getsource(vrcs.VRChatSession.relogin_loop)
        assert "try:" in src and "except Exception" in src, (
            "attempt_login must be guarded; an unguarded raise kills "
            "the only thread that can recover a VRChat session"
        )


# ---------------------------------------------------------------
# Session persistence
# ---------------------------------------------------------------
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

    def test_disabled_when_unset(self):
        assert vrcs.attach_session_store(account(""), self._client()) is None

    def test_real_cookie_roundtrips_and_would_be_sent(self, tmp_path):
        """A REAL cookie must survive save+load and still be sendable.

        The previous version of this test persisted an EMPTY jar, so it passed
        on nothing but the Netscape header -- it stayed green even if
        ignore_discard was dropped, which silently discards the session
        cookie that makes 2FA-skipping work.
        """
        acct = account(tmp_path / "session.txt")

        client = self._client()
        jar = vrcs.attach_session_store(acct, client)
        assert client.rest_client.cookie_jar is jar
        jar.set_cookie(self._cookie())
        # session cookie (expires=None) -- only kept if ignore_discard is used
        jar.set_cookie(self._cookie("twoFactorAuth", "tfa_" + "B" * 40, None))
        vrcs.persist_session(acct, jar)

        reloaded = vrcs.attach_session_store(acct, self._client())
        names = sorted(c.name for c in reloaded)
        assert names == ["auth", "twoFactorAuth"], f"lost cookies: {names}"
        assert vrcs.jar_sends_cookies(reloaded, self.HOST) is True

    def test_torn_file_yields_no_cookies(self, tmp_path):
        """A half-written file must not leave a TRUNCATED cookie behind.

        MozillaCookieJar.save truncates in place and load() inserts every row
        it parsed before raising, so a torn file used to come back holding a
        partial auth cookie while the log claimed it was ignored.
        """
        path = tmp_path / "session.txt"
        acct = account(path)
        jar = vrcs.attach_session_store(acct, self._client())
        jar.set_cookie(self._cookie())
        jar.set_cookie(self._cookie("twoFactorAuth", "tfa_" + "B" * 40, 2000000000))
        vrcs.persist_session(acct, jar)

        raw = path.read_bytes()
        path.write_bytes(raw[: int(len(raw) * 0.8)])  # simulate a torn write

        reloaded = vrcs.attach_session_store(acct, self._client())
        assert reloaded is not None
        assert len(reloaded) == 0, "partial cookies survived a torn file"
        assert vrcs.jar_sends_cookies(reloaded, self.HOST) is False

    def test_persist_is_atomic_no_tmp_left_behind(self, tmp_path):
        path = tmp_path / "session.txt"
        acct = account(path)
        jar = vrcs.attach_session_store(acct, self._client())
        jar.set_cookie(self._cookie())
        vrcs.persist_session(acct, jar)
        assert path.exists()
        assert not (tmp_path / "session.txt.tmp").exists()

    def test_expired_only_jar_is_not_a_live_session(self, tmp_path):
        """len(jar) counts expired cookies; they are never actually sent.

        Treating that as a live session made the ordinary "2FA required"
        response look like a rejected cookie.
        """
        acct = account(tmp_path / "session.txt")
        jar = vrcs.attach_session_store(acct, self._client())
        jar.set_cookie(self._cookie(expires=1))  # long expired
        vrcs.persist_session(acct, jar)

        reloaded = vrcs.attach_session_store(acct, self._client())
        assert len(reloaded) == 1  # loaded with ignore_expires
        assert vrcs.jar_sends_cookies(reloaded, self.HOST) is False

    def test_jar_sends_cookies_handles_none_and_empty(self):
        assert vrcs.jar_sends_cookies(None, self.HOST) is False
        assert vrcs.jar_sends_cookies(MozillaCookieJar(), self.HOST) is False

    def test_persist_failure_is_swallowed(self, monkeypatch, tmp_path):
        acct = account(tmp_path / "s.txt")
        jar = vrcs.attach_session_store(acct, self._client())

        def boom(*a, **k):
            raise OSError("read-only filesystem")

        monkeypatch.setattr(jar, "save", boom)
        vrcs.persist_session(acct, jar)  # must not raise

    def test_persist_noop_when_disabled(self):
        vrcs.persist_session(account(""), None)  # must not raise

    def test_discard_removes_file(self, tmp_path):
        path = tmp_path / "session.txt"
        path.write_text("x", encoding="utf-8")
        acct = account(path)
        vrcs.discard_stored_session(acct)
        assert not path.exists()
        vrcs.discard_stored_session(acct)  # already gone: still must not raise

    def _patch_login_stack(self, monkeypatch, get_current_user, cookies_on_auth=None):
        """Fake ApiClient/AuthenticationApi but keep the REAL jar and file.

        Deliberately does NOT stub attach_session_store/persist_session --
        the earlier version did, so the real jar and file were never exercised
        and the tests proved nothing about persistence.
        """
        monkeypatch.setattr(
            vrcs.vrchatapi,
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

        monkeypatch.setattr(vrcs.vrchatapi, "ApiClient", make_client)

        def make_auth(client):
            def _get(**kwargs):
                result = get_current_user(**kwargs)
                # Simulate VRChat issuing fresh cookies on a successful login.
                if cookies_on_auth:
                    for c in cookies_on_auth:
                        client.rest_client.cookie_jar.set_cookie(c)
                return result

            return SimpleNamespace(get_current_user=_get)

        monkeypatch.setattr(vrcs.authentication_api, "AuthenticationApi", make_auth)
        monkeypatch.setattr(
            vrcs, "fetch_latest_2fa_code",
            lambda acct: (_ for _ in ()).throw(AssertionError("unexpected 2FA")),
        )
        return created

    def test_rejected_session_still_persists_the_new_one(self, monkeypatch, tmp_path):
        """The retry after a rejected cookie MUST still store its session.

        Regression: the retry passed use_stored_session=False, which also
        skipped attaching the jar, so persist_session got None. The stale
        file had already been deleted -- so a rejected session left NOTHING
        on disk and the next restart burned a 2FA email, defeating the whole
        feature in exactly the case it exists for. The previous test actually
        asserted the broken behaviour (`attach == 1`).
        """
        path = tmp_path / "session.txt"
        acct = account(path)

        # Seed a stored session that VRChat will reject.
        seed = MozillaCookieJar(str(path))
        seed.set_cookie(self._cookie("auth", "stale_" + "C" * 40))
        seed.save(ignore_discard=True, ignore_expires=True)
        assert path.exists()

        n = {"calls": 0}

        def get_current_user(**kwargs):
            n["calls"] += 1
            if n["calls"] == 1:
                raise vrcs.UnauthorizedException(status=401)
            return SimpleNamespace(display_name="ClubLA Bot")

        fresh = self._cookie("auth", "fresh_" + "D" * 40)
        self._patch_login_stack(monkeypatch, get_current_user, cookies_on_auth=[fresh])

        client, err = vrcs.login(acct)
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
        acct = account(path)
        seed = MozillaCookieJar(str(path))
        seed.set_cookie(self._cookie("auth", "expired_" + "E" * 40, expires=1))
        seed.save(ignore_discard=True, ignore_expires=True)

        n = {"calls": 0, "discards": 0}
        monkeypatch.setattr(
            vrcs, "discard_stored_session",
            lambda acct: n.__setitem__("discards", n["discards"] + 1),
        )

        def get_current_user(**kwargs):
            n["calls"] += 1
            if n["calls"] == 1:
                raise vrcs.UnauthorizedException(status=200)
            return SimpleNamespace(display_name="ClubLA Bot")

        fresh = self._cookie("auth", "fresh_" + "F" * 40)
        self._patch_login_stack(monkeypatch, get_current_user, cookies_on_auth=[fresh])
        monkeypatch.setattr(vrcs, "fetch_latest_2fa_code", lambda acct: "123456")
        monkeypatch.setattr(
            vrcs.authentication_api, "AuthenticationApi",
            lambda c: SimpleNamespace(
                get_current_user=lambda **k: (
                    get_current_user(**k),
                    c.rest_client.cookie_jar.set_cookie(fresh),
                )[0],
                verify2_fa_email_code=lambda code, **k: None,
                verify2_fa=lambda code, **k: None,
            ),
        )

        client, err = vrcs.login(acct)
        assert err is None and client is not None
        assert n["discards"] == 0, "2FA challenge was misread as a bad cookie"

    def test_stored_session_skips_2fa_entirely(self, monkeypatch, tmp_path):
        """A live stored cookie must authenticate with no 2FA at all."""
        path = tmp_path / "session.txt"
        acct = account(path)
        seed = MozillaCookieJar(str(path))
        seed.set_cookie(self._cookie())
        seed.save(ignore_discard=True, ignore_expires=True)

        # _patch_login_stack makes fetch_latest_2fa_code raise if called.
        self._patch_login_stack(
            monkeypatch, lambda **k: SimpleNamespace(display_name="ClubLA Bot")
        )
        client, err = vrcs.login(acct)
        assert err is None and client is not None
        assert path.exists()  # session refreshed on disk


# ---------------------------------------------------------------
# Two accounts in one process
#
# New with the extraction. These are the properties the whole split exists
# to provide, and none of them held while the session lived in module
# globals.
# ---------------------------------------------------------------
class TestAccountIsolation:
    def test_sessions_do_not_share_state(self):
        first = vrcs.VRChatSession(account(label="checker"))
        second = vrcs.VRChatSession(account(label="inviter"))

        sentinel = object()
        first._set_state(sentinel, None)

        assert first.get()[0] is sentinel
        assert second.get()[0] is None, "a second account saw the first one's client"
        assert second.get()[1]["error_type"] == "vrchat_session_unavailable"

    def test_invalidating_one_leaves_the_other_logged_in(self):
        first = vrcs.VRChatSession(account(label="checker"))
        second = vrcs.VRChatSession(account(label="inviter"))
        first._set_state(object(), None)
        second._set_state(object(), None)

        second.invalidate()

        assert first.get()[0] is not None, "invalidating one account logged the other out"
        assert second.get()[0] is None

    def test_each_account_persists_to_its_own_file(self, tmp_path):
        """Sharing one session file would have the accounts overwrite each
        other's auth cookie and thrash between sessions."""
        first = account(tmp_path / "checker.txt", label="checker")
        second = account(tmp_path / "inviter.txt", label="inviter")

        jar = vrcs.attach_session_store(
            first, SimpleNamespace(rest_client=SimpleNamespace(cookie_jar=None))
        )
        jar.set_cookie(TestSessionPersistence._cookie("auth", "checker_" + "A" * 40))
        vrcs.persist_session(first, jar)

        other = vrcs.attach_session_store(
            second, SimpleNamespace(rest_client=SimpleNamespace(cookie_jar=None))
        )
        assert len(other) == 0, "the second account loaded the first account's cookie"

    def test_from_env_prefix_does_not_fall_back_to_the_bare_names(self, monkeypatch):
        """A prefixed account must never inherit the checker's credentials.

        Silently falling back would log the invite bot in AS the age checker,
        which is precisely the separation this feature is built around.
        """
        monkeypatch.setenv("VRCHAT_USERNAME", "age-checker")
        monkeypatch.setenv("VRCHAT_PASSWORD", "checker-secret")
        monkeypatch.delenv("INVITE_VRCHAT_USERNAME", raising=False)
        monkeypatch.delenv("INVITE_VRCHAT_PASSWORD", raising=False)

        acct = vrcs.VRChatAccount.from_env(
            user_agent=USER_AGENT, prefix="INVITE_", label="inviter"
        )
        assert acct.username is None
        assert acct.password is None

        monkeypatch.setenv("INVITE_VRCHAT_USERNAME", "invite-bot")
        acct = vrcs.VRChatAccount.from_env(
            user_agent=USER_AGENT, prefix="INVITE_", label="inviter"
        )
        assert acct.username == "invite-bot"


class TestConfigurationIsActuallyLoaded:
    """.env must be loaded before this module freezes its constants.

    Regression: when this machinery was extracted, the importing service
    still called load_dotenv() in its own body -- which runs after the import
    has already evaluated every os.getenv below. Timeouts, the relogin
    interval and the status-page settings all silently fell back to their
    defaults. Docker hid it (compose passes the environment directly), so it
    would only have shown up on a bare-metal run, as settings that quietly
    did nothing.
    """

    def test_dotenv_is_loaded_before_the_constants_are_read(self):
        import inspect

        src = inspect.getsource(vrcs)
        load_at = src.find("load_dotenv()")
        first_getenv = src.find("os.getenv(")
        assert load_at != -1, "vrc_session must load .env itself, not rely on its importer"
        assert load_at < first_getenv, (
            "load_dotenv() must run before the module-level os.getenv calls; "
            "below them it is too late and every setting silently defaults"
        )

    def test_settings_come_from_the_environment(self, monkeypatch, tmp_path):
        """The constants are read at import time, so re-import to observe it."""
        import importlib

        monkeypatch.setenv("VRCHAT_RELOGIN_INTERVAL_SECONDS", "1234")
        monkeypatch.setenv("VRCHAT_API_READ_TIMEOUT_SECONDS", "7.5")
        reloaded = importlib.reload(vrcs)
        try:
            assert reloaded.VRCHAT_RELOGIN_INTERVAL_SECONDS == 1234
            assert reloaded.VRCHAT_API_READ_TIMEOUT_SECONDS == 7.5
        finally:
            monkeypatch.undo()
            importlib.reload(vrcs)
