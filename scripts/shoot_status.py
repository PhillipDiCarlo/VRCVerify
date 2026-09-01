"""Screenshot the status page so somebody can look at it.

    python scripts/shoot_status.py [outdir]

Points at a RUNNING status Worker, which is not started here because starting
it needs Docker and a wrangler install -- see status/README.md for the one
command that does it. Set STATUS_BASE to shoot something else, including the
real https://status.vrcverify.com once it is deployed.

WHY THIS EXISTS SEPARATELY FROM shoot_pages.py: that script drives the Flask
preview, which this page has nothing to do with. What the two share is the
reason either exists -- nine pull requests across epic #122 shipped saying "not
verified in a browser", and the first browser pass found a glyph that was
illegible at the size it was actually drawn. This page is four glyphs and a
colour, so it is exactly the kind of thing reading the CSS cannot check.

The theme is chosen the way a reader chooses it: by writing the same
localStorage key /theme.js reads. There is no server here to hold a cookie.

DELIBERATELY NOT A TEST, and Playwright stays out of requirements-dev.txt:

    pip install playwright && python -m playwright install chromium
"""

from __future__ import annotations

import os
import pathlib
import sys
import urllib.error
import urllib.request

BASE = os.environ.get("STATUS_BASE", "http://localhost:8787")

# Both themes explicitly, plus "system" left to the OS preference -- a third
# state and not a synonym for either. On this site the absence of an attribute
# means dark, so "system" is stored explicitly; see site/theme.js.
THEMES = [("dark", "dark", "dark"), ("light", "light", "light"), ("system", "system", "light")]

# Wide, phone, and one narrow enough to prove the row layout survives the
# brand, the nav and the theme picker stacking.
VIEWPORTS = [("wide", 1100, 1000), ("phone", 390, 900), ("narrow", 320, 900)]


def main() -> None:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise SystemExit(
            "\n  This needs Playwright, which is deliberately not a project"
            " dependency:\n\n"
            "    pip install playwright && python -m playwright install chromium\n"
        )

    try:
        urllib.request.urlopen(BASE, timeout=5).read()
    except (OSError, urllib.error.HTTPError) as exc:
        raise SystemExit(f"  nothing answering at {BASE}: {exc}\n  see status/README.md")

    out = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else "/tmp/vrcverify-status-shots")
    out.mkdir(parents=True, exist_ok=True)

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
                    f"try {{ localStorage.setItem('vrcverify-theme', '{stored}'); }} catch (e) {{}}"
                )
                page = context.new_page()
                page.goto(BASE, wait_until="networkidle")
                page.screenshot(path=str(out / f"status-{theme}-{label}.png"), full_page=True)

                # The state glyphs are 17px as drawn. Shot again at eight times
                # that, because "is this legible" is not a question a 17px
                # screenshot can answer -- which is the specific mistake the
                # first browser pass on this project caught.
                if theme == "dark" and label == "wide":
                    row = page.locator(".card .row").first
                    row.screenshot(path=str(out / "status-row-detail.png"))
                context.close()
        browser.close()

    print(f"  wrote {len(list(out.glob('*.png')))} shots to {out}")


if __name__ == "__main__":
    main()
