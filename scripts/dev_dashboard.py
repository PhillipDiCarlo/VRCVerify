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

`BOT_API_URL` points at a closed port on purpose. Anything past the sign-in
page needs a real session and a reachable bot, and will fail here; that is the
intended blast radius, not a bug to fix.

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
    STRIPE_ENABLED="0",
)

from dashboard.app import create_app  # noqa: E402  (after os.environ is set)

app = create_app()

# Templates re-read from disk on every render. The process reloader is left
# OFF deliberately: it forks, and a forked process drops the debugger, so
# breakpoints stop working exactly when you want them. This gets the useful
# half -- edit a template, refresh the browser -- without that cost.
app.config["TEMPLATES_AUTO_RELOAD"] = True
app.jinja_env.auto_reload = True


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
    print("  Sign-in page only -- anything past it needs a real bot.\n")
    print("  Theme (devtools console, then reload):")
    print('    document.cookie = "vrcverify_theme=dark;   path=/"')
    print('    document.cookie = "vrcverify_theme=light;  path=/"')
    print('    document.cookie = "vrcverify_theme=system; path=/"')
    print('    document.cookie = "vrcverify_theme=; path=/; max-age=0"   (clear)\n')
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
