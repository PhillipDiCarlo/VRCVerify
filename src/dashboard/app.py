"""The dashboard web app: login, picker, and one server's three sections.

Picking a server lands on Overview and the sidebar leads to Settings and
Subscriptions. All three authorise identically -- a session to prove who is
asking, then the bot to decide what they may see -- and all three fail
identically, through `_guild_page_unavailable`. That second half matters as
much as the first: an oracle for "which servers run 18+ gating" only has to
exist on one of the three routes to be worth using, so none of them gets to
invent its own refusal page.

Which fields the write path covers is the bot's decision, reported per field in
the settings payload, so this app renders a control only where the bot has
already said it would accept the value. It does not hold a copy of that list,
because a second copy is a thing that can disagree with the enforcing one.

Nothing here validates a value it sends. The bot re-checks Administrator,
re-checks the plan, validates against its own allowlist, and records the change
— which is the arrangement that lets this process be the one assumed to fall
over.

* **The page must say exactly what the bot would.** Two settings a lapsed plan
  saves but does not act on, three it refuses to save at all. Collapsing that
  into one "premium" state would make the website stricter than the slash
  commands; `settings_view` keeps them apart.

Design notes worth keeping in view while reading:

* **Cloudflare Access sits in front of this in development and comes off at
  launch.** Nothing here may ever read `Cf-Access-*` headers, because code that
  authorised on them would silently become a complete bypass the day the wall
  is removed. Authority is the Discord session plus the bot's own answer.
* **The reverse proxy is trusted for exactly one thing**: the scheme, via
  `X-Forwarded-Proto`. Without it Flask builds `http://` callback URLs and
  Discord rejects them.
* **No client-side framework, no CDN, no inline script.** The CSP below is
  strict enough to be worth having only because the pages are plain enough not
  to need anything else. The collapsing sidebar is a form for that reason --
  `/prefs/nav` writes a cookie and redirects back, and it is the one POST here
  that never reaches the bot. A hidden checkbox with a sibling selector would
  collapse it with no request at all, and was rejected because the collapsed
  state has to survive navigation: the cookie would then be a second copy of
  the same fact, free to disagree with what the browser was showing.
"""

from __future__ import annotations

import hashlib
import logging
import mimetypes
import os
import re
import secrets
import threading
import time
from urllib.parse import urlsplit
from typing import Optional

from flask import (
    Flask,
    abort,
    g,
    make_response,
    redirect,
    render_template,
    request,
    url_for,
)
from markupsafe import Markup
import requests
from werkzeug.middleware.proxy_fix import ProxyFix

from dashboard import (
    changelog,
    oauth,
    overview_view,
    settings_view,
    stripe_events,
    subscription_view,
)
from dashboard.botapi import BotAPIClient, BotAPIError
from dashboard.config import DashboardConfig
from dashboard.sessions import SessionStore
from dashboard.stripe_api import StripeAPIError, StripeClient

# Flat import: shipped alongside dashboard/ in the image, like api_tokens.
from log_safety import install_log_scrubbing

logger = logging.getLogger(__name__)

# The webhook's own budget. Sized well above Stripe's honest traffic -- a
# renewal storm across every subscribed guild is still a handful a second -- and
# far below what it would take to make this endpoint interesting to point a
# botnet at. Exceeding it returns 429, which Stripe treats as a retry rather
# than a failure, so the cost of a burst is delay and never loss.
STRIPE_WEBHOOK_RATE_LIMIT = 120
STRIPE_WEBHOOK_RATE_WINDOW = 60

# The hard ceiling on any request body reaching this app, applied globally in
# create_app. Generous next to the largest honest body here and small enough
# that buffering one costs nothing.
MAX_REQUEST_BYTES = 256 * 1024

# How long the plan list from Stripe is reused, in seconds.
#
# Short, because it is the lag between changing a price in Stripe and seeing it
# on the page, and the whole point of fetching them was to make that a Stripe
# change rather than a deploy. Not zero, because otherwise every render of this
# page -- and every checkout, which re-fetches to validate -- is a synchronous
# call to a third party on the request path.
STRIPE_PRICE_CACHE_TTL = 300

# How long a browser may keep the public pricing page (#188). Added to
# STRIPE_PRICE_CACHE_TTL rather than overlapping it: the two are in series, so
# the worst case a reader can see is the sum. See `harden()` for why this is
# `private` and not `public`.
PRICING_PAGE_CACHE_TTL = 60


# A VRChat file id and version, as they appear in a group's icon_url. The
# ONLY parts of an upstream URL this app will accept, and they go into a fixed
# template below -- there is no request in which a caller names a host.
VRCHAT_FILE_ID_RE = re.compile(
    r"^file_[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I
)

# 128 for a 64px slot: the retina size, and the largest that is still served as
# a real image -- above 512 VRChat falls back to the raw upload as
# application/octet-stream. Measured 2026-08-19.
VRCHAT_ICON_URL = "https://api.vrchat.cloud/api/1/image/{file_id}/{version}/128"

# That endpoint answers 302, not 200 -- it hands out a signed, expiring URL on
# VRChat's file host, and the picture is there rather than here. So redirects
# have to be followed, and following them is exactly what would turn a fetcher
# aimed at one fixed host into one aimed wherever a response points, a
# link-local metadata address included.
#
# Followed by hand, one hop at a time, with every hop checked against this set.
# `requests` offers no per-hop hook, and allow_redirects=True would take any of
# them on trust.
VRCHAT_ICON_HOSTS = frozenset({"api.vrchat.cloud", "files.vrchat.cloud"})
VRCHAT_ICON_MAX_HOPS = 3


def _vrchat_hop_allowed(url: str) -> bool:
    """May this fetcher follow to `url`?

    https, a host on the list, and the default port. `.hostname` is the part
    that matters: it lower-cases, and it drops any userinfo, so
    `https://files.vrchat.cloud@evil.test/x` reads as evil.test rather than as
    a host we trust.
    """
    try:
        parsed = urlsplit(url)
    except ValueError:
        return False
    if parsed.scheme != "https" or parsed.hostname not in VRCHAT_ICON_HOSTS:
        return False
    try:
        return parsed.port is None
    except ValueError:
        return False

# VRChat answers 403 to a request without a browser-shaped User-Agent, so this
# has to look like one. It is not pretending to be a person -- the same
# contact address the bot identifies itself with is appended, which is what
# VRChat's own guidelines ask for.
VRCHAT_ICON_USER_AGENT = (
    "Mozilla/5.0 (compatible; VRCVerifyDashboard/1.0; +contact@esattotech.com)"
)

# ASCII digits only. `str.isdigit()` and `\d` both accept Unicode digits --
# "\u0663".isdigit() is True -- and while a percent-encoded Arabic-Indic three
# in the path only earns a 404 from VRChat, a validator that does not mean what
# it says is one somebody will later rely on for something that matters.
VRCHAT_FILE_VERSION_RE = re.compile(r"^[0-9]{1,4}$")

# The measured icon at this size is 24KB, so this is ten times what a real one
# needs. It bounds the cache as much as the response: entries are held in
# memory in a read_only container, and limit x max_bytes is the ceiling.
VRCHAT_ICON_MAX_BYTES = 256 * 1024
VRCHAT_ICON_TIMEOUT = 6
VRCHAT_ICON_TTL = 3600
# Failures expire far sooner than successes. Caching them at all is deliberate
# -- see _IconCache -- but an hour of hidden icon after a thirty-second blip
# upstream is the cache being wrong for far longer than the thing it cached.
VRCHAT_ICON_FAILURE_TTL = 60

# The signatures of the formats an <img> can use and this app will pass on.
# Checked against the bytes rather than the upstream Content-Type, because the
# whole reason this proxy exists is that VRChat's content types are not
# reliable.
_IMAGE_SIGNATURES = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
    (b"RIFF", "image/webp"),  # refined below
)


def _sniff_image(body: bytes) -> Optional[str]:
    """The image type these bytes actually are, or None.

    Bytes, not headers. An upstream that labels a PNG
    `application/octet-stream` is exactly the situation this function is for,
    and passing that label through to a browser is what made the icon a broken
    image in the first place.
    """
    if body[:4] == b"RIFF" and body[8:12] == b"WEBP":
        return "image/webp"
    for signature, content_type in _IMAGE_SIGNATURES:
        if signature != b"RIFF" and body.startswith(signature):
            return content_type
    return None


class _IconCache:
    """Fetched group icons, briefly, so a page view is not an upstream request.

    In-process like _PriceCache and _RateLimiter, for the same reason: there is
    one container, and a shared cache would be a dependency added to hold a
    copy of a picture.

    Failures are cached too, unlike prices -- and deliberately. A missing icon
    costs nothing, while retrying a dead upstream on every render of every
    settings page turns one broken image into a stream of outbound requests.
    They expire much sooner, though: see VRCHAT_ICON_FAILURE_TTL.

    Locked, unlike _RateLimiter and _PriceCache beside it, and for a reason
    neither of those has: this one *iterates* its dict to evict, and gunicorn
    runs four threads per worker. `min()` over a dict another thread is writing
    raises "dictionary changed size during iteration" -- rarely, which is the
    worst frequency for a crash to have. The lock is never held across the
    fetch, so a slow upstream cannot block anything but bookkeeping.
    """

    def __init__(self, ttl: int, limit: int = 256, failure_ttl: Optional[int] = None):
        self.ttl = ttl
        self.failure_ttl = ttl if failure_ttl is None else failure_ttl
        self.limit = limit
        self._entries: dict = {}
        self._lock = threading.Lock()

    def get(self, key, fetch, *, now: float):
        with self._lock:
            hit = self._entries.get(key)
            if hit is not None:
                stored_at, value = hit
                ttl = self.ttl if value is not None else self.failure_ttl
                if now - stored_at < ttl:
                    return value

        # Outside the lock: two threads may fetch the same icon at once, which
        # costs one extra request and is cheaper than serialising every miss
        # behind a six-second timeout.
        value = fetch()

        with self._lock:
            if len(self._entries) >= self.limit:
                # Cheapest possible eviction: drop the oldest single entry.
                # This holds pictures, and the cost of being wrong about which
                # one to keep is one extra fetch.
                oldest = min(self._entries, key=lambda k: self._entries[k][0])
                self._entries.pop(oldest, None)
            self._entries[key] = (now, value)
        return value


def _fetch_vrchat_icon(file_id: str, version: str):
    """One group icon from VRChat, as (content_type, bytes), or None.

    None covers every failure, because they all render the same way -- no
    picture -- and the page has nothing useful to say about which it was.
    The log does.
    """
    url = VRCHAT_ICON_URL.format(file_id=file_id.lower(), version=version)
    body = None

    for _hop in range(VRCHAT_ICON_MAX_HOPS):
        try:
            response = requests.get(
                url,
                headers={
                    "User-Agent": VRCHAT_ICON_USER_AGENT,
                    "Accept": "image/*",
                },
                timeout=VRCHAT_ICON_TIMEOUT,
                stream=True,
                # Never on trust. Each hop is checked below before it is taken.
                allow_redirects=False,
            )
        except requests.RequestException as error:
            logger.warning("Could not fetch a VRChat group icon: %s", error)
            return None

        with response:
            if response.is_redirect or response.is_permanent_redirect:
                target = response.headers.get("Location") or ""
                # Relative Locations are resolved against the URL we just
                # fetched, which is one we already trust -- but the result is
                # checked like any other, so a resolution that lands somewhere
                # else still fails.
                url = requests.compat.urljoin(url, target)
                if not _vrchat_hop_allowed(url):
                    logger.warning(
                        "A VRChat group icon redirected off the allowed hosts; "
                        "not followed."
                    )
                    return None
                continue

            if response.status_code != 200:
                logger.warning(
                    "VRChat refused a group icon: HTTP %s", response.status_code
                )
                return None
            body = response.raw.read(VRCHAT_ICON_MAX_BYTES + 1, decode_content=True)
            break
    else:
        logger.warning("A VRChat group icon redirected more than %s times.",
                       VRCHAT_ICON_MAX_HOPS)
        return None

    if len(body) > VRCHAT_ICON_MAX_BYTES:
        logger.warning("A VRChat group icon exceeded %s bytes; dropped.",
                       VRCHAT_ICON_MAX_BYTES)
        return None
    content_type = _sniff_image(body)
    if content_type is None:
        logger.warning("VRChat served a group icon that is not an image.")
        return None
    return content_type, body


class _PriceCache:
    """The product's active prices, remembered briefly.

    Deliberately in-process, like _RateLimiter, and for the same reason: there
    is one container and a shared cache would be a new dependency to hold a
    copy of something Stripe already stores.

    Failure is NOT cached. A Stripe blip must not switch the plans off for the
    next five minutes -- the next request should try again, because the page it
    renders in the meantime is one that cannot sell anything.
    """

    def __init__(self, ttl: int):
        self.ttl = ttl
        self._value: tuple = ()
        self._fetched_at: float = 0.0

    def get(self, fetch, *, now: float):
        """Cached prices, or a fresh fetch. Propagates StripeAPIError."""
        if self._fetched_at and now - self._fetched_at < self.ttl:
            return self._value
        value = tuple(fetch())
        self._value = value
        self._fetched_at = now
        return value

    def clear(self) -> None:
        self._value = ()
        self._fetched_at = 0.0


class _RateLimiter:
    """A fixed-window counter, per key.

    Deliberately in-process and deliberately tiny. Cloudflare rate limiting
    sits in front of this, but the webhook path is precisely the one that has
    to be excepted from the managed challenge (A-25), so the edge protection on
    it is weaker than on everything else here — which is exactly why the origin
    keeps a budget of its own rather than trusting the one in front.
    """

    def __init__(self, limit: int, window: int):
        self.limit = limit
        self.window = window
        self._buckets: dict = {}

    def allow(self, key: str, *, now: float) -> bool:
        window = int(now // self.window)
        current, count = self._buckets.get(key, (window, 0))
        if current != window:
            current, count = window, 0
        if count >= self.limit:
            self._buckets[key] = (current, count)
            return False
        self._buckets[key] = (current, count + 1)
        # Bounded by the number of distinct keys, and there is exactly one.
        return True

# The sidebar's collapsed state. A UI preference and nothing else: it is not
# read by any authorisation decision, it names no guild, and forging it gets an
# attacker a narrower sidebar. Deliberately NOT `__Host-` prefixed -- that
# prefix belongs to the session cookie, and a second cookie wearing it would
# make the one that matters harder to pick out of a jar.
NAV_COOKIE = "vrcverify_nav"
# A year. The preference is trivial to re-set and there is nothing to expire.
NAV_COOKIE_MAX_AGE = 31536000

# The chosen theme (issue #123). Same class of thing as NAV_COOKIE and for the
# same reasons: a display preference, read by no authorisation decision, naming
# no guild, and worth forging only to change the colour of your own page. Not
# `__Host-` prefixed, so the session cookie stays the one that stands out.
#
# Deliberately NOT httponly, unlike NAV_COOKIE. Phase 4 lets the theme button
# write it from a script so the switch is instant, and a script cannot write a
# header the browser hides from it. With no `connect-src` in the CSP it cannot
# ask the server to do it either, so the choice is between a full navigation on
# every toggle and a cookie the page can write. Nothing reads it but the
# stylesheet, via the attribute theme_attr() puts on <html>.
THEME_COOKIE = "vrcverify_theme"
# A year, like NAV_COOKIE. The preference is one click to re-set and there is
# nothing about it worth expiring.
THEME_COOKIE_MAX_AGE = 31536000

# Which changelog entry this browser has already seen (issue #136). The same
# class of thing again -- a display preference forgeable only to change what
# your own bell looks like -- and the same reasoning for every attribute:
# no `__Host-` prefix, and not httponly, because prefs.js clears the dot on
# open by writing this directly. There is no `connect-src` in the CSP, so a
# script cannot ask the server to write it instead.
#
# It holds ONE id, not a set: the feed is ordered, so "the newest one you have
# seen" answers the only question the dot asks. `changelog.read_seen` validates
# it against the ids actually shipped, so a hand-edited value shows the dot
# once more rather than hiding entries this browser never saw.
SEEN_COOKIE = "vrcverify_seen"
SEEN_COOKIE_MAX_AGE = 31536000

# Which premium entries have been dismissed, per guild (#136 phase 4). Holds
# `guild:entry` pairs, bounded -- see MAX_DISMISSALS in changelog.py for why a
# ceiling is not optional on a cookie sent with every request.
#
# PER GUILD AND PER BROWSER, and only the first half of that is a design goal.
# An admin running four servers is pitched once per server, which is the
# property that matters. Dismissing on a laptop not dismissing on a desktop is
# the accepted cost: the dashboard holds no database credentials by design
# (site/privacy.html states that separation as a guarantee), so the only
# alternative is a bot-side table, two guild-scoped operations, two client
# methods and a bot deploy coupled to a dashboard feature -- a great deal of
# machinery to remember that somebody clicked a dismiss button. The failure
# mode is one card reappearing once on another device.
#
# Not httponly, like the two above, so a future enhancement can dismiss
# without a navigation. Nothing reads it but changelog.parse_dismissed, which
# drops every pair it does not recognise.
DISMISS_COOKIE = "vrcverify_dismissed"
DISMISS_COOKIE_MAX_AGE = 31536000

# What the cookie may say. Anything else is treated as if it were absent, which
# is what stops a hand-edited value reaching a `data-` attribute unchecked.
THEMES = frozenset({"dark", "light", "system"})

# No cookie means dark. That is the product decision from #123 -- the people
# using this page have Discord open on the other monitor, and Discord is dark
# -- and it is why "absent" and "dark" resolve to the same render rather than
# being told apart. A first-time visitor and someone who explicitly chose dark
# want the same thing, so nothing here needs to know which they are.
THEME_DEFAULT = "dark"

# Where the hamburger may send you back to. An endpoint name, never a URL from
# the request -- a form field carrying a path is an open redirect waiting for
# someone to notice, and this form exists to toggle a cookie.
NAV_RETURN_ENDPOINTS = {
    "index": (),
    # The changelog page (#136 phase 3). Global rather than per-guild, so it
    # takes no values -- the theme picker and the bell's own "Mark all as
    # read" both post from this page and both have to land back on it.
    "whats_new": (),
    "guild_overview": ("guild_id",),
    "guild_settings": ("guild_id",),
    "guild_subscription": ("guild_id",),
}

# What each settings sub-page actually reads from the bot, beyond the settings
# themselves -- which every one of them needs and none of them may do without.
#
# This table is also the list of slugs the route will serve, so a group that is
# not here is a 404 rather than a page that renders nothing. It is keyed by the
# slugs in `settings_view.SETTINGS_SLUGS` plus Activity, and a test pins the
# two against each other: a group with no entry would be unreachable, and an
# entry with no group would be a URL the sub-nav can never offer.
#
# ACTIVITY NEEDS ROLES AND CHANNELS, not just the audit trail. #140 says
# "Activity needs `audit` alone", and that is wrong: `build_audit` resolves the
# ids inside each entry into names, so without them a history of role changes
# reads as a list of numbers. It still needs them far less often than every
# group's page did.
SETTINGS_GROUP_READS = {
    "verification": ("roles",),
    "after-verifying": (),
    "panel": ("channels", "panel"),
    "vrchat-group": (),
    "logging": ("channels",),
    "activity": ("roles", "channels", "audit"),
}

# The `__Host-` prefix is enforced by the browser: it only accepts the cookie if
# it is Secure, has no Domain, and is Path=/. That makes it impossible for a
# sibling subdomain to shadow this cookie with a Domain-scoped one of the same
# name -- which matters more now that a session can write settings, since being
# tossed into someone else's session means editing their server believing it is
# yours.
SESSION_COOKIE = "__Host-vrcverify_session"

# Everything is same-origin and self-hosted, so the policy can be close to
# nothing. The one external origin is Discord's icon CDN: guild icons are
# served from there and proxying them would mean the dashboard fetching
# arbitrary URLs on a user's behalf, which is a worse trade than allowing one
# well-known image host. Note it is img-src only -- no script or style may come
# from anywhere but here.
CSP = (
    "default-src 'none'; "
    "script-src 'self'; "
    "style-src 'self'; "
    # Our own origin only, for the one vendored WOFF2 in static/fonts. This
    # directive is here because without it `default-src 'none'` blocks every
    # font -- including a self-hosted one, which is a surprising way to spend
    # an afternoon. It deliberately does not name a font CDN: the file is in
    # the image, so a third party can neither see who is loading the dashboard
    # nor break it by going down.
    "font-src 'self'; "
    # VRChat is deliberately NOT here. Group icons are fetched by this app and
    # served from its own origin -- see the /vrchat-icon route -- which is what
    # lets the policy stay this narrow, and means an admin's browser never
    # makes a request to VRChat at all.
    "img-src 'self' https://cdn.discordapp.com; "
    # Stripe's two hosted pages are named here because `form-action` governs
    # where a form submission may end up INCLUDING AFTER A REDIRECT -- it is
    # not only about the action attribute. The checkout and portal routes both
    # answer a POST with a 303 to Stripe, so with `'self'` alone the browser
    # sends the request, gets the redirect, and silently refuses to follow it.
    # No navigation, no error page, nothing in the server log that looks wrong:
    # the button simply does nothing, and the only evidence is a CSP violation
    # in the console. That was the "Subscribe does nothing" bug of 2026-08-15.
    #
    # This is a much narrower relaxation than it looks. It permits navigation
    # to those two origins as the result of submitting a form, and nothing
    # else: no script, no frame, no style, no image may come from Stripe, so
    # card data still never touches this infrastructure.
    "form-action 'self' https://checkout.stripe.com https://billing.stripe.com; "
    "base-uri 'none'; "
    "frame-ancestors 'none'"
)


def create_app(
    config: Optional[DashboardConfig] = None,
    *,
    store: Optional[SessionStore] = None,
    client: Optional[BotAPIClient] = None,
    stripe: Optional[StripeClient] = None,
) -> Flask:
    config = config or DashboardConfig.from_env()
    app = Flask(__name__)
    app.config["DASHBOARD"] = config
    app.secret_key = config.secret_key

    # Trust exactly one hop, for exactly the scheme and host. cloudflared is
    # the only thing that can reach this app -- it has no published port -- so
    # one hop is the whole chain.
    app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1, x_for=1)

    # A ceiling on every request body, enforced by Werkzeug before a handler
    # sees anything. This is not belt-and-braces on top of the webhook's own
    # size check -- it is the half that actually holds: a request with
    # `Transfer-Encoding: chunked` carries no Content-Length, so a handler
    # checking `request.content_length` first sees None and then reads the
    # whole thing into memory to measure it. Without this the one public,
    # unauthenticated route on this host will buffer whatever it is sent.
    #
    # Nothing here uploads anything. The largest honest body is a settings form
    # or a subscription event of a few KB.
    app.config["MAX_CONTENT_LENGTH"] = MAX_REQUEST_BYTES

    app.config["STORE"] = store or SessionStore(
        config.session_db_path, config.session_max_age
    )
    app.config["BOT_API"] = client or BotAPIClient(
        config.bot_api_url,
        client_cert=config.bot_api_client_cert,
        client_key=config.bot_api_client_key,
        ca_bundle=config.bot_api_ca,
        signing_key=config.bot_api_signing_key,
        timeout=config.request_timeout,
    )

    # Only built when Stripe is switched on, so a dashboard with the kill
    # switch off holds no Stripe client and no secret key -- not even an
    # unused one.
    app.config["STRIPE"] = stripe or (
        StripeClient(config.stripe_secret_key, timeout=config.request_timeout)
        if config.stripe_enabled
        else None
    )
    # Its own budget, deliberately not shared with anything else here. The
    # webhook is the one route a third party is meant to reach, and it is the
    # one route that must keep working when the session-authenticated ones are
    # under load -- a subscription must not be lost because somebody is
    # hammering /login.
    app.config["STRIPE_RATE"] = _RateLimiter(
        STRIPE_WEBHOOK_RATE_LIMIT, STRIPE_WEBHOOK_RATE_WINDOW
    )
    app.config["STRIPE_PRICES"] = _PriceCache(STRIPE_PRICE_CACHE_TTL)
    app.config["ICON_CACHE"] = _IconCache(
        VRCHAT_ICON_TTL, failure_ttl=VRCHAT_ICON_FAILURE_TTL
    )

    # Python's mimetypes table does not know WOFF2 on every platform, and Flask
    # asks it. Served as application/octet-stream the font still works in
    # current browsers, but it is wrong, and "wrong but tolerated" is the kind
    # of thing a future proxy stops tolerating.
    mimetypes.add_type("font/woff2", ".woff2")

    _register_assets(app)
    _register_routes(app)
    _register_hooks(app)
    return app


def _register_assets(app: Flask) -> None:
    """Give every static URL a content digest, so it can be cached forever.

    The pair of decisions here only works together. `harden` marks static
    responses `immutable` for a year, which would be reckless on a bare
    `/static/style.css` -- a deploy would change the file and every admin would
    keep the old one until they cleared their cache. Digesting the content into
    the query string means a changed file is a changed URL, so the stale copy
    is not stale, it is simply never asked for again.

    Digests are computed once at startup rather than per request: the files
    cannot change under a running container, and hashing a 48KB font on every
    page render to discover it is the same font would be a strange way to spend
    the saving.
    """
    digests: dict = {}

    def digest(filename: str) -> str:
        if filename not in digests:
            path = os.path.join(app.static_folder or "", filename)
            try:
                with open(path, "rb") as handle:
                    digests[filename] = hashlib.blake2b(
                        handle.read(), digest_size=6
                    ).hexdigest()
            except OSError:
                # A missing asset is a broken page either way; returning no
                # version at least keeps the URL usable and the 404 legible.
                logger.warning("static asset %s could not be read", filename)
                digests[filename] = ""
        return digests[filename]

    @app.template_global()
    def asset(filename: str) -> str:
        """`asset('style.css')` -> `/static/style.css?v=<digest>`."""
        url = url_for("static", filename=filename)
        version = digest(filename)
        return f"{url}?v={version}" if version else url

    @app.template_global()
    def theme_attr() -> Markup:
        """The `data-theme` attribute for `<html>`, or nothing at all.

        A template global rather than a variable passed to `render_template`,
        and that is the whole point. There are thirteen render calls in this
        module and one of them -- the sign-in page -- passes no arguments
        whatsoever, because before a session exists there is nothing to pass.
        Threading a `theme` through all thirteen would mean every page added
        after this one has to remember to, and the one that forgets renders
        with no theme at all. This way `base.html` asks for it and no call site
        can leave it out.

        **"System" is the absence of the attribute, not a value of it.** The
        stylesheet's third block is `:root:not([data-theme])` inside the dark
        media query, so leaving the attribute off is what hands the decision
        back to the OS. Emitting `data-theme="system"` would match none of the
        three blocks and quietly pin everyone to the light default.

        Returned as Markup because it is markup -- an escaped ` data-theme=...`
        would land in the page as text. Nothing from the request reaches it:
        `_theme()` has already reduced the cookie to one of three known words,
        so there is no path from a header into this attribute.
        """
        chosen = _theme()
        if chosen == "system":
            return Markup("")
        return Markup(' data-theme="%s"') % chosen

    @app.template_global()
    def current_theme() -> str:
        """Which theme is in force, as a plain word.

        `theme_attr()` above answers what `<html>` should carry, which is not
        the same question: "system" renders as no attribute at all, so the
        attribute cannot tell the picker which option to mark as current. One
        of the three words, always -- including "system".
        """
        return _theme()

    @app.template_global()
    def support_invite():
        """The VRCVerify Discord invite, or None if this host has none (#138).

        A template global for the same reason `updates()` is one: the bell
        lives in `base.html`, which backs every page, so threading an argument
        through every `render_template` call would mean the next page somebody
        adds silently renders without it.

        Scheme-checked, and the check is not ceremony. `discord.gg/xxxx` with
        no scheme is the shape people paste, and a browser resolves a
        schemeless `href` as a path RELATIVE TO THIS SITE -- so the row would
        render as an ordinary-looking link that 404s on our own domain rather
        than reaching Discord. Refusing it renders no row at all, which is the
        failure everything else here degrades to anyway.
        """
        cfg = app.config.get("DASHBOARD")
        raw = (getattr(cfg, "support_invite_url", "") or "").strip()
        if not raw or not raw.startswith(("https://", "http://")):
            return None
        return raw

    @app.template_global()
    def updates():
        """What the header's bell should render, if anything.

        A template global for exactly the reason `theme_attr()` is one: the
        bell is in `base.html`, `base.html` backs every page, and threading a
        `bell` argument through every `render_template` call in this module
        would mean the next page somebody adds renders without one. The
        template asks; no call site can forget.

        All the deciding happens in `changelog.py`, which is pure. This
        function's whole job is turning one cookie into an argument.

        `g.changelog_seen` beats the cookie, and it exists for exactly one
        case: the changelog page CLEARS the dot in the same response it
        renders. The cookie it sets is not readable until the next request, so
        without this the bell would sit there announcing unread entries at the
        top of the very page listing them in full -- which is the one place
        the claim is obviously false.
        """
        seen = getattr(g, "changelog_seen", None)
        if seen is None:
            seen = changelog.read_seen(request.cookies.get(SEEN_COOKIE))
        return changelog.build_bell(seen)


# -------------------------------------------------------------------
# Request plumbing
# -------------------------------------------------------------------
def _store() -> SessionStore:
    from flask import current_app

    return current_app.config["STORE"]


def _config() -> DashboardConfig:
    from flask import current_app

    return current_app.config["DASHBOARD"]


def _bot_api() -> BotAPIClient:
    from flask import current_app

    return current_app.config["BOT_API"]


def _offered_plans():
    """The plans currently for sale, and whether we could find out.

    Returns `(plans, unavailable)`. The two are not redundant: an empty tuple
    with `unavailable` false means Stripe answered and this product sells
    nothing, which is a true statement the page may render. Empty with
    `unavailable` true means Stripe did not answer, and the page must say so
    rather than imply there is nothing to buy.

    Never raises. A subscription page that 500s because a third party is slow
    is worse than one that cannot currently take a card -- the rest of it
    (whether the server is premium, when it renews, the Discord route) comes
    from the bot and is still perfectly true.
    """
    from flask import current_app

    config = _config()
    if not config.stripe_enabled:
        # Nothing to fetch and nothing to apologise for: the kill switch being
        # off is not an outage.
        return (), False
    try:
        prices = current_app.config["STRIPE_PRICES"].get(
            lambda: _stripe().list_prices(config.stripe_product_id),
            now=time.time(),
        )
    except StripeAPIError as error:
        logger.warning("could not list plans: %s", error)
        return (), True
    return subscription_view.plans_from_prices(prices), False


def _stripe() -> StripeClient:
    """Only ever reached from routes that already checked stripe_enabled."""
    from flask import current_app

    return current_app.config["STRIPE"]


def _register_hooks(app: Flask) -> None:
    @app.before_request
    def load_session():
        g.session = _store().load(request.cookies.get(SESSION_COOKIE))

    @app.after_request
    def harden(response):
        response.headers["Content-Security-Policy"] = CSP
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        # The dashboard URL identifies a server admin. Sending it to Discord's
        # CDN with every icon request would leak which guild is being looked at.
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cross-Origin-Opener-Policy"] = "same-origin"
        # Authenticated pages must not sit in a shared cache. `no-store` is
        # therefore the default and the three branches below are the named
        # exceptions -- none of which is an authenticated page. Static files
        # are the first, and a real one: `no-store` on everything meant
        # the 48KB font and the stylesheet were re-fetched on every single page
        # view, by every admin, forever. They carry nothing about a session --
        # they are the same bytes for a signed-out stranger -- and their URLs
        # carry a content digest, so a deploy changes the URL and a stale copy
        # is unreachable rather than merely unwanted.
        if request.endpoint == "static":
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        elif request.endpoint == "pricing":
            # PRIVATE, NOT PUBLIC, and the issue asking for this assumed
            # otherwise -- it reasoned that /pricing "is the same bytes for
            # every visitor and carries nothing about a session". It is not.
            # `theme_attr()` stamps the chosen theme into <html> from the
            # THEME_COOKIE, server-side and by design: #123 renders the theme
            # into the markup precisely so a cold load never flashes the wrong
            # one and so the control still works with JavaScript off.
            #
            # (NAV_COOKIE does not vary this page, and that is worth stating
            # because it looks like it should: `nav_collapsed` comes from
            # `_page_context()`, which only guild pages call, so it is simply
            # undefined here. The theme cookie alone is enough.)
            #
            # So the response varies by cookie, and a shared cache that ignored
            # that would hand one reader's light page to the next reader who
            # chose dark. `Vary: Cookie` is the nominal fix and a bad one here:
            # Cloudflare fronts this origin and does not vary on arbitrary
            # request headers, so the header would buy a correctness guarantee
            # this deployment does not actually get.
            #
            # `private` scopes the cache to the one browser whose cookie
            # produced the page, which is exactly the scope that cookie has. A
            # shared cache is off the table until the theme stops being
            # server-rendered, and it should not.
            #
            # The number is short on purpose. Prices are already up to
            # STRIPE_PRICE_CACHE_TTL stale before they reach this handler, and
            # a browser cache adds to that window rather than overlapping it --
            # so a minute buys the back button and a reload while adding a
            # fifth to a staleness budget that already exists, where matching
            # the 300 would double it.
            response.headers["Cache-Control"] = f"private, max-age={PRICING_PAGE_CACHE_TTL}"
        elif request.endpoint == "vrchat_icon":
            # Keeps what the route set: private, so no shared cache holds it,
            # but cacheable, because re-fetching a group icon from VRChat every
            # time an admin reloads the settings page turns one page view into
            # upstream traffic. It carries nothing about a session -- it is a
            # picture of a group anyone can look at.
            pass
        else:
            response.headers["Cache-Control"] = "no-store"
        # Cloudflare terminates TLS, but HSTS is the origin's statement, and a
        # future non-tunnel deployment should still carry it.
        response.headers["Strict-Transport-Security"] = (
            "max-age=31536000; includeSubDomains"
        )
        return response


def _set_session_cookie(response, sid: str, max_age: int):
    response.set_cookie(
        SESSION_COOKIE,
        sid,
        max_age=max_age,
        # Not readable from JavaScript, only sent over TLS, and not attached to
        # cross-site requests -- which is also what makes the logout form's
        # CSRF token a second line rather than the only one.
        httponly=True,
        secure=True,
        samesite="Lax",
        path="/",
    )
    return response


def _require_login():
    session = getattr(g, "session", None)
    if session is None or not session.authenticated:
        return None
    return session


# -------------------------------------------------------------------
# Routes
# -------------------------------------------------------------------
def _register_routes(app: Flask) -> None:
    @app.get("/healthz")
    def healthz():
        """Liveness only. Says nothing about sessions, guilds or the bot."""
        return {"ok": True}

    if app.config["DASHBOARD"].stripe_enabled:
        _register_stripe_webhook(app)

    @app.get("/")
    def index():
        session = _require_login()
        if session is None:
            return render_template("login.html")

        config = _config()
        # Display filter only. `admin_hint` came from Discord at authorisation
        # time and is already stale; it decides which tiles to draw, never what
        # anyone may do.
        candidates = [g_ for g_ in (session.guilds or []) if g_.get("admin_hint")]

        try:
            installed = _bot_api().admin_guild_ids(
                int(session.discord_id), [g_["id"] for g_ in candidates]
            )
            reachable = True
        except BotAPIError as error:
            # The picker still renders, with everything shown as un-installed
            # and a banner explaining why. Better than a blank page that looks
            # like the user has no servers.
            logger.warning("bot API unreachable while rendering the picker: %s", error)
            installed = set()
            reachable = False

        servers = [
            {
                "id": g_["id"],
                "name": g_["name"],
                "icon_url": oauth.icon_url(g_),
                "installed": g_["id"] in installed,
                "invite_url": _invite_url(config.discord_client_id, g_["id"]),
            }
            for g_ in candidates
        ]
        # Installed first, then alphabetical -- the ones you can actually
        # configure are the reason you came.
        servers.sort(key=lambda s: (not s["installed"], s["name"].lower()))

        return render_template(
            "picker.html",
            servers=servers,
            reachable=reachable,
            csrf_token=session.csrf_token,
        )

    @app.get("/pricing")
    def pricing():
        """What Premium costs, readable without a Discord account (#188).

        THE FIRST DELIBERATELY SIGNED-OUT PAGE IN THIS APP, and the reason it
        is here rather than on the apex site is worth keeping in view: prices
        come from Stripe on the render that shows them. A figure typed into a
        static file deploys on a pipeline that knows nothing about Stripe, so
        it would keep quoting the old number after a price change, silently,
        on the same domain the Terms live on. `subscription.html` states the
        rule -- no amount is ever computed, because a second copy of a price on
        a page about money is a second thing to be wrong.

        No `_require_login()`, and that is the whole point: a prospect who has
        not installed the bot is exactly who this page is for. It reaches
        nothing per-guild and nothing per-user -- `_offered_plans()` takes no
        session and no guild, and there is no bot call on this path at all.

        No `section`, so `base.html` renders the sidebar-less layout the sign-in
        page and the picker use. No `csrf_token` either, so the bell, the
        account menu and the theme picker all stay gated off, the same as the
        sign-in page.
        """
        plans, plans_unavailable = _offered_plans()
        config = _config()
        page = subscription_view.build_public_pricing(
            plans,
            plans_unavailable=plans_unavailable,
            stripe_configured=config.stripe_enabled,
        )
        # No guild: a stranger reading a price has no server in context, so
        # this is the generic install link rather than the picker's deep link.
        return render_template(
            "pricing.html",
            page=page,
            install_url=_invite_url(config.discord_client_id),
        )

    @app.get("/login")
    def login():
        state = oauth.new_state()
        session = _store().begin_login(state)
        config = _config()
        response = redirect(
            oauth.authorize_url(
                config.discord_client_id, config.oauth_redirect_uri, state
            )
        )
        # 600s: the pre-auth row's own lifetime. An abandoned login should not
        # leave a cookie behind for hours.
        return _set_session_cookie(response, session.sid, 600)

    @app.get("/callback")
    def callback():
        config = _config()
        store = _store()
        pending = store.load(request.cookies.get(SESSION_COOKIE))

        error = request.args.get("error")
        if error:
            # User clicked Cancel, or Discord refused. Not an error condition
            # worth a stack trace.
            return render_template("error.html", message="Authorisation was declined."), 400

        code = request.args.get("code")
        state = request.args.get("state")
        if pending is None or not pending.oauth_state or not code or not state:
            return render_template("error.html", message="That login link has expired. Please try again."), 400

        # The check that makes CSRF against the login flow impossible: the
        # state came from us, in this browser, for this attempt.
        if not _same_secret(pending.oauth_state, state):
            logger.warning("OAuth state mismatch; refusing the callback.")
            store.destroy(pending.sid)
            return render_template("error.html", message="That login could not be verified. Please try again."), 400

        try:
            discord_id, guilds = oauth.login(
                code,
                client_id=config.discord_client_id,
                client_secret=config.discord_client_secret,
                redirect_uri=config.oauth_redirect_uri,
                timeout=config.request_timeout,
            )
        except oauth.OAuthError as failure:
            logger.warning("OAuth login failed: %s", failure)
            store.destroy(pending.sid)
            return render_template("error.html", message="Discord could not complete the login. Please try again."), 502

        # New session id at the moment privilege is granted; the pre-auth row
        # is deleted. See SessionStore.complete_login.
        session = store.complete_login(pending.sid, discord_id, guilds)
        logger.info("dashboard login actor=%s guilds=%d", discord_id, len(guilds))
        return _set_session_cookie(
            redirect(url_for("index")), session.sid, config.session_max_age
        )

    @app.get("/guild/<int:guild_id>")
    def guild_overview(guild_id: int):
        """Where you land after picking a server: how it's doing, in numbers.

        Authorised exactly like the settings page, and for the same reason --
        the bot re-checks Administrator before answering. This page reports
        aggregates only; there is no per-member data behind it to leak, because
        none is stored.
        """
        session = _require_login()
        if session is None:
            return redirect(url_for("index"))

        actor = int(session.discord_id)
        try:
            overview = _bot_api().overview(actor, guild_id)
        except BotAPIError as error:
            return _guild_page_unavailable(error, guild_id, session, "overview")

        premium = overview.get("premium") or {}
        # #135 phase 4 left `changelog_entry` as an explicit stub with no
        # caller. This is the caller. Everything about WHICH entry, and how it
        # is worded for this server's plan, is decided in changelog.py; the
        # ranking is decided in build_next_step. Neither knows about the
        # other, and this line is the only place they meet.
        changelog_entry = changelog.build_premium_card(
            guild_id,
            dismissed=_dismissed(),
            premium=bool(premium.get("premium")),
            grandfathered=bool(premium.get("grandfathered")),
        )

        return render_template(
            "overview.html",
            tiles=overview_view.build_tiles(overview),
            chart=overview_view.build_chart(overview),
            next_step=overview_view.build_next_step(overview, changelog_entry),
            setup=overview_view.build_setup(overview),
            premium=premium,
            **_guild_chrome(session, guild_id, "overview"),
        )

    @app.get("/guild/<int:guild_id>/subscription")
    def guild_subscription(guild_id: int):
        """One server's plan, and the two ways to buy it.

        The bot decides everything this page states. It reports whether the
        tier is enforced, whether the server is premium, whether that came from
        Discord, and what the mirrored card subscription says -- and this route
        renders that. It never works out for itself whether a server has paid,
        because a second opinion about the premium gate is a second thing that
        can disagree with the one doing the enforcing.

        A failed read is an apology, never "not subscribed". That rule exists
        everywhere on this site and bites hardest here: "not subscribed" beside
        a Buy button is how a paying customer buys a second subscription.
        """
        session = _require_login()
        if session is None:
            return redirect(url_for("index"))

        config = _config()
        try:
            settings = _bot_api().settings(int(session.discord_id), guild_id)
        except BotAPIError as error:
            return _guild_page_unavailable(error, guild_id, session, "subscription")

        # Stripe bounces the browser back here after a completed checkout.
        # A hint, never evidence of payment -- the webhook is what makes a
        # subscription real and may not have landed yet -- but the page must
        # not offer to sell again while that is outstanding.
        just_bought = bool(request.args.get("bought"))
        plans, plans_unavailable = _offered_plans()
        page = subscription_view.build(
            settings,
            application_id=config.discord_client_id,
            plans=plans,
            plans_unavailable=plans_unavailable,
            stripe_configured=config.stripe_enabled,
            just_bought=just_bought,
        )
        notice, notice_kind = _subscription_notice(session)
        if notice is None and just_bought and page.state == "pending":
            notice, notice_kind = SUBSCRIPTION_NOTICES["bought"]
        # csrf_token comes from _guild_chrome, like every other page here.
        return render_template(
            "subscription.html",
            page=page,
            notice=notice,
            notice_kind=notice_kind,
            **_guild_chrome(session, guild_id, "subscription"),
        )

    @app.post("/guild/<int:guild_id>/subscription/checkout")
    def subscription_checkout(guild_id: int):
        """Start a hosted Checkout and hand the browser to Stripe.

        THE PRICE ID IS NEVER TAKEN FROM THE FORM. The browser may name a plan
        slug and nothing else; the id is looked up server-side. A form-supplied
        price would let anyone check out against any price on the account,
        including a $0 one created while testing, and it is the single most
        likely way to get this endpoint wrong.

        Administrator is re-checked by the bot on the settings read below --
        the session proves who is asking, never what they may do -- and the
        same read is what tells us whether this server should be offered a card
        at all. Buying twice is prevented here as well as in the page: a POST
        crafted by hand must not be able to reach checkout for a server that
        already subscribes.
        """
        session = _require_login()
        if session is None:
            return redirect(url_for("index"))
        if not _csrf_ok(session):
            abort(400)

        config = _config()
        if not config.stripe_enabled:
            abort(404)

        # The submitted price is matched against Stripe's own list of this
        # product's ACTIVE prices, never trusted as given. The guarantee is the
        # one the static table used to provide -- no price the server has not
        # authorised, so no checking out against a $0 price made while testing
        # -- but enforced against Stripe's live answer, which also means a
        # price archived a minute ago stops being sellable without a deploy.
        #
        # A failed fetch refuses the purchase rather than falling back to the
        # submitted id. "We could not check" must never resolve to "allow".
        submitted = (request.form.get("price_id") or "").strip()
        plans, plans_unavailable = _offered_plans()
        if plans_unavailable:
            logger.warning("checkout refused: could not read plans from Stripe")
            return _subscription_redirect(session, guild_id, "error:stripe")
        plan = next((p for p in plans if p.price_id == submitted), None)
        if plan is None:
            logger.warning("checkout refused: unknown price %r", submitted)
            return _subscription_redirect(session, guild_id, "error:plan")
        price_id = plan.price_id

        try:
            settings = _bot_api().settings(int(session.discord_id), guild_id)
        except BotAPIError as error:
            return _guild_page_unavailable(error, guild_id, session, "subscription")

        page = subscription_view.build(
            settings,
            application_id=config.discord_client_id,
            plans=plans,
            stripe_configured=True,
        )
        if not page.offers_card:
            # The page would not have shown a Buy button in this state, so a
            # request that reached here was not made by clicking one.
            logger.warning(
                "checkout refused for guild %s: page state is %s", guild_id, page.state
            )
            return _subscription_redirect(session, guild_id, "error:already")

        base = url_for("guild_subscription", guild_id=guild_id, _external=True)
        try:
            checkout_url = _stripe().create_checkout_session(
                price_id=price_id,
                guild_id=str(guild_id),
                actor_discord_id=str(session.discord_id),
                success_url=f"{base}?bought=1",
                cancel_url=base,
                # From the price's own metadata AND this server's eligibility,
                # via the same call the card rendered from. Two things are
                # being prevented, and they are not the same thing:
                #
                # the length has to match what the buyer was shown, or a page
                # advertises 14 days while Stripe grants 7;
                #
                # and the *entitlement to a trial at all* has to be re-checked
                # here, because rendering a card without one is not a gate. A
                # POST is not a click. Anyone who has ever completed checkout
                # has seen this form, and a returning server replaying it with
                # its own price id would otherwise take a second free month,
                # once per cancellation, forever. The bot decides this and the
                # answer arrives in the settings payload read above.
                trial_days=page.trial_days_for(plan),
            )
        except StripeAPIError as error:
            # The page apologises and offers the Discord path. It does not
            # pretend to have created a session.
            logger.warning("could not create a checkout session: %s", error)
            return _subscription_redirect(session, guild_id, "error:stripe")

        # 303 so the browser re-issues as GET. The CSP is untouched: the
        # browser leaves rather than loading anything from Stripe here.
        return redirect(checkout_url, code=303)

    @app.post("/guild/<int:guild_id>/subscription/portal")
    def subscription_portal(guild_id: int):
        """Hand the admin to Stripe's billing portal.

        Cancelling, switching plan and updating a card all happen on Stripe's
        domain, and none of them is reimplemented here. That is not
        convenience: every one is an action on somebody's money, and this is
        the process the threat model assumes will be compromised.
        """
        session = _require_login()
        if session is None:
            return redirect(url_for("index"))
        if not _csrf_ok(session):
            abort(400)

        config = _config()
        if not config.stripe_enabled:
            abort(404)

        try:
            settings = _bot_api().settings(int(session.discord_id), guild_id)
        except BotAPIError as error:
            return _guild_page_unavailable(error, guild_id, session, "subscription")

        # The customer id comes from the bot's mirrored row, never from the
        # form. A portal session names a customer, and a customer id a browser
        # could choose would be a portal into somebody else's billing.
        customer_id = (settings.get("stripe") or {}).get("customer_id")
        if not customer_id:
            return _subscription_redirect(session, guild_id, "error:portal")
        # Checked here as well as where the URL is built. It arrives over mTLS
        # from the bot, so this should never fire -- which is the argument for
        # why it is cheap, not for leaving one of the two checks out.
        if not stripe_events.valid_object_id(str(customer_id)):
            logger.error(
                "guild %s has a malformed Stripe customer id on file", guild_id
            )
            return _subscription_redirect(session, guild_id, "error:portal")

        try:
            portal_url = _stripe().create_portal_session(
                customer_id=str(customer_id),
                return_url=url_for(
                    "guild_subscription", guild_id=guild_id, _external=True
                ),
                configuration=config.stripe_portal_configuration_id or None,
            )
        except StripeAPIError as error:
            logger.warning("could not create a portal session: %s", error)
            return _subscription_redirect(session, guild_id, "error:stripe")

        return redirect(portal_url, code=303)

    @app.get("/guild/<int:guild_id>/settings")
    @app.get("/guild/<int:guild_id>/settings/<group>")
    def guild_settings(guild_id: int, group: Optional[str] = None):
        """One group of one server's settings, read-only.

        Authority is the bot's, on every one of the calls below: each mints its
        own token and the bot re-checks Administrator before answering. The
        session is what proves who is asking, never what they may see -- so a
        stale OAuth guild list cannot widen access, and a demotion in Discord
        takes effect on the next page load rather than at session expiry.

        TWO RULES, ONE VIEW. `/settings` with no group is a hard external
        contract, not a courtesy: the bot posts that URL as Discord link
        buttons, and a link button in message history cannot be edited after
        the fact. Every one already sent has to keep working, so the bare URL
        redirects to the first group rather than 404ing or growing an index
        page nobody asked for.

        Keeping both rules on one endpoint is also what stops this split being
        a flag day -- `url_for("guild_settings", guild_id=...)` still resolves
        everywhere it already appears, and lands the reader on a real page via
        one redirect, so links move deliberately rather than all at once.
        """
        session = _require_login()
        if session is None:
            return redirect(url_for("index"))

        if group is None:
            return redirect(_settings_url(guild_id, settings_view.SETTINGS_DEFAULT_SLUG))

        # A slug nobody serves is a 404, not a quiet bounce to the first group.
        # The sub-nav cannot produce one -- it is built from this same table --
        # so anything arriving here was typed or guessed, and telling a reader
        # their URL is wrong beats silently showing them a different page.
        if group not in SETTINGS_GROUP_READS:
            abort(404)

        actor = int(session.discord_id)
        try:
            settings = _bot_api().settings(actor, guild_id)
        except BotAPIError as error:
            return _guild_page_unavailable(error, guild_id, session, "settings")

        # Only what this group renders. The single page had to read everything
        # because it showed everything; a page per group can ask for less, so
        # splitting Settings costs fewer bot calls per view rather than more,
        # despite more views per visit.
        #
        # Best-effort on purpose: an unresolved id renders as an id, which is
        # less useful but still true, and that is a better page than an error
        # over a secondary read. The settings themselves are not optional --
        # rendering defaults an admin never chose would be a lie the save path
        # could persist.
        needs = SETTINGS_GROUP_READS[group]
        roles = channels = panel = audit = None
        if "roles" in needs:
            roles = _optional_read(
                lambda: _bot_api().roles(actor, guild_id), "roles", guild_id
            )
        if "channels" in needs:
            channels = _optional_read(
                lambda: _bot_api().channels(actor, guild_id), "channels", guild_id
            )
        if "panel" in needs:
            panel = _optional_read(
                lambda: _bot_api().panel(actor, guild_id), "panel", guild_id
            )
        if "audit" in needs:
            audit = _optional_read(
                lambda: _bot_api().audit(actor, guild_id), "audit", guild_id
            )

        # Read once and cleared, so a reload does not repeat it.
        notice = _store().take_notice(session.sid)

        # True when everything THIS page needed came back, which is not the
        # same question the single page asked. A group with no ids to resolve
        # has nothing to warn about and must not inherit a warning from the
        # reads it never made.
        names_resolved = ("roles" not in needs or roles is not None) and (
            "channels" not in needs or channels is not None
        )

        if group == settings_view.ACTIVITY_SLUG:
            return render_template(
                "activity.html",
                audit=settings_view.build_audit(audit, roles, channels),
                # The shared header says what this server is paying for, so
                # this page needs it too -- one sentence, one place, rather
                # than a second copy that can disagree with Settings'.
                premium=settings.get("premium") or {},
                names_resolved=names_resolved,
                **_guild_chrome(session, guild_id, "settings", group),
            )

        groups = settings_view.build_groups(settings, roles, channels, panel)
        current = next(one for one in groups if one["slug"] == group)

        return render_template(
            "settings.html",
            # A list of one. The template's loop body is unchanged by the
            # split, which is why the field renderer is not duplicated five
            # times -- the page is the same page, given less.
            groups=[current],
            premium=settings.get("premium") or {},
            upgrade=settings_view.build_upgrade(
                settings, _config().discord_client_id
            ),
            names_resolved=names_resolved,
            auto_verify_column_present=settings.get("auto_verify_column_present", True),
            saved=notice == "saved",
            # Deliberately not "checked": the answer arrives over a queue and
            # is not in this response. Saying it succeeded would be a claim
            # this page cannot make yet.
            group_check=notice == "group_check",
            panel_result=PANEL_RESULTS.get(_notice_arg(notice, "panel")),
            panel_stale=notice == "stale",
            save_error=(
                _save_error_message(_notice_arg(notice, "error"))
                or _panel_error_message(_notice_arg(notice, "panel_error"))
            ),
            **_guild_chrome(session, guild_id, "settings", group),
        )

    @app.get("/updates")
    def whats_new():
        """Everything that shipped, in one list.

        Deliberately NOT in `SECTIONS`. Every entry there takes a `guild_id`
        and is a view of one server; this is global, and putting it in the
        sidebar would make it look like a property of whichever server you
        happened to be looking at. It is reached from the bell and from the
        footer, which are the two places that are also global.

        Signed-in only. The public version is #137's job on the apex site,
        which is why the model carries a `public` flag -- an entry here may
        address the admin of a server, and a page a stranger can read may not.

        THIS GET WRITES A COOKIE, which is worth defending rather than
        leaving to be found. Normally a GET that changes something is a
        mistake; what changes here is the record of what this browser has
        been shown, and being shown the whole list is exactly what this GET
        did. It is the same class of thing as the theme cookie: no session
        state, no bot call, nothing to corrupt, and the entire consequence of
        a forged navigation is that somebody's own unread dot goes out.

        It also matters most for the reader who has no JavaScript. prefs.js
        clears the dot when the panel opens; without it, following "See all
        updates" from that panel would leave the dot lit over a list the
        reader has just read in full.
        """
        session = _require_login()
        if session is None:
            return redirect(url_for("index"))

        # Before the render, not after: `updates()` reads this while building
        # the header's bell, and the cookie set below is not visible until the
        # next request. See that function.
        newest = changelog.newest_id()
        g.changelog_seen = newest

        response = make_response(
            render_template(
                "changelog.html",
                entries=changelog.ENTRIES,
                csrf_token=session.csrf_token,
                nav_return_to="whats_new",
            )
        )
        if newest is not None:
            response.set_cookie(
                SEEN_COOKIE,
                newest,
                max_age=SEEN_COOKIE_MAX_AGE,
                secure=True,
                httponly=False,
                samesite="Lax",
                path="/",
            )
        return response

    @app.post("/prefs/nav")
    def set_nav_preference():
        """Collapse or expand the sidebar. Writes one cookie and nothing else.

        A form post rather than a script because this app has none -- the
        checkbox in the template does the collapsing on its own, and this is
        only what makes the choice survive the next page load.

        Three things keep a preference toggle from becoming a hole:

        1. It requires a session and a CSRF token, like every other POST here.
           Not because a forged one is dangerous, but because "this endpoint is
           harmless" is an assumption that ages badly.
        2. It never touches the bot API, so it cannot be used to probe for
           guilds or to spend the bot's rate limit.
        3. The return trip is an endpoint *name* looked up in a fixed table,
           never a path from the form. A hidden field carrying a URL is how a
           settings toggle turns into an open redirect.
        """
        session = _require_login()
        if session is None:
            return redirect(url_for("index"))
        if not _csrf_ok(session):
            abort(400)

        collapsed = bool(request.form.get("collapsed"))
        response = redirect(_preference_return_url())
        if collapsed:
            response.set_cookie(
                NAV_COOKIE,
                "1",
                max_age=NAV_COOKIE_MAX_AGE,
                secure=True,
                httponly=True,
                samesite="Lax",
                path="/",
            )
        else:
            # Expanded is the default, so the preference is the absence of the
            # cookie rather than a second value to interpret.
            response.delete_cookie(NAV_COOKIE, path="/")
        return response

    @app.post("/prefs/seen")
    def mark_updates_seen():
        """Clear the bell's unread dot. Writes one cookie and nothing else.

        THE NO-JAVASCRIPT PATH, and the reason the dot is honest.

        prefs.js clears the dot the moment the panel opens, by writing this
        cookie directly -- which is what makes the interaction feel like every
        other notification panel. With scripts blocked, nothing tells the
        server the panel was ever opened, so without this route the dot would
        sit there permanently on a browser that had already read everything.
        That is worse than no dot at all: an indicator that never clears stops
        being an indicator.

        So the panel carries a plain "Mark all as read" button that posts
        here. It is not a fallback bolted on for the no-script case -- every
        comparable panel has one, and it works identically whether or not
        prefs.js ran.

        Follows `set_nav_preference` rather than `set_theme_preference` on
        session and CSRF: the bell is only rendered for a signed-in admin, so
        there is always a session and always a token, and none of the theme
        route's reasons for going without apply.

        The value is not taken from the form. The newest id is something this
        process already knows, and accepting one from a request would mean a
        crafted post could mark an entry seen that the browser never saw.
        """
        session = _require_login()
        if session is None:
            return redirect(url_for("index"))
        if not _csrf_ok(session):
            abort(400)

        response = redirect(_preference_return_url())
        newest = changelog.newest_id()
        if newest is None:
            # No feed, so nothing to have seen. Clearing rather than writing
            # keeps "absent" the only representation of "seen nothing".
            response.delete_cookie(SEEN_COOKIE, path="/")
            return response

        response.set_cookie(
            SEEN_COOKIE,
            newest,
            max_age=SEEN_COOKIE_MAX_AGE,
            secure=True,
            # NOT httponly, for the same reason as THEME_COOKIE: prefs.js
            # writes this one too, and with no `connect-src` it has no other
            # way to record that the panel was opened.
            httponly=False,
            samesite="Lax",
            path="/",
        )
        return response

    @app.post("/prefs/dismiss")
    def dismiss_update():
        """Put one premium entry's Overview card away, for one server.

        Session and CSRF, like `/prefs/nav` -- this only renders for a
        signed-in admin, so there is always a token to require.

        THE ENTRY ID IS CHECKED AGAINST WHAT WE SHIPPED. It has to come from
        the form, unlike `/prefs/seen`'s value: which card was on screen is
        something only the page knows. So it is validated against the shipped
        ids rather than trusted -- an id we do not recognise changes nothing
        and simply redirects back, which also keeps a crafted post from
        filling a bounded cookie with pairs that will never match anything.

        The guild id is likewise checked for being a number and nothing more.
        Neither value reaches the bot; this route writes one cookie.
        """
        session = _require_login()
        if session is None:
            return redirect(url_for("index"))
        if not _csrf_ok(session):
            abort(400)

        response = redirect(_preference_return_url())

        entry_id = request.form.get("entry_id") or ""
        if entry_id not in {entry.id for entry in changelog.ENTRIES}:
            return response
        try:
            guild_id = int(request.form.get("guild_id") or "")
        except ValueError:
            return response

        value = changelog.add_dismissal(_dismissed(), guild_id, entry_id)
        response.set_cookie(
            DISMISS_COOKIE,
            value,
            max_age=DISMISS_COOKIE_MAX_AGE,
            secure=True,
            httponly=False,
            samesite="Lax",
            path="/",
        )
        return response

    @app.post("/prefs/theme")
    def set_theme_preference():
        """Dark, Light or System. Writes one cookie and nothing else.

        **THIS IS THE ONE POST IN THIS APP THAT REQUIRES NEITHER A SESSION NOR
        A CSRF TOKEN, AND THE DEPARTURE IS DELIBERATE.**

        `set_nav_preference` above argues the opposite for itself -- that "this
        endpoint is harmless" is an assumption that ages badly -- so the
        difference is worth stating rather than leaving to be rediscovered.

        Why no session: the sign-in page has one of these buttons, and before
        signing in there is no session to require. `index` renders
        `login.html` with no arguments at all, so there is no CSRF token on
        that page either. Requiring either would mean the theme control works
        everywhere except the first page anybody sees, and would break the
        no-JavaScript promise exactly where it is easiest to notice.

        Why that is safe here, and would not be on the route above: this one
        reads nothing, stores nothing server-side, and never touches the bot.
        The entire consequence of a forged request is that somebody's own page
        renders in a different colour on their next load. There is no state to
        corrupt, nothing to leak, and no authority to borrow -- the cookie is
        read by exactly one thing, `_theme()`, which reduces it to one of three
        known words before it reaches the markup.

        Not rate-limited, also deliberately. It does strictly less work than
        the page render it redirects to -- no database, no crypto, no bot call
        -- so budgeting this while leaving every GET unbudgeted would be
        guarding the cheap path. The Stripe webhook has a limiter because it
        verifies a signature and writes rows; this sets a cookie.

        What it still does, because these are not about authentication:

        1. The submitted value is checked against a fixed set. A value from a
           form reaching a `data-` attribute unchecked is how a preference
           becomes an injection, and `_theme()` is the second line of that
           defence rather than the only one.
        2. The return trip is an endpoint *name* looked up in a fixed table,
           never a path from the form -- the same rule `set_nav_preference`
           follows, and for the same reason.
        """
        chosen = request.form.get("theme") or ""
        response = redirect(_preference_return_url())
        if chosen not in THEMES:
            # A form that submitted nothing recognisable changes nothing. No
            # error page: there is no way for an admin to cause this, so the
            # only reachable cause is a hand-built request, and the honest
            # answer to one of those is the page they asked to go back to.
            return response

        if chosen == THEME_DEFAULT:
            # Dark is what no cookie already means, so choosing it removes the
            # cookie rather than storing a second way of saying the same
            # thing. Same shape as the sidebar's "expanded" above -- one state,
            # one representation, nothing to keep in agreement.
            response.delete_cookie(THEME_COOKIE, path="/")
            return response

        response.set_cookie(
            THEME_COOKIE,
            chosen,
            max_age=THEME_COOKIE_MAX_AGE,
            secure=True,
            # NOT httponly: phase 4 has the button write this from a script so
            # the switch is instant, and the CSP has no `connect-src`, so a
            # script cannot ask the server to set it instead. See THEME_COOKIE.
            httponly=False,
            samesite="Lax",
            path="/",
        )
        return response

    @app.post("/guild/<int:guild_id>/verification")
    def save_verification_settings(guild_id: int):
        """The verification group: which roles, and auto-verify on join.

        Same shape as the panel save below, and the same division of labour --
        the bot confirms each role actually exists in the guild, which is the
        guarantee Discord's role picker gives `/vrcverify_setup` for free and
        this form cannot give itself.
        """
        session = _require_login()
        if session is None:
            return redirect(url_for("index"))
        if not _csrf_ok(session):
            abort(400)

        changes = {}
        if "role_id" in request.form:
            changes["role_id"] = request.form.get("role_id")
        if "unverified_role_id" in request.form:
            # A select always submits, so an empty value here is a real choice
            # -- "None". /vrcverify_setup clears it the same way, by leaving
            # the argument off.
            changes["unverified_role_id"] = request.form.get("unverified_role_id") or None
        _read_checkbox(changes, "auto_verify_new_members")

        return _save(guild_id, session, changes, "verification")

    @app.post("/guild/<int:guild_id>/panel/post")
    def post_panel(guild_id: int):
        """Put the instructions panel in a channel.

        The one control here that makes the bot act in a server rather than
        store a value. What "put it there" means -- a fresh post, a refresh of
        the one already there, or a move -- is decided by the bot, because it
        is the only side that can see where the panel actually is. This route's
        job is to carry the admin's choice of channel and report back what
        happened.
        """
        session = _require_login()
        if session is None:
            return redirect(url_for("index"))
        if not _csrf_ok(session):
            abort(400)

        channel_id = (request.form.get("panel_channel_id") or "").strip()
        if not channel_id:
            return redirect(_settings_url(guild_id, "panel"))

        try:
            result = _bot_api().post_panel(
                int(session.discord_id), guild_id, channel_id
            )
        except BotAPIError as error:
            logger.warning("panel post refused for guild %s: %s", guild_id, error)
            _store().set_notice(session.sid, f"panel_error:{_panel_error_code(error)}")
            return redirect(_settings_url(guild_id, "panel"))

        # Clamped to a known key before it travels, like the two error codes
        # are. This is the bot's own string, but it is the one place a bot value
        # reached a URL unchecked, and the invariant this module claims is that
        # nothing from over the wire is echoed back without being looked up.
        action = result.get("action")
        _store().set_notice(
            session.sid,
            f"panel:{action if action in PANEL_RESULTS else 'posted'}",
        )
        return redirect(_settings_url(guild_id, "panel"))

    @app.post("/guild/<int:guild_id>/member")
    def save_member_settings(guild_id: int):
        """Nickname sync and the custom verification DM.

        The message is submitted exactly as typed. Every rule about it -- the
        length cap, the zero-width stripping, the @everyone defusal, the
        discord.com/vrchat.com link allowlist -- belongs to the bot, which runs
        the same sanitiser its own slash command does. Trimming or cleaning it
        here would create a second opinion about what an admin is allowed to
        say through the bot.
        """
        session = _require_login()
        if session is None:
            return redirect(url_for("index"))
        if not _csrf_ok(session):
            abort(400)

        changes = {}
        _read_checkbox(changes, "auto_nickname_change")
        if "custom_verification_requested_message" in request.form:
            changes["custom_verification_requested_message"] = request.form.get(
                "custom_verification_requested_message"
            )

        return _save(guild_id, session, changes, "after-verifying")

    @app.post("/guild/<int:guild_id>/logging")
    def save_logging_settings(guild_id: int):
        """Where verification activity is logged, or nowhere."""
        session = _require_login()
        if session is None:
            return redirect(url_for("index"))
        if not _csrf_ok(session):
            abort(400)

        changes = {}
        if "verification_log_channel_id" in request.form:
            # A select always submits, so blank is the real choice "off" --
            # which is how /vrcverify_logchannel turns it off too.
            changes["verification_log_channel_id"] = (
                request.form.get("verification_log_channel_id") or None
            )

        return _save(guild_id, session, changes, "logging")

    @app.post("/guild/<int:guild_id>/panel")
    def save_panel_settings(guild_id: int):
        """Save the instructions panel group. The only write in the app.

        Three things guard it, and the third is the only one that counts:

        1. A session cookie, `SameSite=Lax` and `HttpOnly`.
        2. A CSRF token compared with `compare_digest`. The cookie policy
           already stops a cross-site POST, so this is the second line.
        3. The bot, which re-checks Administrator, re-checks the plan, and
           validates every value against its own allowlist. Nothing below is
           trusted by the thing that actually writes the row.

        Values are turned into JSON types here because HTML forms only carry
        strings, and the bot's API takes an int for a colour and a bool for a
        toggle. That conversion is not validation -- a colour that survives it
        can still be refused, and the refusal is what decides.
        """
        session = _require_login()
        if session is None:
            return redirect(url_for("index"))
        if not _csrf_ok(session):
            abort(400)

        changes = {}

        locale = request.form.get("instructions_locale")
        if locale:
            changes["instructions_locale"] = locale

        _read_checkbox(changes, "panel_show_icon")
        if request.form.get("present_panel_embed_color"):
            if request.form.get("panel_color_default"):
                changes["panel_embed_color"] = None
            else:
                changes["panel_embed_color"] = _colour_to_int(
                    request.form.get("panel_embed_color")
                )

        return _save(guild_id, session, changes, "panel")

    @app.get("/vrchat-icon/<file_id>/<version>")
    def vrchat_icon(file_id: str, version: str):
        """A VRChat group icon, served from this origin.

        VRChat's own URL cannot be used in an <img>. The endpoint the API puts
        in `icon_url` answers `application/octet-stream`, which browsers
        refuse to draw and offer to download instead; the resized endpoint
        answers `image/png` but only up to 512, and relying on a third party's
        content types is what produced a broken image on a live page twice.

        Serving it from here settles all of it at once: one origin, a content
        type this app decided by looking at the bytes, no CSP exception, and
        an admin's browser that never contacts VRChat at all.

        Not an open proxy. The path carries a file id and a version, both
        pattern-matched, and they go into a fixed host and path -- there is no
        request in which a caller names a host. Login is required so it cannot
        be used as an anonymous relay, though the files themselves are public.
        """
        session = _require_login()
        if session is None:
            abort(404)
        if not VRCHAT_FILE_ID_RE.match(file_id):
            abort(404)
        if not VRCHAT_FILE_VERSION_RE.match(version):
            abort(404)

        cache: _IconCache = app.config["ICON_CACHE"]
        found = cache.get(
            (file_id.lower(), version),
            lambda: _fetch_vrchat_icon(file_id, version),
            now=time.time(),
        )
        if found is None:
            # The page renders its own placeholder around this, so a 404 is a
            # gap in the layout rather than a broken-image icon.
            abort(404)

        content_type, body = found
        response = app.response_class(body, mimetype=content_type)
        # Private: it is only reachable by a signed-in admin, and a shared
        # cache holding it buys nothing.
        response.headers["Cache-Control"] = f"private, max-age={VRCHAT_ICON_TTL}"
        return response

    @app.post("/guild/<int:guild_id>/group")
    def save_group_settings(guild_id: int):
        """The VRChat group a server invites verified members to.

        The group field is submitted exactly as typed. Parsing it -- bare id or
        vrchat.com URL, case folding, refusing vrc.group short links -- is the
        bot's, and doing any of it here would create a second opinion about
        what a valid group is, on the side of the wire that does not enforce
        anything.
        """
        session = _require_login()
        if session is None:
            return redirect(url_for("index"))
        if not _csrf_ok(session):
            abort(400)

        changes = {}
        if "vrchat_group_id" in request.form:
            # Empty is a real choice: it disconnects the group and releases
            # the claim, so another server could then connect it.
            changes["vrchat_group_id"] = request.form.get("vrchat_group_id") or None
        _read_checkbox(changes, "vrchat_group_invite_enabled")

        return _save(guild_id, session, changes, "vrchat-group")

    @app.post("/guild/<int:guild_id>/group/verify")
    def verify_group(guild_id: int):
        """Ask the bot to join this guild's VRChat group and report back.

        The second control here that makes the bot act rather than store, and
        the only one that sends nothing at all. There is no group id in this
        request and there must never be: the bot reads the group from the
        guild's own settings, which is what stops this being a way to make a
        VRChat account join a group somebody names in a form.
        """
        session = _require_login()
        if session is None:
            return redirect(url_for("index"))
        if not _csrf_ok(session):
            abort(400)

        try:
            _bot_api().verify_group(int(session.discord_id), guild_id)
        except BotAPIError as error:
            logger.warning(
                "group check refused for guild %s: %s", guild_id, error
            )
            _store().set_notice(session.sid, f"error:{_save_error_code(error)}")
            return redirect(_settings_url(guild_id, "vrchat-group"))

        _store().set_notice(session.sid, "group_check")
        return redirect(_settings_url(guild_id, "vrchat-group"))

    @app.post("/logout")
    def logout():
        session = _require_login()
        if session is not None:
            if not _csrf_ok(session):
                abort(400)
            _store().destroy(session.sid)
        response = redirect(url_for("index"))
        response.delete_cookie(SESSION_COOKIE, path="/")
        return response

    @app.post("/logout/everywhere")
    def logout_everywhere():
        """End every session this Discord user has, not just this browser's.

        A separate route rather than a field on /logout, because the two are
        different promises and a mis-parsed form field must not silently
        downgrade this one into an ordinary sign-out. The count is logged: a
        user revoking four sessions when they only remember opening one is
        exactly the event this control exists for, and it is invisible unless
        somebody writes it down.
        """
        session = _require_login()
        if session is not None:
            if not _csrf_ok(session):
                abort(400)
            ended = _store().destroy_all_for(session.discord_id)
            logger.info(
                "actor=%s signed out of %s session(s) everywhere",
                session.discord_id,
                ended,
            )
        response = redirect(url_for("index"))
        response.delete_cookie(SESSION_COOKIE, path="/")
        return response

    def _chrome_for_error() -> dict:
        """The header the error pages were rendering without.

        base.html gates the account menu on `csrf_token`, so an error page that
        did not pass one came out with no way to sign out. That is the one
        thing the menu is in the bar for: the comment there says "sign out
        everywhere" is what you want at the moment you realise somebody else
        has your session, "and at that moment you should not have to go
        looking". Mistyping a URL should not be the thing that takes it away.

        `getattr` rather than `g.session` because a 500 raised inside
        `load_session` itself would leave the attribute unset, and an error
        handler that raises is the one place there is no second chance.
        """
        session = getattr(g, "session", None)
        return {"csrf_token": session.csrf_token} if session else {}

    @app.errorhandler(404)
    def not_found(_error):
        return render_template(
            "error.html", message="Page not found.", **_chrome_for_error()
        ), 404

    @app.errorhandler(500)
    def server_error(_error):  # pragma: no cover - defensive
        return render_template(
            "error.html", message="Something went wrong.", **_chrome_for_error()
        ), 500


# The refusals worth explaining differently, and the copy for each. Anything
# not listed falls through to the generic message, so an unrecognised reason
# can never reach the page as text.
SAVE_ERRORS = {
    "requires_premium": (
        "That setting needs VRCVerify Premium. Nothing was changed."
    ),
    "unsupported_language": (
        "That language isn't one VRCVerify supports. Nothing was changed."
    ),
    "server_not_set_up": (
        "Run /vrcverify_setup in your server first -- VRCVerify needs a "
        "verified role before it can store anything else."
    ),
    # No "use /vrcverify_settings instead". That command stopped being an
    # editor when configuration moved here -- it shows what is stored and links
    # back -- so sending somebody there to make a change would hand them a
    # command that reports success at displaying and alters nothing. It is the
    # worst place to do it, too: this string is read by an admin whose save was
    # just refused, who is looking for the thing that will work.
    "not_writable_yet": (
        "That setting can't be changed from the website yet. Nothing was "
        "changed."
    ),
    "unavailable": (
        "The bot couldn't complete the save, so nothing was changed. Try again "
        "shortly."
    ),
    "role_not_in_guild": (
        "That role isn't in this server any more. Reload the page and pick "
        "again."
    ),
    "role_required": "Pick a verified role -- verification can't run without one.",
    # The offending links are deliberately not echoed back. The rule is short
    # enough to state, the admin is looking at their own message, and the page
    # stays free of text that came from a request.
    "message_links_not_allowed": (
        "Links in the custom message may only point to discord.com or "
        "vrchat.com. Nothing was changed."
    ),
    "message_too_long": (
        "That custom message is too long. The limit is 1000 characters."
    ),
    "channel_is_announcement": (
        "Verification logs can't go in an announcement channel -- other servers "
        "can follow one, which would republish your members' age status."
    ),
    "channel_not_in_guild": (
        "That channel isn't in this server any more. Reload the page and pick "
        "again."
    ),
    "channel_not_writable": (
        "VRCVerify can't post in that channel, so it can't log there. Check the "
        "channel's permissions and try again."
    ),
    "column_missing": (
        "This bot's database is missing the column for that setting. Contact "
        "the bot operator."
    ),
    # The VRChat group (issue #49). The controls that submit these arrive with
    # the settings section; the copy is here first because the alternative --
    # an admin being told "that change couldn't be saved" when the real answer
    # is "another server already has that group" -- is a support ticket that
    # nobody can resolve from the page.
    "not_a_group": (
        "That doesn't look like a VRChat group. Paste the group's ID (it "
        "starts with grp_) or the vrchat.com link to the group."
    ),
    "group_shortlink_unsupported": (
        "VRChat's vrc.group short links can't be looked up. Open the group on "
        "vrchat.com and paste that link instead, or the group ID starting "
        "with grp_."
    ),
    # Deliberately does not say which server holds it. That is another
    # customer's guild, and naming it here would turn a group ID into a way to
    # find out who else uses this bot.
    "group_claimed_elsewhere": (
        "Another Discord server has already linked that VRChat group. A group "
        "can only belong to one server -- contact support if that's wrong."
    ),
    "no_group_configured": (
        "Add your VRChat group first, then run the setup check."
    ),
}
GENERIC_SAVE_ERROR = "That change couldn't be saved, so nothing was changed."

# The panel button shares reason codes with the settings saves, but not their
# wording. "so it can't log there" is the log channel's sentence and says
# nothing true about a panel, and a panel needs Embed Links as well as Send
# Messages -- which the log channel does not, so SAVE_ERRORS cannot just say so.
PANEL_ERRORS = {
    # Plain text: this is rendered into a web page, not sent to Discord, so
    # markdown asterisks would show up literally.
    "channel_not_writable": (
        "VRCVerify can't post the panel in that channel. It needs both Send "
        "Messages and Embed Links there -- Embed Links is the one that's "
        "usually missing, because the panel is an embed."
    ),
    "channel_not_in_guild": (
        "That channel isn't in this server any more. Reload the page and pick "
        "again."
    ),
}
GENERIC_PANEL_ERROR = (
    "The panel couldn't be posted just now. Try again shortly."
)

# Same treatment as the refusals: a code chosen by the bot, copy chosen here.
PANEL_RESULTS = {
    "posted": "Panel posted.",
    "refreshed": (
        "That channel already had the panel, so it was refreshed rather than "
        "posted again."
    ),
    "moved": (
        "Panel posted in the new channel. The old one is still up in its "
        "previous channel -- delete it in Discord when you're ready."
    ),
    # Panels posted by /vrcverify_instructions before it stopped replying with
    # them belong to a webhook, and Discord quietly ignores embed edits on those
    # -- so the language and colour could never be applied to one. The only
    # repair is a new message, which is why this reads as an explanation rather
    # than as a plain success.
    "replaced": (
        "That panel was posted in a way Discord won't let the bot edit, so it "
        "was replaced with a fresh one. Your settings apply to it now."
    ),
}


def _register_stripe_webhook(app: Flask) -> None:
    """The one public, unauthenticated inbound route this project has.

    Registered only when STRIPE_ENABLED is set, so with the switch off the path
    is a plain 404 rather than a handler trusted to decline. Everything else on
    this host is reached by a browser holding a session; this is reached by
    Stripe's infrastructure holding a signature, and it is deliberately NOT
    under `/guild/` so that no reviewer skimming the routes mistakes it for a
    session-authenticated one.

    Read the order of operations here as the security design, because it is:

    1. Budget, before anything is read.
    2. Size cap, before the body is taken into memory.
    3. **Signature, before the body is parsed.** Until this passes, the bytes
       are an unknown party's, and nothing has looked at them.
    4. Only then: parse, decide whether it is an event we act on, fetch current
       state, forward.

    And read the response codes as the retry contract. Stripe retries a non-2xx
    for up to three days, which comfortably covers a bot restart, a Tailscale
    blip or a homelab power cut. So anything we could not complete must be a
    non-2xx, and anything genuinely finished — including "this is not an event
    we care about" — must be a 200, or Stripe spends three days redelivering
    something that will never be actionable.
    """

    @app.post("/stripe/webhook")
    def stripe_webhook():
        config = _config()
        now = time.time()

        if not app.config["STRIPE_RATE"].allow("stripe", now=now):
            logger.warning("stripe webhook rate limited")
            return {"error": "rate_limited"}, 429

        # Checked before reading, so an oversized body is refused rather than
        # buffered. 413 is a client error and Stripe will not usefully retry
        # it, which is correct: nothing legitimate is this large.
        length = request.content_length
        if length is not None and length > stripe_events.MAX_BODY_BYTES:
            logger.warning("stripe webhook body too large: %s bytes", length)
            return {"error": "too_large"}, 413

        # get_data(), never get_json(): the HMAC covers the exact bytes, and
        # letting Flask parse first would both defeat the signature and run the
        # parser on unverified input.
        payload = request.get_data(cache=False)
        if len(payload) > stripe_events.MAX_BODY_BYTES:
            return {"error": "too_large"}, 413

        try:
            stripe_events.verify_signature(
                payload,
                request.headers.get("Stripe-Signature"),
                config.stripe_webhook_secret,
                now=now,
            )
        except stripe_events.SignatureError as error:
            # Logged like every other auth decision -- a run of these is the
            # thing worth alerting on. The response says nothing useful: an
            # endpoint that explains why a signature failed helps someone
            # construct one that doesn't.
            logger.warning("stripe webhook rejected: %s", error.reason)
            return {"error": "invalid_signature"}, 400

        try:
            event = stripe_events.parse_event(payload)
        except stripe_events.SignatureError as error:
            logger.warning("stripe webhook unreadable: %s", error.reason)
            return {"error": "invalid_payload"}, 400

        event_id = event.get("id")
        if not isinstance(event_id, str) or not event_id:
            logger.warning("stripe webhook has no event id; ignoring")
            return {"ok": True, "ignored": "no_event_id"}

        subscription = stripe_events.subscription_from(event)
        if subscription is None:
            # Acknowledged and dropped. Somebody enabling an extra event type
            # in the Stripe dashboard must not start a three-day retry storm.
            return {"ok": True, "ignored": "event_type"}

        guild_id = stripe_events.guild_id_from(subscription)
        if guild_id is None:
            # Nothing to route it to, and guessing is not an option. 200,
            # because retrying will not add metadata that was never set --
            # this is a checkout built wrong, and the log is where it gets
            # noticed.
            logger.error(
                "Stripe subscription %s has no guild_id in its metadata; "
                "nothing to record. Check how the Checkout Session is built.",
                subscription.get("id"),
            )
            return {"ok": True, "ignored": "no_guild"}

        # Current state rather than the event's snapshot -- see
        # StripeClient.get_subscription for why ordering makes this the right
        # read. A failure here is a non-2xx, so Stripe retries.
        try:
            current = app.config["STRIPE"].get_subscription(subscription["id"])
        except StripeAPIError as error:
            logger.warning("could not read subscription from Stripe: %s", error)
            return {"error": "stripe_unavailable"}, 503

        # We asked about one subscription; anything else coming back means the
        # request did not go where this code believes it went. It should be
        # impossible, which is exactly why it is checked rather than assumed:
        # without this, a read that landed on a different object would be
        # written to whichever guild *that* object names.
        if current.get("id") != subscription["id"]:
            logger.error(
                "Asked Stripe for subscription %s and got %r back; refusing to "
                "record it. Event %s.",
                subscription["id"],
                current.get("id"),
                event_id,
            )
            return {"error": "subscription_mismatch"}, 503

        # The guild binding prefers what Stripe just told us, because the
        # fetched object is the authority on every other field and taking one
        # field from the older copy is how the two quietly disagree. It falls
        # back to the event's copy only when the fetched object carries no
        # guild at all -- metadata being cleared should not unsubscribe a
        # server that is still paying.
        current_guild = stripe_events.guild_id_from(current)
        if current_guild is not None:
            guild_id = current_guild

        normalised = stripe_events.normalise(
            current, event_id=event_id, event_created=event.get("created")
        )
        if normalised is None:
            logger.error(
                "Stripe subscription %s is missing fields this cannot record; "
                "ignoring. Event %s.",
                subscription.get("id"),
                event_id,
            )
            return {"ok": True, "ignored": "incomplete"}

        try:
            result = _bot_api().put_stripe_subscription(guild_id, normalised)
        except BotAPIError as error:
            # Never 200-and-drop. This is the failure the three-day retry
            # window exists for, and answering 200 here is the one thing that
            # loses a paid subscription permanently.
            logger.warning(
                "could not forward Stripe event %s for guild %s: %s",
                event_id,
                guild_id,
                error,
            )
            return {"error": "bot_unavailable"}, 503

        logger.info(
            "stripe webhook applied=%s guild=%s event=%s",
            result.get("applied"),
            guild_id,
            event_id,
        )
        return {"ok": True, "applied": bool(result.get("applied"))}


def _read_checkbox(changes: dict, name: str) -> None:
    """Record a checkbox only if its control was actually on the page.

    An unticked box submits nothing, which is indistinguishable from a control
    that was never rendered -- so the template emits a `present_<name>` marker
    beside every checkbox. Without it, saving a free server's language would
    look exactly like switching its branding off, and the bot would dutifully
    be asked to do so.
    """
    if request.form.get(f"present_{name}"):
        changes[name] = bool(request.form.get(name))


def _same_secret(expected: str, submitted: str) -> bool:
    """Constant-time compare that survives whatever the request carried.

    `secrets.compare_digest` raises TypeError on a str containing non-ASCII, so
    comparing a submitted value directly turns "wrong token" into an unhandled
    500 -- including on `/callback`, which a stranger can reach. Comparing the
    utf-8 bytes keeps the timing property and makes every wrong value simply
    wrong.
    """
    return secrets.compare_digest(
        str(expected or "").encode("utf-8"), str(submitted or "").encode("utf-8")
    )


def _csrf_ok(session) -> bool:
    """The second line. `SameSite=Lax` on the cookie is the first."""
    return _same_secret(session.csrf_token, request.form.get("csrf_token", ""))


def _settings_url(guild_id: int, group: str) -> str:
    """Where a POST from a settings group returns to.

    ONE PLACE, because #140 phase 2 splits Settings into a page per group and
    every redirect below has to follow the admin to the group they were
    actually on -- otherwise a save on Logging answers on Verification, with
    the right notice on the wrong page.

    Phase 1 threaded the slug through all nine call sites and ignored it here.
    This is the line that phase promised: the same nine now answer on the page
    the admin was actually on, instead of putting the right notice on the wrong
    one. The easy site to miss is `_save`'s `if not changes:` guard, which reads
    like a no-op.

    The slug is checked against the table rather than trusted. Every caller
    passes a literal, so one outside SETTINGS_SLUGS is a typo -- and a typo
    here is now a redirect to a URL that 404s, which is exactly what one table
    of slugs exists to prevent.
    """
    if group not in settings_view.SETTINGS_SLUGS:
        raise ValueError(f"unknown settings group: {group!r}")
    return url_for("guild_settings", guild_id=guild_id, group=group)


def _save(guild_id: int, session, changes: dict, group: str):
    """Hand a group's changes to the bot and turn the answer into a redirect.

    Shared by every group so there is exactly one place that talks to the write
    endpoint, one place that decides what a refusal looks like, and one thing
    to re-read if that ever needs to change.

    `group` is the caller's slug from `settings_view.SETTINGS_SLUGS`, passed so
    every one of the four exits below can return to the page the save came
    from. See `_settings_url`.
    """
    if not changes:
        return redirect(_settings_url(guild_id, group))

    try:
        saved = _bot_api().update_settings(int(session.discord_id), guild_id, changes)
        # The save worked; the panel may not have followed it. Carried as its
        # own flag rather than an error, because "stored but the panel still
        # shows the old thing" is a true success plus a caveat, and reporting it
        # as a failure would send an admin round the loop that produced it.
        if isinstance(saved, dict) and saved.get("panel_stale"):
            _store().set_notice(session.sid, "stale")
            return redirect(_settings_url(guild_id, group))
    except BotAPIError as error:
        logger.warning("save refused for guild %s: %s", guild_id, error)
        # A code, never the bot's text. What comes back is a fixed reason
        # string today, but round-tripping it through a URL and into a page
        # would make the bot's error strings part of this app's HTML, and the
        # day one of them carries something a caller influenced is not the day
        # to find that out.
        _store().set_notice(session.sid, f"error:{_save_error_code(error)}")
        return redirect(_settings_url(guild_id, group))

    _store().set_notice(session.sid, "saved")
    return redirect(_settings_url(guild_id, group))


# What the Subscriptions page may say back to an admin after a POST, keyed so
# nothing from a form or from Stripe reaches the page as text. Same discipline
# as the panel results: a message is chosen from this table or not shown.
SUBSCRIPTION_NOTICES = {
    "bought": (
        "Thanks — your subscription is being set up. Premium switches on as "
        "soon as Stripe confirms the payment, usually within a few seconds.",
        "ok",
    ),
    "plan": ("That plan isn't one we offer. Nothing has been charged.", "error"),
    "already": (
        "This server already has Premium, so there was nothing to buy. "
        "Nothing has been charged.",
        "error",
    ),
    "stripe": (
        "We couldn't reach Stripe just now, so nothing has been charged. "
        "Try again in a moment, or subscribe inside Discord instead.",
        "error",
    ),
    "portal": (
        "There's no card subscription on this server to manage.",
        "error",
    ),
}


def _subscription_redirect(session, guild_id: int, notice: str):
    """Send the admin back to the page with one of the notices above."""
    _store().set_notice(session.sid, f"subscription:{notice}")
    return redirect(url_for("guild_subscription", guild_id=guild_id))


def _subscription_notice(session):
    """The pending notice for this page, as (message, kind)."""
    raw = _store().take_notice(session.sid) or ""
    if not raw.startswith("subscription:"):
        return None, None
    key = raw.partition(":")[2]
    # `error:stripe` arrives as `error:stripe`; take the last segment.
    key = key.rpartition(":")[2] or key
    message = SUBSCRIPTION_NOTICES.get(key)
    return message if message else (None, None)


def _notice_arg(notice: Optional[str], kind: str) -> Optional[str]:
    """`panel:moved` -> `moved`, but only when asked for the right kind."""
    if not notice or ":" not in notice:
        return None
    prefix, _, value = notice.partition(":")
    return value if prefix == kind else None


def _save_error_code(error: BotAPIError) -> str:
    reason = str(error)
    return reason if reason in SAVE_ERRORS else "unknown"


def _save_error_message(code: Optional[str]) -> Optional[str]:
    """Copy for a refusal, chosen by us -- the code is only ever a lookup key."""
    if not code:
        return None
    return SAVE_ERRORS.get(code, GENERIC_SAVE_ERROR)


def _panel_error_code(error: BotAPIError) -> str:
    reason = str(error)
    return reason if reason in PANEL_ERRORS else "unknown"


def _panel_error_message(code: Optional[str]) -> Optional[str]:
    if not code:
        return None
    return PANEL_ERRORS.get(code, GENERIC_PANEL_ERROR)


def _colour_to_int(raw: Optional[str]) -> Optional[int]:
    """`#rrggbb` from a colour input, as the integer the bot stores.

    Returns None for anything that is not that shape, which the bot then
    refuses -- rather than guessing at a colour the admin did not pick.
    """
    text = (raw or "").strip().lstrip("#")
    if len(text) != 6:
        return None
    try:
        return int(text, 16)
    except ValueError:
        return None


def _session_guild(session, guild_id: int) -> Optional[dict]:
    """The OAuth record for this guild, for its name and icon only.

    Display, never authority -- the bot has already decided whether this page
    may be rendered at all. A guild missing from the list still renders, because
    an admin promoted since login has a stale list and is nonetheless entitled
    to the page.
    """
    target = str(guild_id)
    for guild in session.guilds or []:
        if str(guild.get("id")) == target:
            return guild
    return None


# The sidebar, in order. Kept here rather than in the template so the section
# list is one thing in one place -- a nav that disagrees with the routes is a
# link to a 404, and a route with no nav entry is a page nobody can reach.
SECTIONS = (
    ("overview", "Overview", "guild_overview"),
    ("settings", "Settings", "guild_settings"),
    ("subscription", "Subscriptions", "guild_subscription"),
)

SECTION_ENDPOINTS = {key: endpoint for key, _label, endpoint in SECTIONS}


def _guild_chrome(
    session, guild_id: int, section: str, group: Optional[str] = None
) -> dict:
    """Everything every guild page needs regardless of which section it is.

    The name and icon come from the session's OAuth copy, which is display data
    and stale by design -- the bot has already decided whether this page may be
    rendered at all, and a guild missing from a stale list still renders rather
    than pretending an admin promoted since login has no server.

    `group` is the settings sub-page, when there is one. It travels because the
    theme picker, the sidebar toggle and the bell's "Mark all as read" all post
    from every page and all have to land the reader back on the one they were
    reading -- and once Settings is six pages, "the settings page" is no longer
    an answer.
    """
    guild = _session_guild(session, guild_id)
    return {
        "guild_name": (guild or {}).get("name") or f"Server {guild_id}",
        "guild_icon": oauth.icon_url(guild) if guild else None,
        "guild_id": str(guild_id),
        "section": section,
        "sections": SECTIONS,
        "nav_collapsed": _nav_collapsed(),
        # Which page the hamburger should return to. A key from our own table,
        # so the form carries a name we recognise rather than a path it chose.
        "nav_return_to": SECTION_ENDPOINTS.get(section, "index"),
        # Empty on every page that has no sub-page, which the forms treat as
        # "no group" rather than as a value.
        "nav_return_group": group or "",
        # The Settings sub-nav, on every page rather than only on Settings --
        # the disclosure is in the sidebar, so it is rendered (closed) beside
        # Overview and Subscriptions too. Slugs and labels come from the one
        # table the routes read, so the nav cannot offer a page that does not
        # exist. Activity is appended rather than listed there because it is
        # not a settings group: `build_groups()` does not return it.
        "settings_subnav": settings_view.SETTINGS_GROUPS
        + ((settings_view.ACTIVITY_SLUG, settings_view.ACTIVITY_TITLE),),
        "settings_group": group or "",
        "csrf_token": session.csrf_token,
    }


def _dismissed() -> tuple:
    """The `guild:entry` pairs this browser has dismissed.

    `changelog.parse_dismissed` drops anything malformed and anything naming
    an entry we no longer ship, so nothing downstream has to be defensive
    about a hand-edited cookie.
    """
    return changelog.parse_dismissed(request.cookies.get(DISMISS_COOKIE))


def _nav_collapsed() -> bool:
    """Whether this browser last asked for the narrow sidebar."""
    return request.cookies.get(NAV_COOKIE) == "1"


def _theme() -> str:
    """Which of the three themes this browser last asked for.

    Anything unrecognised -- absent, empty, hand-edited, left over from a
    future version -- becomes the default rather than an error. There is no
    state to corrupt and nothing to warn about: the reader gets a dark page and
    can pick again.
    """
    chosen = request.cookies.get(THEME_COOKIE)
    return chosen if chosen in THEMES else THEME_DEFAULT


def _preference_return_url() -> str:
    """Where a preference form sends you back to, from a name we chose.

    Shared by the sidebar toggle and the theme picker -- both post from every
    page in the app and both have to land the reader back where they were.

    The form submits an endpoint name and, for a guild page, an id. Both are
    checked here: an unknown name falls back to the picker, and a guild id that
    is not a number is dropped. Nothing from the request is ever interpolated
    into a redirect target, so the worst a crafted form achieves is landing the
    user on their own server list.

    The theme picker reaches this while signed out, where the only reachable
    entry is `index` -- which renders the sign-in page. That is the correct
    destination and needs no special case.
    """
    endpoint = request.form.get("return_to") or ""
    if endpoint not in NAV_RETURN_ENDPOINTS:
        return url_for("index")

    values = {}
    for name in NAV_RETURN_ENDPOINTS[endpoint]:
        raw = request.form.get(name)
        try:
            values[name] = int(raw)
        except (TypeError, ValueError):
            return url_for("index")

    # The one string a return form may carry, and the only reason this function
    # needed touching for #140: Settings is six pages now, so "back to
    # Settings" is no longer an answer. Without this, changing theme on
    # /settings/logging drops the reader on /settings/verification.
    #
    # Looked up, never passed through. A slug is no more trustworthy than a
    # path for being short, and the rule this function states -- that nothing
    # from the request is interpolated into a redirect target -- has to hold
    # for strings as well as for ids.
    #
    # Anything unrecognised falls through to the bare settings URL rather than
    # to the server list: a form cached before this shipped carries no group at
    # all, and bouncing a reader out of the server they were configuring is a
    # worse answer than landing them on its first settings page. A hand-edited
    # one gets the same treatment, because the value never reaches `url_for`.
    if endpoint == "guild_settings":
        group = request.form.get("group") or ""
        if group in SETTINGS_GROUP_READS:
            values["group"] = group

    return url_for(endpoint, **values)


def _guild_page_unavailable(
    error: BotAPIError, guild_id: int, session, section: str = "settings"
):
    """Turn a refusal into a page, without saying which refusal it was.

    The bot distinguishes 404 "not in that guild" from 403 "you do not
    administer it", which is right inside the mTLS boundary and wrong on the
    open web. Rendered differently, a signed-in user could walk arbitrary guild
    ids and learn which servers run VRCVerify -- a census of communities
    operating 18+ gating, from a browser, with nothing compromised. It is the
    same oracle handle_list_guilds was hardened against, arriving by a
    different door.

    Shared by all three sections, and that sharing is the control. Three
    sections that each decided how to fail would be three chances for one of
    them to be more forthcoming than the others -- and an oracle only has to
    exist on one route to be worth using. `section` reaches the log line and
    nothing else; the page an outsider sees is identical whichever door they
    tried.

    503 is kept separate: it says the bot cannot answer right now, which
    discloses nothing about any particular guild, and telling an admin to try
    again is far better than telling them the server does not exist.
    """
    if error.status in (403, 404):
        logger.info(
            "%s page refused for actor=%s guild=%s (status %s)",
            section,
            session.discord_id,
            guild_id,
            error.status,
        )
        return (
            render_template(
                "error.html",
                message=(
                    "That server isn't available. Either VRCVerify isn't in it, "
                    "or you don't have the Administrator permission there."
                ),
                csrf_token=session.csrf_token,
            ),
            404,
        )

    logger.warning(
        "%s read failed for guild %s: %s", section, guild_id, error
    )
    return (
        render_template(
            "error.html",
            message=(
                # Section-neutral, because all three land here. It used to name
                # settings, which on the Overview would have been telling an
                # admin the wrong thing was unavailable. "Nothing has changed"
                # stays: it is the sentence that matters after a failed save,
                # and it is true on a page with no save in it.
                "Can't reach the bot right now, so this page can't be shown. "
                "Nothing has changed. Try again shortly."
            ),
            csrf_token=session.csrf_token,
        ),
        503,
    )


def _optional_read(call, what: str, guild_id: int):
    """A secondary read whose failure must not cost the whole page."""
    try:
        return call()
    except BotAPIError as error:
        logger.warning("could not read %s for guild %s: %s", what, guild_id, error)
        return None


def _invite_url(client_id: str, guild_id: Optional[str] = None) -> str:
    """The bot's install flow, at one specific server or at none.

    `disable_guild_select` plus `guild_id` means the admin lands on the right
    server rather than a dropdown, which is the whole reason a greyed-out tile
    is worth clicking.

    WITHOUT a guild it is the generic install link, which is what /pricing
    needs: a stranger reading a price has no server in context and the whole
    point of the page is that they have not signed in. They get the dropdown,
    which is correct -- it is the only way to choose.

    One function for both so the permissions integer below is written once.
    The apex site hardcodes its own copy of this URL with NO permissions at
    all, which lands the installer on a consent screen asking for nothing;
    that is #195's business, not this function's, but it is why the number
    living in exactly one place here matters.

    The permissions integer is what the bot actually needs: Manage Roles (to
    assign the verified role), and Send Messages / Embed Links / Read History
    (to post and maintain the instructions panel). Asking for more would be a
    worse pitch and a bigger blast radius.
    """
    permissions = (
        0x10000000  # Manage Roles
        | 0x800  # Send Messages
        | 0x4000  # Embed Links
        | 0x10000  # Read Message History
        | 0x400  # View Channel
    )
    url = (
        "https://discord.com/oauth2/authorize"
        f"?client_id={client_id}"
        f"&scope=bot+applications.commands"
        f"&permissions={permissions}"
    )
    if guild_id is None:
        return url
    return f"{url}&guild_id={guild_id}&disable_guild_select=true"


def main():  # pragma: no cover - container entrypoint
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )
    # This process logs the most externally-controlled text of the four:
    # OAuth claims, Stripe event and subscription ids, and guild ids taken
    # from Checkout metadata that anyone can set. See log_safety.
    #
    # gunicorn's two loggers are named explicitly because it sets
    # propagate = False on both and gives them their own handlers, so they are
    # invisible from root. It runs this factory after configuring them, which
    # is what makes naming them here work at all.
    install_log_scrubbing(
        logging.getLogger(),
        logging.getLogger("gunicorn.error"),
        logging.getLogger("gunicorn.access"),
    )
    return create_app()


if __name__ == "__main__":  # pragma: no cover
    main().run(host="127.0.0.1", port=8000)
