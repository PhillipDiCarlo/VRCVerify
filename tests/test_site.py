"""The apex site (issue #88, phase 10).

Four static pages with no build step, which is the right call for four
documents that rarely change -- but it means the header, the footer and the
contact address are copied four times and can drift four ways. Stripe, Discord
and the dashboard all link here, so a page that quietly stops naming the seller
or points at a dead link is a compliance problem rather than a cosmetic one.

These tests are the substitute for the template engine deliberately not used.
"""

import pathlib
import re
from html.parser import HTMLParser

import pytest

SITE = pathlib.Path(__file__).resolve().parent.parent / "site"
PAGES = sorted(SITE.glob("*.html"))
PAGE_NAMES = [p.name for p in PAGES]

CONTACT = "contact@esattotech.com"
ENTITY = "Esatto Technologies"

# Every page Stripe or Discord is configured to link to. Losing one of these
# breaks an external configuration nothing in this repo can see.
REQUIRED_PAGES = {"index.html", "terms.html", "privacy.html", "refunds.html"}

VOID = {
    "meta", "link", "br", "hr", "img", "input", "area",
    "base", "col", "embed", "source", "track", "wbr",
}


def read(page: pathlib.Path) -> str:
    return page.read_text(encoding="utf-8")


class _Tags(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack = []
        self.errors = []
        self.links = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            href = dict(attrs).get("href")
            if href:
                self.links.append(href)
        if tag not in VOID:
            self.stack.append(tag)

    def handle_endtag(self, tag):
        if tag in VOID:
            return
        if not self.stack or self.stack[-1] != tag:
            top = self.stack[-1] if self.stack else "nothing"
            self.errors.append(f"</{tag}> closes <{top}>")
        else:
            self.stack.pop()


def parse(page: pathlib.Path) -> _Tags:
    tags = _Tags()
    tags.feed(read(page))
    return tags


def test_the_pages_external_services_link_to_all_exist():
    """Stripe's portal and Discord's application settings hold these URLs.

    Renaming or deleting one is a change to configuration held in two places
    outside this repository, neither of which will complain.
    """
    assert REQUIRED_PAGES <= set(PAGE_NAMES)


@pytest.mark.parametrize("page", PAGES, ids=PAGE_NAMES)
def test_every_tag_is_closed(page):
    tags = parse(page)
    assert not tags.errors, f"{page.name}: {tags.errors}"
    assert not tags.stack, f"{page.name}: never closed {tags.stack}"


@pytest.mark.parametrize("page", PAGES, ids=PAGE_NAMES)
def test_internal_links_resolve(page):
    """A dead link in a footer copied onto four pages is dead four times."""
    available = set(PAGE_NAMES) | {"style.css"}
    dead = []
    for href in parse(page).links:
        if not href.startswith("/") or href.startswith("//"):
            continue
        # "/" is the landing page. Everything else is linked without the .html
        # -- see test_internal_links_are_canonical -- so both forms resolve to
        # the same file on disk.
        target = href.lstrip("/") or "index.html"
        if target not in available and f"{target}.html" not in available:
            dead.append(href)
    assert not dead, f"{page.name}: {dead}"


@pytest.mark.parametrize("page", PAGES, ids=PAGE_NAMES)
def test_internal_links_are_canonical(page):
    """No `.html` in an internal link, because Cloudflare redirects it away.

    Cloudflare's default `html_handling` is "auto-trailing-slash", which
    answers /terms.html with a 307 to /terms. Both work, so this is invisible
    until it matters -- and where it matters is the URLs pasted into Stripe's
    billing portal and Discord's application settings, which are long-lived,
    live outside this repository, and should point at the address that answers
    rather than the one that forwards.

    Verified against the live site on 2026-08-18: /terms.html -> 307 -> /terms.
    """
    internal = [
        href for href in parse(page).links
        if href.startswith("/") and not href.startswith("//")
    ]
    with_extension = [href for href in internal if href.endswith(".html")]
    assert not with_extension, f"{page.name}: {with_extension}"


@pytest.mark.parametrize("page", PAGES, ids=PAGE_NAMES)
def test_every_page_names_the_seller_and_a_way_to_reach_them(page):
    """The two things a consumer regulator, a card issuer and a disputing
    customer all look for first, and the two most likely to be dropped from a
    page written later than the others."""
    text = read(page)
    assert ENTITY in text
    assert CONTACT in text


@pytest.mark.parametrize("page", PAGES, ids=PAGE_NAMES)
def test_every_page_disclaims_affiliation(page):
    """VRChat and Discord are both named throughout. Implying endorsement by
    either is the kind of claim that gets a Discord application removed."""
    assert "Not affiliated with" in read(page)


@pytest.mark.parametrize("page", PAGES, ids=PAGE_NAMES)
def test_every_page_has_a_title_and_a_viewport(page):
    text = read(page)
    assert re.search(r"<title>.+?</title>", text, re.S), page.name
    assert 'name="viewport"' in text, page.name


def test_the_footer_is_identical_on_every_page():
    """No template engine, so this is what keeps four copies in step."""
    footers = {}
    for page in PAGES:
        match = re.search(r'<footer class="site">(.*?)</footer>', read(page), re.S)
        assert match, f"{page.name} has no site footer"
        footers[page.name] = match.group(1).strip()
    assert len(set(footers.values())) == 1, (
        "footers have drifted: " + ", ".join(sorted(footers))
    )


def test_the_header_nav_is_identical_on_every_page():
    headers = {}
    for page in PAGES:
        match = re.search(r'<header class="site">(.*?)</header>', read(page), re.S)
        assert match, f"{page.name} has no site header"
        headers[page.name] = match.group(1).strip()
    assert len(set(headers.values())) == 1, (
        "headers have drifted: " + ", ".join(sorted(headers))
    )


@pytest.mark.parametrize("page", PAGES, ids=PAGE_NAMES)
def test_nothing_is_loaded_from_a_third_party(page):
    """The reason these pages are static and dependency-free.

    A policy page that needs a CDN can be unavailable at the moment somebody
    needs to read it, and an external request from a privacy policy is its own
    small joke. `mailto:` and links to other sites are fine; *loading* from one
    is not.

    THIS USED TO BAN SCRIPTS OUTRIGHT -- `assert "<script" not in text` -- and
    #137 phase 1 narrowed it to third-party scripts, which is all this test's
    own docstring ever claimed. The blanket version was free to be stronger
    than its stated rule for as long as the site had no behaviour at all; the
    theme toggle is the first script here, it is served from this origin, and
    every page still renders completely without it.

    The property being defended is "nothing on this page is fetched from
    somebody else's server", not "this page has no behaviour". An inline
    <script> block is still refused: same-origin is checked by reading the
    `src`, so a script with no `src` has nothing to check, and keeping the one
    path from repository to browser a reviewable file is worth more than the
    convenience.
    """
    text = read(page)
    loaders = re.findall(r'(?:src|href)="(https?://[^"]+)"', text)
    stylesheets_and_scripts = [
        url
        for url in loaders
        if re.search(rf'<(?:link|script)[^>]*"{re.escape(url)}"', text)
    ]
    assert not stylesheets_and_scripts, f"{page.name}: {stylesheets_and_scripts}"

    for tag in re.findall(r"<script[^>]*>", text):
        src = re.search(r'src="([^"]+)"', tag)
        assert src, f"{page.name} has an inline script: {tag}"
        assert src.group(1).startswith("/"), (
            f"{page.name} loads a script from elsewhere: {src.group(1)}"
        )


# index.html and 404.html are not policies; the rest are, and a policy with no
# date cannot be shown to have been in force on a given day.
NOT_POLICIES = {"index.html", "404.html"}
POLICIES = [p for p in PAGES if p.name not in NOT_POLICIES]


@pytest.mark.parametrize("page", POLICIES, ids=[p.name for p in POLICIES])
def test_every_policy_says_when_it_was_last_updated(page):
    """A policy with no date cannot be shown to have been in force on a given
    day, which is the only question that ever gets asked about one."""
    assert re.search(r'class="updated">Last updated \d{1,2} \w+ \d{4}', read(page)), (
        f"{page.name} has no 'Last updated' line"
    )


def test_the_404_page_exists_because_wrangler_promises_it():
    """`not_found_handling = "404-page"` in wrangler.toml names this file.

    Delete it and Cloudflare falls back to a bare, unbranded 404 on a domain
    whose whole job is telling people where the terms are. Nothing at deploy
    time complains.
    """
    config = (SITE.parent / "wrangler.toml").read_text(encoding="utf-8")
    if 'not_found_handling = "404-page"' in config:
        assert (SITE / "404.html").exists(), (
            "wrangler.toml promises a 404 page and site/404.html is missing"
        )


def test_the_deployed_directory_is_the_one_these_tests_check():
    """These tests are worth nothing if wrangler publishes a different folder."""
    config = (SITE.parent / "wrangler.toml").read_text(encoding="utf-8")
    assert 'directory = "./site"' in config


def test_the_worker_serves_assets_only():
    """No `main`, so no code runs on a request to the apex.

    The whole argument for keeping the legal pages off the dashboard was that
    they should not share a failure domain or an attack surface with running
    code. A `main` here would quietly undo half of that.
    """
    config = (SITE.parent / "wrangler.toml").read_text(encoding="utf-8")
    mains = [
        line for line in config.splitlines()
        if line.strip().startswith("main") and "=" in line
    ]
    assert not mains, f"wrangler.toml declares a Worker script: {mains}"


# --------------------------------------------------------------------------
# Theming (#137 phase 1). Dark by default, with a three-way picker.
#
# The dashboard renders its theme attribute server-side, so a wrong first
# paint there is a server bug. Here there is no server: the default lives in
# the cascade, and these tests are what keep it there.
# --------------------------------------------------------------------------

STYLE = SITE / "style.css"
THEME_JS = SITE / "theme.js"
THEMES = {"dark", "light", "system"}


@pytest.mark.parametrize("page", PAGES, ids=PAGE_NAMES)
def test_every_page_loads_the_theme_script(page):
    """Blocking and in <head>, or a stored Light choice flashes dark first.

    `defer` or `async` would let the body paint before the attribute is
    stamped, which is the exact flash the script exists to prevent.
    """
    text = read(page)
    head = re.search(r"<head>(.*?)</head>", text, re.S)
    assert head, f"{page.name} has no head"
    tag = re.search(r'<script[^>]*src="/theme\.js"[^>]*>', head.group(1))
    assert tag, f"{page.name} does not load /theme.js in its head"
    assert "defer" not in tag.group(0), f"{page.name} defers the theme script"
    assert "async" not in tag.group(0), f"{page.name} loads the theme script async"


@pytest.mark.parametrize("page", PAGES, ids=PAGE_NAMES)
def test_the_theme_picker_ships_hidden(page):
    """A control that needs JavaScript must not be painted before it arrives.

    With the script blocked the page is simply dark, which is a complete
    experience. A visible select that did nothing would not be.
    """
    picker = re.search(r'<div class="theme-picker"([^>]*)>', read(page))
    assert picker, f"{page.name} has no theme picker"
    assert "hidden" in picker.group(1), f"{page.name}'s theme picker is not hidden"


@pytest.mark.parametrize("page", PAGES, ids=PAGE_NAMES)
def test_the_picker_offers_exactly_the_three_themes(page):
    """The markup and `VALID` in theme.js have to agree.

    An option the script rejects would silently do nothing when chosen; a
    theme the script accepts with no option is one nobody can reach.
    """
    options = set(re.findall(r'<option value="([^"]+)"', read(page)))
    assert options == THEMES, f"{page.name}: {sorted(options)}"


def test_the_script_accepts_exactly_the_themes_the_markup_offers():
    valid = re.search(r'VALID = \[([^\]]+)\]', THEME_JS.read_text(encoding="utf-8"))
    assert valid, "theme.js has no VALID list"
    assert set(re.findall(r'"([^"]+)"', valid.group(1))) == THEMES


def test_dark_is_the_floor_rather_than_a_media_query():
    """The default has to survive JavaScript being off.

    `:root` with no attribute is what a first visit renders, and what every
    visit renders for a reader with scripting disabled. If dark lived only
    behind `prefers-color-scheme: dark`, a light-OS visitor would get a light
    page and the epic's "dark is what a first-time visitor sees" would be
    false for them.
    """
    css = STYLE.read_text(encoding="utf-8")
    floor = re.search(r"^:root,\n:root\[data-theme=\"dark\"\] \{(.*?)^\}", css, re.S | re.M)
    assert floor, "style.css has no bare-:root dark floor"
    assert "color-scheme: dark" in floor.group(1)
    assert "--bg: #1e1f22" in floor.group(1), "the floor is not painting dark values"


def test_light_is_reachable_both_explicitly_and_through_system():
    """Three states, and `system` has to be an attribute rather than an absence.

    Absence means dark here -- it is the pre-JavaScript state -- so unlike the
    dashboard, "follow the OS" needs a value of its own to be expressible.
    """
    css = STYLE.read_text(encoding="utf-8")
    assert ':root[data-theme="light"] {' in css
    assert "@media (prefers-color-scheme: light)" in css
    assert ':root[data-theme="system"]' in css


def test_the_stylesheets_cross_reference_each_other():
    """The tokens are duplicated on purpose; the comments are what keep the
    duplication honest. Undocumented duplication is how two surfaces drift
    into looking like two products -- see #137.
    """
    assert "src/dashboard/static/style.css" in STYLE.read_text(encoding="utf-8"), (
        "site/style.css does not say where the other copy of the tokens lives"
    )


def test_the_site_loads_no_script_other_than_its_own():
    """One script, and it is this one. The site's dependency-free claim is
    only as good as the list of things it fetches."""
    for page in PAGES:
        srcs = re.findall(r'<script[^>]*src="([^"]+)"', read(page))
        assert srcs == ["/theme.js"], f"{page.name}: {srcs}"


def _luminance(hex_colour):
    hex_colour = hex_colour.lstrip("#")
    channels = []
    for i in (0, 2, 4):
        c = int(hex_colour[i:i + 2], 16) / 255
        channels.append(c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4)
    r, g, b = channels
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _contrast(a, b):
    la, lb = _luminance(a), _luminance(b)
    return (max(la, lb) + 0.05) / (min(la, lb) + 0.05)


def _token(name):
    """The value a token is declared with, read straight out of the stylesheet."""
    match = re.search(rf"^\s*{re.escape(name)}:\s*(#[0-9a-f]{{6}});", 
                      STYLE.read_text(encoding="utf-8"), re.M | re.I)
    assert match, f"{name} is not declared with a literal in style.css"
    return match.group(1)


def test_the_theme_picker_has_a_visible_edge_in_both_themes():
    """WCAG 1.4.11: a control has to be identifiable as a control, at 3:1
    against what surrounds it.

    This is pinned because it already failed once. The select was drawn with
    `--line`, which is a *separator* token -- 1.35:1 against the header bar on
    dark and 1.19:1 on light, an edge nobody could see. `--control-line` is the
    token for the job, and on light it had to be darkened from the dashboard's
    #8d919b (2.84:1 on this site's --chrome) because the dashboard measured it
    against white cards rather than against a chrome bar.

    Both surfaces matter: the bar the control sits ON, and the fill it
    surrounds. An edge that vanishes into either one is not an edge.
    """
    for theme, edge, chrome, panel in (
        ("dark", _token("--control-line"), _token("--chrome"), _token("--panel")),
        ("light", _token("--light-control-line"),
         _token("--light-chrome"), _token("--light-panel")),
    ):
        against_bar = _contrast(edge, chrome)
        against_fill = _contrast(edge, panel)
        assert against_bar >= 3.0, (
            f"{theme}: control edge {edge} is {against_bar:.2f}:1 on the "
            f"header bar {chrome}, under the 3:1 a UI component needs"
        )
        assert against_fill >= 3.0, (
            f"{theme}: control edge {edge} is {against_fill:.2f}:1 against the "
            f"fill it surrounds {panel}"
        )


def test_the_picker_label_is_readable_in_both_themes():
    """`--faint` is the quietest thing on the page and is still text: 4.5:1."""
    for theme, faint, chrome in (
        ("dark", _token("--faint"), _token("--chrome")),
        ("light", _token("--light-faint"), _token("--light-chrome")),
    ):
        ratio = _contrast(faint, chrome)
        assert ratio >= 4.5, f"{theme}: label {faint} on {chrome} is {ratio:.2f}:1"
