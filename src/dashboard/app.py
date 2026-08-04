"""The dashboard web app — steps 3 to 5 of issue #65: login, picker, settings.

One write path, opened in step 5: the instructions panel group, and nothing
else. Which fields that covers is the bot's decision, reported per field in the
settings payload, so this app renders a control only where the bot has already
said it would accept the value. It does not hold a copy of that list, because
a second copy is a thing that can disagree with the enforcing one.

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
  to need anything else.
"""

from __future__ import annotations

import logging
import secrets
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

from dashboard import oauth, settings_view
from dashboard.botapi import BotAPIClient, BotAPIError
from dashboard.config import DashboardConfig
from dashboard.sessions import SessionStore

logger = logging.getLogger(__name__)

SESSION_COOKIE = "vrcverify_session"

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
) -> Flask:
    config = config or DashboardConfig.from_env()
    app = Flask(__name__)
    app.config["DASHBOARD"] = config
    app.secret_key = config.secret_key

    # Trust exactly one hop, for exactly the scheme and host. cloudflared is
    # the only thing that can reach this app -- it has no published port -- so
    # one hop is the whole chain.
    app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1, x_for=1)

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

    _register_routes(app)
    _register_hooks(app)
    return app


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
        # Authenticated pages must not sit in a shared cache, and there is
        # nothing here worth caching anyway.
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
        if not secrets.compare_digest(pending.oauth_state, state):
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
            return _settings_unavailable(error, guild_id, session)

        # Names for ids, and the panel's whereabouts. Best-effort on purpose:
        # an unresolved id renders as an id, which is less useful but still
        # true, and that is a better page than an error over a secondary read.
        # The settings themselves are not optional -- rendering defaults an
        # admin never chose would be a lie the step-5 save path could persist.
        roles = _optional_read(lambda: _bot_api().roles(actor, guild_id), "roles", guild_id)
        channels = _optional_read(
            lambda: _bot_api().channels(actor, guild_id), "channels", guild_id
        )
        panel = _optional_read(
            lambda: _bot_api().panel(actor, guild_id), "panel", guild_id
        )

        guild = _session_guild(session, guild_id)
        return render_template(
            "settings.html",
            guild_name=(guild or {}).get("name") or f"Server {guild_id}",
            guild_icon=oauth.icon_url(guild) if guild else None,
            guild_id=str(guild_id),
            groups=settings_view.build_groups(settings, roles, channels, panel),
            premium=settings.get("premium") or {},
            names_resolved=roles is not None and channels is not None,
            auto_verify_column_present=settings.get("auto_verify_column_present", True),
            saved=request.args.get("saved") == "1",
            save_error=_save_error_message(request.args.get("error")),
            csrf_token=session.csrf_token,
        )

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

        submitted = request.form.get("csrf_token", "")
        if not secrets.compare_digest(session.csrf_token, submitted):
            abort(400)

        changes = {}

        locale = request.form.get("instructions_locale")
        if locale:
            changes["instructions_locale"] = locale

        if request.form.get("panel_fields_present"):
            # A checkbox that is off sends nothing at all, so the form declares
            # that its branding controls were on the page. Without this, an
            # unticked "show icon" would be indistinguishable from a free
            # server whose controls were never rendered, and the save would
            # quietly turn the icon off.
            changes["panel_show_icon"] = bool(request.form.get("panel_show_icon"))
            if request.form.get("panel_color_default"):
                changes["panel_embed_color"] = None
            else:
                changes["panel_embed_color"] = _colour_to_int(
                    request.form.get("panel_embed_color")
                )

        if not changes:
            return redirect(url_for("guild_settings", guild_id=guild_id))

        try:
            _bot_api().update_settings(int(session.discord_id), guild_id, changes)
        except BotAPIError as error:
            logger.warning("save refused for guild %s: %s", guild_id, error)
            # A code, never the bot's text. What comes back is a fixed reason
            # string today, but round-tripping it through a URL and into a page
            # would make the bot's error strings part of this app's HTML, and
            # the day one of them carries something a caller influenced is not
            # the day to find that out.
            return redirect(
                url_for(
                    "guild_settings",
                    guild_id=guild_id,
                    error=_save_error_code(error),
                )
            )

        return redirect(url_for("guild_settings", guild_id=guild_id, saved=1))

    @app.post("/logout")
    def logout():
        session = _require_login()
        if session is not None:
            submitted = request.form.get("csrf_token", "")
            if not secrets.compare_digest(session.csrf_token, submitted):
                abort(400)
            _store().destroy(session.sid)
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
}
GENERIC_SAVE_ERROR = "That change couldn't be saved, so nothing was changed."


def _save_error_code(error: BotAPIError) -> str:
    reason = str(error)
    return reason if reason in SAVE_ERRORS else "unknown"


def _save_error_message(code: Optional[str]) -> Optional[str]:
    """Copy for a refusal, chosen by us -- the code is only ever a lookup key."""
    if not code:
        return None
    return SAVE_ERRORS.get(code, GENERIC_SAVE_ERROR)


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


def _settings_unavailable(error: BotAPIError, guild_id: int, session):
    """Turn a refusal into a page, without saying which refusal it was.

    The bot distinguishes 404 "not in that guild" from 403 "you do not
    administer it", which is right inside the mTLS boundary and wrong on the
    open web. Rendered differently, a signed-in user could walk arbitrary guild
    ids and learn which servers run VRCVerify -- a census of communities
    operating 18+ gating, from a browser, with nothing compromised. It is the
    same oracle handle_list_guilds was hardened against, arriving by a
    different door.

    503 is kept separate: it says the bot cannot answer right now, which
    discloses nothing about any particular guild, and telling an admin to try
    again is far better than telling them the server does not exist.
    """
    if error.status in (403, 404):
        logger.info(
            "settings page refused for actor=%s guild=%s (status %s)",
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

    logger.warning("settings read failed for guild %s: %s", guild_id, error)
    return (
        render_template(
            "error.html",
            message=(
                "Can't reach the bot right now, so this server's settings can't "
                "be shown. Nothing has changed. Try again shortly."
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
