"""Screenshot the dashboard's pages so somebody can look at them.

Nine pull requests across epic #122 shipped with the words "not verified in a
browser" in them, because this repo's UI work was being checked by reading CSS
and rendered markup. That is enough to catch a wrong token or a missing
attribute and not nearly enough to catch a glyph that is illegible at the size
it is drawn -- which is exactly what the first run of this script found.

    python scripts/shoot_pages.py [outdir]

Drives scripts/dev_dashboard.py, so every guarantee that file makes holds here
too: no .env, fake credentials, loopback only, a stubbed bot. Nothing here
talks to anything real.

DELIBERATELY NOT A TEST, AND NOT IN requirements-dev.txt.
Playwright needs a ~100MB browser download. Making the suite depend on that
would mean every checkout and every CI run pays for it to assert things a
human still has to look at anyway. Screenshots are for looking at; the suite
asserts what can be asserted. Install it yourself when you need this:

    pip install playwright && python -m playwright install chromium
"""

from __future__ import annotations

import os
import pathlib
import subprocess
import sys
import time
import urllib.error
import urllib.request

REPO = pathlib.Path(__file__).resolve().parent.parent
HOST, PORT = "127.0.0.1", 5001
BASE = f"http://{HOST}:{PORT}"

# The preview's four invented servers; see scripts/preview_bot.py.
PREMIUM = "700000000000000001"
FREE = "700000000000000002"

# The signed-in preview is held behind a per-run token (#162), and this script
# sends the server's banner to /dev/null -- so it chooses the token instead of
# reading it. Fixed rather than random on purpose: nothing here is a secret
# from anybody, and a constant keeps a failed run reproducible by hand.
PREVIEW_TOKEN = "shoot-pages-not-a-real-token"

# What to shoot, and which preview mode reaches it. A page nobody can see is a
# page nobody has checked, so the outage and signed-out states are in here on
# the same footing as the ordinary ones.
PAGES = [
    ("signin", "/", {"PREVIEW_SIGNED_IN": "0"}),
    ("picker", "/", {}),
    ("overview", f"/guild/{PREMIUM}", {}),
    ("settings", f"/guild/{PREMIUM}/settings", {}),
    ("settings-free", f"/guild/{FREE}/settings", {}),
    ("subscription", f"/guild/{PREMIUM}/subscription", {}),
    ("refusal", f"/guild/{PREMIUM}", {"PREVIEW_BOT_DOWN": "1"}),
]

# Both themes explicitly, plus "system" left to the OS preference -- which is a
# third state and not a synonym for either, because it is the one that renders
# with no data-theme attribute at all.
THEMES = [
    ("light", "light", "light"),
    ("dark", None, "dark"),
    ("system", None, "dark"),
]

VIEWPORTS = [("wide", 1100, 900), ("phone", 375, 900)]


def _wait_for_server(timeout: float = 20.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            urllib.request.urlopen(BASE + "/", timeout=1).read()
            return
        except urllib.error.HTTPError:
            return  # answering at all is enough
        except OSError:
            time.sleep(0.25)
    raise SystemExit(f"  the preview never came up on {BASE}")


def main() -> None:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise SystemExit(
            "\n  This needs Playwright, which is deliberately not a project"
            " dependency:\n\n"
            "    pip install playwright && python -m playwright install chromium\n"
        )

    out = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else "/tmp/vrcverify-shots")
    out.mkdir(parents=True, exist_ok=True)

    shot_count = 0
    for env_key in {tuple(sorted(env.items())) for _n, _p, env in PAGES}:
        env = dict(os.environ, PREVIEW_TOKEN=PREVIEW_TOKEN, **dict(env_key))
        here = [(n, p) for n, p, e in PAGES if tuple(sorted(e.items())) == env_key]
        server = subprocess.Popen(
            [sys.executable, str(REPO / "scripts" / "dev_dashboard.py")],
            env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        try:
            _wait_for_server()
            with sync_playwright() as pw:
                browser = pw.chromium.launch()
                for name, path in here:
                    for theme, cookie, scheme in THEMES:
                        for label, width, height in VIEWPORTS:
                            context = browser.new_context(
                                viewport={"width": width, "height": height},
                                device_scale_factor=2,
                                color_scheme=scheme,
                            )
                            # Every context is a fresh cookie jar, so the
                            # preview token goes in each one rather than being
                            # exchanged once through the query string -- these
                            # pages are opened as deep links, not walked to.
                            jar = [{
                                "name": "vrcverify_preview",
                                "value": PREVIEW_TOKEN,
                                "domain": HOST, "path": "/",
                            }]
                            if cookie:
                                jar.append({
                                    "name": "vrcverify_theme", "value": cookie,
                                    "domain": HOST, "path": "/",
                                })
                            context.add_cookies(jar)
                            page = context.new_page()
                            page.goto(BASE + path, wait_until="networkidle")
                            page.screenshot(
                                path=str(out / f"{name}-{theme}-{label}.png"),
                                full_page=True,
                            )
                            context.close()
                            shot_count += 1
                browser.close()
        finally:
            # Always, and before the next mode starts -- two of these fighting
            # over port 5001 is a confusing way to find out you left one behind.
            server.terminate()
            server.wait()

    print(f"\n  {shot_count} screenshots in {out}\n")


if __name__ == "__main__":
    main()
