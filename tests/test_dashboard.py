"""Unit tests for the web dashboard (issue #65, step 3).

This is the internet-facing half of the system, so the tests are mostly about
the ways a login can be subverted rather than the happy path:

- the Discord access token must never be persisted anywhere
- a callback whose `state` doesn't match ours is refused
- the session id changes at the moment privilege is granted
- authority is never taken from OAuth data or from Cf-Access-* headers
- the app refuses to start on a weak or missing secret
- nothing here can write: the read-only phase is pinned
"""

import json
import sqlite3
import time
from types import SimpleNamespace

import pytest

pytest.importorskip("flask")

from dashboard import oauth  # noqa: E402
from dashboard.app import CSP, SESSION_COOKIE, create_app  # noqa: E402
from dashboard.botapi import BotAPIError  # noqa: E402
from dashboard.config import DashboardConfig, DashboardConfigError  # noqa: E402
from dashboard.sessions import SessionStore  # noqa: E402

ACTOR = "424242424242"
GUILD_IN = "111111111111"
GUILD_OUT = "222222222222"
GUILD_NOT_ADMIN = "333333333333"

SIGNING_KEY = "s" * 48
SECRET_KEY = "k" * 48
ACCESS_TOKEN = "discord-access-token-that-must-never-be-stored"


# -------------------------------------------------------------------
# Fixtures
# -------------------------------------------------------------------
@pytest.fixture
def certs(tmp_path):
    for name in ("client.pem", "client.key", "ca.pem"):
        (tmp_path / name).write_text("placeholder")
    return tmp_path


@pytest.fixture
def config(tmp_path, certs):
    return DashboardConfig(
        discord_client_id="1335738139825799188",
        discord_client_secret="client-secret",
        oauth_redirect_uri="https://dashboard.vrcverify.com/callback",
        secret_key=SECRET_KEY,
        session_db_path=str(tmp_path / "sessions.db"),
        bot_api_url="https://100.117.6.99:5002",
        bot_api_client_cert=str(certs / "client.pem"),
        bot_api_client_key=str(certs / "client.key"),
        bot_api_ca=str(certs / "ca.pem"),
        bot_api_signing_key=SIGNING_KEY.encode(),
    )


class FakeBotAPI:
    """Stands in for the bot. Records what it was asked."""

    def __init__(self, installed=(GUILD_IN,), fail=False):
        self.installed = {str(g) for g in installed}
        self.fail = fail
        self.calls = []

    def admin_guild_ids(self, actor_id, guild_ids):
        self.calls.append((actor_id, list(guild_ids)))
        if self.fail:
            raise BotAPIError("bot unreachable")
        return {g for g in map(str, guild_ids) if g in self.installed}


@pytest.fixture
def store(config):
    return SessionStore(config.session_db_path, config.session_max_age)


@pytest.fixture
def bot_api():
    return FakeBotAPI()


@pytest.fixture
def app(config, store, bot_api):
    application = create_app(config, store=store, client=bot_api)
    application.config.update(TESTING=True)
    return application


@pytest.fixture
def client(app):
    return app.test_client()


GUILDS = [
    {"id": GUILD_IN, "name": "Alpha Club", "icon": "abc123", "admin_hint": True},
    {"id": GUILD_OUT, "name": "Beta Lounge", "icon": None, "admin_hint": True},
    {"id": GUILD_NOT_ADMIN, "name": "Gamma Hall", "icon": None, "admin_hint": False},
]


def login_as(client, store, guilds=None):
    """Put an authenticated session in place without walking the OAuth flow."""
    pending = store.begin_login("unused-state")
    session = store.complete_login(pending.sid, ACTOR, guilds or GUILDS)
    client.set_cookie(SESSION_COOKIE, session.sid, domain="localhost")
    return session


# -------------------------------------------------------------------
# Configuration
# -------------------------------------------------------------------
class TestConfig:
    @pytest.fixture(autouse=True)
    def clean_env(self, monkeypatch):
        for name in (
            "DISCORD_CLIENT_ID",
            "DISCORD_CLIENT_SECRET",
            "OAUTH_REDIRECT_URI",
            "DASHBOARD_SECRET_KEY",
            "BOT_API_URL",
            "BOT_API_CLIENT_CERT",
            "BOT_API_CLIENT_KEY",
            "BOT_API_CA",
            "BOT_API_TOKEN_SIGNING_KEY",
            "SESSION_DB_PATH",
        ):
            monkeypatch.delenv(name, raising=False)

    def env(self, monkeypatch, certs, **overrides):
        values = {
            "DISCORD_CLIENT_ID": "123",
            "DISCORD_CLIENT_SECRET": "shh",
            "OAUTH_REDIRECT_URI": "https://dashboard.vrcverify.com/callback",
            "DASHBOARD_SECRET_KEY": SECRET_KEY,
            "BOT_API_URL": "https://100.117.6.99:5002",
            "BOT_API_CLIENT_CERT": str(certs / "client.pem"),
            "BOT_API_CLIENT_KEY": str(certs / "client.key"),
            "BOT_API_CA": str(certs / "ca.pem"),
            "BOT_API_TOKEN_SIGNING_KEY": SIGNING_KEY,
        }
        values.update(overrides)
        for name, value in values.items():
            if value is None:
                monkeypatch.delenv(name, raising=False)
            else:
                monkeypatch.setenv(name, value)

    def test_happy_path(self, monkeypatch, certs):
        self.env(monkeypatch, certs)
        assert DashboardConfig.from_env().discord_client_id == "123"

    @pytest.mark.parametrize(
        "missing",
        [
            "DISCORD_CLIENT_ID",
            "DISCORD_CLIENT_SECRET",
            "OAUTH_REDIRECT_URI",
            "DASHBOARD_SECRET_KEY",
            "BOT_API_URL",
            "BOT_API_TOKEN_SIGNING_KEY",
        ],
    )
    def test_missing_settings_refuse_to_start(self, monkeypatch, certs, missing):
        self.env(monkeypatch, certs, **{missing: None})
        with pytest.raises(DashboardConfigError):
            DashboardConfig.from_env()

    def test_a_weak_session_key_refuses_to_start(self, monkeypatch, certs):
        self.env(monkeypatch, certs, DASHBOARD_SECRET_KEY="short")
        with pytest.raises(DashboardConfigError):
            DashboardConfig.from_env()

    def test_reusing_one_key_for_both_purposes_refuses_to_start(
        self, monkeypatch, certs
    ):
        """Cookie signing and API authorisation are separate trust domains."""
        self.env(
            monkeypatch,
            certs,
            DASHBOARD_SECRET_KEY=SIGNING_KEY,
            BOT_API_TOKEN_SIGNING_KEY=SIGNING_KEY,
        )
        with pytest.raises(DashboardConfigError):
            DashboardConfig.from_env()

    def test_plaintext_bot_api_refuses_to_start(self, monkeypatch, certs):
        self.env(monkeypatch, certs, BOT_API_URL="http://100.117.6.99:5002")
        with pytest.raises(DashboardConfigError):
            DashboardConfig.from_env()

    def test_a_plaintext_redirect_refuses_to_start(self, monkeypatch, certs):
        """An authorisation code over http can be stolen in flight."""
        self.env(
            monkeypatch, certs, OAUTH_REDIRECT_URI="http://dashboard.vrcverify.com/callback"
        )
        with pytest.raises(DashboardConfigError):
            DashboardConfig.from_env()

    def test_localhost_http_is_allowed_for_development(self, monkeypatch, certs):
        self.env(monkeypatch, certs, OAUTH_REDIRECT_URI="http://localhost:8000/callback")
        assert DashboardConfig.from_env() is not None

    def test_a_missing_client_certificate_refuses_to_start(self, monkeypatch, certs):
        self.env(monkeypatch, certs, BOT_API_CLIENT_CERT=str(certs / "absent.pem"))
        with pytest.raises(DashboardConfigError):
            DashboardConfig.from_env()


# -------------------------------------------------------------------
# Sessions
# -------------------------------------------------------------------
class TestSessions:
    def test_a_pre_auth_session_is_not_authenticated(self, store):
        pending = store.begin_login("state-123")
        assert pending.authenticated is False
        assert store.load(pending.sid).oauth_state == "state-123"

    def test_completing_login_issues_a_new_id(self, store):
        """Session fixation: the id must change when privilege is granted."""
        pending = store.begin_login("state-123")
        session = store.complete_login(pending.sid, ACTOR, GUILDS)

        assert session.sid != pending.sid
        assert session.authenticated is True
        # And the old row is gone, not merely superseded.
        assert store.load(pending.sid) is None

    def test_expired_sessions_are_refused_and_removed(self, store):
        pending = store.begin_login("state-123")
        session = store.complete_login(pending.sid, ACTOR, GUILDS)
        later = time.time() + store.max_age + 1

        assert store.load(session.sid, now=later) is None
        assert store.load(session.sid) is None  # removed on sight

    def test_logout_destroys_the_row(self, store):
        pending = store.begin_login("s")
        session = store.complete_login(pending.sid, ACTOR, GUILDS)
        store.destroy(session.sid)
        assert store.load(session.sid) is None

    def test_guild_cache_freshness(self, store):
        pending = store.begin_login("s")
        session = store.complete_login(pending.sid, ACTOR, GUILDS)
        assert session.guilds_fresh(900) is True
        assert session.guilds_fresh(900, now=time.time() + 901) is False

    def test_purge_removes_only_expired_rows(self, store):
        live = store.complete_login(store.begin_login("a").sid, ACTOR, GUILDS)
        stale = store.begin_login("b")
        assert store.purge_expired(now=time.time() + 601) == 1
        assert store.load(live.sid) is not None
        assert store.load(stale.sid) is None


class TestTokenIsNeverStored:
    """The single most important property of the whole login flow.

    The public host is assumed to be compromised eventually. A stored Discord
    token would let whoever compromised it act as every user who ever logged
    in, indefinitely, against Discord itself. An id and a stale guild list are
    worth far less -- so the token is read once and dropped.
    """

    def test_login_returns_only_identity_and_guilds(self, monkeypatch):
        captured = {}

        def fake_exchange(code, **kwargs):
            captured["code"] = code
            return ACCESS_TOKEN

        def fake_identity(token, **kwargs):
            captured["token_seen"] = token
            return ACTOR, GUILDS

        monkeypatch.setattr(oauth, "exchange_code", fake_exchange)
        monkeypatch.setattr(oauth, "fetch_identity", fake_identity)

        result = oauth.login(
            "the-code",
            client_id="1",
            client_secret="2",
            redirect_uri="https://x/callback",
        )
        assert result == (ACTOR, GUILDS)
        # It was used, and it is not in what came back.
        assert captured["token_seen"] == ACCESS_TOKEN
        assert ACCESS_TOKEN not in json.dumps(result)

    def test_no_session_row_ever_contains_the_token(self, store, config):
        pending = store.begin_login("state")
        store.complete_login(pending.sid, ACTOR, GUILDS)

        with sqlite3.connect(config.session_db_path) as conn:
            dump = "".join(
                str(row) for row in conn.execute("SELECT * FROM sessions").fetchall()
            )
        assert ACCESS_TOKEN not in dump
        assert "access_token" not in dump
        assert "refresh_token" not in dump

    def test_the_session_schema_has_nowhere_to_put_one(self, store, config):
        with sqlite3.connect(config.session_db_path) as conn:
            columns = {
                row[1] for row in conn.execute("PRAGMA table_info(sessions)").fetchall()
            }
        assert not any("token" in c for c in columns if c != "csrf_token")


# -------------------------------------------------------------------
# The OAuth flow
# -------------------------------------------------------------------
class TestOAuthFlow:
    def test_login_redirects_to_discord_with_state(self, client, store):
        response = client.get("/login")
        assert response.status_code == 302
        location = response.headers["Location"]
        assert location.startswith("https://discord.com/oauth2/authorize")
        assert "scope=identify+guilds" in location
        assert "state=" in location

    def test_only_identity_and_guilds_are_requested(self):
        assert oauth.SCOPES == "identify guilds"

    def test_a_mismatched_state_is_refused(self, client, store, monkeypatch):
        """Without this check, an attacker can have you log in as them."""
        client.get("/login")

        def should_not_run(*args, **kwargs):
            raise AssertionError("the code must not be exchanged on a bad state")

        monkeypatch.setattr(oauth, "login", should_not_run)
        response = client.get("/callback?code=abc&state=not-the-one")
        assert response.status_code == 400

    def test_a_callback_without_a_session_is_refused(self, client, monkeypatch):
        monkeypatch.setattr(
            oauth, "login", lambda *a, **k: (_ for _ in ()).throw(AssertionError)
        )
        response = client.get("/callback?code=abc&state=whatever")
        assert response.status_code == 400

    def test_a_declined_authorisation_is_handled_quietly(self, client):
        client.get("/login")
        response = client.get("/callback?error=access_denied")
        assert response.status_code == 400
        assert b"declined" in response.data

    def test_a_successful_callback_signs_you_in(self, client, store, monkeypatch):
        client.get("/login")
        monkeypatch.setattr(oauth, "login", lambda *a, **k: (ACTOR, GUILDS))

        response = client.get("/callback?code=abc&state=" + _pending_state(store))
        assert response.status_code == 302
        assert response.headers["Location"].endswith("/")

    def test_the_session_cookie_is_locked_down(self, client, store, monkeypatch):
        client.get("/login")
        monkeypatch.setattr(oauth, "login", lambda *a, **k: (ACTOR, GUILDS))
        response = client.get("/callback?code=abc&state=" + _pending_state(store))

        cookie = response.headers.get("Set-Cookie", "")
        assert "HttpOnly" in cookie
        assert "Secure" in cookie
        assert "SameSite=Lax" in cookie


def _pending_state(store):
    """The state of the one outstanding pre-auth row."""
    with sqlite3.connect(store.path) as conn:
        row = conn.execute(
            "SELECT oauth_state FROM sessions WHERE oauth_state IS NOT NULL"
        ).fetchone()
    return row[0]


# -------------------------------------------------------------------
# The picker
# -------------------------------------------------------------------
class TestPicker:
    def test_signed_out_visitors_get_the_login_page(self, client):
        response = client.get("/")
        assert response.status_code == 200
        assert b"Sign in with Discord" in response.data

    def test_the_bot_decides_which_servers_are_installed(
        self, client, store, bot_api
    ):
        login_as(client, store)
        response = client.get("/")
        assert response.status_code == 200
        assert b"Alpha Club" in response.data
        assert b"Installed" in response.data
        assert b"Add to this server" in response.data

    def test_servers_without_the_bot_offer_a_targeted_invite(self, client, store):
        login_as(client, store)
        page = client.get("/").data.decode()
        assert f"guild_id={GUILD_OUT}" in page
        assert "disable_guild_select=true" in page

    def test_non_admin_guilds_are_not_shown_at_all(self, client, store, bot_api):
        login_as(client, store)
        page = client.get("/").data.decode()
        assert "Gamma Hall" not in page
        # And they are never even mentioned to the bot.
        _actor, asked = bot_api.calls[-1]
        assert GUILD_NOT_ADMIN not in asked

    def test_the_actor_sent_to_the_bot_is_the_session_owner(
        self, client, store, bot_api
    ):
        login_as(client, store)
        client.get("/")
        actor, _asked = bot_api.calls[-1]
        assert str(actor) == ACTOR

    def test_an_unreachable_bot_still_renders_a_useful_page(
        self, client, store, config
    ):
        app = create_app(config, store=store, client=FakeBotAPI(fail=True))
        app.config.update(TESTING=True)
        test_client = app.test_client()
        login_as(test_client, store)

        response = test_client.get("/")
        assert response.status_code == 200
        assert b"Can&#39;t reach the bot" in response.data or b"Can't reach the bot" in response.data


# -------------------------------------------------------------------
# Hardening
# -------------------------------------------------------------------
class TestHardening:
    def test_security_headers_on_every_response(self, client):
        headers = client.get("/").headers
        assert headers["X-Content-Type-Options"] == "nosniff"
        assert headers["X-Frame-Options"] == "DENY"
        assert headers["Referrer-Policy"] == "no-referrer"
        assert headers["Cache-Control"] == "no-store"
        assert "max-age=31536000" in headers["Strict-Transport-Security"]

    def test_the_csp_allows_no_external_script_or_style(self, client):
        policy = client.get("/").headers["Content-Security-Policy"]
        assert "script-src 'self'" in policy
        assert "style-src 'self'" in policy
        assert "default-src 'none'" in policy
        assert "frame-ancestors 'none'" in policy
        # The one deliberate exception, and it is images only.
        assert "img-src 'self' https://cdn.discordapp.com" in policy
        assert "unsafe-inline" not in policy
        assert "unsafe-eval" not in policy

    def test_no_page_carries_inline_script(self, client, store):
        login_as(client, store)
        for path in ("/", "/nonexistent"):
            assert b"<script" not in client.get(path).data

    def test_logout_requires_the_csrf_token(self, client, store):
        session = login_as(client, store)
        assert client.post("/logout", data={"csrf_token": "wrong"}).status_code == 400
        # The session survived the failed attempt.
        assert store.load(session.sid) is not None

    def test_logout_with_the_token_destroys_the_session(self, client, store):
        session = login_as(client, store)
        response = client.post("/logout", data={"csrf_token": session.csrf_token})
        assert response.status_code == 302
        assert store.load(session.sid) is None

    def test_cf_access_headers_grant_nothing(self, client, store):
        """The Access policy comes off at launch (A-14).

        Any code that authorised on these headers would silently become a
        complete authentication bypass on that day, with no error and no
        deploy to correlate against. So they must do nothing, ever.
        """
        response = client.get(
            "/",
            headers={
                "Cf-Access-Authenticated-User-Email": "sasha-1221@hotmail.com",
                "Cf-Access-Jwt-Assertion": "eyJhbGciOiJSUzI1NiJ9.fake.fake",
            },
        )
        assert response.status_code == 200
        assert b"Sign in with Discord" in response.data

    def test_healthz_says_nothing_useful(self, client):
        assert client.get("/healthz").get_json() == {"ok": True}


class TestReadOnlyPhase:
    """Step 3 is login and a picker. Nothing here may change a server."""

    def test_the_only_post_route_is_logout(self, app):
        posts = {
            rule.rule
            for rule in app.url_map.iter_rules()
            if "POST" in (rule.methods or set())
        }
        assert posts == {"/logout"}, f"an unexpected write route appeared: {posts}"

    def test_the_dashboard_holds_no_database_credential(self):
        """A compromise of the public host must not reach the users table."""
        import dashboard.app as module

        source = open(module.__file__, encoding="utf-8").read()
        for forbidden in ("DATABASE_URL", "psycopg2", "sqlalchemy", "DISCORD_BOT_TOKEN"):
            assert forbidden not in source


class TestUnreadableCredentials:
    """The same gap on the dashboard side, and the one that actually bit.

    The client certificate key was mounted mode 0600 owned by the host user
    while the image runs as uid 10001. `os.path.isfile` succeeded, the app
    started, served the login page, and then failed every call to the bot with
    a TLS error that named nothing useful. Checking readability turns that into
    a refusal to start, with the cause in the message.
    """

    def test_an_unreadable_client_key_refuses_to_start(
        self, monkeypatch, certs, tmp_path
    ):
        import os as os_module

        from dashboard import config as config_module

        key = certs / "client.key"
        real_access = os_module.access

        def no_read(path, mode):
            if str(path) == str(key) and mode == os_module.R_OK:
                return False
            return real_access(path, mode)

        monkeypatch.setattr(config_module.os, "access", no_read)
        for name, value in {
            "DISCORD_CLIENT_ID": "123",
            "DISCORD_CLIENT_SECRET": "shh",
            "OAUTH_REDIRECT_URI": "https://dashboard.vrcverify.com/callback",
            "DASHBOARD_SECRET_KEY": SECRET_KEY,
            "BOT_API_URL": "https://100.117.6.99:5002",
            "BOT_API_CLIENT_CERT": str(certs / "client.pem"),
            "BOT_API_CLIENT_KEY": str(key),
            "BOT_API_CA": str(certs / "ca.pem"),
            "BOT_API_TOKEN_SIGNING_KEY": SIGNING_KEY,
        }.items():
            monkeypatch.setenv(name, value)

        with pytest.raises(DashboardConfigError) as error:
            DashboardConfig.from_env()
        assert "cannot read" in str(error.value)
