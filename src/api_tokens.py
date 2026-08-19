"""The scoped request tokens the dashboard signs and the bot verifies.

Split out of `bot_api.py` so both ends can import the *same* implementation
without the dashboard also having to install aiohttp. That sharing is the whole
point: a signer and a verifier that drift apart is the classic way an auth
scheme quietly stops meaning anything, and this format's security rests
entirely on both sides agreeing about what is covered by the signature.

Deliberately stdlib-only. Nothing here should ever grow a dependency, because
the moment it does, one of the two services has to take it on for no reason of
its own.

The token names one Discord user, one guild, and one operation, and is good for
about thirty seconds and a single use. See `bot_api.py` for the replay guard
and the enforcement side.
"""

from __future__ import annotations

import base64
import binascii
import hmac
import json
import secrets
import time
from dataclasses import dataclass
from hashlib import sha256
from typing import Optional

TOKEN_VERSION = "v1"

# The operations both ends must agree on, verbatim. The bot derives what it
# expects from the aiohttp route it actually matched, so these are the route
# templates exactly as registered -- braces and all, never the interpolated
# path. Getting one wrong is a 403 `wrong_operation`, which looks like a
# permissions bug rather than a typo, so they live here with the signer.
OP_LIST_GUILDS = "GET /api/v1/guilds"
OP_GUILD_SETTINGS = "GET /api/v1/guilds/{guild_id}/settings"
OP_GUILD_ROLES = "GET /api/v1/guilds/{guild_id}/roles"
OP_GUILD_CHANNELS = "GET /api/v1/guilds/{guild_id}/channels"
OP_GUILD_PANEL = "GET /api/v1/guilds/{guild_id}/panel"
OP_GUILD_AUDIT = "GET /api/v1/guilds/{guild_id}/audit"
OP_GUILD_OVERVIEW = "GET /api/v1/guilds/{guild_id}/overview"
# The method is part of the operation, so a token minted to read a guild's
# settings cannot be replayed to write them.
OP_UPDATE_SETTINGS = "PATCH /api/v1/guilds/{guild_id}/settings"
# An action, not a setting: it makes the bot post in a server.
OP_POST_PANEL = "POST /api/v1/guilds/{guild_id}/panel"
# Also an action: it puts a job on a queue that makes a VRChat account join
# a group. The group is not in the request -- the bot reads it from the
# guild's own settings -- so this token authorises the *asking*, nothing more.
OP_VERIFY_GROUP = "POST /api/v1/guilds/{guild_id}/verify-group"
# The only operation with no human behind it: a Stripe webhook, verified on the
# dashboard and forwarded here. See SYSTEM_ACTOR_ID.
OP_PUT_STRIPE_SUBSCRIPTION = "PUT /api/v1/guilds/{guild_id}/stripe-subscription"

# The actor named by a token that no person asked for.
#
# Every other token in this scheme names a signed-in Discord user, and the bot
# then checks that person is an Administrator of the guild. A subscription
# renewal a year from now has no such person: the admin who checked out may
# have left the server, and taking the actor from the webhook body would mean
# authority coming from a request body, which is the one thing this design
# forbids everywhere else.
#
# So system operations name this instead. Zero is not a valid Discord snowflake
# (they are timestamp-derived and start well above it), so it cannot collide
# with a real user, and it is not a value the dashboard can be talked into
# producing for a human -- the operation is what selects it, not the caller.
#
# The bot skips its Administrator check for exactly the operations it has
# decided are system operations, and for no others. See `bot_api._authorize`.
SYSTEM_ACTOR_ID = 0
# Which operations are allowed to arrive with SYSTEM_ACTOR_ID. An allowlist
# rather than a flag on the token, so a token cannot claim to be a system one:
# both ends derive it from the operation, which is bound to the method and path
# the router actually matched.
SYSTEM_OPERATIONS = frozenset({OP_PUT_STRIPE_SUBSCRIPTION})

# Tokens are minted per request and used immediately. Thirty seconds is
# generous for a call across a tunnel and short enough that a captured token is
# worthless by the time anyone notices they have it.
DEFAULT_TOKEN_TTL = 30
# Allowance for clock drift between the VPS and the homelab. Small on purpose:
# every second here widens the replay window at both ends.
DEFAULT_CLOCK_SKEW = 5

# An HMAC key shorter than its own digest is weaker than the primitive it feeds.
MIN_SIGNING_KEY_BYTES = 32


class TokenError(Exception):
    """A token was absent, malformed, expired, or not for this request.

    `actor` is set for every failure raised *after* the signature verified, so
    the denial log can name who minted the token. That is the difference
    between "someone sent us garbage" and "this specific signed-in user tried
    to reach a guild they were not scoped for" -- and the second is the one
    worth alerting on.
    """

    def __init__(self, reason: str, actor: Optional[int] = None):
        super().__init__(reason)
        self.reason = reason
        self.actor = actor


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
