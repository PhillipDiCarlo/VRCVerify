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
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlparse

from api_tokens import MIN_SIGNING_KEY_BYTES

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
    # The product whose active prices are the plans on offer. One id, and the
    # plans themselves live in Stripe: creating a price publishes a plan and
    # archiving one retires it, with nothing here to keep in step.
    #
    # This replaced three STRIPE_PRICE_* variables. Those had to be edited,
    # deployed and kept matching an account they could not see, and the failure
    # they invited was silent -- a price rotated in Stripe left the page
    # offering an id that no longer existed, and the error arrived at the
    # moment somebody clicked Buy.
    #
    # It does NOT mean the browser may name a price. The form submits a price
    # id, and the checkout route accepts it only after finding it in a freshly
    # fetched list of this product's active prices. The guarantee is the same
    # one as before -- no price the server has not authorised -- enforced
    # against Stripe's live answer instead of a static table.
    stripe_product_id: str = ""
    # Which billing-portal configuration a portal session opens with. Empty
    # means Stripe's account default, which is what shipped and is the reason
    # this exists.
    #
    # A portal configuration decides what a customer may do to their own
    # subscription, and "switch plans" is one of the things it decides. The
    # account default is shared by every product on the Stripe account -- and
    # this account has others -- so its plan list is not necessarily this
    # product's. A customer who can switch to another product's cheaper
    # recurring price keeps premium at that price, because the bot grants on
    # subscription STATUS and treats an unrecognised price id as still paid.
    # That rule is deliberate (a catalogue mismatch must not switch off a
    # paying customer) and it is exactly why the price cannot also be the gate.
    #
    # Naming a configuration scoped to this product's prices closes it without
    # touching the shared default, which other products depend on.
    #
    # Deliberately OPTIONAL rather than required-when-enabled, unlike every
    # other Stripe variable here. Those have no safe default and refusing to
    # boot is better than pretending; this one degrades to exactly the
    # behaviour that is live today. Making it mandatory would mean a deploy
    # that reaches this host before the variable does takes the whole site
    # down, which has already happened once on 2026-08-15.
    stripe_portal_configuration_id: str = ""
    # The VRCVerify Discord, offered on the changelog page and in the bell
    # panel so an admin can follow the announcement channel and have updates
    # crossposted into their own server (#138).
    #
    # Optional, and empty means the row is simply not rendered -- the same
    # posture bot.py takes. This host and the bot's deploy separately, so one
    # will have the value before the other; neither is broken while they
    # disagree, which is the point of making absence inert rather than an error.
    support_invite_url: str = ""

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
        stripe_product_id = ""
        stripe_portal_configuration_id = ""
        if stripe_enabled:
            # Refuse to boot rather than come up half-configured. A dashboard
            # missing its webhook secret rejects every event Stripe sends and
            # looks healthy doing it. Worse than not starting, and one typo
            # away.
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
            stripe_product_id = _require("STRIPE_PRODUCT_ID")
            if not stripe_product_id.startswith("prod_"):
                # A price id here is the plausible mistake -- they sit next to
                # each other in the Stripe dashboard and both are "the thing I
                # copied for the plan". It would list no prices at all, so the
                # page would apologise forever with nothing in the logs saying
                # why, which is exactly the failure this check is cheap enough
                # to be worth preventing.
                raise DashboardConfigError(
                    "STRIPE_PRODUCT_ID must be a Stripe product id (prod_...); "
                    f"got {stripe_product_id!r}. A price id (price_...) here "
                    "would match no prices and offer no plans."
                )
            stripe_portal_configuration_id = (
                os.getenv("STRIPE_PORTAL_CONFIGURATION_ID") or ""
            ).strip()
            if stripe_portal_configuration_id and not (
                stripe_portal_configuration_id.startswith("bpc_")
            ):
                # Checked even though the variable is optional, because the
                # plausible mistakes all fail LATE and quietly: a product id
                # here, or a configuration id from the other mode. Stripe
                # rejects the portal session, the customer is bounced to
                # error:portal, and nothing says which of the two ids was
                # wrong. An empty value is the one thing this must not treat
                # as an error -- that is the documented way to keep Stripe's
                # default.
                raise DashboardConfigError(
                    "STRIPE_PORTAL_CONFIGURATION_ID must be a billing portal "
                    f"configuration id (bpc_...); got "
                    f"{stripe_portal_configuration_id!r}. Leave it unset to "
                    "use the Stripe account's default configuration."
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
            stripe_product_id=stripe_product_id,
            stripe_portal_configuration_id=stripe_portal_configuration_id,
            support_invite_url=(os.getenv("SUPPORT_INVITE_URL") or "").strip(),
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
    if not os.access(path, os.R_OK):
        # Existence is not enough, and checking only existence is how this
        # fails in the worst possible way: os.path.isfile is a stat, which
        # succeeds on a file the process cannot open. The service then starts
        # cleanly, serves pages, and dies at the first TLS handshake with an
        # error that names neither the file nor the reason.
        #
        # In a container this is almost always ownership rather than mode: the
        # image runs as uid 10001 and a key mounted from the host usually
        # belongs to whoever generated it, at 0600.
        raise DashboardConfigError(
            f"{name} points at {path!r}, which this process cannot read. "
            "If this is a container, check the file's owner against the uid "
            "the image runs as -- mode 0600 plus a different owner reads as "
            "'exists' but not as 'readable'."
        )
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
