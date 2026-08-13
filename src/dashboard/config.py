"""Dashboard configuration, validated at startup or not started at all.

Same discipline as `bot_api.BotAPIConfig`: a misconfiguration must stop the
service rather than degrade it. A web app that boots with a weak session key or
a missing client certificate is worse than one that refuses to boot, because
nobody finds out until it matters.

Everything here is read from the environment. Nothing is defaulted that would
be dangerous to guess.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Mapping, Optional
from urllib.parse import urlparse

from api_tokens import MIN_SIGNING_KEY_BYTES

# The three plans, as slugs. The browser may name one of these and nothing
# else; every price id is looked up server-side from the table below.
STRIPE_PLAN_SLUGS = ("monthly", "six_months", "yearly")

# Flask signs the session cookie with this. The cookie carries only an opaque
# session id, but forging one is still forging a session, so it gets the same
# floor as the API signing key.
MIN_SECRET_KEY_BYTES = 32

# How long a login lasts, no matter how active. Deliberately not sliding: an
# absolute cap means a stolen cookie has a bounded life even if the thief keeps
# it warm.
DEFAULT_SESSION_MAX_AGE = 8 * 3600
# How long the OAuth guild list is reused before the user has to log in again
# to refresh it. The Discord token is discarded at login (see oauth.py), so
# there is no way to refresh this without a new authorisation -- which is the
# intended trade.
DEFAULT_GUILD_CACHE_TTL = 900


class DashboardConfigError(RuntimeError):
    """The dashboard cannot start safely with the environment it was given."""


@dataclass(frozen=True)
class DashboardConfig:
    discord_client_id: str
    discord_client_secret: str
    oauth_redirect_uri: str
    secret_key: str
    session_db_path: str
    bot_api_url: str
    bot_api_client_cert: str
    bot_api_client_key: str
    bot_api_ca: str
    bot_api_signing_key: bytes
    session_max_age: int = DEFAULT_SESSION_MAX_AGE
    guild_cache_ttl: int = DEFAULT_GUILD_CACHE_TTL
    request_timeout: int = 10
    # --- Stripe (#88). All absent unless STRIPE_ENABLED is set. ---
    stripe_enabled: bool = False
    stripe_secret_key: str = ""
    stripe_webhook_secret: str = ""
    # Slug -> price id. The browser can only ever supply the slug; the price is
    # looked up here. A form-supplied price id would let anyone check out
    # against any price on the account, including a $0 one made while testing.
    stripe_prices: Mapping[str, str] = field(default_factory=dict)

    def plan_for(self, price_id: str) -> Optional[str]:
        """The plan slug for a price id, or None if it is not one of ours.

        None is not an error and must never be treated as "not subscribed" —
        see the callers. A price can be absent from this table for entirely
        ordinary reasons: a plan switched in the billing portal, a price
        replaced during a pricing change, an id rotated between test and live.
        The subscription is real and paid for; only the label is unknown.
        """
        for slug, configured in self.stripe_prices.items():
            if configured == price_id:
                return slug
        return None

    @classmethod
    def from_env(cls) -> "DashboardConfig":
        client_id = _require("DISCORD_CLIENT_ID")
        client_secret = _require("DISCORD_CLIENT_SECRET")
        redirect_uri = _require("OAUTH_REDIRECT_URI")
        _validate_redirect(redirect_uri)

        secret_key = _require("DASHBOARD_SECRET_KEY")
        if len(secret_key) < MIN_SECRET_KEY_BYTES:
            raise DashboardConfigError(
                f"DASHBOARD_SECRET_KEY must be at least {MIN_SECRET_KEY_BYTES} "
                "characters; refusing to start."
            )

        signing_key = _require("BOT_API_TOKEN_SIGNING_KEY").encode("utf-8")
        if len(signing_key) < MIN_SIGNING_KEY_BYTES:
            raise DashboardConfigError(
                f"BOT_API_TOKEN_SIGNING_KEY must be at least {MIN_SIGNING_KEY_BYTES} "
                "characters, and identical to the bot's; refusing to start."
            )
        if signing_key == secret_key.encode("utf-8"):
            # Two different trust domains: one signs cookies handed to browsers,
            # the other authorises calls into the homelab. Sharing them means a
            # cookie-signing bug becomes an API-forgery bug.
            raise DashboardConfigError(
                "BOT_API_TOKEN_SIGNING_KEY and DASHBOARD_SECRET_KEY must differ."
            )

        bot_api_url = _require("BOT_API_URL").rstrip("/")
        if not bot_api_url.startswith("https://"):
            raise DashboardConfigError(
                f"BOT_API_URL must be https; got {bot_api_url!r}. The mTLS is the "
                "authentication, and plain http would discard it."
            )

        stripe_enabled = _truthy(os.getenv("STRIPE_ENABLED"))
        stripe_secret_key = ""
        stripe_webhook_secret = ""
        stripe_prices: dict = {}
        if stripe_enabled:
            # Refuse to boot rather than come up half-configured. A dashboard
            # missing its webhook secret rejects every event Stripe sends and
            # looks healthy doing it; one missing a price id renders a plan card
            # that 500s when somebody clicks Buy. Both are worse than not
            # starting, and both are one typo away.
            stripe_secret_key = _require("STRIPE_SECRET_KEY")
            stripe_webhook_secret = _require("STRIPE_WEBHOOK_SECRET")
            if not stripe_secret_key.startswith(("sk_", "rk_")):
                raise DashboardConfigError(
                    "STRIPE_SECRET_KEY does not look like a Stripe secret key. "
                    "A publishable key (pk_) here would fail every call."
                )
            if not stripe_webhook_secret.startswith("whsec_"):
                raise DashboardConfigError(
                    "STRIPE_WEBHOOK_SECRET must be the signing secret from the "
                    "webhook endpoint (whsec_...), not an API key."
                )
            stripe_prices = {
                slug: _require(f"STRIPE_PRICE_{slug.upper()}")
                for slug in STRIPE_PLAN_SLUGS
            }
            duplicates = len(stripe_prices) - len(set(stripe_prices.values()))
            if duplicates:
                # Two plans on one price means someone pays for six months and
                # is billed monthly, or the reverse. Cheap to check, expensive
                # to discover from a customer.
                raise DashboardConfigError(
                    "STRIPE_PRICE_* must name three different prices; "
                    f"{duplicates + 1} of them are the same id."
                )

        return cls(
            discord_client_id=client_id,
            discord_client_secret=client_secret,
            oauth_redirect_uri=redirect_uri,
            secret_key=secret_key,
            session_db_path=os.getenv("SESSION_DB_PATH", "/data/sessions.db"),
            bot_api_url=bot_api_url,
            bot_api_client_cert=_require_file("BOT_API_CLIENT_CERT"),
            bot_api_client_key=_require_file("BOT_API_CLIENT_KEY"),
            bot_api_ca=_require_file("BOT_API_CA"),
            bot_api_signing_key=signing_key,
            session_max_age=_int_env("DASHBOARD_SESSION_MAX_AGE", DEFAULT_SESSION_MAX_AGE),
            guild_cache_ttl=_int_env("DASHBOARD_GUILD_CACHE_TTL", DEFAULT_GUILD_CACHE_TTL),
            request_timeout=_int_env("DASHBOARD_REQUEST_TIMEOUT", 10),
            stripe_enabled=stripe_enabled,
            stripe_secret_key=stripe_secret_key,
            stripe_webhook_secret=stripe_webhook_secret,
            stripe_prices=stripe_prices,
        )


def _truthy(raw: Optional[str]) -> bool:
    return (raw or "").strip().lower() in {"1", "true", "yes", "on"}


def _require(name: str) -> str:
    value = (os.getenv(name) or "").strip()
    if not value:
        raise DashboardConfigError(f"{name} is required; refusing to start.")
    return value


def _require_file(name: str) -> str:
    path = _require(name)
    if not os.path.isfile(path):
        raise DashboardConfigError(f"{name} points at {path!r}, which is not a file.")
    return path


def _int_env(name: str, default: int, minimum: int = 1) -> int:
    try:
        return max(minimum, int(os.getenv(name, str(default))))
    except ValueError:
        return default


def _validate_redirect(uri: str) -> None:
    """The redirect URI is where Discord sends an authorisation code.

    Over plain http that code crosses the network in the clear and can be
    stolen and exchanged before the real user's browser gets there. Discord
    itself allows http for localhost, and so do we, for local development only.
    """
    parsed = urlparse(uri)
    if parsed.scheme == "https":
        return
    if parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1"}:
        return
    raise DashboardConfigError(
        f"OAUTH_REDIRECT_URI must be https (or http on localhost); got {uri!r}."
    )


def optional_config() -> Optional[DashboardConfig]:
    """Config, or None -- for tooling that must import without a full env."""
    try:
        return DashboardConfig.from_env()
    except DashboardConfigError:
        return None
