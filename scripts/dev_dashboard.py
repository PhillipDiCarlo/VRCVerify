"""Run the dashboard locally, for looking at it.

    Run and Debug -> "Dashboard (local preview)"      or
    .venv/bin/python scripts/dev_dashboard.py

    -> http://127.0.0.1:5001/

WHAT THIS IS FOR, AND WHAT IT IS NOT

It exists so a change to a template or a stylesheet can be *seen* before it is
merged. It is not a deployment path, not a staging environment, and not a way
to work against real data. The dashboard proper is served by gunicorn behind
the cloudflared tunnel; see docker/Dockerfile-dashboard and VPS_RUNBOOK.md.

EVERY CREDENTIAL BELOW IS FAKE, AND THAT IS THE POINT

The sign-in page renders before any session exists, so it calls neither
Discord nor the bot. That is what lets this run with placeholder values, and
it is why the placeholders are deliberately non-functional rather than merely
absent -- a config that half-works against real infrastructure is worse than
one that cannot reach it at all.

`BOT_API_URL` points at a closed port on purpose, and with the stub below in
place nothing ever calls it -- see scripts/preview_bot.py. The preview is
therefore *less* able to reach production than it was, not more.

SIGNED IN OR SIGNED OUT
-----------------------
Signed in by default, because almost everything left to restyle lives behind
the sign-in page. Set PREVIEW_SIGNED_IN=0 for the sign-in page itself, which
#134 redesigns; there is a Run and Debug entry for each.

Signed in, the session is injected on the request rather than handed to the
browser as a cookie, so signing out does nothing here. That is the trade for
not having to walk the OAuth flow against a Discord that would refuse these
credentials anyway. To look at the signed-out pages, use the other entry.

**This file never reads .env.** The repository root has one, it holds real
credentials, and loading it would silently turn a preview into a client of
production Discord and the live bot API. If you need to exercise a signed-in
page, do it against a real deployment or with the test suite, not by pointing
this at live secrets.
"""

from __future__ import annotations

import errno
import os
import pathlib
import socket
import subprocess
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

# Loopback rather than 0.0.0.0: this binds where the network cannot reach it,
# so running it on an untrusted wifi exposes nothing. The port is named once
# because three things have to agree on it -- the OAuth redirect below, the
# bind, and the banner. Changing PORT here is enough: Run and Debug opens
# whatever URL the banner prints, so .vscode/launch.json needs no edit.
HOST = "127.0.0.1"
PORT = 5001

# Fake, and structured so nothing here could be mistaken for a real value.
# The two keys are different from each other because config.py refuses to start
# when they match -- one signs cookies, the other authorises calls into the
# homelab, and sharing them turns a cookie bug into an API-forgery bug.
os.environ.update(
    DISCORD_CLIENT_ID="000000000000000000",
    DISCORD_CLIENT_SECRET="local-preview-not-a-real-secret",
    OAUTH_REDIRECT_URI=f"http://{HOST}:{PORT}/callback",
    DASHBOARD_SECRET_KEY="local-preview-cookie-key-" + "x" * 40,
    BOT_API_TOKEN_SIGNING_KEY="local-preview-signing-key-" + "y" * 40,
    # Closed port, chosen so a stray call fails immediately and loudly rather
    # than hanging or, worse, reaching something real.
    BOT_API_URL="https://127.0.0.1:9",
    BOT_API_CLIENT_CERT=str(REPO / "certs/dashboard.pem"),
    BOT_API_CLIENT_KEY=str(REPO / "certs/dashboard.key"),
    BOT_API_CA=str(REPO / "certs/ca.pem"),
    # Not the deployed path, and not inside the repo.
    SESSION_DB_PATH="/tmp/vrcverify-preview-sessions.db",
    # On, unlike the bot API above, and stubbed rather than closed off. The
    # plan cards are most of what #141 restyles and the kill switch being off
    # hides them completely -- a preview that cannot draw the page's main
    # content is not much of a preview. `_PreviewPrices` below is what answers;
    # nothing here reaches Stripe.
    STRIPE_ENABLED="1",
    STRIPE_SECRET_KEY="sk_test_preview_not_a_real_key",
    STRIPE_PRODUCT_ID="prod_preview",
    STRIPE_WEBHOOK_SECRET="whsec_preview",
    # #138's rows in the bell panel and at the foot of the changelog page
    # render only when this is set, so the preview sets it -- a preview that
    # cannot draw the thing being reviewed is not much of a preview.
    #
    # Deliberately not the real invite, on this file's standing rule that every
    # value here is fake. Only the shape matters for looking at it: the scheme
    # check has to pass, and the link text is what is being judged.
    SUPPORT_INVITE_URL="https://discord.gg/preview-not-a-real-invite",
)

from flask import g  # noqa: E402
from dashboard.app import create_app  # noqa: E402  (after os.environ is set)

# Signed in unless told otherwise: the sign-in page is one of the few surfaces
# that renders without a session, and everything else needs one.
PREVIEW_SIGNED_IN = os.environ.get("PREVIEW_SIGNED_IN", "1") != "0"

if PREVIEW_SIGNED_IN:
    from preview_bot import ACTOR, GUILDS, PreviewBotAPI  # noqa: E402

    app = create_app(client=PreviewBotAPI())
else:
    app = create_app()

# Templates re-read from disk on every render. The process reloader is left
# OFF deliberately: it forks, and a forked process drops the debugger, so
# breakpoints stop working exactly when you want them. This gets the useful
# half -- edit a template, refresh the browser -- without that cost.
app.config["TEMPLATES_AUTO_RELOAD"] = True
app.jinja_env.auto_reload = True


# --- the plans, without Stripe -------------------------------------------
#
# `_offered_plans()` asks `app.config["STRIPE_PRICES"]` for the price list and
# turns a StripeAPIError into `plans_unavailable`, which is a DIFFERENT state
# from an empty list -- "we cannot tell what there is to sell" versus "there is
# nothing to sell". Both are rendered differently and both are worth being able
# to look at, so this stub can do either.
#
# It replaces the cache object rather than the client so the real
# `plans_from_prices` still runs: what the preview draws is built by the same
# code the deployed page uses, from prices shaped the way Stripe sends them.
class _PreviewPrices:
    """Stands in for `_PriceCache`. Same one-method surface: `.get(loader, now)`.

    Takes its prices as an argument rather than reading a module global. The
    first version closed over names that only exist inside the signed-in
    branch below, so the class was fine in practice and a NameError waiting
    for anybody who moved it.
    """

    def __init__(self, prices, unavailable: bool = False):
        self._prices = prices
        self._unavailable = unavailable

    def get(self, loader, now=None):
        if self._unavailable:
            from dashboard.stripe_api import StripeAPIError

            raise StripeAPIError("preview: pretending Stripe is unreachable")
        return self._prices


# --- checkout and the portal, without an account ------------------------
#
# THIS EXISTS FOR ONE CHECK THAT TESTS CANNOT MAKE (#141 phase 3).
#
# `form-action` is `'self'` plus Stripe's two hosted pages, and it governs
# where a submission may end up INCLUDING AFTER A REDIRECT. Both routes answer
# a POST with a 303 to Stripe. Get the policy wrong and the browser sends the
# request, receives the redirect, and silently refuses to follow it: no
# navigation, no error page, nothing in the server log. The button simply does
# nothing. That was the "Subscribe does nothing" bug of 2026-08-15.
#
# A test cannot catch it, because there is no server-side evidence to assert
# on -- the refusal happens entirely inside the browser. What CAN catch it is a
# real browser following a real 303 to a real `checkout.stripe.com` URL, which
# is exactly what this returns. No key, no account, no network: the CSP does
# not care whether the page at the other end exists, only what origin it is
# on.
class _PreviewStripe:
    """Stands in for `StripeClient` for the two redirect-producing calls."""

    def create_checkout_session(self, **kwargs) -> str:
        # A real Stripe origin with an obviously fake session id. Following it
        # lands on Stripe's own "this session does not exist" page, which is
        # the correct outcome: the navigation is the thing being tested.
        return "https://checkout.stripe.com/c/pay/cs_test_preview_not_a_real_session"

    def create_portal_session(self, **kwargs) -> str:
        return "https://billing.stripe.com/p/session/preview_not_a_real_session"


if PREVIEW_SIGNED_IN:
    from preview_bot import PREVIEW_SUB  # noqa: E402
    from test_subscription_page import PRICES as PREVIEW_PRICES  # noqa: E402

    app.config["STRIPE_PRICES"] = _PreviewPrices(
        PREVIEW_PRICES, unavailable=PREVIEW_SUB == "none"
    )
    app.config["STRIPE"] = _PreviewStripe()


if PREVIEW_SIGNED_IN:
    # A real Session row, built by the real store, rather than a hand-made
    # object: the store is a local SQLite file the preview already owns, and
    # letting it construct the session means this cannot drift if the dataclass
    # gains a field.
    _store = app.config["STORE"]
    _preview_session = _store.complete_login(
        _store.begin_login("preview-not-a-real-oauth-state").sid, ACTOR, GUILDS
    )

    @app.before_request
    def _sign_in():
        """Put the session on `g` directly, without a cookie.

        Registered after create_app, so it runs after the app's own
        `load_session` hook and overwrites what that found -- which is nothing,
        because no browser here has a session cookie.

        Handing the browser a real cookie was the alternative. It would make
        sign-out work, at the cost of a session that expires mid-session and
        cannot be renewed without an OAuth round trip Discord would refuse. A
        preview that logs you out after an hour of styling is worse than one
        where the sign-out button is inert.
        """
        g.session = _preview_session


# A deliberately BROKEN policy, for one purpose: proving that the check which
# verifies the Stripe redirect can actually fail. `form-action` refusing a
# redirect leaves no server-side evidence, so the only honest way to trust a
# green result is to watch the same check go red against a policy with Stripe
# taken out. Off unless asked for, and it only ever removes permissions.
if os.environ.get("PREVIEW_BREAK_FORM_ACTION") == "1":
    import re as _re

    import dashboard.app as _app_module

    # The MODULE CONSTANT, not an after_request hook. The app sets this header
    # in a hook of its own registered inside create_app, and Flask runs
    # after_request handlers in reverse registration order -- so anything added
    # out here runs first and is then overwritten. The first version of this
    # did exactly that, and the control it exists to provide came back green
    # against a policy that had not actually changed.
    _app_module.CSP = _re.sub(
        r"form-action[^;]*", "form-action 'self'", _app_module.CSP
    )


@app.after_request
def _drop_secure_from_cookies(response):
    """Let the browser actually keep the cookies this app sets.

    Every cookie here is `Secure`, which is right: in production the dashboard
    is HTTPS-only behind the tunnel. But this preview is plain HTTP on
    loopback, and a browser asked to store a `Secure` cookie over `http://`
    may simply decline -- Safari always does, and Chrome does in some
    configurations. The request succeeds, the redirect happens, the page
    reloads, and nothing changes, which reads exactly like a broken feature.

    It is worth knowing that curl does NOT behave this way: it keeps and
    replays `Secure` cookies over http regardless, so an automated round-trip
    through curl passes against a preview a real browser cannot use. That gap
    is how the theme picker looked verified and still failed on first click.

    Stripped here and only here. The flag itself is pinned by the test suite,
    so production keeps it and this cannot quietly become the real behaviour.

    The alternative -- serving the preview over HTTPS with the repo's certs --
    tests the real flags but puts a browser warning in front of every reload.
    Not worth it for looking at a stylesheet.
    """
    cookies = response.headers.getlist("Set-Cookie")
    if cookies:
        del response.headers["Set-Cookie"]
        for cookie in cookies:
            response.headers.add(
                "Set-Cookie",
                "; ".join(
                    part
                    for part in cookie.split("; ")
                    if part.strip().lower() != "secure"
                ),
            )
    return response


@app.after_request
def _never_cache(response):
    """Make every reload a genuine cold load.

    `_register_assets` marks static responses immutable for a year, which is
    right in production and useless here: a 304 on the stylesheet would mean
    looking at the previous version of the change you just made. It also makes
    "is there a flash of the wrong theme on first paint" a question this can
    actually answer.
    """
    response.headers["Cache-Control"] = "no-store, max-age=0"
    return response


def _port_is_free(host: str, port: int) -> bool:
    """Ask the kernel whether we could bind, without keeping the socket.

    `SO_REUSEADDR` is set because werkzeug sets it, and the whole point of
    this check is to predict what werkzeug is about to do. Without it a socket
    left in TIME_WAIT by a preview stopped seconds ago would look occupied,
    and we would refuse to start a server that would in fact have started
    fine -- a false alarm is worse than the error message it replaces.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((host, port))
        except OSError as exc:
            if exc.errno in (errno.EADDRINUSE, errno.EACCES):
                return False
            raise
    return True


def _listeners(port: int) -> list[tuple[str, str]]:
    """Best-effort (pid, command) for whatever is listening on the port.

    Best-effort in the strict sense: `lsof` may be missing, may be a different
    `lsof` on another platform, or may return nothing because the owner
    belongs to another user. Every one of those ends as an empty list and a
    slightly less specific message -- none of them is allowed to turn a
    diagnostic into a second failure, which is why this catches broadly and
    the caller treats the result as optional.
    """
    def _run(args: list[str]) -> str:
        try:
            done = subprocess.run(
                args, capture_output=True, text=True, timeout=3, check=False
            )
        except (OSError, subprocess.SubprocessError):
            return ""
        return done.stdout

    pids = [
        pid
        for pid in _run(["lsof", "-ti", f"tcp:{port}", "-sTCP:LISTEN"]).split()
        if pid.isdigit()
    ]
    return [
        (pid, " ".join(_run(["ps", "-o", "command=", "-p", pid]).split()))
        for pid in dict.fromkeys(pids)
    ]


def _explain_port_in_use(port: int) -> None:
    """Say what is wrong and what to type, instead of a bare traceback.

    The failure this replaces is `OSError: [Errno 48] Address already in use`
    at the bottom of a werkzeug stack, which says nothing about *what* holds
    the port -- and the answer is nearly always an earlier copy of this very
    script, because closing the browser tab does not stop a server and a run
    started from a terminal outlives the Run and Debug stop button.
    """
    owners = _listeners(port)
    mine = [(pid, cmd) for pid, cmd in owners if "dev_dashboard.py" in cmd]

    print(f"\n  Port {port} is in use, so the preview did not start.\n", file=sys.stderr)

    if mine:
        print("  An earlier preview is still running:", file=sys.stderr)
        for pid, cmd in mine:
            print(f"    pid {pid}  {cmd}", file=sys.stderr)
        print(f"\n  Stop it with:\n    kill {' '.join(pid for pid, _ in mine)}\n",
              file=sys.stderr)
    elif owners:
        # Deliberately no `kill` line here. We did not start this and have no
        # idea what it is; handing over a command to kill an unidentified
        # process is how a preview script eats someone's database.
        print("  Something that is not a preview is holding it:", file=sys.stderr)
        for pid, cmd in owners:
            print(f"    pid {pid}  {cmd or '(command unavailable)'}", file=sys.stderr)
        print("\n  Stop that, or change PORT near the top of this file -- Run and\n"
              "  Debug follows the URL in the banner, so nothing else needs editing.\n",
              file=sys.stderr)
    else:
        # lsof told us nothing, but the bind still failed -- so the port is
        # genuinely taken by a process we cannot see, usually another user's.
        print("  Nothing could be identified as the owner -- it may belong to\n"
              "  another user. To look yourself:\n"
              f"    lsof -i tcp:{port}\n", file=sys.stderr)


if __name__ == "__main__":
    # Checked before the banner rather than after: `serverReadyAction` in
    # launch.json opens a browser at the first URL this prints, and printing
    # one we are about to fail to serve sends VS Code to a dead tab.
    if not _port_is_free(HOST, PORT):
        _explain_port_in_use(PORT)
        raise SystemExit(1)

    print(f"\n  Dashboard preview: http://{HOST}:{PORT}/")
    if PREVIEW_SIGNED_IN:
        from preview_bot import FREE, PREMIUM, UNREACHABLE

        print("  Signed in against a stub. Every server below is invented.\n")
        print(f"    premium      /guild/{PREMIUM}/settings")
        print(f"    free         /guild/{FREE}/settings")
        print(f"    always down  /guild/{UNREACHABLE}         (error.html)")
        print("    not added    on the picker at /\n")
        if os.environ.get("PREVIEW_BOT_DOWN") == "1":
            print("  THE BOT IS PRETENDING TO BE DOWN. Every card reads as")
            print("  unknown and nothing offers to install anything.\n")
        print("  Sign-out does nothing here; run the signed-out entry for that.\n")
    else:
        print("  Signed out -- the sign-in page only.\n")
    try:
        app.run(host=HOST, port=PORT, debug=False, use_reloader=False)
    except OSError as exc:
        # The check above is not a lock: something can take the port in the
        # moment between the two binds. Rare, but the traceback it produces is
        # the exact one this file exists to avoid, so handle it in both places.
        if exc.errno != errno.EADDRINUSE:
            raise
        _explain_port_in_use(PORT)
        raise SystemExit(1) from None
