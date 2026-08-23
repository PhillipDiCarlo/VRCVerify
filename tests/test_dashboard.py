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
import logging
import os
import re
import sqlite3
import stat
import struct
import time
from html.parser import HTMLParser
from types import SimpleNamespace

import pytest

pytest.importorskip("flask")

from dashboard import oauth, overview_view, settings_view  # noqa: E402
from dashboard.app import CSP, SESSION_COOKIE, create_app  # noqa: E402
from dashboard import app as app_module  # noqa: E402
from dashboard.botapi import BotAPIError  # noqa: E402
from dashboard.config import DashboardConfig, DashboardConfigError  # noqa: E402
from dashboard import sessions as sessions_module  # noqa: E402
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
    "vrchat_group_id": ("group_invite", False, True),
    "vrchat_group_invite_enabled": ("group_invite", False, True),
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
    "vrchat_group_id": None,
    "vrchat_group_invite_enabled": False,
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


SKU_ID = "1533325058573865051"

GROUP_ID = "grp_0e1d4755-2f87-4129-a192-5587068cbf73"
INVITE_ACCOUNT = "usr_0e59962a-3e0d-4303-802b-9314623027e5"

# What read_dashboard_settings sends for a guild that has never configured a
# group. Shaped exactly as the bot builds it.
GROUP_INVITE_NONE = {
    "state": "unverified",
    "error": None,
    "group_name": None,
    "icon_url": None,
    "can_invite": False,
    "can_see_members": False,
    "claim_code": None,
    "account_to_invite": INVITE_ACCOUNT,
    "joined_account": None,
    "verified_at": None,
    "requested_at": None,
}


def group_invite_block(**overrides):
    block = dict(GROUP_INVITE_NONE)
    block.update(overrides)
    return block


def make_settings(
    premium=False,
    values=None,
    auto_verify_column=True,
    writable=None,
    grandfathered=False,
    enforced=True,
    sku_id=SKU_ID,
    group_invite=None,
):
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
        "premium": {
            "enforced": enforced,
            "premium": premium,
            "grandfathered": grandfathered,
            "sku_id": sku_id,
        },
        "auto_verify_column_present": auto_verify_column,
        "choices": {"instructions_locale": list(LOCALES)},
        "group_invite": group_invite if group_invite is not None else GROUP_INVITE_NONE,
        "fields": fields,
    }


def make_overview(
    member_count=1284,
    total=417,
    today=3,
    last_7_days=12,
    last_30_days=63,
    collecting_since="2026-06-01",
    known=True,
    panel=None,
    configured=None,
    premium=False,
    grandfathered=False,
):
    """A payload shaped exactly like read_dashboard_overview returns.

    Windows are passed through as given, including None -- which is the state
    that means "not collecting that far back", and is the one worth being able
    to construct explicitly in a test.
    """
    return {
        "guild_id": GUILD_IN,
        "member_count": member_count,
        "premium": {
            "enforced": True,
            "premium": premium,
            "grandfathered": grandfathered,
            "sku_id": SKU_ID,
        },
        "verifications": {
            "total": total,
            "today": today,
            "last_7_days": last_7_days,
            "last_30_days": last_30_days,
            "collecting_since": collecting_since,
            "known": known,
        },
        "panel": {"posted": True, "channel_id": LOG_CHANNEL} if panel is None else panel,
        "configured": (
            {
                "verified_role": True,
                "unverified_role": False,
                "log_channel": False,
                "auto_verify": True,
            }
            if configured is None
            else configured
        ),
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
        "can_embed": True,
    },
    {
        "id": NEWS_CHANNEL,
        "name": "announcements",
        "category": None,
        "position": 2,
        "is_news": True,
        "can_send": True,
        "can_embed": True,
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
        panel_result=None,
        errors=None,
        saved=None,
        overview=None,
    ):
        self.installed = {str(g) for g in installed}
        self.fail = fail
        self.calls = []
        self.reads = []
        self.saves = []
        self.panel_posts = []
        self.group_checks = []
        self._settings = settings
        self._roles = DEFAULT_ROLES if roles is None else roles
        self._channels = DEFAULT_CHANNELS if channels is None else channels
        self._panel = {"posted": False} if panel is None else panel
        self._audit = [] if audit is None else audit
        self._overview = overview
        self._panel_result = (
            {"action": "posted", "channel_id": LOG_CHANNEL}
            if panel_result is None else panel_result
        )
        # What the write endpoint answers with. The bot returns the re-read
        # settings, plus `panel_stale` when the live panel did not follow.
        self._saved = saved
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

    def overview(self, actor_id, guild_id):
        return self._answer(
            "overview",
            actor_id,
            guild_id,
            self._overview if self._overview is not None else make_overview(),
        )

    def post_panel(self, actor_id, guild_id, channel_id):
        self.panel_posts.append((str(actor_id), str(guild_id), str(channel_id)))
        if "post_panel" in self.errors:
            raise self.errors["post_panel"]
        return self._panel_result

    def verify_group(self, actor_id, guild_id):
        self.group_checks.append((str(actor_id), str(guild_id)))
        if "verify_group" in self.errors:
            raise self.errors["verify_group"]
        return {"guild_id": str(guild_id), "group_invite": {"state": "checking"}}

    def update_settings(self, actor_id, guild_id, changes):
        self.saves.append((str(actor_id), str(guild_id), dict(changes)))
        if "update_settings" in self.errors:
            raise self.errors["update_settings"]
        answer = dict(
            self._settings if self._settings is not None else make_settings()
        )
        answer.update(self._saved or {})
        return answer


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


class _Markup(HTMLParser):
    """Every script and every attribute on a page, found by parsing it.

    These checks were regexes until CodeQL pointed out the obvious: a pattern
    matching `<script` does not match `<SCRIPT`, so a test asserting "no inline
    script reaches the page" would have waved one through. The alert was on
    test code, but the guarantee it weakened is a real one.

    A parser removes the whole class of problem rather than the one instance:
    `HTMLParser` lowercases tag and attribute names for us, and it is not
    fooled by a `>` inside a quoted attribute value, which the regex also was.
    """

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.scripts = []
        self.attributes = []
        self._depth = 0

    def handle_starttag(self, tag, attrs):
        for name, value in attrs:
            self.attributes.append((tag, name, value or ""))
        if tag == "script":
            self.scripts.append({"attrs": dict(attrs), "body": ""})
            self._depth += 1

    def handle_endtag(self, tag):
        if tag == "script" and self._depth:
            self._depth -= 1

    def handle_data(self, data):
        if self._depth and self.scripts:
            self.scripts[-1]["body"] += data


def markup(data) -> _Markup:
    parser = _Markup()
    parser.feed(data.decode() if isinstance(data, bytes) else data)
    parser.close()
    return parser


def page_text(data) -> str:
    """A page with the asset cache-busters stripped out.

    Static URLs carry a digest of the file's contents (`style.css?v=73779eb24032`),
    which is a hex string that can contain anything -- including the digits of
    an HTTP status a refusal page is asserted *not* to name. That is exactly
    what happened: editing a CSS comment changed the digest, the new one
    contained "403", and three tests about information disclosure started
    failing over the contents of a stylesheet.

    Any assertion about what a page says should go through here, so a test
    describes the page rather than the build.
    """
    text = data.decode() if isinstance(data, bytes) else data
    return re.sub(r"\?v=[0-9a-f]+", "", text)


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

    def test_starting_a_login_sweeps_abandoned_ones(self, store):
        """Nothing schedules a purge, so the growth path has to be the one
        that prunes. Abandoned pre-auth rows are never presented again, so
        `load()`'s lazy deletion never sees them and the file grows forever.

        This matters specifically once the Cloudflare Access policy comes off
        (A-14) and /login faces the open internet.
        """
        abandoned = [store.begin_login(f"s{n}").sid for n in range(3)]
        signed_in = store.complete_login(store.begin_login("live").sid, ACTOR, GUILDS)

        store.begin_login("much-later", now=time.time() + 601)

        for sid in abandoned:
            assert store.load(sid) is None
        # The sweep is by expiry, not by age or by kind.
        assert store.load(signed_in.sid) is not None

    def test_the_sweep_uses_the_time_it_was_given(self, store):
        """Otherwise the test above would pass on wall-clock luck alone."""
        abandoned = store.begin_login("s").sid
        store.begin_login("immediately-after")
        assert store.load(abandoned) is not None


class TestRevokingEverySessionAUserHas:
    """A-20: signing out normally leaves the stolen session alone.

    It ends the one in front of you, which is the one an attacker is not
    using. Theirs stays valid until max_age, and nothing else in this codebase
    would ever cut it off.
    """

    def test_it_ends_the_other_sessions_too(self, store):
        first = store.complete_login(store.begin_login("a").sid, ACTOR, GUILDS)
        second = store.complete_login(store.begin_login("b").sid, ACTOR, GUILDS)
        third = store.complete_login(store.begin_login("c").sid, ACTOR, GUILDS)

        assert store.destroy_all_for(ACTOR) == 3
        for session in (first, second, third):
            assert store.load(session.sid) is None

    def test_it_ends_nobody_else_s(self, store):
        mine = store.complete_login(store.begin_login("a").sid, ACTOR, GUILDS)
        theirs = store.complete_login(store.begin_login("b").sid, "77777777", GUILDS)

        assert store.destroy_all_for(ACTOR) == 1
        assert store.load(mine.sid) is None
        assert store.load(theirs.sid) is not None

    def test_a_login_in_progress_is_left_alone(self, store):
        """Pre-auth rows carry no identity to match on and no authority.

        Matching them would mean matching on NULL, which in SQL is not equality
        -- so this pins the behaviour rather than the accident.
        """
        pending = store.begin_login("mid-flight")
        store.complete_login(store.begin_login("b").sid, ACTOR, GUILDS)

        assert store.destroy_all_for(ACTOR) == 1
        assert store.load(pending.sid) is not None

    def test_an_empty_id_revokes_nothing(self, store):
        """The hazard is a NULL match, not a falsy one.

        Written as `WHERE discord_id IS ?` this would be SQL's `IS NULL` and
        would delete every login in flight across the whole site -- signing
        one person out would break everybody's. The early return in
        `destroy_all_for` is belt over braces: `= ?` already matches neither
        NULL nor a real id. What is pinned here is the outcome, which is what
        survives someone rewriting the query.
        """
        session = store.complete_login(store.begin_login("a").sid, ACTOR, GUILDS)
        pending = store.begin_login("mid-flight")

        assert store.destroy_all_for(None) == 0
        assert store.destroy_all_for("") == 0
        assert store.load(session.sid) is not None
        assert store.load(pending.sid) is not None


class TestTheSessionFileIsOwnerOnly:
    """A-21: every live session id sits in this file, in cleartext.

    They are bearer credentials -- anything that can read the file can act as
    any signed-in admin without a password, a cookie, or Discord.
    """

    def test_owner_only_is_asked_for_on_every_platform(self, tmp_path, monkeypatch):
        """The portable half, and the one that actually runs.

        The real mode check below is POSIX-only, and this project runs its
        suite on Windows with no CI executing pytest at all -- so a test
        skipped on Windows is a test that runs nowhere. This one pins the
        request itself, which is platform-independent, and would catch the
        mode being widened or the call being dropped.
        """
        asked = []
        real_chmod = sessions_module.os.chmod
        monkeypatch.setattr(
            sessions_module.os,
            "chmod",
            lambda path, mode: (asked.append(mode), real_chmod(path, mode))[0],
        )
        SessionStore(str(tmp_path / "sessions.sqlite"), 3600)
        assert asked == [0o600]

    @pytest.mark.skipif(
        os.name == "nt", reason="POSIX mode bits are not meaningful on Windows"
    )
    def test_the_database_is_not_readable_by_anyone_else(self, tmp_path):
        """The real thing, on the platform this actually deploys to."""
        path = tmp_path / "sessions.sqlite"
        SessionStore(str(path), 3600)
        assert stat.S_IMODE(path.stat().st_mode) == 0o600

    def test_a_refused_chmod_warns_instead_of_failing(self, tmp_path, monkeypatch, caplog):
        """Taking the dashboard down over a hardening measure would be worse.

        The warning is the whole mitigation in that case, so it is pinned.
        """
        def refuse(*_args, **_kwargs):
            raise PermissionError("nope")

        monkeypatch.setattr(sessions_module.os, "chmod", refuse)
        with caplog.at_level(logging.WARNING):
            store = SessionStore(str(tmp_path / "sessions.sqlite"), 3600)

        # Still usable -- the store came up.
        assert store.load(store.begin_login("s").sid) is not None
        assert "Could not restrict permissions" in caplog.text


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


PANEL_CHANNEL = "800000000003"
SHUT_CHANNEL = "800000000004"


def _locked_panel():
    """A live panel in a channel VRCVerify may no longer send new messages to."""
    return {
        "posted": True,
        "channel_id": PANEL_CHANNEL,
        "message_id": "456",
        "channel_name": "verify",
        "channel_exists": True,
        "channel_postable": False,
        "locale": "en-US",
    }


class TestSettingsPage:
    def test_signed_out_visitors_are_sent_to_the_login_page(self, client):
        response = client.get(f"/guild/{GUILD_IN}/settings")
        assert response.status_code == 302
        assert response.headers["Location"].endswith("/")

    def test_values_are_shown_with_names_not_ids(self, config, store):
        """A read-only field shows the role's name and never its id."""
        test_client, _api = settings_client(
            config, store, settings=make_settings(writable=set())
        )
        page = test_client.get(f"/guild/{GUILD_IN}/settings").data.decode()
        assert "Verified" in page
        assert VERIFIED_ROLE not in page

    def test_an_editable_role_is_labelled_by_name(self, config, store):
        """Editable, the id has to be in the option value -- the label doesn't."""
        test_client, _api = settings_client(config, store)
        page = test_client.get(f"/guild/{GUILD_IN}/settings").data.decode()
        assert f'<option value="{VERIFIED_ROLE}" selected>Verified</option>' in page

    def test_every_read_is_scoped_to_the_session_owner_and_that_guild(
        self, config, store
    ):
        test_client, api = settings_client(config, store)
        test_client.get(f"/guild/{GUILD_IN}/settings")
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
        assert b"Alpha Club" in test_client.get(f"/guild/{GUILD_IN}/settings").data

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

        response = test_client.get(f"/guild/{GUILD_IN}/settings")
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
        return test_client.get(f"/guild/{GUILD_IN}/settings")

    def test_403_and_404_are_byte_identical(self, config, store):
        # One client, so the comparison isn't confounded by the per-session
        # CSRF token in the sign-out form.
        test_client, api = settings_client(
            config, store, errors={"settings": BotAPIError("nope", 403)}
        )
        forbidden = test_client.get(f"/guild/{GUILD_IN}/settings")

        api.errors = {"settings": BotAPIError("nope", 404)}
        missing = test_client.get(f"/guild/{GUILD_IN}/settings")

        assert forbidden.status_code == missing.status_code == 404
        assert forbidden.data == missing.data

    def test_neither_names_the_reason(self, config, store):
        page = page_text(self._response(config, store, 403).data).lower()
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
        page = test_client.get(f"/guild/{GUILD_IN}/settings").data.decode()
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
        page = test_client.get(f"/guild/{GUILD_IN}/settings").data.decode()
        # The values are shown, not hidden or replaced with an upsell.
        assert "Unverified" in page
        assert "Welcome aboard!" in page
        assert "Not applied</span>" in page
        assert "Saved, but not acted on without Premium" in page

    def test_a_premium_server_sees_no_badges(self, config, store):
        test_client, _api = settings_client(
            config, store, settings=make_settings(premium=True)
        )
        page = test_client.get(f"/guild/{GUILD_IN}/settings").data.decode()
        assert "Premium</span>" not in page
        assert "Not applied</span>" not in page
        assert "VRCVerify Premium is active" in page

    def test_auto_verify_is_never_gated(self, config, store):
        """Free for everyone, forever -- mirrors TestAutoVerifyOnJoinIsFree."""
        test_client, _api = settings_client(config, store)
        page = test_client.get(f"/guild/{GUILD_IN}/settings").data.decode()
        section = page.split("Auto-verify on join")[1].split("</div>")[0]
        assert "badge" not in section

    def test_a_missing_auto_verify_column_is_declared(self, config, store):
        test_client, _api = settings_client(
            config, store, settings=make_settings(auto_verify_column=False)
        )
        page = test_client.get(f"/guild/{GUILD_IN}/settings").data.decode()
        assert "missing the auto-verify column" in page


class TestTheUpgradeOffer:
    """Settings points at Subscriptions; it does not sell anything itself.

    This block used to carry the whole pitch and the Discord store link, which
    was right while Subscriptions was a placeholder and there was one way to
    buy. There are two now, and the 6- and 12-month plans are card-only -- so a
    copy of the pitch here could never be more than partly true, and two places
    to buy with two different option sets is how copy drifts apart.
    """

    def test_a_free_server_is_pointed_at_the_subscriptions_page(self, config, store):
        test_client, _api = settings_client(config, store)
        page = test_client.get(f"/guild/{GUILD_IN}/settings").data.decode()
        assert "Upgrade to VRCVerify Premium" in page
        assert f"/guild/{GUILD_IN}/subscription" in page

    def test_settings_no_longer_sells_anything_itself(self, config, store):
        """The pitch lives in exactly one place now.

        Both halves matter: no purchase instructions here, and no store link
        either -- otherwise the page that cannot mention the longer plans is
        still the one an admin reads about buying.
        """
        test_client, _api = settings_client(config, store)
        page = test_client.get(f"/guild/{GUILD_IN}/settings").data.decode()
        assert "/vrcverify_subscription" not in page
        assert "application-directory" not in page
        assert "you choose the server during checkout" not in page

    def test_a_subscribed_server_is_not_sold_to(self, config, store):
        test_client, _api = settings_client(
            config, store, settings=make_settings(premium=True)
        )
        page = test_client.get(f"/guild/{GUILD_IN}/settings").data.decode()
        assert "VRCVerify Premium is active" in page
        assert "/vrcverify_subscription" not in page
        assert "application-directory" not in page

    def test_a_grandfathered_server_is_offered_the_rest(self, config, store):
        """It keeps three features free forever, but not the premium-only set.

        Treating it as already-sold would hide the log channel and branded
        panel behind a badge with no way to reach them.
        """
        test_client, _api = settings_client(
            config, store, settings=make_settings(grandfathered=True)
        )
        page = test_client.get(f"/guild/{GUILD_IN}/settings").data.decode()
        assert "Add VRCVerify Premium" in page
        assert "grandfathered extras stay free" in page
        assert f"/guild/{GUILD_IN}/subscription" in page

    def test_no_offer_when_the_tier_is_switched_off(self, config, store):
        """PREMIUM_SKU_ID unset: every gate answers "allowed", so there is
        nothing to sell and an upgrade block would be charging for what is
        free. This is the payload the bot really sends in that state -- both
        `premium` and `sku_id` say so, so it does not pin any one guard. The
        `enforced` guard itself is pinned directly below.
        """
        test_client, _api = settings_client(
            config,
            store,
            settings=make_settings(premium=True, enforced=False, sku_id=None),
        )
        page = test_client.get(f"/guild/{GUILD_IN}/settings").data.decode()
        assert "Verified role" in page  # the settings page really rendered
        assert "/vrcverify_subscription" not in page
        assert "application-directory" not in page

    def test_enforced_alone_is_enough_to_withhold_the_offer(self):
        """Defence in depth, tested on the pure function.

        A payload saying "not subscribed, here is the SKU" while the tier is
        switched off is not one the bot emits today. It is exactly what a
        half-applied config change would look like, though, and selling a
        subscription for features that are currently free to everyone is the
        one outcome worth being redundant about.
        """
        settings = make_settings(premium=False, enforced=False, sku_id=SKU_ID)
        assert settings_view.build_upgrade(settings, "123") is None

    def test_a_missing_sku_produces_no_link_rather_than_a_broken_one(
        self, config, store
    ):
        """What a half-configured deployment looks like. A link built around a
        missing id would 404 inside Discord instead of failing here."""
        test_client, _api = settings_client(
            config, store, settings=make_settings(sku_id=None)
        )
        page = test_client.get(f"/guild/{GUILD_IN}/settings").data.decode()
        assert "Verified role" in page  # the settings page really rendered
        assert "application-directory" not in page
        assert "store/None" not in page

    def test_the_stale_read_only_notice_is_gone(self, config, store):
        """Every group saves now. The page said otherwise for a while, which
        sent admins to the slash commands past a working Save button."""
        test_client, _api = settings_client(config, store)
        page = test_client.get(f"/guild/{GUILD_IN}/settings").data.decode()
        assert "Verified role" in page  # the settings page really rendered
        assert "Only the instructions panel settings can be changed" not in page


class TestSettingsWarnings:
    """The point of the dashboard: say it now, not at verification time."""

    def test_an_unassignable_verified_role_is_called_out(self, config, store):
        test_client, _api = settings_client(
            config, store, settings=make_settings(values={"role_id": UNASSIGNABLE_ROLE})
        )
        page = test_client.get(f"/guild/{GUILD_IN}/settings").data.decode()
        assert "cannot grant this role" in page
        assert "Server Settings -&gt; Roles" in page

    def test_a_deleted_role_is_called_out(self, config, store):
        test_client, _api = settings_client(
            config, store, settings=make_settings(values={"role_id": "404404404404"})
        )
        page = test_client.get(f"/guild/{GUILD_IN}/settings").data.decode()
        assert "no longer exists" in page

    def test_no_verified_role_is_called_out(self, config, store):
        test_client, _api = settings_client(
            config, store, settings=make_settings(values={"role_id": None})
        )
        page = test_client.get(f"/guild/{GUILD_IN}/settings").data.decode()
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
        page = test_client.get(f"/guild/{GUILD_IN}/settings").data.decode()
        assert "republish age disclosures" in page

    def test_a_locked_panel_channel_is_called_out_without_crying_wolf(
        self, config, store
    ):
        """The panel is not broken -- it just cannot be replaced in place."""
        test_client, _api = settings_client(
            config,
            store,
            panel=_locked_panel(),
            channels=[
                {
                    "id": PANEL_CHANNEL,
                    "name": "verify",
                    "position": 0,
                    "is_news": False,
                    "can_send": False,
                    "can_embed": False,
                },
            ],
        )
        page = test_client.get(f"/guild/{GUILD_IN}/settings").data.decode()
        # Both permissions named: the panel is an embed, so Embed Links alone
        # being off produces this with no other symptom anywhere on the page.
        assert "Send Messages and Embed Links" in page
        assert "still works and can still be refreshed" in page

    def test_a_channel_without_embed_links_is_not_offered_for_the_panel(
        self, config, store
    ):
        """It would accept the log and refuse the panel, so it is not a choice.

        The log picker still offers it -- the verification log is plain text.
        """
        channels = [
            {
                "id": LOG_CHANNEL,
                "name": "verify-log",
                "position": 0,
                "is_news": False,
                "can_send": True,
                "can_embed": False,
            },
        ]
        # Premium, so the log channel's own picker actually renders and the
        # comparison means something.
        test_client, _api = settings_client(
            config, store, channels=channels, settings=make_settings(premium=True)
        )
        page = test_client.get(f"/guild/{GUILD_IN}/settings").data.decode()
        # Offered once, by the log channel's own select -- not by the panel's.
        assert page.count(f'<option value="{LOG_CHANNEL}"') == 1

    def test_the_panels_own_channel_stays_in_the_picker_when_locked(
        self, config, store
    ):
        """Choosing it refreshes, which needs no Send Messages.

        The startup sweep refreshes this very panel unprompted; a page that
        removed the option would be stricter than the bot it configures.
        """
        test_client, _api = settings_client(
            config,
            store,
            panel=_locked_panel(),
            channels=[
                {
                    "id": PANEL_CHANNEL,
                    "name": "verify",
                    "position": 0,
                    "is_news": False,
                    "can_send": False,
                    "can_embed": False,
                },
                {
                    "id": SHUT_CHANNEL,
                    "name": "shut",
                    "position": 1,
                    "is_news": False,
                    "can_send": False,
                    "can_embed": False,
                },
            ],
        )
        page = test_client.get(f"/guild/{GUILD_IN}/settings").data.decode()
        assert f'<option value="{PANEL_CHANNEL}"' in page
        assert f'<option value="{SHUT_CHANNEL}"' not in page


class TestSecondaryReadsDegradeGracefully:
    """A name lookup failing must not cost the whole page."""

    def test_the_page_renders_without_roles(self, config, store):
        test_client, _api = settings_client(
            config, store, errors={"roles": BotAPIError("unavailable", 503)}
        )
        response = test_client.get(f"/guild/{GUILD_IN}/settings")
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
        page = test_client.get(f"/guild/{GUILD_IN}/settings").data.decode()
        assert 'name="role_id"' not in page
        assert 'name="unverified_role_id"' not in page

    def test_an_unresolved_id_is_not_reported_as_deleted(self, config, store):
        """"We could not check" and "it is gone" are different claims."""
        test_client, _api = settings_client(
            config, store, errors={"roles": BotAPIError("unavailable", 503)}
        )
        page = test_client.get(f"/guild/{GUILD_IN}/settings").data.decode()
        assert "no longer exists" not in page

    def test_the_page_renders_without_the_audit_read(self, config, store):
        """An empty history and an unavailable one are different facts."""
        test_client, _api = settings_client(
            config, store, errors={"audit": BotAPIError("unavailable", 503)}
        )
        response = test_client.get(f"/guild/{GUILD_IN}/settings")
        assert response.status_code == 200
        page = response.data.decode()
        assert "Couldn't load the history" in page
        assert "No changes have been made" not in page

    def test_the_page_renders_without_the_panel_read(self, config, store):
        test_client, _api = settings_client(
            config, store, errors={"panel": BotAPIError("unavailable", 503)}
        )
        response = test_client.get(f"/guild/{GUILD_IN}/settings")
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
        # The notice is session state now, not a query parameter, so the
        # redirect target is bare and "Saved." appears on the next render.
        assert response.headers["Location"].endswith(f"/guild/{GUILD_IN}/settings")
        assert "Saved." in test_client.get(response.headers["Location"]).data.decode()
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
        assert leak not in response.headers["Location"]
        page = test_client.get(response.headers["Location"]).data.decode()
        assert leak not in page
        assert "couldn&#39;t be saved" in page

    def test_a_notice_cannot_be_conjured_from_the_url(self, config, store):
        """Notices are session state, so a crafted link cannot show one.

        They used to be query parameters, which meant anyone who could get an
        admin to open a link could show them "Saved." for a save that never
        happened -- most usefully to stop them noticing something was broken.
        """
        test_client, _api, _session = self.logged_in(config, store)
        page = test_client.get(
            f"/guild/{GUILD_IN}/settings?saved=1&panel=replaced&panel_stale=1"
            "&error=%3Cimg+src%3Dx+onerror%3Dalert(1)%3E"
        ).data.decode()
        assert "onerror" not in page
        assert "Saved." not in page
        assert "replaced with a fresh one" not in page
        assert "still shows the old" not in page
        assert "couldn&#39;t be saved" not in page


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
        page = test_client.get(f"/guild/{GUILD_IN}/settings").data.decode()
        assert 'name="instructions_locale"' in page
        assert 'name="panel_embed_color"' in page
        assert 'name="panel_show_icon"' in page
        assert "Save changes" in page

    def test_a_free_server_gets_the_language_control_only(self, config, store):
        """Branding is write-locked, so no control -- but the language is free
        and must stay editable."""
        test_client, _api = settings_client(config, store)
        page = test_client.get(f"/guild/{GUILD_IN}/settings").data.decode()
        assert 'name="instructions_locale"' in page
        assert 'type="color"' not in page
        assert 'name="panel_show_icon"' not in page

    def test_a_field_the_bot_has_not_opened_gets_no_control(self, config, store):
        test_client, _api = settings_client(
            config, store, settings=make_settings(premium=True, writable=set())
        )
        page = test_client.get(f"/guild/{GUILD_IN}/settings").data.decode()
        assert 'name="panel_embed_color"' not in page
        assert 'name="instructions_locale"' not in page
        assert "Save changes" not in page

    def test_the_language_options_come_from_the_bot(self, config, store):
        """Not from the dashboard's display-name table, which may be stale."""
        test_client, _api = settings_client(
            config,
            store,
            settings=make_settings(premium=True),
        )
        page = test_client.get(f"/guild/{GUILD_IN}/settings").data.decode()
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
        page = test_client.get(f"/guild/{GUILD_IN}/settings").data.decode()
        logging_section = page.split("<h2>Logging</h2>")[1]
        assert f'value="{LOG_CHANNEL}"' in logging_section
        assert f'value="{NEWS_CHANNEL}"' not in logging_section

    def test_the_panel_picker_does_offer_announcement_channels(self, config, store):
        """The opposite call from the log channel, and deliberately so.

        A panel is public instructions and /vrcverify_instructions can be run
        in an announcement channel, so excluding them would be stricter than
        the bot. The log channel is excluded because the bot refuses it.
        """
        test_client, _api = settings_client(
            config, store, settings=make_settings(premium=True)
        )
        page = test_client.get(f"/guild/{GUILD_IN}/settings").data.decode()
        panel_form = page.split('class="panel-post"')[1]
        assert f'value="{NEWS_CHANNEL}"' in panel_form

    def test_a_channel_picker_with_nothing_to_pick_is_not_offered(self, config, store):
        test_client, _api = settings_client(
            config,
            store,
            settings=make_settings(premium=True),
            errors={"channels": BotAPIError("unavailable", 503)},
        )
        page = test_client.get(f"/guild/{GUILD_IN}/settings").data.decode()
        assert 'name="verification_log_channel_id"' not in page

    def test_the_custom_message_textarea_carries_the_bot_s_cap(self, config, store):
        test_client, _api = settings_client(
            config, store, settings=make_settings(premium=True)
        )
        page = test_client.get(f"/guild/{GUILD_IN}/settings").data.decode()
        assert 'maxlength="1000"' in page

    # The theme picker is the one form in the app with no CSRF token, and the
    # exception is deliberate: it posts to /prefs/theme, which is reachable
    # signed out -- the sign-in page carries the control and has no token to
    # give it. Named here rather than subtracted silently, so a *second*
    # tokenless form still fails this test.
    CSRF_EXEMPT_FORMS = 1

    def test_every_form_carries_a_csrf_token(self, config, store):
        test_client, _api = settings_client(
            config, store, settings=make_settings(premium=True)
        )
        page = test_client.get(f"/guild/{GUILD_IN}/settings").data.decode()
        assert (
            page.count('name="csrf_token"')
            >= page.count("<form") - self.CSRF_EXEMPT_FORMS
        )

    def test_the_only_tokenless_form_is_the_theme_picker(self, config, store):
        """Pins *which* form the exemption above is spending itself on.

        Without this, the allowance is a hole any future form could fall into
        by accident -- the count would still pass and nobody would look.
        """
        test_client, _api = settings_client(
            config, store, settings=make_settings(premium=True)
        )
        page = test_client.get(f"/guild/{GUILD_IN}/settings").data.decode()
        tokenless = [
            form
            for form in re.findall(r"<form\b.*?</form>", page, re.S)
            if 'name="csrf_token"' not in form
        ]
        assert len(tokenless) == self.CSRF_EXEMPT_FORMS
        assert 'action="/prefs/theme"' in tokenless[0]


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

    def test_fonts_may_come_only_from_us(self, client):
        """`font-src 'self'` names no CDN, and that is the point.

        A font loaded from a third party tells them who is opening the
        dashboard and lets them break it by going down. The one face we use is
        in the image.
        """
        policy = client.get("/").headers["Content-Security-Policy"]
        assert "font-src 'self'" in policy
        for cdn in ("fonts.googleapis.com", "fonts.gstatic.com", "cdn.jsdelivr.net"):
            assert cdn not in policy

    def test_nothing_can_be_fetched_at_runtime(self, client):
        """No `connect-src`, so `default-src 'none'` blocks fetch and XHR.

        Deliberate rather than forgotten: the one script here has no business
        talking to anything, and a background request is the shape most
        exfiltration takes. Adding one is a decision to make on purpose.
        """
        policy = client.get("/").headers["Content-Security-Policy"]
        assert "connect-src" not in policy

    def test_pages_are_never_cached_but_assets_always_are(self, config, store):
        """The one place `no-store` is relaxed, and why it is safe there.

        Every page can contain a guild name, a plan state, and the CSRF token,
        so none of them may sit in a shared cache. Static files contain the
        same bytes for a signed-out stranger, and their URLs carry a content
        digest -- so a deploy changes the URL rather than leaving anyone on a
        stale stylesheet.

        Getting this backwards in either direction is a real bug: `no-store` on
        assets re-downloaded a 48KB font on every page view, and dropping it
        from pages would put an admin's session-shaped HTML in a proxy.
        """
        test_client, _api = settings_client(config, store)

        for path in ("/", f"/guild/{GUILD_IN}", f"/guild/{GUILD_IN}/settings"):
            assert test_client.get(path).headers["Cache-Control"] == "no-store"

        for path in ("/static/style.css", "/static/app.js"):
            headers = test_client.get(path).headers
            assert "public" in headers["Cache-Control"]
            assert "immutable" in headers["Cache-Control"]

    def test_asset_urls_carry_a_content_digest(self, config, store):
        """Without this the year-long cache above would be reckless."""
        test_client, _api = settings_client(config, store)
        page = test_client.get(f"/guild/{GUILD_IN}/settings").data.decode()
        assets = re.findall(r'(?:href|src)="(/static/[^"]+)"', page)
        assert assets, "no static assets referenced"
        for url in assets:
            assert re.search(r"\?v=[0-9a-f]{6,}$", url), f"undigested asset: {url}"
            assert test_client.get(url).status_code == 200

    def test_the_guard_tracks_forms_separately(self):
        """The bug the first version of this script shipped with.

        A single page-wide `dirty` flag reads as obviously correct and defeats
        the whole purpose: saving group B clears the flag group A set, so the
        one sequence the guard exists to catch -- edit one group, save another
        -- passes silently. This asserts the flag is per form.
        """
        import dashboard

        js = open(
            os.path.join(os.path.dirname(dashboard.__file__), "static", "app.js"),
            encoding="utf-8",
        ).read()
        # Cleared for one form, not for the page.
        assert "setDirty(form, false)" in js
        assert "dirty = false" not in js

    def test_the_guard_reaches_the_settings_forms(self, config, store):
        """A marker that stops matching is a guard that silently does nothing."""
        test_client, _api = settings_client(config, store)
        page = test_client.get(f"/guild/{GUILD_IN}/settings").data.decode()
        assert page.count("data-guard") >= 1
        # The panel-post form is an action rather than a set of edits, so it is
        # deliberately not guarded -- warning there would fire on a button that
        # loses nothing.
        assert 'class="panel-post"' not in page or "data-guard" in page

    def test_the_script_never_talks_to_anything(self):
        """No `connect-src`, so a request would be blocked -- but not written.

        Cheaper to assert the file contains no network call than to discover a
        blocked one in a console.
        """
        import dashboard

        js = open(
            os.path.join(os.path.dirname(dashboard.__file__), "static", "app.js"),
            encoding="utf-8",
        ).read()
        # Comments stripped first: the file documents at length why it uses
        # none of these, and those sentences would otherwise fail the test that
        # checks it doesn't.
        code = re.sub(r"/\*.*?\*/", "", js, flags=re.S)
        code = re.sub(r"^\s*//.*$", "", code, flags=re.M)
        for forbidden in ("fetch(", "XMLHttpRequest", "innerHTML", "eval(", "import("):
            assert forbidden not in code, f"app.js should not use {forbidden}"

    def test_identical_bytes_digest_identically(self, config, store):
        """Restarting must not bust the cache.

        The digest has to come from the file's contents and nothing else -- no
        timestamp, no start time, no random salt. If it moved on restart, every
        redeploy would re-download the font for everyone, which is the cost the
        digest exists to avoid.
        """
        first_app = create_app(config, store=store, client=FakeBotAPI())
        second_app = create_app(config, store=store, client=FakeBotAPI())
        with first_app.test_request_context():
            first = first_app.jinja_env.globals["asset"]("style.css")
        with second_app.test_request_context():
            second = second_app.jinja_env.globals["asset"]("style.css")
        assert first == second

    def test_a_refusal_page_says_nothing_regardless_of_the_digest(
        self, config, store, monkeypatch
    ):
        """The disclosure guarantee must not depend on a hash.

        A content digest is a hex string, so it can contain the digits of the
        status the page is not allowed to name -- and one did, which quietly
        turned three information-disclosure tests into tests of the
        stylesheet's contents. This forces the worst case and checks the page
        still gives nothing away.
        """
        app = create_app(config, store=store, client=FakeBotAPI(
            errors={"settings": BotAPIError("nope", 403)}
        ))
        app.config.update(TESTING=True)
        # A digest that contains every status this page must not confirm.
        monkeypatch.setitem(
            app.jinja_env.globals,
            "asset",
            lambda filename: f"/static/{filename}?v=403404503dead",
        )
        test_client = app.test_client()
        login_as(test_client, store)

        response = test_client.get(f"/guild/{GUILD_IN}/settings")
        page = page_text(response.data).lower()
        assert response.status_code == 404
        for status in ("403", "404", "503"):
            assert status not in page

    def test_a_missing_asset_still_renders_a_usable_url(self, config, store):
        """A broken page either way, but a legible 404 beats a crash."""
        app = create_app(config, store=store, client=FakeBotAPI())
        with app.test_request_context():
            url = app.jinja_env.globals["asset"]("does-not-exist.css")
        assert url.endswith("/static/does-not-exist.css")

    def test_the_font_is_actually_in_the_image(self):
        """The @font-face URL has to resolve to a file we ship.

        A missing font fails silently -- `font-display: swap` means the page
        renders in the system face and nobody notices the 404 until they
        compare screenshots.
        """
        import dashboard

        static = os.path.join(os.path.dirname(dashboard.__file__), "static")
        font = os.path.join(static, "fonts", "inter-latin-var.woff2")
        assert os.path.exists(font), "the vendored font is missing"
        with open(font, "rb") as handle:
            assert handle.read(4) == b"wOF2", "not a WOFF2 file"
        # Vendoring a font means vendoring its licence.
        assert os.path.exists(os.path.join(static, "fonts", "Inter-LICENSE.txt"))

    def test_the_stylesheet_asks_for_no_external_origin(self):
        """One stylesheet, and every URL in it relative.

        `style-src 'self'` would block a remote @import anyway; this catches it
        at the point where it would otherwise be written, rather than at the
        point where a page silently loses its font.
        """
        import dashboard

        css_path = os.path.join(
            os.path.dirname(dashboard.__file__), "static", "style.css"
        )
        css = open(css_path, encoding="utf-8").read()
        # Comments stripped first: the file explains *why* it loads nothing
        # remotely, and those sentences would otherwise trip the check.
        stripped = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
        assert "http://" not in stripped
        assert "https://" not in stripped
        assert "@import" not in stripped
        # Every url() is relative to the stylesheet, i.e. served by us.
        for url in re.findall(r"url\((.*?)\)", stripped):
            assert not url.strip("\"'").startswith(("http", "//"))

    def test_every_script_is_external_and_empty(self, config, store):
        """`script-src 'self'` allows a file; it forbids inline code.

        The app used to carry no script at all, which made this test a simple
        "no <script>". It now carries exactly one -- the unsaved-changes guard
        -- so the assertion moved to the property that actually matters and is
        still absolute: a script tag must name a `src` on our own origin and
        must have no body. An inline block would be silently dropped by the
        browser, and a CDN URL would hand a third party execution on the page
        that holds admin sessions.
        """
        test_client, _api = settings_client(config, store)
        for path in ("/", f"/guild/{GUILD_IN}", f"/guild/{GUILD_IN}/settings"):
            for script in markup(test_client.get(path).data).scripts:
                assert script["body"].strip() == "", f"inline script on {path}"
                src = script["attrs"].get("src") or ""
                assert src.startswith("/static/"), f"non-local script on {path}"

    def test_an_uppercase_script_tag_would_still_be_caught(self):
        """The bug CodeQL found, pinned so it cannot come back.

        `<SCRIPT>` is as valid as `<script>` and runs identically. A check that
        misses it is a check that would pass on the page it exists to reject.
        """
        found = markup('<SCRIPT SRC="https://evil.example/x.js">bad()</SCRIPT>')
        assert len(found.scripts) == 1
        assert found.scripts[0]["body"] == "bad()"
        assert found.scripts[0]["attrs"]["src"] == "https://evil.example/x.js"

    # The scripts this application has decided to load, in order. Two, and each
    # one is a decision with a reason written in its own header:
    #
    #   app.js   -- warns that you edited one settings group and saved another.
    #   prefs.js -- makes the theme switch instant. The picker works without it.
    #
    # A third must not arrive quietly, which is what the test below is for.
    EXPECTED_SCRIPTS = ["app.js", "prefs.js"]

    def test_the_only_scripts_are_the_ones_we_meant_to_add(self, config, store):
        """A script arriving without a decision should fail here."""
        test_client, _api = settings_client(config, store)
        scripts = markup(test_client.get(f"/guild/{GUILD_IN}/settings").data).scripts
        assert len(scripts) == len(self.EXPECTED_SCRIPTS)
        for script, expected in zip(scripts, self.EXPECTED_SCRIPTS):
            assert expected in script["attrs"].get("src", "")

    def test_no_script_is_inline(self, config, store):
        """`script-src 'self'` blocks inline script, so an inline block would
        not run -- but it would also not error anywhere a developer looks. The
        page should never contain one to begin with."""
        test_client, _api = settings_client(config, store)
        scripts = markup(test_client.get(f"/guild/{GUILD_IN}/settings").data).scripts
        for script in scripts:
            assert script["attrs"].get("src"), "a script with no src is inline"
            assert not script["body"].strip()

    def test_the_page_still_works_without_the_script(self, config, store):
        """Progressive enhancement, pinned.

        Nothing the script does is required to render, navigate or save. The
        server output must therefore contain every control fully formed -- no
        element that only becomes usable once JavaScript has run.
        """
        test_client, _api = settings_client(config, store)
        response = test_client.get(f"/guild/{GUILD_IN}/settings")
        page = response.data.decode()
        # Real actions, present in the markup rather than wired up later.
        assert 'action="/guild/' in page
        assert 'type="submit"' in page

        # No inline handler of any kind, found by attribute name rather than by
        # searching for the two spellings that came to mind -- this catches
        # onload, onerror and ONCLICK as well, and they are all blocked by
        # `script-src 'self'` anyway, which means one would fail silently.
        for tag, name, _value in markup(response.data).attributes:
            assert not name.startswith("on"), f"inline handler {name} on <{tag}>"

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
        for path in ("/", f"/guild/{GUILD_IN}/settings"):
            page = test_client.get(path).data
            assert b"style=" not in page, f"inline style on {path}"
        page = test_client.get(f"/guild/{GUILD_IN}/settings").data
        assert b'fill="#ff00ff"' in page   # the panel colour
        assert b'fill="#5865f2"' in page   # the verified role's colour

    def test_the_settings_page_closes_every_container_it_opens(
        self, config, store
    ):
        """Each settings group is its own `<section class="panel">`, which put
        the open and close tags on opposite sides of a `{% for %}`.

        A stray `</section>` renders fine in a browser -- it just silently
        reparents everything after it -- so nothing else here would notice.
        The groups are built in a loop, so getting it wrong once gets it wrong
        for every server.
        """
        import html.parser

        class Balance(html.parser.HTMLParser):
            watched = {"section", "form", "dl", "div", "ul"}

            def __init__(self):
                super().__init__()
                self.stack = []
                self.errors = []

            def handle_starttag(self, tag, _attrs):
                if tag in self.watched:
                    self.stack.append(tag)

            def handle_endtag(self, tag):
                if tag not in self.watched:
                    return
                if not self.stack:
                    self.errors.append(f"</{tag}> with nothing open")
                elif self.stack[-1] != tag:
                    self.errors.append(f"</{tag}> closed a <{self.stack[-1]}>")
                    self.stack.pop()
                else:
                    self.stack.pop()

        test_client, _api = settings_client(config, store)
        parser = Balance()
        parser.feed(test_client.get(f"/guild/{GUILD_IN}/settings").data.decode())

        assert not parser.errors, parser.errors
        assert not parser.stack, f"never closed: {parser.stack}"

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

    def test_signing_out_everywhere_requires_the_csrf_token(self, client, store):
        """Otherwise it is a one-click denial of service on any admin who can
        be made to load an attacker's page."""
        session = login_as(client, store)
        other = store.complete_login(store.begin_login("b").sid, ACTOR, GUILDS)
        assert (
            client.post("/logout/everywhere", data={"csrf_token": "wrong"}).status_code
            == 400
        )
        assert store.load(session.sid) is not None
        assert store.load(other.sid) is not None

    def test_signing_out_everywhere_ends_the_session_it_cannot_see(
        self, client, store
    ):
        """The whole point: /logout leaves this one alive, and this does not."""
        session = login_as(client, store)
        elsewhere = store.complete_login(store.begin_login("b").sid, ACTOR, GUILDS)

        response = client.post(
            "/logout/everywhere", data={"csrf_token": session.csrf_token}
        )
        assert response.status_code == 302
        assert store.load(session.sid) is None
        assert store.load(elsewhere.sid) is None

    def test_an_ordinary_sign_out_still_leaves_the_others_alone(self, client, store):
        """Pins the difference, so the two routes cannot quietly converge."""
        session = login_as(client, store)
        elsewhere = store.complete_login(store.begin_login("b").sid, ACTOR, GUILDS)

        client.post("/logout", data={"csrf_token": session.csrf_token})
        assert store.load(elsewhere.sid) is not None

    def test_both_sign_out_controls_are_on_the_page(self, client, store):
        """A revocation control nobody can find revokes nothing."""
        login_as(client, store)
        page = client.get("/").data.decode()
        assert 'action="/logout"' in page
        assert 'action="/logout/everywhere"' in page

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


class TestPostingThePanel:
    """The one control that makes the bot act in a server."""

    def logged_in(self, config, store, **kwargs):
        api = FakeBotAPI(**kwargs)
        app = create_app(config, store=store, client=api)
        app.config.update(TESTING=True)
        test_client = app.test_client()
        return test_client, api, login_as(test_client, store)

    def post(self, test_client, session, **form):
        form.setdefault("csrf_token", session.csrf_token)
        return test_client.post(f"/guild/{GUILD_IN}/panel/post", data=form)

    def page_after_saving(self, config, store, **api_kwargs):
        """The settings page an admin lands on after a real save."""
        test_client, _api, session = self.logged_in(config, store, **api_kwargs)
        response = test_client.post(
            f"/guild/{GUILD_IN}/panel",
            data={"csrf_token": session.csrf_token, "instructions_locale": "ja"},
        )
        return test_client.get(response.headers["Location"]).data.decode()

    def test_the_chosen_channel_reaches_the_bot(self, config, store):
        test_client, api, session = self.logged_in(config, store)
        response = self.post(test_client, session, panel_channel_id=LOG_CHANNEL)
        assert response.status_code == 302
        assert api.panel_posts == [(ACTOR, GUILD_IN, LOG_CHANNEL)]

    @pytest.mark.parametrize(
        "action, expected",
        [
            ("posted", "Panel posted."),
            ("refreshed", "refreshed rather than posted again"),
            ("moved", "old one is still up"),
        ],
    )
    def test_what_the_bot_did_is_reported_back(self, config, store, action, expected):
        """The bot decides between post, refresh and move; the page explains it."""
        test_client, _api, session = self.logged_in(
            config, store, panel_result={"action": action, "channel_id": LOG_CHANNEL}
        )
        response = self.post(test_client, session, panel_channel_id=LOG_CHANNEL)
        page = test_client.get(response.headers["Location"]).data.decode()
        assert expected in page

    def test_an_unknown_action_says_nothing_rather_than_echoing_it(
        self, config, store
    ):
        test_client, _api, session = self.logged_in(
            config, store, panel_result={"action": "<script>alert(1)</script>"}
        )
        response = self.post(test_client, session, panel_channel_id=LOG_CHANNEL)
        page = test_client.get(response.headers["Location"]).data.decode()
        assert "alert(1)" not in page

    def test_it_needs_the_csrf_token(self, config, store):
        test_client, api, _session = self.logged_in(config, store)
        response = test_client.post(
            f"/guild/{GUILD_IN}/panel/post",
            data={"csrf_token": "wrong", "panel_channel_id": LOG_CHANNEL},
        )
        assert response.status_code == 400
        assert api.panel_posts == []

    def test_a_signed_out_visitor_cannot_post_a_panel(self, client, bot_api):
        response = client.post(
            f"/guild/{GUILD_IN}/panel/post", data={"panel_channel_id": LOG_CHANNEL}
        )
        assert response.status_code == 302
        assert not getattr(bot_api, "panel_posts", [])

    def test_no_channel_means_no_call(self, config, store):
        test_client, api, session = self.logged_in(config, store)
        self.post(test_client, session)
        assert api.panel_posts == []

    def test_a_refusal_is_explained(self, config, store):
        """In the panel's own words, not the log channel's.

        Both raise channel_not_writable, but a panel is an embed, so it needs
        Embed Links as well -- and "it can't log there" is a sentence about a
        setting this button has nothing to do with.
        """
        test_client, _api, session = self.logged_in(
            config,
            store,
            errors={"post_panel": BotAPIError("channel_not_writable", 400)},
        )
        response = self.post(test_client, session, panel_channel_id=LOG_CHANNEL)
        page = test_client.get(response.headers["Location"]).data.decode()
        assert "Embed Links" in page
        assert "can&#39;t log there" not in page

    def test_an_unrecognised_action_never_reaches_the_url(self, config, store):
        """The one place a bot value used to travel without being looked up."""
        test_client, _api, session = self.logged_in(
            config, store, panel_result={"action": "\r\nSet-Cookie: x=1", "channel_id": "1"}
        )
        response = self.post(test_client, session, panel_channel_id=LOG_CHANNEL)
        assert "Set-Cookie" not in response.headers["Location"]
        page = test_client.get(response.headers["Location"]).data.decode()
        assert "Panel posted." in page

    def test_a_stale_panel_after_a_save_is_reported(self, config, store):
        """Saved is true; "your panel shows it" is not, and only one is obvious."""
        page = self.page_after_saving(config, store, saved={"panel_stale": "frozen"})
        assert "still shows the old" in page

    def test_a_clean_save_says_nothing_about_the_panel(self, config, store):
        page = self.page_after_saving(config, store)
        assert "Saved." in page
        assert "still shows the old" not in page

    def test_a_notice_is_shown_once_and_not_again_on_reload(self, config, store):
        """Otherwise every later page load claims the last save just happened."""
        test_client, _api, session = self.logged_in(config, store)
        response = test_client.post(
            f"/guild/{GUILD_IN}/panel",
            data={"csrf_token": session.csrf_token, "instructions_locale": "ja"},
        )
        target = response.headers["Location"]
        assert "Saved." in test_client.get(target).data.decode()
        assert "Saved." not in test_client.get(target).data.decode()

    def test_an_unrecognised_panel_refusal_never_reaches_the_page_as_text(
        self, config, store
    ):
        leak = "surprising-internal-detail"
        test_client, _api, session = self.logged_in(
            config, store, errors={"post_panel": BotAPIError(leak, 400)}
        )
        response = self.post(test_client, session, panel_channel_id=LOG_CHANNEL)
        page = test_client.get(response.headers["Location"]).data.decode()
        assert leak not in page
        assert "couldn&#39;t be posted" in page


class TestTheChangeHistoryOfPanelActions:
    """The panel row is the one entry whose pair is not (before, after).

    The bot stores (what it did, where), so both halves need resolving
    differently -- without that it rendered as the raw column name and a bare
    channel id, for the feature this branch exists to add.
    """

    def entry(self, action="posted", channel=LOG_CHANNEL):
        return [
            {
                "field": "instructions_panel",
                "old_value": action,
                "new_value": channel,
                "actor_id": ACTOR,
                "actor_name": "Sasha",
                "changed_at": "2026-08-11T07:11:36.118000+00:00",
            }
        ]

    def test_it_reads_as_an_action_and_a_channel(self):
        row = settings_view.build_audit(
            self.entry(), DEFAULT_ROLES, DEFAULT_CHANNELS
        )[0]
        assert row["label"] == "Instructions panel"
        assert row["old"] == "posted in"
        assert row["new"] == "#verify-log"

    def test_the_destructive_action_is_named_plainly(self):
        row = settings_view.build_audit(
            self.entry(action="replaced"), DEFAULT_ROLES, DEFAULT_CHANNELS
        )[0]
        assert row["old"] == "replaced in"

    def test_a_failed_channel_read_does_not_claim_the_channel_is_gone(self):
        """"We couldn't check" and "it was deleted" are different facts."""
        row = settings_view.build_audit(self.entry(), DEFAULT_ROLES, None)[0]
        assert "no longer exists" not in row["new"]
        assert LOG_CHANNEL in row["new"]

    def test_a_failed_role_read_does_not_claim_the_role_is_gone(self):
        entries = [
            {
                "field": "role_id",
                "old_value": None,
                "new_value": VERIFIED_ROLE,
                "actor_id": ACTOR,
                "actor_name": "Sasha",
                "changed_at": None,
            }
        ]
        row = settings_view.build_audit(entries, None, DEFAULT_CHANNELS)[0]
        assert "no longer exists" not in row["new"]

    def test_a_timestamp_that_is_not_a_string_does_not_break_the_page(self):
        entries = self.entry()
        entries[0]["changed_at"] = 1234
        assert settings_view.build_audit(entries, DEFAULT_ROLES, DEFAULT_CHANNELS)[0][
            "when_text"
        ] == ""


class TestTheChangeHistory:
    def test_it_names_the_setting_the_actor_and_both_values(self, config, store):
        test_client, _api = settings_client(config, store, audit=AUDIT_ENTRIES)
        page = test_client.get(f"/guild/{GUILD_IN}/settings").data.decode()
        assert "Verified role" in page
        assert "Sasha" in page
        # The id is resolved to the role's name, as on the settings above.
        assert "not set &rarr; Verified" in page or "not set → Verified" in page

    def test_an_actor_who_left_is_shown_by_id(self, config, store):
        test_client, _api = settings_client(config, store, audit=AUDIT_ENTRIES)
        page = test_client.get(f"/guild/{GUILD_IN}/settings").data.decode()
        assert "ID 555555555555" in page

    def test_a_colour_reads_as_a_colour(self, config, store):
        test_client, _api = settings_client(config, store, audit=AUDIT_ENTRIES)
        assert "#ff0000" in test_client.get(f"/guild/{GUILD_IN}/settings").data.decode()

    def test_an_empty_history_says_so(self, config, store):
        test_client, _api = settings_client(config, store, audit=[])
        assert b"No changes have been made" in test_client.get(
            f"/guild/{GUILD_IN}/settings"
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


class TestTheOverviewPage:
    """The landing page after picking a server."""

    def test_signed_out_visitors_are_sent_to_the_login_page(self, client):
        response = client.get(f"/guild/{GUILD_IN}")
        assert response.status_code == 302
        assert response.headers["Location"].endswith("/")

    def test_the_picker_links_here_not_to_settings(self, config, store):
        test_client, _api = settings_client(config, store)
        page = test_client.get("/").data.decode()
        assert f'href="/guild/{GUILD_IN}"' in page
        assert f'href="/guild/{GUILD_IN}/settings"' not in page

    def test_it_shows_the_counts(self, config, store):
        test_client, _api = settings_client(config, store)
        page = test_client.get(f"/guild/{GUILD_IN}").data.decode()
        assert "1,284" in page  # members, with a thousands separator
        assert "Today (UTC)" in page
        assert "Last 7 days" in page
        assert "Last 30 days" in page

    def test_it_reads_the_overview_and_nothing_else(self, config, store):
        """One call, so one Administrator check and one round trip."""
        test_client, api = settings_client(config, store)
        test_client.get(f"/guild/{GUILD_IN}")
        assert {what for what, _, _ in api.reads} == {"overview"}
        for _what, actor, guild in api.reads:
            assert actor == ACTOR
            assert guild == GUILD_IN


class TestZeroAndBlankAreDifferentAnswers:
    """The distinction the whole page rests on.

    A window that is covered and empty is `0` -- a panel is up and nobody is
    using it, which is the problem an admin came here to find. A window with
    nothing behind it is blank, because no number would be true. Rendering
    either as the other tells them something false.
    """

    def _page(self, config, store, **overview):
        test_client, _api = settings_client(
            config, store, overview=make_overview(**overview)
        )
        return test_client.get(f"/guild/{GUILD_IN}").data.decode()

    # The em dash also appears in the page title, so these look at the tile
    # markup rather than at the character anywhere on the page.
    ZERO = '<span class="tile-value">0</span>'
    BLANK = '<span class="tile-value">—</span>'

    def test_a_real_zero_is_printed_as_zero(self, config, store):
        page = self._page(config, store, today=0, last_7_days=0, last_30_days=0)
        assert page.count(self.ZERO) == 3
        assert self.BLANK not in page

    def test_a_window_with_no_data_is_blank_not_zero(self, config, store):
        page = self._page(config, store, last_30_days=None)
        assert self.BLANK in page
        assert "Only counting since 2026-06-01" in page

    def test_the_windows_are_always_labelled_even_when_blank(self, config, store):
        """The tiles stay in place. A missing tile looks like a broken page."""
        page = self._page(
            config, store, today=None, last_7_days=None, last_30_days=None
        )
        for label in ("Today (UTC)", "Last 7 days", "Last 30 days"):
            assert label in page

    def test_an_unreadable_rollup_says_so(self, config, store):
        """Not blank, and certainly not zero -- this is the page failing."""
        page = self._page(config, store, known=False)
        assert "Couldn&#39;t check" in page
        assert self.ZERO not in page
        assert self.BLANK not in page

    def test_a_missing_total_omits_the_tile_rather_than_showing_zero(
        self, config, store
    ):
        page = self._page(config, store, total=None)
        assert "Verified, all time" not in page

    def test_an_unknown_member_count_is_not_zero(self, config, store):
        page = self._page(config, store, member_count=None)
        assert "Couldn&#39;t check" in page


class TestTheOverviewSuggestsOneNextStep:
    def _page(self, config, store, **overview):
        test_client, _api = settings_client(
            config, store, overview=make_overview(**overview)
        )
        return test_client.get(f"/guild/{GUILD_IN}").data.decode()

    def test_a_configured_server_is_not_nagged(self, config, store):
        page = self._page(config, store)
        assert "No verified role" not in page
        assert "No instructions panel" not in page

    def test_no_verified_role_comes_first(self, config, store):
        """It blocks everything after it, including the panel being useful."""
        page = self._page(
            config,
            store,
            configured={
                "verified_role": False,
                "unverified_role": False,
                "log_channel": False,
                "auto_verify": True,
            },
            panel={"posted": False},
        )
        assert "No verified role is set" in page
        assert "No instructions panel is posted" not in page

    def test_a_missing_panel_is_reported_when_the_role_is_fine(self, config, store):
        page = self._page(config, store, panel={"posted": False})
        assert "No instructions panel is posted" in page


class TestEverySectionFailsTheSameWay:
    """The oracle only has to exist on one route to be worth using.

    Three sections that each decided how to refuse would be three chances for
    one of them to be more forthcoming. They share `_guild_page_unavailable`,
    and these tests are what stops a fourth section quietly not doing so.
    """

    SECTIONS = ("", "/settings", "/subscription")

    @pytest.mark.parametrize("suffix", SECTIONS)
    def test_403_and_404_are_byte_identical(self, config, store, suffix):
        test_client, api = settings_client(
            config,
            store,
            errors={
                "settings": BotAPIError("nope", 403),
                "overview": BotAPIError("nope", 403),
            },
        )
        forbidden = test_client.get(f"/guild/{GUILD_IN}{suffix}")

        api.errors = {
            "settings": BotAPIError("nope", 404),
            "overview": BotAPIError("nope", 404),
        }
        missing = test_client.get(f"/guild/{GUILD_IN}{suffix}")

        assert forbidden.status_code == missing.status_code == 404
        assert forbidden.data == missing.data

    @pytest.mark.parametrize("suffix", SECTIONS)
    def test_every_section_refuses_with_the_same_page(self, config, store, suffix):
        """Including the placeholder, which has nothing on it to protect.

        Subscriptions calls the bot before rendering an empty page purely so
        that it cannot become the one route that answers differently.
        """
        test_client, _api = settings_client(
            config,
            store,
            errors={
                "settings": BotAPIError("nope", 403),
                "overview": BotAPIError("nope", 403),
            },
        )
        page = page_text(test_client.get(f"/guild/{GUILD_IN}{suffix}").data).lower()
        assert "administrator permission there" in page
        assert "403" not in page

    @pytest.mark.parametrize("suffix", SECTIONS)
    def test_an_unavailable_bot_is_a_503_everywhere(self, config, store, suffix):
        test_client, _api = settings_client(
            config,
            store,
            errors={
                "settings": BotAPIError("down", 503),
                "overview": BotAPIError("down", 503),
            },
        )
        assert test_client.get(f"/guild/{GUILD_IN}{suffix}").status_code == 503


class TestTheSubscriptionPage:
    """Reachability only. Every page STATE is pinned in test_subscription_page.

    These two stay here because they are about the route existing and about the
    sidebar not leading to a dead end, which is this file's concern.
    """

    def test_it_renders(self, config, store):
        test_client, _api = settings_client(config, store)
        response = test_client.get(f"/guild/{GUILD_IN}/subscription")
        assert response.status_code == 200
        assert b"Subscriptions" in response.data

    def test_the_discord_path_is_offered_when_stripe_is_switched_off(
        self, config, store
    ):
        """With no Stripe configured this page IS the old placeholder's job,
        done properly: the Discord route, and no card anywhere.

        Note the store link now belongs here rather than on Settings -- this is
        the page about buying, and it is the only one that can describe both
        ways to do it.
        """
        test_client, _api = settings_client(config, store)
        page = test_client.get(f"/guild/{GUILD_IN}/subscription").data.decode()
        assert "/vrcverify_subscription" in page
        assert "application-directory" in page
        assert "Pay by card" not in page
        assert "/subscription/checkout" not in page


class TestTheSidebar:
    def test_it_lists_every_section_on_a_guild_page(self, config, store):
        test_client, _api = settings_client(config, store)
        page = test_client.get(f"/guild/{GUILD_IN}").data.decode()
        assert f'href="/guild/{GUILD_IN}"' in page
        assert f'href="/guild/{GUILD_IN}/settings"' in page
        assert f'href="/guild/{GUILD_IN}/subscription"' in page

    def test_no_section_is_reduced_to_its_initial(self, config, store):
        """The links used to carry the section's first letter, which was
        ambiguous as well as ugly: Settings and Subscriptions share an initial,
        so the rail offered two identical marks for two different pages.

        Icons replaced them. #133 phase 2 then stopped hiding those icons when
        the sidebar is expanded -- so the rule this pins is no longer "a label
        stands alone", it is "a letter never stands in for a section".
        """
        test_client, _api = settings_client(config, store)
        page = test_client.get(f"/guild/{GUILD_IN}/settings").data.decode()

        for label in ("Overview", "Settings", "Subscriptions"):
            assert f'>{label[:1]}</span>' not in page
            assert f'<span class="side-text">{label}</span>' in page

    def test_it_marks_the_current_section(self, config, store):
        test_client, _api = settings_client(config, store)
        page = test_client.get(f"/guild/{GUILD_IN}/settings").data.decode()
        # The accessible half of the highlight, not just a class.
        assert 'aria-current="page"' in page

    def test_it_offers_the_way_back_to_the_server_list(self, config, store):
        test_client, _api = settings_client(config, store)
        page = test_client.get(f"/guild/{GUILD_IN}/settings").data.decode()
        assert "All servers" in page

    def test_the_picker_has_no_sidebar(self, config, store):
        """It has nothing to navigate. The chrome would be empty."""
        test_client, _api = settings_client(config, store)
        page = test_client.get("/").data.decode()
        assert 'class="sidebar"' not in page
        assert "hamburger" not in page

    def test_the_login_page_has_no_sidebar(self, client):
        page = client.get("/").data.decode()
        assert 'class="sidebar"' not in page


class TestTheSidebarLayout:
    """How the sidebar is ordered and what shows in each state (#133 phase 2)."""

    @staticmethod
    def _sidebar(page: str) -> str:
        start = page.index('<nav class="sidebar"')
        return page[start : page.index("</nav>", start)]

    def _page(self, config, store, collapsed=False):
        test_client, _api = settings_client(config, store)
        if collapsed:
            test_client.set_cookie("vrcverify_nav", "1", domain="localhost")
        return test_client.get(f"/guild/{GUILD_IN}/settings").data.decode()

    def test_every_section_renders_an_icon(self, config, store):
        sidebar = self._sidebar(self._page(config, store))
        assert sidebar.count('class="side-mark"') == 3
        assert sidebar.count('<span class="side-text">') >= 3

    def test_the_icons_are_not_hidden_when_the_sidebar_is_expanded(self):
        """This has to read the stylesheet, and the first version of it did
        not -- which made it pass against the behaviour it was written to
        change.

        The icons were ALWAYS in the markup; `display: none` is what kept them
        off the expanded sidebar. So counting `side-mark` in the HTML says
        nothing at all about whether they are visible, and a markup assertion
        here is worse than none: it reads like coverage.
        """
        import dashboard

        with open(
            os.path.join(os.path.dirname(dashboard.__file__), "static", "style.css"),
            encoding="utf-8",
        ) as handle:
            css = handle.read()

        rule = re.search(r"^\.side-mark\s*\{([^}]*)\}", css, flags=re.M)
        assert rule, ".side-mark has no rule of its own"
        assert "display: none" not in rule.group(1)
        # And nothing may re-reveal them only on the rail, which is what the
        # pair of rules used to do between them.
        assert ".collapsed .side-mark" not in css

    def test_the_collapsed_rail_keeps_the_icons_and_the_links(self, config, store):
        """Labels go, targets stay. A collapsed sidebar that drops its links
        is a broken one."""
        sidebar = self._sidebar(self._page(config, store, collapsed=True))
        assert sidebar.count('class="side-mark"') == 3
        for endpoint in ("", "/settings", "/subscription"):
            assert f'href="/guild/{GUILD_IN}{endpoint}"' in sidebar

    def test_the_way_out_comes_before_the_server_it_leaves(self, config, store):
        """Order on screen is order in the hierarchy.

        "All servers" sat at the foot of the sidebar under a hairline, where it
        read as a fourth section. The three real sections navigate *within* one
        server; this one leaves it, so it belongs above the server's name
        rather than below the list of its pages.
        """
        sidebar = self._sidebar(self._page(config, store))
        assert sidebar.index("All servers") < sidebar.index("side-guild")
        assert sidebar.index("side-guild") < sidebar.index("side-nav")

    def test_the_way_out_is_not_dressed_as_a_section(self, config, store):
        """It can never be the current page, so it must never wear the class
        that means "this is where you are"."""
        sidebar = self._sidebar(self._page(config, store))
        up = sidebar[sidebar.index("side-up") : sidebar.index("side-guild")]
        assert "side-link" not in up
        assert "aria-current" not in up

    def test_the_server_name_is_not_marked_up_as_a_heading(self, config, store):
        """It is styled as one and is deliberately not one: the sidebar comes
        before the page's <h1> in reading order, and a heading here would
        announce the server as the document's first section."""
        sidebar = self._sidebar(self._page(config, store))
        for level in ("h1", "h2", "h3"):
            assert f"<{level}" not in sidebar

    def test_the_server_icon_outsizes_the_section_icons(self, config, store):
        """The block is a header for the list, not the first row in it. Sized
        to match, the server becomes another nav item."""
        page = self._page(config, store)
        sidebar = self._sidebar(page)
        guild_icon = re.search(r'<img[^>]*width="(\d+)"', sidebar)
        assert guild_icon and int(guild_icon.group(1)) > 16

    def test_no_sidebar_icon_paints_itself_a_surface_colour(self, config, store):
        """An icon cannot know what is behind it.

        The settings glyph filled its slider knobs with `--panel` to punch a
        hole in the track. The sidebar is `--chrome`, a hovered row is
        `--hover` and the current row is `--selected`, so that disc matched
        nothing it was ever drawn on. It hid while these icons only appeared on
        the collapsed rail; phase 2 shows them always.

        The general rule, which is what this pins: a glyph may use
        `currentColor` and nothing else. Anything that needs a hole in it
        should be drawn with a gap.
        """
        sidebar = self._sidebar(self._page(config, store))
        for surface in ("--panel", "--chrome", "--bg", "--inset", "--hover"):
            assert f"var({surface})" not in sidebar

    def test_every_section_still_carries_its_accessible_name(self, config, store):
        """The icons are aria-hidden, so the label is the whole accessible
        name. If a future change ever hides the label from assistive
        technology too, these links become three blank targets."""
        sidebar = self._sidebar(self._page(config, store, collapsed=True))
        for label in ("Overview", "Settings", "Subscriptions"):
            assert f'>{label}</span>' in sidebar
        assert "offscreen" not in sidebar


class TestTheSidebarPreference:
    """`/prefs/nav`. A cookie, and nothing else."""

    def test_collapsing_sets_the_cookie_and_returns_to_the_page(
        self, config, store
    ):
        test_client, _api = settings_client(config, store)
        session = store.load(
            test_client.get_cookie(SESSION_COOKIE).value
        )
        response = test_client.post(
            "/prefs/nav",
            data={
                "csrf_token": session.csrf_token,
                "collapsed": "1",
                "return_to": "guild_settings",
                "guild_id": GUILD_IN,
            },
        )
        assert response.status_code == 302
        assert response.headers["Location"].endswith(f"/guild/{GUILD_IN}/settings")
        assert test_client.get_cookie("vrcverify_nav").value == "1"

    def test_the_collapsed_state_survives_the_next_page(self, config, store):
        test_client, _api = settings_client(config, store)

        expanded = test_client.get(f"/guild/{GUILD_IN}/settings").data.decode()
        assert 'class="layout collapsed"' not in expanded

        test_client.set_cookie("vrcverify_nav", "1", domain="localhost")
        collapsed = test_client.get(f"/guild/{GUILD_IN}/settings").data.decode()
        assert 'class="layout collapsed"' in collapsed

    def test_a_collapsed_sidebar_keeps_every_link(self, config, store):
        """A rail, not a removal.

        Hiding the labels is the whole effect. If the links themselves went,
        the sidebar would be unusable by keyboard and there would be no way
        back to the server list.
        """
        test_client, _api = settings_client(config, store)
        test_client.set_cookie("vrcverify_nav", "1", domain="localhost")
        page = test_client.get(f"/guild/{GUILD_IN}/settings").data.decode()

        assert f'href="/guild/{GUILD_IN}"' in page
        assert f'href="/guild/{GUILD_IN}/subscription"' in page
        assert "All servers" in page

    def test_expanding_clears_the_cookie(self, config, store):
        test_client, _api = settings_client(config, store)
        test_client.set_cookie("vrcverify_nav", "1", domain="localhost")
        session = store.load(test_client.get_cookie(SESSION_COOKIE).value)

        test_client.post(
            "/prefs/nav",
            data={
                "csrf_token": session.csrf_token,
                "return_to": "guild_settings",
                "guild_id": GUILD_IN,
            },
        )
        # Expanded is the absence of the cookie rather than a second value.
        remaining = test_client.get_cookie("vrcverify_nav")
        assert remaining is None or remaining.value == ""

    def test_it_needs_a_csrf_token(self, config, store):
        test_client, _api = settings_client(config, store)
        response = test_client.post("/prefs/nav", data={"collapsed": "1"})
        assert response.status_code == 400
        assert test_client.get_cookie("vrcverify_nav") is None

    def test_it_needs_a_session(self, client):
        response = client.post("/prefs/nav", data={"collapsed": "1"})
        assert response.status_code == 302
        assert client.get_cookie("vrcverify_nav") is None

    def test_the_nav_preference_never_reaches_the_bot(self, config, store):
        """A UI toggle must not be a way to spend the bot's rate limit."""
        test_client, api = settings_client(config, store)
        session = store.load(test_client.get_cookie(SESSION_COOKIE).value)
        test_client.post(
            "/prefs/nav",
            data={
                "csrf_token": session.csrf_token,
                "collapsed": "1",
                "return_to": "guild_settings",
                "guild_id": GUILD_IN,
            },
        )
        assert api.reads == []
        assert api.calls == []
        assert api.saves == []

    @pytest.mark.parametrize(
        "return_to",
        [
            "https://evil.example/steal",
            "//evil.example",
            "/guild/1/settings",
            "static",
            "login",
        ],
    )
    def test_it_only_returns_to_endpoints_we_named(self, config, store, return_to):
        """The form carries an endpoint name, never a path.

        A hidden field holding a URL is how a preference toggle becomes an open
        redirect. Anything unrecognised lands on the picker.
        """
        test_client, _api = settings_client(config, store)
        session = store.load(test_client.get_cookie(SESSION_COOKIE).value)
        response = test_client.post(
            "/prefs/nav",
            data={
                "csrf_token": session.csrf_token,
                "collapsed": "1",
                "return_to": return_to,
                "guild_id": GUILD_IN,
            },
        )
        assert response.headers["Location"].endswith("/")

    def test_a_non_numeric_guild_id_falls_back_to_the_picker(self, config, store):
        test_client, _api = settings_client(config, store)
        session = store.load(test_client.get_cookie(SESSION_COOKIE).value)
        response = test_client.post(
            "/prefs/nav",
            data={
                "csrf_token": session.csrf_token,
                "return_to": "guild_settings",
                "guild_id": "../../etc/passwd",
            },
        )
        assert response.headers["Location"].endswith("/")


class TestTheThemeAttribute:
    """What `<html>` carries, and why "system" carries nothing (issue #123).

    The stylesheet resolves three states from two selectors and an absence:
    `[data-theme="dark"]`, `[data-theme="light"]`, and
    `:root:not([data-theme])` inside the dark media query. So "follow the OS"
    is the *absence* of the attribute, and a test that only checked for the
    right string would pass on a page that pinned every reader to light.
    """

    @staticmethod
    def _html_tag(page: str) -> str:
        start = page.index("<html")
        return page[start : page.index(">", start) + 1]

    def test_no_cookie_means_dark(self, client):
        """The product decision from #123, and the reason this phase exists."""
        assert 'data-theme="dark"' in self._html_tag(client.get("/").data.decode())

    @pytest.mark.parametrize("chosen", ["dark", "light"])
    def test_an_explicit_choice_is_rendered(self, client, chosen):
        client.set_cookie("vrcverify_theme", chosen)
        tag = self._html_tag(client.get("/").data.decode())
        assert f'data-theme="{chosen}"' in tag

    def test_system_renders_no_attribute_at_all(self, client):
        """Not `data-theme="system"`.

        That value matches none of the stylesheet's three blocks, so it would
        silently pin the reader to the light floor -- the opposite of what
        choosing "System" asks for, and invisible to anyone whose OS is light.
        """
        client.set_cookie("vrcverify_theme", "system")
        assert "data-theme" not in self._html_tag(client.get("/").data.decode())

    @pytest.mark.parametrize(
        "value",
        ["", "System", "DARK", "purple", "dark light", '"><script>', "../../etc"],
    )
    def test_anything_unrecognised_falls_back_to_dark(self, client, value):
        """The cookie is attacker-controlled; the attribute is not.

        `_theme()` reduces it to one of three known words before it is
        interpolated, so nothing from a header can reach the markup. Note the
        casing cases: matching is exact, so `DARK` is not a third spelling of
        dark, it is simply unrecognised.
        """
        client.set_cookie("vrcverify_theme", value)
        tag = self._html_tag(client.get("/").data.decode())
        assert 'data-theme="dark"' in tag
        assert "<script>" not in tag

    def test_the_signed_out_page_is_themed_too(self, client):
        """The sign-in page renders with no session and no arguments at all.

        `render_template("login.html")` passes nothing, which is exactly why
        the theme is a template global rather than a context variable. If it
        were threaded through render calls, this page is the one that would
        have been missed.
        """
        page = client.get("/").data.decode()
        assert b"Sign in with Discord" in page.encode()
        assert 'data-theme="dark"' in self._html_tag(page)

    def test_every_page_is_themed_not_just_the_first(self, config, store):
        """A signed-in page inherits it from the same base template."""
        test_client, _api = settings_client(config, store)
        for path in ("/", f"/guild/{GUILD_IN}/settings"):
            tag = self._html_tag(test_client.get(path).data.decode())
            assert 'data-theme="dark"' in tag, path

    def test_the_theme_costs_no_extra_bot_calls(self, config, store):
        """A display preference must not be a way to spend the bot's calls.

        Compared against the same page rendered with no cookie rather than
        against zero: the picker legitimately asks the bot which guilds it is
        in, so the question is whether choosing a theme adds anything, not
        whether the page talks to the bot at all.
        """
        plain_client, plain_api = settings_client(config, store)
        plain_client.get("/")
        baseline = (
            len(plain_api.reads), len(plain_api.calls), len(plain_api.saves)
        )

        themed_client, themed_api = settings_client(config, store)
        themed_client.set_cookie("vrcverify_theme", "light")
        themed_client.get("/")

        assert (
            len(themed_api.reads), len(themed_api.calls), len(themed_api.saves)
        ) == baseline


class TestTheThemePicker:
    """`/prefs/theme`, and the control that posts to it (issue #123 phase 3).

    The route is the one POST in this app that requires neither a session nor
    a CSRF token. That is deliberate -- the sign-in page carries this control
    and has neither -- so the tests below pin both halves: that it works
    without them, and that dropping them bought nothing an attacker wants.
    """

    @staticmethod
    def _html_tag(page: str) -> str:
        start = page.index("<html")
        return page[start : page.index(">", start) + 1]

    @staticmethod
    def _current_option(page: str):
        """Which option the page marks as in force, by aria-current."""
        match = re.search(r'<button[^>]*aria-current="true"[^>]*>', page)
        return re.search(r'value="([a-z]+)"', match.group(0)).group(1) if match else None

    # --- the control itself ---

    def test_the_picker_offers_all_three_and_needs_no_session(self, client):
        page = client.get("/").data.decode()
        for value in ("dark", "light", "system"):
            assert f'name="theme" value="{value}"' in page

    def test_the_picker_is_on_the_signed_out_page(self, client):
        """The page with no session and no CSRF token is the whole reason the
        route requires neither."""
        page = client.get("/").data.decode()
        assert b"Sign in with Discord" in page.encode()
        assert 'action="/prefs/theme"' in page

    @pytest.mark.parametrize("chosen", ["dark", "light", "system"])
    def test_the_option_in_force_is_marked(self, client, chosen):
        client.set_cookie("vrcverify_theme", chosen)
        assert self._current_option(client.get("/").data.decode()) == chosen

    def test_it_needs_no_javascript(self, client):
        """`<details>` opens it, a submit button applies it. If either became
        script-driven this page would stop working with JS off, which is the
        promise the whole app is built on."""
        page = client.get("/").data.decode()
        assert "<details" in page
        assert "onclick" not in page and "javascript:" not in page

    # --- the route ---

    @pytest.mark.parametrize("chosen", ["light", "system"])
    def test_choosing_a_non_default_stores_it(self, client, chosen):
        response = client.post("/prefs/theme", data={"theme": chosen})
        assert response.status_code == 302
        assert client.get_cookie("vrcverify_theme").value == chosen

    def test_choosing_dark_clears_the_cookie_rather_than_storing_it(self, client):
        """Dark is what no cookie already means.

        Storing "dark" would be a second way of saying the same thing, and two
        representations of one state is a thing to keep in agreement forever.
        Same shape as the sidebar's "expanded".
        """
        client.set_cookie("vrcverify_theme", "light")
        client.post("/prefs/theme", data={"theme": "dark"})
        remaining = client.get_cookie("vrcverify_theme")
        assert remaining is None or remaining.value == ""
        # And the page still renders dark, which is the point.
        assert 'data-theme="dark"' in self._html_tag(client.get("/").data.decode())

    def test_it_works_with_no_session_at_all(self, client):
        """No login, no CSRF token, and it still applies."""
        response = client.post("/prefs/theme", data={"theme": "light"})
        assert response.status_code == 302
        assert client.get_cookie("vrcverify_theme").value == "light"

    def test_the_cookie_is_readable_by_script(self, client):
        """Phase 4 has the button write this directly -- there is no
        `connect-src` in the CSP, so a script cannot ask the server instead.
        httponly here would make that impossible."""
        client.post("/prefs/theme", data={"theme": "light"})
        header = "\n".join(
            v for k, v in client.post(
                "/prefs/theme", data={"theme": "light"}
            ).headers if k.lower() == "set-cookie"
        )
        assert "vrcverify_theme=light" in header
        assert "httponly" not in header.lower()
        assert "Secure" in header and "SameSite=Lax" in header

    @pytest.mark.parametrize(
        "value", ["", "purple", "DARK", "dark light", '"><script>', "../../etc"]
    )
    def test_an_unrecognised_choice_changes_nothing(self, client, value):
        client.set_cookie("vrcverify_theme", "light")
        response = client.post("/prefs/theme", data={"theme": value})
        assert response.status_code == 302
        assert client.get_cookie("vrcverify_theme").value == "light"

    def test_a_missing_choice_changes_nothing(self, client):
        client.set_cookie("vrcverify_theme", "light")
        client.post("/prefs/theme", data={})
        assert client.get_cookie("vrcverify_theme").value == "light"

    # --- the redirect, which is the part that could actually hurt ---

    @pytest.mark.parametrize(
        "return_to",
        ["https://evil.example", "//evil.example", "/guild/1/settings", "nope", ""],
    )
    def test_the_redirect_cannot_be_steered(self, client, return_to):
        """An endpoint name from a fixed table, never a path from the form.

        Dropping CSRF from this route makes it trivially callable by anyone,
        so the redirect target is the one thing here worth attacking.
        """
        response = client.post(
            "/prefs/theme",
            data={"theme": "light", "return_to": return_to, "guild_id": "123"},
        )
        assert response.headers["Location"] == "/"

    def test_a_known_endpoint_returns_you_to_the_page_you_were_on(self, client):
        response = client.post(
            "/prefs/theme",
            data={"theme": "light", "return_to": "guild_settings", "guild_id": "123"},
        )
        assert response.headers["Location"] == "/guild/123/settings"

    def test_a_non_numeric_guild_is_dropped(self, client):
        response = client.post(
            "/prefs/theme",
            data={
                "theme": "light",
                "return_to": "guild_settings",
                "guild_id": "../../etc/passwd",
            },
        )
        assert response.headers["Location"] == "/"

    def test_the_theme_never_reaches_the_bot(self, config, store):
        """A display preference must not be a way to spend the bot's calls."""
        test_client, api = settings_client(config, store)
        api.reads.clear(); api.calls.clear(); api.saves.clear()
        test_client.post("/prefs/theme", data={"theme": "light"})
        assert api.reads == [] and api.calls == [] and api.saves == []


class TestTheHeaderBar:
    """The chrome every page sits inside (issue #133 phase 1).

    Two things are pinned here beyond appearance. The account menu must keep
    working with no JavaScript, like everything else in this app. And the slot
    #136's notification bell drops into must stay where this phase put it --
    reserving it is most of why this phase went first, since #123, #136 and the
    rest of #133 all edit base.html.
    """

    def test_the_account_menu_opens_without_a_script(self, client, store):
        """A <details> disclosure, not a script. If this ever became
        script-driven, signing out would stop working with JS off -- and
        signing out is the one control nobody can be asked to do without."""
        login_as(client, store)
        page = client.get("/").data.decode()
        assert '<details class="account bar-menu">' in page
        assert "onclick" not in page and "javascript:" not in page

    def test_both_sign_out_routes_are_inside_that_menu(self, client, store):
        """The whole menu, from <details> to </details>, has to contain both --
        otherwise one of them is still loose in the bar."""
        login_as(client, store)
        page = client.get("/").data.decode()
        start = page.index('<details class="account')
        menu = page[start : page.index("</details>", start)]
        assert 'action="/logout"' in menu
        assert 'action="/logout/everywhere"' in menu

    def test_each_sign_out_form_carries_its_own_token(self, client, store):
        """Two forms, two routes, two tokens. The theme picker next to them is
        the one form in this app allowed to go without."""
        session = login_as(client, store)
        page = client.get("/").data.decode()
        start = page.index('<details class="account')
        menu = page[start : page.index("</details>", start)]
        assert menu.count(f'value="{session.csrf_token}"') == 2

    def test_there_is_no_account_menu_when_nobody_is_signed_in(self, client):
        """The sign-in page has no session to sign out of."""
        page = client.get("/").data.decode()
        assert '<details class="account' not in page
        assert 'action="/logout"' not in page

    def test_what_everywhere_means_is_text_rather_than_a_tooltip(
        self, client, store
    ):
        """It used to live only in a `title` attribute, which is to say only
        for somebody using a mouse on a desktop. The distinction between the
        two controls is the entire reason there are two."""
        login_as(client, store)
        page = client.get("/").data.decode()
        explanation = "on every device"
        assert f'title="{explanation}' not in page
        assert explanation in page
        # And in the menu, not somewhere else on the page.
        start = page.index('<details class="account')
        assert explanation in page[start : page.index("</details>", start)]

    def test_neither_control_is_styled_as_destructive(self, client, store):
        """Red would be the obvious choice for "sign out everywhere" and the
        wrong one: it destroys no data, and it is exactly what you want
        somebody to do without hesitating when they think they have been
        compromised. A hazard colour would discourage it."""
        login_as(client, store)
        page = client.get("/").data.decode()
        start = page.index('<details class="account')
        menu = page[start : page.index("</details>", start)]
        assert "danger" not in menu

    # --- the reserved slot ---

    def test_the_bell_slot_is_reserved_before_the_theme_picker(self, client):
        """#136 adds one element inside .bar-actions. This pins that the
        container exists and that the theme picker is inside it, which is what
        stops the two issues fighting over this file."""
        page = client.get("/").data.decode()
        actions = page.index('class="bar-actions"')
        assert actions < page.index('<details class="theme bar-menu">')

    def test_the_menus_share_one_pattern(self, client, store):
        """Both wear .bar-menu, which is what prefs.js dismisses and what #136
        will wear too. Two menus styled two ways is how a bar ends up with
        three popovers that each close differently."""
        login_as(client, store)
        page = client.get("/").data.decode()
        assert page.count("bar-menu") == 2
        assert page.count("bar-panel") == 2

    # --- the logo ---

    def test_the_logo_is_served_from_our_own_static_files(self, client):
        """`img-src` is 'self' plus Discord's CDN. A logo from anywhere else
        would need the CSP widened, which is not a trade worth making for a
        picture."""
        page = client.get("/").data.decode()
        mark = re.search(r"<img[^>]*brand-mark[^>]*>", page).group(0)
        assert 'src="/static/logo.png?v=' in mark

    def test_the_logo_is_marked_decorative(self, client):
        """The word "VRCVerify" sits beside it saying the same thing. A screen
        reader announcing "VRCVerify logo, VRCVerify" is a worse link than one
        that just says where it goes."""
        page = client.get("/").data.decode()
        mark = re.search(r"<img[^>]*brand-mark[^>]*>", page).group(0)
        assert 'alt=""' in mark
        assert "VRCVerify" in page[page.index(mark) : page.index(mark) + 400]

    def test_the_logo_declares_its_intrinsic_size(self, client):
        """Without these the wordmark jumps sideways when the image lands.
        They are the file's real dimensions, not its rendered ones -- the
        browser wants the ratio, the stylesheet sets the height."""
        import dashboard

        page = client.get("/").data.decode()
        mark = re.search(r"<img[^>]*brand-mark[^>]*>", page).group(0)
        declared = (
            int(re.search(r'width="(\d+)"', mark).group(1)),
            int(re.search(r'height="(\d+)"', mark).group(1)),
        )
        path = os.path.join(
            os.path.dirname(dashboard.__file__), "static", "logo.png"
        )
        with open(path, "rb") as handle:
            header = handle.read(24)
        assert struct.unpack(">II", header[16:24]) == declared

    def test_the_dark_theme_recolours_the_logo_rather_than_swapping_it(self):
        """One file, inverted. The alternative is a second PNG and a standing
        obligation to keep two images in step forever."""
        import dashboard

        with open(
            os.path.join(os.path.dirname(dashboard.__file__), "static", "style.css"),
            encoding="utf-8",
        ) as handle:
            css = handle.read()
        assert "--logo-filter: none;" in css
        assert "--dark-logo-filter: invert(1);" in css
        # Once per dark selector: the explicit one and the OS one.
        assert css.count("--logo-filter: var(--dark-logo-filter);") == 2


class TestOverviewViewModel:
    """The three tile states, tested without a request."""

    def test_zero_prints_as_zero(self):
        """`value or "-"` is the bug this exists to prevent."""
        tile = overview_view.Tile("Today (UTC)", 0)
        assert tile.display == "0"

    def test_a_count_gets_a_thousands_separator(self):
        assert overview_view.Tile("Members", 1284).display == "1,284"

    def test_a_blank_window_prints_a_dash(self):
        tile = overview_view.Tile("Last 30 days", state="blank")
        assert tile.display == "—"

    def test_an_unknown_tile_says_so(self):
        tile = overview_view.Tile("Members", state="unknown")
        assert tile.display == "Couldn't check"

    def test_a_blank_window_names_the_collection_start_when_known(self):
        tiles = overview_view.build_tiles(
            {
                "member_count": 5,
                "verifications": {
                    "total": None,
                    "today": 1,
                    "last_7_days": None,
                    "last_30_days": None,
                    "collecting_since": "2026-08-14",
                    "known": True,
                },
            }
        )
        blanks = [tile for tile in tiles if tile.state == "blank"]
        assert len(blanks) == 2
        assert all("2026-08-14" in tile.note for tile in blanks)

    def test_an_unknown_rollup_marks_every_window_unknown_not_blank(self):
        tiles = overview_view.build_tiles(
            {
                "member_count": 5,
                "verifications": {
                    "total": None,
                    "today": None,
                    "last_7_days": None,
                    "last_30_days": None,
                    "collecting_since": None,
                    "known": False,
                },
            }
        )
        windows = [tile for tile in tiles if tile.label != "Members"]
        assert {tile.state for tile in windows} == {"unknown"}

    def test_no_payload_means_no_tiles(self):
        """The page shows an apology rather than a row of dashes."""
        assert overview_view.build_tiles(None) == []

    def test_the_next_step_is_at_most_one_thing(self):
        step = overview_view.build_next_step(
            {
                "configured": {"verified_role": False},
                "panel": {"posted": False},
            }
        )
        assert "verified role" in step["title"]

    def test_setup_reports_each_piece_as_a_yes_or_no(self):
        rows = overview_view.build_setup(
            {
                "configured": {
                    "verified_role": True,
                    "unverified_role": False,
                    "log_channel": False,
                    "auto_verify": True,
                }
            }
        )
        assert {row["label"]: row["on"] for row in rows} == {
            "Verified role": True,
            "Auto-verify on join": True,
            "Unverified role": False,
            "Verification log": False,
        }

    def test_only_the_verified_role_counts_as_missing(self):
        """The other three are choices, not faults.

        Marking a deliberately-unset optional feature as a problem would report
        a working server as broken.
        """
        rows = overview_view.build_setup(
            {"configured": {"verified_role": False, "log_channel": False}}
        )
        flagged = [row["label"] for row in rows if row["required"] and not row["on"]]
        assert flagged == ["Verified role"]

    def test_a_failed_settings_read_shows_no_setup_rows(self):
        """Better silent than reporting four features as switched off."""
        assert overview_view.build_setup({"configured": None}) == []

    def test_no_next_step_for_a_configured_server(self):
        assert (
            overview_view.build_next_step(
                {
                    "configured": {"verified_role": True},
                    "panel": {"posted": True},
                }
            )
            is None
        )


class TestWriteSurface:
    """One save path per group and nothing else. Widening it means editing this."""

    def test_the_post_routes_are_logout_and_one_save_per_group(self, app):
        posts = {
            rule.rule
            for rule in app.url_map.iter_rules()
            if "POST" in (rule.methods or set())
        }
        assert posts == {
            "/logout",
            "/logout/everywhere",
            "/guild/<int:guild_id>/verification",
            "/guild/<int:guild_id>/member",
            "/guild/<int:guild_id>/panel",
            "/guild/<int:guild_id>/logging",
            "/guild/<int:guild_id>/group",
            # The two that make the bot act rather than store.
            "/guild/<int:guild_id>/panel/post",
            # Sends no body at all: the group it checks comes from the guild's
            # stored settings on the bot's side, never from this form.
            "/guild/<int:guild_id>/group/verify",
            # Writes a cookie and nothing else. It is in this list because the
            # list is meant to be complete, not because it reaches the bot --
            # test_the_nav_preference_never_reaches_the_bot pins that it does
            # not.
            "/prefs/nav",
            # The same, and the only route here that requires neither a session
            # nor a CSRF token -- the sign-in page carries the theme control
            # and has neither to offer. It reads nothing, stores nothing
            # server-side and never reaches the bot; the whole effect of a
            # forged call is that the caller's own next page is a different
            # colour. See set_theme_preference, and TestTheThemePicker for the
            # redirect, which is the only part of it worth attacking.
            "/prefs/theme",
            # The two that spend money, or end a subscription. Neither writes
            # anything here: each creates a session on Stripe and hands the
            # browser over, so the whole of what they can do is bounded by what
            # Stripe's hosted pages allow.
            "/guild/<int:guild_id>/subscription/checkout",
            "/guild/<int:guild_id>/subscription/portal",
        }, f"an unexpected write route appeared: {posts}"

    def test_the_stripe_webhook_is_not_in_that_list_unless_switched_on(self, app):
        """The public route is registered by config, so the pin above only
        describes a dashboard with Stripe off -- which is what `app` is.

        Worth its own assertion: if the webhook ever appeared in a default app,
        the kill switch would have stopped meaning anything.
        """
        posts = {
            rule.rule
            for rule in app.url_map.iter_rules()
            if "POST" in (rule.methods or set())
        }
        assert "/stripe/webhook" not in posts

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


# -------------------------------------------------------------------
# The VRChat group section (issue #49, phase 4)
# -------------------------------------------------------------------
class TestGroupSetupSummary:
    """The status beside the two settings. Nothing here is ever saved.

    Its job is to turn one of a dozen state codes into a sentence naming the
    next thing the admin can do. That is the whole reason the worker reports
    nine distinct verdicts rather than "setup failed": an admin told only that
    it failed opens a support ticket, and an admin told the bot is in the group
    but lacks Manage Group Invites goes and ticks the box.
    """

    def summary(self, group_id=GROUP_ID, premium=True, **block):
        settings = make_settings(
            premium=premium,
            values={"vrchat_group_id": group_id},
            group_invite=group_invite_block(**block),
        )
        return settings_view.group_setup_summary(settings)

    def test_an_unconfigured_guild_has_nothing_to_show(self):
        summary = self.summary(group_id=None)
        assert summary["configured"] is False
        assert summary["group_url"] is None

    def test_every_state_the_bot_can_send_has_copy(self):
        """A raw state code reaching the page would be gibberish to an admin.

        Derived from the bot's own vocabulary, so a state added there fails
        here until somebody writes the sentence for it.
        """
        import bot as bot_module

        for state in bot_module.GROUP_SETUP_STATES:
            assert state in settings_view.GROUP_SETUP_COPY, state

    def test_an_unknown_state_falls_back_rather_than_leaking(self):
        summary = self.summary(state="something_new")
        assert summary["headline"] == settings_view.GROUP_SETUP_FALLBACK[1]
        assert "something_new" not in summary["headline"]

    def test_a_ready_group_reads_as_ready(self):
        summary = self.summary(
            state="ready", can_invite=True, can_see_members=True, group_name="Club LA"
        )
        assert summary["tone"] == "ok"
        assert summary["group_name"] == "Club LA"
        assert summary["warnings"] == []

    def test_ready_without_member_visibility_suggests_the_optional_permission(self):
        """Not a failure -- invites work without it. It only decides whether a
        member already in the group can be told so."""
        summary = self.summary(state="ready", can_invite=True, can_see_members=False)
        assert summary["tone"] == "ok"
        assert any("View All Members" in w for w in summary["warnings"])

    def test_the_permission_failure_says_admin_is_not_enough(self):
        """Confirmed against a live group: group-invites-manage is its own tick
        box that a 24-permission admin role can lack. "Make it an admin" is
        advice that produces this exact state."""
        summary = self.summary(state="no_invite_permission")
        assert summary["tone"] == "warn"
        assert "admin" in summary["detail"].lower()

    def test_the_claim_code_is_shown_until_the_check_passes(self):
        pending = self.summary(state="code_missing", claim_code="VRCG-7K2M4P")
        assert pending["show_claim_code"] is True
        ready = self.summary(state="ready", claim_code="VRCG-7K2M4P")
        # It has done its job, and leaving it up invites someone to leave it in
        # their group description for ever.
        assert ready["show_claim_code"] is False

    def test_the_account_to_invite_is_linked_by_id(self):
        """Display names are not unique, so the usr_ id is the part that
        matters -- an admin who invites a lookalike gets "not invited" with
        nothing explaining why."""
        summary = self.summary()
        assert summary["account_id"] == INVITE_ACCOUNT
        assert summary["account_url"].endswith(INVITE_ACCOUNT)

    def test_the_group_is_linked_too(self):
        assert self.summary()["group_url"].endswith(GROUP_ID)

    def test_a_deployment_with_no_invite_account_says_so(self):
        summary = self.summary(account_to_invite=None)
        assert summary["account_url"] is None
        assert any("operator" in w for w in summary["warnings"])

    def test_a_very_long_error_is_clipped(self):
        """Not about injection -- Jinja escapes it. About a page that stays
        readable when an upstream error turns out to be a wall of JSON."""
        summary = self.summary(state="vrchat_unavailable", error="x" * 5000)
        assert len(summary["error"]) <= settings_view.GROUP_ERROR_MAX_LEN

    def test_the_bots_own_error_travels_with_the_advice(self):
        """One says what to do, the other says what VRChat actually replied."""
        summary = self.summary(state="banned", error="The bot is banned")
        assert summary["error"] == "The bot is banned"
        assert summary["detail"] != summary["error"]


class TestTheGroupFields:
    def test_the_group_id_is_a_single_line_input(self):
        settings = make_settings(premium=True, values={"vrchat_group_id": GROUP_ID})
        field = next(
            f
            for group in settings_view.build_groups(
                settings, DEFAULT_ROLES, DEFAULT_CHANNELS
            )
            for f in group["fields"]
            if f.name == "vrchat_group_id"
        )
        assert field.kind == "line"
        assert field.editable is True
        assert field.value == GROUP_ID

    def test_a_free_server_sees_it_locked(self):
        settings = make_settings(values={"vrchat_group_id": GROUP_ID})
        field = next(
            f
            for group in settings_view.build_groups(
                settings, DEFAULT_ROLES, DEFAULT_CHANNELS
            )
            for f in group["fields"]
            if f.name == "vrchat_group_id"
        )
        assert field.badge == "premium"
        assert field.editable is False


class TestSavingTheGroup:
    def logged_in(self, config, store, **kwargs):
        api = FakeBotAPI(**kwargs)
        app = create_app(config, store=store, client=api)
        app.config.update(TESTING=True)
        test_client = app.test_client()
        session = login_as(test_client, store)
        return test_client, api, session

    def post(self, test_client, session, **form):
        form.setdefault("csrf_token", session.csrf_token)
        return test_client.post(f"/guild/{GUILD_IN}/group", data=form)

    def test_the_group_is_sent_exactly_as_typed(self, config, store):
        """Parsing is the bot's -- bare id or URL, case folding, refusing
        vrc.group links. Doing any of it here would be a second opinion about
        what a valid group is, on the side that enforces nothing."""
        test_client, api, session = self.logged_in(config, store)
        self.post(
            test_client,
            session,
            vrchat_group_id=f"  HTTPS://VRChat.com/home/group/{GROUP_ID}  ",
        )
        assert api.saves[-1][2]["vrchat_group_id"] == (
            f"  HTTPS://VRChat.com/home/group/{GROUP_ID}  "
        )

    def test_an_empty_field_disconnects_the_group(self, config, store):
        test_client, api, session = self.logged_in(config, store)
        self.post(test_client, session, vrchat_group_id="")
        assert api.saves[-1][2] == {"vrchat_group_id": None}

    def test_the_toggle_travels_as_a_bool(self, config, store):
        test_client, api, session = self.logged_in(config, store)
        self.post(
            test_client,
            session,
            present_vrchat_group_invite_enabled="1",
            vrchat_group_invite_enabled="on",
        )
        assert api.saves[-1][2] == {"vrchat_group_invite_enabled": True}

    def test_a_missing_csrf_token_is_refused(self, config, store):
        test_client, api, session = self.logged_in(config, store)
        response = test_client.post(
            f"/guild/{GUILD_IN}/group", data={"vrchat_group_id": GROUP_ID}
        )
        assert response.status_code == 400
        assert api.saves == []

    def test_a_refusal_becomes_copy_not_the_bots_text(self, config, store):
        test_client, _api, session = self.logged_in(
            config,
            store,
            errors={"update_settings": BotAPIError("group_claimed_elsewhere", 400)},
        )
        response = self.post(test_client, session, vrchat_group_id=GROUP_ID)
        page = test_client.get(response.headers["Location"]).data.decode()
        assert "already linked that VRChat group" in page


class TestTheGroupCheckButton:
    def logged_in(self, config, store, **kwargs):
        api = FakeBotAPI(**kwargs)
        app = create_app(config, store=store, client=api)
        app.config.update(TESTING=True)
        test_client = app.test_client()
        session = login_as(test_client, store)
        return test_client, api, session

    def post(self, test_client, session, **form):
        form.setdefault("csrf_token", session.csrf_token)
        return test_client.post(f"/guild/{GUILD_IN}/group/verify", data=form)

    def test_the_button_asks_the_bot(self, config, store):
        test_client, api, session = self.logged_in(config, store)
        response = self.post(test_client, session)
        assert response.status_code == 302
        assert api.group_checks == [(ACTOR, GUILD_IN)]

    def test_it_carries_no_group_id_however_hard_you_try(self, config, store):
        """The security property, from this end.

        The client method takes an actor and a guild and nothing else, so a
        group id in the form has nowhere to go. If that ever stops being true,
        this endpoint becomes a way to make a VRChat account join whatever is
        posted to it.
        """
        test_client, api, session = self.logged_in(config, store)
        self.post(test_client, session, vrchat_group_id="grp_attacker")
        assert api.group_checks == [(ACTOR, GUILD_IN)]

    def test_the_page_says_checking_not_checked(self, config, store):
        """The answer comes back over a queue, so it is not in this response.
        Claiming success would be a claim this page cannot make."""
        test_client, _api, session = self.logged_in(config, store)
        response = self.post(test_client, session)
        page = test_client.get(response.headers["Location"]).data.decode()
        assert "Checking your VRChat group" in page

    def test_a_missing_csrf_token_is_refused(self, config, store):
        test_client, api, session = self.logged_in(config, store)
        response = test_client.post(f"/guild/{GUILD_IN}/group/verify", data={})
        assert response.status_code == 400
        assert api.group_checks == []

    def test_a_refusal_becomes_copy(self, config, store):
        test_client, _api, session = self.logged_in(
            config, store, errors={"verify_group": BotAPIError("no_group_configured", 400)}
        )
        response = self.post(test_client, session)
        page = test_client.get(response.headers["Location"]).data.decode()
        assert "Add your VRChat group first" in page

    def test_an_anonymous_visitor_gets_nowhere(self, config, store):
        api = FakeBotAPI()
        app = create_app(config, store=store, client=api)
        app.config.update(TESTING=True)
        response = app.test_client().post(f"/guild/{GUILD_IN}/group/verify", data={})
        assert response.status_code in (302, 400)
        assert api.group_checks == []


class TestALockedSectionStopsGivingInstructions:
    """A lapsed server keeps its group -- the field is write_locked, so the bot
    refuses the save rather than clearing it -- and therefore keeps this status.

    What it must not keep is the list of things to go and do. There is no check
    button on a locked section, so "paste this code into your group description
    and invite this account" would be instructions for a task the page gives
    them no way to finish.
    """

    def summary(self, **block):
        settings = make_settings(
            premium=False,
            values={"vrchat_group_id": GROUP_ID},
            group_invite=group_invite_block(**block),
        )
        return settings_view.group_setup_summary(settings)

    def test_the_setup_code_is_not_shown(self):
        assert self.summary(claim_code="VRCG-7K2M4P")["show_claim_code"] is False

    def test_the_account_to_invite_is_not_named(self):
        summary = self.summary()
        assert summary["account_id"] is None
        assert summary["account_url"] is None

    def test_no_warning_asks_for_an_action(self):
        assert self.summary(state="ready", can_see_members=False)["warnings"] == []

    def test_the_next_step_is_the_same_promise_the_locked_fields_make(self):
        summary = self.summary(state="not_invited")
        assert "Premium" in summary["detail"]
        assert "kept" in summary["detail"]

    def test_where_they_got_to_is_still_shown(self):
        """True, and worth seeing. Only the next step changes."""
        assert self.summary(state="ready", group_name="Club LA")["headline"] == "Ready"
        assert self.summary(state="ready", group_name="Club LA")["group_name"] == "Club LA"

    def test_their_own_group_is_still_linked(self):
        """A link to a group they own is not an instruction."""
        assert self.summary()["group_url"].endswith(GROUP_ID)

    def test_a_premium_server_still_gets_the_instructions(self):
        """The other half of the pair, so this cannot pass by suppressing
        everything for everybody."""
        settings = make_settings(
            premium=True,
            values={"vrchat_group_id": GROUP_ID},
            group_invite=group_invite_block(claim_code="VRCG-7K2M4P"),
        )
        summary = settings_view.group_setup_summary(settings)
        assert summary["show_claim_code"] is True
        assert summary["account_id"] == INVITE_ACCOUNT
        assert summary["locked"] is False


class TestValuesCarryNoTemplateWhitespace:
    """`.value` is `white-space: pre-wrap`, so template indentation is content.

    The CSS is deliberate -- the custom verification message is free text an
    admin wrote, and reflowing their line breaks would show them something they
    did not type. The cost is that every other value in one of those paragraphs
    renders the template's own newlines and indentation too, which is a hanging
    indent and a blank line under every setting on the page.

    It went unnoticed until a status line long enough to wrap made it obvious.
    This is the guard, and it is page-wide rather than about one field.
    """

    def page(self, config, store, **kwargs):
        api = FakeBotAPI(**kwargs)
        app = create_app(config, store=store, client=api)
        app.config.update(TESTING=True)
        test_client = app.test_client()
        login_as(test_client, store)
        return test_client.get(f"/guild/{GUILD_IN}/settings").data.decode()

    def values(self, html):
        return re.findall(r'<p class="value[^"]*">(.*?)</p>', html, re.S)

    def test_no_value_starts_or_ends_with_whitespace(self, config, store):
        html = self.page(
            config,
            store,
            settings=make_settings(
                premium=True,
                values={"vrchat_group_id": GROUP_ID},
                group_invite=group_invite_block(
                    state="ready", can_invite=True, can_see_members=True,
                    group_name="Club LA",
                ),
            ),
        )
        rendered = self.values(html)
        assert rendered, "no values on the page at all -- the regex is wrong"
        for value in rendered:
            assert value == value.strip(), repr(value)

    def test_the_status_line_is_one_line(self, config, store):
        """It reads "Ready — Club LA", not "Ready" and then a hanging dash."""
        html = self.page(
            config,
            store,
            settings=make_settings(
                premium=True,
                values={"vrchat_group_id": GROUP_ID},
                group_invite=group_invite_block(
                    state="ready", can_invite=True, can_see_members=True,
                    group_name="Club LA",
                ),
            ),
        )
        assert '<p class="value">Ready — Club LA</p>' in html

    def test_an_admins_own_line_breaks_still_survive(self, config, store):
        """The reason the CSS is what it is. Stripping the template's
        whitespace must not touch the value's own."""
        html = self.page(
            config,
            store,
            settings=make_settings(
                premium=True,
                values={"custom_verification_requested_message": "one\ntwo"},
            ),
        )
        assert "one\ntwo" in html


class TestTheAccountIsNamedOnlyWhileItMatters:
    """"Invite this account" is an instruction, so it stops once the bot is in
    the group. A completed instruction left on screen reads as one that did not
    work -- which is exactly how it looked beside "the bot is in your group and
    can send invites"."""

    def summary(self, **block):
        settings = make_settings(
            premium=True,
            values={"vrchat_group_id": GROUP_ID},
            group_invite=group_invite_block(**block),
        )
        return settings_view.group_setup_summary(settings)

    @pytest.mark.parametrize(
        "state", ["unverified", "not_invited", "code_missing", "join_requested"]
    )
    def test_it_is_named_while_somebody_still_has_to_invite_it(self, state):
        assert self.summary(state=state)["show_account"] is True

    @pytest.mark.parametrize("state", ["ready", "no_invite_permission"])
    def test_it_is_not_named_once_the_bot_is_in_the_group(self, state):
        """no_invite_permission is a member that cannot invite -- a permissions
        problem, not an invitation one, and telling them to invite it again
        sends them down the wrong path entirely."""
        assert self.summary(state=state)["show_account"] is False

    def test_a_locked_section_never_names_it(self):
        settings = make_settings(
            premium=False,
            values={"vrchat_group_id": GROUP_ID},
            group_invite=group_invite_block(),
        )
        assert settings_view.group_setup_summary(settings)["show_account"] is False

    def test_the_toggle_label_is_short_enough_to_sit_beside_a_checkbox(self):
        """The template renders the label twice -- once as the setting's name,
        once as the checkbox's caption -- which is fine for "Nickname sync" and
        silly for a sentence."""
        settings = make_settings(premium=True)
        field = next(
            f
            for group in settings_view.build_groups(
                settings, DEFAULT_ROLES, DEFAULT_CHANNELS
            )
            for f in group["fields"]
            if f.name == "vrchat_group_invite_enabled"
        )
        assert len(field.label.split()) <= 4


# What the API stores: the raw file, which an <img> cannot use.
ICON_URL = (
    "https://api.vrchat.cloud/api/1/file/"
    "file_5ec52378-026d-4479-a2ea-914c52598964/1/file"
)
# ...and what the page must emit instead: a path on this site, because the
# icon is proxied rather than hotlinked.
ICON_DISPLAY_URL = "/vrchat-icon/file_5ec52378-026d-4479-a2ea-914c52598964/1"


class TestTheGroupIconOnThePage:
    def summary(self, premium=True, **block):
        settings = make_settings(
            premium=premium,
            values={"vrchat_group_id": GROUP_ID},
            group_invite=group_invite_block(**block),
        )
        return settings_view.group_setup_summary(settings)

    def test_the_page_points_at_this_site_not_at_vrchat(self):
        """Two attempts at hotlinking VRChat's own URLs both shipped broken.
        The icon is fetched by this app and served from its own origin, which
        is the version that does not depend on what a third party decides to
        call its bytes today."""
        emitted = self.summary(icon_url=ICON_URL)["icon_url"]
        assert emitted == ICON_DISPLAY_URL
        assert "vrchat.cloud" not in emitted

    def test_the_emitted_url_is_built_and_never_echoed(self):
        """Assembled from a file id and a version that both had to match, so
        there is no path by which a stored string reaches an src attribute
        intact."""
        weird = (
            "https://api.vrchat.cloud/api/1/file/"
            "FILE_5EC52378-026D-4479-A2EA-914C52598964/1/file"
        )
        assert self.summary(icon_url=weird)["icon_url"] == ICON_DISPLAY_URL

    def test_a_group_with_no_icon_shows_none(self):
        assert self.summary()["icon_url"] is None

    @pytest.mark.parametrize(
        "url",
        [
            # Not https.
            ICON_URL.replace("https://", "http://"),
            # The host is a prefix of another host, which is not the same host.
            ICON_URL.replace("api.vrchat.cloud", "api.vrchat.cloud.evil.test"),
            # The host in a path.
            "https://evil.test/api.vrchat.cloud/api/1/file/file_x/1/file",
            # Anything appended: the pattern is anchored at both ends.
            ICON_URL + "/../../evil",
            ICON_URL + "?x=1",
            # A file id that is not one.
            ICON_URL.replace("file_5ec52378-026d-4479-a2ea-914c52598964", "file_x"),
            "javascript:alert(1)",
            "data:image/png;base64,AAAA",
            "/local/path.png",
            12345,
        ],
    )
    def test_anything_the_csp_would_refuse_is_not_emitted(self, url):
        """Not really about an attacker -- the value comes from the worker,
        which read it off the group. It is about not emitting a `src` the
        browser will refuse, which renders as a broken image with the
        explanation only in a console nobody has open.
        """
        assert self.summary(icon_url=url)["icon_url"] is None

    def test_a_locked_section_still_shows_it(self):
        """A picture of the admin's own group is not a step they are being
        asked to take, so it survives what the instructions do not."""
        summary = self.summary(premium=False, icon_url=ICON_URL)
        assert summary["locked"] is True
        assert summary["icon_url"] == ICON_DISPLAY_URL
        assert summary["show_account"] is False

    def test_the_page_renders_it_above_the_name(self, config, store):
        api = FakeBotAPI(settings=make_settings(
            premium=True,
            values={"vrchat_group_id": GROUP_ID},
            group_invite=group_invite_block(
                state="ready", can_invite=True, can_see_members=True,
                group_name="Club LA", icon_url=ICON_URL,
            ),
        ))
        app = create_app(config, store=store, client=api)
        app.config.update(TESTING=True)
        test_client = app.test_client()
        login_as(test_client, store)
        html = test_client.get(f"/guild/{GUILD_IN}/settings").data.decode()

        assert f'<img class="group-icon" src="{ICON_DISPLAY_URL}"' in html
        assert html.index("group-icon") < html.index("Ready — Club LA")

    def test_the_csp_never_had_to_be_widened_for_vrchat(self, config, store):
        """The point of proxying. A same-origin image needs no exception, so
        the policy went back to what it was before the icon existed."""
        app = create_app(config, store=store, client=FakeBotAPI())
        app.config.update(TESTING=True)
        response = app.test_client().get("/")
        directive = next(
            part.strip()
            for part in response.headers["Content-Security-Policy"].split(";")
            if part.strip().startswith("img-src")
        )
        assert directive == "img-src 'self' https://cdn.discordapp.com"
        assert "vrchat" not in response.headers["Content-Security-Policy"]


class TestTheIconProxy:
    """Serving the icon ourselves, which is what made it work at all.

    VRChat's own URLs were tried twice: `icon_url` is `application/octet-stream`
    and browsers refuse to draw it, and the resized endpoint is `image/png`
    only up to 512. Relying on a third party's content type is what put a
    broken image on a live page, so this app looks at the bytes and decides.
    """

    PNG = bytes([137, 80, 78, 71, 13, 10, 26, 10]) + b"rest of a png"
    FILE_ID = "file_5ec52378-026d-4479-a2ea-914c52598964"

    def client_for(self, config, store, fetch=None):
        app = create_app(config, store=store, client=FakeBotAPI())
        app.config.update(TESTING=True)
        if fetch is not None:
            app.config["ICON_CACHE"] = _StubCache(fetch)
        test_client = app.test_client()
        login_as(test_client, store)
        return test_client

    def test_a_signed_in_admin_gets_the_image(self, config, store):
        test_client = self.client_for(
            config, store, fetch=lambda: ("image/png", self.PNG)
        )
        response = test_client.get(f"/vrchat-icon/{self.FILE_ID}/1")
        assert response.status_code == 200
        assert response.headers["Content-Type"].startswith("image/png")
        assert response.data == self.PNG

    def test_it_is_not_an_anonymous_relay(self, config, store):
        """The files are public, so this is not protecting them. It is
        refusing to be a general-purpose fetcher for people with no account."""
        app = create_app(config, store=store, client=FakeBotAPI())
        app.config.update(TESTING=True)
        response = app.test_client().get(f"/vrchat-icon/{self.FILE_ID}/1")
        assert response.status_code == 404

    @pytest.mark.parametrize(
        "path",
        [
            "/vrchat-icon/usr_5ec52378-026d-4479-a2ea-914c52598964/1",
            "/vrchat-icon/file_not-a-uuid/1",
            "/vrchat-icon/file_5ec52378-026d-4479-a2ea-914c52598964/abc",
            "/vrchat-icon/file_5ec52378-026d-4479-a2ea-914c52598964/99999",
            # str.isdigit() and \d both say yes to this. [0-9] does not, and
            # a validator that does not mean what it says is one somebody
            # later relies on for something that matters.
            "/vrchat-icon/file_5ec52378-026d-4479-a2ea-914c52598964/\u0663",
            "/vrchat-icon/file_5ec52378-026d-4479-a2ea-914c52598964/\u00b2",
        ],
    )
    def test_only_a_real_file_id_and_version_are_accepted(self, config, store, path):
        """The pair goes into a fixed host and path, so there is no request in
        which a caller names a host -- but a malformed one must not reach
        VRChat at all."""
        called = []

        def fetch():
            called.append(True)
            return ("image/png", self.PNG)

        test_client = self.client_for(config, store, fetch=fetch)
        assert test_client.get(path).status_code == 404
        assert called == []

    def test_a_failed_fetch_is_a_gap_not_a_crash(self, config, store):
        test_client = self.client_for(config, store, fetch=lambda: None)
        assert test_client.get(f"/vrchat-icon/{self.FILE_ID}/1").status_code == 404

    def test_it_is_cached_privately(self, config, store):
        test_client = self.client_for(
            config, store, fetch=lambda: ("image/png", self.PNG)
        )
        response = test_client.get(f"/vrchat-icon/{self.FILE_ID}/1")
        assert "private" in response.headers["Cache-Control"]
        assert "no-store" not in response.headers["Cache-Control"]


class _StubCache:
    """Stands in for _IconCache without caching, so each test sees its own
    fetch rather than the previous test's."""

    def __init__(self, fetch):
        self._fetch = fetch

    def get(self, key, fetch, *, now):
        return self._fetch()


class TestSniffingTheBytes:
    """The upstream content type is the thing that could not be trusted, so
    the proxy does not repeat it."""

    def test_a_png_is_recognised(self):
        assert app_module._sniff_image(bytes([137, 80, 78, 71, 13, 10, 26, 10])) == "image/png"

    def test_a_jpeg_is_recognised(self):
        assert app_module._sniff_image(b"\xff\xd8\xff\xe0rest") == "image/jpeg"

    def test_a_webp_is_recognised(self):
        assert app_module._sniff_image(b"RIFF\x00\x00\x00\x00WEBPVP8 ") == "image/webp"

    def test_a_riff_that_is_not_a_webp_is_not_an_image(self):
        assert app_module._sniff_image(b"RIFF\x00\x00\x00\x00WAVEfmt ") is None

    @pytest.mark.parametrize(
        "body",
        [b"", b"<!doctype html>", b"{\"error\": \"nope\"}", b"%PDF-1.4"],
    )
    def test_anything_else_is_refused(self, body):
        """An upstream that answers with an error page must not have it
        forwarded to a browser as a picture."""
        assert app_module._sniff_image(body) is None


class TestTheProxyCannotBeAimedElsewhere:
    """The safety argument is that the host is fixed and no caller names one.

    Everything here is about keeping that true, because the moment it stops
    being true this is a fetcher that runs inside the network perimeter.
    """

    FILE_ID = "file_" + "0" * 8 + "-0000-0000-0000-" + "0" * 12
    PNG = bytes([137, 80, 78, 71, 13, 10, 26, 10]) + b"body"

    def fake_requests(self, monkeypatch, script):
        """`script` maps a URL to (status, headers, body). Records the hops."""
        seen = []

        class Raw:
            def __init__(self, body):
                self._body = body

            def read(self, size, decode_content=False):
                return self._body[:size]

        class Resp:
            def __init__(self, status, headers, body):
                self.status_code = status
                self.headers = headers
                self.raw = Raw(body)
                self.is_redirect = status in (301, 302, 303, 307, 308)
                self.is_permanent_redirect = status in (301, 308)

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def fake_get(url, **kwargs):
            seen.append((url, kwargs))
            if url not in script:
                raise AssertionError(f"unscripted fetch: {url}")
            return Resp(*script[url])

        monkeypatch.setattr(app_module.requests, "get", fake_get)
        return seen

    def test_every_hop_is_taken_deliberately(self, monkeypatch):
        """allow_redirects is always False. The loop takes each hop itself,
        after checking it -- requests offers no per-hop hook, and
        allow_redirects=True would take any of them on trust."""
        first = app_module.VRCHAT_ICON_URL.format(file_id=self.FILE_ID, version="1")
        second = "https://files.vrchat.cloud/thumbnails/x.png?Signature=y"
        seen = self.fake_requests(monkeypatch, {
            first: (302, {"Location": second}, b""),
            second: (200, {}, self.PNG),
        })

        assert app_module._fetch_vrchat_icon(self.FILE_ID, "1") == ("image/png", self.PNG)
        assert [url for url, _ in seen] == [first, second]
        assert all(kw["allow_redirects"] is False for _, kw in seen)

    @pytest.mark.parametrize(
        "location",
        [
            "http://169.254.169.254/latest/meta-data/",
            "https://169.254.169.254/latest/meta-data/",
            "https://evil.test/x",
            "https://files.vrchat.cloud@evil.test/x",
            "http://files.vrchat.cloud/x",
            "https://files.vrchat.cloud:8443/x",
            "//evil.test/x",
        ],
    )
    def test_a_redirect_off_the_allowed_hosts_is_refused(self, monkeypatch, location):
        """The one that matters. Following a response's choice of host is how a
        fetcher inside the network perimeter becomes somebody else's."""
        first = app_module.VRCHAT_ICON_URL.format(file_id=self.FILE_ID, version="1")
        seen = self.fake_requests(monkeypatch, {first: (302, {"Location": location}, b"")})

        assert app_module._fetch_vrchat_icon(self.FILE_ID, "1") is None
        assert len(seen) == 1, "it must not have fetched the redirect target"

    def test_a_relative_redirect_is_resolved_and_still_checked(self, monkeypatch):
        first = app_module.VRCHAT_ICON_URL.format(file_id=self.FILE_ID, version="1")
        resolved = (
            "https://api.vrchat.cloud/api/1/image/"
            + self.FILE_ID
            + "/1/elsewhere.png"
        )
        seen = self.fake_requests(monkeypatch, {
            first: (302, {"Location": "elsewhere.png"}, b""),
            resolved: (200, {}, self.PNG),
        })
        assert app_module._fetch_vrchat_icon(self.FILE_ID, "1") == ("image/png", self.PNG)
        assert [url for url, _ in seen] == [first, resolved]

    def test_a_redirect_loop_ends(self, monkeypatch):
        first = app_module.VRCHAT_ICON_URL.format(file_id=self.FILE_ID, version="1")
        seen = self.fake_requests(monkeypatch, {first: (302, {"Location": first}, b"")})
        assert app_module._fetch_vrchat_icon(self.FILE_ID, "1") is None
        assert len(seen) == app_module.VRCHAT_ICON_MAX_HOPS

    @pytest.mark.parametrize(
        "url,allowed",
        [
            ("https://api.vrchat.cloud/x", True),
            ("https://files.vrchat.cloud/x", True),
            ("https://FILES.VRCHAT.CLOUD/x", True),
            ("https://files.vrchat.cloud@evil.test/x", False),
            ("https://evil.test/x", False),
            ("http://files.vrchat.cloud/x", False),
            ("https://files.vrchat.cloud:8443/x", False),
            ("https://sub.files.vrchat.cloud/x", False),
            ("https://files.vrchat.cloud.evil.test/x", False),
            ("not a url at all", False),
        ],
    )
    def test_which_hosts_are_allowed(self, url, allowed):
        assert app_module._vrchat_hop_allowed(url) is allowed

    def test_an_oversized_response_is_dropped(self, monkeypatch):
        """The cap bounds the cache as much as the response: entries live in
        memory in a read_only container, and limit x max_bytes is the ceiling."""

        class Raw:
            def read(self, size, decode_content=False):
                return b"\x89PNG\r\n\x1a\n" + b"x" * size

        class Resp:
            status_code = 200
            headers: dict = {}
            raw = Raw()
            is_redirect = False
            is_permanent_redirect = False

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        monkeypatch.setattr(app_module.requests, "get", lambda url, **kw: Resp())
        assert app_module._fetch_vrchat_icon(
            "file_" + "0" * 8 + "-0000-0000-0000-" + "0" * 12, "1"
        ) is None


class TestTheIconCache:
    def test_a_success_is_reused(self):
        cache = app_module._IconCache(ttl=100)
        calls = []
        fetch = lambda: (calls.append(1), ("image/png", b"x"))[1]
        assert cache.get("k", fetch, now=0) == ("image/png", b"x")
        assert cache.get("k", fetch, now=50) == ("image/png", b"x")
        assert len(calls) == 1

    def test_a_failure_expires_much_sooner_than_a_success(self):
        """Caching failures stops a dead upstream becoming a stream of
        outbound requests. Caching them for an hour would hide an icon far
        longer than the blip that lost it."""
        cache = app_module._IconCache(ttl=3600, failure_ttl=60)
        calls = []

        def fetch():
            calls.append(1)
            return None

        cache.get("k", fetch, now=0)
        cache.get("k", fetch, now=30)
        assert len(calls) == 1, "still inside the failure window"
        cache.get("k", fetch, now=90)
        assert len(calls) == 2, "past it, so it tries again"

    def test_it_stays_within_its_limit(self):
        cache = app_module._IconCache(ttl=100, limit=3)
        for n in range(10):
            cache.get(n, lambda: ("image/png", b"x"), now=float(n))
        assert len(cache._entries) <= 3

    def test_eviction_survives_another_thread_writing(self):
        """min() over a dict another thread is writing raises "dictionary
        changed size during iteration" -- rarely, which is the worst frequency
        a crash can have. gunicorn runs four threads per worker."""
        import threading

        cache = app_module._IconCache(ttl=100, limit=64)
        errors = []

        def hammer(base):
            try:
                for n in range(400):
                    cache.get((base, n), lambda: ("image/png", b"x"), now=float(n))
            except Exception as error:  # pragma: no cover - the bug being pinned
                errors.append(error)

        threads = [threading.Thread(target=hammer, args=(i,)) for i in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        assert errors == []
        assert len(cache._entries) <= 64
