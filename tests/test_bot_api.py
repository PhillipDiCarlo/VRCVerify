"""Unit tests for the bot's internal API (issue #65, steps 1 and 5).

This is the first inbound surface the project has ever had, on a bot whose
database links Discord identities to VRChat accounts and 18+ status, so these
tests are almost entirely about the ways the door can be opened by someone who
should not be able to open it:

- a token is good for one actor, one guild and one operation, once
- authority is the bot's answer about Administrator, never the caller's claim
- a misconfigured listener refuses to start rather than binding wider than asked
- the phase itself is pinned: the write surface is exactly one route and one
  capability, and widening it means deliberately editing this file
- a token minted to READ a guild's settings cannot be replayed to write them

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
def make_deps(written=None, posted=None, **overrides) -> bot_api.BotAPIDeps:
    """A permissive set of capabilities, so each test breaks exactly one thing.

    `written` and `posted` collect what reached the two mutating capabilities,
    so a test can assert the handler passed the body through unchanged -- and,
    more usefully, that a refused request never reached them at all.
    """
    written = [] if written is None else written
    posted = [] if posted is None else posted

    async def is_admin(guild_id, user_id):
        return int(user_id) == ADMIN_ID

    async def read_admin_guilds(user_id, guild_ids):
        if int(user_id) != ADMIN_ID:
            return []
        return [gid for gid in guild_ids if int(gid) == GUILD_ID]

    async def read_settings(guild_id):
        return {"guild_id": str(guild_id), "fields": {}}

    async def read_roles(guild_id):
        return [{"id": "1", "name": "Verified"}]

    async def read_channels(guild_id):
        return [{"id": "2", "name": "general"}]

    async def read_panel(guild_id):
        return {"posted": False}

    async def read_audit(guild_id):
        return []

    async def read_overview(guild_id):
        return {
            "guild_id": str(guild_id),
            "member_count": 12,
            "verifications": {
                "total": 4,
                "today": 1,
                "last_7_days": 2,
                "last_30_days": 4,
                "collecting_since": "2026-06-01",
                "known": True,
            },
        }

    async def post_panel(guild_id, actor_id, channel_id):
        posted.append((int(guild_id), int(actor_id), str(channel_id)))
        return {"action": "posted", "channel_id": str(channel_id)}

    async def write_settings(guild_id, actor_id, changes):
        written.append((int(guild_id), int(actor_id), dict(changes)))
        return {"guild_id": str(guild_id), "fields": {}, "written": dict(changes)}

    defaults = dict(
        is_ready=lambda: True,
        guild_present=lambda guild_id: int(guild_id) == GUILD_ID,
        is_admin=is_admin,
        read_admin_guilds=read_admin_guilds,
        read_settings=read_settings,
        read_roles=read_roles,
        read_channels=read_channels,
        read_panel=read_panel,
        read_audit=read_audit,
        read_overview=read_overview,
        write_settings=write_settings,
        post_panel=post_panel,
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


async def patch(client, path, token=None, json=None, data=None):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    kwargs = {"data": data} if data is not None else {"json": json}
    response = await client.patch(path, headers=headers, **kwargs)
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

    def test_writes_have_their_own_smaller_budget(self):
        """A GET costs Discord nothing; a save costs it real REST calls.

        The general budget was sized when every route was a cached read, so
        writes are metered again on top of it rather than sharing that number.
        """

        async def scenario(client):
            results = []
            for _ in range(3):
                results.append(
                    await patch(client, SETTINGS_PATH, token_for(WRITE_OP),
                                json={"fields": {"instructions_locale": "de"}})
                )
            return results

        results = serve(scenario, config=make_config(write_rate_limit=2))
        assert [status for status, _ in results] == [200, 200, 429]

    def test_reads_do_not_spend_the_write_budget(self):
        """Otherwise hammering the picker would lock an admin out of saving."""

        async def scenario(client):
            for _ in range(5):
                await get(client, SETTINGS_PATH, token_for(SETTINGS_OP))
            return await patch(
                client, SETTINGS_PATH, token_for(WRITE_OP),
                json={"fields": {"instructions_locale": "de"}},
            )

        status, _body = serve(scenario, config=make_config(write_rate_limit=2))
        assert status == 200

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

    def token(self, actor_id=ADMIN_ID):
        return bot_api.mint_token(
            SIGNING_KEY, actor_id=actor_id, operation=self.OP, guild_id=None
        )

    def test_returns_only_the_guilds_the_bot_is_in(self):
        async def scenario(client):
            return await get(
                client,
                f"/api/v1/guilds?ids={GUILD_ID},{OTHER_GUILD_ID}",
                self.token(),
            )

        status, body = serve(scenario)
        assert status == 200
        assert body == {"present": [str(GUILD_ID)]}

    def test_it_is_not_a_bot_presence_oracle(self):
        """The bug this endpoint shipped with, pinned shut.

        It used to answer "is the bot in this guild?" for any id the caller
        sent, making it the one endpoint not bounded by the bot's own authority
        check. Since a compromised dashboard holds the signing key and can mint
        a token for any actor, that let an attacker walk arbitrary ids and
        enumerate every server running this bot.
        """

        async def scenario(client):
            return await get(
                client,
                # GUILD_ID is a real guild the bot is in — but this caller has
                # no standing in it, so it must come back indistinguishable
                # from one the bot has never joined.
                f"/api/v1/guilds?ids={GUILD_ID},{OTHER_GUILD_ID}",
                self.token(actor_id=MEMBER_ID),
            )

        status, body = serve(scenario)
        assert status == 200
        assert body == {"present": []}

    def test_an_unreadable_answer_is_a_503_not_an_empty_list(self):
        """An empty list means "none of these"; it must not mean "we failed"."""

        async def unavailable(user_id, guild_ids):
            return None

        async def scenario(client):
            return await get(
                client, f"/api/v1/guilds?ids={GUILD_ID}", self.token()
            )

        status, body = serve(scenario, deps=make_deps(read_admin_guilds=unavailable))
        assert status == 503
        assert body["error"] == "unavailable"

    def test_a_guild_scoped_token_cannot_reach_it(self):
        async def scenario(client):
            return await get(client, "/api/v1/guilds", token_for(self.OP))

        status, body = serve(scenario)
        assert status == 403
        assert body["error"] == "wrong_guild"

    def test_an_oversized_query_is_refused(self):
        async def scenario(client):
            ids = ",".join(str(n) for n in range(bot_api.MAX_GUILD_IDS + 1))
            return await get(client, f"/api/v1/guilds?ids={ids}", self.token())

        status, body = serve(scenario)
        assert status == 400
        assert body["error"] == "too_many_ids"

    def test_the_actor_is_taken_from_the_token_not_the_query(self):
        """There is no way to ask on someone else's behalf."""
        seen = {}

        async def record(user_id, guild_ids):
            seen["actor"] = int(user_id)
            return []

        async def scenario(client):
            return await get(
                client,
                f"/api/v1/guilds?ids={GUILD_ID}&user_id={ADMIN_ID}",
                self.token(actor_id=MEMBER_ID),
            )

        serve(scenario, deps=make_deps(read_admin_guilds=record))
        assert seen["actor"] == MEMBER_ID


# -------------------------------------------------------------------
# Pinning the phase
# -------------------------------------------------------------------
WRITE_OP = "PATCH /api/v1/guilds/{guild_id}/settings"


class TestTheWritePath:
    """The one route that changes something gets the same gates as the reads."""

    def path(self, guild_id=GUILD_ID):
        return f"/api/v1/guilds/{guild_id}/settings"

    def test_an_administrator_can_write(self):
        written = []

        async def scenario(client):
            return await patch(
                client,
                self.path(),
                token_for(WRITE_OP),
                json={"fields": {"instructions_locale": "de"}},
            )

        status, body = serve(scenario, deps=make_deps(written=written))
        assert status == 200
        assert written == [(GUILD_ID, ADMIN_ID, {"instructions_locale": "de"})]

    def test_a_read_token_cannot_be_replayed_as_a_write(self):
        """The single most important property of this endpoint.

        Both are `/api/v1/guilds/{id}/settings`; only the method differs, and
        the method is inside the signed operation.
        """
        written = []

        async def scenario(client):
            return await patch(
                client,
                self.path(),
                token_for(SETTINGS_OP),  # the GET token
                json={"fields": {"instructions_locale": "de"}},
            )

        status, body = serve(scenario, deps=make_deps(written=written))
        assert status == 403
        assert body["error"] == "wrong_operation"
        assert written == []

    def test_a_write_token_for_one_guild_does_not_work_on_another(self):
        written = []

        async def scenario(client):
            return await patch(
                client,
                self.path(),
                token_for(WRITE_OP, guild_id=GUILD_ID + 1),
                json={"fields": {"instructions_locale": "de"}},
            )

        status, body = serve(scenario, deps=make_deps(written=written))
        assert status == 403
        assert body["error"] == "wrong_guild"
        assert written == []

    def test_a_non_administrator_cannot_write(self):
        written = []

        async def scenario(client):
            return await patch(
                client,
                self.path(),
                token_for(WRITE_OP, actor_id=ADMIN_ID + 1),
                json={"fields": {"instructions_locale": "de"}},
            )

        status, body = serve(scenario, deps=make_deps(written=written))
        assert status == 403
        assert body["error"] == "not_administrator"
        assert written == []

    def test_a_write_token_cannot_be_used_twice(self):
        written = []
        token = token_for(WRITE_OP)

        async def scenario(client):
            first = await patch(
                client, self.path(), token, json={"fields": {"instructions_locale": "de"}}
            )
            second = await patch(
                client, self.path(), token, json={"fields": {"instructions_locale": "ja"}}
            )
            return first, second

        (first_status, _), (second_status, second_body) = serve(
            scenario, deps=make_deps(written=written)
        )
        assert first_status == 200
        # 401, same as a replayed read token: the token is simply no longer
        # valid. 403 is reserved for a token that is valid but not for this.
        assert second_status == 401
        assert second_body["error"] == "replayed"
        # And the replay never reached the database.
        assert len(written) == 1

    def test_no_token_is_a_401_and_writes_nothing(self):
        written = []

        async def scenario(client):
            return await patch(
                client, self.path(), json={"fields": {"instructions_locale": "de"}}
            )

        status, body = serve(scenario, deps=make_deps(written=written))
        assert status == 401
        assert written == []

    @pytest.mark.parametrize(
        "body",
        [
            {},                       # no fields key
            {"fields": {}},           # empty
            {"fields": "everything"}, # not an object
            {"fields": None},
            ["fields"],               # not an object at the top level
        ],
    )
    def test_a_malformed_body_is_refused(self, body):
        written = []

        async def scenario(client):
            return await patch(client, self.path(), token_for(WRITE_OP), json=body)

        status, _body = serve(scenario, deps=make_deps(written=written))
        assert status == 400
        assert written == []

    def test_a_body_that_is_not_json_is_refused(self):
        written = []

        async def scenario(client):
            return await patch(
                client, self.path(), token_for(WRITE_OP), data=b"{not json"
            )

        status, body = serve(scenario, deps=make_deps(written=written))
        assert status == 400
        assert body["error"] == "bad_json"
        assert written == []

    def test_too_many_fields_is_refused_before_the_bot_is_asked(self):
        written = []
        crowd = {f"field_{n}": n for n in range(bot_api.MAX_SETTINGS_FIELDS + 1)}

        async def scenario(client):
            return await patch(
                client, self.path(), token_for(WRITE_OP), json={"fields": crowd}
            )

        status, body = serve(scenario, deps=make_deps(written=written))
        assert status == 400
        assert body["error"] == "too_many_fields"
        assert written == []

    def test_a_rejected_setting_becomes_a_400(self):
        async def refuse(guild_id, actor_id, changes):
            raise bot_api.SettingRejected("instructions_locale", "unsupported_language")

        async def scenario(client):
            return await patch(
                client,
                self.path(),
                token_for(WRITE_OP),
                json={"fields": {"instructions_locale": "kl"}},
            )

        status, body = serve(scenario, deps=make_deps(write_settings=refuse))
        assert status == 400
        assert body["error"] == "unsupported_language"

    def test_a_locked_setting_becomes_a_403(self):
        async def refuse(guild_id, actor_id, changes):
            raise bot_api.SettingRejected(
                "panel_embed_color", "requires_premium", locked=True
            )

        async def scenario(client):
            return await patch(
                client,
                self.path(),
                token_for(WRITE_OP),
                json={"fields": {"panel_embed_color": 255}},
            )

        status, body = serve(scenario, deps=make_deps(write_settings=refuse))
        assert status == 403
        assert body["error"] == "requires_premium"

    def test_a_writer_that_cannot_complete_is_a_503(self):
        async def give_up(guild_id, actor_id, changes):
            return None

        async def scenario(client):
            return await patch(
                client,
                self.path(),
                token_for(WRITE_OP),
                json={"fields": {"instructions_locale": "de"}},
            )

        status, body = serve(scenario, deps=make_deps(write_settings=give_up))
        assert status == 503
        assert body["error"] == "unavailable"

    def test_a_guild_the_bot_is_not_in_is_a_404(self):
        written = []
        other = GUILD_ID + 99

        async def scenario(client):
            return await patch(
                client,
                self.path(other),
                token_for(WRITE_OP, guild_id=other),
                json={"fields": {"instructions_locale": "de"}},
            )

        status, _body = serve(scenario, deps=make_deps(written=written))
        assert status == 404
        assert written == []

    def test_the_actor_written_is_the_one_in_the_token(self):
        """Not anything the dashboard put in the body."""
        written = []

        async def scenario(client):
            return await patch(
                client,
                self.path(),
                token_for(WRITE_OP),
                json={
                    "fields": {"instructions_locale": "de"},
                    "actor_id": ADMIN_ID + 5000,
                },
            )

        serve(scenario, deps=make_deps(written=written))
        _guild, actor, _changes = written[0]
        assert actor == ADMIN_ID


class TestWriteSurfaceIsExactlyTwoThings:
    """These pin how far the API can change things, and no further.

    Editing any of them is how you widen what the website can do to a server,
    which is the point: it cannot happen as a side effect of adding a route or
    a dependency.
    """

    def test_the_only_mutating_routes_are_the_settings_patch_and_the_panel_post(self):
        app = bot_api.create_app(make_config(), make_deps())
        writes = {
            (route.method, route.resource.canonical)
            for route in app.router.routes()
            if route.method not in {"GET", "HEAD"}
        }
        assert writes == {
            ("PATCH", "/api/v1/guilds/{guild_id}/settings"),
            ("POST", "/api/v1/guilds/{guild_id}/panel"),
        }, f"an unexpected write route appeared: {writes}"

    def test_the_api_holds_one_writer_and_one_action(self):
        assert bot_api.deps_field_names() == frozenset(
            {
                "is_ready",
                "guild_present",
                "is_admin",
                "read_admin_guilds",
                "read_settings",
                "read_roles",
                "read_channels",
                "read_panel",
                "read_audit",
                "read_overview",
                "write_settings",
                "post_panel",
            }
        )

    def test_every_capability_is_named_for_what_it_does(self):
        for field in dataclass_fields(bot_api.BotAPIDeps):
            assert field.name.startswith(("read_", "is_", "guild_", "write_", "post_"))

    def test_the_module_decides_nothing_about_which_fields_are_writable(self):
        """The allowlist lives in the bot, not in the internet-facing path.

        bot_api is imported by the dashboard's half of the contract and is the
        module a compromised request reaches first. If it knew the field names
        it would be a place to negotiate about them.
        """
        source = open(bot_api.__file__, encoding="utf-8").read()
        for field_name in ("instructions_locale", "panel_embed_color", "panel_show_icon"):
            assert field_name not in source


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
    def __init__(self, channel_id, name, position=0, news=False, sendable=True,
                 embeddable=None):
        self.id = channel_id
        self.name = name
        self.position = position
        self.category = None
        self._news = news
        self._sendable = sendable
        # Defaults to following send_messages, because the interesting case is
        # the channel that grants one and not the other -- which accepts the
        # verification log and refuses the instructions panel.
        self._embeddable = sendable if embeddable is None else embeddable

    def is_news(self):
        return self._news

    def permissions_for(self, _member):
        return SimpleNamespace(
            view_channel=True,
            send_messages=self._sendable,
            embed_links=self._embeddable,
        )


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
            session.query(bot.DashboardAudit).delete()

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

    def test_the_sku_travels_with_the_plan(self, free):
        """So the dashboard's upgrade link cannot name the wrong product.

        The website has no copy of this id and no way to derive one; if the bot
        stops sending it the link disappears rather than going somewhere else.
        """
        make_server(row_id=9000)
        payload = run(bot.read_dashboard_settings(GUILD_ID))
        assert payload["premium"]["sku_id"] == str(SKU_ID)

    def test_no_sku_is_sent_while_the_tier_is_off(self):
        """PREMIUM_SKU_ID unset is the kill switch, and nothing is for sale."""
        make_server()
        payload = run(bot.read_dashboard_settings(GUILD_ID))
        assert payload["premium"]["enforced"] is False
        assert payload["premium"]["sku_id"] is None

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


def audit_rows():
    with bot.session_scope() as session:
        return [
            (row.field, row.old_value, row.new_value, row.actor_id)
            for row in session.query(bot.DashboardAudit)
            .order_by(bot.DashboardAudit.id)
            .all()
        ]


def write(changes, guild_id=GUILD_ID, actor_id=ADMIN_ID):
    return run(bot.write_dashboard_settings(guild_id, actor_id, changes))


class TestSettingsWriter:
    """The bot side of the first write path. Validation lives here, not on the
    website, because the website is the half assumed to fall over eventually."""

    def test_a_locale_is_stored_and_audited(self, subscribed):
        make_server()
        result = write({"instructions_locale": "de"})
        assert result["fields"]["instructions_locale"]["value"] == "de"
        assert audit_rows() == [
            ("instructions_locale", "en-US", "de", str(ADMIN_ID))
        ]

    def test_branding_is_stored_and_audited(self, subscribed):
        make_server()
        write({"panel_embed_color": 0xFF0000, "panel_show_icon": True})
        assert bot.load_panel_branding(GUILD_ID) == (0xFF0000, True)
        assert {row[0] for row in audit_rows()} == {
            "panel_embed_color",
            "panel_show_icon",
        }

    def test_a_no_op_write_is_not_audited(self):
        """A form posting every field must not bury the one line that matters."""
        make_server(instructions_locale="en-US")
        write({"instructions_locale": "en-US"})
        assert audit_rows() == []

    def restyles(self, monkeypatch):
        seen = []

        async def fake_restyle(guild_id):
            seen.append(str(guild_id))
            return "ok"

        monkeypatch.setattr(bot, "restyle_instruction_panel", fake_restyle)
        return seen

    def test_a_saved_language_is_applied_to_the_live_panel(
        self, monkeypatch, subscribed
    ):
        """Saving the setting is not the whole job -- the panel renders it.

        Nothing else would: the fleet sweep refreshes the view but passes
        rebuild_embed=False, so the embed would keep the old language until an
        operator forced a full refresh.
        """
        seen = self.restyles(monkeypatch)
        make_server()
        write({"instructions_locale": "de"})
        assert seen == [str(GUILD_ID)]

    def test_saved_branding_is_applied_to_the_live_panel(
        self, monkeypatch, subscribed
    ):
        seen = self.restyles(monkeypatch)
        make_server()
        write({"panel_embed_color": 0xFF0000})
        assert seen == [str(GUILD_ID)]

    def test_a_setting_the_panel_does_not_show_edits_no_panel(
        self, monkeypatch, subscribed
    ):
        """A message edit per save would be a rate limit nobody asked for.

        The audit assertion is load-bearing, not decoration. A save that
        silently did nothing would leave `seen` empty too, so without proof the
        write landed this passes whether or not the field filter works -- which
        is how it was written the first time, using a role id that never got
        past the "role must be in this guild" check because no guild is mocked
        in this class.
        """
        seen = self.restyles(monkeypatch)
        make_server()
        write({"custom_verification_requested_message": "Welcome aboard!"})
        assert [row[0] for row in audit_rows()] == [
            "custom_verification_requested_message"
        ]
        assert seen == []

    def test_a_no_op_language_save_edits_no_panel(self, monkeypatch, subscribed):
        seen = self.restyles(monkeypatch)
        make_server(instructions_locale="en-US")
        write({"instructions_locale": "en-US"})
        assert seen == []

    def test_the_message_discord_receives_is_in_the_new_language(
        self, monkeypatch, subscribed
    ):
        """The whole chain, faked only at the HTTP boundary.

        Every step between the save and the edit is real -- the servers row,
        load_instruction_panel, resolve_panel_style, build_instructions_embed --
        because the halves each worked and the language still did not change.
        """
        edits = []

        class FakeMessage:
            async def edit(self, **payload):
                edits.append(payload)

        monkeypatch.setattr(
            bot.bot,
            "get_partial_messageable",
            lambda _cid: SimpleNamespace(get_partial_message=lambda _mid: FakeMessage()),
        )
        make_server(instructions_channel_id="70", instructions_message_id="900")

        write({"instructions_locale": "ja"})

        assert len(edits) == 1
        german = bot.build_instructions_embed("ja")
        assert edits[0]["embed"].title == german.title
        assert edits[0]["embed"].title != bot.build_instructions_embed("en-US").title

    def test_half_a_branding_change_keeps_the_other_half(self, subscribed):
        make_server()
        write({"panel_embed_color": 0x00FF00, "panel_show_icon": True})
        write({"panel_embed_color": 0x0000FF})
        # The icon was not submitted, so it must survive the merge.
        assert bot.load_panel_branding(GUILD_ID) == (0x0000FF, True)

    @pytest.mark.parametrize(
        "changes",
        [
            {"instructions_locale": "kl"},        # not a supported language
            {"instructions_locale": 5},
            {"panel_embed_color": -1},
            {"panel_embed_color": 0x1000000},     # 25 bits
            {"panel_embed_color": "red"},
            {"panel_embed_color": True},          # bool is an int; not a colour
            {"panel_show_icon": "yes"},
            {"panel_show_icon": 1},
        ],
    )
    def test_a_bad_value_is_rejected(self, changes, subscribed):
        make_server()
        with pytest.raises(bot.SettingRejected) as caught:
            write(changes)
        assert caught.value.locked is False
        assert audit_rows() == []

    def test_every_declared_setting_is_now_writable(self):
        """Step 5 is finished: the whole allowlist is open.

        Kept as an equality rather than deleted, so adding a SettingsField
        forces a decision about whether the website may set it. Failing here
        means someone has to choose, which is the point.
        """
        assert bot.DASHBOARD_WRITABLE_FIELDS == {
            field.name for field in bot.SETTINGS_FIELDS
        }

    def test_an_unknown_field_is_refused(self, subscribed):
        make_server()
        with pytest.raises(bot.SettingRejected) as caught:
            write({"is_admin": True})
        assert caught.value.reason == "unknown_field"

    def test_a_free_server_cannot_write_branding(self, free):
        """The gate is here, not in whatever the website chose to render."""
        make_server(row_id=500)
        with pytest.raises(bot.SettingRejected) as caught:
            write({"panel_embed_color": 0xFF0000})
        assert caught.value.reason == "requires_premium"
        assert caught.value.locked is True
        assert bot.load_panel_branding(GUILD_ID) is None
        assert audit_rows() == []

    def test_a_free_server_can_still_write_its_language(self, free):
        """instructions_locale is not a premium feature, and must not become one."""
        make_server(row_id=500)
        write({"instructions_locale": "ja"})
        assert audit_rows()[0][:3] == ("instructions_locale", "en-US", "ja")

    def test_one_bad_field_saves_none_of_them(self, subscribed):
        make_server()
        with pytest.raises(bot.SettingRejected):
            write({"instructions_locale": "de", "panel_embed_color": "nope"})
        with bot.session_scope() as session:
            srv = session.query(bot.Server).filter_by(server_id=str(GUILD_ID)).first()
            assert srv.instructions_locale == "en-US"
        assert audit_rows() == []

    def test_a_guild_that_never_ran_setup_cannot_set_a_language(self, subscribed):
        """servers.owner_id is NOT NULL and the dashboard has no honest value.

        Inserting a row would also mint a fresh servers.id, pushing the guild
        above the grandfather line as a side effect of a language change.
        """
        with pytest.raises(bot.SettingRejected) as caught:
            write({"instructions_locale": "de"})
        assert caught.value.reason == "server_not_set_up"

    def test_unreadable_branding_is_refused_rather_than_overwritten(
        self, monkeypatch, subscribed
    ):
        make_server()
        monkeypatch.setattr(
            bot, "load_panel_branding", lambda guild_id: bot.BRANDING_UNREADABLE
        )
        assert write({"panel_embed_color": 0xFF0000}) is None
        assert audit_rows() == []

    def test_an_empty_change_set_is_refused(self, subscribed):
        make_server()
        for empty in ({}, None, "fields"):
            with pytest.raises(bot.SettingRejected):
                write(empty)

    # ----- the verification group -----
    def guild_with_roles(self, monkeypatch):
        guild = FakeGuild(
            roles=[
                FakeRole(1, "@everyone", 0, default=True),
                FakeRole(2, "Verified", 10),
                FakeRole(3, "Unverified", 9),
                FakeRole(4, "Admins", 99),          # above the bot
                FakeRole(5, "Booster", 5, managed=True),
            ]
        )
        monkeypatch.setattr(bot.bot, "get_guild", lambda _id: guild)
        return guild

    def test_a_verified_role_is_stored_and_audited(self, monkeypatch, subscribed):
        self.guild_with_roles(monkeypatch)
        make_server(role_id="2")
        write({"role_id": "3"})
        assert audit_rows() == [("role_id", "2", "3", str(ADMIN_ID))]

    def test_an_unverified_role_can_be_cleared(self, monkeypatch, subscribed):
        """/vrcverify_setup clears it by omitting the argument, so this must too."""
        self.guild_with_roles(monkeypatch)
        make_server(role_id="2", unverified_role_id="3")
        write({"unverified_role_id": None})
        with bot.session_scope() as session:
            srv = session.query(bot.Server).filter_by(server_id=str(GUILD_ID)).first()
            assert srv.unverified_role_id is None

    def test_the_verified_role_cannot_be_cleared(self, monkeypatch, subscribed):
        """verified_role is a required argument on the slash command."""
        self.guild_with_roles(monkeypatch)
        make_server(role_id="2")
        with pytest.raises(bot.SettingRejected) as caught:
            write({"role_id": None})
        assert caught.value.reason == "role_required"

    @pytest.mark.parametrize("wanted", ["1", "9999", "not-a-number", True])
    def test_a_role_that_is_not_a_real_role_here_is_refused(
        self, monkeypatch, subscribed, wanted
    ):
        """`1` is @everyone, `9999` is another guild's or a deleted one.

        Discord's role picker gives the slash command this guarantee for free.
        The dashboard submits a raw id, so it has to provide it for itself.
        """
        self.guild_with_roles(monkeypatch)
        make_server(role_id="2")
        with pytest.raises(bot.SettingRejected):
            write({"role_id": wanted})
        assert audit_rows() == []

    @pytest.mark.parametrize("wanted", ["4", "5"])
    def test_an_unassignable_role_is_allowed_and_only_warned_about(
        self, monkeypatch, subscribed, wanted
    ):
        """Above the bot, or managed by an integration.

        /vrcverify_setup performs no hierarchy check at all, so refusing here
        would make the website stricter than the slash command -- and would
        block an admin who means to set the role first and fix the ordering
        after. The settings page shows the warning instead.
        """
        self.guild_with_roles(monkeypatch)
        make_server(role_id="2")
        write({"role_id": wanted})
        with bot.session_scope() as session:
            srv = session.query(bot.Server).filter_by(server_id=str(GUILD_ID)).first()
            assert srv.role_id == wanted

    def test_an_id_that_arrived_as_a_number_is_stored_as_a_string(
        self, monkeypatch, subscribed
    ):
        self.guild_with_roles(monkeypatch)
        make_server(role_id="2")
        write({"role_id": 3})
        with bot.session_scope() as session:
            srv = session.query(bot.Server).filter_by(server_id=str(GUILD_ID)).first()
            assert srv.role_id == "3"

    def test_auto_verify_is_written_for_a_free_server(self, free):
        """Never gated, for anyone, ever."""
        make_server(row_id=500, auto_verify_new_members=True)
        write({"auto_verify_new_members": False})
        assert audit_rows()[0][:3] == ("auto_verify_new_members", "True", "False")

    def test_auto_verify_is_refused_when_the_column_is_missing(
        self, monkeypatch, subscribed
    ):
        make_server()
        monkeypatch.setattr(bot, "server_has_column", lambda name: False)
        with pytest.raises(bot.SettingRejected) as caught:
            write({"auto_verify_new_members": False})
        assert caught.value.reason == "column_missing"

    def test_a_guild_the_bot_cannot_see_does_not_write_a_role(
        self, monkeypatch, subscribed
    ):
        make_server(role_id="2")
        monkeypatch.setattr(bot.bot, "get_guild", lambda _id: None)
        assert write({"role_id": "3"}) is None
        assert audit_rows() == []

    # ----- the custom DM -----
    def test_a_custom_message_is_stored_sanitised(self, subscribed):
        """The stored text, not the submitted text.

        The @everyone defusal has to survive into the database -- passing the
        check and then saving the original would be worse than no check.
        """
        make_server()
        write({"custom_verification_requested_message": "Welcome @everyone!"})
        with bot.session_scope() as session:
            srv = session.query(bot.Server).filter_by(server_id=str(GUILD_ID)).first()
            assert "@everyone" not in srv.custom_verification_requested_message
            assert "@ everyone" in srv.custom_verification_requested_message

    @pytest.mark.parametrize(
        "text",
        [
            "Join us at https://evil.example.com",
            "See http://discord.com.attacker.test/x",
        ],
    )
    def test_a_message_linking_off_the_allowlist_is_refused(self, text, subscribed):
        make_server()
        with pytest.raises(bot.SettingRejected) as caught:
            write({"custom_verification_requested_message": text})
        assert caught.value.reason == "message_links_not_allowed"
        assert audit_rows() == []

    def test_an_allowed_link_is_accepted(self, subscribed):
        make_server()
        write(
            {"custom_verification_requested_message": "Guide: https://vrchat.com/home"}
        )
        assert audit_rows()[0][0] == "custom_verification_requested_message"

    def test_a_message_over_the_modal_cap_is_refused(self, subscribed):
        make_server()
        with pytest.raises(bot.SettingRejected) as caught:
            write(
                {
                    "custom_verification_requested_message": "x"
                    * (bot.CUSTOM_MESSAGE_MAX_LEN + 1)
                }
            )
        assert caught.value.reason == "message_too_long"

    @pytest.mark.parametrize("text", ["", "   ", "clear", "None", "DEFAULT"])
    def test_the_words_the_slash_command_clears_on_also_clear_here(
        self, text, subscribed
    ):
        """Otherwise the website could store a value the command cannot set."""
        make_server(custom_verification_requested_message="old")
        write({"custom_verification_requested_message": text})
        with bot.session_scope() as session:
            srv = session.query(bot.Server).filter_by(server_id=str(GUILD_ID)).first()
            assert srv.custom_verification_requested_message is None

    # ----- the log channel -----
    def guild_with_channels(self, monkeypatch, sendable=True):
        guild = FakeGuild(
            channels=[
                FakeChannel(70, "verify-log", position=0, sendable=sendable),
                FakeChannel(71, "announcements", position=1, news=True),
            ]
        )
        monkeypatch.setattr(bot.bot, "get_guild", lambda _id: guild)
        return guild

    def test_a_log_channel_is_stored_and_audited(self, monkeypatch, subscribed):
        self.guild_with_channels(monkeypatch)
        make_server()
        write({"verification_log_channel_id": "70"})
        assert bot.load_log_channel_id(GUILD_ID) == "70"
        assert audit_rows()[0][:3] == ("verification_log_channel_id", None, "70")

    def test_a_log_channel_can_be_cleared(self, monkeypatch, subscribed):
        self.guild_with_channels(monkeypatch)
        make_server()
        bot.set_log_channel(GUILD_ID, "70")
        write({"verification_log_channel_id": None})
        assert bot.load_log_channel_id(GUILD_ID) is None

    def test_an_announcement_channel_is_refused(self, monkeypatch, subscribed):
        """Other servers can follow one, republishing an age disclosure."""
        self.guild_with_channels(monkeypatch)
        make_server()
        with pytest.raises(bot.SettingRejected) as caught:
            write({"verification_log_channel_id": "71"})
        assert caught.value.reason == "channel_is_announcement"
        assert bot.load_log_channel_id(GUILD_ID) is None

    def test_a_channel_from_another_guild_is_refused(self, monkeypatch, subscribed):
        self.guild_with_channels(monkeypatch)
        make_server()
        with pytest.raises(bot.SettingRejected) as caught:
            write({"verification_log_channel_id": "9999"})
        assert caught.value.reason == "channel_not_in_guild"

    def test_a_channel_the_bot_cannot_post_in_is_refused(self, monkeypatch, subscribed):
        """/vrcverify_logchannel finds this out by posting; we ask instead."""
        self.guild_with_channels(monkeypatch, sendable=False)
        make_server()
        with pytest.raises(bot.SettingRejected) as caught:
            write({"verification_log_channel_id": "70"})
        assert caught.value.reason == "channel_not_writable"

    def test_a_free_server_cannot_set_a_log_channel(self, monkeypatch, free):
        self.guild_with_channels(monkeypatch)
        make_server(row_id=500)
        with pytest.raises(bot.SettingRejected) as caught:
            write({"verification_log_channel_id": "70"})
        assert caught.value.reason == "requires_premium"
        assert bot.load_log_channel_id(GUILD_ID) is None

    # ----- nickname sync -----
    def test_nickname_sync_is_premium_only(self, free):
        make_server(row_id=500)
        with pytest.raises(bot.SettingRejected) as caught:
            write({"auto_nickname_change": True})
        assert caught.value.locked is True

    def test_nickname_sync_is_stored_for_a_premium_server(self, subscribed):
        make_server()
        write({"auto_nickname_change": True})
        assert audit_rows()[0][:3] == ("auto_nickname_change", "False", "True")

    def test_the_writer_reports_the_stored_state_not_the_request(self, subscribed):
        """What the admin sees next is what is saved, not what they submitted."""
        make_server()
        result = write({"instructions_locale": "ja"})
        assert result == run(bot.read_dashboard_settings(GUILD_ID))


async def _noop_delete():
    return None


async def _style_ok(_guild_id, _guild):
    """A resolvable panel style, so probe reaches the edit."""
    return (0x5865F2, None)


def _always(outcome):
    async def probe(entry, rebuild_embed, already_checked=False):
        return outcome

    return probe


class TestPanelAction:
    """Posting a panel is the one thing that shows up in somebody's server."""

    def setup_guild(self, monkeypatch, sendable=True, sent=None, webhook_owned=False,
                    deleted=None):
        sent = [] if sent is None else sent
        deleted = [] if deleted is None else deleted

        class Sendable(FakeChannel):
            async def send(self, **kwargs):
                sent.append((self.id, kwargs))
                new_id = 555000 + self.id

                async def delete():
                    deleted.append(new_id)

                return SimpleNamespace(id=new_id, delete=delete)

            def permissions_for(self, _member):
                return SimpleNamespace(
                    view_channel=True, send_messages=sendable, embed_links=sendable
                )

            async def fetch_message(self, message_id):
                # webhook_id set means the panel was posted as a slash-command
                # reply, which Discord will not let the bot edit the embed of.
                return SimpleNamespace(
                    id=message_id,
                    webhook_id=1335738139825799188 if webhook_owned else None,
                )

            def get_partial_message(self, message_id):
                async def delete():
                    deleted.append(message_id)

                return SimpleNamespace(id=message_id, delete=delete)

        guild = FakeGuild(
            channels=[Sendable(70, "verify"), Sendable(71, "general", position=1)]
        )
        monkeypatch.setattr(bot.bot, "get_guild", lambda _id: guild)
        monkeypatch.setattr(
            bot, "build_instructions_embed", lambda *a, **k: SimpleNamespace()
        )
        monkeypatch.setattr(
            bot, "VRCVerifyInstructionView", lambda **k: SimpleNamespace()
        )
        return sent

    def post(self, channel_id="70"):
        return run(bot.post_dashboard_panel(GUILD_ID, ADMIN_ID, channel_id))

    def test_a_first_post_records_where_it_went(self, monkeypatch, subscribed):
        sent = self.setup_guild(monkeypatch)
        make_server()
        result = self.post()
        assert result["action"] == "posted"
        assert len(sent) == 1
        with bot.session_scope() as session:
            srv = session.query(bot.Server).filter_by(server_id=str(GUILD_ID)).first()
            assert srv.instructions_channel_id == "70"
            assert srv.instructions_message_id == "555070"

    def test_posting_into_the_same_channel_refreshes_instead(
        self, monkeypatch, subscribed
    ):
        """The double-click case. One edit, not a second live panel."""
        sent = self.setup_guild(monkeypatch)
        make_server(instructions_channel_id="70", instructions_message_id="900")
        probed = []

        async def fake_probe(entry, rebuild_embed, already_checked=False):
            # The panel button has already asked whether the message is
            # editable, so probe must not spend a second fetch on it.
            assert already_checked is True
            probed.append(entry)
            return "ok"

        monkeypatch.setattr(bot, "probe_instruction_panel", fake_probe)

        assert self.post()["action"] == "refreshed"
        assert sent == []          # nothing new was posted
        assert len(probed) == 1

    def test_a_different_channel_is_a_move_and_says_where_the_old_one_is(
        self, monkeypatch, subscribed
    ):
        sent = self.setup_guild(monkeypatch)
        make_server(instructions_channel_id="70", instructions_message_id="900")
        result = self.post("71")
        assert result["action"] == "moved"
        assert result["previous_channel_id"] == "70"
        assert len(sent) == 1

    def test_a_record_pointing_at_a_deleted_message_posts_afresh(
        self, monkeypatch, subscribed
    ):
        sent = self.setup_guild(monkeypatch)
        make_server(instructions_channel_id="70", instructions_message_id="900")

        async def gone(entry, rebuild_embed, already_checked=False):
            return "gone"

        monkeypatch.setattr(bot, "probe_instruction_panel", gone)
        assert self.post()["action"] == "posted"
        assert len(sent) == 1

    def test_nothing_is_posted_when_the_record_cannot_be_read(
        self, monkeypatch, subscribed
    ):
        """"No panel" and "the database blinked" look identical here.

        Guessing wrong leaves a duplicate in somebody's server, so it refuses.
        """
        sent = self.setup_guild(monkeypatch)
        make_server()

        def boom(_guild_id):
            raise RuntimeError("db down")

        monkeypatch.setattr(bot, "_stored_panel_location", boom)
        assert self.post() is None
        assert sent == []

    def test_a_channel_outside_the_guild_is_refused(self, monkeypatch, subscribed):
        sent = self.setup_guild(monkeypatch)
        make_server()
        with pytest.raises(bot.SettingRejected) as caught:
            self.post("9999")
        assert caught.value.reason == "channel_not_in_guild"
        assert sent == []

    def test_a_channel_the_bot_cannot_post_in_is_refused(
        self, monkeypatch, subscribed
    ):
        sent = self.setup_guild(monkeypatch, sendable=False)
        make_server()
        with pytest.raises(bot.SettingRejected) as caught:
            self.post()
        assert caught.value.reason == "channel_not_writable"
        assert sent == []

    def test_a_locked_channel_can_still_have_its_own_panel_refreshed(
        self, monkeypatch, subscribed
    ):
        """Send Messages is the wrong test for a refresh -- it edits.

        The startup sweep edits this panel every restart with no permission
        check at all, so refusing here would make the dashboard stricter than
        the bot it configures.
        """
        sent = self.setup_guild(monkeypatch, sendable=False)
        make_server(instructions_channel_id="70", instructions_message_id="900")
        monkeypatch.setattr(
            bot, "probe_instruction_panel", _always("ok")
        )
        assert self.post()["action"] == "refreshed"
        assert sent == []

    def test_a_refresh_discord_actually_refuses_is_reported(
        self, monkeypatch, subscribed
    ):
        """Attempting the edit is the honest test; Forbidden is its answer."""
        self.setup_guild(monkeypatch)
        make_server(instructions_channel_id="70", instructions_message_id="900")
        monkeypatch.setattr(bot, "probe_instruction_panel", _always("forbidden"))
        with pytest.raises(bot.SettingRejected) as caught:
            self.post()
        assert caught.value.reason == "channel_not_writable"

    def test_a_locked_channel_holding_no_panel_is_still_refused(
        self, monkeypatch, subscribed
    ):
        """The relaxation is only for the channel the panel is already in."""
        sent = self.setup_guild(monkeypatch, sendable=False)
        make_server(instructions_channel_id="70", instructions_message_id="900")
        monkeypatch.setattr(bot, "probe_instruction_panel", _always("ok"))
        with pytest.raises(bot.SettingRejected) as caught:
            self.post("71")
        assert caught.value.reason == "channel_not_writable"
        assert sent == []

    def test_a_panel_discord_will_not_let_us_edit_is_replaced(
        self, monkeypatch, subscribed
    ):
        """The bug this whole path exists to survive.

        A panel posted as a slash-command reply belongs to a webhook. Discord
        answers 200 to an embed edit on one and keeps the old embed, so a
        language change came out as new buttons above the old text. Refreshing
        it again would be the same silent no-op, so it is replaced.
        """
        deleted = []
        sent = self.setup_guild(monkeypatch, webhook_owned=True, deleted=deleted)
        make_server(instructions_channel_id="70", instructions_message_id="900")
        monkeypatch.setattr(bot, "probe_instruction_panel", _always("ok"))

        result = self.post()

        assert result["action"] == "replaced"
        assert len(sent) == 1                    # a new message, not an edit
        assert deleted == [900]                  # and the dead one is gone
        with bot.session_scope() as session:
            srv = session.query(bot.Server).filter_by(server_id=str(GUILD_ID)).first()
            assert srv.instructions_message_id == "555070"

    def test_an_ordinary_panel_is_still_refreshed_not_replaced(
        self, monkeypatch, subscribed
    ):
        """Replacing an editable panel would throw away its pins and links."""
        deleted = []
        sent = self.setup_guild(monkeypatch, webhook_owned=False, deleted=deleted)
        make_server(instructions_channel_id="70", instructions_message_id="900")
        monkeypatch.setattr(bot, "probe_instruction_panel", _always("ok"))

        assert self.post()["action"] == "refreshed"
        assert sent == []
        assert deleted == []

    def test_nothing_is_replaced_when_the_message_cannot_be_read(
        self, monkeypatch, subscribed
    ):
        """"Cannot tell" must not read as "safe to replace" -- that is a duplicate.

        probe_instruction_panel is stubbed to succeed on purpose. Without it the
        refresh branch would fail on its own against a disconnected bot and
        return None too, so the assertion would hold whether or not the guard
        existed -- it would pass for the wrong reason, which is worse than not
        being written. Stubbed, "it wrongly carried on" shows up as "refreshed".
        """
        sent = self.setup_guild(monkeypatch)
        make_server(instructions_channel_id="70", instructions_message_id="900")

        async def unreadable(_channel, _message_id):
            return None

        monkeypatch.setattr(bot, "_panel_is_webhook_owned", unreadable)
        monkeypatch.setattr(bot, "probe_instruction_panel", _always("ok"))
        assert self.post() is None
        assert sent == []

    def test_a_failed_delete_does_not_undo_the_replacement(
        self, monkeypatch, subscribed
    ):
        """The new panel is up and recorded; the stale one is a cleanup problem."""
        sent = self.setup_guild(monkeypatch, webhook_owned=True)
        make_server(instructions_channel_id="70", instructions_message_id="900")

        class Boom(FakeChannel):
            pass

        def exploding_partial(_self, _mid):
            async def delete():
                raise RuntimeError("no manage messages")

            return SimpleNamespace(delete=delete)

        guild = bot.bot.get_guild(GUILD_ID)
        channel = next(c for c in guild.text_channels if c.id == 70)
        monkeypatch.setattr(
            type(channel), "get_partial_message", exploding_partial, raising=False
        )

        assert self.post()["action"] == "replaced"
        assert len(sent) == 1

    def test_two_overlapping_posts_do_not_produce_two_panels(
        self, monkeypatch, subscribed
    ):
        """The docstring's central promise, against a send that actually yields.

        The fixture's send returns without suspending, so the existing tests
        could never catch this: the function reads the recorded location and
        then awaits three times before writing a new one, so two requests in
        flight together both saw "nothing here" and both posted.
        """
        sent = []

        class Slow(FakeChannel):
            async def send(self, **kwargs):
                await asyncio.sleep(0)  # a real HTTP call suspends; so does this
                sent.append(self.id)
                return SimpleNamespace(id=555000 + self.id)

            def permissions_for(self, _member):
                return SimpleNamespace(
                    view_channel=True, send_messages=True, embed_links=True
                )

            async def fetch_message(self, message_id):
                await asyncio.sleep(0)
                return SimpleNamespace(id=message_id, webhook_id=None)

            def get_partial_message(self, message_id):
                return SimpleNamespace(id=message_id, delete=_noop_delete)

        guild = FakeGuild(channels=[Slow(70, "verify")])
        monkeypatch.setattr(bot.bot, "get_guild", lambda _id: guild)
        monkeypatch.setattr(bot, "build_instructions_embed", lambda *a, **k: SimpleNamespace())
        monkeypatch.setattr(bot, "VRCVerifyInstructionView", lambda **k: SimpleNamespace())
        # So the loser's refresh reports cleanly instead of failing against a
        # disconnected bot -- which would return None and hide whether the lock
        # worked behind a generic failure.
        monkeypatch.setattr(bot, "probe_instruction_panel", _always("ok"))
        make_server()

        async def both():
            return await asyncio.gather(
                bot.post_dashboard_panel(GUILD_ID, ADMIN_ID, "70"),
                bot.post_dashboard_panel(GUILD_ID, ADMIN_ID, "70"),
            )

        results = asyncio.run(both())

        assert len(sent) == 1, "a double click posted two live panels"
        # The second request found the first one's panel and refreshed it.
        assert {r["action"] for r in results} == {"posted", "refreshed"}

    def test_a_panel_that_cannot_be_recorded_is_taken_back_down(
        self, monkeypatch, subscribed
    ):
        """An unrecorded panel has live buttons and nothing that can find it.

        The admin sees a failure and clicks again, so leaving it up costs one
        orphan per retry.
        """
        deleted = []
        sent = self.setup_guild(monkeypatch, deleted=deleted)
        make_server()

        class Boom(Exception):
            pass

        real_scope = bot.session_scope
        calls = {"n": 0}

        def failing_scope(*args, **kwargs):
            # Let the reads through; break only the write that records the ids.
            calls["n"] += 1
            if calls["n"] >= 2:
                raise Boom("db down")
            return real_scope(*args, **kwargs)

        monkeypatch.setattr(bot, "session_scope", failing_scope)

        assert self.post() is None
        assert len(sent) == 1
        assert deleted == [555070], "the unrecorded panel was left live"

    def test_probe_skips_the_ownership_fetch_when_told_it_was_done(
        self, monkeypatch
    ):
        """One question, one fetch. The panel button has already asked.

        Without this the POST costs three Discord calls (two fetches and an
        edit) where the design assumes one edit.
        """
        asked = []

        async def spy(_channel, message_id):
            asked.append(message_id)
            return False

        monkeypatch.setattr(bot, "_panel_is_webhook_owned", spy)
        monkeypatch.setattr(bot, "resolve_panel_style", _style_ok)
        monkeypatch.setattr(bot, "build_instructions_embed", lambda *a, **k: SimpleNamespace())
        monkeypatch.setattr(bot, "VRCVerifyInstructionView", lambda **k: SimpleNamespace())

        edits = []

        class Msg:
            async def edit(self, **payload):
                edits.append(payload)

        monkeypatch.setattr(
            bot.bot, "get_partial_messageable",
            lambda _cid: SimpleNamespace(get_partial_message=lambda _mid: Msg()),
        )
        entry = {"server_id": str(GUILD_ID), "channel_id": "70",
                 "message_id": "900", "locale": "en-US"}

        run(bot.probe_instruction_panel(entry, rebuild_embed=True, already_checked=True))
        assert asked == [], "asked Discord a question the caller had answered"
        assert len(edits) == 1

        run(bot.probe_instruction_panel(entry, rebuild_embed=True))
        assert asked == ["900"], "the default must still check"

    def test_a_frozen_panel_makes_the_save_report_it(self, monkeypatch, subscribed):
        """Storing the value is not the same as the panel showing it.

        This is the production symptom that started the investigation: the
        setting saved, the page showed the new language, and the panel silently
        stayed as it was.
        """
        make_server()

        async def frozen(_guild_id):
            return "frozen"

        monkeypatch.setattr(bot, "restyle_instruction_panel", frozen)
        result = write({"instructions_locale": "de"})
        assert result["fields"]["instructions_locale"]["value"] == "de"
        assert result["panel_stale"] == "frozen"

    def test_a_clean_restyle_says_nothing_about_staleness(
        self, monkeypatch, subscribed
    ):
        make_server()

        async def fine(_guild_id):
            return "ok"

        monkeypatch.setattr(bot, "restyle_instruction_panel", fine)
        assert "panel_stale" not in write({"instructions_locale": "de"})

    def test_a_row_created_by_posting_names_the_real_owner(
        self, monkeypatch, subscribed
    ):
        """owner_id feeds resolve_config_admin, which decides who gets DMs.

        Filling it with whoever clicked quietly appoints that admin -- and on
        the dashboard they need not be the owner at all.
        """
        self.setup_guild(monkeypatch)
        with bot.session_scope() as session:
            session.query(bot.Server).delete()

        assert self.post()["action"] == "posted"
        with bot.session_scope() as session:
            srv = session.query(bot.Server).filter_by(server_id=str(GUILD_ID)).first()
            assert srv.owner_id == str(OWNER_ID)
            assert srv.owner_id != str(ADMIN_ID)

    def test_the_action_is_audited(self, monkeypatch, subscribed):
        self.setup_guild(monkeypatch)
        make_server()
        self.post()
        assert audit_rows()[0][:3] == ("instructions_panel", "posted", "70")

    def test_a_guild_the_bot_cannot_see_posts_nothing(self, monkeypatch, subscribed):
        monkeypatch.setattr(bot.bot, "get_guild", lambda _id: None)
        make_server()
        assert self.post() is None


class TestAuditReader:
    def test_it_returns_this_guilds_changes_newest_first(self, subscribed):
        make_server()
        write({"instructions_locale": "de"})
        write({"instructions_locale": "ja"})
        entries = run(bot.read_dashboard_audit(GUILD_ID))
        assert [e["new_value"] for e in entries] == ["ja", "de"]
        assert all(e["actor_id"] == str(ADMIN_ID) for e in entries)

    def test_it_never_returns_another_guilds_history(self, subscribed):
        make_server()
        write({"instructions_locale": "de"})
        assert run(bot.read_dashboard_audit(OTHER_GUILD_ID)) == []

    def test_an_actor_still_in_the_guild_is_named(self, monkeypatch, subscribed):
        guild = FakeGuild()
        guild._members[ADMIN_ID] = SimpleNamespace(display_name="Sasha")
        monkeypatch.setattr(bot.bot, "get_guild", lambda _id: guild)
        make_server()
        write({"instructions_locale": "de"})
        assert run(bot.read_dashboard_audit(GUILD_ID))[0]["actor_name"] == "Sasha"

    def test_an_actor_who_left_is_still_an_entry(self, monkeypatch, subscribed):
        """The admin who left is exactly the row worth keeping."""
        monkeypatch.setattr(bot.bot, "get_guild", lambda _id: FakeGuild())
        make_server()
        write({"instructions_locale": "de"})
        entry = run(bot.read_dashboard_audit(GUILD_ID))[0]
        assert entry["actor_name"] is None
        assert entry["actor_id"] == str(ADMIN_ID)

    def test_the_row_count_is_bounded(self, subscribed):
        make_server()
        assert run(bot.read_dashboard_audit(GUILD_ID, limit=10_000)) is not None
        # A hostile limit cannot turn one page load into a table scan.
        assert bot.MAX_AUDIT_ROWS <= 50

    def test_an_unreadable_trail_is_none_not_an_empty_history(self, monkeypatch):
        """Empty and unavailable must not look the same to an admin."""
        def boom():
            raise RuntimeError("db down")

        monkeypatch.setattr(bot, "session_scope", boom)
        assert run(bot.read_dashboard_audit(GUILD_ID)) is None


class TestBothHalvesTogether:
    """The dashboard's real client against the bot's real handler and writer.

    Everything else in this file fakes one side or the other, which cannot
    catch the seam between them: an operation string only one end updated, a
    body key spelled differently on each side, a colour that survives one
    validator and not the other. Here the only fake is the transport, which is
    plain HTTP because TLS is configured at the socket rather than in either
    application.
    """

    @pytest.fixture(autouse=True)
    def placeholder_certs(self, tmp_path):
        """requests stats these even for http, so they have to exist."""
        for name in ("client.pem", "client.key", "ca.pem"):
            (tmp_path / name).write_text("placeholder")
        self.certs = tmp_path

    def client_for(self, server):
        botapi = pytest.importorskip("dashboard.botapi")
        return botapi.BotAPIClient(
            str(server.make_url("")).rstrip("/"),
            client_cert=str(self.certs / "client.pem"),
            client_key=str(self.certs / "client.key"),
            ca_bundle=str(self.certs / "ca.pem"),
            signing_key=SIGNING_KEY,
        )

    def run_against_bot(self, call):
        """Drive the sync client from inside the loop running the server."""
        app = bot_api.create_app(
            make_config(),
            make_deps(write_settings=bot.write_dashboard_settings,
                      read_settings=bot.read_dashboard_settings),
        )

        async def runner():
            server = TestServer(app)
            await server.start_server()
            try:
                client = self.client_for(server)
                loop = asyncio.get_running_loop()
                return await loop.run_in_executor(None, lambda: call(client))
            finally:
                await server.close()

        return asyncio.run(runner())

    def test_a_save_travels_end_to_end(self, subscribed):
        make_server()
        result = self.run_against_bot(
            lambda client: client.update_settings(
                ADMIN_ID,
                GUILD_ID,
                {"instructions_locale": "ja", "panel_embed_color": 0x00FF00},
            )
        )
        assert result["fields"]["instructions_locale"]["value"] == "ja"
        assert bot.load_panel_branding(GUILD_ID) == (0x00FF00, False)
        assert {row[0] for row in audit_rows()} == {
            "instructions_locale",
            "panel_embed_color",
        }

    def test_the_read_the_dashboard_gets_carries_what_it_needs_to_render(self):
        """The two keys step 5 added, over the wire rather than in a fixture."""
        make_server()
        payload = self.run_against_bot(
            lambda client: client.settings(ADMIN_ID, GUILD_ID)
        )
        assert payload["choices"]["instructions_locale"]
        assert all(
            field["writable"] is True for field in payload["fields"].values()
        )

    def test_a_refusal_arrives_as_its_reason(self, free):
        """The dashboard maps these to copy, so the string has to survive."""
        botapi = pytest.importorskip("dashboard.botapi")
        make_server(row_id=500)
        with pytest.raises(botapi.BotAPIError) as caught:
            self.run_against_bot(
                lambda client: client.update_settings(
                    ADMIN_ID, GUILD_ID, {"panel_embed_color": 0xFF0000}
                )
            )
        assert str(caught.value) == "requires_premium"
        assert caught.value.status == 403

    def test_a_non_administrator_is_refused_end_to_end(self, subscribed):
        botapi = pytest.importorskip("dashboard.botapi")
        make_server()
        with pytest.raises(botapi.BotAPIError) as caught:
            self.run_against_bot(
                lambda client: client.update_settings(
                    MEMBER_ID, GUILD_ID, {"instructions_locale": "ja"}
                )
            )
        assert caught.value.status == 403
        assert audit_rows() == []


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

    def test_send_without_embed_links_is_its_own_answer(self, monkeypatch):
        """The permission pair that cost a long debugging session.

        The verification log is plain text and only needs Send Messages; the
        instructions panel is an embed and needs both. A channel granting one
        and not the other reads as perfectly writable and then refuses the
        panel, so the two questions get two flags.
        """
        guild = FakeGuild(
            channels=[FakeChannel(1, "no-embeds", sendable=True, embeddable=False)]
        )
        monkeypatch.setattr(bot.bot, "get_guild", lambda _id: guild)
        channel = run(bot.read_dashboard_channels(GUILD_ID))[0]
        assert channel["can_send"] is True
        assert channel["can_embed"] is False

    def test_a_channel_it_cannot_see_can_do_neither(self, monkeypatch):
        guild = FakeGuild(
            channels=[FakeChannel(1, "hidden", sendable=False, embeddable=True)]
        )
        monkeypatch.setattr(bot.bot, "get_guild", lambda _id: guild)
        channel = run(bot.read_dashboard_channels(GUILD_ID))[0]
        assert channel["can_send"] is False
        assert channel["can_embed"] is False


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
        assert panel["channel_postable"] is True

    def test_a_deleted_channel_is_reported(self, monkeypatch):
        make_server(instructions_channel_id="999", instructions_message_id="55")
        monkeypatch.setattr(bot.bot, "get_guild", lambda _id: FakeGuild())
        panel = run(bot.read_dashboard_panel(GUILD_ID))
        assert panel["channel_exists"] is False
        assert panel["channel_postable"] is None

    def test_a_channel_without_embed_links_is_not_postable(self, monkeypatch):
        """Send Messages alone is not enough for a panel, and this is the only
        place that says so before the post is attempted and refused."""
        make_server(instructions_channel_id="1", instructions_message_id="55")
        guild = FakeGuild(
            channels=[FakeChannel(1, "verify", sendable=True, embeddable=False)]
        )
        monkeypatch.setattr(bot.bot, "get_guild", lambda _id: guild)
        assert run(bot.read_dashboard_panel(GUILD_ID))["channel_postable"] is False

    def test_a_locked_channel_reports_unpostable_not_broken(self, monkeypatch):
        """A panel in a channel the bot cannot send to is still a live panel.

        Buttons are interactions and a refresh edits a message the bot owns, so
        neither needs Send Messages. This flag is only about posting a new one.
        """
        make_server(instructions_channel_id="1", instructions_message_id="55")
        guild = FakeGuild(channels=[FakeChannel(1, "verify", sendable=False)])
        monkeypatch.setattr(bot.bot, "get_guild", lambda _id: guild)
        panel = run(bot.read_dashboard_panel(GUILD_ID))
        assert panel["posted"] is True
        assert panel["channel_exists"] is True
        assert panel["channel_postable"] is False


def member(administrator=True):
    return SimpleNamespace(
        guild_permissions=SimpleNamespace(administrator=administrator)
    )


@pytest.fixture(autouse=True)
def clear_admin_cache():
    """Verdicts are cached, so they would otherwise leak between tests."""
    bot._admin_check_cache._store.clear()
    yield
    bot._admin_check_cache._store.clear()


class TestAdminCheck:
    def guild_with(self, monkeypatch, fetch_result=None, fetches=None, **kwargs):
        guild = FakeGuild(**kwargs)

        async def fetch_member(user_id):
            if fetches is not None:
                fetches.append(user_id)
            if isinstance(fetch_result, Exception):
                raise fetch_result
            if fetch_result is None:
                raise discord.NotFound(SimpleNamespace(status=404), "nope")
            return fetch_result

        guild.fetch_member = fetch_member
        monkeypatch.setattr(bot.bot, "get_guild", lambda _id: guild)
        return guild

    def test_the_owner_needs_no_lookup(self, monkeypatch):
        fetches = []
        self.guild_with(monkeypatch, fetch_result=member(), fetches=fetches)
        assert run(bot.dashboard_is_admin(GUILD_ID, OWNER_ID)) is True
        assert fetches == []

    def test_an_administrator_is_allowed(self, monkeypatch):
        self.guild_with(monkeypatch, fetch_result=member(administrator=True))
        assert run(bot.dashboard_is_admin(GUILD_ID, ADMIN_ID)) is True

    def test_manage_server_is_not_enough(self, monkeypatch):
        """Administrator only, matching every slash command's own check."""
        self.guild_with(monkeypatch, fetch_result=member(administrator=False))
        assert run(bot.dashboard_is_admin(GUILD_ID, ADMIN_ID)) is False

    def test_a_non_member_is_refused(self, monkeypatch):
        self.guild_with(monkeypatch, fetch_result=None)
        assert run(bot.dashboard_is_admin(GUILD_ID, ADMIN_ID)) is False

    def test_an_unanswerable_question_fails_closed(self, monkeypatch):
        self.guild_with(monkeypatch, fetch_result=RuntimeError("gateway is unhappy"))
        assert run(bot.dashboard_is_admin(GUILD_ID, ADMIN_ID)) is False

    def test_an_absent_guild_is_refused(self, monkeypatch):
        monkeypatch.setattr(bot.bot, "get_guild", lambda _id: None)
        assert run(bot.dashboard_is_admin(GUILD_ID, ADMIN_ID)) is False

    def test_the_verdict_is_cached_across_a_page_load(self, monkeypatch):
        """Four endpoint calls behind one page must not be four REST calls."""
        fetches = []
        self.guild_with(monkeypatch, fetch_result=member(), fetches=fetches)

        async def scenario():
            return [await bot.dashboard_is_admin(GUILD_ID, ADMIN_ID) for _ in range(4)]

        assert run(scenario()) == [True] * 4
        assert len(fetches) == 1

    def test_the_admin_cache_is_not_the_verification_cache(self):
        """The whole point of fix #2: a 180s member cache must not decide authority.

        Sharing REST_TTL_SECONDS would mean a demoted admin kept configuring
        the server for up to three minutes — precisely the window that matters
        when the role is being pulled because an account was compromised.
        """
        assert bot._admin_check_cache is not bot._member_fetch_cache
        assert bot._admin_check_cache.ttl < bot._member_fetch_cache.ttl
        assert bot.BOT_API_ADMIN_TTL <= 30

    def test_a_demotion_takes_effect_once_the_entry_expires(self, monkeypatch):
        current = member(administrator=True)
        fetches = []
        self.guild_with(monkeypatch, fetch_result=current, fetches=fetches)

        async def scenario():
            assert await bot.dashboard_is_admin(GUILD_ID, ADMIN_ID) is True

            current.guild_permissions.administrator = False

            # Age the cached verdict past its TTL rather than deleting it, so
            # this exercises _TTLCache's expiry branch — the thing that
            # actually bounds how long a demoted admin keeps access.
            store = bot._admin_check_cache._store
            stale = asyncio.get_event_loop().time() - 1
            for key, (_expires_at, value) in list(store.items()):
                store[key] = (stale, value)

            return await bot.dashboard_is_admin(GUILD_ID, ADMIN_ID)

        assert run(scenario()) is False
        assert len(fetches) == 2


class TestAdminGuildList:
    def setup_guilds(self, monkeypatch, present, admin_of):
        def get_guild(guild_id):
            if int(guild_id) not in present:
                return None
            guild = FakeGuild()
            guild.id = int(guild_id)
            return guild

        async def is_admin(guild_id, user_id):
            return int(guild_id) in admin_of

        monkeypatch.setattr(bot.bot, "get_guild", get_guild)
        monkeypatch.setattr(bot, "dashboard_is_admin", is_admin)

    def test_returns_the_intersection(self, monkeypatch):
        self.setup_guilds(monkeypatch, present={1, 2, 3}, admin_of={2, 3, 9})
        assert run(bot.dashboard_admin_guilds(ADMIN_ID, [1, 2, 3, 9])) == [2, 3]

    def test_a_guild_the_caller_does_not_administer_looks_absent(self, monkeypatch):
        """Indistinguishable from one the bot never joined — that's the fix."""
        self.setup_guilds(monkeypatch, present={1}, admin_of=set())
        assert run(bot.dashboard_admin_guilds(MEMBER_ID, [1, 2])) == []

    def test_absent_guilds_cost_no_authority_check(self, monkeypatch):
        checked = []

        async def is_admin(guild_id, user_id):
            checked.append(int(guild_id))
            return True

        monkeypatch.setattr(bot.bot, "get_guild", lambda gid: None)
        monkeypatch.setattr(bot, "dashboard_is_admin", is_admin)
        assert run(bot.dashboard_admin_guilds(ADMIN_ID, [1, 2, 3])) == []
        assert checked == []

    def test_unparseable_ids_are_skipped_not_fatal(self, monkeypatch):
        self.setup_guilds(monkeypatch, present={7}, admin_of={7})
        assert run(bot.dashboard_admin_guilds(ADMIN_ID, ["7", "nonsense", None])) == [7]

    def test_one_failed_check_does_not_grant_the_others(self, monkeypatch):
        async def is_admin(guild_id, user_id):
            if int(guild_id) == 2:
                raise RuntimeError("gateway is unhappy")
            return True

        monkeypatch.setattr(bot.bot, "get_guild", lambda gid: FakeGuild())
        monkeypatch.setattr(bot, "dashboard_is_admin", is_admin)
        # The raiser is dropped, never treated as an allow.
        assert run(bot.dashboard_admin_guilds(ADMIN_ID, [1, 2, 3])) == [1, 3]

    def test_an_unanswerable_list_is_none_not_empty(self, monkeypatch):
        def boom(_guild_id):
            raise RuntimeError("cache is gone")

        monkeypatch.setattr(bot.bot, "get_guild", boom)
        assert run(bot.dashboard_admin_guilds(ADMIN_ID, [1])) is None


# ---------------------------------------------------------------
# Configuring moved to the dashboard
# ---------------------------------------------------------------
class FakeInteractionResponse:
    def __init__(self):
        self.deferred = None

    async def defer(self, **kwargs):
        self.deferred = kwargs


class FakeFollowup:
    def __init__(self):
        self.sent = []

    async def send(self, content=None, **kwargs):
        self.sent.append({"content": content, **kwargs})


def fake_interaction(guild):
    return SimpleNamespace(
        guild=guild,
        guild_id=guild.id,
        user=SimpleNamespace(id=OWNER_ID),
        locale="en-US",
        response=FakeInteractionResponse(),
        followup=FakeFollowup(),
    )


class SummaryGuild(FakeGuild):
    """A guild whose roles and channels resolve, so ids render as names."""

    def get_role(self, role_id):
        return next((r for r in self.roles if r.id == role_id), None)

    def get_channel(self, channel_id):
        return next((c for c in self.text_channels if c.id == channel_id), None)


class TestTheRetiredCommandsStillAnswer:
    """/vrcverify_settings, _logchannel and _setrequestmessage no longer edit.

    They were kept rather than deleted because a slash command that vanishes
    leaves an admin typing a name Discord no longer offers and getting nothing
    back, with no clue where it went. So each shows what is stored and links
    to the dashboard.
    """

    def test_the_summary_reads_the_same_payload_the_website_does(self, free):
        make_server(row_id=9000, role_id="900000000001")
        guild = SummaryGuild()
        embed = run(bot.build_settings_summary(guild))

        names = [f.name for f in embed.fields]
        # Every field the API reports, in the summary. A setting that exists
        # but is not shown is one an admin cannot discover from Discord.
        assert len(names) == len(bot.SETTINGS_FIELDS)
        assert any(n.startswith("Verified role") for n in names)

    def test_a_locked_field_and_a_badge_only_field_read_differently(self, free):
        """The distinction the dashboard draws, drawn the same way here.

        Collapsing them would tell an admin they cannot set something they can
        plainly set -- the exact failure SettingsField exists to prevent.
        """
        make_server(row_id=9000)
        embed = run(bot.build_settings_summary(SummaryGuild()))
        labels = {f.name for f in embed.fields}

        assert any("Nickname sync" in n and "\N{LOCK}" in n for n in labels)
        assert any("Unverified role" in n and "not applied" in n for n in labels)
        # ...and never both markers on one field.
        assert not any("\N{LOCK}" in n and "not applied" in n for n in labels)

    def test_an_unreadable_settings_read_says_so_rather_than_showing_defaults(
        self, monkeypatch
    ):
        """A page of "Not set" for a database blip would invite an admin to
        reconfigure a server that was fine."""

        async def unreadable(_guild_id):
            return None

        monkeypatch.setattr(bot, "read_dashboard_settings", unreadable)
        interaction = fake_interaction(SummaryGuild())
        run(bot.send_settings_summary(interaction))

        (reply,) = interaction.followup.sent
        assert reply["content"] == bot.localizations["en-US"]["settings_unreadable"]
        assert "embed" not in reply

    def test_the_reply_carries_a_deep_link_to_this_guild(self, monkeypatch, free):
        make_server(row_id=9000)
        monkeypatch.setattr(bot, "DASHBOARD_URL", "https://dashboard.vrcverify.com")
        interaction = fake_interaction(SummaryGuild())
        run(bot.send_settings_summary(interaction))

        (reply,) = interaction.followup.sent
        (button,) = reply["view"].children
        # The guild, not the picker. Making an admin find the server they were
        # already looking at is how "use the website" becomes "the website is
        # annoying".
        #
        # And the settings section specifically, not the guild root -- the root
        # is the Overview, and this button sits under "change them on the
        # dashboard".
        assert (
            button.url
            == f"https://dashboard.vrcverify.com/guild/{GUILD_ID}/settings"
        )

    def test_no_dashboard_configured_still_gives_a_usable_answer(
        self, monkeypatch, free
    ):
        """A self-hoster with no dashboard gets the summary and no dead button."""
        make_server(row_id=9000)
        monkeypatch.setattr(bot, "DASHBOARD_URL", None)
        interaction = fake_interaction(SummaryGuild())
        run(bot.send_settings_summary(interaction))

        (reply,) = interaction.followup.sent
        assert reply["embed"] is not None
        assert "view" not in reply

    def test_the_summary_is_always_ephemeral(self, monkeypatch, free):
        """It names roles and channels and the server's plan. Not for the channel."""
        make_server(row_id=9000)
        monkeypatch.setattr(bot, "DASHBOARD_URL", "https://dashboard.vrcverify.com")
        interaction = fake_interaction(SummaryGuild())
        run(bot.send_settings_summary(interaction))

        assert interaction.response.deferred["ephemeral"] is True
        assert interaction.followup.sent[0]["ephemeral"] is True


class TestNothingEditsSettingsFromDiscordAnyMore:
    def test_the_retired_commands_take_no_arguments(self):
        """/vrcverify_logchannel used to take a channel.

        Keeping the parameter and refusing it would be worse than removing it:
        Discord would still offer the picker, an admin would choose a channel,
        and only the reply would reveal that nothing happened.
        """
        for name in (
            "vrcverify_settings",
            "vrcverify_logchannel",
            "vrcverify_setrequestmessage",
        ):
            command = bot.bot.tree.get_command(name)
            assert command is not None, f"{name} disappeared entirely"
            assert command.parameters == [], f"{name} still accepts input"

    def test_the_paged_editor_is_gone(self):
        """Its rules lived in a second place; TestEveryFieldDeclaresItsGate in
        test_premium.py now pins the one that is left."""
        for gone in (
            "PagedSettingsView",
            "PanelColorModal",
            "SetRequestMessageModal",
            "SETTINGS_PAGE_FEATURE",
        ):
            assert not hasattr(bot, gone), f"{gone} came back"

    def test_the_sanitiser_did_not_leave_with_the_modal(self):
        """The modal called it; the dashboard write path still does. Losing it
        would drop the @everyone defusing and the link allowlist."""
        assert callable(bot.sanitize_custom_message)
        cleaned, invalid = bot.sanitize_custom_message("hi @everyone")
        assert "@everyone" not in cleaned
