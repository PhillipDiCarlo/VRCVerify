"""The bot's internal API — the only door the web dashboard may knock on.

This module is deliberately *pure*: it imports neither `bot` nor `discord` nor
SQLAlchemy. Everything it is allowed to touch arrives as a callable on
`BotAPIDeps`, which the bot builds and hands over at startup.

That is not tidiness, it is the "least privilege on the API surface" control
issue #65 asks for. There is no generic "write these columns" entry point here
because there is no writer in `BotAPIDeps` at all — in this phase the API is
structurally incapable of changing a row. Adding a write path later means
adding a named writer to that dataclass, which is a visible, reviewable diff
rather than a new query string someone slipped past.

Three things guard every request, in this order:

1. **mTLS.** The client must present a certificate signed by our own CA. The
   Tailscale tunnel this runs inside is a segmentation control, not an
   authentication one, so the transport is authenticated in its own right.
2. **A scoped, short-lived token.** It names the acting Discord user, the target
   guild and the exact operation, and it is checked against the real method and
   path. A captured request cannot be replayed against another guild, against
   another endpoint, or after its window.
3. **The bot's own answer.** Authority never comes from the dashboard, from the
   token, or from the `permissions` field Discord handed the dashboard at login.
   It comes from asking the bot whether this user is an Administrator of this
   guild, re-checked per request against a cache with a deliberately short life
   (`BOT_API_ADMIN_TTL`, 15s) — so revoking someone's admin role revokes their
   dashboard access within seconds rather than whenever a session expires.

The whole thing is inert unless `BOT_API_ENABLED` is set. With it unset nothing
binds and nothing listens.
"""

from __future__ import annotations

import base64
import binascii
import hmac
import ipaddress
import json
import logging
import os
import secrets
import ssl
import time
from dataclasses import dataclass, fields
from hashlib import sha256
from typing import Any, Awaitable, Callable, Optional

from aiohttp import web

logger = logging.getLogger(__name__)

# Reusing the port the Dockerfile has always (uselessly) EXPOSEd.
DEFAULT_PORT = 5002

# Tokens are minted per request and used immediately. Thirty seconds is
# generous for a call across a tunnel and short enough that a captured token is
# worthless by the time anyone notices they have it.
DEFAULT_TOKEN_TTL = 30
# Allowance for clock drift between the VPS and the homelab. Small on purpose:
# every second here widens the replay window at both ends.
DEFAULT_CLOCK_SKEW = 5

# An HMAC key shorter than its own digest is weaker than the primitive it feeds.
MIN_SIGNING_KEY_BYTES = 32

# Per-actor request budget. The dashboard renders a handful of calls per page,
# so this is roughly two orders of magnitude above honest use.
DEFAULT_RATE_LIMIT = 60
DEFAULT_RATE_WINDOW = 60
# A second, global bucket so one compromised session cannot saturate the bot's
# event loop even while staying inside its own per-actor budget.
DEFAULT_GLOBAL_RATE_LIMIT = 600

# Nothing here accepts a body yet; this only has to be big enough for headers.
MAX_REQUEST_BYTES = 16 * 1024

# The picker asks about the guilds the signed-in user is already a member of.
# Capped so a malformed or hostile query cannot turn one request into an
# unbounded loop.
MAX_GUILD_IDS = 200

TOKEN_VERSION = "v1"
# Scope used by the one endpoint that is about the actor rather than a guild.
OP_LIST_GUILDS = "GET /api/v1/guilds"


class BotAPIConfigError(RuntimeError):
    """The API was switched on but cannot be started safely."""


# Typed keys rather than bare strings, so a handler reaching for something the
# app was never given fails at the lookup instead of at whatever it does with
# the None it got back.
CONFIG_KEY: "web.AppKey" = web.AppKey("config")
DEPS_KEY: "web.AppKey" = web.AppKey("deps")
REPLAY_KEY: "web.AppKey" = web.AppKey("replay")
RATE_KEY: "web.AppKey" = web.AppKey("rate_limiter")
GLOBAL_RATE_KEY: "web.AppKey" = web.AppKey("global_rate_limiter")


class TokenError(Exception):
    """A token was absent, malformed, expired, or not for this request.

    `actor` is set for every failure raised *after* the signature verified, so
    the denial log can name who minted the token. That is the difference
    between "someone sent us garbage" and "this specific signed-in user tried
    to reach a guild they were not scoped for" — and the second is the one
    worth alerting on.
    """

    def __init__(self, reason: str, actor: Optional[int] = None):
        super().__init__(reason)
        self.reason = reason
        self.actor = actor


# -------------------------------------------------------------------
# What the bot lets this module reach
# -------------------------------------------------------------------
@dataclass(frozen=True)
class BotAPIDeps:
    """The complete list of things the API can do to the bot.

    Every entry is a reader. Keep it that way: `tests/test_bot_api.py` pins the
    exact field set, so a writer cannot be added by accident.

    Readers take and return plain data — ids, dicts, lists — never discord.py
    objects. Shaping a Guild into JSON is the bot's job, not this module's,
    which is what keeps `discord` out of the import list above and makes every
    handler testable against a dict.
    """

    # Has the gateway connected? Answers are meaningless before it has.
    is_ready: Callable[[], bool]
    # Is the bot in this guild at all? A pure cache lookup, no REST.
    # Used only to tell "no such guild" (404) from "not yours" (403) on the
    # guild-scoped routes; it is never an answer on its own.
    guild_present: Callable[[int], bool]
    # The authority check. Async because an uncached member costs a fetch.
    is_admin: Callable[[int, int], Awaitable[bool]]
    # The same check, for the picker: narrows a caller's own guild list to the
    # ones they administer. Kept as its own reader rather than a loop over
    # is_admin so the bot can bound the work it does in one pass.
    read_admin_guilds: Callable[[int, list], Awaitable[Optional[list]]]
    read_settings: Callable[[int], Awaitable[Optional[dict]]]
    read_roles: Callable[[int], Awaitable[Optional[list]]]
    read_channels: Callable[[int], Awaitable[Optional[list]]]
    read_panel: Callable[[int], Awaitable[Optional[dict]]]


# -------------------------------------------------------------------
# Configuration
# -------------------------------------------------------------------
def _truthy(raw: Optional[str]) -> bool:
    return (raw or "").strip().lower() in {"1", "true", "yes", "on"}


def _int_env(name: str, default: int, minimum: int = 1) -> int:
    try:
        return max(minimum, int(os.getenv(name, str(default))))
    except ValueError:
        return default


@dataclass(frozen=True)
class BotAPIConfig:
    bind: str
    port: int
    cert_path: str
    key_path: str
    ca_path: str
    signing_key: bytes
    # Optional pin on *which* client certificate is acceptable, so a second
    # cert issued by the same CA still cannot call the API.
    client_cn: Optional[str] = None
    token_ttl: int = DEFAULT_TOKEN_TTL
    clock_skew: int = DEFAULT_CLOCK_SKEW
    rate_limit: int = DEFAULT_RATE_LIMIT
    rate_window: int = DEFAULT_RATE_WINDOW
    global_rate_limit: int = DEFAULT_GLOBAL_RATE_LIMIT

    @staticmethod
    def enabled() -> bool:
        """The kill switch, read fresh so tests can flip it."""
        return _truthy(os.getenv("BOT_API_ENABLED"))

    @classmethod
    def from_env(cls) -> Optional["BotAPIConfig"]:
        """Build the config, or None when the API is switched off.

        Raises BotAPIConfigError when it is switched *on* but misconfigured.
        Failing loudly is the point: the alternative to "refuse to start" is a
        listener that came up in a weaker shape than the operator intended, and
        the whole design here assumes the tunnel is not load-bearing.
        """
        if not cls.enabled():
            return None

        bind = (os.getenv("BOT_API_BIND") or "").strip()
        _validate_bind(bind)

        cert_path = _require_file("BOT_API_CERT")
        key_path = _require_file("BOT_API_KEY")
        ca_path = _require_file("BOT_API_CA")

        raw_key = (os.getenv("BOT_API_TOKEN_SIGNING_KEY") or "").strip()
        signing_key = raw_key.encode("utf-8")
        if len(signing_key) < MIN_SIGNING_KEY_BYTES:
            raise BotAPIConfigError(
                "BOT_API_TOKEN_SIGNING_KEY must be at least "
                f"{MIN_SIGNING_KEY_BYTES} characters; refusing to start."
            )

        client_cn = (os.getenv("BOT_API_CLIENT_CN") or "").strip() or None

        return cls(
            bind=bind,
            port=_int_env("BOT_API_PORT", DEFAULT_PORT),
            cert_path=cert_path,
            key_path=key_path,
            ca_path=ca_path,
            signing_key=signing_key,
            client_cn=client_cn,
            token_ttl=_int_env("BOT_API_TOKEN_TTL", DEFAULT_TOKEN_TTL),
            clock_skew=_int_env("BOT_API_CLOCK_SKEW", DEFAULT_CLOCK_SKEW, minimum=0),
            rate_limit=_int_env("BOT_API_RATE_LIMIT", DEFAULT_RATE_LIMIT),
            rate_window=_int_env("BOT_API_RATE_WINDOW", DEFAULT_RATE_WINDOW),
            global_rate_limit=_int_env(
                "BOT_API_GLOBAL_RATE_LIMIT", DEFAULT_GLOBAL_RATE_LIMIT
            ),
        )


def _validate_bind(bind: str) -> None:
    """Refuse anything that would listen on more than one interface.

    A blank or wildcard bind is the single misconfiguration that would undo the
    entire network design — the API would answer on the VPS-facing side of
    whatever host it lands on, and only the firewall would be left standing
    between it and the internet. It is cheaper to not start.
    """
    if not bind:
        raise BotAPIConfigError(
            "BOT_API_BIND is required when BOT_API_ENABLED is set; it must name "
            "the tailnet interface, never a wildcard."
        )
    if bind in {"*", "[::]"}:
        raise BotAPIConfigError(f"BOT_API_BIND={bind!r} is a wildcard; refusing to start.")
    try:
        address = ipaddress.ip_address(bind)
    except ValueError:
        # A hostname (a tailnet DNS name, say). Resolution happens at bind time.
        return
    if address.is_unspecified:
        raise BotAPIConfigError(
            f"BOT_API_BIND={bind!r} listens on every interface; refusing to start. "
            "Bind the tailnet address explicitly."
        )


def _require_file(name: str) -> str:
    path = (os.getenv(name) or "").strip()
    if not path:
        raise BotAPIConfigError(f"{name} is required when BOT_API_ENABLED is set.")
    if not os.path.isfile(path):
        raise BotAPIConfigError(f"{name} points at {path!r}, which is not a file.")
    return path


def build_ssl_context(config: BotAPIConfig) -> ssl.SSLContext:
    """A TLS 1.3 server context that *demands* a client certificate."""
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_3
    context.verify_mode = ssl.CERT_REQUIRED
    context.load_verify_locations(cafile=config.ca_path)
    context.load_cert_chain(certfile=config.cert_path, keyfile=config.key_path)
    return context


# -------------------------------------------------------------------
# Scoped request tokens
# -------------------------------------------------------------------
def _b64encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64decode(raw: str) -> bytes:
    padding = "=" * (-len(raw) % 4)
    return base64.urlsafe_b64decode(raw + padding)


def _canonical(payload: dict) -> bytes:
    return json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")


def mint_token(
    signing_key: bytes,
    *,
    actor_id: int,
    operation: str,
    guild_id: Optional[int] = None,
    ttl: int = DEFAULT_TOKEN_TTL,
    now: Optional[float] = None,
) -> str:
    """Sign one token for one call.

    Lives here rather than in the dashboard so both ends share exactly one
    implementation of the format — a signer and a verifier that drift apart is
    the classic way an auth scheme quietly stops meaning anything.
    """
    issued = int(now if now is not None else time.time())
    payload = {
        "act": int(actor_id),
        "gid": None if guild_id is None else int(guild_id),
        "op": operation,
        "iat": issued,
        "exp": issued + int(ttl),
        "jti": _b64encode(secrets.token_bytes(12)),
    }
    encoded = _b64encode(_canonical(payload))
    signature = hmac.new(signing_key, encoded.encode("ascii"), sha256).digest()
    return f"{TOKEN_VERSION}.{encoded}.{_b64encode(signature)}"


@dataclass(frozen=True)
class TokenClaims:
    actor_id: int
    guild_id: Optional[int]
    operation: str
    expires_at: int
    jti: str


def verify_token(
    token: str,
    signing_key: bytes,
    *,
    expected_operation: str,
    expected_guild_id: Optional[int],
    max_ttl: int = DEFAULT_TOKEN_TTL,
    clock_skew: int = DEFAULT_CLOCK_SKEW,
    now: Optional[float] = None,
) -> TokenClaims:
    """Authenticate a token and confirm it was minted for *this* request.

    The signature is checked before the payload is parsed, so no attacker-
    supplied JSON is ever deserialised on an unauthenticated path.
    """
    current = now if now is not None else time.time()

    parts = token.split(".")
    if len(parts) != 3 or parts[0] != TOKEN_VERSION:
        raise TokenError("malformed")
    _, encoded, signature = parts

    expected_sig = hmac.new(signing_key, encoded.encode("ascii"), sha256).digest()
    try:
        provided_sig = _b64decode(signature)
    except (binascii.Error, ValueError):
        raise TokenError("malformed")
    if not hmac.compare_digest(expected_sig, provided_sig):
        raise TokenError("bad_signature")

    try:
        payload = json.loads(_b64decode(encoded))
    except (binascii.Error, ValueError, UnicodeDecodeError):
        raise TokenError("malformed")
    if not isinstance(payload, dict):
        raise TokenError("malformed")

    try:
        actor_id = int(payload["act"])
        operation = payload["op"]
        issued_at = int(payload["iat"])
        expires_at = int(payload["exp"])
        jti = payload["jti"]
        raw_guild = payload["gid"]
        guild_id = None if raw_guild is None else int(raw_guild)
    except (KeyError, TypeError, ValueError):
        raise TokenError("malformed")
    if not isinstance(operation, str) or not isinstance(jti, str) or not jti:
        raise TokenError("malformed")

    # Past this point the signature has verified, so every refusal below can
    # name the actor it was minted for.
    if expires_at <= current - clock_skew:
        raise TokenError("expired", actor=actor_id)
    if issued_at > current + clock_skew:
        raise TokenError("not_yet_valid", actor=actor_id)
    # A token whose own window is longer than we ever issue is either a bug in
    # the dashboard or someone minting their own with a leaked key. Neither is
    # a request worth serving.
    if expires_at - issued_at > max_ttl:
        raise TokenError("ttl_too_long", actor=actor_id)

    # The two checks that make interception useless: a token is good for one
    # operation against one guild, and nothing else.
    if operation != expected_operation:
        raise TokenError("wrong_operation", actor=actor_id)
    if guild_id != expected_guild_id:
        raise TokenError("wrong_guild", actor=actor_id)

    return TokenClaims(
        actor_id=actor_id,
        guild_id=guild_id,
        operation=operation,
        expires_at=expires_at,
        jti=jti,
    )


class ReplayGuard:
    """Remembers spent token ids for as long as they could still be replayed.

    Expiry checking alone leaves a window the length of the token's TTL in which
    a captured request can be sent again. This closes it.
    """

    def __init__(self, maxsize: int = 20000):
        self.maxsize = maxsize
        self._seen: dict[str, int] = {}

    def spend(
        self,
        jti: str,
        expires_at: int,
        now: Optional[float] = None,
        actor: Optional[int] = None,
    ) -> None:
        """Record a token id, or raise if it has already been used."""
        current = now if now is not None else time.time()
        if self._seen.get(jti, 0) > current:
            raise TokenError("replayed", actor=actor)

        # Anything already expired can never be replayed, so it is free to drop.
        if len(self._seen) >= self.maxsize:
            self._seen = {
                key: exp for key, exp in self._seen.items() if exp > current
            }
        if len(self._seen) >= self.maxsize:
            # Only reachable if the rate limiters have already failed. Refusing
            # is the safe direction — evicting live entries would hand back the
            # replay window this class exists to close.
            raise TokenError("replay_guard_full", actor=actor)

        self._seen[jti] = int(expires_at)


class RateLimiter:
    """A fixed-window request budget, keyed by actor and by 'everyone'."""

    def __init__(self, limit: int, window: int, maxsize: int = 10000):
        self.limit = limit
        self.window = window
        self.maxsize = maxsize
        self._hits: dict[str, list[float]] = {}

    def allow(self, key: str, now: Optional[float] = None) -> bool:
        current = now if now is not None else time.time()
        cutoff = current - self.window

        hits = [stamp for stamp in self._hits.get(key, ()) if stamp > cutoff]
        if len(hits) >= self.limit:
            self._hits[key] = hits
            return False

        hits.append(current)
        self._hits[key] = hits

        if len(self._hits) > self.maxsize:
            self._hits = {
                other: stamps
                for other, stamps in self._hits.items()
                if any(stamp > cutoff for stamp in stamps)
            }
        return True


# -------------------------------------------------------------------
# Request authorisation
# -------------------------------------------------------------------
def _peer_identities(request: web.Request) -> set[str]:
    """Every name the presented client certificate claims.

    TLS has already proven the certificate is signed by our CA by the time this
    runs; this only answers *which* of our certificates it is.
    """
    transport = request.transport
    peercert = transport.get_extra_info("peercert") if transport is not None else None
    if not peercert:
        return set()

    names = set()
    for rdn in peercert.get("subject", ()):  # ((('commonName', 'x'),), ...)
        for attribute, value in rdn:
            if attribute == "commonName":
                names.add(value)
    for kind, value in peercert.get("subjectAltName", ()):
        if kind in {"DNS", "IP Address"}:
            names.add(value)
    return names


def _deny(request: web.Request, status: int, reason: str, actor: Any = "unknown"):
    """Refuse a request and leave the reason in the log.

    Every denial is logged. Under an assume-breach model these lines are the
    forensic record, and a run of them is the thing worth alerting on.
    """
    logger.warning(
        "bot-api DENY actor=%s guild=%s op=%s reason=%s",
        actor,
        request.match_info.get("guild_id", "-"),
        _operation_for(request),
        reason,
    )
    return web.json_response(
        {"error": reason}, status=status, headers={"Cache-Control": "no-store"}
    )


def _operation_for(request: web.Request) -> str:
    """The canonical `METHOD /template/path` this request resolved to.

    Taken from the matched route rather than the raw URL, so the token is bound
    to the endpoint the router actually chose — not to a string the caller
    could dress up to look like a different one.
    """
    route = request.match_info.route
    resource = route.resource
    canonical = resource.canonical if resource is not None else request.path
    return f"{request.method} {canonical}"


class _Denied(Exception):
    """Carries a ready-made refusal back out of the authorisation helper."""

    def __init__(self, response: web.Response):
        self.response = response


async def _authorize(request: web.Request, *, guild_scoped: bool) -> TokenClaims:
    """Run all three gates. Returns the claims, or raises _Denied."""
    config: BotAPIConfig = request.app[CONFIG_KEY]
    deps: BotAPIDeps = request.app[DEPS_KEY]
    operation = _operation_for(request)

    # --- The client certificate, if we are pinning one ---
    if config.client_cn is not None:
        if config.client_cn not in _peer_identities(request):
            raise _Denied(_deny(request, 403, "client_certificate_not_permitted"))

    # --- The token ---
    header = request.headers.get("Authorization", "")
    scheme, _, raw_token = header.partition(" ")
    if scheme.lower() != "bearer" or not raw_token.strip():
        raise _Denied(_deny(request, 401, "missing_token"))

    guild_id: Optional[int] = None
    if guild_scoped:
        try:
            guild_id = int(request.match_info["guild_id"])
        except (KeyError, ValueError):
            raise _Denied(_deny(request, 400, "bad_guild_id"))

    try:
        claims = verify_token(
            raw_token.strip(),
            config.signing_key,
            expected_operation=operation,
            expected_guild_id=guild_id,
            max_ttl=config.token_ttl,
            clock_skew=config.clock_skew,
        )
        request.app[REPLAY_KEY].spend(claims.jti, claims.expires_at, actor=claims.actor_id)
    except TokenError as error:
        # 401 for "this token is not valid", 403 for "this token is valid but
        # not for this" — the second is the interesting one in the log, and it
        # is the one that can always name an actor.
        status = 403 if error.reason in {"wrong_operation", "wrong_guild"} else 401
        raise _Denied(
            _deny(
                request,
                status,
                error.reason,
                actor="unknown" if error.actor is None else error.actor,
            )
        )

    # --- Budgets, charged to the authenticated actor ---
    limiter: RateLimiter = request.app[RATE_KEY]
    global_limiter: RateLimiter = request.app[GLOBAL_RATE_KEY]
    if not limiter.allow(str(claims.actor_id)) or not global_limiter.allow("*"):
        raise _Denied(_deny(request, 429, "rate_limited", actor=claims.actor_id))

    # --- The bot's own answer ---
    if not deps.is_ready():
        raise _Denied(_deny(request, 503, "not_ready", actor=claims.actor_id))

    if guild_scoped:
        if not deps.guild_present(guild_id):
            raise _Denied(_deny(request, 404, "guild_not_found", actor=claims.actor_id))
        # Re-checked here on every request, never cached against the session:
        # losing Administrator revokes access within one request rather than
        # whenever the session happens to expire.
        if not await deps.is_admin(guild_id, claims.actor_id):
            raise _Denied(_deny(request, 403, "not_administrator", actor=claims.actor_id))

    logger.info(
        "bot-api ALLOW actor=%s guild=%s op=%s",
        claims.actor_id,
        guild_id if guild_id is not None else "-",
        operation,
    )
    return claims


def _json(payload: Any) -> web.Response:
    return web.json_response(payload, headers={"Cache-Control": "no-store"})


# -------------------------------------------------------------------
# Handlers — every one of them a read
# -------------------------------------------------------------------
async def handle_health(request: web.Request) -> web.Response:
    """Liveness, behind mTLS but not behind a token.

    Deliberately says nothing about guilds, servers or members: it exists so the
    dashboard can tell "the tunnel and the certificates are working" apart from
    "my token is wrong", which is otherwise a miserable thing to debug.
    """
    deps: BotAPIDeps = request.app[DEPS_KEY]
    config: BotAPIConfig = request.app[CONFIG_KEY]
    if config.client_cn is not None and config.client_cn not in _peer_identities(request):
        return _deny(request, 403, "client_certificate_not_permitted")
    # The one endpoint with no token, so the per-actor budget cannot apply. It
    # still gets the global one rather than being a free unmetered handler.
    if not request.app[GLOBAL_RATE_KEY].allow("*"):
        return _deny(request, 429, "rate_limited")
    return _json({"ok": True, "ready": bool(deps.is_ready())})


async def handle_list_guilds(request: web.Request) -> web.Response:
    """Which of the caller's guilds this bot is in AND the caller administers.

    Names and icons are NOT returned, because the dashboard already has them:
    they came from the user's OAuth guild list, which is where the picker gets
    its tiles. All this adds is the bit the dashboard cannot know — whether the
    bot is present — which decides greyscale-with-an-invite versus a link into
    the settings pages.

    The Administrator filter is not decoration. An earlier version answered
    "is the bot in this guild?" for any id the caller sent, which made this the
    one endpoint not bounded by the bot's own authority check. Under the design
    assumption that the public host is eventually compromised — and a
    compromised host holds the signing key, so it can mint tokens for any
    actor — that turned into an unbounded oracle: walk arbitrary ids and
    enumerate every server running this bot, i.e. a census of communities
    operating 18+ gating. Every other endpoint yields only what a real
    administrator could already see, and this one now matches.

    So the answer is never about a guild the caller has no standing in, and a
    guild the caller does not administer is indistinguishable from one the bot
    has never joined.
    """
    try:
        claims = await _authorize(request, guild_scoped=False)
    except _Denied as denied:
        return denied.response

    deps: BotAPIDeps = request.app[DEPS_KEY]
    raw_ids = (request.query.get("ids") or "").strip()
    if not raw_ids:
        return _json({"present": []})

    candidates = [chunk for chunk in raw_ids.split(",") if chunk]
    if len(candidates) > MAX_GUILD_IDS:
        return _deny(request, 400, "too_many_ids", actor=claims.actor_id)

    guild_ids = []
    for candidate in candidates:
        try:
            guild_ids.append(int(candidate))
        except ValueError:
            return _deny(request, 400, "bad_guild_id", actor=claims.actor_id)

    present = await deps.read_admin_guilds(claims.actor_id, guild_ids)
    if present is None:
        return _deny(request, 503, "unavailable", actor=claims.actor_id)
    return _json({"present": [str(guild_id) for guild_id in present]})


def _guild_reader(read: str):
    """Build a handler that authorises, then returns one guild-scoped read."""

    async def handler(request: web.Request) -> web.Response:
        try:
            claims = await _authorize(request, guild_scoped=True)
        except _Denied as denied:
            return denied.response

        deps: BotAPIDeps = request.app[DEPS_KEY]
        guild_id = int(request.match_info["guild_id"])
        payload = await getattr(deps, read)(guild_id)
        if payload is None:
            # The guild is present (checked above), so this is the bot failing
            # to answer rather than the caller asking about something they
            # should not see.
            return _deny(request, 503, "unavailable", actor=claims.actor_id)
        return _json(payload)

    handler.__name__ = f"handle_{read}"
    return handler


def create_app(config: BotAPIConfig, deps: BotAPIDeps) -> web.Application:
    """Wire the routes. Every route here is a GET, and that is load-bearing."""
    app = web.Application(client_max_size=MAX_REQUEST_BYTES)
    app[CONFIG_KEY] = config
    app[DEPS_KEY] = deps
    app[REPLAY_KEY] = ReplayGuard()
    app[RATE_KEY] = RateLimiter(config.rate_limit, config.rate_window)
    app[GLOBAL_RATE_KEY] = RateLimiter(config.global_rate_limit, config.rate_window)

    app.router.add_get("/healthz", handle_health)
    app.router.add_get("/api/v1/guilds", handle_list_guilds)
    app.router.add_get(
        "/api/v1/guilds/{guild_id}/settings", _guild_reader("read_settings")
    )
    app.router.add_get("/api/v1/guilds/{guild_id}/roles", _guild_reader("read_roles"))
    app.router.add_get(
        "/api/v1/guilds/{guild_id}/channels", _guild_reader("read_channels")
    )
    app.router.add_get("/api/v1/guilds/{guild_id}/panel", _guild_reader("read_panel"))

    app.on_response_prepare.append(_harden_response)
    return app


async def _harden_response(request: web.Request, response: web.StreamResponse) -> None:
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    # aiohttp advertises its version by default; there is no reason to help.
    response.headers["Server"] = "vrcverify"


# -------------------------------------------------------------------
# Lifecycle
# -------------------------------------------------------------------
class BotAPIServer:
    """Owns the running listener so the bot can shut it down cleanly."""

    def __init__(self, config: BotAPIConfig, deps: BotAPIDeps):
        self.config = config
        self.deps = deps
        self._runner: Optional[web.AppRunner] = None

    async def start(self) -> None:
        app = create_app(self.config, self.deps)
        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()
        site = web.TCPSite(
            self._runner,
            host=self.config.bind,
            port=self.config.port,
            ssl_context=build_ssl_context(self.config),
        )
        await site.start()
        logger.info(
            "Bot API listening on %s:%s (mTLS required%s).",
            self.config.bind,
            self.config.port,
            f", client CN pinned to {self.config.client_cn}"
            if self.config.client_cn
            else "",
        )

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None


def deps_field_names() -> frozenset[str]:
    """Every capability the API has been granted. Pinned by the tests."""
    return frozenset(field.name for field in fields(BotAPIDeps))
