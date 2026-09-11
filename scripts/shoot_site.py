"""Screenshot the apex site so somebody can look at it.

    python scripts/shoot_site.py [outdir]

THE THIRD OF THREE, and it was missing until #243's close-out. The dashboard
has shoot_pages.py and the status page has shoot_status.py, so the one surface
with no way to produce its own evidence was the marketing site: every page a
stranger sees before they decide to install anything.

That gap had a cost. #243's last acceptance criterion asked for screenshots of
every changed page, in both themes, attached to each sub-issue before it
closed. It was the one criterion missed, across all five, and part of the
reason is that producing the apex half meant writing this script first.

Starts scripts/dev_site.py itself, the way shoot_pages.py starts the dashboard
preview, so there is one command rather than two terminals. Set SITE_BASE to
shoot something already running, including the real https://vrcverify.com.

The theme is chosen the way a reader chooses it: by writing the same
localStorage key /theme.js reads. There is no server here to hold a cookie.

DELIBERATELY NOT A TEST, and Playwright stays out of requirements-dev.txt:

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
HOST, PORT = "127.0.0.1", 5002
BASE = os.environ.get("SITE_BASE", f"http://{HOST}:{PORT}")

# Every page the Worker serves. The 404 is deliberately a path that cannot
# exist rather than a link to 404.html: what is being checked is that the edge
# rule and the page agree, which is the thing dev_site.py exists to reproduce.
PAGES = [
    ("home", "/"),
    ("terms", "/terms"),
    ("privacy", "/privacy"),
    ("refunds", "/refunds"),
    ("changelog", "/changelog"),
    ("404", "/no-such-page"),
]

# Both themes explicitly, plus "system" left to the OS -- a third state and not
# a synonym for either. On this site the absence of an attribute means dark, so
# "system" is stored explicitly; see site/theme.js.
THEMES = [("dark", "dark", "dark"), ("light", "light", "light"), ("system", "system", "light")]

# Wide, phone, and one narrow enough to prove the header survives the brand,
# the nav, the status pill and the theme picker stacking.
VIEWPORTS = [("wide", 1100, 900), ("phone", 390, 900), ("narrow", 320, 900)]


def _wait_for_server(timeout: float = 20.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            urllib.request.urlopen(BASE + "/", timeout=1).read()
            return
        except urllib.error.HTTPError:
            return  # answering at all is enough
        except OSError:
            time.sleep(0.3)
    raise SystemExit(f"  nothing answering at {BASE}")


def main() -> None:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise SystemExit(
            "\n  This needs Playwright, which is deliberately not a project"
            " dependency:\n\n"
            "    pip install playwright && python -m playwright install chromium\n"
        )

    out = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else "/tmp/vrcverify-site-shots")
    out.mkdir(parents=True, exist_ok=True)

    server = None
    if "SITE_BASE" not in os.environ:
        server = subprocess.Popen(
            [sys.executable, str(REPO / "scripts" / "dev_site.py")],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    try:
        _wait_for_server()
        shots = 0
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            for theme, stored, scheme in THEMES:
                for label, width, height in VIEWPORTS:
                    context = browser.new_context(
                        viewport={"width": width, "height": height},
                        device_scale_factor=2,
                        color_scheme=scheme,
                    )
                    context.add_init_script(
                        f"try {{ localStorage.setItem('vrcverify-theme', '{stored}'); }}"
                        " catch (e) {}"
                    )
                    page = context.new_page()
                    for name, path in PAGES:
                        page.goto(BASE + path, wait_until="networkidle")
                        page.screenshot(
                            path=str(out / f"site-{name}-{theme}-{label}.png"),
                            full_page=True,
                        )
                        shots += 1
                    context.close()
            browser.close()
        print(f"  {shots} screenshots in {out}")
    finally:
        if server is not None:
            server.terminate()
            server.wait(timeout=5)


if __name__ == "__main__":
    main()
