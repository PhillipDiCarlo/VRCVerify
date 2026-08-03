"""Unit tests for the bot's internal API (issue #65, step 1).

This is the first inbound surface the project has ever had, on a bot whose
database links Discord identities to VRChat accounts and 18+ status, so these
tests are almost entirely about the ways the door can be opened by someone who
should not be able to open it:

- a token is good for one actor, one guild and one operation, once
- authority is the bot's answer about Administrator, never the caller's claim
- a misconfigured listener refuses to start rather than binding wider than asked
- the phase itself is pinned: no write route and no writer capability can land
  here without deliberately editing this file

TLS is configured at the socket, not in the application, so the mTLS chain is
verified by hand against a real listener (see the issue's step-1 checklist).
What is testable here is that a bad configuration never reaches that point.
"""

import asyncio
import time
from dataclasses import fields as dataclass_fields
from types import SimpleNamespace

import pytest
from aiohttp.test_utils import TestClient, TestServer

import bot
import bot_api

GUILD_ID = 987654321
OTHER_GUILD_ID = 123456789
ADMIN_ID = 4242
MEMBER_ID = 9999
OWNER_ID = 77
SKU_ID = 555000111
SIGNING_KEY = b"k" * bot_api.MIN_SIGNING_KEY_BYTES

SETTINGS_PATH = f"/api/v1/guilds/{GUILD_ID}/settings"
SETTINGS_OP = "GET /api/v1/guilds/{guild_id}/settings"
ROLES_OP = "GET /api/v1/guilds/{guild_id}/roles"


def run(coro):
    """Run an async helper from a sync test (no pytest-asyncio needed)."""
    return asyncio.run(coro)


# -------------------------------------------------------------------
# Fakes
# -------------------------------------------------------------------
def make_deps(**overrides) -> bot_api.BotAPIDeps:
    """A permissive set of readers, so each test can break exactly one thing."""

    async def is_admin(guild_id, user_id):
        return int(user_id) == ADMIN_ID

    async def read_settings(guild_id):
        return {"guild_id": str(guild_id), "fields": {}}

    async def read_roles(guild_id):
        return [{"id": "1", "name": "Verified"}]

    async def read_channels(guild_id):
        return [{"id": "2", "name": "general"}]

    async def read_panel(guild_id):
        return {"posted": False}

    defaults = dict(
        is_ready=lambda: True,
        guild_present=lambda guild_id: int(guild_id) == GUILD_ID,
        is_admin=is_admin,
        read_settings=read_settings,
        read_roles=read_roles,
        read_channels=read_channels,
        read_panel=read_panel,
    )
    defaults.update(overrides)
    return bot_api.BotAPIDeps(**defaults)


def make_config(**overrides) -> bot_api.BotAPIConfig:
    base = dict(
        bind="127.0.0.1",
        port=0,
        cert_path="unused-in-app-tests",
        key_path="unused-in-app-tests",
        ca_path="unused-in-app-tests",
        signing_key=SIGNING_KEY,
    )
    base.update(overrides)
    return bot_api.BotAPIConfig(**base)


def token_for(operation, guild_id=GUILD_ID, actor_id=ADMIN_ID, **kwargs):
    return bot_api.mint_token(
        SIGNING_KEY, actor_id=actor_id, operation=operation, guild_id=guild_id, **kwargs
    )


def serve(scenario, *, config=None, deps=None):
    """Run `scenario(client)` against a live (plain HTTP) instance of the app."""
    app = bot_api.create_app(config or make_config(), deps or make_deps())

    async def runner():
        async with TestClient(TestServer(app)) as client:
            return await scenario(client)

    return asyncio.run(runner())


async def get(client, path, token=None):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    response = await client.get(path, headers=headers)
    return response.status, await response.json()


# -------------------------------------------------------------------
# Tokens
# -------------------------------------------------------------------
class TestTokens:
    def verify(self, token, **overrides):
        kwargs = dict(
            expected_operation=SETTINGS_OP,
            expected_guild_id=GUILD_ID,
        )
        kwargs.update(overrides)
        return bot_api.verify_token(token, SIGNING_KEY, **kwargs)

    def test_round_trip(self):
        claims = self.verify(token_for(SETTINGS_OP))
        assert claims.actor_id == ADMIN_ID
        assert claims.guild_id == GUILD_ID
        assert claims.operation == SETTINGS_OP

    def test_expired_token_is_rejected(self):
        old = token_for(SETTINGS_OP, ttl=1, now=time.time() - 3600)
        with pytest.raises(bot_api.TokenError) as error:
            self.verify(old)
        assert error.value.reason == "expired"

    def test_token_from_the_future_is_rejected(self):
        ahead = token_for(SETTINGS_OP, now=time.time() + 3600)
        with pytest.raises(bot_api.TokenError) as error:
            self.verify(ahead)
        assert error.value.reason == "not_yet_valid"

    def test_self_minted_long_lived_token_is_rejected(self):
        """A leaked signing key must not buy a token that lasts all week."""
        forever = token_for(SETTINGS_OP, ttl=86400)
        with pytest.raises(bot_api.TokenError) as error:
            self.verify(forever)
        assert error.value.reason == "ttl_too_long"

    def test_token_cannot_be_replayed_against_another_guild(self):
        with pytest.raises(bot_api.TokenError) as error:
            self.verify(token_for(SETTINGS_OP), expected_guild_id=OTHER_GUILD_ID)
        assert error.value.reason == "wrong_guild"

    def test_token_cannot_be_replayed_against_another_endpoint(self):
        with pytest.raises(bot_api.TokenError) as error:
            self.verify(token_for(ROLES_OP), expected_operation=SETTINGS_OP)
        assert error.value.reason == "wrong_operation"

    def test_tampered_payload_is_rejected(self):
        version, payload, signature = token_for(SETTINGS_OP).split(".")
        forged = bot_api._b64encode(
            bot_api._canonical(
                {
                    "act": ADMIN_ID,
                    "gid": GUILD_ID,
                    "op": SETTINGS_OP,
                    "iat": int(time.time()),
                    "exp": int(time.time()) + 30,
                    "jti": "forged",
                }
            )
        )
        with pytest.raises(bot_api.TokenError) as error:
            self.verify(f"{version}.{forged}.{signature}")
        assert error.value.reason == "bad_signature"

    def test_another_key_is_rejected(self):
        other = bot_api.mint_token(
            b"z" * bot_api.MIN_SIGNING_KEY_BYTES,
            actor_id=ADMIN_ID,
            operation=SETTINGS_OP,
            guild_id=GUILD_ID,
        )
        with pytest.raises(bot_api.TokenError) as error:
            self.verify(other)
        assert error.value.reason == "bad_signature"

    @pytest.mark.parametrize(
        "bad",
        ["", "nonsense", "v1.only-two", "v2.a.b", "v1.!!!.???", "v1..", "a.b.c"],
    )
    def test_malformed_tokens_are_rejected(self, bad):
        with pytest.raises(bot_api.TokenError):
            self.verify(bad)

    def test_payload_is_not_parsed_before_the_signature_is_checked(self):
        """Unauthenticated JSON must never reach json.loads()."""
        payload = bot_api._b64encode(b'{"act": "not-an-int"}')
        with pytest.raises(bot_api.TokenError) as error:
            self.verify(f"v1.{payload}.{bot_api._b64encode(b'wrong')}")
        assert error.value.reason == "bad_signature"


class TestReplayGuard:
    def test_a_token_id_can_only_be_spent_once(self):
        guard = bot_api.ReplayGuard()
        expires = time.time() + 30
        guard.spend("abc", expires)
        with pytest.raises(bot_api.TokenError) as error:
            guard.spend("abc", expires)
        assert error.value.reason == "replayed"

    def test_expired_ids_stop_being_remembered(self):
        guard = bot_api.ReplayGuard(maxsize=2)
        guard.spend("old", time.time() - 1)
        guard.spend("new", time.time() + 30)
        # 'old' can no longer be replayed, so forgetting it is free.
        guard.spend("old", time.time() + 30)

    def test_a_full_guard_refuses_rather_than_forgetting(self):
        """Evicting a live entry would hand back the window this closes."""
        guard = bot_api.ReplayGuard(maxsize=2)
        guard.spend("a", time.time() + 30)
        guard.spend("b", time.time() + 30)
        with pytest.raises(bot_api.TokenError) as error:
            guard.spend("c", time.time() + 30)
        assert error.value.reason == "replay_guard_full"


class TestRateLimiter:
    def test_budget_runs_out(self):
        limiter = bot_api.RateLimiter(limit=3, window=60)
        assert all(limiter.allow("actor") for _ in range(3))
        assert not limiter.allow("actor")

    def test_budgets_are_per_key(self):
        limiter = bot_api.RateLimiter(limit=1, window=60)
        assert limiter.allow("one")
        assert not limiter.allow("one")
        assert limiter.allow("two")

    def test_budget_refills_as_the_window_moves(self):
        limiter = bot_api.RateLimiter(limit=1, window=60)
        now = time.time()
        assert limiter.allow("actor", now=now)
        assert not limiter.allow("actor", now=now + 1)
        assert limiter.allow("actor", now=now + 61)


# -------------------------------------------------------------------
# Configuration — the listener must refuse to come up wrong
# -------------------------------------------------------------------
class TestConfig:
    @pytest.fixture(autouse=True)
    def clear_env(self, monkeypatch):
        for name in (
            "BOT_API_ENABLED",
            "BOT_API_BIND",
            "BOT_API_PORT",
            "BOT_API_CERT",
            "BOT_API_KEY",
            "BOT_API_CA",
            "BOT_API_TOKEN_SIGNING_KEY",
            "BOT_API_CLIENT_CN",
        ):
            monkeypatch.delenv(name, raising=False)

    def enable(self, monkeypatch, tmp_path, **overrides):
        for name in ("cert", "key", "ca"):
            (tmp_path / f"{name}.pem").write_text("placeholder")
        env = {
            "BOT_API_ENABLED": "1",
            "BOT_API_BIND": "100.64.0.2",
            "BOT_API_CERT": str(tmp_path / "cert.pem"),
            "BOT_API_KEY": str(tmp_path / "key.pem"),
            "BOT_API_CA": str(tmp_path / "ca.pem"),
            "BOT_API_TOKEN_SIGNING_KEY": "s" * 40,
        }
        env.update(overrides)
        for name, value in env.items():
            if value is None:
                monkeypatch.delenv(name, raising=False)
            else:
                monkeypatch.setenv(name, value)

    def test_unset_is_the_kill_switch(self):
        assert bot_api.BotAPIConfig.from_env() is None

    @pytest.mark.parametrize("value", ["", "0", "false", "no", "off", "maybe"])
    def test_only_an_explicit_yes_switches_it_on(self, monkeypatch, value):
        monkeypatch.setenv("BOT_API_ENABLED", value)
        assert bot_api.BotAPIConfig.from_env() is None

    def test_happy_path(self, monkeypatch, tmp_path):
        self.enable(monkeypatch, tmp_path)
        config = bot_api.BotAPIConfig.from_env()
        assert config.bind == "100.64.0.2"
        assert config.port == bot_api.DEFAULT_PORT
        assert len(config.signing_key) >= bot_api.MIN_SIGNING_KEY_BYTES

    @pytest.mark.parametrize("bind", ["0.0.0.0", "::", "*", "[::]", ""])
    def test_a_wildcard_bind_refuses_to_start(self, monkeypatch, tmp_path, bind):
        """The one misconfiguration that would undo the whole network design."""
        self.enable(monkeypatch, tmp_path, BOT_API_BIND=bind)
        with pytest.raises(bot_api.BotAPIConfigError):
            bot_api.BotAPIConfig.from_env()

    def test_a_tailnet_hostname_is_accepted(self, monkeypatch, tmp_path):
        self.enable(monkeypatch, tmp_path, BOT_API_BIND="vrcverify.tailnet.ts.net")
        assert bot_api.BotAPIConfig.from_env().bind == "vrcverify.tailnet.ts.net"

    @pytest.mark.parametrize("missing", ["BOT_API_CERT", "BOT_API_KEY", "BOT_API_CA"])
    def test_missing_credentials_refuse_to_start(self, monkeypatch, tmp_path, missing):
        self.enable(monkeypatch, tmp_path, **{missing: None})
        with pytest.raises(bot_api.BotAPIConfigError):
            bot_api.BotAPIConfig.from_env()

    def test_a_certificate_path_that_is_not_a_file_refuses_to_start(
        self, monkeypatch, tmp_path
    ):
        self.enable(monkeypatch, tmp_path, BOT_API_CERT=str(tmp_path / "absent.pem"))
        with pytest.raises(bot_api.BotAPIConfigError):
            bot_api.BotAPIConfig.from_env()

    def test_a_weak_signing_key_refuses_to_start(self, monkeypatch, tmp_path):
        self.enable(monkeypatch, tmp_path, BOT_API_TOKEN_SIGNING_KEY="short")
        with pytest.raises(bot_api.BotAPIConfigError):
            bot_api.BotAPIConfig.from_env()

    def test_the_ssl_context_really_loads_the_files(self, monkeypatch, tmp_path):
        """Placeholder files must blow up, proving the chain isn't ignored."""
        self.enable(monkeypatch, tmp_path)
        config = bot_api.BotAPIConfig.from_env()
        with pytest.raises(Exception):
            bot_api.build_ssl_context(config)


class TestStartupWiring:
    """bot.start_bot_api never takes the bot down and never binds by accident."""

    @pytest.fixture(autouse=True)
    def no_server(self, monkeypatch):
        monkeypatch.setattr(bot, "_bot_api_server", None)
        monkeypatch.delenv("BOT_API_ENABLED", raising=False)

    def test_disabled_starts_nothing(self, caplog):
        with caplog.at_level("INFO"):
            run(bot.start_bot_api())
        assert bot._bot_api_server is None
        assert "disabled" in caplog.text

    def test_a_bad_configuration_is_loud_but_not_fatal(self, monkeypatch, caplog):
        monkeypatch.setenv("BOT_API_ENABLED", "1")  # enabled, nothing else set
        with caplog.at_level("ERROR"):
            run(bot.start_bot_api())
        assert bot._bot_api_server is None
        assert "misconfigured" in caplog.text

    def test_deps_are_wired_to_the_real_readers(self):
        deps = bot.build_bot_api_deps()
        assert deps.read_settings is bot.read_dashboard_settings
        assert deps.read_roles is bot.read_dashboard_roles
        assert deps.read_channels is bot.read_dashboard_channels
        assert deps.read_panel is bot.read_dashboard_panel
        assert deps.is_admin is bot.dashboard_is_admin


# -------------------------------------------------------------------
# The request path, end to end
# -------------------------------------------------------------------
class TestRequests:
    def test_health_needs_no_token(self):
        async def scenario(client):
            return await get(client, "/healthz")

        status, body = serve(scenario)
        assert status == 200
        assert body == {"ok": True, "ready": True}

    def test_a_read_needs_a_token(self):
        async def scenario(client):
            return await get(client, SETTINGS_PATH)

        status, body = serve(scenario)
        assert status == 401
        assert body["error"] == "missing_token"

    def test_a_valid_token_gets_the_payload(self):
        async def scenario(client):
            return await get(client, SETTINGS_PATH, token_for(SETTINGS_OP))

        status, body = serve(scenario)
        assert status == 200
        assert body["guild_id"] == str(GUILD_ID)

    def test_a_token_for_one_guild_does_not_work_on_another(self):
        async def scenario(client):
            return await get(
                client,
                f"/api/v1/guilds/{OTHER_GUILD_ID}/settings",
                token_for(SETTINGS_OP, guild_id=GUILD_ID),
            )

        status, body = serve(scenario)
        assert status == 403
        assert body["error"] == "wrong_guild"

    def test_a_token_for_one_endpoint_does_not_work_on_another(self):
        async def scenario(client):
            return await get(client, SETTINGS_PATH, token_for(ROLES_OP))

        status, body = serve(scenario)
        assert status == 403
        assert body["error"] == "wrong_operation"

    def test_a_token_cannot_be_used_twice(self):
        async def scenario(client):
            token = token_for(SETTINGS_OP)
            first = await get(client, SETTINGS_PATH, token)
            second = await get(client, SETTINGS_PATH, token)
            return first, second

        (first_status, _), (second_status, second_body) = serve(scenario)
        assert first_status == 200
        assert second_status == 401
        assert second_body["error"] == "replayed"

    def test_an_expired_token_is_refused(self):
        async def scenario(client):
            stale = token_for(SETTINGS_OP, ttl=1, now=time.time() - 3600)
            return await get(client, SETTINGS_PATH, stale)

        status, body = serve(scenario)
        assert status == 401
        assert body["error"] == "expired"

    def test_a_non_administrator_is_refused(self):
        """A valid token proves who you are, never what you may do."""

        async def scenario(client):
            return await get(
                client, SETTINGS_PATH, token_for(SETTINGS_OP, actor_id=MEMBER_ID)
            )

        status, body = serve(scenario)
        assert status == 403
        assert body["error"] == "not_administrator"

    def test_a_guild_the_bot_is_not_in_is_a_404(self):
        async def scenario(client):
            return await get(
                client,
                f"/api/v1/guilds/{OTHER_GUILD_ID}/settings",
                token_for(SETTINGS_OP, guild_id=OTHER_GUILD_ID),
            )

        status, body = serve(scenario)
        assert status == 404
        assert body["error"] == "guild_not_found"

    def test_nothing_is_answered_before_the_gateway_connects(self):
        async def scenario(client):
            return await get(client, SETTINGS_PATH, token_for(SETTINGS_OP))

        status, body = serve(scenario, deps=make_deps(is_ready=lambda: False))
        assert status == 503
        assert body["error"] == "not_ready"

    def test_an_unreadable_setting_is_a_503_not_a_guess(self):
        async def unreadable(guild_id):
            return None

        async def scenario(client):
            return await get(client, SETTINGS_PATH, token_for(SETTINGS_OP))

        status, body = serve(scenario, deps=make_deps(read_settings=unreadable))
        assert status == 503
        assert body["error"] == "unavailable"

    def test_the_budget_runs_out(self):
        async def scenario(client):
            results = []
            for _ in range(3):
                results.append(await get(client, SETTINGS_PATH, token_for(SETTINGS_OP)))
            return results

        results = serve(scenario, config=make_config(rate_limit=2))
        assert [status for status, _ in results] == [200, 200, 429]

    def test_health_is_metered_too(self):
        """The one tokenless endpoint still can't be an unmetered handler."""

        async def scenario(client):
            return [await get(client, "/healthz") for _ in range(3)]

        results = serve(scenario, config=make_config(global_rate_limit=2))
        assert [status for status, _ in results] == [200, 200, 429]

    def test_responses_are_not_cacheable(self):
        async def scenario(client):
            response = await client.get("/healthz")
            return dict(response.headers)

        headers = serve(scenario)
        assert headers["Cache-Control"] == "no-store"
        assert headers["X-Content-Type-Options"] == "nosniff"
        assert "aiohttp" not in headers.get("Server", "")

    def test_a_pinned_client_certificate_is_required_when_configured(self):
        """Plain HTTP presents no certificate, so the pin must refuse it."""

        async def scenario(client):
            return await get(client, SETTINGS_PATH, token_for(SETTINGS_OP))

        status, body = serve(scenario, config=make_config(client_cn="dashboard"))
        assert status == 403
        assert body["error"] == "client_certificate_not_permitted"

    def test_peer_identities_reads_both_subject_and_san(self):
        request = SimpleNamespace(
            transport=SimpleNamespace(
                get_extra_info=lambda _: {
                    "subject": ((("commonName", "dashboard"),),),
                    "subjectAltName": (("DNS", "dashboard.tailnet.ts.net"),),
                }
            )
        )
        assert bot_api._peer_identities(request) == {
            "dashboard",
            "dashboard.tailnet.ts.net",
        }

    def test_every_denial_is_logged_with_actor_guild_and_operation(self, caplog):
        async def scenario(client):
            return await get(
                client, SETTINGS_PATH, token_for(SETTINGS_OP, actor_id=MEMBER_ID)
            )

        with caplog.at_level("WARNING"):
            serve(scenario)
        assert f"actor={MEMBER_ID}" in caplog.text
        assert f"guild={GUILD_ID}" in caplog.text
        assert "reason=not_administrator" in caplog.text

    def test_a_signed_token_names_its_actor_even_when_refused(self, caplog):
        """The forensically interesting case: a real session, out of scope.

        The signature verified, so we know exactly whose token this was — that
        is the field worth alerting on, and logging 'unknown' would lose it.
        """

        async def scenario(client):
            return await get(
                client,
                f"/api/v1/guilds/{OTHER_GUILD_ID}/settings",
                token_for(SETTINGS_OP, guild_id=GUILD_ID, actor_id=MEMBER_ID),
            )

        with caplog.at_level("WARNING"):
            serve(scenario)
        assert f"actor={MEMBER_ID}" in caplog.text
        assert "reason=wrong_guild" in caplog.text

    def test_a_replayed_token_names_its_actor(self, caplog):
        async def scenario(client):
            token = token_for(SETTINGS_OP)
            await get(client, SETTINGS_PATH, token)
            return await get(client, SETTINGS_PATH, token)

        with caplog.at_level("WARNING"):
            serve(scenario)
        assert f"actor={ADMIN_ID}" in caplog.text
        assert "reason=replayed" in caplog.text

    def test_an_unsigned_token_cannot_name_an_actor(self, caplog):
        """A claim we never authenticated must not be written into the log."""

        async def scenario(client):
            return await get(client, SETTINGS_PATH, "v1.garbage.garbage")

        with caplog.at_level("WARNING"):
            serve(scenario)
        assert "actor=unknown" in caplog.text


class TestGuildList:
    OP = bot_api.OP_LIST_GUILDS

    def test_returns_only_the_guilds_the_bot_is_in(self):
        async def scenario(client):
            return await get(
                client,
                f"/api/v1/guilds?ids={GUILD_ID},{OTHER_GUILD_ID}",
                bot_api.mint_token(
                    SIGNING_KEY, actor_id=ADMIN_ID, operation=self.OP, guild_id=None
                ),
            )

        status, body = serve(scenario)
        assert status == 200
        assert body == {"present": [str(GUILD_ID)]}

    def test_a_guild_scoped_token_cannot_reach_it(self):
        async def scenario(client):
            return await get(client, "/api/v1/guilds", token_for(self.OP))

        status, body = serve(scenario)
        assert status == 403
        assert body["error"] == "wrong_guild"

    def test_an_oversized_query_is_refused(self):
        async def scenario(client):
            ids = ",".join(str(n) for n in range(bot_api.MAX_GUILD_IDS + 1))
            return await get(
                client,
                f"/api/v1/guilds?ids={ids}",
                bot_api.mint_token(
                    SIGNING_KEY, actor_id=ADMIN_ID, operation=self.OP, guild_id=None
                ),
            )

        status, body = serve(scenario)
        assert status == 400
        assert body["error"] == "too_many_ids"


# -------------------------------------------------------------------
# Pinning the phase
# -------------------------------------------------------------------
class TestReadOnlyPhase:
    """Step 1 is read-only. Both guards below have to be edited on purpose."""

    def test_no_route_can_change_anything(self):
        app = bot_api.create_app(make_config(), make_deps())
        methods = {route.method for route in app.router.routes()}
        assert methods <= {"GET", "HEAD"}, f"a write route appeared: {methods}"

    def test_the_api_holds_no_writer(self):
        assert bot_api.deps_field_names() == frozenset(
            {
                "is_ready",
                "guild_present",
                "is_admin",
                "read_settings",
                "read_roles",
                "read_channels",
                "read_panel",
            }
        )

    def test_every_capability_is_named_as_a_read(self):
        for field in dataclass_fields(bot_api.BotAPIDeps):
            assert field.name.startswith(("read_", "is_", "guild_"))


# -------------------------------------------------------------------
# The bot-side readers
# -------------------------------------------------------------------
def make_server(server_id=str(GUILD_ID), row_id=10, **overrides):
    fields = dict(
        id=row_id,
        server_id=server_id,
        owner_id=str(OWNER_ID),
        role_id="1",
        instructions_locale="en-US",
    )
    fields.update(overrides)
    with bot.session_scope() as session:
        session.add(bot.Server(**fields))


class FakeRole:
    def __init__(self, role_id, name, position, managed=False, default=False):
        self.id = role_id
        self.name = name
        self.position = position
        self.managed = managed
        self.color = SimpleNamespace(value=0x5865F2)
        self._default = default

    def is_default(self):
        return self._default

    def __gt__(self, other):
        return self.position > other.position


class FakeChannel:
    def __init__(self, channel_id, name, position=0, news=False, sendable=True):
        self.id = channel_id
        self.name = name
        self.position = position
        self.category = None
        self._news = news
        self._sendable = sendable

    def is_news(self):
        return self._news

    def permissions_for(self, _member):
        return SimpleNamespace(view_channel=True, send_messages=self._sendable)


class FakeGuild:
    def __init__(self, roles=(), channels=(), me=True, owner_id=OWNER_ID):
        self.id = GUILD_ID
        self.owner_id = owner_id
        self.roles = list(roles)
        self.text_channels = list(channels)
        self.me = SimpleNamespace(top_role=FakeRole(90, "Bot", 90)) if me else None
        self._members = {}

    def get_member(self, user_id):
        return self._members.get(user_id)

    def get_channel_or_thread(self, channel_id):
        for channel in self.text_channels:
            if channel.id == channel_id:
                return channel
        return None


@pytest.fixture(autouse=True)
def clean_db():
    def wipe():
        with bot.session_scope() as session:
            session.query(bot.Server).delete()
            session.query(bot.InstructionPanelBranding).delete()
            session.query(bot.VerificationLogChannel).delete()
            session.query(bot.PremiumGrandfatherLine).delete()
            session.query(bot.InstructionPanelView).delete()

    wipe()
    bot.premium_status_cache.clear()
    yield
    wipe()
    bot.premium_status_cache.clear()


@pytest.fixture
def enforced(monkeypatch):
    monkeypatch.setattr(bot, "PREMIUM_SKU_ID", SKU_ID)
    monkeypatch.setattr(bot, "PREMIUM_ENFORCED", True)
    bot.premium_status_cache.clear()


@pytest.fixture
def free(monkeypatch, enforced):
    """Not subscribed and not grandfathered."""

    async def no(guild_id):
        return False

    monkeypatch.setattr(bot, "guild_has_premium", no)
    with bot.session_scope() as session:
        session.add(bot.PremiumGrandfatherLine(id=1, max_server_id=1))
    bot.premium_status_cache.clear()


@pytest.fixture
def subscribed(monkeypatch, enforced):
    async def yes(guild_id):
        return True

    monkeypatch.setattr(bot, "guild_has_premium", yes)
    bot.premium_status_cache.clear()


class TestSettingsReader:
    def test_an_unconfigured_guild_reads_as_defaults(self):
        payload = run(bot.read_dashboard_settings(GUILD_ID))
        assert payload["fields"]["role_id"]["value"] is None
        assert payload["fields"]["auto_verify_new_members"]["value"] is True

    def test_stored_values_come_back(self):
        make_server(role_id="555", auto_nickname_change=True, instructions_locale="fr")
        payload = run(bot.read_dashboard_settings(GUILD_ID))
        fields = payload["fields"]
        assert fields["role_id"]["value"] == "555"
        assert fields["auto_nickname_change"]["value"] is True
        assert fields["instructions_locale"]["value"] == "fr"

    def test_nothing_is_locked_while_the_tier_is_off(self):
        make_server()
        payload = run(bot.read_dashboard_settings(GUILD_ID))
        assert not any(field["locked"] for field in payload["fields"].values())
        assert all(field["active"] for field in payload["fields"].values())

    def test_a_subscribed_guild_has_everything_active(self, subscribed):
        make_server()
        payload = run(bot.read_dashboard_settings(GUILD_ID))
        assert payload["premium"]["premium"] is True
        assert not any(field["locked"] for field in payload["fields"].values())

    def test_a_free_guild_locks_only_what_the_bot_locks(self, free):
        make_server(row_id=9000)
        fields = run(bot.read_dashboard_settings(GUILD_ID))["fields"]

        # Refused at save time by PagedSettingsView, so refused here too.
        assert fields["auto_nickname_change"]["locked"] is True
        assert fields["panel_embed_color"]["locked"] is True
        assert fields["panel_show_icon"]["locked"] is True
        assert fields["verification_log_channel_id"]["locked"] is True

        # Saveable by anyone today via /vrcverify_setup and
        # /vrcverify_setrequestmessage. The website must not be stricter than
        # the slash command an admin can already run.
        assert fields["unverified_role_id"]["locked"] is False
        assert fields["unverified_role_id"]["active"] is False
        assert fields["custom_verification_requested_message"]["locked"] is False
        assert fields["custom_verification_requested_message"]["active"] is False

    def test_auto_verify_on_join_is_never_gated(self, free):
        """Free for everyone, forever — the same guard test_premium.py pins."""
        make_server(row_id=9000)
        field = run(bot.read_dashboard_settings(GUILD_ID))["fields"][
            "auto_verify_new_members"
        ]
        assert field["feature"] is None
        assert field["locked"] is False
        assert field["active"] is True

    def test_a_grandfathered_guild_keeps_its_three_features(self, free):
        make_server(row_id=1)  # at or below the line drawn in the fixture
        payload = run(bot.read_dashboard_settings(GUILD_ID))
        fields = payload["fields"]
        assert payload["premium"]["grandfathered"] is True
        assert fields["auto_nickname_change"]["active"] is True
        assert fields["unverified_role_id"]["active"] is True
        # New features are not grandfathered — nobody is losing these.
        assert fields["verification_log_channel_id"]["active"] is False
        assert fields["verification_log_channel_id"]["locked"] is True

    def test_branding_is_reported_when_stored(self):
        make_server()
        bot.save_panel_branding(GUILD_ID, 0xFF0000, True)
        fields = run(bot.read_dashboard_settings(GUILD_ID))["fields"]
        assert fields["panel_embed_color"]["value"] == 0xFF0000
        assert fields["panel_show_icon"]["value"] is True

    def test_unreadable_branding_refuses_rather_than_showing_defaults(
        self, monkeypatch
    ):
        """Rendering 'default blue' for a server that chose otherwise is a lie."""
        make_server()
        monkeypatch.setattr(
            bot, "load_panel_branding", lambda guild_id: bot.BRANDING_UNREADABLE
        )
        assert run(bot.read_dashboard_settings(GUILD_ID)) is None

    def test_the_log_channel_is_reported(self):
        make_server()
        bot.set_log_channel(GUILD_ID, "424242")
        fields = run(bot.read_dashboard_settings(GUILD_ID))["fields"]
        assert fields["verification_log_channel_id"]["value"] == "424242"

    def test_a_database_failure_reads_as_unavailable(self, monkeypatch):
        def boom():
            raise RuntimeError("database is down")

        monkeypatch.setattr(bot, "session_scope", boom)
        assert run(bot.read_dashboard_settings(GUILD_ID)) is None


class TestRoleReader:
    def guild(self, monkeypatch, **kwargs):
        guild = FakeGuild(**kwargs)
        monkeypatch.setattr(bot.bot, "get_guild", lambda _id: guild)
        return guild

    def test_roles_are_ranked_and_everyone_is_dropped(self, monkeypatch):
        self.guild(
            monkeypatch,
            roles=[
                FakeRole(1, "@everyone", 0, default=True),
                FakeRole(2, "Verified", 10),
                FakeRole(3, "Mods", 50),
            ],
        )
        roles = run(bot.read_dashboard_roles(GUILD_ID))
        assert [role["name"] for role in roles] == ["Mods", "Verified"]

    def test_a_role_above_the_bot_is_not_assignable(self, monkeypatch):
        self.guild(
            monkeypatch,
            roles=[FakeRole(2, "Below", 10), FakeRole(3, "Above", 99)],
        )
        roles = {role["name"]: role["assignable"] for role in run(
            bot.read_dashboard_roles(GUILD_ID)
        )}
        assert roles["Below"] is True
        assert roles["Above"] is False

    def test_an_integration_role_is_never_assignable(self, monkeypatch):
        self.guild(monkeypatch, roles=[FakeRole(2, "Booster", 5, managed=True)])
        assert run(bot.read_dashboard_roles(GUILD_ID))[0]["assignable"] is False

    def test_assignability_is_unknown_rather_than_no_without_a_bot_member(
        self, monkeypatch
    ):
        self.guild(monkeypatch, roles=[FakeRole(2, "Verified", 10)], me=False)
        assert run(bot.read_dashboard_roles(GUILD_ID))[0]["assignable"] is None

    def test_an_absent_guild_reads_as_unavailable(self, monkeypatch):
        monkeypatch.setattr(bot.bot, "get_guild", lambda _id: None)
        assert run(bot.read_dashboard_roles(GUILD_ID)) is None


class TestChannelReader:
    def test_announcement_channels_are_flagged(self, monkeypatch):
        guild = FakeGuild(
            channels=[
                FakeChannel(1, "general", position=0),
                FakeChannel(2, "announcements", position=1, news=True),
            ]
        )
        monkeypatch.setattr(bot.bot, "get_guild", lambda _id: guild)
        channels = run(bot.read_dashboard_channels(GUILD_ID))
        assert [channel["is_news"] for channel in channels] == [False, True]

    def test_unpostable_channels_are_flagged(self, monkeypatch):
        guild = FakeGuild(channels=[FakeChannel(1, "locked", sendable=False)])
        monkeypatch.setattr(bot.bot, "get_guild", lambda _id: guild)
        assert run(bot.read_dashboard_channels(GUILD_ID))[0]["can_send"] is False


class TestPanelReader:
    def test_no_panel_reads_as_not_posted(self, monkeypatch):
        make_server()
        monkeypatch.setattr(bot.bot, "get_guild", lambda _id: FakeGuild())
        assert run(bot.read_dashboard_panel(GUILD_ID)) == {"posted": False}

    def test_a_posted_panel_reports_where_it_is(self, monkeypatch):
        make_server(instructions_channel_id="1", instructions_message_id="55")
        guild = FakeGuild(channels=[FakeChannel(1, "verify")])
        monkeypatch.setattr(bot.bot, "get_guild", lambda _id: guild)
        panel = run(bot.read_dashboard_panel(GUILD_ID))
        assert panel["posted"] is True
        assert panel["channel_name"] == "verify"
        assert panel["channel_reachable"] is True

    def test_a_deleted_channel_is_reported(self, monkeypatch):
        make_server(instructions_channel_id="999", instructions_message_id="55")
        monkeypatch.setattr(bot.bot, "get_guild", lambda _id: FakeGuild())
        panel = run(bot.read_dashboard_panel(GUILD_ID))
        assert panel["channel_exists"] is False
        assert panel["channel_reachable"] is None


class TestAdminCheck:
    def test_the_owner_needs_no_lookup(self, monkeypatch):
        guild = FakeGuild()

        async def never(*args):
            raise AssertionError("the owner shortcut should avoid a fetch")

        monkeypatch.setattr(bot.bot, "get_guild", lambda _id: guild)
        monkeypatch.setattr(bot, "fetch_member_cached", never)
        assert run(bot.dashboard_is_admin(GUILD_ID, OWNER_ID)) is True

    def test_an_administrator_is_allowed(self, monkeypatch):
        guild = FakeGuild()
        member = SimpleNamespace(
            guild_permissions=SimpleNamespace(administrator=True)
        )

        async def fetch(_guild, _user_id):
            return member

        monkeypatch.setattr(bot.bot, "get_guild", lambda _id: guild)
        monkeypatch.setattr(bot, "fetch_member_cached", fetch)
        assert run(bot.dashboard_is_admin(GUILD_ID, ADMIN_ID)) is True

    def test_manage_server_is_not_enough(self, monkeypatch):
        """Administrator only, matching every slash command's own check."""
        guild = FakeGuild()
        member = SimpleNamespace(
            guild_permissions=SimpleNamespace(administrator=False)
        )

        async def fetch(_guild, _user_id):
            return member

        monkeypatch.setattr(bot.bot, "get_guild", lambda _id: guild)
        monkeypatch.setattr(bot, "fetch_member_cached", fetch)
        assert run(bot.dashboard_is_admin(GUILD_ID, ADMIN_ID)) is False

    def test_a_non_member_is_refused(self, monkeypatch):
        async def fetch(_guild, _user_id):
            return None

        monkeypatch.setattr(bot.bot, "get_guild", lambda _id: FakeGuild())
        monkeypatch.setattr(bot, "fetch_member_cached", fetch)
        assert run(bot.dashboard_is_admin(GUILD_ID, ADMIN_ID)) is False

    def test_an_unanswerable_question_fails_closed(self, monkeypatch):
        async def boom(_guild, _user_id):
            raise RuntimeError("gateway is unhappy")

        monkeypatch.setattr(bot.bot, "get_guild", lambda _id: FakeGuild())
        monkeypatch.setattr(bot, "fetch_member_cached", boom)
        assert run(bot.dashboard_is_admin(GUILD_ID, ADMIN_ID)) is False

    def test_an_absent_guild_is_refused(self, monkeypatch):
        monkeypatch.setattr(bot.bot, "get_guild", lambda _id: None)
        assert run(bot.dashboard_is_admin(GUILD_ID, ADMIN_ID)) is False
