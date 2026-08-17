"""The bot's internal API — the only door the web dashboard may knock on.

This module is deliberately *pure*: it imports neither `bot` nor `discord` nor
SQLAlchemy. Everything it is allowed to touch arrives as a callable on
`BotAPIDeps`, which the bot builds and hands over at startup.

That is not tidiness, it is the "least privilege on the API surface" control
issue #65 asks for. There is no generic "write these columns" entry point here:
`BotAPIDeps` carries exactly three mutating capabilities — `write_settings`,
`write_stripe_subscription` and `post_panel` — each named for the one thing it
changes, and this module does not know which fields exist, which are premium,
or what values are legal. It validates the envelope and hands the body to the
bot, which decides. Adding a fourth means editing that dataclass and the test
that pins it — a visible, reviewable diff rather than a new query string someone
slipped past.

One of the three is unlike the others: `write_stripe_subscription` is reached by
a route with **no human actor**, because a subscription renewal is not something
a person asks for. It is authenticated identically in every other respect — mTLS,
a signed token bound to this method, this path and this guild, the replay guard,
the write budget — and it is the *only* operation for which the Administrator
check is skipped, from an allowlist in `api_tokens.SYSTEM_OPERATIONS` that a
token cannot talk its way into.

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

import ipaddress
import logging
import os
import ssl
import time
from dataclasses import dataclass, fields
from typing import Any, Awaitable, Callable, Optional

from aiohttp import web

# The token format lives in its own stdlib-only module so the dashboard can
# sign with the identical implementation this verifies with, without also
# taking on aiohttp. Re-exported below, because this module is where the
# enforcement lives and callers should not need to know the split exists.
from api_tokens import (  # noqa: F401  (re-exported)
    DEFAULT_CLOCK_SKEW,
    DEFAULT_TOKEN_TTL,
    MIN_SIGNING_KEY_BYTES,
    OP_LIST_GUILDS,
    OP_PUT_STRIPE_SUBSCRIPTION,
    SYSTEM_ACTOR_ID,
    SYSTEM_OPERATIONS,
    TOKEN_VERSION,
    TokenClaims,
    TokenError,
    _b64decode,
    _b64encode,
    _canonical,
    mint_token,
    verify_token,
)

logger = logging.getLogger(__name__)

# Reusing the port the Dockerfile has always (uselessly) EXPOSEd.
DEFAULT_PORT = 5002

# Per-actor request budget. The dashboard renders a handful of calls per page,
# so this is roughly two orders of magnitude above honest use.
DEFAULT_RATE_LIMIT = 60
DEFAULT_RATE_WINDOW = 60
# A second, global bucket so one compromised session cannot saturate the bot's
# event loop even while staying inside its own per-actor budget.
DEFAULT_GLOBAL_RATE_LIMIT = 600

# Mutating requests get their own, much smaller budget on top of the two above.
# The budgets before this were sized when every route was a GET answered from
# the gateway cache, costing Discord nothing. That is no longer what a request
# costs: posting a panel is up to three Discord REST calls (fetch, send, delete)
# and saving a language or colour is up to two (fetch, edit). At the general
# limits alone the API would authorise enough of those to make a dent in the
# bot's account-wide REST budget -- which verification shares, and verification
# is the product.
#
# Ten a minute is far more than a human configuring a server will ever need
# (saving all four groups and posting a panel is five), so this bites long
# before honest use does. Both are env-tunable because the right number depends
# on how many admins are actually using the dashboard.
DEFAULT_WRITE_RATE_LIMIT = 10
DEFAULT_GLOBAL_WRITE_RATE_LIMIT = 60

# The ceiling aiohttp enforces before a handler sees anything. Comfortably
# above the largest honest body — a settings patch of a few fields, or a
# normalised Stripe subscription of eight short values — and far below anything
# worth spending memory on.
MAX_REQUEST_BYTES = 16 * 1024

# The picker asks about the guilds the signed-in user is already a member of.
# Capped so a malformed or hostile query cannot turn one request into an
# unbounded loop.
MAX_GUILD_IDS = 200


class BotAPIConfigError(RuntimeError):
    """The API was switched on but cannot be started safely."""


class SettingRejected(Exception):
    """A submitted setting cannot be stored, with the reason a caller may see.

    Defined here rather than in bot.py because it is the contract between the
    two: the bot raises it from its writer, and this module maps it to a
    status. It carries no data the caller did not already send.

    `locked` separates "your plan does not include this" (403) from "that is
    not a valid value" (400). Collapsing them would leave the website unable to
    tell an admin which of the two happened.
    """

    def __init__(self, field: str, reason: str, *, locked: bool = False):
        super().__init__(reason)
        self.field = field
        self.reason = reason
        self.locked = locked


# One call may not carry more fields than exist. Bounded here as well as by
# client_max_size, because the cheap check is the one that runs first.
MAX_SETTINGS_FIELDS = 32


# Typed keys rather than bare strings, so a handler reaching for something the
# app was never given fails at the lookup instead of at whatever it does with
# the None it got back.
CONFIG_KEY: "web.AppKey" = web.AppKey("config")
DEPS_KEY: "web.AppKey" = web.AppKey("deps")
REPLAY_KEY: "web.AppKey" = web.AppKey("replay")
RATE_KEY: "web.AppKey" = web.AppKey("rate_limiter")
GLOBAL_RATE_KEY: "web.AppKey" = web.AppKey("global_rate_limiter")
WRITE_RATE_KEY: "web.AppKey" = web.AppKey("write_rate_limiter")
GLOBAL_WRITE_RATE_KEY: "web.AppKey" = web.AppKey("global_write_rate_limiter")


# -------------------------------------------------------------------
# What the bot lets this module reach
# -------------------------------------------------------------------
@dataclass(frozen=True)
class BotAPIDeps:
    """The complete list of things the API can do to the bot.

    Ten of the thirteen only read or check. `tests/test_bot_api.py` pins the
    exact field set, so a fourth mutating capability cannot be added by accident
    — adding one means editing that test on purpose, which is a reviewable diff
    rather than a quiet widening.

    `write_settings`, `write_stripe_subscription` and `post_panel` are the whole
    mutating surface. All three are named callables that validate against their
    own rules inside the bot, not generic setters, so the worst a compromised
    dashboard can do is submit valid values for the handful of fields the bot
    has decided are writable, mirror a subscription into one table, and ask for
    the one message it is allowed to post.

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
    read_audit: Callable[[int], Awaitable[Optional[list]]]
    # The Overview page's counts. Aggregates only — no member ids reach this
    # side of the wire, because none are stored to begin with.
    read_overview: Callable[[int], Awaitable[Optional[dict]]]
    # The only writer. Takes (guild_id, actor_id, changes) and either returns
    # the re-read settings, returns None because it could not complete, or
    # raises the bot's SettingRejected for anything the caller got wrong.
    write_settings: Callable[[int, int, dict], Awaitable[Optional[dict]]]
    # Mirror one Stripe subscription event into the bot's own table. Takes
    # (guild_id, payload) and returns whether it was applied, or None because
    # it could not complete — which becomes a 503, which makes Stripe retry.
    #
    # No actor argument, unlike write_settings: there is no human behind a
    # renewal, and the bot records a fixed system actor rather than anything
    # this side could name. The payload is already normalised by the dashboard;
    # nothing raw from Stripe crosses the wire.
    write_stripe_subscription: Callable[[int, dict], Awaitable[Optional[dict]]]
    # The one action. Everything else here stores a value; this makes the bot
    # post a message in somebody's server, so it is named separately rather
    # than folded into write_settings.
    post_panel: Callable[[int, int, str], Awaitable[Optional[dict]]]


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
    write_rate_limit: int = DEFAULT_WRITE_RATE_LIMIT
    global_write_rate_limit: int = DEFAULT_GLOBAL_WRITE_RATE_LIMIT
    # With this off the Stripe route is never registered at all -- a 404, not a
    # disabled handler that has to be trusted to refuse. Same discipline as
    # BOT_API_ENABLED itself: the safest form of "switched off" is code that
    # was never wired up.
    stripe_enabled: bool = False

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
            write_rate_limit=_int_env(
                "BOT_API_WRITE_RATE_LIMIT", DEFAULT_WRITE_RATE_LIMIT
            ),
            global_write_rate_limit=_int_env(
                "BOT_API_GLOBAL_WRITE_RATE_LIMIT", DEFAULT_GLOBAL_WRITE_RATE_LIMIT
            ),
            stripe_enabled=_truthy(os.getenv("STRIPE_ENABLED")),
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
    if not os.access(path, os.R_OK):
        # Existence is not readability, and the difference matters: isfile() is
        # a stat, which succeeds on a key this process cannot open. Without
        # this the API would bind happily and then fail every handshake, which
        # looks like a client problem from both ends.
        #
        # In a container this is nearly always ownership -- the image runs as
        # uid 10001 and a mounted key usually belongs to whoever generated it,
        # at mode 0600.
        raise BotAPIConfigError(
            f"{name} points at {path!r}, which this process cannot read. "
            "If this is a container, check the file's owner against the uid "
            "the image runs as."
        )
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
# The format itself lives in api_tokens.py so the dashboard can sign with the
# identical implementation this verifies with, without also having to install
# aiohttp. Re-exported here because this module is where the enforcement side
# lives, and callers should not need to know the split exists.


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


async def _authorize(
    request: web.Request, *, guild_scoped: bool, system: bool = False
) -> TokenClaims:
    """Run all three gates. Returns the claims, or raises _Denied.

    `system=True` is for the one operation with no person behind it — a Stripe
    webhook, verified on the dashboard and forwarded here. It changes exactly
    two things and nothing else:

    * the actor must be SYSTEM_ACTOR_ID, and the operation must be on the
      `SYSTEM_OPERATIONS` allowlist. Both are checked, in both directions: a
      system token cannot be used on a human route, and a human token cannot be
      used on a system one. Since the operation is derived from the route the
      router matched, neither is something a caller can assert.
    * the Administrator check is skipped, because there is nobody to check. So
      is `guild_present` — a renewal must still be recorded for a guild the bot
      has been kicked from, or a server that re-adds the bot would find itself
      unsubscribed despite still being billed.

    Everything else is identical: mTLS, the signature, the guild binding, the
    method binding, the replay guard and the write budget all still apply.
    """
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

    # --- Human or machine, and never the wrong one for this route ---
    # Checked before the budgets so a mismatched token is refused rather than
    # charged, and immediately after the signature so both sides of the test
    # are on verified claims.
    is_system_operation = operation in SYSTEM_OPERATIONS
    if is_system_operation != system:
        # Only reachable if a route was registered with the wrong flag; the
        # allowlist and the handler would then disagree about what this is.
        raise _Denied(
            _deny(request, 403, "operation_not_permitted", actor=claims.actor_id)
        )
    if system and claims.actor_id != SYSTEM_ACTOR_ID:
        raise _Denied(_deny(request, 403, "not_system_actor", actor=claims.actor_id))
    if not system and claims.actor_id == SYSTEM_ACTOR_ID:
        # The system actor has no Administrator to check, so it must never be
        # able to reach a route whose only authority check is that one.
        raise _Denied(_deny(request, 403, "system_actor_not_permitted", actor=claims.actor_id))

    # --- Budgets, charged to the authenticated actor ---
    limiter: RateLimiter = request.app[RATE_KEY]
    global_limiter: RateLimiter = request.app[GLOBAL_RATE_KEY]
    if not limiter.allow(str(claims.actor_id)) or not global_limiter.allow("*"):
        raise _Denied(_deny(request, 429, "rate_limited", actor=claims.actor_id))

    # Mutating requests pay a second, much smaller budget on top. A GET is
    # answered from the gateway cache and costs Discord nothing; a PATCH or a
    # POST here turns into real REST calls against the same account-wide budget
    # verification runs on. Charged after the general one so a caller cannot
    # spend write slots by hammering reads.
    if request.method != "GET":
        write_limiter: RateLimiter = request.app[WRITE_RATE_KEY]
        global_write_limiter: RateLimiter = request.app[GLOBAL_WRITE_RATE_KEY]
        if not write_limiter.allow(str(claims.actor_id)) or not global_write_limiter.allow("*"):
            raise _Denied(
                _deny(request, 429, "rate_limited", actor=claims.actor_id)
            )

    # --- The bot's own answer ---
    if not deps.is_ready():
        raise _Denied(_deny(request, 503, "not_ready", actor=claims.actor_id))

    if guild_scoped and not system:
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


async def handle_update_settings(request: web.Request) -> web.Response:
    """The one route that changes anything. Same three gates as every read.

    The token that authorises this is bound to `PATCH /...`, not just to the
    path, so a token minted for the settings *read* cannot be replayed to write
    them -- see `_operation_for`, which takes the method from the route the
    router actually matched.

    What may be written is decided entirely inside the bot. This handler does
    not know which fields exist, which are premium, or what values are legal;
    it validates the envelope, hands the body to the one writer in BotAPIDeps,
    and turns a refusal into a status. That is deliberate: the dashboard is the
    internet-facing half and is assumed to be compromised eventually, so the
    list of writable fields must not live on that side of the wire, nor in a
    module that could be talked into a generic update.
    """
    try:
        claims = await _authorize(request, guild_scoped=True)
    except _Denied as denied:
        return denied.response

    deps: BotAPIDeps = request.app[DEPS_KEY]
    guild_id = int(request.match_info["guild_id"])

    try:
        body = await request.json()
    except (ValueError, UnicodeDecodeError):
        return _deny(request, 400, "bad_json", actor=claims.actor_id)

    if not isinstance(body, dict):
        return _deny(request, 400, "bad_json", actor=claims.actor_id)
    changes = body.get("fields")
    if not isinstance(changes, dict) or not changes:
        return _deny(request, 400, "bad_fields", actor=claims.actor_id)
    if len(changes) > MAX_SETTINGS_FIELDS:
        return _deny(request, 400, "too_many_fields", actor=claims.actor_id)

    try:
        payload = await deps.write_settings(guild_id, claims.actor_id, changes)
    except SettingRejected as rejected:
        # 403 for "not on your plan", 400 for "that is not a valid value".
        return _deny(
            request,
            403 if rejected.locked else 400,
            rejected.reason,
            actor=claims.actor_id,
        )

    if payload is None:
        return _deny(request, 503, "unavailable", actor=claims.actor_id)
    # The stored state, re-read, rather than an echo of the request. What the
    # admin sees next is then what is actually saved.
    return _json(payload)


async def handle_post_panel(request: web.Request) -> web.Response:
    """Post or refresh a guild's instructions panel.

    The only endpoint whose effect is visible to people who are not the caller:
    everything else changes a row, this puts a message in a server. It gets the
    same three gates and one more consideration -- what happens when it is
    called twice. That is answered inside the bot, which refreshes an existing
    panel rather than posting a second one, so a double-click costs an edit.
    """
    try:
        claims = await _authorize(request, guild_scoped=True)
    except _Denied as denied:
        return denied.response

    deps: BotAPIDeps = request.app[DEPS_KEY]
    guild_id = int(request.match_info["guild_id"])

    try:
        body = await request.json()
    except (ValueError, UnicodeDecodeError):
        return _deny(request, 400, "bad_json", actor=claims.actor_id)
    if not isinstance(body, dict):
        return _deny(request, 400, "bad_json", actor=claims.actor_id)

    channel_id = body.get("channel_id")
    if not isinstance(channel_id, (str, int)) or not str(channel_id).isdigit():
        return _deny(request, 400, "bad_channel_id", actor=claims.actor_id)

    try:
        result = await deps.post_panel(guild_id, claims.actor_id, str(channel_id))
    except SettingRejected as rejected:
        return _deny(
            request,
            403 if rejected.locked else 400,
            rejected.reason,
            actor=claims.actor_id,
        )

    if result is None:
        return _deny(request, 503, "unavailable", actor=claims.actor_id)
    return _json(result)


# The normalised subscription payload's complete field set. Listed here so the
# envelope check is exhaustive rather than "the ones we happened to read": an
# extra key means the two ends disagree about the contract, and the interesting
# case is the one where the dashboard has been talked into sending more than it
# should.
STRIPE_PAYLOAD_FIELDS = frozenset(
    {
        "event_id",
        "event_created",
        "customer_id",
        "subscription_id",
        "price_id",
        "status",
        "current_period_end",
        "cancel_at_period_end",
    }
)

# No field in that payload is a free-text one. Stripe ids are short, statuses
# are a closed set of words, timestamps are ISO-8601 — so anything long is
# either a bug or someone probing what this will store.
MAX_STRIPE_FIELD_LEN = 255


async def handle_put_stripe_subscription(request: web.Request) -> web.Response:
    """Record a card subscription's current state. The one route with no human.

    Registered only when `STRIPE_ENABLED` is set on the bot; with it unset this
    handler is not wired up at all and the path is a plain 404. That is the same
    kill-switch discipline as `BOT_API_ENABLED` and for the same reason — the
    most reliable way to be sure a handler cannot run is for it never to have
    been added to the router.

    The signature that makes this trustworthy is Stripe's, and it was checked on
    the dashboard, which is where the public ingress is. What crosses this wire
    is the dashboard's normalised summary of an event it already verified, and
    it arrives with the same token, mTLS and replay protection as every other
    call. This handler validates the envelope only; what any of it *means* — a
    replay, an out-of-order event, a status that grants premium — is decided
    inside the bot.
    """
    try:
        claims = await _authorize(request, guild_scoped=True, system=True)
    except _Denied as denied:
        return denied.response

    deps: BotAPIDeps = request.app[DEPS_KEY]
    guild_id = int(request.match_info["guild_id"])

    try:
        body = await request.json()
    except (ValueError, UnicodeDecodeError):
        return _deny(request, 400, "bad_json", actor=claims.actor_id)
    if not isinstance(body, dict):
        return _deny(request, 400, "bad_json", actor=claims.actor_id)

    subscription = body.get("subscription")
    if not isinstance(subscription, dict) or not subscription:
        return _deny(request, 400, "bad_subscription", actor=claims.actor_id)
    if set(subscription) - STRIPE_PAYLOAD_FIELDS:
        return _deny(request, 400, "unknown_field", actor=claims.actor_id)
    for value in subscription.values():
        if isinstance(value, str) and len(value) > MAX_STRIPE_FIELD_LEN:
            return _deny(request, 400, "field_too_long", actor=claims.actor_id)
    # The one non-string field, checked for its actual type rather than left to
    # be coerced. `bool("false")` is True, so a normalisation slip on the other
    # side of the wire would silently turn "renews on the 3rd" into "ends on
    # the 3rd" -- a wrong statement about somebody's money, arriving through
    # the one field where a string and a boolean look equally plausible.
    if "cancel_at_period_end" in subscription and not isinstance(
        subscription["cancel_at_period_end"], bool
    ):
        return _deny(request, 400, "bad_cancel_at_period_end", actor=claims.actor_id)

    try:
        result = await deps.write_stripe_subscription(guild_id, subscription)
    except SettingRejected as rejected:
        return _deny(request, 400, rejected.reason, actor=claims.actor_id)

    if result is None:
        # 503 rather than 200: the dashboard turns this into a non-2xx for
        # Stripe, which retries for up to three days. A bot restart or a
        # tunnel blip must never cost a subscription event.
        return _deny(request, 503, "unavailable", actor=claims.actor_id)
    return _json(result)


def create_app(config: BotAPIConfig, deps: BotAPIDeps) -> web.Application:
    """Wire the routes. One PATCH, one POST, one conditional PUT, rest GET."""
    app = web.Application(client_max_size=MAX_REQUEST_BYTES)
    app[CONFIG_KEY] = config
    app[DEPS_KEY] = deps
    app[REPLAY_KEY] = ReplayGuard()
    app[RATE_KEY] = RateLimiter(config.rate_limit, config.rate_window)
    app[GLOBAL_RATE_KEY] = RateLimiter(config.global_rate_limit, config.rate_window)
    app[WRITE_RATE_KEY] = RateLimiter(config.write_rate_limit, config.rate_window)
    app[GLOBAL_WRITE_RATE_KEY] = RateLimiter(
        config.global_write_rate_limit, config.rate_window
    )

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
    app.router.add_get("/api/v1/guilds/{guild_id}/audit", _guild_reader("read_audit"))
    app.router.add_get(
        "/api/v1/guilds/{guild_id}/overview", _guild_reader("read_overview")
    )
    app.router.add_patch(
        "/api/v1/guilds/{guild_id}/settings", handle_update_settings
    )
    app.router.add_post("/api/v1/guilds/{guild_id}/panel", handle_post_panel)
    if config.stripe_enabled:
        app.router.add_put(
            "/api/v1/guilds/{guild_id}/stripe-subscription",
            handle_put_stripe_subscription,
        )

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
