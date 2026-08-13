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
import secrets
import time
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
from werkzeug.middleware.proxy_fix import ProxyFix

from dashboard import oauth, overview_view, settings_view, stripe_events
from dashboard.botapi import BotAPIClient, BotAPIError
from dashboard.config import DashboardConfig
from dashboard.sessions import SessionStore
from dashboard.stripe_api import StripeAPIError, StripeClient

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

# Where the hamburger may send you back to. An endpoint name, never a URL from
# the request -- a form field carrying a path is an open redirect waiting for
# someone to notice, and this form exists to toggle a cookie.
NAV_RETURN_ENDPOINTS = {
    "index": (),
    "guild_overview": ("guild_id",),
    "guild_settings": ("guild_id",),
    "guild_subscription": ("guild_id",),
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
    "img-src 'self' https://cdn.discordapp.com; "
    "form-action 'self'; "
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
        # Authenticated pages must not sit in a shared cache. Static files are
        # the exception, and it is a real one: `no-store` on everything meant
        # the 48KB font and the stylesheet were re-fetched on every single page
        # view, by every admin, forever. They carry nothing about a session --
        # they are the same bytes for a signed-out stranger -- and their URLs
        # carry a content digest, so a deploy changes the URL and a stale copy
        # is unreachable rather than merely unwanted.
        if request.endpoint == "static":
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
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

        return render_template(
            "overview.html",
            tiles=overview_view.build_tiles(overview),
            next_step=overview_view.build_next_step(overview),
            setup=overview_view.build_setup(overview),
            premium=(overview.get("premium") or {}),
            **_guild_chrome(session, guild_id, "overview"),
        )

    @app.get("/guild/<int:guild_id>/subscription")
    def guild_subscription(guild_id: int):
        """Placeholder for the subscriptions section.

        It calls the bot before rendering, which looks like a wasted round trip
        for a page with nothing on it -- and is the point. An unauthorised
        visitor must get the same answer here as they would from the other two
        sections, or this becomes the one route that tells them whether a guild
        exists. It costs one call on a page nobody reloads.
        """
        session = _require_login()
        if session is None:
            return redirect(url_for("index"))

        try:
            _bot_api().settings(int(session.discord_id), guild_id)
        except BotAPIError as error:
            return _guild_page_unavailable(error, guild_id, session, "subscription")

        return render_template(
            "subscription.html", **_guild_chrome(session, guild_id, "subscription")
        )

    @app.get("/guild/<int:guild_id>/settings")
    def guild_settings(guild_id: int):
        """One server's settings, read-only.

        Authority is the bot's, on every one of the calls below: each mints its
        own token and the bot re-checks Administrator before answering. The
        session is what proves who is asking, never what they may see -- so a
        stale OAuth guild list cannot widen access, and a demotion in Discord
        takes effect on the next page load rather than at session expiry.
        """
        session = _require_login()
        if session is None:
            return redirect(url_for("index"))

        actor = int(session.discord_id)
        try:
            settings = _bot_api().settings(actor, guild_id)
        except BotAPIError as error:
            return _guild_page_unavailable(error, guild_id, session, "settings")

        # Names for ids, and the panel's whereabouts. Best-effort on purpose:
        # an unresolved id renders as an id, which is less useful but still
        # true, and that is a better page than an error over a secondary read.
        # The settings themselves are not optional -- rendering defaults an
        # admin never chose would be a lie the step-5 save path could persist.
        roles = _optional_read(lambda: _bot_api().roles(actor, guild_id), "roles", guild_id)
        channels = _optional_read(
            lambda: _bot_api().channels(actor, guild_id), "channels", guild_id
        )
        # Read once and cleared, so a reload does not repeat it.
        notice = _store().take_notice(session.sid)
        panel = _optional_read(
            lambda: _bot_api().panel(actor, guild_id), "panel", guild_id
        )
        audit = _optional_read(
            lambda: _bot_api().audit(actor, guild_id), "audit", guild_id
        )

        return render_template(
            "settings.html",
            groups=settings_view.build_groups(settings, roles, channels, panel),
            audit=settings_view.build_audit(audit, roles, channels),
            premium=settings.get("premium") or {},
            upgrade=settings_view.build_upgrade(
                settings, _config().discord_client_id
            ),
            names_resolved=roles is not None and channels is not None,
            auto_verify_column_present=settings.get("auto_verify_column_present", True),
            saved=notice == "saved",
            panel_result=PANEL_RESULTS.get(_notice_arg(notice, "panel")),
            panel_stale=notice == "stale",
            save_error=(
                _save_error_message(_notice_arg(notice, "error"))
                or _panel_error_message(_notice_arg(notice, "panel_error"))
            ),
            **_guild_chrome(session, guild_id, "settings"),
        )

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
        response = redirect(_nav_return_url())
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

        return _save(guild_id, session, changes)

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
            return redirect(url_for("guild_settings", guild_id=guild_id))

        try:
            result = _bot_api().post_panel(
                int(session.discord_id), guild_id, channel_id
            )
        except BotAPIError as error:
            logger.warning("panel post refused for guild %s: %s", guild_id, error)
            _store().set_notice(session.sid, f"panel_error:{_panel_error_code(error)}")
            return redirect(url_for("guild_settings", guild_id=guild_id))

        # Clamped to a known key before it travels, like the two error codes
        # are. This is the bot's own string, but it is the one place a bot value
        # reached a URL unchecked, and the invariant this module claims is that
        # nothing from over the wire is echoed back without being looked up.
        action = result.get("action")
        _store().set_notice(
            session.sid,
            f"panel:{action if action in PANEL_RESULTS else 'posted'}",
        )
        return redirect(url_for("guild_settings", guild_id=guild_id))

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

        return _save(guild_id, session, changes)

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

        return _save(guild_id, session, changes)

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

        return _save(guild_id, session, changes)

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

    @app.errorhandler(404)
    def not_found(_error):
        return render_template("error.html", message="Page not found."), 404

    @app.errorhandler(500)
    def server_error(_error):  # pragma: no cover - defensive
        return render_template("error.html", message="Something went wrong."), 500


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
    "not_writable_yet": (
        "That setting can't be changed from the website yet. Use "
        "/vrcverify_settings in your server."
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


def _save(guild_id: int, session, changes: dict):
    """Hand a group's changes to the bot and turn the answer into a redirect.

    Shared by every group so there is exactly one place that talks to the write
    endpoint, one place that decides what a refusal looks like, and one thing
    to re-read if that ever needs to change.
    """
    if not changes:
        return redirect(url_for("guild_settings", guild_id=guild_id))

    try:
        saved = _bot_api().update_settings(int(session.discord_id), guild_id, changes)
        # The save worked; the panel may not have followed it. Carried as its
        # own flag rather than an error, because "stored but the panel still
        # shows the old thing" is a true success plus a caveat, and reporting it
        # as a failure would send an admin round the loop that produced it.
        if isinstance(saved, dict) and saved.get("panel_stale"):
            _store().set_notice(session.sid, "stale")
            return redirect(url_for("guild_settings", guild_id=guild_id))
    except BotAPIError as error:
        logger.warning("save refused for guild %s: %s", guild_id, error)
        # A code, never the bot's text. What comes back is a fixed reason
        # string today, but round-tripping it through a URL and into a page
        # would make the bot's error strings part of this app's HTML, and the
        # day one of them carries something a caller influenced is not the day
        # to find that out.
        _store().set_notice(session.sid, f"error:{_save_error_code(error)}")
        return redirect(url_for("guild_settings", guild_id=guild_id))

    _store().set_notice(session.sid, "saved")
    return redirect(url_for("guild_settings", guild_id=guild_id))


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


def _guild_chrome(session, guild_id: int, section: str) -> dict:
    """Everything every guild page needs regardless of which section it is.

    The name and icon come from the session's OAuth copy, which is display data
    and stale by design -- the bot has already decided whether this page may be
    rendered at all, and a guild missing from a stale list still renders rather
    than pretending an admin promoted since login has no server.
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
        "csrf_token": session.csrf_token,
    }


def _nav_collapsed() -> bool:
    """Whether this browser last asked for the narrow sidebar."""
    return request.cookies.get(NAV_COOKIE) == "1"


def _nav_return_url() -> str:
    """Where the hamburger sends you back to, from a name we chose.

    The form submits an endpoint name and, for a guild page, an id. Both are
    checked here: an unknown name falls back to the picker, and a guild id that
    is not a number is dropped. Nothing from the request is ever interpolated
    into a redirect target, so the worst a crafted form achieves is landing the
    user on their own server list.
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


def _invite_url(client_id: str, guild_id: str) -> str:
    """Deep-link the bot's install flow at one specific server.

    `disable_guild_select` plus `guild_id` means the admin lands on the right
    server rather than a dropdown, which is the whole reason a greyed-out tile
    is worth clicking.

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
    return (
        "https://discord.com/oauth2/authorize"
        f"?client_id={client_id}"
        f"&scope=bot+applications.commands"
        f"&permissions={permissions}"
        f"&guild_id={guild_id}"
        "&disable_guild_select=true"
    )


def main():  # pragma: no cover - container entrypoint
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )
    return create_app()


if __name__ == "__main__":  # pragma: no cover
    main().run(host="127.0.0.1", port=8000)
