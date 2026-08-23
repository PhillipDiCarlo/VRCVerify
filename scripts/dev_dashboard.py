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

import os
import pathlib
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

# Fake, and structured so nothing here could be mistaken for a real value.
# The two keys are different from each other because config.py refuses to start
# when they match -- one signs cookies, the other authorises calls into the
# homelab, and sharing them turns a cookie bug into an API-forgery bug.
os.environ.update(
    DISCORD_CLIENT_ID="000000000000000000",
    DISCORD_CLIENT_SECRET="local-preview-not-a-real-secret",
    OAUTH_REDIRECT_URI="http://127.0.0.1:5001/callback",
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


if __name__ == "__main__":
    print("\n  Dashboard preview: http://127.0.0.1:5001/")
    print("  Sign-in page only -- anything past it needs a real bot.\n")
    print("  Theme (devtools console, then reload):")
    print('    document.cookie = "vrcverify_theme=dark;   path=/"')
    print('    document.cookie = "vrcverify_theme=light;  path=/"')
    print('    document.cookie = "vrcverify_theme=system; path=/"')
    print('    document.cookie = "vrcverify_theme=; path=/; max-age=0"   (clear)\n')
    # 127.0.0.1 rather than 0.0.0.0: this binds to the loopback only, so it is
    # not reachable from the network even on an untrusted one.
    app.run(host="127.0.0.1", port=5001, debug=False, use_reloader=False)
