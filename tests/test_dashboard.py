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

import dataclasses
import json
import logging
import os
import re
import sqlite3
import stat
import struct
import threading
import time
from datetime import date
from html.parser import HTMLParser
from types import SimpleNamespace

import pytest

pytest.importorskip("flask")

from dashboard import changelog, i18n, oauth, overview_view, settings_view  # noqa: E402
from dashboard.app import CSP, SESSION_COOKIE, create_app  # noqa: E402
from dashboard import app as app_module  # noqa: E402
from dashboard.botapi import BotAPIError  # noqa: E402
from dashboard.config import DashboardConfig, DashboardConfigError  # noqa: E402
from dashboard import sessions as sessions_module  # noqa: E402
from dashboard.sessions import SessionStore  # noqa: E402

ACTOR = "424242424242"


def _premium_entry():
    """The one premium entry in the shipped feed.

    Asserted rather than assumed: several tests below read as nonsense if the
    feed ever carries two, and a helper quietly returning the first would hide
    that rather than say so.
    """
    found = [entry for entry in changelog.ENTRIES if entry.premium]
    assert len(found) == 1, "these tests assume exactly one premium entry"
    return found[0]
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


_UNSET = object()


def make_overview(
    member_count=1284,
    total=417,
    today=3,
    last_7_days=12,
    last_30_days=63,
    collecting_since="2026-06-01",
    daily=_UNSET,
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

    `daily` defaults to a plausible 30-day series rather than to None, because
    None is the failure state and a fixture should default to the ordinary
    one. Pass `daily=None` to build the state where the rollup could not be
    read at all.

    That this shape really does match the bot is checked by
    `test_overview.py::TestTheFakePayloadMatchesTheRealOne` -- the claim in the
    line above used to be only a claim.
    """
    if daily is _UNSET:
        # Thirty measured days, the last of them carrying `today`, so the
        # series and the tiles beside it tell the same story by default.
        daily = [
            {"day": f"2026-07-{day:02d}", "count": 0} for day in range(1, 30)
        ] + [{"day": "2026-07-30", "count": today}]

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
            "daily": daily,
            "collecting_since": collecting_since,
            "known": known,
        },
        "panel": {"posted": True, "channel_id": LOG_CHANNEL} if panel is None else panel,
        "configured": (
            {
                "verified_role": True,
                "verified_role_exists": True,
                "verified_role_assignable": True,
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


# Settings is a page per group now (#140), so "the settings page" is no longer
# an address. A test either says which group it is about, or -- when its
# subject is markup that lives on Settings without belonging to any one group
# -- reads all five and joins them.
SETTINGS_GROUPS = ("verification", "after-verifying", "panel", "vrchat-group", "logging")


def settings_page(test_client, group="verification", guild=None):
    """One group's settings page. Verification by default: it is the group the
    bare /settings URL redirects to, so it is what "Settings" used to mean for
    every test that only needed the page's chrome."""
    return test_client.get(f"/guild/{guild or GUILD_IN}/settings/{group}")


def every_settings_page(test_client, guild=None):
    """All five groups' pages, joined.

    The faithful translation of a test that scanned "the settings page" for a
    field, a badge or a warning: the markup is still all on Settings, just no
    longer all in one response. Callers that search for something specific
    still assert they found it, so a join that silently returned nothing would
    fail rather than pass vacuously.
    """
    return "".join(
        settings_page(test_client, group, guild).data.decode()
        for group in SETTINGS_GROUPS
    )


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


def csrf_from(page: str) -> str:
    """The token a rendered page is carrying, read the way a browser would.

    Posting `session.csrf_token` directly is what most tests here do and is
    fine; this exists for the ones that are also asserting the FORM carries a
    usable token, where taking it from the session would pass even if the
    template had left the field out.
    """
    match = re.search(r'name="csrf_token" value="([^"]+)"', page)
    assert match, "no CSRF token in the page"
    return match.group(1)


def set_cookie_header(response, name: str) -> str:
    """The one `Set-Cookie` header for `name`, attributes and all.

    `response.headers.getlist` rather than `.get`, which returns whichever
    happens to be first -- a response setting two cookies would otherwise be
    asserted against the wrong one.
    """
    for header in response.headers.getlist("Set-Cookie"):
        if header.startswith(f"{name}="):
            return header
    raise AssertionError(f"no Set-Cookie for {name!r}")


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
        # An install action for the one the bot is not in. Asserted as the
        # invite link rather than the button's wording -- the copy is #133's
        # to change, the routing is not.
        assert b"Add to server" in response.data
        # Installed servers link into their pages; absent ones offer the
        # invite instead and link nowhere.
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

    # --- the cards themselves (#133 phase 3) ---

    @staticmethod
    def _card(page: str, guild_id: str) -> str:
        """One card's markup, found by the link or invite it contains."""
        cards = re.findall(r'<li class="server-card [^"]*">.*?</li>', page, re.S)
        for card in cards:
            if guild_id in card:
                return card
        raise AssertionError(f"no card for {guild_id}")

    def test_an_installed_server_offers_no_install_button(self, client, store):
        """The button is the whole signal. On a card that is already set up it
        would be an instruction to redo something that is done."""
        login_as(client, store)
        card = self._card(client.get("/").data.decode(), GUILD_IN)
        assert "server-action" not in card
        assert "Add to server" not in card

    def test_an_absent_server_has_a_button_and_goes_nowhere_else(
        self, client, store
    ):
        """Its pages would refuse -- the bot is not in it -- so the name is
        text and the invite is the only thing to click."""
        login_as(client, store)
        card = self._card(client.get("/").data.decode(), GUILD_OUT)
        assert "Add to server" in card
        assert f'href="/guild/{GUILD_OUT}"' not in card

    def test_the_two_states_differ_by_more_than_a_colour(self, client, store):
        """The point of the Sentry pattern this is shaped after: a card is
        told apart by what is in it, not by what shade it is. Someone seeing
        the page in greyscale, or not seeing colour at all, still gets it."""
        page = client.get("/").data.decode()
        login_as(client, store)
        page = client.get("/").data.decode()
        installed = self._card(page, GUILD_IN)
        absent = self._card(page, GUILD_OUT)

        def structure(card):
            return sorted(set(re.findall(r'class="([a-z- ]+)"', card)))

        assert structure(installed) != structure(absent)

    def test_an_unreachable_bot_offers_no_install_buttons_at_all(
        self, client, store, config
    ):
        """THE BUG THIS PHASE FIXES.

        `installed` is false for a server the bot is not in AND for every
        server when the bot could not be asked. Rendering those the same way
        meant that a brief outage turned this page into an invitation to
        reinstall the bot on servers it was already running in -- with only
        the banner above to contradict it.

        Unknown is now its own state, and it offers nothing to click that
        could make things worse.
        """
        app = create_app(config, store=store, client=FakeBotAPI(fail=True))
        app.config.update(TESTING=True)
        test_client = app.test_client()
        login_as(test_client, store)

        page = test_client.get("/").data.decode()
        assert "Add to server" not in page
        assert "disable_guild_select" not in page
        # And it says so on each card rather than only in the banner.
        assert page.count("Can't check this server right now") == 2

    def test_an_unreachable_bot_still_lets_you_try_a_server(
        self, client, store, config
    ):
        """Refusing to link anywhere would be the other overreaction. The
        server pages will either work or explain themselves; that beats this
        page deciding on their behalf from a failed read."""
        app = create_app(config, store=store, client=FakeBotAPI(fail=True))
        app.config.update(TESTING=True)
        test_client = app.test_client()
        login_as(test_client, store)

        page = test_client.get("/").data.decode()
        assert f'href="/guild/{GUILD_IN}"' in page

    def test_the_empty_state_offers_the_fix_it_describes(self, client, store):
        """It is reachable by an ordinary route -- promoted since last sign-in
        -- and the remedy really is to sign in again, so the page should do
        that rather than describe it."""
        # `admin_hint` false is exactly the case the copy describes: Discord
        # reported these servers at sign-in and said you administered none of
        # them. `guilds=[]` would not do -- login_as falls back to the default
        # list on an empty one.
        login_as(
            client,
            store,
            guilds=[
                {"id": GUILD_IN, "name": "Alpha Club", "icon": None,
                 "admin_hint": False}
            ],
        )
        page = client.get("/").data.decode()

        assert "No servers to show" in page
        # Scoped to the empty block on purpose. Searching the whole page for
        # `action="/logout"` passes on the account menu in the header, which
        # is on every page -- so the unscoped version of this assertion could
        # not fail, and did not when the form was deleted to check.
        start = page.index('<div class="empty-state">')
        empty = page[start : page.index("</div>", start)]
        assert 'action="/logout"' in empty
        assert "empty-title" in empty


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
        response = settings_page(client)
        assert response.status_code == 302
        assert response.headers["Location"].endswith("/")

    def test_values_are_shown_with_names_not_ids(self, config, store):
        """A read-only field shows the role's name and never its id."""
        test_client, _api = settings_client(
            config, store, settings=make_settings(writable=set())
        )
        page = settings_page(test_client).data.decode()
        assert "Verified" in page
        assert VERIFIED_ROLE not in page

    def test_an_editable_role_is_labelled_by_name(self, config, store):
        """Editable, the id has to be in the option value -- the label doesn't."""
        test_client, _api = settings_client(config, store)
        page = settings_page(test_client).data.decode()
        assert f'<option value="{VERIFIED_ROLE}" selected>Verified</option>' in page

    def test_every_read_is_scoped_to_the_session_owner_and_that_guild(
        self, config, store
    ):
        """Across all six sub-pages, so no group gets its own idea of scope."""
        test_client, api = settings_client(config, store)
        for group in SETTINGS_GROUPS + ("activity",):
            settings_page(test_client, group)
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

    # ----- what each group actually reads (#140) -----
    #
    # The point of the split that is easy to lose: a page per group can ask the
    # bot for less than a page showing everything had to. If one of these ever
    # grows a read it does not render, this is the test that says so.
    @pytest.mark.parametrize(
        "group, expected",
        [
            ("verification", {"settings", "roles"}),
            ("after-verifying", {"settings"}),
            ("panel", {"settings", "channels", "panel"}),
            ("vrchat-group", {"settings"}),
            ("logging", {"settings", "channels"}),
            # Roles and channels because the history resolves the ids inside
            # each entry into names -- without them a record of role changes
            # reads as a list of numbers.
            ("activity", {"settings", "roles", "channels", "audit"}),
        ],
    )
    def test_a_group_reads_only_what_it_renders(self, config, store, group, expected):
        test_client, api = settings_client(config, store)
        settings_page(test_client, group)
        assert {what for what, _, _ in api.reads} == expected

    def test_no_group_reads_the_audit_trail_except_activity(self, config, store):
        """It used to be fetched on every settings page load, and shown on one."""
        test_client, api = settings_client(config, store)
        for group in SETTINGS_GROUPS:
            settings_page(test_client, group)
        assert not [what for what, _, _ in api.reads if what == "audit"]

    def test_the_guild_name_comes_from_the_session_not_the_bot(self, config, store):
        test_client, _api = settings_client(config, store)
        assert b"Alpha Club" in settings_page(test_client).data

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

        response = settings_page(test_client)
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
        return settings_page(test_client)

    def test_403_and_404_are_byte_identical(self, config, store):
        # One client, so the comparison isn't confounded by the per-session
        # CSRF token in the sign-out form.
        test_client, api = settings_client(
            config, store, errors={"settings": BotAPIError("nope", 403)}
        )
        forbidden = settings_page(test_client)

        api.errors = {"settings": BotAPIError("nope", 404)}
        missing = settings_page(test_client)

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
        # "English (en-US)" rather than the bare code, because since #97 the
        # page legitimately carries `lang="en-US"` on <html> -- that attribute
        # is the language this reader is being answered in and has nothing to
        # do with the guild whose settings could not be read. What must never
        # appear is the locale FIELD's default, and `locale_label` is what
        # renders it.
        for never in ("Not set", "Default blue", "English (en-US)"):
            assert never not in page


class TestTheBell:
    """The what's-new panel in the header (issue #136 phase 2).

    The rules about WHICH entries go where are pinned in test_changelog.py
    against the pure module. What is pinned here is the part only a request
    can answer: that the panel is markup rather than a script, that it is
    absent for anybody who should not see it, and that the dot tells the truth
    about this browser.
    """

    def test_it_opens_without_a_script(self, client, store):
        """A <details>, like the two menus beside it. This is not a
        preference here: the CSP is `default-src 'none'` with no
        `connect-src`, so a panel that fetched its contents could not open at
        all."""
        login_as(client, store)
        page = client.get("/").data.decode()
        assert '<details class="bell bar-menu"' in page
        bell = page.split('<details class="bell', 1)[1].split("</details>", 1)[0]
        assert "onclick" not in bell and "javascript:" not in bell

    def test_it_is_absent_when_signed_out(self, client):
        """The sign-in page has nothing to announce to somebody who has not
        arrived yet. #137 is where a stranger reads this list."""
        page = client.get("/").data.decode()
        assert "bell bar-menu" not in page

    def test_it_renders_the_entries(self, client, store):
        login_as(client, store)
        page = client.get("/").data.decode()
        newest = changelog.ENTRIES[0]
        assert newest.title in page
        assert newest.display_date in page
        assert f'datetime="{newest.date.isoformat()}"' in page

    def test_it_shows_at_most_a_handful(self, client, store):
        """A dropdown is a summary; #137's page is the list."""
        login_as(client, store)
        page = client.get("/").data.decode()
        bell = page.split('<details class="bell', 1)[1].split("</details>", 1)[0]
        assert bell.count("bell-title") <= changelog.BELL_LIMIT

    def test_a_premium_entry_never_wears_the_lock_badge(self, client, store):
        """`.badge.premium` means "your plan cannot use this" on the settings
        page, and this bell renders on that page. One chip with two meanings
        on one document is a chip that means neither."""
        login_as(client, store)
        page = client.get("/").data.decode()
        bell = page.split('<details class="bell', 1)[1].split("</details>", 1)[0]
        assert "badge premium" not in bell
        assert "bell-tag premium" in bell

    def test_the_dot_is_there_for_a_browser_that_has_seen_nothing(
        self, client, store
    ):
        login_as(client, store)
        page = client.get("/").data.decode()
        assert "bell-dot" in page
        assert "There are updates you haven" in page

    def test_the_dot_goes_once_the_newest_is_seen(self, client, store):
        login_as(client, store)
        client.set_cookie("vrcverify_seen", changelog.ENTRIES[0].id)
        page = client.get("/").data.decode()
        assert "bell-dot" not in page

    def test_an_older_id_still_leaves_the_dot(self, client, store):
        login_as(client, store)
        client.set_cookie("vrcverify_seen", changelog.ENTRIES[-1].id)
        page = client.get("/").data.decode()
        assert "bell-dot" in page

    def test_a_hand_edited_cookie_shows_the_dot_rather_than_hiding_entries(
        self, client, store
    ):
        """Validated against the ids actually shipped. Trusting an
        unrecognised value would hide entries this browser has never seen; the
        dot appearing once more is the harmless direction to fail in."""
        login_as(client, store)
        client.set_cookie("vrcverify_seen", "../../etc/passwd")
        page = client.get("/").data.decode()
        assert "bell-dot" in page
        assert "etc/passwd" not in page

    def test_marking_read_writes_the_cookie_and_comes_back(self, client, store):
        login_as(client, store)
        token = csrf_from(client.get("/").data.decode())
        response = client.post(
            "/prefs/seen", data={"csrf_token": token, "return_to": "index"}
        )
        assert response.status_code == 302
        assert response.headers["Location"] == "/"
        cookie = set_cookie_header(response, "vrcverify_seen")
        assert changelog.ENTRIES[0].id in cookie
        assert "Secure" in cookie and "SameSite=Lax" in cookie
        # Not httponly: prefs.js writes this one too, and with no connect-src
        # it has no other way to say the panel was opened.
        assert "HttpOnly" not in cookie

    def test_it_takes_the_newest_id_from_us_not_from_the_form(self, client, store):
        """A crafted post must not be able to mark an entry seen that the
        browser never saw."""
        login_as(client, store)
        token = csrf_from(client.get("/").data.decode())
        response = client.post(
            "/prefs/seen",
            data={"csrf_token": token, "return_to": "index", "seen": "whatever"},
        )
        assert changelog.ENTRIES[0].id in set_cookie_header(response, "vrcverify_seen")

    def test_marking_read_needs_a_token(self, client, store):
        login_as(client, store)
        assert client.post("/prefs/seen", data={"return_to": "index"}).status_code == 400

    def test_marking_read_needs_a_session(self, client):
        response = client.post("/prefs/seen", data={"return_to": "index"})
        assert response.status_code == 302
        assert response.headers["Location"] == "/"

    def test_it_never_reaches_the_bot(self, config, store):
        """A dot in the header must not be a way to spend the bot's rate
        limit. Same assertion as `/prefs/nav`, for the same reason."""
        test_client, api = settings_client(config, store)
        session = store.load(test_client.get_cookie(SESSION_COOKIE).value)
        api.reads.clear()
        api.calls.clear()
        api.saves.clear()
        test_client.post(
            "/prefs/seen",
            data={
                "csrf_token": session.csrf_token,
                "return_to": "guild_settings",
                "guild_id": GUILD_IN,
            },
        )
        assert api.reads == []
        assert api.calls == []
        assert api.saves == []


class TestDismissingAPremiumCard:
    """The `guild:entry` cookie and the POST that writes it (#136 phase 4)."""

    def _client(self, config, store):
        test_client, _api = settings_client(
            config, store, overview=make_overview(last_30_days=214)
        )
        return test_client

    def _dismiss(self, test_client, store, guild_id=None, entry_id=None):
        session = store.load(test_client.get_cookie(SESSION_COOKIE).value)
        return test_client.post(
            "/prefs/dismiss",
            data={
                "csrf_token": session.csrf_token,
                "return_to": "guild_overview",
                "guild_id": guild_id or GUILD_IN,
                "entry_id": entry_id or _premium_entry().id,
            },
        )

    def test_the_card_is_dismissible_with_javascript_disabled(self, config, store):
        """An ordinary form post. The acceptance criterion is about the reader
        with scripts blocked, so the control cannot be script-driven."""
        test_client = self._client(config, store)
        page = test_client.get(f"/guild/{GUILD_IN}").data.decode()
        slot = page.split('class="panel group next-step"', 1)[1].split("</section>", 1)[0]
        assert 'action="/prefs/dismiss"' in slot
        assert "onclick" not in slot and "javascript:" not in slot

    def test_dismissing_puts_it_away_and_comes_back(self, config, store):
        test_client = self._client(config, store)
        response = self._dismiss(test_client, store)
        assert response.status_code == 302
        assert response.headers["Location"] == f"/guild/{GUILD_IN}"
        cookie = set_cookie_header(response, "vrcverify_dismissed")
        assert f"{GUILD_IN}:{_premium_entry().id}" in cookie
        assert "Secure" in cookie and "SameSite=Lax" in cookie

    def test_it_stays_dismissed(self, config, store):
        test_client = self._client(config, store)
        self._dismiss(test_client, store)
        page = test_client.get(f"/guild/{GUILD_IN}").data.decode()
        slot = page.split('class="panel group next-step"', 1)[1].split("</section>", 1)[0]
        assert _premium_entry().title not in slot

    def test_dismissing_one_guild_leaves_another_showing(self, config, store):
        """The property the whole per-guild design exists for: an admin
        running four servers is pitched once per server, not once in total."""
        test_client = self._client(config, store)
        self._dismiss(test_client, store, guild_id=GUILD_IN)
        cookie = test_client.get_cookie("vrcverify_dismissed").value
        assert changelog.is_dismissed(
            changelog.parse_dismissed(cookie), GUILD_IN, _premium_entry().id
        )
        assert not changelog.is_dismissed(
            changelog.parse_dismissed(cookie), "999999999999", _premium_entry().id
        )

    def test_an_unknown_entry_id_changes_nothing(self, config, store):
        """It has to come from the form -- which card was on screen is
        something only the page knows -- so it is checked against the shipped
        ids rather than trusted. That also keeps a crafted post from filling a
        bounded cookie with pairs that will never match."""
        test_client = self._client(config, store)
        response = self._dismiss(test_client, store, entry_id="not-an-entry")
        assert response.status_code == 302
        with pytest.raises(AssertionError):
            set_cookie_header(response, "vrcverify_dismissed")

    def test_a_non_numeric_guild_changes_nothing(self, config, store):
        test_client = self._client(config, store)
        response = self._dismiss(test_client, store, guild_id="../../etc")
        with pytest.raises(AssertionError):
            set_cookie_header(response, "vrcverify_dismissed")

    def test_it_needs_the_csrf_token(self, config, store):
        test_client = self._client(config, store)
        response = test_client.post(
            "/prefs/dismiss",
            data={"guild_id": GUILD_IN, "entry_id": _premium_entry().id,
                  "return_to": "guild_overview"},
        )
        assert response.status_code == 400

    def test_it_never_reaches_the_bot(self, config, store):
        test_client, api = settings_client(config, store)
        api.reads.clear()
        api.calls.clear()
        api.saves.clear()
        self._dismiss(test_client, store)
        assert api.reads == [] and api.calls == [] and api.saves == []

    def test_a_hand_edited_cookie_is_ignored_rather_than_trusted(self, config, store):
        """Dropping what it cannot parse means the card comes back, which is
        the harmless direction. Trusting it would let a crafted value hide an
        announcement the reader never saw."""
        test_client = self._client(config, store)
        test_client.set_cookie(
            "vrcverify_dismissed", "<script>:x,notaguild:y", domain="localhost"
        )
        page = test_client.get(f"/guild/{GUILD_IN}").data.decode()
        slot = page.split('class="panel group next-step"', 1)[1].split("</section>", 1)[0]
        assert _premium_entry().title in slot
        assert "<script>" not in slot

    def test_the_setup_step_and_the_demo_carry_no_dismiss_control(self, config, store):
        """Only a changelog entry has a permanent id to record dismissal
        against. A broken server must not be able to hide the reason, and the
        demo comes and goes with the numbers on its own."""
        test_client, _api = settings_client(
            config, store,
            overview=make_overview(last_30_days=214,
                                   configured={"verified_role": False}),
        )
        page = test_client.get(f"/guild/{GUILD_IN}").data.decode()
        slot = page.split('class="panel group next-step"', 1)[1].split("</section>", 1)[0]
        assert "/prefs/dismiss" not in slot

    def test_no_class_is_claimed_twice_from_two_ends_of_the_stylesheet(self):
        """#158, and the fourth of these is what this exists to stop.

        Three have now happened, all one shape: one word, two meanings, and a
        second rule block that silently inherits from the first.

          `.centered`  caught in #133 phase 5, before it shipped
          `.empty`     settings values drawn as bordered cards; shipped on
                       main through three phases with a green suite
          `.plan`      the purchase card inheriting the settings footnote's
                       italic and --muted, so the PRICE rendered as a footnote
                       on the page that takes money

        WHAT THIS MEASURES, AND WHY NOT THE OBVIOUS THING. The first attempt
        looked for component classes shared between two templates. That is not
        the defect: `.panel`, `.group` and `.button` are shared on purpose and
        it flagged all of them. Sharing a class is the design system working.

        The defect is in the STYLESHEET -- one bare class claimed by two rule
        blocks written by two different intentions. Distance is what tells that
        apart from a deliberate second rule: a state variant or a split reset
        sits within a screen of the original (`.bar-button` twice, three lines
        apart). `.plan` was declared at 1411 and 2233, in two different
        sections of the file. Eight hundred lines is not a second thought about
        the same component; it is a second component.

        `@media` and `@supports` blocks are excluded outright. An override
        there is the entire point of writing one, and every responsive rule in
        this file lives at the bottom -- including them made the check flag
        nineteen innocent classes and nothing else.
        """
        import pathlib
        import re as _re

        import dashboard

        css = (
            pathlib.Path(dashboard.__file__).parent / "static" / "style.css"
        ).read_text(encoding="utf-8")

        # Blank out at-rule blocks, keeping line numbers intact.
        kept, depth, inside = [], 0, False
        for line in css.splitlines():
            if not inside and _re.match(r"\s*@(media|supports)", line):
                inside, depth = True, line.count("{") - line.count("}")
                kept.append("")
                continue
            if inside:
                depth += line.count("{") - line.count("}")
                kept.append("")
                if depth <= 0:
                    inside = False
                continue
            kept.append(line)

        spots = {}
        for number, line in enumerate(kept, 1):
            for match in _re.finditer(
                r"(?:^|,)\s*\.([a-z][a-z0-9-]*)\s*(?:\{|,\s*$)", line
            ):
                spots.setdefault(match.group(1), []).append(number)

        # A screen or two apart is a second thought about one component.
        # Further than that is two components wearing one name.
        far = {
            name: lines
            for name, lines in spots.items()
            if len(lines) > 1 and max(lines) - min(lines) > 100
        }
        assert not far, (
            "a bare class claimed from two ends of the stylesheet -- one word, "
            f"two meanings is how #158 happened: {far}"
        )

    def test_no_template_puts_a_form_inside_a_paragraph(self):
        """A <form> is block-level, so an HTML parser CLOSES an open <p> when
        it meets one -- the form is hoisted out and lands on its own line, and
        no stylesheet can put it back. Nothing errors; the browser repairs the
        markup silently.

        This cost a measuring session: `.actions` computed `display: flex`,
        had 700px of room to spare, and the button still wrapped. Pinned
        across every template rather than only the one that had it, because
        the mistake is invisible in the source and looks like a CSS bug.
        """
        import pathlib

        import dashboard

        folder = pathlib.Path(dashboard.__file__).parent / "templates"
        # `<p>` or `<p class=...`, never `<path>` -- base.html is full of
        # inline SVG and the first version of this check flagged every one of
        # them.
        opener = re.compile(r"<p[\s>]")
        for template in folder.glob("*.html"):
            # Jinja comments are stripped first: these files explain
            # themselves at length, and the comment warning about this very
            # mistake quotes the markup that makes it.
            markup = re.sub(
                r"\{#.*?#\}", "", template.read_text(encoding="utf-8"), flags=re.S
            )
            for match in opener.finditer(markup):
                paragraph = markup[match.start():].split("</p>", 1)[0]
                assert "<form" not in paragraph, (
                    f"{template.name}: a <form> inside a <p>. The parser will "
                    "close the paragraph before it -- use a <div>."
                )

    def test_the_entry_names_its_own_button(self, config, store):
        test_client = self._client(config, store)
        page = test_client.get(f"/guild/{GUILD_IN}").data.decode()
        slot = page.split('class="panel group next-step"', 1)[1].split("</section>", 1)[0]
        assert _premium_entry().cta_label in slot
        assert "See plans and subscribe" not in slot


class TestTheChangelogPage:
    """The full list the bell summarises (issue #136 phase 3)."""

    def test_it_lists_every_entry_in_full(self, client, store):
        """Bodies are NOT clamped here. This page is where the panel's
        two-line previews point, so clamping again would leave the text with
        nowhere to be read."""
        login_as(client, store)
        page = client.get("/updates").data.decode()
        for item in changelog.ENTRIES:
            assert item.title in page
            assert item.body in page

    def test_it_is_signed_in_only(self, client):
        """The public version is #137's job, which is what the `public` flag
        on the model exists for."""
        response = client.get("/updates")
        assert response.status_code == 302
        assert response.headers["Location"] == "/"

    def test_it_links_out_to_the_public_changelog(self, client, store):
        """#137 phase 5, and the link goes this way round deliberately.

        The bell still points HERE rather than at the apex site: this page is
        the only surface allowed to render a `public=False` entry, and sending
        a signed-in admin off the property to read their own product's history
        is worse than keeping them on it. What the public page offers an admin
        is a URL that works signed out -- something to share.
        """
        login_as(client, store)
        page = client.get("/updates").data.decode()
        assert "https://vrcverify.com/changelog" in page

    def test_the_bell_still_points_at_this_page_not_the_public_one(self, client, store):
        """The decision above, pinned from the other side.

        #137's scope originally said to repoint the bell at the public
        changelog. It was written before #136 phase 3 existed and following it
        would have been a regression, so this fails if somebody later works
        from that instruction.
        """
        login_as(client, store)
        page = client.get("/").data.decode()
        bell = page.split('<details class="bell', 1)[1].split("</details>", 1)[0]
        assert 'href="/updates"' in bell
        assert "vrcverify.com/changelog" not in bell

    def test_the_follow_row_is_absent_without_an_invite(self, client, store):
        """#138. Absent rather than disabled: a dead row on the page whose job
        is credibility is worse than no row, and the two hosts deploy
        separately, so the dashboard will run without the value for a while."""
        login_as(client, store)
        page = client.get("/updates").data.decode()
        assert "changelog-follow" not in page

    def test_the_follow_row_appears_once_configured(self, client, store, app):
        login_as(client, store)
        app.config["DASHBOARD"] = dataclasses.replace(
            app.config["DASHBOARD"], support_invite_url="https://discord.gg/abc"
        )
        page = client.get("/updates").data.decode()
        assert "changelog-follow" in page
        assert "https://discord.gg/abc" in page

    def test_a_schemeless_invite_renders_no_row(self, client, store, app):
        """A schemeless href resolves RELATIVE TO THIS SITE, so the row would
        look like an ordinary link and 404 on our own domain instead of
        reaching Discord."""
        login_as(client, store)
        app.config["DASHBOARD"] = dataclasses.replace(
            app.config["DASHBOARD"], support_invite_url="discord.gg/abc"
        )
        page = client.get("/updates").data.decode()
        assert "changelog-follow" not in page

    def test_the_bell_carries_it_too(self, client, store, app):
        """The slot #136 reserved, filled -- between "See all updates" and the
        mark-as-read form, which is the order that comment specified."""
        app.config["DASHBOARD"] = dataclasses.replace(
            app.config["DASHBOARD"], support_invite_url="https://discord.gg/abc"
        )
        login_as(client, store)
        page = client.get("/").data.decode()
        bell = page.split('<details class="bell', 1)[1].split("</details>", 1)[0]
        assert "https://discord.gg/abc" in bell
        assert bell.index('href="/updates"') < bell.index("https://discord.gg/abc")

    def test_the_bell_has_no_dead_row_without_an_invite(self, client, store):
        login_as(client, store)
        page = client.get("/").data.decode()
        bell = page.split('<details class="bell', 1)[1].split("</details>", 1)[0]
        assert "discord.gg" not in bell

    def test_it_has_no_sidebar(self, client, store):
        """It belongs to no server. The sidebar navigates WITHIN one, so
        rendering it here would make a global page look like a property of
        whichever server happened to be open."""
        login_as(client, store)
        page = client.get("/updates").data.decode()
        assert '<nav class="sidebar"' not in page

    def test_no_entry_body_reaches_the_page_as_markup(self, monkeypatch, client, store):
        """The acceptance criterion, tested at the END of the path.

        `validate_entries` stops markup getting into the constant, and
        test_changelog.py pins that. This is the other end: even if something
        did, the template must render it as text. Asserting "the shipped
        entries contain no angle brackets" would pass without proving
        anything about the template at all, which is why an entry carrying
        markup is put through a real render here.
        """
        hostile = changelog.Entry(
            id="2026-09-hostile",
            date=date(2026, 9, 1),
            title="<script>alert(1)</script>",
            body="<img src=x onerror=alert(1)> & <b>bold</b>",
        )
        monkeypatch.setattr(changelog, "ENTRIES", (hostile,))
        login_as(client, store)
        page = client.get("/updates").data.decode()
        listing = page.split('<ol class="changelog">', 1)[1]
        assert "<script>" not in listing
        assert "<img" not in listing
        assert "&lt;script&gt;" in listing
        assert "&amp;" in listing

    def test_a_premium_entry_never_wears_the_lock_badge(self, client, store):
        """Same collision as the bell: this page is reachable from a premium
        server, and there is no guild in context to be plan-specific about."""
        login_as(client, store)
        page = client.get("/updates").data.decode()
        listing = page.split('<ol class="changelog">', 1)[1]
        assert "badge premium" not in listing
        assert "bell-tag premium" in listing

    def test_visiting_it_clears_the_dot(self, client, store):
        """The reader with no JavaScript is the one this matters for. prefs.js
        clears the dot when the panel opens; without it, following "See all
        updates" would leave the dot lit over a list just read in full."""
        login_as(client, store)
        response = client.get("/updates")
        cookie = set_cookie_header(response, "vrcverify_seen")
        assert changelog.ENTRIES[0].id in cookie
        assert "Secure" in cookie and "SameSite=Lax" in cookie
        assert "HttpOnly" not in cookie
        assert "bell-dot" not in client.get("/").data.decode()

    def test_its_own_bell_is_not_still_claiming_unread(self, client, store):
        """The response that clears the dot also renders the bell. The cookie
        it sets is not readable until the NEXT request, so without an override
        the header would announce unread entries at the top of the very page
        listing them in full."""
        login_as(client, store)
        assert "bell-dot" not in client.get("/updates").data.decode()

    def test_it_never_reaches_the_bot(self, config, store):
        test_client, api = settings_client(config, store)
        api.reads.clear()
        api.calls.clear()
        api.saves.clear()
        test_client.get("/updates")
        assert api.reads == [] and api.calls == [] and api.saves == []

    def test_the_bell_links_to_it(self, client, store):
        """What makes the panel's two-line clamp honest."""
        login_as(client, store)
        page = client.get("/").data.decode()
        bell = page.split('<details class="bell', 1)[1].split("</details>", 1)[0]
        assert 'href="/updates"' in bell

    def test_the_footer_links_to_it(self, client, store):
        """A page reachable only from inside a dropdown is a page most people
        never find."""
        login_as(client, store)
        footer = client.get("/").data.decode().split("<footer>", 1)[1]
        assert 'href="/updates"' in footer

    def test_the_footer_keeps_the_legal_links_absolute(self, client, store):
        """They live on the apex site, which is a separate failure domain on
        purpose -- Terms and Privacy have to resolve when this app is down."""
        login_as(client, store)
        footer = client.get("/").data.decode().split("<footer>", 1)[1]
        for path in ("terms", "privacy", "refunds"):
            assert f'href="https://vrcverify.com/{path}"' in footer

    def test_the_footer_offers_no_signed_in_page_to_a_stranger(self, client):
        """The sign-in page renders this footer too, and /updates would just
        bounce them back."""
        footer = client.get("/").data.decode().split("<footer>", 1)[1]
        assert 'href="/updates"' not in footer
        assert 'href="https://vrcverify.com/terms"' in footer

    def test_the_theme_picker_comes_back_here(self, client, store):
        """`whats_new` had to join NAV_RETURN_ENDPOINTS, or switching theme on
        this page would drop the reader on the picker."""
        login_as(client, store)
        client.get("/updates")
        response = client.post(
            "/prefs/theme", data={"theme": "light", "return_to": "whats_new"}
        )
        assert response.headers["Location"] == "/updates"


class TestPlanBadgesMirrorTheBot:
    """The site must be neither stricter nor looser than the slash commands.

    THESE ASSERTIONS READ `<main>`, NOT THE WHOLE DOCUMENT. They used to grep
    the response body, which worked only while the settings form was the sole
    thing on the page that could say "Premium" -- #136's bell put a second,
    unrelated use of the word in the header and broke the proxy.

    The subject was always the FIELDS. `_form` makes that explicit rather than
    leaving the next feature to trip over the same shortcut, and it is the
    reason the bell had to stop using `.badge.premium`: one chip meaning "your
    plan cannot use this" beside a form and "this exists" in a dropdown on the
    same page is a badge that means nothing.
    """

    @staticmethod
    def _form(page: str) -> str:
        """Everything inside <main> -- the settings forms and nothing else.

        The header (bell, theme picker, account menu) and the footer are
        chrome, and no assertion in this class is about them.

        EVERY <main>, not the first: `every_settings_page` joins one response
        per group (#140), and taking only the first would have quietly narrowed
        these assertions to the Verification page -- where the fields they are
        about do not live.
        """
        mains = re.findall(r"<main>(.*?)</main>", page, re.S)
        assert mains, "no <main> on the page"
        return "".join(mains)

    def test_write_locked_fields_are_marked_premium(self, config, store):
        test_client, _api = settings_client(config, store)
        page = self._form(every_settings_page(test_client))
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
        page = self._form(every_settings_page(test_client))
        # The values are shown, not hidden or replaced with an upsell.
        assert "Unverified" in page
        assert "Welcome aboard!" in page
        assert "Not applied</span>" in page
        assert "Saved, but not acted on without Premium" in page

    def test_a_premium_server_sees_no_badges(self, config, store):
        test_client, _api = settings_client(
            config, store, settings=make_settings(premium=True)
        )
        page = self._form(every_settings_page(test_client))
        assert "Premium</span>" not in page
        assert "Not applied</span>" not in page
        assert "VRCVerify Premium is active" in page

    def test_auto_verify_is_never_gated(self, config, store):
        """Free for everyone, forever -- mirrors TestAutoVerifyOnJoinIsFree."""
        test_client, _api = settings_client(config, store)
        page = every_settings_page(test_client)
        section = page.split("Auto-verify on join")[1].split("</div>")[0]
        assert "badge" not in section

    def test_a_missing_auto_verify_column_is_declared(self, config, store):
        test_client, _api = settings_client(
            config, store, settings=make_settings(auto_verify_column=False)
        )
        page = every_settings_page(test_client)
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
        page = settings_page(test_client).data.decode()
        assert "Upgrade to VRCVerify Premium" in page
        assert f"/guild/{GUILD_IN}/subscription" in page

    def test_settings_no_longer_sells_anything_itself(self, config, store):
        """The pitch lives in exactly one place now.

        Both halves matter: no purchase instructions here, and no store link
        either -- otherwise the page that cannot mention the longer plans is
        still the one an admin reads about buying.
        """
        test_client, _api = settings_client(config, store)
        page = settings_page(test_client).data.decode()
        assert "/vrcverify_subscription" not in page
        assert "application-directory" not in page
        assert "you choose the server during checkout" not in page

    def test_a_subscribed_server_is_not_sold_to(self, config, store):
        test_client, _api = settings_client(
            config, store, settings=make_settings(premium=True)
        )
        page = settings_page(test_client).data.decode()
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
        page = settings_page(test_client).data.decode()
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
        page = settings_page(test_client).data.decode()
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
        page = settings_page(test_client).data.decode()
        assert "Verified role" in page  # the settings page really rendered
        assert "application-directory" not in page
        assert "store/None" not in page

    def test_the_stale_read_only_notice_is_gone(self, config, store):
        """Every group saves now. The page said otherwise for a while, which
        sent admins to the slash commands past a working Save button."""
        test_client, _api = settings_client(config, store)
        page = settings_page(test_client).data.decode()
        assert "Verified role" in page  # the settings page really rendered
        assert "Only the instructions panel settings can be changed" not in page


class TestSettingsWarnings:
    """The point of the dashboard: say it now, not at verification time."""

    def test_an_unassignable_verified_role_is_called_out(self, config, store):
        test_client, _api = settings_client(
            config, store, settings=make_settings(values={"role_id": UNASSIGNABLE_ROLE})
        )
        page = every_settings_page(test_client)
        assert "cannot grant this role" in page
        assert "Server Settings -&gt; Roles" in page

    def test_a_deleted_role_is_called_out(self, config, store):
        test_client, _api = settings_client(
            config, store, settings=make_settings(values={"role_id": "404404404404"})
        )
        page = every_settings_page(test_client)
        assert "no longer exists" in page

    def test_no_verified_role_is_called_out(self, config, store):
        test_client, _api = settings_client(
            config, store, settings=make_settings(values={"role_id": None})
        )
        page = every_settings_page(test_client)
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
        page = every_settings_page(test_client)
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
        page = every_settings_page(test_client)
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
        page = every_settings_page(test_client)
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
        page = every_settings_page(test_client)
        assert f'<option value="{PANEL_CHANNEL}"' in page
        assert f'<option value="{SHUT_CHANNEL}"' not in page


class TestSecondaryReadsDegradeGracefully:
    """A name lookup failing must not cost the whole page."""

    def test_the_page_renders_without_roles(self, config, store):
        test_client, _api = settings_client(
            config, store, errors={"roles": BotAPIError("unavailable", 503)}
        )
        response = settings_page(test_client)
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
        page = settings_page(test_client).data.decode()
        assert 'name="role_id"' not in page
        assert 'name="unverified_role_id"' not in page

    def test_an_unresolved_id_is_not_reported_as_deleted(self, config, store):
        """"We could not check" and "it is gone" are different claims."""
        test_client, _api = settings_client(
            config, store, errors={"roles": BotAPIError("unavailable", 503)}
        )
        page = settings_page(test_client).data.decode()
        assert "no longer exists" not in page

    def test_the_page_renders_without_the_audit_read(self, config, store):
        """An empty history and an unavailable one are different facts."""
        test_client, _api = settings_client(
            config, store, errors={"audit": BotAPIError("unavailable", 503)}
        )
        response = settings_page(test_client, "activity")
        assert response.status_code == 200
        page = response.data.decode()
        assert "Couldn't load the history" in page
        assert "No changes have been made" not in page

    def test_the_page_renders_without_the_panel_read(self, config, store):
        test_client, _api = settings_client(
            config, store, errors={"panel": BotAPIError("unavailable", 503)}
        )
        response = settings_page(test_client, "panel")
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
        # redirect target carries no flags -- and it names the group the save
        # came from (#140), so "Saved." appears on the page the admin was on.
        assert response.headers["Location"].endswith(f"/guild/{GUILD_IN}/settings/panel")
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


class TestTheGroupSlugs:
    """#140 phase 1. Nothing here is visible yet -- that is the point.

    The slugs exist before the routes that will serve them so that the routes,
    the sidebar sub-nav and `build_groups()` all read one table instead of each
    keeping a list. A sub-nav that names a slug no route serves is a link to a
    404, and two hand-maintained lists is how that happens.
    """

    def test_every_group_carries_its_slug_in_table_order(self):
        groups = settings_view.build_groups({}, [], [], None)
        assert [g["slug"] for g in groups] == list(settings_view.SETTINGS_SLUGS)

    def test_no_group_is_missing_one(self):
        """A group without a slug is a group phase 2 cannot route to."""
        groups = settings_view.build_groups({}, [], [], None)
        assert all(g.get("slug") for g in groups)

    def test_the_slugs_survive_being_put_in_a_url(self):
        """They become a bookmarkable contract the moment phase 2 ships them."""
        for slug in settings_view.SETTINGS_SLUGS + (settings_view.ACTIVITY_SLUG,):
            assert re.fullmatch(r"[a-z][a-z-]*[a-z]", slug), slug

    def test_activity_is_not_one_of_the_groups(self):
        """The audit log is not returned by build_groups() and never was."""
        assert settings_view.ACTIVITY_SLUG not in settings_view.SETTINGS_SLUGS
        groups = settings_view.build_groups({}, [], [], None)
        assert settings_view.ACTIVITY_SLUG not in {g["slug"] for g in groups}

    def test_the_default_is_the_first_group(self):
        """Where the bare /settings URL in old Discord buttons will land."""
        assert (
            settings_view.SETTINGS_DEFAULT_SLUG == settings_view.SETTINGS_SLUGS[0]
        )

    def test_a_slug_the_table_does_not_know_is_refused(self, app):
        """Loud in a test run beats a redirect to a 404 in phase 2."""
        with app.test_request_context():
            with pytest.raises(ValueError):
                app_module._settings_url(GUILD_IN, "verifikation")

    def test_the_save_path_returns_to_the_group_it_came_from(self, app):
        """The line phase 1 promised: the right notice on the right page."""
        with app.test_request_context():
            for slug in settings_view.SETTINGS_SLUGS:
                assert app_module._settings_url(GUILD_IN, slug) == (
                    f"/guild/{GUILD_IN}/settings/{slug}"
                )

    # Every POST that returns to Settings passes a literal slug, so a typo in
    # one is only found by running it. These exercise the exit that is easiest
    # to get wrong -- the early guard that looks like a no-op -- for all five
    # save routes, plus both action routes. The success exits share the same
    # `group` variable and are covered by the save tests above.
    @pytest.mark.parametrize(
        "route, slug",
        [
            ("verification", "verification"),
            ("member", "after-verifying"),
            ("logging", "logging"),
            ("panel", "panel"),
            ("group", "vrchat-group"),
        ],
    )
    def test_a_save_with_nothing_to_save_returns_to_its_own_group(
        self, config, store, route, slug
    ):
        api = FakeBotAPI()
        application = create_app(config, store=store, client=api)
        application.config.update(TESTING=True)
        test_client = application.test_client()
        session = login_as(test_client, store)
        response = test_client.post(
            f"/guild/{GUILD_IN}/{route}", data={"csrf_token": session.csrf_token}
        )
        assert response.status_code == 302
        assert response.headers["Location"].endswith(
            f"/guild/{GUILD_IN}/settings/{slug}"
        )
        assert api.saves == []

    def test_the_panel_post_with_no_channel_redirects_without_raising(
        self, config, store
    ):
        api = FakeBotAPI()
        application = create_app(config, store=store, client=api)
        application.config.update(TESTING=True)
        test_client = application.test_client()
        session = login_as(test_client, store)
        response = test_client.post(
            f"/guild/{GUILD_IN}/panel/post",
            data={"csrf_token": session.csrf_token, "panel_channel_id": ""},
        )
        assert response.status_code == 302
        assert response.headers["Location"].endswith(
            f"/guild/{GUILD_IN}/settings/panel"
        )


class TestTheSettingsSubNav:
    """#140 phase 3. The sidebar's second level."""

    @staticmethod
    def _sidebar(page: str) -> str:
        start = page.index('<nav class="sidebar"')
        return page[start : page.index("</nav>", start)]

    def test_it_lists_every_group_and_activity(self, config, store):
        test_client, _api = settings_client(config, store)
        nav = self._sidebar(settings_page(test_client).data.decode())
        for slug, label in settings_view.SETTINGS_GROUPS:
            assert f'href="/guild/{GUILD_IN}/settings/{slug}"' in nav, slug
            assert label in nav, label
        assert f'href="/guild/{GUILD_IN}/settings/activity"' in nav
        assert settings_view.ACTIVITY_TITLE in nav

    def test_the_nav_cannot_offer_a_page_that_does_not_exist(self, config, store):
        """The reason it is built from the table rather than written out.

        A nav that disagrees with the routes is a link to a 404, so every
        target it renders is fetched.
        """
        test_client, _api = settings_client(config, store)
        nav = self._sidebar(settings_page(test_client).data.decode())
        targets = set(
            re.findall(rf'href="(/guild/{GUILD_IN}/settings/[a-z-]+)"', nav)
        )
        # The five groups, Activity, and the section link itself -- which
        # points at the first group rather than at a page of its own.
        assert len(targets) == len(settings_view.SETTINGS_SLUGS) + 1
        for target in targets:
            assert test_client.get(target).status_code == 200, target

    def test_the_labels_are_the_headings(self, config, store):
        """One table, so renaming a group renames its nav entry by the same
        edit. Two lists is how a nav starts describing a page it no longer
        matches."""
        assert settings_view.SETTINGS_TITLES == {
            group["slug"]: group["title"]
            for group in settings_view.build_groups({}, [], [], None)
        }

    def test_the_open_group_is_marked_current(self, config, store):
        test_client, _api = settings_client(config, store)
        nav = self._sidebar(settings_page(test_client, "logging").data.decode())
        current = re.findall(r'href="([^"]+)"[^>]*aria-current="page"', nav)
        assert current == [f"/guild/{GUILD_IN}/settings/logging"]

    def test_activity_can_be_the_current_one(self, config, store):
        test_client, _api = settings_client(config, store)
        nav = self._sidebar(settings_page(test_client, "activity").data.decode())
        assert (
            f'href="/guild/{GUILD_IN}/settings/activity"' in nav
            and 'aria-current="page"' in nav
        )

    def test_the_section_link_is_not_marked_as_the_page(self, config, store):
        """It points at the first group, so calling it "page" names the wrong
        one the moment you are on any of the other five."""
        test_client, _api = settings_client(config, store)
        nav = self._sidebar(settings_page(test_client, "logging").data.decode())
        section = re.search(r'<a[^>]*class="side-link[^"]*"[^>]*>', nav).group(0)
        assert "aria-current" not in section
        # The bare URL is a redirect, not a destination; nothing links to it.
        assert f'href="/guild/{GUILD_IN}/settings"' not in nav

    def test_it_shows_inside_settings_and_nowhere_else(self, config, store):
        """No cookie and no open state: the list is rendered when you are in
        the section and not otherwise, so it cannot disagree with the page."""
        test_client, _api = settings_client(config, store)
        assert 'class="side-sub"' in settings_page(test_client).data.decode()
        for path in (f"/guild/{GUILD_IN}", f"/guild/{GUILD_IN}/subscription"):
            assert 'class="side-sub"' not in test_client.get(path).data.decode(), path

    def test_it_needs_no_javascript(self, config, store):
        """`<details>` opens by itself, which is why this is a disclosure and
        not a menu."""
        test_client, _api = settings_client(config, store)
        nav = self._sidebar(settings_page(test_client).data.decode())
        assert "<script" not in nav
        assert "onclick" not in nav

    def test_it_is_not_dressed_as_a_popover(self, config, store):
        """prefs.js closes every `details.bar-menu` on outside-click and on
        Escape, and opening one closes the rest. Right for the bell, the theme
        picker and the account menu; wrong for a nav section, which would shut
        itself the moment the reader clicked the form beside it."""
        test_client, _api = settings_client(config, store)
        nav = self._sidebar(settings_page(test_client).data.decode())
        assert "bar-menu" not in nav
        assert "<details" not in nav

    def test_settings_is_one_tap_from_anywhere(self, config, store):
        """The section link goes straight to a page rather than expanding.

        A disclosure would have cost a tap from every other section, which is
        half of why this is a list rather than the `<details>` #140 specified.
        """
        test_client, _api = settings_client(config, store)
        for path in (f"/guild/{GUILD_IN}", f"/guild/{GUILD_IN}/subscription"):
            nav = self._sidebar(test_client.get(path).data.decode())
            assert f'href="/guild/{GUILD_IN}/settings/verification"' in nav, path

    def test_no_group_is_marked_current_outside_settings(self, config, store):
        test_client, _api = settings_client(config, store)
        nav = self._sidebar(test_client.get(f"/guild/{GUILD_IN}").data.decode())
        assert 'class="side-sub-link current"' not in nav


class TestSettingsIsAPagePerGroup:
    """#140 phase 2. The split itself, and the links that had to move with it."""

    def logged_in(self, config, store, **kwargs):
        test_client, _api = settings_client(config, store, **kwargs)
        return test_client

    # ----- the external contract -----
    def test_the_bare_url_still_resolves(self, config, store):
        """Discord link buttons in message history cannot be edited.

        The bot posts /guild/<id>/settings from /vrcverify_setup and the
        summary commands. Every one already sent has to keep working, so this
        is the one URL in the app that may never simply stop.
        """
        test_client = self.logged_in(config, store)
        response = test_client.get(f"/guild/{GUILD_IN}/settings")
        assert response.status_code == 302
        assert response.headers["Location"].endswith(
            f"/guild/{GUILD_IN}/settings/{settings_view.SETTINGS_DEFAULT_SLUG}"
        )

    def test_the_bare_url_lands_on_a_real_page(self, config, store):
        """A redirect to a 404 would satisfy the letter and not the promise."""
        test_client = self.logged_in(config, store)
        landed = test_client.get(f"/guild/{GUILD_IN}/settings", follow_redirects=True)
        assert landed.status_code == 200
        assert b"Verification" in landed.data

    def test_a_group_nobody_serves_is_a_404(self, config, store):
        """Typed or guessed, since the nav is built from the same table."""
        test_client = self.logged_in(config, store)
        assert test_client.get(f"/guild/{GUILD_IN}/settings/verifikation").status_code == 404

    # ----- one table, two readers -----
    def test_every_slug_has_a_page_and_every_page_has_a_slug(self, config, store):
        """The reads table and the slug table cannot drift apart.

        A group missing from one is unreachable; an entry with no group is a
        URL the sub-nav can never offer. Either is the 404 the single table
        exists to prevent.
        """
        assert set(app_module.SETTINGS_GROUP_READS) == set(
            settings_view.SETTINGS_SLUGS
        ) | {settings_view.ACTIVITY_SLUG}

    @pytest.mark.parametrize("group", SETTINGS_GROUPS + ("activity",))
    def test_every_group_renders(self, config, store, group):
        test_client = self.logged_in(config, store)
        assert settings_page(test_client, group).status_code == 200

    # ----- one form per page, where that is possible -----
    def test_the_three_single_form_groups_carry_exactly_one_save(
        self, config, store
    ):
        """The hazard `app.js` exists for, removed rather than mitigated.

        Edit one group, save a different one, and the first group's edits are
        discarded with no error. On these three pages there is no second form
        to submit by mistake.
        """
        test_client = self.logged_in(
            config, store, settings=make_settings(premium=True)
        )
        for group in ("verification", "after-verifying", "logging"):
            page = settings_page(test_client, group).data.decode()
            main = re.search(r"<main>(.*?)</main>", page, re.S).group(1)
            assert main.count("<form") == 1, group

    def test_the_two_action_groups_still_carry_their_second_form(
        self, config, store
    ):
        """And the issue should not pretend otherwise.

        `post_panel` and `verify_group` post to different endpoints, and a form
        inside a form is not a thing, so those two pages keep two forms and
        keep the hazard. The copy that warns about it has to stay.
        """
        # A group has to be configured for its check button to exist at all --
        # there is nothing to check otherwise, and that is the state this test
        # is about.
        test_client = self.logged_in(
            config,
            store,
            settings=make_settings(
                premium=True, values={"vrchat_group_id": GROUP_ID}
            ),
        )
        # Each says it in its own words, and both must survive the split --
        # this copy is the only thing standing between the reader and the
        # hazard on these two pages.
        # The panel's wording depends on whether one is already posted, so it
        # is matched rather than quoted; the group check has one sentence.
        warning = {
            "panel": r"the settings (saved )?above",
            "vrchat-group": r"doesn't save the settings above",
        }
        for group in ("panel", "vrchat-group"):
            page = settings_page(test_client, group).data.decode()
            main = re.search(r"<main>(.*?)</main>", page, re.S).group(1)
            assert main.count("<form") == 2, group
            assert re.search(warning[group], main), group

    # ----- a group shows its own fields and no others -----
    @pytest.mark.parametrize(
        "group, mine, not_mine",
        [
            ("verification", 'name="role_id"', 'name="verification_log_channel_id"'),
            ("logging", 'name="verification_log_channel_id"', 'name="role_id"'),
            ("panel", 'name="instructions_locale"', 'name="role_id"'),
        ],
    )
    def test_a_group_renders_its_own_fields_only(
        self, config, store, group, mine, not_mine
    ):
        test_client = self.logged_in(
            config, store, settings=make_settings(premium=True)
        )
        page = settings_page(test_client, group).data.decode()
        assert mine in page
        assert not_mine not in page

    def test_activity_carries_the_history_and_no_settings_form(
        self, config, store
    ):
        """It was never one of the groups -- `build_groups()` does not return it."""
        test_client = self.logged_in(config, store, audit=AUDIT_ENTRIES)
        page = settings_page(test_client, "activity").data.decode()
        assert "Activity" in page
        main = re.search(r"<main>(.*?)</main>", page, re.S).group(1)
        assert "<form" not in main

    def test_every_group_page_can_reach_the_history(self, config, store):
        """It used to be at the bottom of this page, so it needs a door.

        Phase 2 spent a line at the foot of each group on this; the sub-nav
        (phase 3) took the job over and that line is gone.
        """
        test_client = self.logged_in(config, store)
        target = f'href="/guild/{GUILD_IN}/settings/activity"'
        for group in SETTINGS_GROUPS:
            assert target in settings_page(test_client, group).data.decode(), group

    def test_no_group_page_renders_the_history_itself(self, config, store):
        """It used to be read on every page load and shown at the bottom.

        The list, not the word: every group links to Activity by name, so
        "Recent changes" appears on all of them. What must not appear is the
        trail.
        """
        test_client = self.logged_in(config, store, audit=AUDIT_ENTRIES)
        for group in SETTINGS_GROUPS:
            page = settings_page(test_client, group).data.decode()
            assert 'class="audit"' not in page, group
            assert "Preview Admin" not in page, group

    # ----- the preference forms come back to the right page -----
    def _prefs(self, test_client, session, group=None, path="/prefs/theme"):
        data = {
            "csrf_token": session.csrf_token,
            "return_to": "guild_settings",
            "guild_id": GUILD_IN,
            "theme": "light",
        }
        if group is not None:
            data["group"] = group
        return test_client.post(path, data=data)

    def test_changing_theme_returns_to_the_group_you_were_on(self, config, store):
        """Otherwise the picker silently moves you to a different page."""
        test_client, _api = settings_client(config, store)
        session = login_as(test_client, store)
        response = self._prefs(test_client, session, "logging")
        assert response.headers["Location"].endswith(
            f"/guild/{GUILD_IN}/settings/logging"
        )

    def test_collapsing_the_sidebar_returns_to_the_group_you_were_on(
        self, config, store
    ):
        test_client, _api = settings_client(config, store)
        session = login_as(test_client, store)
        response = test_client.post(
            "/prefs/nav",
            data={
                "csrf_token": session.csrf_token,
                "collapsed": "1",
                "return_to": "guild_settings",
                "guild_id": GUILD_IN,
                "group": "vrchat-group",
            },
        )
        assert response.headers["Location"].endswith(
            f"/guild/{GUILD_IN}/settings/vrchat-group"
        )

    def test_activity_is_a_place_a_preference_form_can_return_to(
        self, config, store
    ):
        test_client, _api = settings_client(config, store)
        session = login_as(test_client, store)
        response = self._prefs(test_client, session, "activity")
        assert response.headers["Location"].endswith(
            f"/guild/{GUILD_IN}/settings/activity"
        )

    def test_a_form_carrying_no_group_still_lands_on_settings(
        self, config, store
    ):
        """A page cached before this shipped, and the cost of guessing wrong.

        Bouncing a reader out to the server list would be a worse answer than
        the first settings page, and the bare URL redirects there anyway.
        """
        test_client, _api = settings_client(config, store)
        session = login_as(test_client, store)
        response = self._prefs(test_client, session)
        assert response.headers["Location"].endswith(f"/guild/{GUILD_IN}/settings")

    def test_a_group_the_table_does_not_know_never_reaches_the_url(
        self, config, store
    ):
        """The rule this path states is that nothing from a form is
        interpolated into a redirect target. A slug is no more trustworthy for
        being short."""
        test_client, _api = settings_client(config, store)
        session = login_as(test_client, store)
        response = self._prefs(test_client, session, "../../evil")
        assert response.headers["Location"].endswith(f"/guild/{GUILD_IN}/settings")
        assert "evil" not in response.headers["Location"]

    def test_the_group_travels_in_the_page_the_forms_are_on(self, config, store):
        """The hidden field, without which none of the above can happen."""
        test_client, _api = settings_client(config, store)
        page = settings_page(test_client, "logging").data.decode()
        assert '<input type="hidden" name="group" value="logging">' in page

    def test_no_group_field_on_a_page_that_has_no_group(self, config, store):
        test_client, _api = settings_client(config, store)
        page = test_client.get(f"/guild/{GUILD_IN}").data.decode()
        assert 'name="group"' not in page


class TestTheFormMatchesWhatTheBotAccepts:
    """Controls appear only where the bot said it would take the value."""

    def test_a_premium_server_gets_all_three_controls(self, config, store):
        test_client, _api = settings_client(
            config, store, settings=make_settings(premium=True)
        )
        page = every_settings_page(test_client)
        assert 'name="instructions_locale"' in page
        assert 'name="panel_embed_color"' in page
        assert 'name="panel_show_icon"' in page
        assert "Save changes" in page

    def test_a_free_server_gets_the_language_control_only(self, config, store):
        """Branding is write-locked, so no control -- but the language is free
        and must stay editable."""
        test_client, _api = settings_client(config, store)
        page = every_settings_page(test_client)
        assert 'name="instructions_locale"' in page
        assert 'type="color"' not in page
        assert 'name="panel_show_icon"' not in page

    def test_a_field_the_bot_has_not_opened_gets_no_control(self, config, store):
        test_client, _api = settings_client(
            config, store, settings=make_settings(premium=True, writable=set())
        )
        page = every_settings_page(test_client)
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
        page = every_settings_page(test_client)
        # Scoped to the `instructions_locale` <select>, not to the whole page.
        #
        # Since #97 the header bar carries a language picker of its own, and it
        # offers all twelve -- correctly, because it is a different question.
        # This select is "which language should the bot speak to my members
        # in", and only the bot can answer which those are. The picker in the
        # bar is "which language should this website answer *me* in", and the
        # answer to that is which catalogues this image was built with. Two
        # lists, two sources, and an unscoped substring search cannot tell the
        # difference between them.
        select = re.search(
            r'<select[^>]*name="instructions_locale".*?</select>', page, re.S
        )
        assert select, "the locale select should be on the page"
        options = select.group(0)
        for code in LOCALES:
            assert f'value="{code}"' in options
        # Present in LOCALE_NAMES, absent from what the bot offered.
        assert 'value="pa-IN"' not in options

    def test_announcement_channels_are_not_offered_at_all(self, config, store):
        """Unlike an unassignable role, the bot refuses these outright.

        So leaving them out of the picker is matching the bot rather than being
        stricter than it -- the opposite call from the role list, for the
        opposite reason.
        """
        test_client, _api = settings_client(
            config, store, settings=make_settings(premium=True)
        )
        page = every_settings_page(test_client)
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
        page = every_settings_page(test_client)
        panel_form = page.split('class="panel-post"')[1]
        assert f'value="{NEWS_CHANNEL}"' in panel_form

    def test_a_channel_picker_with_nothing_to_pick_is_not_offered(self, config, store):
        test_client, _api = settings_client(
            config,
            store,
            settings=make_settings(premium=True),
            errors={"channels": BotAPIError("unavailable", 503)},
        )
        page = every_settings_page(test_client)
        assert 'name="verification_log_channel_id"' not in page

    def test_the_custom_message_textarea_carries_the_bot_s_cap(self, config, store):
        test_client, _api = settings_client(
            config, store, settings=make_settings(premium=True)
        )
        page = every_settings_page(test_client)
        assert 'maxlength="1000"' in page

    # The theme picker and the language picker are the only forms in the app
    # with no CSRF token, and both exceptions are deliberate: they post to
    # /prefs/theme and /prefs/lang, which are reachable signed out -- the
    # sign-in page carries both controls and has no token to give either.
    #
    # The language picker's case is the stronger of the two: #97 exists for
    # people who cannot read English, and a picker reachable only after
    # navigating a page you cannot read is most of the way to not having one.
    #
    # Named here rather than subtracted silently, so a *third* tokenless form
    # still fails this test.
    CSRF_EXEMPT_FORMS = 2
    CSRF_EXEMPT_ACTIONS = {"/prefs/theme", "/prefs/lang"}

    def test_every_form_carries_a_csrf_token(self, config, store):
        test_client, _api = settings_client(
            config, store, settings=make_settings(premium=True)
        )
        # Per page, not over the five joined: the exemption is one form of
        # chrome, and every group's page carries its own copy of it.
        for group in SETTINGS_GROUPS:
            page = settings_page(test_client, group).data.decode()
            assert (
                page.count('name="csrf_token"')
                >= page.count("<form") - self.CSRF_EXEMPT_FORMS
            ), group

    def test_the_only_tokenless_forms_are_the_two_prefs_pickers(self, config, store):
        """Pins *which* forms the exemption above is spending itself on.

        Without this, the allowance is a hole any future form could fall into
        by accident -- the count would still pass and nobody would look.
        """
        test_client, _api = settings_client(
            config, store, settings=make_settings(premium=True)
        )
        for group in SETTINGS_GROUPS:
            page = settings_page(test_client, group).data.decode()
            tokenless = [
                form
                for form in re.findall(r"<form\b.*?</form>", page, re.S)
                if 'name="csrf_token"' not in form
            ]
            assert len(tokenless) == self.CSRF_EXEMPT_FORMS, group
            actions = {
                re.search(r'action="([^"]+)"', form).group(1) for form in tokenless
            }
            assert actions == self.CSRF_EXEMPT_ACTIONS, group


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

        for path in ("/", f"/guild/{GUILD_IN}", f"/guild/{GUILD_IN}/settings/verification"):
            assert test_client.get(path).headers["Cache-Control"] == "no-store"

        for path in ("/static/style.css", "/static/app.js"):
            headers = test_client.get(path).headers
            assert "public" in headers["Cache-Control"]
            assert "immutable" in headers["Cache-Control"]

    def test_asset_urls_carry_a_content_digest(self, config, store):
        """Without this the year-long cache above would be reckless."""
        test_client, _api = settings_client(config, store)
        page = settings_page(test_client).data.decode()
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
        page = settings_page(test_client).data.decode()
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

        response = settings_page(test_client)
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
        seen = 0
        for path in ("/", f"/guild/{GUILD_IN}", f"/guild/{GUILD_IN}/settings/verification"):
            for script in markup(test_client.get(path).data).scripts:
                seen += 1
                assert script["body"].strip() == "", f"inline script on {path}"
                src = script["attrs"].get("src") or ""
                assert src.startswith("/static/"), f"non-local script on {path}"
        # Every assertion above is inside the loop, so a parser that stopped
        # finding <script> would turn a CSP test green rather than red.
        assert seen, "no scripts found at all -- this test proved nothing"

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
        scripts = markup(settings_page(test_client).data).scripts
        assert len(scripts) == len(self.EXPECTED_SCRIPTS)
        for script, expected in zip(scripts, self.EXPECTED_SCRIPTS):
            assert expected in script["attrs"].get("src", "")

    def test_no_script_is_inline(self, config, store):
        """`script-src 'self'` blocks inline script, so an inline block would
        not run -- but it would also not error anywhere a developer looks. The
        page should never contain one to begin with."""
        test_client, _api = settings_client(config, store)
        scripts = markup(settings_page(test_client).data).scripts
        assert scripts, "no scripts found at all -- this test proved nothing"
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
        response = settings_page(test_client)
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
        # Both settings pages that carry a swatch: the role colours are on
        # Verification, the panel colour on its own page (#140).
        for path in (
            "/",
            f"/guild/{GUILD_IN}/settings/verification",
            f"/guild/{GUILD_IN}/settings/panel",
        ):
            page = test_client.get(path).data
            assert b"style=" not in page, f"inline style on {path}"
        # And the swatches really are there to be got wrong -- one per page
        # now, so each is asserted where it actually renders.
        assert b'fill="#ff00ff"' in settings_page(test_client, "panel").data
        assert b'fill="#5865f2"' in settings_page(test_client, "verification").data

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
        parser.feed(settings_page(test_client).data.decode())

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
        page = settings_page(test_client, "activity").data.decode()
        assert "Verified role" in page
        assert "Sasha" in page
        # The id is resolved to the role's name, as on the settings above.
        assert "not set &rarr; Verified" in page or "not set → Verified" in page

    def test_an_actor_who_left_is_shown_by_id(self, config, store):
        test_client, _api = settings_client(config, store, audit=AUDIT_ENTRIES)
        page = settings_page(test_client, "activity").data.decode()
        assert "ID 555555555555" in page

    def test_a_colour_reads_as_a_colour(self, config, store):
        test_client, _api = settings_client(config, store, audit=AUDIT_ENTRIES)
        assert "#ff0000" in settings_page(test_client, "activity").data.decode()

    def test_an_empty_history_says_so(self, config, store):
        test_client, _api = settings_client(config, store, audit=[])
        assert b"No changes have been made" in settings_page(
            test_client, "activity"
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

    def test_a_tile_leads_with_its_label_not_its_number(self, config, store):
        """#135 phase 5: the Featurebase/Framer shape reads label-then-value,
        the reverse of the original tile order -- pinned so a future edit
        cannot swap it back without noticing."""
        test_client, _api = settings_client(config, store)
        page = test_client.get(f"/guild/{GUILD_IN}").data.decode()
        one_tile = re.search(r'<li class="tile">.*?</li>', page, re.S)
        assert one_tile, "no fully-known tile on the default fixture"
        label_at = one_tile.group(0).index('class="tile-label"')
        value_at = one_tile.group(0).index('class="tile-value"')
        assert label_at < value_at


class TestTheChartOnThePage(object):
    """#135 phase 2. The rendered SVG, its offscreen table, and the two
    degraded states -- as opposed to TestTheChartGeometry, which checks the
    numbers without a request at all.
    """

    def _page(self, config, store, **overview_kwargs):
        test_client, _api = settings_client(
            config, store, overview=make_overview(**overview_kwargs)
        )
        return test_client.get(f"/guild/{GUILD_IN}").data.decode()

    def test_the_chart_renders_as_inline_svg(self, config, store):
        """No CDN, no <script>, no library -- CSP forbids both."""
        page = self._page(config, store)
        assert "<svg" in page
        assert re.search(r'<rect class="chart-bar[^"]*"', page)

    def test_no_style_attribute_appears_anywhere_in_it(self, config, store):
        """`style-src 'self'` drops an inline style="" SILENTLY -- no error,
        no console warning, the rule simply never applies. The chart is drawn
        entirely in x/y/width/height/fill attributes for exactly that reason."""
        page = self._page(config, store)
        chart = re.search(r'<div class="chart".*?</div>', page, re.S)
        assert chart
        assert 'style="' not in chart.group(0)

    def test_no_script_tag_or_handler_is_needed_to_draw_it(self, config, store):
        """It needs no JavaScript at all -- the numbers arrive already
        rendered, unlike the theme picker or the unsaved-changes indicator,
        which are enhancements on top of working markup."""
        page = self._page(config, store)
        chart = re.search(r'<div class="chart".*?</div>', page, re.S)
        assert chart
        assert "onclick" not in chart.group(0) and "<script" not in chart.group(0)

    def test_the_bars_use_presentation_attributes_not_hardcoded_colour(
        self, config, store
    ):
        """No hex value anywhere in the markup -- the whole point of
        `fill="currentColor"` plus a CSS class is that the chart is wrong in
        exactly one theme the moment somebody hardcodes a colour here."""
        page = self._page(config, store)
        chart = re.search(r'<div class="chart".*?</div>', page, re.S)
        assert chart
        assert not re.search(r"#[0-9a-fA-F]{3,8}", chart.group(0))
        assert 'fill="currentColor"' in chart.group(0)

    def test_zero_and_no_data_are_different_numbers_of_bars(self, config, store):
        """The acceptance criterion. A quiet day draws a <rect>; an unmeasured
        one draws nothing -- so the count of rects is the count of MEASURED
        days, not the count of days in the window."""
        daily = (
            [{"day": f"2026-07-{d:02d}", "count": None} for d in range(1, 11)]
            + [{"day": f"2026-07-{d:02d}", "count": 0} for d in range(11, 26)]
            + [{"day": f"2026-07-{d:02d}", "count": 3} for d in range(26, 31)]
        )
        page = self._page(config, store, daily=daily)
        chart = re.search(r'<div class="chart".*?</div>', page, re.S)
        assert chart, "no chart section rendered"
        rects = re.findall(r"<rect\b[^>]*>", chart.group(0))
        assert len(rects) == 20  # the 15 zero days + the 5 real days, not 30

    def test_the_offscreen_table_carries_every_day_including_gaps(
        self, config, store
    ):
        """The table is the accessible fallback, and it is not allowed to be
        thinner than the chart -- an unmeasured day is real information
        ("we don't know yet"), so it gets a row too, not a silent omission."""
        daily = [{"day": f"2026-07-{d:02d}", "count": None} for d in range(1, 6)] + [
            {"day": f"2026-07-{d:02d}", "count": d} for d in range(6, 31)
        ]
        page = self._page(config, store, daily=daily)
        table = re.search(r'<table class="offscreen">.*?</table>', page, re.S)
        assert table
        assert table.group(0).count("<tr>") == 31  # header row + 30 days
        assert table.group(0).count("Not measured") == 5

    def test_the_table_numbers_agree_with_the_bars(self, config, store):
        """Read together rather than independently -- a mismatch here would
        mean a sighted and a non-sighted reader learn different things from
        the same page."""
        page = self._page(
            config,
            store,
            daily=[{"day": "2026-07-30", "count": 7}] + [
                {"day": f"2026-07-{d:02d}", "count": 0} for d in range(1, 30)
            ],
        )
        assert "<td>7</td>" in page
        assert re.search(r'<rect class="chart-bar"[^>]*height="64', page)

    def test_a_failed_rollup_read_shows_neither_chart_nor_bad_table(
        self, config, store
    ):
        page = self._page(config, store, known=False)
        group = re.search(r'<h2>Verifications</h2>.*?</section>', page, re.S)
        assert group, "no Verifications section on the page"
        assert "<svg" not in group.group(0)
        assert "<table" not in group.group(0)
        assert "Couldn't load the daily trend" in group.group(0)

    def test_nothing_collected_yet_names_the_state_without_a_chart(
        self, config, store
    ):
        page = self._page(
            config,
            store,
            daily=[{"day": f"d{i}", "count": None} for i in range(30)],
            collecting_since=None,
        )
        group = re.search(r'<h2>Verifications</h2>.*?</section>', page, re.S)
        assert group, "no Verifications section on the page"
        assert "<svg" not in group.group(0)
        assert "not collecting" in group.group(0).lower()

    def test_the_chart_has_a_text_alternative_for_a_screen_reader(
        self, config, store
    ):
        """`role="img"` on the wrapper takes the SVG itself out of a screen
        reader's way; the aria-label and the offscreen table are what a
        non-sighted reader actually gets instead of a picture."""
        page = self._page(config, store)
        wrapper = re.search(r'<div class="chart" role="img"[^>]*>', page)
        assert wrapper and "aria-label=" in wrapper.group(0)
        assert '<svg viewBox="0 0 300 64" aria-hidden="true"' in page


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
        assert "Only counting since June 1, 2026" in page

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


class TestThePremiumPitchOnThePage:
    """#135 phase 4, and what #136 phase 4 did to it.

    THE DEMO IS NO LONGER THE DEFAULT OCCUPANT OF THIS SLOT. #135 wrote these
    against a feed that had no premium entries, so a configured free server
    fell straight through to the data-backed demo. There is one premium entry
    now, and it ranks ABOVE the demo -- which is the behaviour #135's own
    docstring specified and left with no caller.

    So each of these dismisses that entry to reach the demo. That is not a
    workaround: it is the real sequence an admin goes through, and testing the
    demo through it is what proves the fall-through works rather than the
    entry simply hiding it forever.
    """

    @staticmethod
    def _slot(page: str) -> str:
        """The next-step card alone.

        Necessary since #136 phase 2: the bell renders every entry's title and
        body in the header of every page, so "the entry is not on this page"
        is no longer the same claim as "the entry is not in the slot". A
        whole-page grep would now pass or fail for the wrong reason.
        """
        marker = '<section class="panel group next-step">'
        if marker not in page:
            return ""
        return page.split(marker, 1)[1].split("</section>", 1)[0]

    def _page(self, config, store, dismissed=True, **overview):
        test_client, _api = settings_client(
            config, store, overview=make_overview(**overview)
        )
        if dismissed:
            # The state after an admin has put the changelog card away for
            # this server. `add_dismissal` builds the value rather than a
            # literal, so a change to the cookie format cannot leave these
            # passing against a shape the app no longer writes.
            test_client.set_cookie(
                "vrcverify_dismissed",
                changelog.add_dismissal((), GUILD_IN, _premium_entry().id),
                domain="localhost",
            )
        return test_client.get(f"/guild/{GUILD_IN}").data.decode()

    def test_the_changelog_entry_outranks_the_demo(self, config, store):
        """The whole point of #136 phase 4. An undismissed premium entry takes
        the slot from a demo that would otherwise have it."""
        slot = self._slot(self._page(config, store, dismissed=False, last_30_days=214))
        assert _premium_entry().title in slot
        assert "214 members verified" not in slot

    def test_the_demo_returns_once_the_entry_is_dismissed(self, config, store):
        """The fall-through, and the reason dismissal is per-entry rather than
        "no more pitches on this page ever"."""
        page = self._page(config, store, last_30_days=214)
        assert "Upgrade to VRCVerify Premium" in page
        assert "214 members verified" in page

    def test_the_demo_links_to_subscriptions_not_settings(self, config, store):
        page = self._page(config, store, last_30_days=214)
        section = re.search(r'<h2>Upgrade to VRCVerify Premium</h2>.*?</section>', page, re.S)
        assert section
        assert f'href="/guild/{GUILD_IN}/subscription"' in section.group(0)
        assert "See plans and subscribe" in section.group(0)

    def test_a_premium_server_sees_no_pitch_at_all(self, config, store):
        """The demo is suppressed for a premium server, and the changelog
        entry is reframed rather than sold -- so neither of the two words a
        pitch would use appears."""
        page = self._page(config, store, dismissed=False, last_30_days=214, premium=True)
        assert "Upgrade to VRCVerify Premium" not in page
        assert "Add VRCVerify Premium" not in page
        assert "New in Premium" not in page
        assert "New in your plan" in page

    def test_a_grandfathered_server_sees_the_reassurance_first(self, config, store):
        page = self._page(config, store, last_30_days=214, grandfathered=True)
        assert "Add VRCVerify Premium" in page
        assert "grandfathered extras stay free" in page

    def test_a_grandfathered_server_is_reassured_by_the_entry_too(self, config, store):
        """The rule from #59 has to hold on whichever of the two is showing --
        a server that meets both over time must never read either as a
        threat to what it already has."""
        slot = self._slot(self._page(config, store, dismissed=False,
                                     last_30_days=214, grandfathered=True))
        assert "grandfathered extras stay free" in slot
        assert _premium_entry().title in slot

    def test_a_quiet_server_sees_no_pitch(self, config, store):
        page = self._page(config, store, last_30_days=0)
        assert "VRCVerify Premium" not in page

    def test_a_blank_window_sees_no_pitch(self, config, store):
        page = self._page(config, store, last_30_days=None)
        assert "VRCVerify Premium" not in page

    def test_a_broken_server_is_fixed_rather_than_sold_to(self, config, store):
        """Rank 1 is absolute. A server that cannot finish a verification sees
        the reason, not an announcement -- even an undismissed one."""
        slot = self._slot(self._page(
            config, store, dismissed=False, last_30_days=214,
            configured={"verified_role": False},
        ))
        assert "No verified role is set" in slot
        assert _premium_entry().title not in slot

    def test_only_ever_one_item_in_the_slot(self, config, store):
        page = self._page(config, store, dismissed=False, last_30_days=214)
        assert page.count('class="panel group next-step"') == 1


class TestTheSetupListOnThePage:
    """#135 phase 3. The merged Apollo-pattern list -- health and setup as one
    actioned list -- as opposed to TestOverviewViewModel, which checks the
    row data without a request."""

    def _page(self, config, store, **overview_kwargs):
        test_client, _api = settings_client(
            config, store, overview=make_overview(**overview_kwargs)
        )
        return test_client.get(f"/guild/{GUILD_IN}").data.decode()

    def _configured(self, **overrides):
        base = {
            "verified_role": True,
            "verified_role_exists": True,
            "verified_role_assignable": True,
            "unverified_role": False,
            "log_channel": False,
            "auto_verify": True,
        }
        base.update(overrides)
        return base

    def test_every_row_label_appears(self, config, store):
        page = self._page(config, store, panel={"posted": True})
        for label in (
            "Verified role", "Instructions panel", "Auto-verify on join",
            "Unverified role", "Verification log",
        ):
            assert label in page

    def test_a_broken_role_shows_its_own_note_and_a_settings_link(self, config, store):
        page = self._page(
            config, store,
            configured=self._configured(verified_role_exists=False,
                                         verified_role_assignable=None),
            panel={"posted": True},
        )
        assert "has been deleted" in page
        section = re.search(r'<h2>Setup</h2>.*?</section>', page, re.S)
        assert section
        assert (
            f'href="/guild/{GUILD_IN}/settings/verification#f-role_id"'
            in section.group(0)
        )

    def test_a_broken_panel_shows_its_own_note_and_a_settings_link(self, config, store):
        page = self._page(
            config, store,
            panel={"posted": True, "channel_exists": True, "channel_postable": False},
        )
        section = re.search(r'<h2>Setup</h2>.*?</section>', page, re.S)
        assert section
        assert "check its permissions" in section.group(0)
        # The link this split would have broken silently: the panel field is
        # not on the page the bare /settings URL redirects to, so a fragment
        # alone would have scrolled to nothing.
        assert (
            f'href="/guild/{GUILD_IN}/settings/panel#panel_channel_id"'
            in section.group(0)
        )

    def test_an_unfinished_required_row_reads_differently_from_an_off_optional_one(
        self, config, store
    ):
        """The bug a screenshot caught: a missing verified role and a
        deliberately-unset optional toggle both draw an X, and without a
        second class they would be visually identical -- which is exactly the
        thing #123's `.setup-row.missing` existed to prevent, one CSS
        refactor before this test was written to keep it that way."""
        page = self._page(
            config, store,
            configured=self._configured(verified_role=False,
                                         verified_role_exists=None,
                                         verified_role_assignable=None,
                                         log_channel=False),
            panel={"posted": True},
        )
        assert "setup-row setup-row-todo" in page
        assert "setup-row setup-row-off" in page

    def test_a_done_row_carries_no_action_button(self, config, store):
        page = self._page(config, store, panel={"posted": True})
        section = re.search(r'<h2>Setup</h2>.*?</section>', page, re.S)
        assert section
        # Two rows can be unfinished at most in this fixture (role, panel);
        # both are done here, so no Settings link should appear at all.
        assert "setup-action" not in section.group(0)

    def test_the_complete_banner_appears_only_when_both_required_rows_are_done(
        self, config, store
    ):
        done = self._page(config, store, panel={"posted": True})
        assert "Setup complete" in done

        not_done = self._page(config, store, panel={"posted": False})
        assert "Setup complete" not in not_done

    def test_no_style_attribute_appears_in_the_setup_section(self, config, store):
        """`style-src 'self'` drops inline style="" silently -- the same trap
        the chart's own render test guards against."""
        page = self._page(config, store, panel={"posted": True})
        section = re.search(r'<h2>Setup</h2>.*?</section>', page, re.S)
        assert section
        assert 'style="' not in section.group(0)


class TestEverySectionFailsTheSameWay:
    """The oracle only has to exist on one route to be worth using.

    Three sections that each decided how to refuse would be three chances for
    one of them to be more forthcoming. They share `_guild_page_unavailable`,
    and these tests are what stops a fourth section quietly not doing so.
    """

    SECTIONS = ("", "/settings/verification", "/subscription")

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
        """Settings is a disclosure now (#140 phase 3), so it is reached
        through its children rather than by a link of its own -- the summary
        toggles, the sub-links navigate."""
        test_client, _api = settings_client(config, store)
        page = test_client.get(f"/guild/{GUILD_IN}").data.decode()
        assert f'href="/guild/{GUILD_IN}"' in page
        assert f'href="/guild/{GUILD_IN}/settings/verification"' in page
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
        page = settings_page(test_client).data.decode()

        for label in ("Overview", "Settings", "Subscriptions"):
            assert f'>{label[:1]}</span>' not in page
            assert f'<span class="side-text">{label}</span>' in page

    def test_it_marks_the_current_section(self, config, store):
        test_client, _api = settings_client(config, store)
        page = settings_page(test_client).data.decode()
        # The accessible half of the highlight, not just a class.
        assert 'aria-current="page"' in page

    def test_it_offers_the_way_back_to_the_server_list(self, config, store):
        test_client, _api = settings_client(config, store)
        page = settings_page(test_client).data.decode()
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


def _copy_only(source: str, filename: str) -> str:
    """The parts of a file a person could actually read on the page.

    Comments are stripped because a comment naming a retired command is a
    NOTE about it, not an instruction to use it -- and this exact test failed
    on the comment written to explain the bug it had just found. Python
    comments go through `tokenize` rather than a regex so a `#` inside a
    string, of which this codebase has several as colours, cannot swallow the
    rest of the line.
    """
    if filename.endswith(".html"):
        source = re.sub(r"\{#.*?#\}", "", source, flags=re.S)
        return re.sub(r"<!--.*?-->", "", source, flags=re.S)

    import io
    import tokenize

    kept = []
    try:
        for token in tokenize.generate_tokens(io.StringIO(source).readline):
            if token.type != tokenize.COMMENT:
                kept.append(token.string)
    except (tokenize.TokenError, IndentationError):  # pragma: no cover
        return source
    return "\n".join(kept)


class TestTheControls(object):
    """Inputs, selects, buttons and the refusal page (#133 phase 5)."""

    @staticmethod
    def _css() -> str:
        import dashboard

        with open(
            os.path.join(os.path.dirname(dashboard.__file__), "static", "style.css"),
            encoding="utf-8",
        ) as handle:
            return re.sub(r"/\*.*?\*/", "", handle.read(), flags=re.S)

    @staticmethod
    def _refusal(config, store) -> str:
        """The page _guild_page_unavailable renders when a read is refused.

        `fail=True` is not enough: it only breaks admin_guild_ids, which the
        picker uses. A guild page reads `overview`, so that is the endpoint
        that has to say no.
        """
        api = FakeBotAPI(errors={"overview": BotAPIError("down", status=503)})
        app = create_app(config, store=store, client=api)
        app.config.update(TESTING=True)
        test_client = app.test_client()
        login_as(test_client, store)
        return test_client.get(f"/guild/{GUILD_IN}").data.decode()

    def test_no_rule_removes_a_focus_outline(self):
        """THE BUG THIS PHASE FIXES, and it was invisible by design.

        `select:focus, textarea:focus { ... outline: none }` is MORE SPECIFIC
        than the `:focus-visible` rule that draws the ring, so it won every
        time -- those two controls had no focus ring at all, for anybody,
        including somebody navigating entirely by keyboard.

        The giveaway was two rules further down: `select:focus-visible {
        outline-offset: 0 }` adjusts the position of an outline that was never
        being drawn.

        Stated as a blanket rule because that is what makes it hold: any new
        `outline: none` reintroduces the same class of bug, whatever control it
        is attached to.
        """
        assert "outline: none" not in self._css()
        assert "outline:none" not in self._css()

    def test_the_focus_ring_is_defined_once(self):
        """From the tokens #123 added, so it cannot drift per control."""
        css = self._css()
        assert "outline: var(--focus-width) solid var(--accent-text)" in css

    def test_every_text_control_is_styled_by_us(self):
        """`input[type="text"]` was missing from the control rule entirely, so
        the VRChat group field rendered as the browser's default -- a white box
        with a blue ring, in a dark theme, at a height nothing else shared."""
        css = self._css()
        rule = re.search(
            r'select,\s*textarea,\s*input\[type="text"\],'
            r'\s*input\[type="color"\]\s*\{',
            css,
        )
        assert rule, "the shared control rule no longer covers all four"

    def test_a_control_has_an_edge_of_its_own(self):
        """It was `border: 1px solid transparent`, leaving the boundary to a
        fill measuring 1.17:1 against a white card. A control the eye has to
        infer is not a control that has been drawn."""
        css = self._css()
        assert "border: 1px solid var(--control-line)" in css
        assert "border: 1px solid transparent" not in css

    def test_one_height_serves_every_control(self):
        """So a select, an input and a button in a row line up on both edges
        rather than nearly."""
        css = self._css()
        assert "--control-h:" in css
        for rule in ("min-height: var(--control-h)", "height: var(--control-h)"):
            assert rule in css

    # --- the page reached when the bot cannot answer ---

    def test_the_refusal_page_uses_the_same_chrome_as_every_other(
        self, client, store, config
    ):
        """It is read by somebody whose server may be having a problem right
        now. A page that looks broken while explaining that something is broken
        makes the outage feel bigger than it is."""
        page = self._refusal(config, store)
        assert 'class="panel centered refusal"' in page
        assert 'class="button"' in page

    def test_the_refusal_page_does_not_restyle_the_sign_in_page(self):
        """`.centered` is worn by both. Centring the refusal page through it
        would silently restyle a page this phase has no business touching --
        #134 redesigns that one."""
        css = self._css()
        assert ".centered { text-align: center" not in css
        assert ".refusal { text-align: center; }" in css

    def test_the_refusal_mark_is_not_alarming_and_not_announced(
        self, client, store, config
    ):
        """--notice, not --danger: an unreachable bot is a situation rather
        than a fault, and the copy is at pains to say nothing was lost. And it
        is decorative -- the sentence beside it already says what happened."""
        page = self._refusal(config, store)
        mark = re.search(r'<p class="error-mark"[^>]*>', page)
        assert mark and 'aria-hidden="true"' in mark.group(0)

        css = self._css()
        rule = re.search(r"\.error-mark\s*\{([^}]*)\}", css)
        assert rule and "var(--notice)" in rule.group(1)
        assert "var(--danger)" not in rule.group(1)


class TestTheSignedOutPageHasNoTokenToGive(object):
    """#134 phase 2, retargeted.

    The phase as written asked for four things -- that the header renders with
    no session, that the theme button works signed out, that it works with
    JavaScript off, and a signed-out theme test. All four were already covered
    by tests #123 phase 3 and #133 phase 1 wrote while this issue waited, so
    what is here instead is the gap nobody had named.

    `test_the_only_tokenless_form_is_the_theme_picker` pins that exactly one
    form on the SETTINGS page carries no CSRF token, and which one. Nothing
    made the same promise about the signed-out page -- where it matters more,
    because there is no session and therefore no token to give any form at
    all. A second form added to login.html would be silently tokenless, its
    POST would be refused, and no test would have anything to say about it.
    """

    def test_the_only_forms_here_are_the_ones_that_need_no_token(self, client):
        """The signed-out counterpart of the settings-page invariant.

        `/prefs/theme` and `/prefs/lang` are exempt on purpose: this page
        carries both controls and has nothing to sign either with, which is the
        whole reason #123 made the first route session-free and CSRF-free and
        #97 made the second one. The exemption is spent, twice, here.

        The language picker's case for being on this page in particular is the
        strongest one either control has. Somebody who cannot read English
        arrives here first, and "Sign in with Discord" is not a sentence they
        should have to parse before the site will offer to speak to them.
        """
        page = client.get("/").data.decode()
        forms = re.findall(r"<form\b.*?</form>", page, re.S)
        actions = {re.search(r'action="([^"]*)"', form).group(1) for form in forms}
        assert actions == {"/prefs/theme", "/prefs/lang"}, actions
        for form in forms:
            assert 'name="csrf_token"' not in form

    def test_nothing_here_renders_an_empty_token(self, client):
        """The failure this page invites, and it fails silently.

        `render_template("login.html")` passes no `csrf_token`, and Jinja's
        default undefined renders as an empty string rather than raising. So a
        form written here with `{{ csrf_token }}` in it produces
        `value=""` -- no template error, no warning, and a 400 the first time
        anybody submits it. The page has to carry no token input at all.
        """
        page = client.get("/").data.decode()
        assert 'name="csrf_token"' not in page
        assert 'value=""' not in page

    def test_the_page_survives_the_chrome_it_shares_with_signed_in_pages(
        self, client
    ):
        """base.html branches on `section` and `csrf_token`, and this is the
        one page that has neither. Asserted here as well as in #133 because
        that is where a future header change would break it -- the account
        menu is inside `{% if csrf_token %}`, and the sidebar inside
        `{% if section %}`."""
        page = client.get("/").data.decode()
        assert 'class="sidebar"' not in page
        assert 'class="nav-toggle"' not in page
        assert "Sign out" not in page
        # And what must still be there: the bar itself, and the one control on
        # it that works without a session.
        assert 'class="bar"' in page
        assert 'action="/prefs/theme"' in page


class TestTheSignInCard(object):
    """#134 phase 1. The first page anybody sees.

    It was 24 lines of prose with a link in it. The copy was good -- unusually
    so: it states the limits of what signing in grants, in plain language,
    before asking. That paragraph was `class="muted"`, which is to say the most
    valuable thing on the page was styled as the least important.
    """

    # The three statements, in the words that matter rather than verbatim, so
    # the assertions survive a comma moving.
    PROMISES = (
        "who you are and which servers you are in",
        "don't keep your Discord token",
        "can't see who has verified",
    )

    @staticmethod
    def _css() -> str:
        import dashboard

        with open(
            os.path.join(os.path.dirname(dashboard.__file__), "static", "style.css"),
            encoding="utf-8",
        ) as handle:
            return re.sub(r"/\*.*?\*/", "", handle.read(), flags=re.S)

    @staticmethod
    def _rows(page: str) -> list:
        block = re.search(r'<ul class="signin-grants">(.*?)</ul>', page, re.S)
        assert block, "no promise list on the sign-in page"
        rows = re.findall(r"<li>(.*?)</li>", block.group(1), re.S)
        assert rows, "the promise list is empty"
        # Collapsed, because a claim that wraps across two source lines is one
        # phrase once the browser has it. Asserting against the raw indentation
        # would fail on a reflow that changes nothing anybody reads.
        return [re.sub(r"\s+", " ", row) for row in rows]

    def test_each_promise_is_its_own_row_with_its_own_mark(self, client):
        """Three sentences in one grey paragraph is a paragraph. Three rows
        with a glyph each is a list of what you are granting, which is what
        these actually are."""
        rows = self._rows(client.get("/").data.decode())
        assert len(rows) == 3, f"expected three promises, found {len(rows)}"
        for promise in self.PROMISES:
            assert any(promise in row for row in rows), promise
        for row in rows:
            assert 'class="signin-grant-mark"' in row

    def test_the_promises_are_not_the_quietest_thing_on_the_page(self, client):
        """The substantive change. `muted` on this block is what the redesign
        exists to undo, and it would be an easy thing to reintroduce while
        tidying."""
        page = client.get("/").data.decode()
        block = re.search(r'<ul class="signin-grants"[^>]*>.*?</ul>', page, re.S)
        assert block and "muted" not in block.group(0)
        # And the block has a surface of its own, so it reads as a statement
        # rather than as more prose.
        rule = re.search(r"\.signin-grants\s*\{([^}]*)\}", self._css())
        assert rule and "background:" in rule.group(1)

    def test_the_claim_in_each_row_carries_the_weight(self, client):
        """Somebody skimming should read the three claims and none of the
        grammar joining them."""
        for row in self._rows(client.get("/").data.decode()):
            strong = re.search(r"<strong>(.*?)</strong>", row, re.S)
            assert strong, f"nothing emphasised in: {row.strip()[:60]}"

    def test_there_is_one_primary_action_and_it_starts_the_flow(self, client):
        """"One unmistakable primary action." A second `.button` anywhere on
        this page would be a competing one."""
        page = client.get("/").data.decode()
        buttons = re.findall(r'<a class="button"[^>]*href="([^"]+)"', page)
        assert buttons == ["/login"], buttons
        assert page.count('class="button"') == 1

    def test_the_discord_mark_rides_inside_the_button(self, client):
        """Inline SVG with presentation attributes only -- `style-src 'self'`
        drops inline style="" silently, and a data: URI would mean widening
        img-src to draw one shape."""
        page = client.get("/").data.decode()
        anchor = re.search(r'<a class="button".*?</a>', page, re.S)
        assert anchor and 'class="signin-discord"' in anchor.group(0)
        mark = re.search(r"<svg class=\"signin-discord\"[^>]*>", page)
        assert mark and 'aria-hidden="true"' in mark.group(0)
        assert "style=" not in mark.group(0)

    def test_the_logo_is_local_and_carries_a_digest(self, client):
        """`img-src` is 'self' plus Discord's CDN, so it has to be served from
        here -- and through asset(), or a deploy leaves a year-cached stale
        copy behind."""
        page = client.get("/").data.decode()
        src = re.search(r'<img class="signin-mark" src="([^"]+)"', page)
        assert src, "no logo on the sign-in card"
        assert src.group(1).startswith("/static/")
        assert "?v=" in src.group(1)

    def test_the_terms_are_reachable_from_the_page_that_asks_you_to_agree(
        self, client
    ):
        """A sign-in screen that never mentions the terms is a gap. They live
        on the apex site, so these are absolute rather than url_for."""
        page = client.get("/").data.decode()
        for path in ("terms", "privacy", "refunds"):
            assert f"https://vrcverify.com/{path}" in page, path

    def test_the_card_does_not_share_a_class_with_another_page(self, client):
        """`.centered` was worn by this page and the refusal page both, and
        #133 phase 5 nearly restyled this one by accident through it. Three
        collisions of that shape turned up across #133 -- `.centered`,
        `.empty`, `.plan` -- so this card's classes are prefixed, and this is
        what keeps them that way.
        """
        import dashboard

        directory = os.path.join(os.path.dirname(dashboard.__file__), "templates")
        with open(os.path.join(directory, "login.html"), encoding="utf-8") as handle:
            login = re.sub(r"\{#.*?#\}", "", handle.read(), flags=re.S)

        mine = {
            name
            for attr in re.findall(r'class="([^"{]+)"', login)
            for name in attr.split()
            if name.startswith("signin")
        }
        assert mine, "the card introduced no classes of its own"

        for other in os.listdir(directory):
            if other in ("login.html", "base.html"):
                continue
            with open(os.path.join(directory, other), encoding="utf-8") as handle:
                body = re.sub(r"\{#.*?#\}", "", handle.read(), flags=re.S)
            used = {
                name
                for attr in re.findall(r'class="([^"{]+)"', body)
                for name in attr.split()
            }
            assert not (mine & used), f"{other} shares {sorted(mine & used)}"

        # And the page no longer borrows the refusal page's class.
        assert "centered" not in login


class TestThePickerSaysOnlyWhatItKnows(object):
    """`installed` is two answers wearing one name.

    `admin_guild_ids` returns the guilds the bot is in AND the caller
    administers; its docstring says a guild missing from that answer means
    either "bot not there" or "not yours" -- indistinguishable on purpose,
    because telling them apart would let a signed-in user map which
    communities run 18+ gating.

    Phase 3 rebuilt the card and had it print "VRCVerify isn't in this server
    yet": a definite claim about the bot's presence, drawn from a signal that
    carries no such fact. An admin demoted since their last sign-in was told
    their working bot was not installed, and offered a link to install it
    again.
    """

    def _absent_card(self, client, store) -> str:
        """The card for GUILD_OUT, which the default fake bot is not in."""
        login_as(client, store)
        page = client.get("/").data.decode()
        cards = re.findall(r'<li class="server-card absent">.*?</li>', page, re.S)
        assert len(cards) == 1, f"expected one absent card, got {len(cards)}"
        return cards[0]

    def test_the_card_does_not_claim_the_bot_is_absent(self, client, store):
        """The bot's presence is precisely what this page cannot establish."""
        card = self._absent_card(client, store)
        for claim in (
            "isn't in this server",
            "is not in this server",
            "isn't here yet",
            "not installed",
        ):
            assert claim not in card, f"card asserts {claim!r}"

    def test_the_card_carries_both_readings(self, client, store):
        """The refusal page one click away has always got this right: "Either
        VRCVerify isn't in it, or you don't have the Administrator permission
        there." The card has to mean the same thing at its own width."""
        card = self._absent_card(client, store).lower()
        assert "or" in card
        assert "not set up here" in card

    def test_the_offer_survives_both_readings(self, client, store):
        """The install link stays, and stays an offer rather than an
        instruction: Discord refuses it to anyone without Manage Server, and
        re-authorising a bot already in the server changes nothing. A sentence
        telling somebody to go and install it would be right under only one of
        the two readings."""
        card = self._absent_card(client, store)
        assert "Add to server" in card
        assert "/oauth2/authorize" in card or "discord.com" in card


class TestTheAccountMenuIsWhereItPromisedToBe(object):
    """base.html gates the account menu on `csrf_token`, and the 404 and 500
    handlers passed none -- so a signed-in admin who mistyped a URL landed on a
    page with no way to sign out.

    That is the one thing the menu is in the bar for. The comment beside it
    says "sign out everywhere" is what you want at the moment you realise
    somebody else has your session, "and at that moment you should not have to
    go looking". A typo should not be what takes it away.
    """

    def test_a_mistyped_url_still_offers_a_way_out(self, client, store):
        login_as(client, store)
        page = client.get("/nope").data.decode()
        assert "Sign out everywhere" in page, "no account menu on the 404 page"

    def test_the_signed_out_404_still_offers_nothing(self, client):
        """The other half: no session, no menu, and no CSRF token minted for
        somebody who has not signed in."""
        page = client.get("/nope").data.decode()
        assert "Sign out everywhere" not in page

    def test_the_guild_refusal_pages_were_always_right(self, client, store, config):
        """They pass the token through `_guild_page_unavailable`, which is why
        only the generic handlers were affected. Asserted so the two paths
        cannot drift apart again."""
        api = FakeBotAPI(errors={"overview": BotAPIError("down", status=503)})
        app = create_app(config, store=store, client=api)
        app.config.update(TESTING=True)
        test_client = app.test_client()
        login_as(test_client, store)
        page = test_client.get(f"/guild/{GUILD_IN}").data.decode()
        assert "Sign out everywhere" in page


class TestEverySettingsControlIsLabelled(object):
    """Found by an adversarial pass over the whole of #133, not by the suite.

    Phase 4 put `id="l-<field>"` on all eleven setting rows and then wired only
    the four switches to it, so six controls -- both role pickers, the log
    channel, the language, the group id and the custom message -- had no
    accessible name at all. A screen reader announced the verified-role picker
    as an unnamed combo box.

    The same phase took something away on the way past. A boolean used to be
    `<label class="check"><input type="checkbox"> Auto verify</label>`, where
    the words were part of the target. The switch that replaced it had a name
    via `aria-labelledby` and no label element, so the caption stopped being
    clickable and a 40x24 switch became the only thing you could hit.
    """

    @staticmethod
    def _controls(page: str) -> list:
        """Every control a person can operate, with where its name comes from."""
        labels_for = set(re.findall(r'<label[^>]*\sfor="([^"]+)"', page))
        wrapped = set()
        for block in re.findall(r"<label\b.*?</label>", page, re.S):
            wrapped.update(
                re.findall(r'<(?:input|select|textarea)\b[^>]*name="([^"]+)"', block)
            )

        found = []
        for match in re.finditer(r"<(input|select|textarea)\b([^>]*)>", page, re.S):
            attrs = match.group(2)
            kind = re.search(r'type="([^"]+)"', attrs)
            if kind and kind.group(1) in ("hidden", "submit"):
                continue
            name = re.search(r'name="([^"]+)"', attrs)
            name = name.group(1) if name else ""
            control_id = re.search(r'\sid="([^"]+)"', attrs)
            sources = []
            if "aria-labelledby" in attrs:
                sources.append("aria-labelledby")
            if "aria-label=" in attrs:
                sources.append("aria-label")
            if control_id and control_id.group(1) in labels_for:
                sources.append("label-for")
            if name in wrapped:
                sources.append("label-wraps-it")
            found.append((match.group(1), name, attrs, sources))

        assert found, "no controls on the page -- every assertion below is vacuous"
        return found

    def _page(self, config, store, **kwargs):
        test_client, _api = settings_client(config, store, **kwargs)
        return every_settings_page(test_client)

    def test_every_control_has_an_accessible_name(self, config, store):
        """Six of the thirteen had none. The mechanism to fix it was already on
        every row -- the <dt> has carried an id since phase 4."""
        page = self._page(config, store, settings=make_settings(premium=True))
        nameless = [
            name or tag
            for tag, name, _attrs, sources in self._controls(page)
            if not sources
        ]
        assert not nameless, f"no accessible name: {nameless}"

    def test_the_words_beside_a_control_are_part_of_its_target(
        self, config, store
    ):
        """What the switches took away, and the reason phase 6's target-size
        fix was treating a symptom.

        `aria-labelledby` names a control; it does not make anything clickable.
        Only a <label> does -- either pointing at the control with `for` or
        wrapping it -- and clicking the caption is what a checkbox has always
        done. Every operable control on this page has to have one, whatever
        shape it is drawn as.
        """
        page = self._page(config, store, settings=make_settings(premium=True))
        unreachable = [
            name or tag
            for tag, name, attrs, sources in self._controls(page)
            if "disabled" not in attrs
            and not {"label-for", "label-wraps-it"} & set(sources)
        ]
        assert not unreachable, f"caption is not part of the target: {unreachable}"

    def test_a_label_never_points_at_a_control_nobody_can_operate(
        self, config, store
    ):
        """A locked field's caption must not invite a click that does nothing.
        The disabled switches carry no id at all, so nothing can point at
        them -- this is what keeps that true."""
        page = self._page(config, store)
        targets = set(re.findall(r'<label[^>]*\sfor="([^"]+)"', page))
        for target in targets:
            control = re.search(
                r'<(?:input|select|textarea)\b[^>]*\sid="' + re.escape(target) + r'"[^>]*>',
                page,
            )
            assert control, f"label points at {target}, which does not exist"
            assert "disabled" not in control.group(0), target

    def test_the_unsaved_warning_is_announced_and_not_only_shown(
        self, config, store
    ):
        """CSS revealing a static node fires no accessibility event. Without a
        live region the page makes the switch's promise visually and corrects
        it visually, which is no correction at all for somebody listening."""
        page = self._page(config, store, settings=make_settings(premium=True))
        indicator = re.search(r'<span class="unsaved"[^>]*>', page)
        assert indicator, "no unsaved indicator on a saveable page"
        assert 'role="status"' in indicator.group(0)

    def test_a_switch_row_sits_at_the_same_height_as_every_other_row(self):
        """It is a <p>, and it was the only one in the stylesheet left with the
        UA `margin: 1em 0` -- so a boolean row stood taller than the select
        beside it, on the page phase 5 restyled to line those up."""
        import dashboard

        with open(
            os.path.join(os.path.dirname(dashboard.__file__), "static", "style.css"),
            encoding="utf-8",
        ) as handle:
            css = re.sub(r"/\*.*?\*/", "", handle.read(), flags=re.S)
        rule = re.search(r"\.switch-row\s*\{([^}]*)\}", css)
        assert rule and "margin: 0" in rule.group(1)


class TestNarrowScreens(object):
    """#133 phase 6. What the app does on a phone.

    An admin fixing their server from a phone is the case the issue asks to be
    decided on purpose rather than left to whatever falls out of the collapse
    cookie -- so the decision is asserted here and explained in the stylesheet
    beside the rules that carry it.
    """

    @staticmethod
    def _css() -> str:
        import dashboard

        with open(
            os.path.join(os.path.dirname(dashboard.__file__), "static", "style.css"),
            encoding="utf-8",
        ) as handle:
            return re.sub(r"/\*.*?\*/", "", handle.read(), flags=re.S)

    @classmethod
    def _block(cls, opener: str) -> str:
        """The body of one @media block, brace-matched.

        Searching the whole file would find the wide rule and the narrow
        override alike, and every assertion here is about which of the two it
        landed in.
        """
        css = cls._css()
        start = css.index(opener) + len(opener)
        depth = 1
        for i in range(start, len(css)):
            depth += {"{": 1, "}": -1}.get(css[i], 0)
            if depth == 0:
                return css[start:i]
        raise AssertionError(f"unclosed block: {opener}")

    @classmethod
    def _narrow(cls) -> str:
        return cls._block("@media (max-width: 48rem) {")

    @classmethod
    def _touch(cls) -> str:
        return cls._block("@media (pointer: coarse) {")

    @staticmethod
    def _rule(css: str, selector: str) -> str:
        found = re.search(
            r"(?<![\w.:-])" + re.escape(selector) + r"\s*\{([^}]*)\}", css
        )
        assert found, f"no rule for {selector}"
        return found.group(1)

    @staticmethod
    def _px(length: str) -> float:
        """rem at the 16px root this stylesheet sets on <html>."""
        if length.endswith("rem"):
            return float(length[:-3]) * 16
        if length.endswith("px"):
            return float(length[:-2])
        return float(length)

    # --- the sidebar decision ---

    def test_the_collapse_button_is_hidden_where_collapsing_does_nothing(self):
        """The narrow-screen bug, and it was a control that lied.

        Below 48rem the sidebar is a strip rather than a column, and the same
        block restores the labels whatever the cookie says -- so `collapsed`
        and expanded render identically down here. The hamburger was still in
        the bar: pressing it posted a form, reloaded the page, and changed
        nothing except its own label from "Hide the sidebar" to "Show the
        sidebar". A screen reader was being told something had been hidden
        that had not been.
        """
        narrow = self._narrow()
        assert "display: none" in self._rule(narrow, ".nav-toggle")
        # The reason it does nothing, asserted alongside so the two cannot
        # drift apart: if collapsing ever means something at this width again,
        # this line goes and the button should come back with it.
        assert "display: inline" in self._rule(narrow, ".layout.collapsed .side-text")

    def test_the_button_is_hidden_by_the_stylesheet_and_not_by_the_template(
        self, config, store
    ):
        """The obvious alternative fix is wrong, and quietly so.

        Dropping the hamburger from base.html behind a condition would hide it
        on a desktop too: the server renders the same HTML for every viewport
        and has no way to know how wide the window is. Only CSS knows. So the
        markup stays on every section page and the media query decides -- and
        the collapse preference keeps round-tripping, so a rail collapsed on a
        desktop is still collapsed when its owner goes back to one.
        """
        test_client, _api = settings_client(config, store)
        page = every_settings_page(test_client)
        assert 'class="nav-toggle"' in page
        assert 'name="collapsed" value="1"' in page

    def test_the_strip_puts_the_way_out_and_the_server_on_one_row(self):
        """Three stacked rows -- "All servers", the server, the sections --
        cost most of a phone screen before any content. Two rows is the
        decision: a breadcrumb line, then the sections as tabs beneath a
        rule."""
        narrow = self._narrow()
        assert "display: grid" in self._rule(narrow, ".sidebar")
        nav = self._rule(narrow, ".side-nav")
        assert "grid-column: 1 / -1" in nav
        # The separator moves from under the identity block to above the tabs,
        # so the two rows are divided once rather than twice.
        assert "border-top" in nav
        assert "border-bottom: 0" in self._rule(narrow, ".side-guild")

    # --- pages with no sidebar at all ---

    def test_a_page_without_a_sidebar_keeps_its_margins_on_a_phone(self, client):
        """The sign-in page and the server picker wear `.layout.plain`, which
        zeroes the wrapper's padding and leaves `main` to carry it. The narrow
        block set `main` to `padding: 1rem 0 2rem`, so those two ran edge to
        edge on a phone -- cards touching both sides of the screen, on the
        first page anybody sees after signing in."""
        sides = self._rule(self._narrow(), "main").split("padding:")[1]
        horizontal = sides.split(";")[0].split()[1]
        assert self._px(horizontal) > 0, "plain pages lose their side padding"

    def test_every_other_page_still_takes_its_inset_from_the_wrapper(self):
        """The pairing that makes the rule above safe. A page WITH a sidebar
        gets its inset from `.layout`, and `main` adding its own would inset
        the content twice -- so the more specific rule zeroing it has to stay
        or the fix above becomes a different bug.

        `padding-inline: 0` rather than the left/right pair (#229): a logical
        shorthand zeroes both sides in either writing direction, which a
        physical pair would not once Arabic's `dir="rtl"` is in play."""
        rule = self._rule(self._css(), ".layout:not(.plain) main")
        assert "padding-inline: 0" in rule

    def test_the_chrome_and_the_content_share_one_left_edge(self):
        """The bar was inset 1.5rem while the content it sits over was inset
        0.75rem, so the brand hung inboard of every card on the page. The
        1.5rem was also the widest thing in the bar, and losing it is half of
        what stops the header overflowing a 320px screen -- the hidden
        hamburger is the other half."""
        narrow = self._narrow()
        edges = {
            selector: self._rule(narrow, selector).split("padding:")[1].split(";")[0]
            for selector in (".bar", ".layout", "footer")
        }
        insets = {name: self._px(value.split()[1]) for name, value in edges.items()}
        assert len(set(insets.values())) == 1, insets

    # --- touch targets ---

    def test_a_switch_clears_the_minimum_target_size(self):
        """WCAG 2.2 SC 2.5.8 asks for 24x24 CSS pixels. The switch this issue
        added was drawn at 36x20 -- four pixels short, and short for everybody
        rather than only on a phone, since the requirement is not about which
        pointer you happen to be using."""
        base = self._rule(self._css(), ".switch")
        width = self._px(re.search(r"width:\s*(\S+);", base).group(1))
        height = self._px(re.search(r"height:\s*(\S+);", base).group(1))
        assert (width, height) >= (24, 24), (width, height)

        # And comfortably bigger where the pointer is a thumb. Not 44 tall:
        # see the note in the stylesheet about the row rhythm on the densest
        # page in the app.
        touch = self._rule(self._touch(), ".switch")
        assert self._px(re.search(r"width:\s*(\S+);", touch).group(1)) >= 44
        assert self._px(re.search(r"height:\s*(\S+);", touch).group(1)) >= 24

    def test_the_knob_stops_at_the_end_of_the_track(self):
        """At BOTH sizes, which is why the two widths were picked the way they
        were: width - knob - (2 x inset) lands on 16px either way, so one
        `translate` serves both. The base size satisfied it before this phase
        by luck of the original numbers; the touch size did not exist. It is
        here so that a future resize cannot quietly leave the knob overhanging
        the end of its track -- nothing else in the suite would notice.

        The rest position is `inset-inline-start`, not `left` (#229): it has
        to be the inline-start side in both directions, and only the logical
        property gives that for free under `dir="rtl"`. The math here stays
        LTR-only -- `translate` does not mirror on its own, so RTL gets its
        own checked-knob rule, asserted separately below rather than here."""
        css = self._css()
        travel = self._px(
            re.search(r"\.switch:checked::before\s*\{[^}]*translate:\s*(\S+)\s", css)
            .group(1)
        )
        inset = self._px(
            re.search(
                r"\.switch::before\s*\{[^}]*inset-inline-start:\s*(\S+);", css, re.S
            ).group(1)
        )

        for scope, knob_rule in (
            (self._css(), r"\.switch::before\s*\{"),
            (self._touch(), r"\.switch::before\s*\{"),
        ):
            track = self._px(
                re.search(r"(?<![\w.:-])\.switch\s*\{[^}]*width:\s*(\S+);", scope).group(1)
            )
            knob = self._px(
                re.search(knob_rule + r"[^}]*width:\s*(\S+);", scope, re.S).group(1)
            )
            assert track - knob - 2 * inset == travel, (track, knob, travel)

    def test_the_rtl_knob_travels_the_same_distance_the_other_way(self):
        """`translate` moves along the physical X axis regardless of `dir`,
        so mirroring the rest position with `inset-inline-start` above is not
        enough on its own -- a `[dir="rtl"]` override has to send the checked
        knob the other way by the same distance, or it either overhangs the
        track or falls short of it (#229)."""
        css = self._css()
        travel = self._px(
            re.search(r"\.switch:checked::before\s*\{[^}]*translate:\s*(\S+)\s", css)
            .group(1)
        )
        rtl_travel = self._px(
            re.search(
                r'\[dir="rtl"\]\s*\.switch:checked::before\s*\{[^}]*translate:\s*(\S+)\s',
                css,
            ).group(1)
        )
        assert rtl_travel == -travel

    # --- one word, two meanings ---

    def test_the_pickers_empty_state_cannot_restyle_a_settings_value(self, config, store):
        """`.empty` was two different things in two templates.

        settings.html marks a value with nothing behind it as
        `class="value empty"` -- "Not set", "No panel found", "Couldn't check"
        -- and the picker's empty-state card was a bare `.empty`, so every one
        of those little grey phrases was being drawn as a padded, bordered
        card on the densest page in the app.
        """
        test_client, _api = settings_client(config, store)
        page = every_settings_page(test_client)
        assert 'class="value empty"' in page, "settings still marks empty values"

        bare = re.findall(r"(?:^|[\s,>+~{}])(\.empty)(?![\w-])", self._css())
        assert not bare, "`.empty` on its own reaches a settings value"


class TestSettingsPagesHaveDistinctTitles:
    """Found on an adversarial pass over #140: six routes shared one title.

    Verification and Logging, open in two tabs -- the exact workflow the split
    makes MORE likely, since it trades one long page for six short ones -- were
    impossible to tell apart from the tab bar, and neither was distinguishable
    from Overview's tab for the same guild.
    """

    def logged_in(self, config, store, **kwargs):
        test_client, _api = settings_client(config, store, **kwargs)
        return test_client

    @staticmethod
    def _title(page: str) -> str:
        return re.search(r"<title>(.*?)</title>", page, re.S).group(1)

    def test_every_group_gets_its_own_title(self, config, store):
        test_client = self.logged_in(config, store)
        titles = {
            group: self._title(settings_page(test_client, group).data.decode())
            for group in SETTINGS_GROUPS
        }
        assert len(set(titles.values())) == len(titles), titles

    def test_activity_is_not_named_like_a_group(self, config, store):
        test_client = self.logged_in(config, store)
        titles = {
            self._title(settings_page(test_client, group).data.decode())
            for group in SETTINGS_GROUPS
        }
        assert self._title(
            settings_page(test_client, "activity").data.decode()
        ) not in titles

    def test_no_settings_title_collides_with_overview(self, config, store):
        test_client = self.logged_in(config, store)
        overview_title = self._title(
            test_client.get(f"/guild/{GUILD_IN}").data.decode()
        )
        for group in SETTINGS_GROUPS + ("activity",):
            assert (
                self._title(settings_page(test_client, group).data.decode())
                != overview_title
            ), group

    def test_the_title_names_the_group_by_its_own_label(self, config, store):
        """Not a second copy of the name -- read from the same table the nav
        and the page heading already share."""
        test_client = self.logged_in(config, store)
        for slug, label in settings_view.SETTINGS_GROUPS:
            title = self._title(settings_page(test_client, slug).data.decode())
            assert title.startswith(label), (slug, title)


class TestTheSubNavOnAPhone(object):
    """#140 phase 3. The one part of this the reference screenshot could not
    answer, because the reference is a desktop sidebar.

    Below --bp there is no sidebar to indent into: `.side-nav` is a row of tabs
    above the content. The decision is a SECOND STRIP -- one row, scrolling
    sideways -- rather than letting six children wrap the nav to three or four
    rows before a single setting. Featurebase, Reddit and Etsy all put
    sub-navigation on the content side this way.

    Asserted against the stylesheet like the rest of #133 phase 6, and measured
    in a real browser besides: the first two attempts at this rule produced a
    row that looked right and scrolled the whole PAGE sideways.
    """

    # Borrowed rather than copied: the same brace-matching reader #133 phase 6
    # uses, so both classes are asserting against the same parse. Wrapped in
    # `staticmethod` because a bare assignment would turn them back into
    # instance methods and hand each one a `self` it has no parameter for.
    _css = staticmethod(TestNarrowScreens._css)
    _block = classmethod(TestNarrowScreens._block.__func__)
    _narrow = staticmethod(TestNarrowScreens._narrow)
    _rule = staticmethod(TestNarrowScreens._rule)

    def test_the_children_take_a_row_of_their_own(self):
        rule = self._rule(self._narrow(), ".side-branch")
        assert "100%" in rule

    def test_that_row_comes_after_the_whole_strip(self):
        """In source order it sits between Settings and Subscriptions, which
        pushed Subscriptions onto a third line of its own."""
        assert "order:" in self._rule(self._narrow(), ".side-branch")

    def test_it_is_one_row_that_scrolls(self):
        rule = self._rule(self._narrow(), ".side-sub")
        assert "nowrap" in rule
        assert "overflow-x: auto" in rule

    def test_the_row_can_shrink_below_its_contents(self):
        """`min-width: auto` is a flex item's default and means "never shrink
        below your content". With six tabs that is 679px on a 390px phone: the
        strip stops scrolling and the page scrolls sideways instead. Nothing in
        the markup shows this -- it was found by measuring the rendered box."""
        rule = self._rule(self._narrow(), ".side-sub")
        assert "min-width: 0" in rule

    def test_a_tab_keeps_its_width_rather_than_squeezing(self):
        """The point of scrolling is that "Instructions panel" stays readable."""
        assert "0 0 auto" in self._rule(self._narrow(), ".side-sub li")
        assert "nowrap" in self._rule(self._narrow(), ".side-sub-link")

    def test_the_rail_still_shows_it_here(self):
        """A collapsed sidebar is a tab strip at this width, so the desktop
        rule that hides the sub-nav in the rail must not apply."""
        assert "display: flex" in self._rule(
            self._narrow(), ".layout.collapsed .side-sub"
        )

    def test_the_wide_layout_indents_instead(self):
        """And does not inherit any of the above."""
        wide = self._css().split("@media (max-width: 48rem)")[0]
        rule = self._rule(wide, ".side-sub")
        assert "padding" in rule
        assert "overflow-x" not in rule

    def test_the_rail_hides_it_on_a_real_sidebar(self):
        """Icons only, and a sub-item has no icon to be."""
        wide = self._css().split("@media (max-width: 48rem)")[0]
        assert "display: none" in self._rule(wide, ".layout.collapsed .side-sub")

    def test_the_touch_target_matches_every_other_sidebar_row(self):
        """Found on an adversarial pass: every other interactive sidebar row --
        `.side-link`, `.menu-item`, the hamburger, `.button` -- gets bumped to
        44px under `@media (pointer: coarse)`. This is the one new interactive
        element phase 3 introduced for exactly the touch-width case that block
        exists for, and it was left out. Measured before the fix: 34.4px
        rendered on a 390px touch viewport."""
        rule = self._rule(self._block("@media (pointer: coarse) {"), ".side-sub-link")
        assert "min-height: 44px" in rule


class TestWhatTheDashboardSaysAboutDiscord:
    """Issue #154. The dashboard told people to change settings with a command
    that stopped being able to change anything.

    Three commands used to edit settings -- /vrcverify_settings,
    /vrcverify_logchannel and /vrcverify_setrequestmessage. When configuration
    moved to this site all three became the same read-only summary that links
    back here. They were kept deliberately, so a command that is now a
    migration notice still answers rather than vanishing.

    That makes them the worst thing to point somebody at. A command that no
    longer exists fails visibly; one that renders a settings summary looks like
    it worked.
    """

    # Reads and links back. Naming any of these as the way to CHANGE something
    # is the bug this class exists to keep fixed.
    READ_ONLY = (
        "/vrcverify_settings",
        "/vrcverify_logchannel",
        "/vrcverify_setrequestmessage",
    )

    def test_no_save_error_sends_an_admin_to_a_read_only_command(self):
        """Every one of these is read by somebody whose save was just refused,
        looking for the thing that will work instead. There is no wording in
        which naming a read-only command there is correct, so the rule is
        absolute rather than per-string."""
        for reason, message in app_module.SAVE_ERRORS.items():
            for command in self.READ_ONLY:
                assert command not in message, f"{reason}: {message}"

    def test_the_sign_in_page_does_not_claim_discord_can_configure(
        self, client
    ):
        """It said configuration could all be done from Discord instead, and
        that this site was "an alternative, not a replacement". Neither is
        true, on the page read while deciding whether to sign in at all."""
        page = client.get("/").data.decode()
        assert "configure everything" not in page
        assert "an alternative, not a" not in page

    def test_the_sign_in_page_names_the_command_that_can_still_write(
        self, client
    ):
        """Removing the false claim without replacing it would leave somebody
        who does not want a browser with nothing. /vrcverify_setup survived the
        cut and really does configure a server enough to start verifying."""
        page = client.get("/").data.decode()
        assert "/vrcverify_setup" in page

    def test_every_command_the_dashboard_names_still_exists(self):
        """The general version, and the drift this class is one instance of.

        The dashboard image does not ship bot.py, so nothing at runtime can
        notice a command being renamed or removed -- the copy would simply go
        on naming it. Here in the repo the two can be compared.
        """
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "src", "bot.py"), encoding="utf-8") as handle:
            bot_source = handle.read()
        registered = set(re.findall(r'name="(vrcverify[a-z_]*)"', bot_source))
        assert registered, "no slash commands found in bot.py"

        import dashboard

        named = set()
        dashboard_dir = os.path.dirname(dashboard.__file__)
        for folder, _dirs, files in os.walk(dashboard_dir):
            for name in files:
                if not name.endswith((".py", ".html")):
                    continue
                path = os.path.join(folder, name)
                with open(path, encoding="utf-8") as handle:
                    source = handle.read()
                named |= set(re.findall(r"/(vrcverify[a-z_]*)", _copy_only(source, name)))

        assert named, "the dashboard names no commands at all any more"
        assert named <= registered, f"named but not registered: {named - registered}"


class TestTheToggleSwitches:
    """#133 phase 4. The phase the issue puts a warning on.

    Every switch in every product this was compared against saves the moment
    you flip it. This one cannot -- there is no `connect-src`, so saving means
    submitting the form. A switch that looks like it saved and did not is
    WORSE than the checkbox it replaced, so the tests here are mostly about
    the four states staying distinguishable and about the form never sending
    something nobody asked for.
    """

    @staticmethod
    def _rows(page: str) -> dict:
        """Every setting row that renders a switch, by field name.

        ASSERTS THAT IT FOUND SOME. Three tests below are `for row in
        _rows(...)` with every assertion inside the loop, so an empty result
        made all three pass while checking nothing -- including the one that
        guards against silently turning a setting off. Proved rather than
        supposed: renaming `class="switch"` to `class="swtich"` in the
        template, which deletes every switch from the page, left all three
        green.

        One helper, one place to fix it. A per-test `assert rows` would have
        to be remembered by whoever writes the fourth.
        """
        found = {}
        for row in re.findall(r'<div class="setting[^"]*">.*?</div>', page, re.S):
            if 'class="switch"' not in row:
                continue
            name = re.search(r'<dt id="l-([a-z_]+)"', row).group(1)
            found[name] = row
        assert found, "no switch rows on the page -- every caller would pass vacuously"
        return found

    def _page(self, config, store, **kwargs):
        test_client, _api = settings_client(config, store, **kwargs)
        return every_settings_page(test_client)

    def test_a_switch_is_a_real_checkbox(self, config, store):
        """Not a div with a click handler. Keeping the native control is what
        makes the keyboard, the focus ring, the label association and the
        screen reader announcement the browser's problem rather than ours."""
        rows = self._rows(self._page(config, store, settings=make_settings(premium=True)))
        assert rows
        for row in rows.values():
            assert 'type="checkbox"' in row

    def test_a_switch_borrows_its_name_from_the_row_it_sits_in(
        self, config, store
    ):
        """It has no visible label of its own -- the <dt> above says the same
        words, and printing them twice reads as two settings."""
        rows = self._rows(self._page(config, store, settings=make_settings(premium=True)))
        for name, row in rows.items():
            assert f'aria-labelledby="l-{name}"' in row
            assert f'<dt id="l-{name}"' in row

    # --- the four states ---

    def test_a_locked_switch_cannot_be_flipped_and_says_why(
        self, config, store
    ):
        """The bot refuses the save, so offering the control would be an
        invitation to lose a change."""
        row = self._rows(self._page(config, store))["auto_nickname_change"]
        assert "disabled" in row
        assert "badge premium" in row
        assert "Premium only" in row

    def test_an_inactive_switch_can_still_be_flipped(self, config, store):
        """THE DISTINCTION THAT KEEPS THIS SITE FROM BEING STRICTER THAN THE
        SLASH COMMANDS.

        `inactive` means the value saves fine for anyone and the bot simply
        does not act on it. Disabling it here would tell an admin they cannot
        set something they can plainly set in Discord.
        """
        settings = make_settings()
        settings["fields"]["auto_nickname_change"].update(
            active=False, locked=False
        )
        row = self._rows(self._page(config, store, settings=settings))[
            "auto_nickname_change"
        ]
        assert "disabled" not in row
        assert "badge inactive" in row
        assert "not acted on" in row

    def test_locked_and_inactive_do_not_share_a_style(self, config, store):
        """Collapsing them would be a functional regression, not a styling
        choice -- one refuses the save and the other accepts it."""
        settings = make_settings()
        settings["fields"]["auto_nickname_change"].update(
            active=False, locked=False
        )
        inactive = self._rows(self._page(config, store, settings=settings))[
            "auto_nickname_change"
        ]
        locked = self._rows(self._page(config, store))["auto_nickname_change"]

        assert ("disabled" in locked) != ("disabled" in inactive)
        assert "Premium only" in locked and "Premium only" not in inactive

    def test_an_unwritable_switch_does_not_blame_the_plan(self, config, store):
        """The fourth state, and the one with nothing to do with premium: the
        bot's save path has not opened this field to the website. It looks
        non-interactive for the same reason a locked field does and is
        non-interactive for a completely different one."""
        settings = make_settings(
            premium=True, writable=WRITABLE - {"panel_show_icon"}
        )
        row = self._rows(self._page(config, store, settings=settings))[
            "panel_show_icon"
        ]
        assert "disabled" in row
        assert "badge premium" not in row
        assert "Premium only" not in row
        assert "hasn't opened this setting" in row

    # --- the invariant that could lose data ---

    def test_a_switch_nobody_can_flip_is_not_declared_present(
        self, config, store
    ):
        """THE ONE THAT COULD DESTROY A SETTING.

        A disabled input submits nothing at all, which is indistinguishable
        from an unticked one. The hidden `present_<name>` marker is what tells
        the save path "this field was on the page" -- so rendering it beside a
        disabled switch would post "it was there and it is off", which is how
        you ask for a setting to be turned OFF.

        The bot would refuse both of these fields anyway, being the reason they
        are uneditable. That is not a defence: this page must not send a change
        nobody made.
        """
        page = self._page(config, store)
        checked = 0
        for name, row in self._rows(page).items():
            if "disabled" in row:
                checked += 1
                assert f'name="present_{name}"' not in row, name
        # The rows themselves being non-empty is not enough here: the assertion
        # only runs for a DISABLED switch, so a fixture with none would still
        # have proved nothing.
        assert checked, "no disabled switch on the page to check"

    def test_an_editable_switch_is_still_declared_present(self, config, store):
        """The other half. Without the marker, turning a switch OFF and saving
        looks exactly like a form that never carried the field, so the change
        would be dropped instead."""
        page = self._page(config, store, settings=make_settings(premium=True))
        for name, row in self._rows(page).items():
            assert "disabled" not in row
            assert f'name="present_{name}"' in row, name

    # --- the promise a switch makes ---

    def test_every_group_that_saves_carries_an_unsaved_indicator(
        self, config, store
    ):
        """It ships with the switches or the switches do not ship. A switch is
        a much stronger promise than a checkbox -- everywhere else, flipping
        one saves it -- and this page cannot keep that promise."""
        page = self._page(config, store, settings=make_settings(premium=True))
        forms = re.findall(r"<form[^>]*data-guard.*?</form>", page, re.S)
        assert forms
        for form in forms:
            assert 'class="unsaved"' in form
            assert "Save changes" in form

    def test_the_indicator_is_rendered_by_the_page_not_the_script(self):
        """app.js is documented as never writing markup. It reveals the
        indicator with a class the stylesheet already knows; the words come
        from the template on every load."""
        import dashboard

        with open(
            os.path.join(os.path.dirname(dashboard.__file__), "static", "app.js"),
            encoding="utf-8",
        ) as handle:
            script = handle.read()
        # Comments first: this file's own docstring forbids `innerHTML` by
        # name, so a naive search finds the prohibition and calls it a
        # violation. The first version of this test did exactly that.
        code = re.sub(r"/\*.*?\*/", "", script, flags=re.S)
        code = re.sub(r"^\s*//.*$", "", code, flags=re.M)
        assert "innerHTML" not in code
        assert "createElement" not in code
        assert "is-dirty" in code

    def test_the_beforeunload_guard_is_still_there(self):
        """The indicator is the visible half, not a replacement. The prompt is
        the last thing between an admin and a lost edit."""
        import dashboard

        with open(
            os.path.join(os.path.dirname(dashboard.__file__), "static", "app.js"),
            encoding="utf-8",
        ) as handle:
            script = handle.read()
        # Comments stripped for the same reason as above: this file explains
        # the guard at length, so searching the whole text finds the
        # explanation and calls it the guard. Renaming the listener to
        # something inert left this test passing until it was checked.
        code = re.sub(r"/\*.*?\*/", "", script, flags=re.S)
        code = re.sub(r"^\s*//.*$", "", code, flags=re.M)
        assert 'addEventListener("beforeunload"' in code
        assert "returnValue" in code

    def test_the_knob_only_moves_when_motion_is_welcome(self):
        """A switch that stopped changing state under prefers-reduced-motion
        would be a broken control rather than a calmer one, so only the
        travel is inside the guard."""
        import dashboard

        with open(
            os.path.join(os.path.dirname(dashboard.__file__), "static", "style.css"),
            encoding="utf-8",
        ) as handle:
            css = handle.read()

        guard = css.index("@media (prefers-reduced-motion: no-preference)")
        assert css.index(".switch::before { transition") > guard
        # The state change itself is not conditional on anything.
        assert ".switch:checked { background: var(--switch-on); }" in css


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
        # One page, not all five: the subject here is the sidebar, and five
        # joined copies would make "exactly one of these" true five times.
        return settings_page(test_client).data.decode()

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
        # Settings' own target is its first child's: the section is a
        # disclosure and has no page of its own to point at.
        for endpoint in ("", "/settings/verification", "/subscription"):
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
        assert guild_icon, "no server icon in the sidebar"
        # Measured against the section glyphs as they are actually drawn, not
        # against a 16 written here. The old version compared to that literal,
        # so it could not notice the section icons changing size -- which is
        # the entire comparison its name makes.
        marks = [int(w) for w in re.findall(r'<svg[^>]*width="(\d+)"', sidebar)]
        assert marks, "no section glyphs in the sidebar"
        assert int(guild_icon.group(1)) > max(marks)

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

        expanded = settings_page(test_client).data.decode()
        assert 'class="layout collapsed"' not in expanded

        test_client.set_cookie("vrcverify_nav", "1", domain="localhost")
        collapsed = settings_page(test_client).data.decode()
        assert 'class="layout collapsed"' in collapsed

    def test_a_collapsed_sidebar_keeps_every_link(self, config, store):
        """A rail, not a removal.

        Hiding the labels is the whole effect. If the links themselves went,
        the sidebar would be unusable by keyboard and there would be no way
        back to the server list.
        """
        test_client, _api = settings_client(config, store)
        test_client.set_cookie("vrcverify_nav", "1", domain="localhost")
        page = settings_page(test_client).data.decode()

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
        for path in ("/", f"/guild/{GUILD_IN}/settings/verification"):
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


class TestTheGuildsOwnLanguage:
    """The dashboard answering in the language the admin chose for the bot.

    #97 offers three ways to pick a language and calls this one "the most
    consistent with the bot and the least discoverable if it is wrong". Both
    halves are honoured: it decides when nobody has picked, and the picker in
    the bar always beats it.

    The constraint that shaped the implementation is in the issue too -- the
    dashboard holds no database credential, so the locale has to arrive in a
    payload it already receives. Settings and Subscriptions both fetch
    `settings`, which carries `instructions_locale`. Overview fetches a
    different endpoint and does not.
    """

    def _german(self):
        return make_settings(values={"instructions_locale": "de"})

    def test_the_settings_page_answers_in_the_servers_language(self, config, store):
        test_client, _api = settings_client(config, store, settings=self._german())
        page = settings_page(test_client).data.decode()
        assert 'lang="de"' in page

    def test_the_subscription_page_answers_in_it_too(self, config, store):
        """The page #97 cares about most: "excludes tax" and "renews on" are
        where a misunderstanding turns into a chargeback."""
        test_client, _api = settings_client(config, store, settings=self._german())
        page = test_client.get(f"/guild/{GUILD_IN}/subscription").data.decode()
        assert 'lang="de"' in page
        assert "Abonnements" in page

    def test_it_is_remembered_so_overview_agrees_with_the_other_two(self, config, store):
        """Overview reads a different bot endpoint and never sees the field.

        Without the cookie an admin whose server is configured in German would
        get German on two of their three pages and English on the third. One
        language per browser, decided the first time we are in a position to
        decide it.
        """
        test_client, _api = settings_client(config, store, settings=self._german())
        response = settings_page(test_client)
        assert "vrcverify_lang=de" in response.headers.get("Set-Cookie", "")
        # The cookie is now on the client, so Overview renders from it.
        assert 'lang="de"' in test_client.get(f"/guild/{GUILD_IN}").data.decode()

    def test_the_picker_beats_the_servers_language(self, config, store):
        """An explicit choice always wins. This is #97's own answer to the
        discoverability objection it raises against this mechanism."""
        test_client, _api = settings_client(config, store, settings=self._german())
        test_client.set_cookie("vrcverify_lang", "ja")
        assert 'lang="ja"' in settings_page(test_client).data.decode()

    def test_a_language_the_dashboard_cannot_render_falls_through(self, config, store):
        """Two hosts, two deploys: a bot running ahead of the dashboard is a
        normal state, and the honest answer to a language we have no catalogue
        for is the next choice down, not an error."""
        test_client, _api = settings_client(
            config, store, settings=make_settings(values={"instructions_locale": "xx-XX"})
        )
        response = settings_page(test_client)
        assert response.status_code == 200
        assert 'lang="en-US"' in response.data.decode()
        assert "vrcverify_lang" not in response.headers.get("Set-Cookie", "")

    def test_it_costs_no_extra_bot_call(self, config, store):
        """#97: the locale arrives in a payload the dashboard already
        receives, and is never a new lookup it performs for itself. A route
        asking the bot a second time to find out what language to say "Renews
        on" in is exactly what the issue rules out."""
        test_client, api = settings_client(config, store, settings=self._german())
        before = len(api.reads)
        settings_page(test_client)
        after_localised = len(api.reads) - before

        plain_client, plain_api = settings_client(config, store, settings=make_settings())
        before = len(plain_api.reads)
        settings_page(plain_client)
        assert len(plain_api.reads) - before == after_localised


class TestTheLanguagePicker:
    """`/prefs/lang`, and the control that posts to it (issue #97).

    The dashboard spoke English while the bot spoke twelve languages. Since
    #65 moved configuration here and #88 put a payment page here, the two
    things a non-English-speaking admin has to do were both done in English.

    The route is the SECOND POST in this app requiring neither a session nor a
    CSRF token, and the case is stronger than the theme picker's: a picker you
    can only reach after navigating a page you cannot read is most of the way
    to not having one. The tests below pin both halves -- that it works
    without them, and that dropping them bought nothing an attacker wants.
    """

    @staticmethod
    def _current(page: str):
        """Which language the picker marks as in force, by aria-current.

        Scoped to `name="lang"`: the theme picker in the same bar marks its
        current option the same way.
        """
        match = re.search(
            r'<button[^>]*name="lang"[^>]*aria-current="true"[^>]*>', page
        )
        return re.search(r'value="([\w-]+)"', match.group(0)).group(1) if match else None

    # --- the control itself ---

    def test_it_offers_all_twelve_and_needs_no_session(self, client):
        page = client.get("/").data.decode()
        for code in i18n.UI_LANGUAGES:
            assert f'name="lang" value="{code}"' in page

    def test_it_is_on_the_signed_out_page(self, client):
        """The page this feature exists for. Somebody who cannot read English
        arrives here first, and "Sign in with Discord" is not a sentence they
        should have to parse before the site offers to speak to them."""
        page = client.get("/").data.decode()
        assert "Sign in with Discord" in page
        assert 'action="/prefs/lang"' in page

    def test_each_option_is_named_in_its_own_language(self):
        """A menu labelled "Japanese" is no use to somebody looking for the
        word they would recognise."""
        assert i18n.ENDONYMS["ja"] == "\u65e5\u672c\u8a9e"
        assert i18n.ENDONYMS["de"] == "Deutsch"

    def test_each_option_carries_its_own_lang_attribute(self, client):
        """Twelve languages rendered inside one page. Without this a screen
        reader announces every one of them in the page's own voice."""
        page = client.get("/").data.decode()
        assert 'name="lang" value="ja" lang="ja"' in page

    def test_it_needs_no_javascript(self, client):
        """A <details> opens it, a submit button applies it -- the same
        bargain every other control in this bar strikes."""
        page = client.get("/").data.decode()
        form = re.search(
            r'<form[^>]*action="/prefs/lang".*?</form>', page, re.S
        ).group(0)
        assert "onclick" not in form and "javascript:" not in form

    # --- the route ---

    def test_choosing_a_language_sets_the_cookie_with_no_session(self, client):
        response = client.post("/prefs/lang", data={"lang": "ja"})
        assert response.status_code == 302
        assert "vrcverify_lang=ja" in response.headers["Set-Cookie"]

    def test_the_cookie_is_secure_and_same_site_but_not_httponly(self, client):
        """Not httponly, like the theme cookie: the CSP has no `connect-src`,
        so a future instant picker cannot ask the server to write it."""
        header = client.post("/prefs/lang", data={"lang": "de"}).headers["Set-Cookie"]
        assert "Secure" in header
        assert "SameSite=Lax" in header
        assert "HttpOnly" not in header

    def test_an_unrecognised_language_changes_nothing(self, client):
        """The picker only ever offers the twelve, so the only reachable cause
        is a hand-built request. The honest answer to one of those is the page
        they asked to go back to -- not an error page."""
        response = client.post("/prefs/lang", data={"lang": "../../etc/passwd"})
        assert response.status_code == 302
        assert "vrcverify_lang" not in response.headers.get("Set-Cookie", "")

    def test_english_IS_stored_unlike_the_theme_default(self, client):
        """The one place this diverges from the theme and sidebar cookies, and
        deliberately. For them "default" is the absence of a cookie, so one
        state has one representation. Here absent means "nobody has chosen",
        which is what lets the guild's language have its say -- so a German
        admin who deliberately picks English has to be able to say so."""
        header = client.post("/prefs/lang", data={"lang": "en-US"}).headers["Set-Cookie"]
        assert "vrcverify_lang=en-US" in header

    def test_it_never_reaches_the_bot(self, app, client):
        """A preference toggle that could be pointed at the bot API would be
        an unauthenticated way to spend its rate limit."""
        api = app.config["BOT_API"]
        before = (len(api.reads), len(api.calls), len(api.saves))
        client.post("/prefs/lang", data={"lang": "ja"})
        assert (len(api.reads), len(api.calls), len(api.saves)) == before

    # --- what the page then renders ---

    def test_the_chosen_language_reaches_the_html_tag(self, client):
        """`lang="en"` was a lie on a page rendered in Japanese, and not a
        harmless one: it is what a screen reader picks a voice from."""
        client.set_cookie("vrcverify_lang", "ja")
        page = client.get("/").data.decode()
        assert 'lang="ja"' in page
        assert self._current(page) == "ja"

    def test_arabic_sets_dir_rtl_and_the_rest_do_not(self, client):
        """Shipped before the stylesheet mirrors, on purpose: `dir` governs
        the reading order of the text itself, which is a different job from
        the layout. Tracked separately."""
        client.set_cookie("vrcverify_lang", "ar")
        assert 'dir="rtl"' in client.get("/").data.decode()
        client.set_cookie("vrcverify_lang", "ja")
        assert 'dir="ltr"' in client.get("/").data.decode()

    def test_the_page_is_actually_translated_not_merely_labelled(self, client):
        """The failure this whole feature could have: a catalogue that loads,
        reports success and serves the English for everything."""
        client.set_cookie("vrcverify_lang", "de")
        page = client.get("/").data.decode()
        assert "Darstellung" in page  # the theme menu's label
        assert "Appearance" not in page

    def test_a_hand_edited_cookie_cannot_reach_the_lang_attribute(self, client):
        """The chosen code ends up in an attribute and in a filesystem path,
        so nothing reaches either without being found in UI_LANGUAGES."""
        client.set_cookie("vrcverify_lang", '"><script>alert(1)</script>')
        page = client.get("/").data.decode()
        assert 'lang="en-US"' in page
        assert "<script>alert(1)</script>" not in page

    def test_accept_language_is_honoured_with_no_cookie_at_all(self, client):
        """The sign-in page has no guild and no cookie, and is the first page
        anybody sees."""
        page = client.get("/", headers={"Accept-Language": "de-DE,de;q=0.9"}).data.decode()
        assert 'lang="de"' in page


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
        """Which option the THEME picker marks as in force, by aria-current.

        Scoped to `name="theme"` since #97. The language picker in the same bar
        also marks its current option with `aria-current`, and it renders first
        -- so an unscoped search for the attribute finds the language, not the
        theme, and reports whichever of the twelve is in force.
        """
        match = re.search(
            r'<button[^>]*name="theme"[^>]*aria-current="true"[^>]*>', page
        )
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
        """All four wear .bar-menu, which is what prefs.js dismisses. Two
        menus styled two ways is how a bar ends up with four popovers that
        each close differently -- so #136's bell joined the pattern rather
        than bringing a fifth, and #97's language picker joined it rather
        than bringing a fifth of its own.

        Four since #97: the bell, the language picker, the theme picker, the
        account menu, in that order left to right.
        """
        login_as(client, store)
        page = client.get("/").data.decode()
        assert page.count("bar-menu") == 4
        # One more `bar-panel` than `bar-menu`, because the language picker's
        # panel carries `bar-panel-tall` as well: twelve rows is past the
        # height the other three were sized for. Same pattern, one modifier.
        assert page.count("bar-panel") == 5

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


class TestTheChartGeometry:
    """#135 phase 2. Every coordinate the SVG draws, computed without a
    request or a template -- the same reasoning `Tile` is built on, one level
    up: a chart is thirty tiles, and it must not flatten any of their states
    into each other either.
    """

    @staticmethod
    def _series(counts) -> list:
        """`counts` is 30 values, oldest first: an int for a measured day, or
        None for a day before the collection floor. Day labels are synthetic
        -- the geometry cares about order and None-vs-int, not real dates."""
        assert len(counts) == 30, "the bot always returns exactly 30 entries"
        return [{"day": f"d{i}", "count": c} for i, c in enumerate(counts)]

    @staticmethod
    def _overview(daily, known=True, collecting_since="2026-07-01"):
        return {
            "verifications": {
                "daily": daily,
                "known": known,
                "collecting_since": collecting_since,
            }
        }

    # --- the three states ---

    def test_no_overview_at_all_is_unknown(self):
        chart = overview_view.build_chart(None)
        assert chart.state == "unknown"
        assert chart.bars == []

    def test_a_failed_rollup_read_is_unknown_not_blank(self):
        """Mirrors `_window_tile`'s `known` check from the same payload flag,
        so the tiles and the chart can never disagree about whether the read
        itself succeeded."""
        overview = self._overview(daily=None, known=False)
        chart = overview_view.build_chart(overview)
        assert chart.state == "unknown"

    def test_every_day_unmeasured_is_blank_not_unknown(self):
        """A fleet that has never collected anything, anywhere -- a
        successful read of a question the data cannot answer yet. Different
        from the bot failing, and the copy has to say a different thing."""
        overview = self._overview(self._series([None] * 30), collecting_since=None)
        chart = overview_view.build_chart(overview)
        assert chart.state == "blank"
        assert chart.bars == []
        assert "not collecting" in chart.note.lower()

    def test_the_blank_note_names_the_collection_start_when_known(self):
        overview = self._overview(self._series([None] * 30))
        chart = overview_view.build_chart(overview)
        # Formatted, not the raw ISO the bot sent -- see the tile version of
        # this assertion in TestOverviewViewModel (#230).
        assert "July 1, 2026" in chart.note
        assert "2026-07-01" not in chart.note

    # --- the issue's named edge cases ---

    def test_an_empty_series_is_blank_and_does_not_crash(self):
        """Defensive: the bot never actually returns an empty list -- 30
        entries or None -- but the geometry loop divides by the day count, and
        a future change to DAILY_SERIES_DAYS should not be able to reintroduce
        a division by zero here."""
        chart = overview_view.build_chart(self._overview([]))
        assert chart.state == "blank"
        assert chart.bars == []

    def test_all_zeros_still_draws_bars_at_the_floor_height(self):
        """THE FALSY-ZERO BUG ONE LEVEL UP. Every day measured and quiet is
        real data -- a panel is up and nobody is using it -- and must be
        drawn, not folded into "blank" because every value happens to be
        zero."""
        chart = overview_view.build_chart(self._overview(self._series([0] * 30)))
        assert chart.state == "value"
        assert len(chart.bars) == 30
        assert all(bar.height == overview_view.CHART_MIN_BAR_HEIGHT for bar in chart.bars)
        # And still real bars, not gaps -- a reader can tell "measured, zero"
        # from "not measured" only because something is drawn here at all.
        assert all(bar.count == 0 for bar in chart.bars)

    def test_a_single_measured_day_leaves_the_rest_as_gaps(self):
        counts = [None] * 29 + [4]
        chart = overview_view.build_chart(self._overview(self._series(counts)))

        with_bars = [bar for bar in chart.bars if bar.height is not None]
        without = [bar for bar in chart.bars if bar.height is None]
        assert len(with_bars) == 1 and len(without) == 29
        assert with_bars[0].count == 4
        # The lone measured day is also the peak, so it fills the chart.
        assert with_bars[0].height == overview_view.CHART_VIEW_HEIGHT

    def test_a_spike_sets_the_scale_for_every_other_bar(self):
        """The whole reason peak-scaling exists: a quiet day next to a busy
        one must still read as proportionally small, not as its own
        independent 0-to-100 bar."""
        counts = [1] * 29 + [200]
        chart = overview_view.build_chart(self._overview(self._series(counts)))

        spike = chart.bars[-1]
        quiet = chart.bars[0]
        assert spike.count == 200 and quiet.count == 1
        assert spike.height == overview_view.CHART_VIEW_HEIGHT
        # Proportional to the spike, and still cleared by the floor rather
        # than rounding away to nothing.
        expected = max(
            overview_view.CHART_MIN_BAR_HEIGHT,
            (1 / 200) * overview_view.CHART_VIEW_HEIGHT,
        )
        assert quiet.height == pytest.approx(expected)
        assert quiet.height < spike.height

    def test_a_window_straddling_the_floor_mixes_gaps_and_bars(self):
        """The acceptance criterion, stated as geometry: a day with no data
        and a day with zero verifications must be distinguishable -- here,
        because one has no rect and the other has a rect at the floor
        height."""
        counts = [None] * 10 + [0] * 15 + [3] * 5
        chart = overview_view.build_chart(self._overview(self._series(counts)))

        assert chart.state == "value"
        gaps = [bar for bar in chart.bars if bar.height is None]
        zero_bars = [bar for bar in chart.bars if bar.count == 0]
        real_bars = [bar for bar in chart.bars if bar.count == 3]
        assert len(gaps) == 10
        assert len(zero_bars) == 15 and all(
            b.height == overview_view.CHART_MIN_BAR_HEIGHT for b in zero_bars
        )
        assert len(real_bars) == 5 and all(
            b.height == overview_view.CHART_VIEW_HEIGHT for b in real_bars
        )
        # A gap and a zero-height-floor bar are not the same object, so a
        # template that only checks truthiness could still conflate them --
        # pinned explicitly rather than trusted to the height comparison
        # above.
        assert gaps[0].height is None
        assert zero_bars[0].height is not None

    # --- geometry mechanics ---

    def test_bars_are_left_to_right_in_day_order(self):
        chart = overview_view.build_chart(self._overview(self._series([1] * 30)))
        xs = [bar.x for bar in chart.bars]
        assert xs == sorted(xs)
        assert xs[0] == 0

    def test_bars_fit_inside_the_declared_width(self):
        chart = overview_view.build_chart(self._overview(self._series([1] * 30)))
        rightmost = chart.bars[-1].x + chart.bar_width
        assert rightmost <= overview_view.CHART_VIEW_WIDTH

    def test_y_places_the_rect_on_the_baseline(self):
        """SVG y grows downward, so a bar's rect starts at
        (chart height - bar height) and its bottom edge lands on the chart's
        own height -- the x-axis every bar shares."""
        chart = overview_view.build_chart(self._overview(self._series([1] * 30)))
        for bar in chart.bars:
            assert bar.y + bar.height == overview_view.CHART_VIEW_HEIGHT

    def test_another_guilds_scale_cannot_leak_into_this_one(self):
        """Not a real hazard in this pure function -- there is no cross-guild
        state to leak -- but pinned anyway: two independent calls must
        compute two independent peaks."""
        busy = overview_view.build_chart(self._overview(self._series([50] * 30)))
        quiet = overview_view.build_chart(self._overview(self._series([1] * 30)))
        assert busy.bars[0].height == overview_view.CHART_VIEW_HEIGHT
        assert quiet.bars[0].height == overview_view.CHART_VIEW_HEIGHT


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
        # Named as a reader writes a date, not as the bot sent it. This used to
        # assert the raw "2026-08-14" was in the sentence, which is what #230
        # found: an ISO day interpolated into translated prose.
        assert all("August 14, 2026" in tile.note for tile in blanks)
        assert not any("2026-08-14" in tile.note for tile in blanks)

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

    def _configured(self, **overrides):
        base = {
            "verified_role": True,
            "verified_role_exists": True,
            "verified_role_assignable": True,
            "unverified_role": False,
            "log_channel": False,
            "auto_verify": True,
        }
        base.update(overrides)
        return base

    def test_setup_reports_each_optional_piece_as_done_or_off(self):
        setup = overview_view.build_setup(
            {"configured": self._configured(), "panel": {"posted": True}}
        )
        states = {row["label"]: row["state"] for row in setup["rows"]}
        assert states["Auto-verify on join"] == "done"
        assert states["Unverified role"] == "off"
        assert states["Verification log"] == "off"

    def test_a_missing_verified_role_is_todo_not_broken(self):
        """Never configured is a different note from configured-and-broken,
        even though both need the same fix."""
        setup = overview_view.build_setup(
            {"configured": self._configured(verified_role=False,
                                             verified_role_exists=None,
                                             verified_role_assignable=None),
             "panel": {"posted": True}}
        )
        role = next(row for row in setup["rows"] if row["label"] == "Verified role")
        assert role["state"] == "todo"
        assert role["action"] == {
            "label": "Go to Settings",
            # The group as well as the anchor (#140): Settings is five pages
            # and only one of them has a role picker.
            "group": "verification",
            "anchor": "f-role_id",
        }

    def test_a_deleted_verified_role_is_broken_not_todo(self):
        setup = overview_view.build_setup(
            {"configured": self._configured(verified_role_exists=False,
                                             verified_role_assignable=None),
             "panel": {"posted": True}}
        )
        role = next(row for row in setup["rows"] if row["label"] == "Verified role")
        assert role["state"] == "broken"
        assert "deleted" in role["note"]

    def test_an_unassignable_verified_role_is_broken(self):
        """The silent-failure case: the role exists and looks configured, but
        the bot's own role sits below it in the hierarchy."""
        setup = overview_view.build_setup(
            {"configured": self._configured(verified_role_assignable=False),
             "panel": {"posted": True}}
        )
        role = next(row for row in setup["rows"] if row["label"] == "Verified role")
        assert role["state"] == "broken"
        assert "sit above" in role["note"]

    def test_unknown_assignability_is_not_treated_as_broken(self):
        """`None` means the hierarchy could not be checked, not that it
        failed -- crying wolf on a working server is worse than staying
        quiet."""
        setup = overview_view.build_setup(
            {"configured": self._configured(verified_role_assignable=None),
             "panel": {"posted": True}}
        )
        role = next(row for row in setup["rows"] if row["label"] == "Verified role")
        assert role["state"] == "done"

    def test_a_panel_in_a_deleted_channel_is_broken_not_done(self):
        setup = overview_view.build_setup(
            {"configured": self._configured(),
             "panel": {"posted": True, "channel_exists": False}}
        )
        panel = next(row for row in setup["rows"] if row["label"] == "Instructions panel")
        assert panel["state"] == "broken"
        assert "deleted" in panel["note"]

    def test_a_panel_the_bot_cannot_post_to_is_broken(self):
        setup = overview_view.build_setup(
            {"configured": self._configured(),
             "panel": {"posted": True, "channel_exists": True, "channel_postable": False}}
        )
        panel = next(row for row in setup["rows"] if row["label"] == "Instructions panel")
        assert panel["state"] == "broken"

    def test_complete_requires_both_the_role_and_the_panel(self):
        role_missing = overview_view.build_setup(
            {"configured": self._configured(verified_role=False,
                                             verified_role_exists=None,
                                             verified_role_assignable=None),
             "panel": {"posted": True}}
        )
        assert role_missing["complete"] is False

        # The role alone is not enough -- a done role beside an unposted
        # panel must not read as complete either.
        panel_missing = overview_view.build_setup(
            {"configured": self._configured(), "panel": {"posted": False}}
        )
        assert panel_missing["complete"] is False

        complete = overview_view.build_setup(
            {"configured": self._configured(), "panel": {"posted": True}}
        )
        assert complete["complete"] is True

    def test_complete_does_not_require_the_optional_rows(self):
        """An admin who leaves auto-verify or the log channel off has not
        left anything unfinished -- those are choices, not debts."""
        setup = overview_view.build_setup(
            {"configured": self._configured(auto_verify=False, log_channel=False),
             "panel": {"posted": True}}
        )
        assert setup["complete"] is True

    def test_optional_rows_never_carry_an_action(self):
        """No nagging about a deliberately-unset optional feature."""
        setup = overview_view.build_setup(
            {"configured": self._configured(unverified_role=False, log_channel=False),
             "panel": {"posted": True}}
        )
        optional = [row for row in setup["rows"] if row["label"] not in
                    ("Verified role", "Instructions panel")]
        assert all(row["action"] is None for row in optional)

    def test_a_failed_settings_read_shows_no_setup_section(self):
        """Better silent than reporting every row as switched off."""
        assert overview_view.build_setup({"configured": None}) is None

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

    def test_a_failed_settings_read_returns_no_next_step(self):
        """Mirrors build_setup's own silence -- guessing from half the
        checks would be worse than matching it."""
        assert overview_view.build_next_step({"configured": None}) is None

    def test_a_broken_required_row_is_a_setup_step_with_its_own_title(self):
        """Deleted, not just unset -- the top banner has to say which,
        reusing the exact note build_setup already wrote for the row."""
        step = overview_view.build_next_step(
            {
                "configured": self._configured(verified_role_exists=False,
                                                verified_role_assignable=None),
                "panel": {"posted": True},
            }
        )
        assert step["title"] == "The verified role needs attention"
        assert "deleted" in step["body"]
        assert step["action"] == "settings"


class TestTheNextStepRanker:
    """#135 phase 4. `build_next_step` ranks three kinds of thing into one
    slot: a setup step always wins, then an undismissed premium changelog
    entry (#136's contract, not yet a real caller), then the data-backed
    demo built from this server's own 30-day count."""

    def _configured(self, **overrides):
        base = {
            "verified_role": True,
            "verified_role_exists": True,
            "verified_role_assignable": True,
            "unverified_role": False,
            "log_channel": False,
            "auto_verify": True,
        }
        base.update(overrides)
        return base

    def _overview(self, **overrides):
        return make_overview(panel={"posted": True}, **overrides)

    CHANGELOG = {"title": "New: branded panels", "body": "...", "action": "subscription"}

    def test_setup_beats_a_changelog_entry_and_a_demo(self):
        overview = self._overview(
            configured=self._configured(verified_role=False,
                                         verified_role_exists=None,
                                         verified_role_assignable=None),
            last_30_days=214,
        )
        step = overview_view.build_next_step(overview, changelog_entry=self.CHANGELOG)
        assert step["title"] == "No verified role is set"

    def test_a_changelog_entry_beats_the_demo(self):
        overview = self._overview(last_30_days=214)
        step = overview_view.build_next_step(overview, changelog_entry=self.CHANGELOG)
        assert step == self.CHANGELOG

    def test_the_demo_appears_when_nothing_outranks_it(self):
        overview = self._overview(last_30_days=214)
        step = overview_view.build_next_step(overview)
        assert step["action"] == "subscription"
        assert "214" in step["body"]

    def test_no_changelog_entry_by_default(self):
        """The parameter exists for #136; nothing supplies one yet, and
        build_next_step must not invent its own."""
        overview = self._overview(last_30_days=214)
        step = overview_view.build_next_step(overview)
        assert step != self.CHANGELOG

    def test_the_demo_is_suppressed_for_a_premium_server(self):
        overview = self._overview(last_30_days=214, premium=True)
        assert overview_view.build_next_step(overview) is None

    def test_the_demo_is_suppressed_for_a_blank_window(self):
        overview = self._overview(last_30_days=None)
        assert overview_view.build_next_step(overview) is None

    def test_the_demo_is_suppressed_when_the_rollup_is_unknown(self):
        overview = self._overview(last_30_days=None, known=False)
        assert overview_view.build_next_step(overview) is None

    def test_the_demo_is_suppressed_for_a_genuine_zero(self):
        """"0 members verified, upgrade to log them" argues against buying,
        not for it -- the issue's own words for why this must stay silent."""
        overview = self._overview(last_30_days=0)
        assert overview_view.build_next_step(overview) is None

    def test_the_demo_names_the_real_count(self):
        overview = self._overview(last_30_days=7)
        step = overview_view.build_next_step(overview)
        assert "7 members verified" in step["body"]

    def test_singular_count_is_grammatical(self):
        overview = self._overview(last_30_days=1)
        step = overview_view.build_next_step(overview)
        assert "1 member verified" in step["body"]

    def test_a_grandfathered_server_leads_with_what_it_keeps(self):
        overview = self._overview(last_30_days=214, grandfathered=True)
        step = overview_view.build_next_step(overview)
        assert step["title"] == "Add VRCVerify Premium"
        assert "stay free" in step["body"]
        assert "214" in step["body"]


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
            # Putting one premium changelog card away, for one server (#136
            # phase 4). One cookie again, and the only route here that takes
            # an id from the form -- which card was on screen is something
            # only the page knows. It is checked against the shipped entry ids
            # rather than trusted, so a crafted post changes nothing and
            # cannot fill a bounded cookie with pairs that match nothing.
            "/prefs/dismiss",
            # The bell's "Mark all as read" (#136). One cookie, nothing else:
            # no session state, no bot call, and the value written is the
            # newest entry id this process already knows rather than anything
            # from the form -- so a forged post cannot mark an entry seen that
            # the browser never saw. Session and CSRF required, following
            # /prefs/nav rather than /prefs/theme: the bell renders only for a
            # signed-in admin, so there is always a token to require.
            "/prefs/seen",
            # The two that spend money, or end a subscription. Neither writes
            # anything here: each creates a session on Stripe and hands the
            # browser over, so the whole of what they can do is bounded by what
            # Stripe's hosted pages allow.
            "/guild/<int:guild_id>/subscription/checkout",
            "/guild/<int:guild_id>/subscription/portal",
            # Which of the twelve languages to render in (#97). One cookie,
            # nothing else, and the second route here to require neither a
            # session nor a CSRF token -- for a stronger version of the theme
            # picker's reason. This feature exists for people who cannot read
            # English, so a picker reachable only from behind a sign-in page
            # they cannot read would be most of the way to not existing. The
            # submitted value is checked against the twelve before it can reach
            # a `lang` attribute or a catalogue path.
            "/prefs/lang",
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
        return every_settings_page(test_client)

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
        html = settings_page(test_client, "vrchat-group").data.decode()

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


class TestTheSharedTypeScaleAndMeasure(object):
    """One ramp across two stylesheets, and a line a reader can follow (#195 p1)."""

    @staticmethod
    def _css(which: str) -> str:
        import dashboard

        if which == "dashboard":
            path = os.path.join(
                os.path.dirname(dashboard.__file__), "static", "style.css"
            )
        else:
            # From this file, not from a directory name: the repo is not
            # always cloned into a folder called "VRCVerify".
            repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            path = os.path.join(repo, "site", "style.css")
        with open(path, encoding="utf-8") as handle:
            return handle.read()

    def test_the_display_size_is_identical_in_both_stylesheets(self):
        """The ONE type value the two files share outright.

        Every other step differs on purpose -- a console is denser than a
        document -- but the marketing size has to match, because a visitor
        crossing from the landing page to /pricing sees both within one click
        and any difference reads as two products.

        Same bargain as the colour tokens: two origins, two deploys, no shared
        stylesheet possible, so the guarantee is a test rather than an import.
        """
        pattern = r"--text-display:\s*([^;]+);"
        here = re.search(pattern, self._css("dashboard"))
        there = re.search(pattern, self._css("site"))
        assert here and there, "a stylesheet has stopped declaring --text-display"
        assert here.group(1).strip() == there.group(1).strip(), (
            "the marketing size has drifted between the two files: "
            f"dashboard {here.group(1)!r} vs site {there.group(1)!r}"
        )

    def test_both_stylesheets_hold_prose_to_a_measure(self):
        """A count of characters, not a width.

        #195 reported this site at 78 characters and the dashboard at 120.
        Both numbers were wrong -- the metric divided text length by rendered
        line count, which `-webkit-line-clamp` and any author-placed <br> break
        independently. Measured properly, both surfaces ran long and the apex
        site ran longer on some pages.
        """
        for which in ("dashboard", "site"):
            found = re.search(r"--measure:\s*(\d+)ch;", self._css(which))
            assert found, f"{which} declares no --measure"
            # Not the character count: 1ch is the advance of "0", much wider
            # than average lowercase, so the nominal always undershoots what
            # renders. Tuned against the rendered result -- see the comment.
            assert 45 <= int(found.group(1)) <= 65, (
                f"{which} --measure is {found.group(1)}ch, outside the band "
                "that renders inside the 45-75 norm"
            )

    def test_the_page_title_size_is_not_written_twice(self):
        """`h1` hardcoded 1.4rem while --text-page-title said the same thing.

        Two places to change and one of them silently disagreeing is the whole
        failure mode a token layer exists to remove.
        """
        css = self._css("dashboard")
        rule = re.search(r"\nh1 \{([^}]*)\}", css)
        assert rule, "the dashboard has no bare h1 rule"
        assert "var(--text-page-title)" in rule.group(1), (
            "h1 does not draw its size from the token that names it"
        )

    def test_prose_takes_the_measure_but_controls_do_not(self):
        """A label, a table cell and a stat tile are not prose.

        Clamping them would leave dead space where an element is meant to fill
        its column, so the rule is scoped to the prose classes rather than
        applied to every element inside a card.
        """
        css = self._css("dashboard")
        assert re.search(r"\.panel \.blurb,\s*\n\.panel \.desc,\s*\n\.panel > p \{[^}]*var\(--measure\)", css), (
            "the dashboard's prose classes do not take the measure"
        )


class TestThePageHeaderIsNotACard(object):
    """The guild-identity slab, replaced (#195 phase 4)."""

    def test_overview_no_longer_carries_its_own_copy_of_the_guild_head(self):
        """`_guild_head.html` exists so the premium sentence has one home, and
        Overview kept a byte-identical duplicate of it three files away -- the
        exact thing the partial was created to prevent.

        Asserted on the templates rather than the rendered page, because the
        defect is a maintenance one: two copies render the same today and
        diverge the day one is edited.
        """
        import dashboard

        templates = os.path.join(os.path.dirname(dashboard.__file__), "templates")
        overview = open(
            os.path.join(templates, "overview.html"), encoding="utf-8"
        ).read()
        assert '{% include "_guild_head.html" %}' in overview
        assert "VRCVerify Premium is active on this server" not in overview, (
            "Overview has a second copy of the premium sentence again"
        )

    def test_the_guild_head_is_not_wrapped_in_a_card(self):
        """A card is for content that groups; a page title is not content.
        The slab cost ~110px above the content on every guild page."""
        import dashboard

        templates = os.path.join(os.path.dirname(dashboard.__file__), "templates")
        for name in ("overview.html", "settings.html", "activity.html"):
            body = open(os.path.join(templates, name), encoding="utf-8").read()
            before = body[: body.index('{% include "_guild_head.html" %}')]
            # The last section opened before the include must have been closed.
            assert before.count("<section") == before.count("</section>"), (
                f"{name} still opens a card around the page header"
            )

    def test_the_guild_head_does_not_repeat_the_sidebar_icon(self):
        """`.side-guild` shows the same icon and name at every width -- below
        the breakpoint the sidebar becomes a tab strip and the identity block
        stays, shrunk to 32px. A second, larger copy was the slab's bulk."""
        import dashboard

        templates = os.path.join(os.path.dirname(dashboard.__file__), "templates")
        head = open(
            os.path.join(templates, "_guild_head.html"), encoding="utf-8"
        ).read()
        assert "guild_icon" not in head, (
            "the page header draws the server icon a second time"
        )

    def test_the_notices_kept_the_card_the_header_gave_up(self):
        """Measured, not inherited. `.notice.ok` draws no fill, so it takes its
        contrast from what is behind it: --ok on --bg is 4.27:1 in the light
        theme, under AA, against 5.39:1 on --panel.

        The stylesheet already said so next to `.notice.ok` -- filling it was
        rejected for the same reason. Letting the notices follow the header
        onto the page ground would have reintroduced that failure in a
        different shape.
        """
        import dashboard

        templates = os.path.join(os.path.dirname(dashboard.__file__), "templates")
        for name in ("settings.html", "activity.html"):
            body = open(os.path.join(templates, name), encoding="utf-8").read()
            first_notice = body.index('class="notice')
            before = body[:first_notice]
            assert before.count("<section") > before.count("</section>"), (
                f"{name} renders a notice on the page ground, where --ok is "
                "4.27:1 in light"
            )


class TestSettingsRowsAreRows(object):
    """Label left, control right (#195 phase 5)."""

    @staticmethod
    def _css() -> str:
        import dashboard

        with open(
            os.path.join(os.path.dirname(dashboard.__file__), "static", "style.css"),
            encoding="utf-8",
        ) as handle:
            return handle.read()

    def test_the_two_column_row_is_above_the_breakpoint_only(self):
        """Two columns on a 390px phone leaves the control about 40% of the
        width, which is narrower than a select needs for a role name. The rows
        stay stacked there."""
        css = self._css()
        grid = re.search(
            r"@media \(min-width: 48rem\) \{\s*\.setting \{([^}]*)\}", css
        )
        assert grid, "the settings row grid is not behind a min-width query"
        assert "grid-template-columns" in grid.group(1)

        # And nothing outside that query turns `.setting` into a grid.
        outside = css[: css.index("@media (min-width: 48rem)")]
        bare = re.search(r"\n\.setting \{([^}]*)\}", outside)
        assert bare, "the base .setting rule has gone"
        assert "display: grid" not in bare.group(1), (
            "the row is a grid at every width, including a phone"
        )

    def test_the_description_did_not_move_into_the_accessible_name(self):
        """The references put the description in the left column beside the
        label. Doing that here means putting it inside the <dt> -- and
        `aria-labelledby` points at the <dt>, so every control's accessible
        name would become "Verified role. Granted once a member's VRChat
        account is confirmed as 18+."

        Worth fixing properly with `aria-describedby` and a split label. Not
        worth bundling into a layout change, where the mistake is invisible to
        everyone who can see the page. This pins the decision so a later phase
        does it deliberately rather than by accident.
        """
        import dashboard

        templates = os.path.join(os.path.dirname(dashboard.__file__), "templates")
        body = open(
            os.path.join(templates, "settings.html"), encoding="utf-8"
        ).read()
        # Find the field row's <dt>...</dt> and prove the description is not in it.
        row = re.search(r'<dt id="l-\{\{ field\.name \}\}">(.*?)</dt>', body, re.S)
        assert row, "the field label row has changed shape"
        assert "field.description" not in row.group(1), (
            "the description is now inside the element aria-labelledby points "
            "at, which folds it into every control's accessible name"
        )
        assert "{{ field.description }}" in body, "the description has vanished"


class TestTheFooterIsABar(object):
    """Chrome that aligns with the other chrome (#195 phase 6)."""

    @staticmethod
    def _repo() -> str:
        return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    def _css(self) -> str:
        import dashboard

        with open(
            os.path.join(os.path.dirname(dashboard.__file__), "static", "style.css"),
            encoding="utf-8",
        ) as handle:
            return handle.read()

    def test_the_footer_has_a_ground_and_an_edge(self):
        """It was bare text on the page ground at `max-width: 78rem` while
        `main` sits at 60rem, so at 1280px it started at x=16 and the content
        column started at x=296 -- aligned with neither."""
        rule = re.search(r"\nfooter \{([^}]*)\}", self._css())
        assert rule, "the footer rule has gone"
        body = rule.group(1)
        assert "background: var(--chrome)" in body, "the footer is not a bar"
        assert "border-top" in body, "the footer has no edge"
        assert "max-width" not in body, (
            "the footer centres itself again, which is what made it align "
            "with neither the content nor the header"
        )

    def test_the_layout_still_fills_its_width_now_that_body_is_a_column(self):
        """THE TRAP THIS PHASE FELL INTO. A flex item with `auto` margins on
        the cross axis does not stretch -- the auto margins win and it is sized
        to fit-content. `.layout` carries `margin: 0 auto`, so making body a
        column shrank it from 78rem to its content width and moved the whole
        app 81px right.

        Nothing failed. It just rendered narrower, which is why this is pinned
        rather than left to be noticed.
        """
        css = self._css()
        body_rule = re.search(r"\nbody \{([^}]*)\}", css)
        assert body_rule, "the body rule has gone"
        if "flex-direction: column" not in body_rule.group(1):
            return  # the footer is held down some other way; the trap is moot
        layout = re.search(r"\n\.layout \{([^}]*)\}", css)
        assert layout, "the layout rule has gone"
        assert "width: 100%" in layout.group(1), (
            "body is a flex column and .layout has auto margins, so without an "
            "explicit width it collapses to fit-content"
        )

    def test_the_dashboard_names_the_same_seller_as_the_apex_site(self):
        """Two copies, compared rather than trusted -- the same bargain as the
        design tokens and the vendored font.

        `tests/test_site.py` requires the seller on every apex page because
        Stripe and Discord both link there and a page that stops naming the
        seller is a compliance problem. That reasoning did not reach this host
        while every page was behind OAuth; /pricing is public and quotes prices
        in US dollars, so it does now.
        """
        import dashboard

        base = open(
            os.path.join(os.path.dirname(dashboard.__file__), "templates", "base.html"),
            encoding="utf-8",
        ).read()
        terms = open(
            os.path.join(self._repo(), "site", "terms.html"), encoding="utf-8"
        ).read()

        entity = re.search(r"operated by ([^,]+), ([^.<]+)", terms)
        assert entity, "the apex site no longer names its operator"
        assert f"operated by {entity.group(1)}" in base, (
            f"the dashboard does not name {entity.group(1)!r} as the seller"
        )

    def test_the_disclaimer_appears_on_both_hosts(self):
        """A disclaimer that appears on one of a product's two sites is a
        disclaimer with a hole in it -- and the hole was the host that takes
        the money."""
        import dashboard

        base = open(
            os.path.join(os.path.dirname(dashboard.__file__), "templates", "base.html"),
            encoding="utf-8",
        ).read()
        for name in ("VRChat Inc.", "Discord Inc.", "Not affiliated with"):
            assert name in base, f"the dashboard footer does not disclaim {name}"

    def test_a_signed_out_visitor_sees_the_seller(self, config):
        """The whole reason this moved: /pricing is public and quotes prices,
        and the footer's legal block is CSRF-gated only for "What's new"."""
        from dashboard.sessions import SessionStore

        store = SessionStore(config.session_db_path, config.session_max_age)
        app = create_app(config, store=store, client=FakeBotAPI())
        app.config.update(TESTING=True)
        page = app.test_client().get("/pricing").data.decode()
        # Stripe is off in this config, so the page shows no plans -- which is
        # the point: the seller has to be named even on the state of the page
        # that has nothing to sell.
        assert "Esatto Technologies" in page
        assert "Not affiliated with" in page


class TestTheChartNamesItsScale(object):
    """A sparse month has to read as a chart, not a rule (#195 phase 8)."""

    @staticmethod
    def _series(counts):
        from datetime import timedelta

        start = date(2026, 8, 1)
        return [
            {"day": (start + timedelta(days=i)).isoformat(), "count": c}
            for i, c in enumerate(counts)
        ]

    def _overview(self, daily):
        return {
            "verifications": {
                "known": True,
                "daily": daily,
                "collecting_since": "2026-08-01",
            }
        }

    def test_the_peak_is_the_busiest_measured_day(self):
        """Bars are drawn against it, so without naming it a reader has heights
        and no units -- one tall bar beside a row of floor-height slivers is
        the shape of a quiet month and of a broken chart at the same time."""
        chart = overview_view.build_chart(self._overview(self._series([0, 3, 1, 0])))
        assert chart.peak == 3

    def test_an_unmeasured_day_is_not_a_zero_when_taking_the_peak(self):
        """A day before the collection floor is a gap. Treating None as 0 would
        be the same conflation `bar.height is not none` exists to prevent, one
        level up."""
        chart = overview_view.build_chart(self._overview(self._series([None, 2, None])))
        assert chart.peak == 2

    def test_a_window_with_nothing_measured_has_no_peak(self):
        """"Peak 0" on a month of gaps is a number pretending to be a reading.
        The template omits the label entirely rather than printing one."""
        chart = overview_view.build_chart(self._overview(self._series([None] * 30)))
        assert chart.peak is None

    def test_a_measured_month_of_zeroes_still_has_a_peak_of_zero(self):
        """Distinct from the case above, and the distinction is the whole
        point: this server was measured and did nothing, which is a reading."""
        chart = overview_view.build_chart(self._overview(self._series([0] * 30)))
        assert chart.peak == 0

    def test_the_peak_matches_the_tallest_bar_that_is_drawn(self):
        """Read off the bars rather than the payload, so it cannot disagree
        with the tallest thing on screen."""
        chart = overview_view.build_chart(self._overview(self._series([1, 9, 4])))
        tallest = max(b for b in (bar.height for bar in chart.bars) if b is not None)
        peak_bar = next(bar for bar in chart.bars if bar.count == chart.peak)
        assert peak_bar.height == tallest

    def test_the_chart_has_a_baseline_and_the_label_escapes_the_measure(self):
        """Two CSS facts this phase depends on.

        The baseline is on the container, not in the SVG: `preserveAspectRatio`
        is `none`, so a stroke inside the viewBox would stretch with it.

        And `.chart-scale` is a label, not prose -- clamped to the measure it
        right-aligned inside a 490px box in the middle of a 900px card. That is
        the third time a structural `<p>` selector caught something that is not
        a sentence; the apex site's trust strip and flow note were the others.
        """
        import dashboard

        with open(
            os.path.join(os.path.dirname(dashboard.__file__), "static", "style.css"),
            encoding="utf-8",
        ) as handle:
            css = handle.read()
        assert re.search(r"\n\.chart \{ border-bottom: 1px solid var\(--line\); \}", css), (
            "the chart has no baseline"
        )
        assert ".panel > .chart-scale { max-width: none; }" in css, (
            "the axis label is clamped to the prose measure"
        )


def test_the_server_name_is_not_announced_twice_on_a_phone():
    """Deferred from #195 phase 4 and closed here.

    Below the breakpoint the sidebar is a tab strip stacked directly above the
    content, and phase 4 moved the page title onto the ground -- so the server
    name appeared twice within about 300px. On a desktop the two sit in
    different columns and read as nav plus title; stacked, they read as a
    repeat.

    The ICON stays, so the strip still says which server without saying it
    again in words, and the <h1> below is the accessible name either way. This
    hides a duplicate label, never the only one -- which is why the rule has to
    be inside the media query and nowhere else.
    """
    import dashboard

    with open(
        os.path.join(os.path.dirname(dashboard.__file__), "static", "style.css"),
        encoding="utf-8",
    ) as handle:
        css = handle.read()

    hide = ".side-guild .side-guild-name { display: none; }"
    assert hide in css, "the duplicate server name is not hidden anywhere"

    # It must be inside a max-width query: hiding it on the desktop would take
    # the name out of the sidebar, where it is the only one.
    before = css[: css.index(hide)]
    opened = before.count("@media (max-width")
    # every media block that opened before this point and has not closed
    depth = before.count("{") - before.count("}")
    assert opened and depth > 0, (
        "the rule is not inside a media query, so the sidebar loses its name "
        "at every width"
    )


def test_the_two_stylesheets_agree_on_tight_leading_and_explain_the_body_one():
    """#195's last acceptance criterion: every measured difference between the
    two hosts either agrees or has its reason written next to the rule.

    `--leading-tight` is the same in both because a heading is a heading.
    `--leading-body` is not, because a console is operated and a document is
    read -- and that sentence has to exist in both files, or the next person to
    compare them finds two numbers and no argument.
    """
    import dashboard

    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    dash = open(
        os.path.join(os.path.dirname(dashboard.__file__), "static", "style.css"),
        encoding="utf-8",
    ).read()
    site = open(os.path.join(repo, "site", "style.css"), encoding="utf-8").read()

    def token(css, name):
        found = re.search(rf"{name}:\s*([0-9.]+);", css)
        return found.group(1) if found else None

    assert token(dash, "--leading-tight") == token(site, "--leading-tight"), (
        "the two files disagree about heading leading, which nothing justifies"
    )
    assert token(dash, "--leading-body") != token(site, "--leading-body"), (
        "the body leading now matches; delete the notes explaining why it does "
        "not, rather than leaving two files arguing for a difference that is "
        "no longer there"
    )
    for css, name in ((dash, "dashboard"), (site, "site")):
        window = css[max(0, css.index("--leading-body") - 700):css.index("--leading-body")]
        assert "console" in window and "document" in window, (
            f"{name}'s --leading-body differs from the other host's with no "
            "reason written beside it"
        )


class TestTwoLanguagesAtOnce:
    """A request in one language must not change the page another is being
    served (#97).

    THIS IS ABOUT `gunicorn --threads 4`, which is what the image runs. Four
    requests share one Flask app, and therefore one Jinja Environment, whose
    `globals` dict is where `_()` is looked up when a render starts.

    The first version of #97 bound the catalogue in `before_request` with
    `install_gettext_translations`, which writes into exactly that dict. The
    window looked small enough to argue away and was not: between the hook and
    the `render_template` in the same request sits the round trip to the bot
    API. A German admin waiting on that call, while a Japanese admin's request
    arrived, was served `<html lang="de">` with 630 Japanese characters in it.

    The fix was to stop writing per request -- the environment gets callables
    once, at `create_app`, that ask `g` which language THIS request is in. See
    the comment above `install_gettext_callables`.

    Overview and the picker are the subjects because they are the routes with
    the gap: Settings and Subscription happen to re-resolve the language after
    the bot answers, which closed the window there and hid the bug.
    """

    def _race(self, app, store, bot_api, slow_method, path):
        german = app.test_client()
        login_as(german, store)
        german.set_cookie("vrcverify_lang", "de")

        japanese = app.test_client()
        login_as(japanese, store)
        japanese.set_cookie("vrcverify_lang", "ja")

        # Stand in for the network. Without a real delay the two requests do
        # not overlap and this test passes against the broken code.
        original = getattr(bot_api, slow_method)

        def slow(*args, **kwargs):
            time.sleep(0.05)
            return original(*args, **kwargs)

        setattr(bot_api, slow_method, slow)

        pages = {}

        def fetch(name, test_client):
            pages[name] = test_client.get(path).get_data(as_text=True)

        first = threading.Thread(target=fetch, args=("de", german))
        second = threading.Thread(target=fetch, args=("ja", japanese))
        first.start()
        time.sleep(0.02)  # the Japanese request arrives mid-call
        second.start()
        first.join()
        second.join()
        return pages

    @staticmethod
    def _japanese_characters(page: str) -> int:
        """How much of this page is not in the language it claims.

        Not zero: the language picker lists every language in its own script,
        so a German page legitimately carries 日本語 and 简体中文. Seven
        characters. Anything past that is the bug.
        """
        body = page[page.index("<body"):] if "<body" in page else page
        return sum(1 for c in body if 0x3000 < ord(c) < 0xA000)

    def test_the_overview_keeps_its_language(self, app, store, bot_api):
        pages = self._race(app, store, bot_api, "overview", "/guild/1")
        assert 'lang="de"' in pages["de"]
        leaked = self._japanese_characters(pages["de"])
        assert leaked <= 12, (
            f"the German admin's Overview carried {leaked} Japanese "
            "characters: another request swapped the shared catalogue"
        )
        # The other side of it, so a fix that simply broke Japanese fails too.
        assert self._japanese_characters(pages["ja"]) > 50

    def test_the_picker_keeps_its_language(self, app, store, bot_api):
        pages = self._race(app, store, bot_api, "admin_guild_ids", "/")
        assert 'lang="de"' in pages["de"]
        leaked = self._japanese_characters(pages["de"])
        assert leaked <= 12, (
            f"the German picker carried {leaked} Japanese characters"
        )
        assert self._japanese_characters(pages["ja"]) > 50


class TestNumbersAndDaysFollowTheLanguage:
    """#230's other half. The Overview is the page with the figures on it, and
    every one of them was written the way American English writes a number."""

    @staticmethod
    def _overview(**counts):
        base = {
            "total": 1234567,
            "today": 1,
            "last_7_days": 12,
            "last_30_days": 1234,
            "known": True,
        }
        base.update(counts)
        return {"member_count": 1234567, "verifications": base}

    def _tile_values(self, lang):
        tiles = overview_view.build_tiles(self._overview(), lang=lang)
        return [tile.display for tile in tiles if tile.state == "value"]

    def test_a_tile_groups_digits_the_way_the_reader_does(self):
        assert "1,234,567" in self._tile_values("en-US")
        assert "1.234.567" in self._tile_values("de")
        # Not a different separator -- a different grouping. Two, then two,
        # then three. `f"{n:,}"` cannot produce this at all.
        assert "12,34,567" in self._tile_values("hi-IN")

    def test_zero_is_still_a_number_and_not_a_blank(self):
        """The falsy-zero rule `Tile.display` is built around, re-checked at
        the formatting layer: a locale-aware formatter must not be the thing
        that turns a real 0 into an empty tile."""
        tiles = overview_view.build_tiles(self._overview(today=0), lang="de")
        assert "0" in [tile.display for tile in tiles if tile.state == "value"]

    def test_the_unknown_and_blank_states_are_untouched(self):
        """Formatting applies to values. The other two states say words, and
        those come from the catalogue like every other word on the page."""
        tiles = overview_view.build_tiles(
            self._overview(known=False), t=lambda s: s, lang="de"
        )
        unknown = [tile for tile in tiles if tile.state == "unknown"]
        # The three windows. `Members` and the all-time total come from other
        # fields and are unaffected by the rollup read failing.
        assert len(unknown) == 3
        assert all(tile.display == "Couldn't check" for tile in unknown)

    def test_the_chart_table_shows_a_day_and_not_an_iso_string(self):
        """Thirty rows of `2026-08-24` were the machine-readable value being
        read out to a person by a screen reader. `day` keeps the ISO form for
        anything that sorts; `day_label` is what gets rendered."""
        daily = [{"day": "2026-08-24", "count": 3}]
        chart = overview_view.build_chart(
            {"verifications": {"known": True, "daily": daily}}, lang="ja"
        )
        bar = chart.bars[0]
        assert bar.day == "2026-08-24"
        assert bar.day_label == "8月24日"

    def test_the_chart_table_groups_its_counts_too(self):
        """The table under the chart and the tile above it must not write the
        same number two different ways on one page."""
        daily = [{"day": "2026-08-24", "count": 1234}]
        chart = overview_view.build_chart(
            {"verifications": {"known": True, "daily": daily}}, lang="de"
        )
        assert chart.bars[0].count_label == "1.234"
        assert chart.peak_label == "1.234"

    def test_the_peak_is_still_none_when_nothing_was_measured(self):
        """`peak` answers a question about the data and the template branches
        on it, so it stays a number or None. `peak_label` is only the printing.
        """
        chart = overview_view.build_chart(
            {"verifications": {"known": True, "daily": [{"day": "2026-08-24",
                                                        "count": None}]}},
            lang="de",
        )
        assert chart.peak is None
        assert chart.peak_label == ""


    def test_the_pitch_sentence_groups_its_figure_too(self):
        """The figure inside the premium pitch, which is the one that is not in
        a tile. `ngettext` was already choosing the plural form per language
        and then interpolating an English-shaped number into it."""
        overview = make_overview(panel={"posted": True}, last_30_days=1234)
        step = overview_view.build_next_step(
            overview, None, lambda s: s, None, "de"
        )
        assert "1.234" in step["body"]
        assert "1,234" not in step["body"]


class TestTheAuditTrailsTimestamps:
    """#230, the mildest of the four surfaces and still worth doing: the
    Activity page's instants were ISO-shaped for every reader."""

    ENTRY = {
        "field": "role_id",
        "actor_name": "Ada",
        "old_value": "1",
        "new_value": "2",
        "changed_at": "2026-08-11T07:11:36+00:00",
    }

    def _when(self, lang):
        rows = settings_view.build_audit([dict(self.ENTRY)], None, None,
                                         lambda s: s, lang)
        return rows[0]["when_text"]

    def test_the_shape_is_the_readers_and_the_zone_never_is(self):
        assert self._when("de").startswith("11.08.2026")
        assert self._when("en-US").startswith("Aug 11, 2026")
        # UTC in every language. The bot records these in UTC and a reader who
        # subtracts their own offset from an unmarked time gets a wrong answer
        # about who changed what.
        for code in i18n.UI_LANGUAGES:
            assert self._when(code).endswith(" UTC"), code

    def test_the_machine_readable_half_is_still_iso(self):
        """`when` feeds the `<time datetime>` attribute and must stay ISO --
        only `when_text` is for a person."""
        rows = settings_view.build_audit([dict(self.ENTRY)], None, None,
                                         lambda s: s, "ja")
        assert rows[0]["when"] == "2026-08-11T07:11:36+00:00"

    def test_an_unparseable_instant_renders_nothing_rather_than_raising(self):
        """It was a defensive string slice before. It parses now, and it has to
        stay just as hard to break with whatever the bot sends."""
        for bad in (None, "", "yesterday", 17, {}):
            rows = settings_view.build_audit(
                [{**self.ENTRY, "changed_at": bad}], None, None, lambda s: s, "de"
            )
            assert rows[0]["when_text"] == ""


class TestTheFocusRingDoesNotRestyleTheElement(object):
    """The focus ring decorates; it must not redraw what it decorates (#160).

    `:focus-visible` used to carry `border-radius: 2px`. There is no such thing
    as an outline radius -- that declaration rounds the ELEMENT, for as long as
    it holds keyboard focus. The rule is (0,1,0) and lives at the foot of the
    file, so it matched or beat every class-based control rule above it and won
    on order. Tabbing onto the settings toggle squared off a 999px pill; every
    Tab through the header did the same to the icon buttons.

    The failure is invisible in a diff and invisible to anybody using a mouse,
    which is why it survived two passes over this stylesheet. It is pinned
    rather than merely deleted.
    """

    @staticmethod
    def _css() -> str:
        import dashboard

        with open(
            os.path.join(os.path.dirname(dashboard.__file__), "static", "style.css"),
            encoding="utf-8",
        ) as handle:
            return handle.read()

    @staticmethod
    def _focus_block(css: str) -> str:
        block = re.search(r"(?m)^:focus-visible \{([^}]*)\}", css)
        assert block, "the :focus-visible rule has gone"
        return block.group(1)

    def test_the_ring_is_still_drawn(self):
        """Deleting the radius must not have taken the outline with it. This is
        the one style on the page that may never be subtle."""
        body = self._focus_block(self._css())
        assert "outline:" in body
        assert "none" not in body, "the focus ring has been switched off"
        assert "outline-offset:" in body

    def test_it_sets_no_geometry_of_its_own(self):
        """Anything here that is not the ring is a property of the element,
        applied only while focused -- which is a state change nobody asked
        for.

        Declaration NAMES only, not a substring search over the block: the
        ring's own `var(--focus-width)` contains "width", and a test that
        cannot tell a property from a token would fail on the correct file.
        """
        body = self._focus_block(self._css())
        declared = {
            name.strip()
            for name, _ in re.findall(r"([a-z-]+)\s*:\s*([^;]+);", body)
        }
        allowed = {"outline", "outline-offset", "outline-color", "outline-width"}
        assert declared <= allowed, (
            f"{sorted(declared - allowed)} in the :focus-visible block restyle "
            f"the element itself on focus, not the ring around it"
        )

    def test_the_controls_keep_their_own_corners(self):
        """The specific regression: a pill, a control and an input all keep the
        radius their own rule gave them while focused."""
        css = self._css()
        for selector, expected in (
            (r"\.switch \{", "var(--radius-pill)"),
            (r"\.button \{", "var(--radius-control)"),
            (r"\.menu-item \{", "var(--radius-control)"),
        ):
            rule = re.search(r"(?m)^" + selector + r"([^}]*)\}", css)
            assert rule, f"the rule for {selector} has gone"
            assert expected in rule.group(1), (
                f"{selector} no longer sets {expected}"
            )

    def test_the_four_controls_share_one_focus_ring(self):
        """The other half of the row #160 called out.

        `select:focus-visible, textarea:focus-visible { outline-offset: 0 }`
        was written while `select:focus { outline: none }` meant no ring was
        drawn at all -- the comment above `select:focus` names it as the
        giveaway for that bug. Phase 5 restored the ring and left this behind,
        so a focused select sat 2px tighter than the text input beside it.

        Nothing may re-narrow the ring for part of the group. If one of these
        four ever needs a different offset, that is a decision to argue for,
        not a leftover.
        """
        css = self._css()
        stray = re.search(
            r"(?m)^(select|textarea|input)[^{\n]*:focus-visible[^{]*\{([^}]*)\}",
            css,
        )
        assert not stray, (
            f"a control re-styles its own focus ring: {stray.group(0)!r} -- "
            f"the four grouped controls must share one ring"
        )

    def test_the_grouped_control_rule_is_not_split_by_specificity(self):
        """Why the old declaration was worse than it looked.

        `select`, `textarea` and the text inputs are styled by ONE grouped rule
        so they read as one control at one height (#133 phase 5). Specificity
        is computed per selector in that list, not for the list: `select` is
        (0,0,1) and lost to `:focus-visible`, while `input[type="text"]` is
        (0,1,1) and did not. The agreement held at rest and broke on focus, for
        half the group.

        So this asserts the shared rule is still shared -- if it is ever split
        so the elements can diverge, that is the moment to notice.
        """
        css = self._css()
        rule = re.search(
            r"(?m)^select,\s*\ntextarea,\s*\ninput\[type=\"text\"\],\s*\n"
            r"input\[type=\"color\"\] \{([^}]*)\}",
            css,
        )
        assert rule, "the four controls are no longer styled by one rule"
        assert "border-radius: var(--radius-control)" in rule.group(1)


class TestEveryNoticeLivesInACard(object):
    """`.notice.ok` has no fill, so whatever is behind it IS its background.

    That is a deliberate choice -- a success message is the only kind a reader
    does not have to act on, and the fill is what makes the warning hard to
    skip. The cost is that this element has no ground of its own, so where it
    sits decides whether it is legible.

    On `--panel` it is 5.39:1. As a direct child of `<main>` it lands on `--bg`
    at 4.27:1, under AA -- and that is exactly what #159 found on the
    subscription page, on the one sentence telling somebody their payment went
    through. `settings.html`, `activity.html` and `picker.html` all wrapped
    theirs; `activity.html` says in as many words that the pages must not
    disagree about where a notice lives. The subscription page was the one that
    did.

    This is the guard rather than a `("ok", "bg")` entry in test_contrast.py.
    Pinning the pairing would assert a combination nothing renders, which that
    file's docstring refuses to do -- and it would pin the wrong thing anyway.
    The defect was never a colour. It was an element in the wrong place.
    """

    @staticmethod
    def _templates():
        import dashboard

        return os.path.join(os.path.dirname(dashboard.__file__), "templates")

    def _offenders(self, body: str):
        """Every `.notice` in `body` with no `.panel` among its ancestors.

        A text scan rather than a parse: these templates are Jinja, so an HTML
        parser sees `{% if %}` as text and the two branches of an `{% if %}` as
        unbalanced tags. Only `<section>` nesting has to be right, and every
        panel in this app is a `<section class="panel">` whose `{% if %}`, where
        it has one, wraps the whole element.

        A STACK, NOT A DEPTH COUNTER. The first version of this counted only
        panel opens but every `</section>` close, so `<section class="upgrade">`
        inside a panel -- which is a section and is not a panel -- decremented a
        depth it had never incremented, and the two real notices after it in
        settings.html were reported as bare. Sections that are not panels have
        to be pushed too, or closing one pops the panel around it.
        """
        stack, bad = [], []
        for match in re.finditer(
            r'<section\b[^>]*>|</section>|class="[^"]*\bnotice\b[^"]*"',
            body,
        ):
            token = match.group(0)
            if token.startswith("<section"):
                stack.append(bool(re.search(r'class="[^"]*\bpanel\b[^"]*"', token)))
            elif token == "</section>":
                if stack:
                    stack.pop()
            elif not any(stack):
                bad.append(body[: match.start()].count("\n") + 1)
        return bad

    def test_no_notice_is_a_direct_child_of_main(self):
        directory = self._templates()
        offenders = {}
        for name in os.listdir(directory):
            if not name.endswith(".html"):
                continue
            with open(os.path.join(directory, name), encoding="utf-8") as handle:
                lines = self._offenders(handle.read())
            if lines:
                offenders[name] = lines
        assert not offenders, (
            f"a .notice sits outside a .panel at {offenders} -- it has no fill "
            f"of its own, so on the page ground --ok is 4.27:1, under AA"
        )

    def test_the_scan_would_catch_the_regression(self):
        """The check is only worth having if it fails on the shape it exists
        for. This is #159's defect exactly: the notice before the panel."""
        regressed = (
            '{% block content %}\n'
            '{% if notice %}\n'
            '  <p class="notice ok">{{ notice }}</p>\n'
            '{% endif %}\n'
            '<section class="panel">\n'
            '  <h2>Subscriptions</h2>\n'
            '</section>\n'
        )
        assert self._offenders(regressed) == [3]

    def test_the_scan_accepts_a_notice_inside_a_card(self):
        fixed = (
            '{% if notice %}\n'
            '  <section class="panel">\n'
            '    <p class="notice ok">{{ notice }}</p>\n'
            '  </section>\n'
            '{% endif %}\n'
        )
        assert self._offenders(fixed) == []

    def test_a_notice_nested_deeper_than_one_section_still_counts(self):
        """`.panel` is not always the immediate parent -- settings.html puts
        notices inside a panel that also holds a form."""
        nested = (
            '<section class="panel">\n'
            '  <form>\n'
            '    <p class="notice ok">Saved.</p>\n'
            '  </form>\n'
            '</section>\n'
            '<p class="notice">out here, though</p>\n'
        )
        assert nested.count("notice") == 2
        assert self._offenders(nested) == [6]

    def test_a_section_that_is_not_a_panel_does_not_pop_the_panel(self):
        """settings.html's real shape, and what the first version of this scan
        got wrong: `<section class="upgrade">` sits inside the panel, and the
        notices come after it closes. They are still inside the card."""
        real = (
            '<section class="panel">\n'
            '  <section class="upgrade">\n'
            '    <h2>Upgrade</h2>\n'
            '  </section>\n'
            '  <p class="notice">still in the card</p>\n'
            '</section>\n'
        )
        assert self._offenders(real) == []


class TestTheSmallDefectsFoundAlongsideTheThemingWork(object):
    """#163. Eight unrelated findings, each too small for its own issue, all of
    them the kind that survive review because a hex or a missing declaration
    looks like every other hex or missing declaration."""

    @staticmethod
    def _css() -> str:
        import dashboard

        with open(
            os.path.join(os.path.dirname(dashboard.__file__), "static", "style.css"),
            encoding="utf-8",
        ) as handle:
            return handle.read()

    @staticmethod
    def _rule(css: str, selector: str) -> str:
        found = re.search(
            r"(?<![\w.:>-])" + re.escape(selector) + r"\s*\{([^}]*)\}", css
        )
        assert found, f"no rule for {selector}"
        return found.group(1)

    def _coarse(self, css: str) -> str:
        block = re.search(r"@media \(pointer: coarse\) \{(.*?)\n\}", css, re.S)
        assert block, "the touch block has gone"
        return block.group(1)

    def test_the_default_blue_checkbox_meets_the_touch_floor(self):
        """It was the only interactive thing on the page the touch block
        forgot. A bare flex row with no min-height is as tall as its text --
        15px x 1.55 = 23.25px, under the 24x24 of SC 2.5.8 -- and it sits
        directly under a switch this block grew to 44x28."""
        assert "min-height: 44px" in self._rule(self._coarse(self._css()), ".check")

    def test_the_dark_inset_is_not_the_dark_page_ground(self):
        """They were the same hex, `#1e1f22`, ratio 1.000 -- so any inset
        surface landing on the ground rather than on a card was invisible in
        the dark theme. Nothing showed it only because everything inset happens
        to sit inside a `.panel` today, which is composition, not design."""
        import sys

        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from test_contrast import _palettes, contrast

        dark = _palettes()["dark"]
        assert dark["inset"] != dark["bg"], (
            "--dark-inset and --dark-bg are the same colour again"
        )
        # Not an accessibility floor -- SC 1.4.11 is about components, not
        # decorative surfaces. The light theme separates the same two by
        # 1.075:1, and this asks the dark theme to do as much.
        assert contrast(dark["inset"], dark["bg"]) >= 1.05

    def test_the_inset_still_reads_as_recessed_against_a_card(self):
        """The reason it went darker rather than lighter. Mirroring the light
        theme -- where --inset sits between --bg and --panel -- would have
        moved it toward --panel and made the shipped case worse: every inset
        surface in this app is currently drawn on a card."""
        import sys

        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from test_contrast import _palettes, contrast

        dark = _palettes()["dark"]
        assert contrast(dark["inset"], dark["panel"]) >= 1.30, (
            "the inset surface has drifted toward the card it sits in"
        )

    def test_the_clickable_server_card_has_a_boundary(self):
        """`.server-card.ready` is a whole-card target via the stretched link,
        so its edge is the bounds of a UI component: SC 1.4.11, 3:1. It was
        1.32:1 light and 1.24:1 dark, with a 1.17:1 fill behind it."""
        import sys

        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from test_contrast import _palettes, contrast

        assert "var(--card-line)" in self._rule(self._css(), ".server-card")
        for theme, palette in _palettes().items():
            ratio = contrast(palette["card-line"], palette["panel"])
            assert ratio >= 3.0, f"{theme}: the card's edge is {ratio:.2f}:1"

    def test_the_general_hairline_was_not_dragged_up_with_it(self):
        """--card-line is its own token for a reason. --line draws the rule
        under a panel heading, the sidebar's divider and an input's edge --
        none of which bound a control, and all of which would read as a
        wireframe at 3:1."""
        import sys

        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from test_contrast import _palettes, contrast

        for theme, palette in _palettes().items():
            assert contrast(palette["line"], palette["panel"]) < 2.0, (
                f"{theme}: --line has been darkened into a border"
            )
        # And the element that shares the card's fill but is not a control
        # keeps the quiet edge.
        assert "var(--line)" in self._rule(self._css(), ".empty-state")

    def test_the_premium_badge_has_an_edge_the_fill_cannot_give_it(self):
        """--accent on a dark --panel is 2.74:1. The fill cannot be the fix:
        lightening it to clear 3:1 takes the white label below 4.5:1, and the
        label is 11px bold, which is not large text."""
        import sys

        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from test_contrast import _palettes, contrast

        rule = self._rule(self._css(), ".badge.premium")
        assert "box-shadow: inset 0 0 0 1px var(--accent-text)" in rule
        for theme, palette in _palettes().items():
            ring = contrast(palette["accent-text"], palette["panel"])
            assert ring >= 3.0, f"{theme}: the badge's ring is {ring:.2f}:1"
            label = contrast(palette["accent-ink"], palette["accent"])
            assert label >= 4.5, f"{theme}: the badge's label is {label:.2f}:1"

    def test_the_page_title_can_break_a_long_server_name(self):
        """The largest text on the page, holding a user-supplied string, was
        the one place the mitigation applied everywhere else was missing."""
        assert "overflow-wrap: anywhere" in self._rule(self._css(), ".page-head h1")

    def test_main_is_stretched_below_the_breakpoint(self):
        """`align-items: flex-start` becomes a cross-axis declaration when the
        narrow block flips `.layout` to a column, so children take fit-content.
        `.sidebar` was given `width: 100%`; `main` was not."""
        narrow = re.search(r"@media \(max-width: 48rem\)(.*)", self._css(), re.S)
        assert narrow, "the narrow block's query has changed shape"
        assert "width: 100%" in self._rule(narrow.group(1), "main")

    def test_the_plan_badge_uses_tokens_not_literals(self):
        """It was the only raw colour literal outside the token blocks, and a
        hardcoded radius beside a token holding the same value."""
        # Comments stripped first: the rule carries a note naming the two
        # literals it replaced, and a test that cannot tell a declaration from
        # a comment about a declaration would fail on the fixed file.
        rule = re.sub(r"/\*.*?\*/", "", self._rule(self._css(), ".plan-badge"), flags=re.S)
        assert "var(--accent-ink)" in rule and "#fff" not in rule
        assert "var(--radius-pill)" in rule and "999px" not in rule

    def test_no_raw_colour_literal_survives_outside_the_token_blocks(self):
        """The general form of the finding above. Every hex in this file should
        be a token declaration; a colour written into a rule is a colour that
        cannot be rethemed."""
        css = re.sub(r"/\*.*?\*/", "", self._css(), flags=re.S)
        # Drop every `--foo: #hex;` declaration, then look for what is left.
        without_tokens = re.sub(r"--[a-z0-9-]+\s*:\s*#[0-9a-fA-F]{3,8}\s*;", "", css)
        leftovers = re.findall(r"#[0-9a-fA-F]{3,8}\b", without_tokens)
        assert not leftovers, f"raw colour literals in rules: {leftovers}"

    def test_the_collapsed_side_up_is_reset_with_its_siblings(self):
        """`.layout.collapsed` centres `.side-guild`, `.side-up a` and
        `.side-link`. The narrow block put two of the three back."""
        narrow = re.search(r"@media \(max-width: 48rem\)(.*)", self._css(), re.S)
        assert narrow, "the narrow block's query has changed shape"
        body = narrow.group(1)
        for selector in (
            ".layout.collapsed .side-guild",
            ".layout.collapsed .side-up a",
        ):
            assert "justify-content: flex-start" in self._rule(body, selector), (
                f"{selector} still wears the rail's centring at phone width"
            )

    def test_the_dead_guild_head_rules_are_gone(self):
        """#195 phase 4 replaced `.guild-head` with `.page-head` and deleted the
        icon it was a flex row for. The rules matched nothing afterwards."""
        import dashboard

        css = self._css()
        assert ".guild-head" not in re.sub(r"/\*.*?\*/", "", css, flags=re.S)

        templates = os.path.join(os.path.dirname(dashboard.__file__), "templates")
        for name in os.listdir(templates):
            if name.endswith(".html"):
                with open(os.path.join(templates, name), encoding="utf-8") as handle:
                    assert "guild-head" not in handle.read(), name
