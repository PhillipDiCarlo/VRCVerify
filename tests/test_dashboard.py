"""Unit tests for the web dashboard (issue #65, steps 3 to 5).

This is the internet-facing half of the system, so the tests are mostly about
the ways a login can be subverted rather than the happy path:

- the Discord access token must never be persisted anywhere
- a callback whose `state` doesn't match ours is refused
- the session id changes at the moment privilege is granted
- authority is never taken from OAuth data or from Cf-Access-* headers
- the app refuses to start on a weak or missing secret
- nothing here can write: the read-only phase is pinned

Step 4 adds one server's settings, where the recurring theme is that the page
must say exactly what the bot would:

- "refused at save time" and "saved but not acted on" are different badges
- a 403 and a 404 from the bot are one indistinguishable answer, or the page
  becomes an oracle for which servers run 18+ gating
- a failed read shows an id or an apology, never a default nobody chose
"""

import json
import sqlite3
import time
from types import SimpleNamespace

import pytest

pytest.importorskip("flask")

from dashboard import oauth, settings_view  # noqa: E402
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


VERIFIED_ROLE = "900000000001"
UNVERIFIED_ROLE = "900000000002"
UNASSIGNABLE_ROLE = "900000000003"
LOG_CHANNEL = "800000000001"
NEWS_CHANNEL = "800000000002"

# What a free server looks like: the three write-locked features refused, the
# two badge-only ones inactive but unlocked. Straight out of SETTINGS_FIELDS.
FREE_PLAN = {
    "role_id": (None, True, False),
    "unverified_role_id": ("unverified_role_removal", False, False),
    "auto_verify_new_members": (None, True, False),
    "auto_nickname_change": ("nickname_sync", False, True),
    "custom_verification_requested_message": ("custom_dm", False, False),
    "instructions_locale": (None, True, False),
    "panel_embed_color": ("branded_panel", False, True),
    "panel_show_icon": ("branded_panel", False, True),
    "verification_log_channel_id": ("activity_log", False, True),
}

DEFAULT_VALUES = {
    "role_id": VERIFIED_ROLE,
    "unverified_role_id": None,
    "auto_verify_new_members": True,
    "auto_nickname_change": False,
    "custom_verification_requested_message": None,
    "instructions_locale": "en-US",
    "panel_embed_color": None,
    "panel_show_icon": False,
    "verification_log_channel_id": None,
}


# What the bot currently lets the website write. Mirrors
# bot.DASHBOARD_WRITABLE_FIELDS -- the payload carries it so the dashboard
# never has to know.
WRITABLE = set(FREE_PLAN)  # step 5 is complete: every declared setting is open

LOCALES = ["en-US", "es-ES", "ja", "de"]


def make_settings(premium=False, values=None, auto_verify_column=True, writable=None):
    """A settings payload shaped exactly like read_dashboard_settings returns."""
    merged = dict(DEFAULT_VALUES)
    merged.update(values or {})
    open_fields = WRITABLE if writable is None else set(writable)
    fields = {}
    for name, (feature, active, locked) in FREE_PLAN.items():
        fields[name] = {
            "value": merged[name],
            "feature": feature,
            "active": True if premium else active,
            "locked": False if premium else locked,
            "writable": name in open_fields,
        }
    return {
        "guild_id": GUILD_IN,
        "premium": {"enforced": True, "premium": premium, "grandfathered": False},
        "auto_verify_column_present": auto_verify_column,
        "choices": {"instructions_locale": list(LOCALES)},
        "fields": fields,
    }


DEFAULT_ROLES = [
    {
        "id": VERIFIED_ROLE,
        "name": "Verified",
        "position": 5,
        "color": 0x5865F2,
        "managed": False,
        "assignable": True,
    },
    {
        "id": UNVERIFIED_ROLE,
        "name": "Unverified",
        "position": 4,
        "color": 0,
        "managed": False,
        "assignable": True,
    },
    {
        "id": UNASSIGNABLE_ROLE,
        "name": "Above The Bot",
        "position": 90,
        "color": 0,
        "managed": False,
        "assignable": False,
    },
]

DEFAULT_CHANNELS = [
    {
        "id": LOG_CHANNEL,
        "name": "verify-log",
        "category": "Staff",
        "position": 1,
        "is_news": False,
        "can_send": True,
    },
    {
        "id": NEWS_CHANNEL,
        "name": "announcements",
        "category": None,
        "position": 2,
        "is_news": True,
        "can_send": True,
    },
]


class FakeBotAPI:
    """Stands in for the bot. Records what it was asked."""

    def __init__(
        self,
        installed=(GUILD_IN,),
        fail=False,
        settings=None,
        roles=None,
        channels=None,
        panel=None,
        audit=None,
        errors=None,
    ):
        self.installed = {str(g) for g in installed}
        self.fail = fail
        self.calls = []
        self.reads = []
        self.saves = []
        self._settings = settings
        self._roles = DEFAULT_ROLES if roles is None else roles
        self._channels = DEFAULT_CHANNELS if channels is None else channels
        self._panel = {"posted": False} if panel is None else panel
        self._audit = [] if audit is None else audit
        # {"settings": BotAPIError(...), ...} -- per-endpoint failures, so a
        # secondary read can be broken without breaking the page.
        self.errors = errors or {}

    def admin_guild_ids(self, actor_id, guild_ids):
        self.calls.append((actor_id, list(guild_ids)))
        if self.fail:
            raise BotAPIError("bot unreachable")
        return {g for g in map(str, guild_ids) if g in self.installed}

    def _answer(self, what, actor_id, guild_id, payload):
        self.reads.append((what, str(actor_id), str(guild_id)))
        if what in self.errors:
            raise self.errors[what]
        return payload

    def settings(self, actor_id, guild_id):
        return self._answer(
            "settings",
            actor_id,
            guild_id,
            self._settings if self._settings is not None else make_settings(),
        )

    def roles(self, actor_id, guild_id):
        return self._answer("roles", actor_id, guild_id, self._roles)

    def channels(self, actor_id, guild_id):
        return self._answer("channels", actor_id, guild_id, self._channels)

    def panel(self, actor_id, guild_id):
        return self._answer("panel", actor_id, guild_id, self._panel)

    def audit(self, actor_id, guild_id):
        return self._answer("audit", actor_id, guild_id, self._audit)

    def update_settings(self, actor_id, guild_id, changes):
        self.saves.append((str(actor_id), str(guild_id), dict(changes)))
        if "update_settings" in self.errors:
            raise self.errors["update_settings"]
        return self._settings if self._settings is not None else make_settings()


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
        assert b"Add to this server" in response.data
        # Installed servers link into their settings; absent ones link to the
        # invite instead.
        assert f'href="/guild/{GUILD_IN}"'.encode() in response.data
        assert f'href="/guild/{GUILD_OUT}"'.encode() not in response.data

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
# One server's settings (step 4)
# -------------------------------------------------------------------
def settings_client(config, store, **kwargs):
    """A logged-in client whose bot API is configured for these tests."""
    api = FakeBotAPI(**kwargs)
    app = create_app(config, store=store, client=api)
    app.config.update(TESTING=True)
    test_client = app.test_client()
    login_as(test_client, store)
    return test_client, api


class TestSettingsPage:
    def test_signed_out_visitors_are_sent_to_the_login_page(self, client):
        response = client.get(f"/guild/{GUILD_IN}")
        assert response.status_code == 302
        assert response.headers["Location"].endswith("/")

    def test_values_are_shown_with_names_not_ids(self, config, store):
        """A read-only field shows the role's name and never its id."""
        test_client, _api = settings_client(
            config, store, settings=make_settings(writable=set())
        )
        page = test_client.get(f"/guild/{GUILD_IN}").data.decode()
        assert "Verified" in page
        assert VERIFIED_ROLE not in page

    def test_an_editable_role_is_labelled_by_name(self, config, store):
        """Editable, the id has to be in the option value -- the label doesn't."""
        test_client, _api = settings_client(config, store)
        page = test_client.get(f"/guild/{GUILD_IN}").data.decode()
        assert f'<option value="{VERIFIED_ROLE}" selected>Verified</option>' in page

    def test_every_read_is_scoped_to_the_session_owner_and_that_guild(
        self, config, store
    ):
        test_client, api = settings_client(config, store)
        test_client.get(f"/guild/{GUILD_IN}")
        assert {what for what, _, _ in api.reads} == {
            "settings",
            "roles",
            "channels",
            "panel",
            "audit",
        }
        for _what, actor, guild in api.reads:
            assert actor == ACTOR
            assert guild == GUILD_IN

    def test_the_guild_name_comes_from_the_session_not_the_bot(self, config, store):
        test_client, _api = settings_client(config, store)
        assert b"Alpha Club" in test_client.get(f"/guild/{GUILD_IN}").data

    def test_a_guild_missing_from_a_stale_oauth_list_still_renders(
        self, config, store
    ):
        """Promotion to Administrator since login must not lock someone out.

        The OAuth guild list is a display hint that ages; the bot has already
        said yes by the time we get here.
        """
        api = FakeBotAPI()
        app = create_app(config, store=store, client=api)
        app.config.update(TESTING=True)
        test_client = app.test_client()
        login_as(test_client, store, guilds=[])

        response = test_client.get(f"/guild/{GUILD_IN}")
        assert response.status_code == 200
        assert b"Verified" in response.data


class TestSettingsDoesNotLeakWhichServersRunTheBot:
    """403 and 404 must be one indistinguishable answer.

    The bot separates "not in that guild" from "you are not an administrator",
    which is correct behind mTLS and dangerous on the open web: rendered
    differently, any signed-in user could walk guild ids and enumerate the
    servers running 18+ gating.
    """

    def _response(self, config, store, status):
        test_client, _api = settings_client(
            config, store, errors={"settings": BotAPIError("nope", status)}
        )
        return test_client.get(f"/guild/{GUILD_IN}")

    def test_403_and_404_are_byte_identical(self, config, store):
        # One client, so the comparison isn't confounded by the per-session
        # CSRF token in the sign-out form.
        test_client, api = settings_client(
            config, store, errors={"settings": BotAPIError("nope", 403)}
        )
        forbidden = test_client.get(f"/guild/{GUILD_IN}")

        api.errors = {"settings": BotAPIError("nope", 404)}
        missing = test_client.get(f"/guild/{GUILD_IN}")

        assert forbidden.status_code == missing.status_code == 404
        assert forbidden.data == missing.data

    def test_neither_names_the_reason(self, config, store):
        page = self._response(config, store, 403).data.decode().lower()
        # Both possibilities are offered and neither is confirmed.
        assert "administrator permission there" in page
        assert "vrcverify isn" in page
        assert "403" not in page
        assert "not_administrator" not in page

    def test_an_unavailable_bot_is_a_different_answer(self, config, store):
        """503 discloses nothing about a guild, so it may be honest."""
        response = self._response(config, store, 503)
        assert response.status_code == 503
        assert b"Can&#39;t reach the bot" in response.data

    def test_a_failed_settings_read_never_renders_defaults(self, config, store):
        page = self._response(config, store, 503).data.decode()
        for never in ("Not set", "Default blue", "en-US"):
            assert never not in page


class TestPlanBadgesMirrorTheBot:
    """The site must be neither stricter nor looser than the slash commands."""

    def test_write_locked_fields_are_marked_premium(self, config, store):
        test_client, _api = settings_client(config, store)
        page = test_client.get(f"/guild/{GUILD_IN}").data.decode()
        assert "Nickname sync" in page
        assert "Premium</span>" in page

    def test_badge_only_fields_are_not_shown_as_locked(self, config, store):
        """/vrcverify_setup stores these for a free server quite happily.

        Rendering them as Premium-locked would make the website refuse to show
        something an admin can plainly set in Discord.
        """
        test_client, _api = settings_client(
            config,
            store,
            settings=make_settings(
                values={
                    "unverified_role_id": UNVERIFIED_ROLE,
                    "custom_verification_requested_message": "Welcome aboard!",
                }
            ),
        )
        page = test_client.get(f"/guild/{GUILD_IN}").data.decode()
        # The values are shown, not hidden or replaced with an upsell.
        assert "Unverified" in page
        assert "Welcome aboard!" in page
        assert "Not applied</span>" in page
        assert "Saved, but not acted on without Premium" in page

    def test_a_premium_server_sees_no_badges(self, config, store):
        test_client, _api = settings_client(
            config, store, settings=make_settings(premium=True)
        )
        page = test_client.get(f"/guild/{GUILD_IN}").data.decode()
        assert "Premium</span>" not in page
        assert "Not applied</span>" not in page
        assert "VRCVerify Premium is active" in page

    def test_auto_verify_is_never_gated(self, config, store):
        """Free for everyone, forever -- mirrors TestAutoVerifyOnJoinIsFree."""
        test_client, _api = settings_client(config, store)
        page = test_client.get(f"/guild/{GUILD_IN}").data.decode()
        section = page.split("Auto-verify on join")[1].split("</div>")[0]
        assert "badge" not in section

    def test_a_missing_auto_verify_column_is_declared(self, config, store):
        test_client, _api = settings_client(
            config, store, settings=make_settings(auto_verify_column=False)
        )
        page = test_client.get(f"/guild/{GUILD_IN}").data.decode()
        assert "missing the auto-verify column" in page


class TestSettingsWarnings:
    """The point of the dashboard: say it now, not at verification time."""

    def test_an_unassignable_verified_role_is_called_out(self, config, store):
        test_client, _api = settings_client(
            config, store, settings=make_settings(values={"role_id": UNASSIGNABLE_ROLE})
        )
        page = test_client.get(f"/guild/{GUILD_IN}").data.decode()
        assert "cannot grant this role" in page
        assert "Server Settings -&gt; Roles" in page

    def test_a_deleted_role_is_called_out(self, config, store):
        test_client, _api = settings_client(
            config, store, settings=make_settings(values={"role_id": "404404404404"})
        )
        page = test_client.get(f"/guild/{GUILD_IN}").data.decode()
        assert "no longer exists" in page

    def test_no_verified_role_is_called_out(self, config, store):
        test_client, _api = settings_client(
            config, store, settings=make_settings(values={"role_id": None})
        )
        page = test_client.get(f"/guild/{GUILD_IN}").data.decode()
        assert "verification cannot complete" in page

    def test_an_announcement_log_channel_is_called_out(self, config, store):
        """A followed channel republishes an age disclosure to strangers."""
        test_client, _api = settings_client(
            config,
            store,
            settings=make_settings(
                premium=True, values={"verification_log_channel_id": NEWS_CHANNEL}
            ),
        )
        page = test_client.get(f"/guild/{GUILD_IN}").data.decode()
        assert "republish age disclosures" in page

    def test_an_unreachable_panel_channel_is_called_out(self, config, store):
        test_client, _api = settings_client(
            config,
            store,
            panel={
                "posted": True,
                "channel_id": "123",
                "message_id": "456",
                "channel_name": "verify",
                "channel_exists": True,
                "channel_reachable": False,
                "locale": "en-US",
            },
        )
        page = test_client.get(f"/guild/{GUILD_IN}").data.decode()
        assert "can no longer post in that channel" in page


class TestSecondaryReadsDegradeGracefully:
    """A name lookup failing must not cost the whole page."""

    def test_the_page_renders_without_roles(self, config, store):
        test_client, _api = settings_client(
            config, store, errors={"roles": BotAPIError("unavailable", 503)}
        )
        response = test_client.get(f"/guild/{GUILD_IN}")
        assert response.status_code == 200
        page = response.data.decode()
        assert f"Unknown role ({VERIFIED_ROLE})" in page
        assert "show an ID instead of a name" in page

    def test_a_role_picker_with_nothing_to_pick_is_not_offered(self, config, store):
        """An empty <select> invites a save that clears the verified role.

        Better to fall back to the read-only view than to render a control
        whose only options are "leave it" and "break verification".
        """
        test_client, _api = settings_client(
            config, store, errors={"roles": BotAPIError("unavailable", 503)}
        )
        page = test_client.get(f"/guild/{GUILD_IN}").data.decode()
        assert 'name="role_id"' not in page
        assert 'name="unverified_role_id"' not in page

    def test_an_unresolved_id_is_not_reported_as_deleted(self, config, store):
        """"We could not check" and "it is gone" are different claims."""
        test_client, _api = settings_client(
            config, store, errors={"roles": BotAPIError("unavailable", 503)}
        )
        page = test_client.get(f"/guild/{GUILD_IN}").data.decode()
        assert "no longer exists" not in page

    def test_the_page_renders_without_the_audit_read(self, config, store):
        """An empty history and an unavailable one are different facts."""
        test_client, _api = settings_client(
            config, store, errors={"audit": BotAPIError("unavailable", 503)}
        )
        response = test_client.get(f"/guild/{GUILD_IN}")
        assert response.status_code == 200
        page = response.data.decode()
        assert "Couldn't load the history" in page
        assert "No changes have been made" not in page

    def test_the_page_renders_without_the_panel_read(self, config, store):
        test_client, _api = settings_client(
            config, store, errors={"panel": BotAPIError("unavailable", 503)}
        )
        response = test_client.get(f"/guild/{GUILD_IN}")
        assert response.status_code == 200
        # Literal template text, so the apostrophe is not entity-escaped here.
        assert b"Couldn't check" in response.data


class TestSavingThePanelGroup:
    """The app's only write. The bot re-decides everything below regardless."""

    def post(self, test_client, session, **form):
        form.setdefault("csrf_token", session.csrf_token)
        return test_client.post(f"/guild/{GUILD_IN}/panel", data=form)

    def logged_in(self, config, store, **kwargs):
        api = FakeBotAPI(**kwargs)
        app = create_app(config, store=store, client=api)
        app.config.update(TESTING=True)
        test_client = app.test_client()
        session = login_as(test_client, store)
        return test_client, api, session

    def test_a_save_reaches_the_bot_and_redirects(self, config, store):
        test_client, api, session = self.logged_in(config, store)
        response = self.post(
            test_client,
            session,
            instructions_locale="ja",
            present_panel_embed_color="1",
            panel_embed_color="#ff0000",
            present_panel_show_icon="1",
            panel_show_icon="on",
        )
        assert response.status_code == 302
        assert response.headers["Location"].endswith("saved=1")
        actor, guild, changes = api.saves[-1]
        assert (actor, guild) == (ACTOR, GUILD_IN)
        assert changes == {
            "instructions_locale": "ja",
            "panel_embed_color": 0xFF0000,
            "panel_show_icon": True,
        }

    def test_the_colour_is_sent_as_an_integer(self, config, store):
        test_client, api, session = self.logged_in(config, store)
        self.post(
            test_client,
            session,
            present_panel_embed_color="1",
            panel_embed_color="#0a0b0c",
        )
        assert api.saves[-1][2]["panel_embed_color"] == 0x0A0B0C

    def test_the_default_checkbox_clears_the_colour(self, config, store):
        test_client, api, session = self.logged_in(config, store)
        self.post(
            test_client,
            session,
            present_panel_embed_color="1",
            panel_color_default="on",
            panel_embed_color="#ff0000",
        )
        assert api.saves[-1][2]["panel_embed_color"] is None

    def test_an_unticked_checkbox_is_a_false_not_a_missing_field(
        self, config, store
    ):
        """A checkbox that is off sends nothing, which is why the form declares
        that its branding controls were rendered at all."""
        test_client, api, session = self.logged_in(config, store)
        self.post(test_client, session, present_panel_show_icon="1")
        assert api.saves[-1][2]["panel_show_icon"] is False

    def test_branding_is_untouched_when_its_controls_were_not_rendered(
        self, config, store
    ):
        """A free server posts only its language.

        Without the declaration, a form with no branding controls would look
        exactly like one whose icon box was unticked, and saving the language
        would silently turn the icon off.
        """
        test_client, api, session = self.logged_in(config, store)
        self.post(test_client, session, instructions_locale="de")
        assert api.saves[-1][2] == {"instructions_locale": "de"}

    def test_a_save_with_no_changes_touches_nothing(self, config, store):
        test_client, api, session = self.logged_in(config, store)
        response = self.post(test_client, session)
        assert response.status_code == 302
        assert api.saves == []

    # ----- the guards -----
    def test_a_save_without_the_csrf_token_is_refused(self, config, store):
        test_client, api, session = self.logged_in(config, store)
        response = test_client.post(
            f"/guild/{GUILD_IN}/panel",
            data={"csrf_token": "wrong", "instructions_locale": "ja"},
        )
        assert response.status_code == 400
        assert api.saves == []

    def test_a_signed_out_visitor_cannot_save(self, client, bot_api):
        response = client.post(
            f"/guild/{GUILD_IN}/panel", data={"instructions_locale": "ja"}
        )
        assert response.status_code == 302
        assert not getattr(bot_api, "saves", [])

    def test_the_actor_is_the_session_owner_not_the_form(self, config, store):
        test_client, api, session = self.logged_in(config, store)
        self.post(
            test_client, session, instructions_locale="ja", actor_id="999999999999"
        )
        assert api.saves[-1][0] == ACTOR

    # ----- refusals -----
    @pytest.mark.parametrize(
        "reason, expected",
        [
            ("requires_premium", "needs VRCVerify Premium"),
            ("unsupported_language", "isn&#39;t one VRCVerify supports"),
            ("server_not_set_up", "vrcverify_setup"),
            ("unavailable", "couldn&#39;t complete the save"),
        ],
    )
    def test_a_refusal_is_explained_in_our_own_words(
        self, config, store, reason, expected
    ):
        test_client, _api, session = self.logged_in(
            config, store, errors={"update_settings": BotAPIError(reason, 403)}
        )
        response = self.post(test_client, session, instructions_locale="ja")
        page = test_client.get(response.headers["Location"]).data.decode()
        assert expected in page

    def test_an_unrecognised_refusal_never_reaches_the_page_as_text(
        self, config, store
    ):
        """The bot's error strings must not become this app's HTML."""
        leak = "surprising-internal-detail"
        test_client, _api, session = self.logged_in(
            config, store, errors={"update_settings": BotAPIError(leak, 400)}
        )
        response = self.post(test_client, session, instructions_locale="ja")
        assert "error=unknown" in response.headers["Location"]
        page = test_client.get(response.headers["Location"]).data.decode()
        assert leak not in page
        assert "couldn&#39;t be saved" in page

    def test_an_arbitrary_error_code_in_the_url_is_not_reflected(
        self, config, store
    ):
        test_client, _api, _session = self.logged_in(config, store)
        page = test_client.get(
            f"/guild/{GUILD_IN}?error=%3Cimg+src%3Dx+onerror%3Dalert(1)%3E"
        ).data.decode()
        assert "onerror" not in page
        assert "couldn&#39;t be saved" in page


class TestSavingTheVerificationGroup:
    def post(self, test_client, session, **form):
        form.setdefault("csrf_token", session.csrf_token)
        return test_client.post(f"/guild/{GUILD_IN}/verification", data=form)

    def logged_in(self, config, store, **kwargs):
        api = FakeBotAPI(**kwargs)
        app = create_app(config, store=store, client=api)
        app.config.update(TESTING=True)
        test_client = app.test_client()
        return test_client, api, login_as(test_client, store)

    def test_roles_and_auto_verify_are_sent(self, config, store):
        test_client, api, session = self.logged_in(config, store)
        self.post(
            test_client,
            session,
            role_id=VERIFIED_ROLE,
            unverified_role_id=UNVERIFIED_ROLE,
            present_auto_verify_new_members="1",
            auto_verify_new_members="on",
        )
        assert api.saves[-1][2] == {
            "role_id": VERIFIED_ROLE,
            "unverified_role_id": UNVERIFIED_ROLE,
            "auto_verify_new_members": True,
        }

    def test_an_empty_unverified_role_clears_it(self, config, store):
        """A select always submits, so blank is a real choice, not an absence."""
        test_client, api, session = self.logged_in(config, store)
        self.post(test_client, session, role_id=VERIFIED_ROLE, unverified_role_id="")
        assert api.saves[-1][2]["unverified_role_id"] is None

    def test_auto_verify_off_is_sent_as_false(self, config, store):
        test_client, api, session = self.logged_in(config, store)
        self.post(
            test_client,
            session,
            role_id=VERIFIED_ROLE,
            present_auto_verify_new_members="1",
        )
        assert api.saves[-1][2]["auto_verify_new_members"] is False

    def test_a_form_without_the_auto_verify_control_leaves_it_alone(
        self, config, store
    ):
        """The marker is what tells "unticked" from "never rendered"."""
        test_client, api, session = self.logged_in(config, store)
        self.post(test_client, session, role_id=VERIFIED_ROLE)
        assert "auto_verify_new_members" not in api.saves[-1][2]

    def test_it_needs_the_csrf_token(self, config, store):
        test_client, api, _session = self.logged_in(config, store)
        response = test_client.post(
            f"/guild/{GUILD_IN}/verification",
            data={"csrf_token": "wrong", "role_id": VERIFIED_ROLE},
        )
        assert response.status_code == 400
        assert api.saves == []

    def test_a_signed_out_visitor_cannot_save(self, client, bot_api):
        response = client.post(
            f"/guild/{GUILD_IN}/verification", data={"role_id": VERIFIED_ROLE}
        )
        assert response.status_code == 302
        assert not getattr(bot_api, "saves", [])

    def test_a_refused_role_is_explained(self, config, store):
        test_client, _api, session = self.logged_in(
            config, store, errors={"update_settings": BotAPIError("role_not_in_guild", 400)}
        )
        response = self.post(test_client, session, role_id="123")
        page = test_client.get(response.headers["Location"]).data.decode()
        assert "isn&#39;t in this server any more" in page


class TestSavingTheRemainingGroups:
    def logged_in(self, config, store, **kwargs):
        api = FakeBotAPI(**kwargs)
        app = create_app(config, store=store, client=api)
        app.config.update(TESTING=True)
        test_client = app.test_client()
        return test_client, api, login_as(test_client, store)

    def post(self, test_client, session, group, **form):
        form.setdefault("csrf_token", session.csrf_token)
        return test_client.post(f"/guild/{GUILD_IN}/{group}", data=form)

    # ----- after verifying -----
    def test_the_custom_message_is_sent_exactly_as_typed(self, config, store):
        """Sanitising here would be a second opinion about what is allowed."""
        test_client, api, session = self.logged_in(config, store)
        raw = "  Welcome @everyone!  \nhttps://vrchat.com/home  "
        self.post(
            test_client,
            session,
            "member",
            custom_verification_requested_message=raw,
        )
        assert api.saves[-1][2]["custom_verification_requested_message"] == raw

    def test_an_empty_message_is_sent_through_to_be_cleared(self, config, store):
        test_client, api, session = self.logged_in(config, store)
        self.post(
            test_client, session, "member", custom_verification_requested_message=""
        )
        assert api.saves[-1][2]["custom_verification_requested_message"] == ""

    def test_nickname_sync_uses_the_presence_marker(self, config, store):
        test_client, api, session = self.logged_in(config, store)
        self.post(
            test_client,
            session,
            "member",
            present_auto_nickname_change="1",
        )
        assert api.saves[-1][2]["auto_nickname_change"] is False

    def test_a_form_without_the_nickname_control_leaves_it_alone(self, config, store):
        test_client, api, session = self.logged_in(config, store)
        self.post(
            test_client, session, "member", custom_verification_requested_message="hi"
        )
        assert "auto_nickname_change" not in api.saves[-1][2]

    @pytest.mark.parametrize(
        "reason, expected",
        [
            ("message_links_not_allowed", "discord.com or vrchat.com"),
            ("message_too_long", "1000 characters"),
        ],
    )
    def test_a_message_refusal_is_explained(self, config, store, reason, expected):
        test_client, _api, session = self.logged_in(
            config, store, errors={"update_settings": BotAPIError(reason, 400)}
        )
        response = self.post(
            test_client, session, "member", custom_verification_requested_message="x"
        )
        page = test_client.get(response.headers["Location"]).data.decode()
        assert expected in page

    def test_the_offending_links_are_never_echoed_back(self, config, store):
        test_client, _api, session = self.logged_in(
            config,
            store,
            errors={"update_settings": BotAPIError("message_links_not_allowed", 400)},
        )
        response = self.post(
            test_client,
            session,
            "member",
            custom_verification_requested_message="go to https://evil.example.com",
        )
        page = test_client.get(response.headers["Location"]).data.decode()
        assert "evil.example.com" not in page

    # ----- logging -----
    def test_a_log_channel_is_sent(self, config, store):
        test_client, api, session = self.logged_in(config, store)
        self.post(
            test_client, session, "logging", verification_log_channel_id=LOG_CHANNEL
        )
        assert api.saves[-1][2] == {"verification_log_channel_id": LOG_CHANNEL}

    def test_an_empty_channel_selection_turns_logging_off(self, config, store):
        test_client, api, session = self.logged_in(config, store)
        self.post(test_client, session, "logging", verification_log_channel_id="")
        assert api.saves[-1][2]["verification_log_channel_id"] is None

    def test_the_announcement_refusal_explains_why(self, config, store):
        test_client, _api, session = self.logged_in(
            config,
            store,
            errors={"update_settings": BotAPIError("channel_is_announcement", 400)},
        )
        response = self.post(
            test_client, session, "logging", verification_log_channel_id=NEWS_CHANNEL
        )
        page = test_client.get(response.headers["Location"]).data.decode()
        assert "republish your members" in page

    # ----- guards, on both -----
    @pytest.mark.parametrize("group", ["member", "logging"])
    def test_they_need_the_csrf_token(self, config, store, group):
        test_client, api, _session = self.logged_in(config, store)
        response = test_client.post(
            f"/guild/{GUILD_IN}/{group}",
            data={"csrf_token": "wrong", "verification_log_channel_id": LOG_CHANNEL},
        )
        assert response.status_code == 400
        assert api.saves == []

    @pytest.mark.parametrize("group", ["member", "logging"])
    def test_a_signed_out_visitor_cannot_save(self, client, bot_api, group):
        response = client.post(f"/guild/{GUILD_IN}/{group}", data={})
        assert response.status_code == 302
        assert not getattr(bot_api, "saves", [])


class TestTheFormMatchesWhatTheBotAccepts:
    """Controls appear only where the bot said it would take the value."""

    def test_a_premium_server_gets_all_three_controls(self, config, store):
        test_client, _api = settings_client(
            config, store, settings=make_settings(premium=True)
        )
        page = test_client.get(f"/guild/{GUILD_IN}").data.decode()
        assert 'name="instructions_locale"' in page
        assert 'name="panel_embed_color"' in page
        assert 'name="panel_show_icon"' in page
        assert "Save changes" in page

    def test_a_free_server_gets_the_language_control_only(self, config, store):
        """Branding is write-locked, so no control -- but the language is free
        and must stay editable."""
        test_client, _api = settings_client(config, store)
        page = test_client.get(f"/guild/{GUILD_IN}").data.decode()
        assert 'name="instructions_locale"' in page
        assert 'type="color"' not in page
        assert 'name="panel_show_icon"' not in page

    def test_a_field_the_bot_has_not_opened_gets_no_control(self, config, store):
        test_client, _api = settings_client(
            config, store, settings=make_settings(premium=True, writable=set())
        )
        page = test_client.get(f"/guild/{GUILD_IN}").data.decode()
        assert "<form" not in page.split("Instructions panel")[-1]
        assert "Save changes" not in page

    def test_the_language_options_come_from_the_bot(self, config, store):
        """Not from the dashboard's display-name table, which may be stale."""
        test_client, _api = settings_client(
            config,
            store,
            settings=make_settings(premium=True),
        )
        page = test_client.get(f"/guild/{GUILD_IN}").data.decode()
        for code in LOCALES:
            assert f'value="{code}"' in page
        # Present in LOCALE_NAMES, absent from what the bot offered.
        assert 'value="pa-IN"' not in page

    def test_announcement_channels_are_not_offered_at_all(self, config, store):
        """Unlike an unassignable role, the bot refuses these outright.

        So leaving them out of the picker is matching the bot rather than being
        stricter than it -- the opposite call from the role list, for the
        opposite reason.
        """
        test_client, _api = settings_client(
            config, store, settings=make_settings(premium=True)
        )
        page = test_client.get(f"/guild/{GUILD_IN}").data.decode()
        assert f'value="{LOG_CHANNEL}"' in page
        assert f'value="{NEWS_CHANNEL}"' not in page

    def test_a_channel_picker_with_nothing_to_pick_is_not_offered(self, config, store):
        test_client, _api = settings_client(
            config,
            store,
            settings=make_settings(premium=True),
            errors={"channels": BotAPIError("unavailable", 503)},
        )
        page = test_client.get(f"/guild/{GUILD_IN}").data.decode()
        assert 'name="verification_log_channel_id"' not in page

    def test_the_custom_message_textarea_carries_the_bot_s_cap(self, config, store):
        test_client, _api = settings_client(
            config, store, settings=make_settings(premium=True)
        )
        page = test_client.get(f"/guild/{GUILD_IN}").data.decode()
        assert 'maxlength="1000"' in page

    def test_every_form_carries_a_csrf_token(self, config, store):
        test_client, _api = settings_client(
            config, store, settings=make_settings(premium=True)
        )
        page = test_client.get(f"/guild/{GUILD_IN}").data.decode()
        assert page.count('name="csrf_token"') >= page.count("<form")


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

    def test_no_page_carries_an_inline_style_attribute(self, config, store):
        """style-src 'self' blocks these, and it blocks them *silently*.

        Nothing errors -- the browser just drops the declaration and the page
        renders subtly wrong, which is a bad way to find out. The role and panel
        colour swatches are SVG fill attributes for exactly this reason.
        """
        test_client, _api = settings_client(
            config,
            store,
            # Nothing writable, so every field takes the read-only path and the
            # swatches are the presentation under test.
            settings=make_settings(
                premium=True, writable=set(), values={"panel_embed_color": 0xFF00FF}
            ),
        )
        for path in ("/", f"/guild/{GUILD_IN}"):
            page = test_client.get(path).data
            assert b"style=" not in page, f"inline style on {path}"
        page = test_client.get(f"/guild/{GUILD_IN}").data
        assert b'fill="#ff00ff"' in page   # the panel colour
        assert b'fill="#5865f2"' in page   # the verified role's colour

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


AUDIT_ENTRIES = [
    {
        "actor_id": ACTOR,
        "actor_name": "Sasha",
        "field": "role_id",
        "old_value": None,
        "new_value": VERIFIED_ROLE,
        "changed_at": "2026-08-04T09:15:00+00:00",
    },
    {
        "actor_id": "555555555555",
        "actor_name": None,
        "field": "panel_embed_color",
        "old_value": None,
        "new_value": str(0xFF0000),
        "changed_at": "2026-08-04T09:10:00+00:00",
    },
]


class TestTheChangeHistory:
    def test_it_names_the_setting_the_actor_and_both_values(self, config, store):
        test_client, _api = settings_client(config, store, audit=AUDIT_ENTRIES)
        page = test_client.get(f"/guild/{GUILD_IN}").data.decode()
        assert "Verified role" in page
        assert "Sasha" in page
        # The id is resolved to the role's name, as on the settings above.
        assert "not set &rarr; Verified" in page or "not set → Verified" in page

    def test_an_actor_who_left_is_shown_by_id(self, config, store):
        test_client, _api = settings_client(config, store, audit=AUDIT_ENTRIES)
        page = test_client.get(f"/guild/{GUILD_IN}").data.decode()
        assert "ID 555555555555" in page

    def test_a_colour_reads_as_a_colour(self, config, store):
        test_client, _api = settings_client(config, store, audit=AUDIT_ENTRIES)
        assert "#ff0000" in test_client.get(f"/guild/{GUILD_IN}").data.decode()

    def test_an_empty_history_says_so(self, config, store):
        test_client, _api = settings_client(config, store, audit=[])
        assert b"No changes have been made" in test_client.get(
            f"/guild/{GUILD_IN}"
        ).data

    def test_a_long_value_is_truncated(self):
        entries = [
            {
                "actor_id": ACTOR,
                "actor_name": "Sasha",
                "field": "custom_verification_requested_message",
                "old_value": None,
                "new_value": "x" * 500,
                "changed_at": None,
            }
        ]
        rendered = settings_view.build_audit(entries, DEFAULT_ROLES, DEFAULT_CHANNELS)
        assert len(rendered[0]["new"]) <= settings_view.AUDIT_VALUE_MAX

    def test_a_deleted_role_is_named_as_gone_not_as_an_id(self):
        entries = [
            {
                "actor_id": ACTOR,
                "actor_name": None,
                "field": "role_id",
                "old_value": "404404404404",
                "new_value": VERIFIED_ROLE,
                "changed_at": None,
            }
        ]
        rendered = settings_view.build_audit(entries, DEFAULT_ROLES, DEFAULT_CHANNELS)
        assert "no longer exists" in rendered[0]["old"]

    def test_an_unavailable_trail_is_none_not_empty(self):
        assert settings_view.build_audit(None, DEFAULT_ROLES, DEFAULT_CHANNELS) is None


class TestSettingsViewModel:
    """The rendering rules, without a request in the way."""

    def test_no_field_from_the_api_is_silently_dropped(self):
        """Adding a setting to the bot must not quietly skip the website.

        SETTINGS_FIELDS in bot.py is the allowlist for both ends. If a field
        appears there and nothing renders it, an admin sees a page that claims
        to be their settings and isn't -- so this fails rather than omitting.
        """
        settings = make_settings()
        rendered = {
            field.name
            for group in settings_view.build_groups(
                settings, DEFAULT_ROLES, DEFAULT_CHANNELS
            )
            for field in group["fields"]
        }
        assert rendered == set(settings["fields"])

    def test_locked_beats_inactive_on_the_badge(self):
        """A locked field is always inactive too; the stronger claim wins."""
        field = settings_view.Field(
            "x", "X", "", "bool", "On", active=False, locked=True
        )
        assert field.badge == "premium"

    def test_inactive_but_unlocked_is_its_own_badge(self):
        field = settings_view.Field(
            "x", "X", "", "bool", "On", active=False, locked=False
        )
        assert field.badge == "inactive"

    def test_an_available_field_has_no_badge(self):
        assert settings_view.Field("x", "X", "", "bool", "On").badge is None

    @pytest.mark.parametrize(
        "value, expected",
        [
            (0x5865F2, "#5865f2"),
            (0, "#000000"),
            (0xFFFFFFFF, "#ffffff"),  # masked to 24 bits
            (-1, "#ffffff"),
            (None, None),
            ("#ff0000; --x: url(evil)", None),
        ],
    )
    def test_colours_can_only_ever_be_a_colour(self, value, expected):
        """The result lands in an SVG fill attribute, so shape is the guard."""
        assert settings_view._hex(value) == expected

    def test_an_unknown_locale_renders_as_itself(self):
        settings = make_settings(values={"instructions_locale": "xx-YY"})
        groups = settings_view.build_groups(settings, DEFAULT_ROLES, DEFAULT_CHANNELS)
        locale = next(
            field
            for group in groups
            for field in group["fields"]
            if field.name == "instructions_locale"
        )
        assert locale.display == "xx-YY"

    def test_an_unread_panel_is_not_reported_as_absent(self):
        """"Never posted" and "could not read" must not look the same.

        load_instruction_panel returns None for both, so step 6 has to confirm
        before it offers to post a duplicate.
        """
        assert settings_view.panel_summary(None) == {"known": False}
        assert settings_view.panel_summary({"posted": False})["posted"] is False


class TestWriteSurface:
    """Step 5 opens exactly one save path. Widening it means editing this."""

    def test_the_post_routes_are_logout_and_one_save_per_group(self, app):
        posts = {
            rule.rule
            for rule in app.url_map.iter_rules()
            if "POST" in (rule.methods or set())
        }
        assert posts == {
            "/logout",
            "/guild/<int:guild_id>/verification",
            "/guild/<int:guild_id>/member",
            "/guild/<int:guild_id>/panel",
            "/guild/<int:guild_id>/logging",
        }, f"an unexpected write route appeared: {posts}"

    def test_every_group_that_saves_names_a_route_that_exists(self, app, config):
        """A typo in `save_endpoint` would be a 500 on page load, not a test."""
        groups = settings_view.build_groups(make_settings(premium=True), DEFAULT_ROLES, DEFAULT_CHANNELS)
        endpoints = {rule.endpoint for rule in app.url_map.iter_rules()}
        for group in groups:
            if group.get("save_endpoint"):
                assert group["save_endpoint"] in endpoints

    def test_the_dashboard_holds_no_database_credential(self):
        """A compromise of the public host must not reach the users table."""
        import dashboard.app as module

        source = open(module.__file__, encoding="utf-8").read()
        for forbidden in ("DATABASE_URL", "psycopg2", "sqlalchemy", "DISCORD_BOT_TOKEN"):
            assert forbidden not in source
