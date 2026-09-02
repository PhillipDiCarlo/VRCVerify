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
import sys
from html.parser import HTMLParser

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
SITE = ROOT / "site"
PAGES = sorted(SITE.glob("*.html"))
PAGE_NAMES = [p.name for p in PAGES]

def _status_tokens(path: pathlib.Path) -> dict[str, str]:
    """Every `--name: #hex;` declaration in a file, as a flat mapping.

    The same parser test_status_page.py uses, and flat for the same reason: a
    token declared twice with two different literals is precisely the drift
    being looked for, so the last one wins and the comparison fails rather
    than silently reading the first.
    """
    css = re.sub(r"/\*.*?\*/", "", path.read_text(encoding="utf-8"), flags=re.S)
    return {
        name: value.strip()
        for name, value in re.findall(r"(--[a-z0-9-]+)\s*:\s*(#[0-9a-fA-F]{3,8})\s*;", css)
    }

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
#
# `changelog.html` joined them in #137 phase 4 and is worth a word, because
# PAGES is a glob and every new page lands in every parametrised test here
# automatically. That is the good half. The bad half is this set: a page that
# is not a policy has to say so, or it is asked for a "Last updated" line it
# has no business carrying. The changelog dates every entry individually and a
# single date for the page would be meaningless.
NOT_POLICIES = {"index.html", "404.html", "changelog.html"}
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


def test_the_menu_offers_exactly_the_three_themes():
    """The offered list and `VALID` have to agree.

    An option the script rejects would silently do nothing when chosen; a
    theme the script accepts but never offers is one nobody can reach.

    Both lists live in theme.js since #195 phase 7 built the control there --
    the pages carry an empty placeholder now. That removes the markup half of
    this drift, and it does not remove the drift: OPTIONS and VALID are still
    two lists in one file.
    """
    script = THEME_JS.read_text(encoding="utf-8")
    block = re.search(r"var OPTIONS = \[(.*?)\];", script, re.S)
    assert block, "theme.js no longer declares the offered themes"
    offered = set(re.findall(r'\["([a-z]+)"', block.group(1)))
    assert offered == THEMES, sorted(offered)


@pytest.mark.parametrize("page", PAGES, ids=PAGE_NAMES)
def test_no_page_ships_a_theme_control_of_its_own(page):
    """The placeholder is the whole of it. A <select> or a button left behind
    on one page would be a second control the script does not know about, on a
    header that is meant to be byte-identical across six files."""
    body = read(page)
    assert "<option" not in body, f"{page.name} still ships theme options"
    assert "theme-select" not in body, f"{page.name} still ships the old picker"


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


# Both copies of the stylesheet. A brace defect in either one is silent.
STYLESHEETS = [STYLE, SITE.parent / "src" / "dashboard" / "static" / "style.css"]


def _brace_depth_errors(css: str):
    """Walk the braces, returning (line, message) for anything unbalanced."""
    # Blank the comments out in place rather than deleting them, so the line
    # numbers this reports are the line numbers in the file.
    css = re.sub(
        r"/\*.*?\*/", lambda m: "\n" * m.group(0).count("\n"), css, flags=re.S
    )
    errors, depth = [], 0
    for number, line in enumerate(css.split("\n"), start=1):
        for char in line:
            if char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth < 0:
                    errors.append((number, "a closing brace with nothing open"))
                    depth = 0
    if depth:
        errors.append((0, f"{depth} rule(s) left open at end of file"))
    return errors


@pytest.mark.parametrize("sheet", STYLESHEETS, ids=lambda s: s.name)
def test_the_stylesheet_braces_balance(sheet):
    """A stray `}` does not fail loudly. It eats the next rule.

    #190 phase 1 shipped two extra closing braces to `main`. Every browser
    recovers from them, so nothing looked broken and nothing failed -- but one
    of the two started a qualified rule whose prelude ran on into the next
    selector, and `.lede` was dropped from the parsed sheet in Chromium,
    Firefox and WebKit alike. The changelog and 404 ledes silently lost their
    size and their muted colour, and no test in this file could see it,
    because every test here reads the CSS as text and the text was still
    there.

    So this is the one CSS test that checks the file is a stylesheet at all,
    rather than checking what it says.
    """
    errors = _brace_depth_errors(sheet.read_text(encoding="utf-8"))
    assert not errors, "\n".join(
        f"{sheet.name}:{line}: {message}" if line else f"{sheet.name}: {message}"
        for line, message in errors
    )


def test_the_site_loads_no_script_other_than_its_own():
    """Two scripts now, and both are ours. The site's dependency-free claim is
    only as good as the list of things it fetches, so this is an exact list
    rather than a rule about prefixes: a same-origin path is easy to write and
    this test is the place a third one has to be argued for.

    /status.js joined in #170. It is the first script here that opens a
    connection at runtime, which no markup test can see, so the host it may
    reach is pinned separately in test_the_status_script_talks_to_exactly_one_host.
    """
    for page in PAGES:
        srcs = re.findall(r'<script[^>]*src="([^"]+)"', read(page))
        assert srcs == ["/theme.js", "/status.js"], f"{page.name}: {srcs}"


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


# Every surface a bordered control can land on. Pinned as a list rather than as
# the cases that happen to exist, because #227 is the third time --control-line
# moved and all three times the cause was the same: a control appearing on a
# surface nobody had measured it against.
CONTROL_SURFACES = ("bg", "chrome", "panel")


def test_a_bordered_control_has_a_visible_edge_on_every_surface():
    """WCAG 1.4.11: a control has to be identifiable as a control, at 3:1
    against what surrounds it.

    This is pinned because it has already failed twice. The theme select was
    drawn with `--line`, a *separator* token -- 1.35:1 against the header bar
    on dark and 1.19:1 on light, an edge nobody could see. `--control-line` is
    the token for the job, and on light it had to be darkened from the
    dashboard's #8d919b (2.84:1 on this site's --chrome) because the dashboard
    measured it against white cards rather than against a chrome bar.

    Then #227: this test checked --chrome and --panel, the two surfaces that
    had controls on them the day it was written. The `Last updated` pill on the
    legal pages is a direct child of <main>, so it sits on --bg, which is
    darker than --chrome -- and there the token gave 2.81:1, on three pages,
    for as long as the pill has existed. The test could not see it because
    --bg was not in the list.

    So the list is now every surface, not every surface currently in use. A
    control landing somewhere new should fail here rather than ship at 2.81.
    """
    for theme, edge, prefix in (
        ("dark", _token("--control-line"), "--"),
        ("light", _token("--light-control-line"), "--light-"),
    ):
        for surface in CONTROL_SURFACES:
            ground = _token(f"{prefix}{surface}")
            got = _contrast(edge, ground)
            assert got >= 3.0, (
                f"{theme}: control edge {edge} is {got:.2f}:1 on --{surface} "
                f"({ground}), under the 3:1 a UI component boundary needs"
            )


def test_the_picker_label_is_readable_in_both_themes():
    """`--faint` is the quietest thing on the page and is still text: 4.5:1."""
    for theme, faint, chrome in (
        ("dark", _token("--faint"), _token("--chrome")),
        ("light", _token("--light-faint"), _token("--light-chrome")),
    ):
        ratio = _contrast(faint, chrome)
        assert ratio >= 4.5, f"{theme}: label {faint} on {chrome} is {ratio:.2f}:1"


# --------------------------------------------------------------------------
# The landing page (#137 phase 2).
# --------------------------------------------------------------------------

INDEX = SITE / "index.html"


def test_no_premium_amount_is_hardcoded_anywhere_on_the_site():
    """A price typed here is a second copy of one Stripe is the truth for.

    `subscription.html` states the rule this enforces: no amount is ever
    computed, because a second copy of a price on a page about money is a
    second thing to be wrong. This site deploys on a completely separate
    pipeline from the dashboard, so a figure typed here could disagree with
    what is actually being charged and nothing would ever catch it.

    NO figure at all, not even `$0`. The free card says "Free", which states
    the same standing promise without putting a price-shaped token on a site
    that has opted out of quoting prices. Amounts live on the public pricing
    page, which is a live route reading Stripe -- so this assertion is total
    rather than carrying an exception somebody would later widen.

    Comments are stripped before scanning. The rule is about what a reader is
    shown, and the comments explaining the rule necessarily quote the figures
    it forbids -- a check that fired on its own rationale would be noise.
    """
    for page in PAGES:
        visible = re.sub(r"<!--.*?-->", "", read(page), flags=re.S)
        amounts = set(re.findall(r"\$\d+(?:\.\d{2})?", visible))
        assert not amounts, (
            f"{page.name} names a price: {sorted(amounts)}. Amounts belong on "
            "the pricing page, read from Stripe at render time."
        )


def test_the_plan_cards_never_use_a_bare_plan_class():
    """#158, on the other side of the fence.

    The dashboard shipped `.plan` and it collided with an unrelated `.plan` in
    settings.html, rendering the price on the page that takes money as an
    italic grey footnote. It was renamed `.plan-card` and a collision test was
    added there. This site copies the card design, so it copies the lesson --
    a bare `.plan` here would be the same mistake with a fresh stylesheet.
    """
    classes = re.findall(r'class="([^"]+)"', read(INDEX))
    bare = [c for c in classes if "plan" in c.split() ]
    assert not bare, f"index.html uses a bare `plan` class: {bare}"


def test_the_free_card_leads_with_what_it_is():
    """The free column is the product's best trust signal and must not read as
    a crippled tier. If this copy ever inverts into a list of what free lacks,
    that is a deliberate decision and should fail here first."""
    text = read(INDEX)
    assert "Everything you need to verify your members." in text


def test_the_hero_states_the_three_things_the_product_does_not_do():
    """The most trust-building claim on the site, promoted to sit under the
    buttons. The full four, with reasons, stay in their own section below."""
    text = read(INDEX)
    strip = re.search(r'<p class="trust-strip">(.*?)</p>', text, re.S)
    assert strip, "the hero has no trust strip"
    for claim in ("No identity documents", "No photographs", "No manual override"):
        assert claim in strip.group(1), f"the trust strip dropped {claim!r}"
    # The detailed section is not replaced by the strip.
    assert "<h2>What it does not do</h2>" in text


def test_the_flow_has_three_steps_and_its_arrows_are_decorative():
    """An arrow is punctuation between steps; a screen reader announcing it
    would be reading the gaps aloud."""
    text = read(INDEX)
    assert len(re.findall(r'<li class="flow-step">', text)) == 3
    arrows = re.findall(r'<li class="flow-arrow"([^>]*)>', text)
    assert len(arrows) == 2, f"expected 2 arrows between 3 steps, got {len(arrows)}"
    for attrs in arrows:
        assert 'aria-hidden="true"' in attrs


def test_only_the_landing_page_widens_the_measure():
    """`.home` unlocks the wide landing layout. The legal pages keep 46rem,
    which is tuned for reading long-form terms -- a wider measure there would
    be worse, not better."""
    for page in PAGES:
        has_home = 'class="home"' in read(page)
        assert has_home == (page.name == "index.html"), (
            f"{page.name}: class=\"home\" should be on index.html alone"
        )


def test_the_landing_page_text_clears_aa_in_both_themes():
    """Every pairing the landing page introduced, measured rather than assumed.

    Card borders are deliberately NOT in here. A card is a container, not a UI
    component, and this design separates it with the surface ramp -- ground,
    chrome, card -- rather than with a border, which the dashboard's stylesheet
    header states outright. Holding a container edge to the 3:1 a control needs
    would mean heavy borders that contradict the design language.
    """
    pairs = [
        ("trust strip / plans note", "--faint", "--bg"),
        ("flow note", "--muted", "--bg"),
        ("flow body, plan blurb, plan eyebrow", "--muted", "--panel"),
        ("plan price", "--ink-strong", "--panel"),
        ("plan feature tick", "--accent-text", "--panel"),
        ("plan feature text", "--ink", "--panel"),
    ]
    for label, fg, bg in pairs:
        for theme, prefix in (("dark", ""), ("light", "--light")):
            fg_token = fg if theme == "dark" else fg.replace("--", "--light-", 1)
            bg_token = bg if theme == "dark" else bg.replace("--", "--light-", 1)
            ratio = _contrast(_token(fg_token), _token(bg_token))
            assert ratio >= 4.5, (
                f"{theme}: {label} is {ratio:.2f}:1 ({fg_token} on {bg_token})"
            )


def test_the_number_ring_in_the_flow_is_visible():
    """It is drawn with --control-line rather than --line for the same reason
    the theme picker is: a ring that vanishes is not a ring."""
    for theme, edge, panel in (
        ("dark", _token("--control-line"), _token("--panel")),
        ("light", _token("--light-control-line"), _token("--light-panel")),
    ):
        ratio = _contrast(edge, panel)
        assert ratio >= 3.0, f"{theme}: flow number ring is {ratio:.2f}:1"


# --------------------------------------------------------------------------
# The legal pages (#137 phase 3). Typography only -- plus the heading ids,
# which are the one thing here with consequences outside this repository.
# --------------------------------------------------------------------------

POLICY_PAGES = [p for p in PAGES if p.name in {"terms.html", "privacy.html", "refunds.html"}]
POLICY_NAMES = [p.name for p in POLICY_PAGES]


def _headings(page):
    """(level, id, text) for every h2/h3, with the anchor link stripped out."""
    found = []
    for level, attrs, inner in re.findall(r"<h([23])([^>]*)>(.*?)</h\1>", read(page), re.S):
        ident = re.search(r'id="([^"]+)"', attrs)
        text = re.sub(r'<a class="anchor".*?</a>', "", inner, flags=re.S)
        found.append((level, ident.group(1) if ident else None, text.strip()))
    return found


@pytest.mark.parametrize("page", POLICY_PAGES, ids=POLICY_NAMES)
def test_every_policy_heading_is_linkable(page):
    """Stripe, Discord and support replies all hold URLs into these pages, and
    until #137 phase 3 none of them could point at a specific clause."""
    missing = [text for _, ident, text in _headings(page) if not ident]
    assert not missing, f"{page.name}: headings with no id: {missing}"


@pytest.mark.parametrize("page", POLICY_PAGES, ids=POLICY_NAMES)
def test_heading_ids_are_unique_within_a_page(page):
    """Two headings sharing an id means one of them is unreachable."""
    ids = [ident for _, ident, _ in _headings(page)]
    duplicates = {i for i in ids if ids.count(i) > 1}
    assert not duplicates, f"{page.name}: {sorted(duplicates)}"


@pytest.mark.parametrize("page", POLICY_PAGES, ids=POLICY_NAMES)
def test_heading_ids_do_not_carry_the_section_number(page):
    """`#5. Buying...` would break the moment a clause is inserted above it.

    Legal documents get renumbered; that is a normal edit and it must not
    invalidate every link anybody has saved. The id comes from the heading
    text with the leading number stripped, so renumbering is free and only
    a rewording costs an anchor.
    """
    numbered = [ident for _, ident, _ in _headings(page) if re.match(r"^\d+(-|$)", ident or "")]
    assert not numbered, f"{page.name}: ids carrying a section number: {numbered}"


@pytest.mark.parametrize("page", POLICY_PAGES, ids=POLICY_NAMES)
def test_the_anchor_links_are_decorative_and_point_at_their_own_heading(page):
    """A screen reader user navigates by heading and does not need "#"
    announced fourteen times; a keyboard user should not tab past one before
    every section. It is a convenience for copying a link, and only that.

    It must also actually point at the heading it sits in -- an anchor whose
    href has drifted from its parent's id is a link to nowhere.
    """
    for level, ident, _ in _headings(page):
        block = re.search(
            rf'<h{level} id="{re.escape(ident)}">(.*?)</h{level}>', read(page), re.S
        )
        anchor = re.search(r'<a class="anchor"([^>]*)>', block.group(1))
        assert anchor, f"{page.name}: #{ident} has no anchor link"
        attrs = anchor.group(1)
        assert f'href="#{ident}"' in attrs, f"{page.name}: #{ident} anchor points elsewhere"
        assert 'aria-hidden="true"' in attrs, f"{page.name}: #{ident} anchor is not decorative"
        assert 'tabindex="-1"' in attrs, f"{page.name}: #{ident} anchor is in the tab order"


def test_only_the_policies_carry_the_policy_class():
    """`policy` turns on the long-form reading treatment -- a 42rem measure and
    ruled section headings. 404 is not a policy and the landing page has its
    own, wider layout.

    This checks the class is present, not the width it implies. The rendered
    measure is not reachable from here without a browser.
    """
    for page in PAGES:
        expected = page.name in {"terms.html", "privacy.html", "refunds.html"}
        assert ('class="policy"' in read(page)) == expected, page.name


# --------------------------------------------------------------------------
# The generated changelog (#137 phase 4).
#
# The page is committed, so the deploy stays an asset push with no code on the
# request path -- which is what keeps the apex site a separate failure domain
# from the dashboard's VPS. The cost of committing generated output is that it
# can go stale, and nothing regenerates it on push: .github/workflows/ holds
# CodeQL and nothing else. These tests are the thing that notices.
# --------------------------------------------------------------------------

sys.path.insert(0, str(SITE.parent / "scripts"))
sys.path.insert(0, str(SITE.parent / "src"))

import gen_changelog  # noqa: E402
from dashboard import changelog as changelog_module  # noqa: E402

CHANGELOG = SITE / "changelog.html"


def test_the_committed_changelog_matches_the_constant():
    """The drift guard, and the reason a generated file may be committed here.

    Add an entry to ENTRIES, forget to re-run the script, and the public page
    is silently behind the product -- on the copy strangers read. Regenerating
    in memory and comparing is the cheapest thing that catches it.

    If this fails: python scripts/gen_changelog.py
    """
    assert CHANGELOG.exists(), "site/changelog.html has not been generated"
    assert CHANGELOG.read_text(encoding="utf-8") == gen_changelog.render(), (
        "site/changelog.html is out of date with changelog.ENTRIES. "
        "Run: python scripts/gen_changelog.py"
    )


def test_a_private_entry_never_reaches_the_public_page():
    """`public=False` is the one flag standing between an entry written for a
    signed-in admin and a page read by strangers.

    Every entry on `main` is public today, so nothing would exercise this if
    it were only checked against the real constant -- which is exactly how a
    filter rots. The entry is fabricated here so the rule is tested before the
    first real one needs it.
    """
    private = changelog_module.Entry(
        id="test-private",
        date=changelog_module.date(2026, 1, 1),
        title="Only makes sense signed in",
        body="Your server's log channel is on the Settings page.",
        public=False,
    )
    public = changelog_module.Entry(
        id="test-public",
        date=changelog_module.date(2026, 1, 2),
        title="Safe for anybody to read",
        body="Something shipped.",
    )
    rendered = gen_changelog.render(
        changelog_module.public_entries((private, public))
    )
    assert "Safe for anybody to read" in rendered
    assert "Only makes sense signed in" not in rendered
    assert "log channel" not in rendered


def test_entry_text_is_escaped_on_the_way_out():
    """Bodies are plain text by contract and this is the one place on the apex
    site where text from elsewhere becomes HTML. Jinja does this for the
    dashboard; here it is explicit, so it is worth a test."""
    entry = changelog_module.Entry(
        id="test-escaping",
        date=changelog_module.date(2026, 1, 1),
        title="A <script> & an ampersand",
        body='Quote " and <b>bold</b>.',
    )
    rendered = gen_changelog.render((entry,))
    assert "<script>" not in rendered
    assert "<b>bold</b>" not in rendered
    assert "&lt;script&gt;" in rendered
    assert "&amp;" in rendered


def test_the_generated_page_carries_a_do_not_edit_banner():
    """Somebody will open this file to fix a typo. It has to say where the
    typo actually lives before they retype it and lose the change."""
    text = read(CHANGELOG)
    assert "DO NOT EDIT" in text
    assert "gen_changelog.py" in text
    assert "changelog.py" in text


def test_the_generator_copies_the_chrome_rather_than_retyping_it():
    """A third hand-written copy of the header and footer in a generator is
    the drift the identical-chrome tests exist to catch. The source file is
    read at generation time; this asserts the generator holds no literal
    copy of its own."""
    source = (SITE.parent / "scripts" / "gen_changelog.py").read_text(encoding="utf-8")
    assert '<a class="brand"' not in source
    assert "Esatto Technologies" not in source


def test_the_check_flag_reports_staleness(tmp_path, monkeypatch):
    """`--check` is what CI and the release routine call. If it cannot tell a
    stale file from a fresh one it is decoration.

    This used to assert only the fresh path -- which the drift test above
    already covers, and which a `--check` hard-coded to `return 0` would have
    passed. The stale case is the one worth constructing.
    """
    assert gen_changelog.main(["--check"]) == 0, "fresh tree should report clean"

    stale = tmp_path / "changelog.html"
    stale.write_text("<!doctype html><html><body>old</body></html>", encoding="utf-8")
    monkeypatch.setattr(gen_changelog, "OUTPUT", stale)
    assert gen_changelog.main(["--check"]) == 1, "a stale file must report stale"

    missing = tmp_path / "gone.html"
    monkeypatch.setattr(gen_changelog, "OUTPUT", missing)
    assert gen_changelog.main(["--check"]) == 1, "a missing file must report stale"


# --------------------------------------------------------------------------
# Cross-links (#137 phase 5).
# --------------------------------------------------------------------------

@pytest.mark.parametrize("page", PAGES, ids=PAGE_NAMES)
def test_every_page_can_reach_the_changelog(page):
    """Phase 4 shipped the page deliberately unlinked; this is the phase that
    links it.

    Originally in the footer rather than the header nav, because the header
    was the contested space: three legal links plus the dashboard, and #188
    adding Pricing. #200 took the legal links out of the header, which is what
    freed the room, and the changelog moved up into it. The footer link stays
    -- the footer is the index of everything, and losing it there would make
    the changelog reachable only from a row a reader has to scan for.

    So this passes on both surfaces now. The header-specific claim is
    `test_the_changelog_is_offered_in_the_header`.
    """
    assert '<a href="/changelog">' in read(page), (
        f"{page.name} cannot reach the changelog"
    )


@pytest.mark.parametrize("page", PAGES, ids=PAGE_NAMES)
def test_the_changelog_is_offered_in_the_header(page):
    """The slot #200 freed. Asserted separately from the footer link above,
    because that one passes on a page that only has the footer copy and this
    is the half that is easy to lose when somebody edits the chrome."""
    header = re.search(r'<header class="site">(.*?)</header>', read(page), re.S)
    assert header, f"{page.name} has no site header"
    assert '<a href="/changelog">' in header.group(1), (
        f"{page.name} does not offer the changelog in its header"
    )


def test_the_changelog_link_survives_regeneration():
    """changelog.html is generated, and its footer is copied from a hand-written
    page. So the footer link reaches it only if somebody re-runs the script
    after editing the others -- which the drift test already insists on, but
    this states the dependency where a reader will see it."""
    assert '<a href="/changelog">' in read(CHANGELOG)


# --------------------------------------------------------------------------
# What an adversarial pass over all five phases of #137 found.
# --------------------------------------------------------------------------

def test_only_main_takes_its_measure_from_the_body_class():
    """The shared chrome must be one width on every page.

    `.wrap` wraps the header, the main content AND the footer, so a per-page
    measure written as `.home .wrap` silently captures the chrome. That
    shipped: the header rendered at four different widths across six pages and
    the brand moved 144px between the landing page and Terms.

    Nothing caught it. `test_the_header_nav_is_identical_on_every_page`
    compares MARKUP, and the markup was identical -- the divergence was
    entirely in CSS keyed off the body class, which no test here can see.

    So this pins the shape instead: a page-scoped measure must say `main`.
    """
    css = STYLE.read_text(encoding="utf-8")
    offenders = []
    for selector, body in re.findall(r"([^{}]+)\{([^}]*)\}", css):
        selector = selector.split("*/")[-1].strip()
        if "max-width" not in body:
            continue
        for part in selector.split(","):
            part = part.strip()
            if not part.endswith(".wrap"):
                continue
            scoped_to_page = re.match(r"^\.(home|policy|changelog-page)\s", part)
            is_chrome = part.startswith(("header.site", "footer.site"))
            if scoped_to_page and not is_chrome and "main.wrap" not in part:
                offenders.append(part)
    assert not offenders, (
        "these set a measure on the shared chrome as well as the content: "
        f"{offenders}. Scope them to `main.wrap`."
    )


def test_the_chrome_pins_its_own_width():
    """The other half of the rule above: something has to state the one width."""
    css = STYLE.read_text(encoding="utf-8")
    assert re.search(
        r"header\.site \.wrap,\s*\n?footer\.site \.wrap \{[^}]*max-width", css
    ), "no rule pins the header/footer measure"


def test_the_outlined_button_is_readable_in_every_state():
    """Two contrast bugs lived here at once, and both were invisible to the
    landing-page contrast test because it only measured text against --bg and
    --panel, never against a hover fill or a border's backdrop.

    1. Hover set `background: --line-soft` under an --accent-text label, which
       is 4.30:1 in dark -- the control became LESS readable at the moment of
       interaction, and on keyboard focus.
    2. The border was --accent, and this button also sits inside a plan card
       whose fill is --panel, where --accent is 2.74:1. It is the button's only
       edge, since the background is transparent.
    """
    css = STYLE.read_text(encoding="utf-8")
    rule = re.search(r"\.cta\.secondary \{([^}]*)\}", css)
    assert rule, ".cta.secondary has no rule"
    assert "border: 1px solid var(--accent-text)" in rule.group(1), (
        "the outlined button's border must be --accent-text: --accent is "
        "2.74:1 on --panel, and this border is the button's only edge"
    )

    hover = re.search(r"\.cta\.secondary:hover[^{]*\{([^}]*)\}", css)
    assert hover, ".cta.secondary has no hover rule"
    assert "var(--line-soft)" not in hover.group(1), (
        "--line-soft under an --accent-text label is 4.30:1 in dark"
    )

    # Whatever the hover fill is, the label on it must clear AA in both themes.
    for theme, ink, fill in (
        ("dark", _token("--accent-ink"), _token("--accent")),
        ("light", _token("--light-accent-ink"), _token("--light-accent")),
    ):
        ratio = _contrast(ink, fill)
        assert ratio >= 4.5, f"{theme}: hovered label is {ratio:.2f}:1"


def test_a_pill_that_is_only_a_border_can_actually_be_seen():
    """`.updated` and the changelog tag are chips whose entire shape is their
    border. Drawn with --line they were 1.62:1 in dark and 1.05:1 in light --
    so neither rendered as a pill at all, just text where a chip was drawn.

    Not a WCAG failure (a decorative boundary is exempt), which is exactly why
    nothing else here would ever catch it.
    """
    css = STYLE.read_text(encoding="utf-8")
    for selector in (r"\.policy \.updated", r"\.entry \.tag"):
        rule = re.search(selector + r" \{([^}]*)\}", css)
        assert rule, f"{selector} has no rule"
        assert "border: 1px solid var(--line)" not in rule.group(1), (
            f"{selector} draws its whole shape with --line, which is 1.05:1 "
            "on the ground in light"
        )


# --------------------------------------------------------------------------
# The public pricing page, linked in (#188 phase 2).
# --------------------------------------------------------------------------

PRICING_URL = "https://dashboard.vrcverify.com/pricing"


@pytest.mark.parametrize("page", PAGES, ids=PAGE_NAMES)
def test_every_page_offers_pricing_in_the_header(page):
    """The claim #137 phase 5 made when it put the changelog in the footer
    instead: Pricing has a far stronger claim on the header than a changelog
    does, so this is the link that took the contested space.

    Six pages, and one of them is generated -- `changelog.html` gets its header
    copied out of `terms.html` by `scripts/gen_changelog.py`, so this passes on
    that page only if somebody re-ran the script after editing the others.
    """
    assert f'<a href="{PRICING_URL}">Pricing</a>' in read(page), (
        f"{page.name} does not offer pricing in its header"
    )


def test_pricing_is_the_first_thing_in_the_nav():
    """Ahead of the three legal links, which is the whole point of adding it.

    A visitor deciding whether to install the bot is looking for a price; the
    Terms are there for the people Stripe sends and for anyone who goes
    looking. Putting Pricing fourth would be adding it to satisfy this issue
    rather than to be found.
    """
    nav = re.search(r"<nav>(.*?)</nav>", read(SITE / "index.html"), re.S)
    assert nav, "the landing page has no header nav"
    hrefs = re.findall(r'<a href="([^"]+)"', nav.group(1))
    assert hrefs and hrefs[0] == PRICING_URL, f"nav order is {hrefs}"


def test_the_premium_card_sends_people_to_the_price_and_not_the_front_door():
    """#137 phase 2 had to point this button at the dashboard root, because
    there was nowhere else for it to go: the only page with a price on it was
    behind OAuth. That was flagged in PR #187 as a likely drop-off rather than
    fixed, and this is the fix.

    A button reading "See pricing" that lands on a sign-in screen is the exact
    bait-and-switch this epic is trying to remove.
    """
    text = read(SITE / "index.html")
    card = re.search(r'<div class="plan-card plan-featured">(.*?)</div>', text, re.S)
    assert card, "the Premium card is gone"
    assert f'href="{PRICING_URL}"' in card.group(1), (
        "the Premium card still points at the dashboard front door"
    )


# --------------------------------------------------------------------------
# One typeface across both surfaces (#195 phase 2).
# --------------------------------------------------------------------------

FONT = SITE / "fonts" / "inter-latin-var.woff2"


def test_the_font_is_vendored_and_not_fetched_from_anywhere():
    """The rule this amended was "no fonts"; the rule it left standing is "no
    third party". A font CDN would let someone else see who reads the Privacy
    Policy, which is the same reasoning that vendored Inter into the dashboard.

    So: the file exists here, and every `src` in the stylesheet is relative.
    """
    assert FONT.exists(), "the woff2 is not vendored into site/fonts"
    css = STYLE.read_text(encoding="utf-8")
    face = re.search(r"@font-face \{(.*?)\}", css, re.S)
    assert face, "the site declares no @font-face"
    for url in re.findall(r"url\(([^)]*)\)", face.group(1)):
        assert "//" not in url, f"@font-face reaches off-origin: {url}"


def test_the_font_licence_ships_beside_it():
    """Inter is SIL OFL 1.1, which requires the licence to travel with the
    font. Vendoring the binary and leaving the licence in the other host's
    directory would be shipping it without terms."""
    licence = SITE / "fonts" / "Inter-LICENSE.txt"
    assert licence.exists(), "the font ships without its licence"
    assert "SIL Open Font License" in licence.read_text(encoding="utf-8")


def test_the_two_hosts_serve_the_same_font_file():
    """Copied, not shared -- different origin, different deploy, same reasoning
    as the colour tokens. A test rather than an import, because there is no
    mechanism that could keep them equal on its own.

    Compared by bytes: two files with the same name and different contents
    would render the two surfaces in two subtly different typefaces, which is
    the exact failure this phase exists to remove.
    """
    dashboard_font = (
        pathlib.Path(__file__).resolve().parent.parent
        / "src" / "dashboard" / "static" / "fonts" / "inter-latin-var.woff2"
    )
    assert dashboard_font.exists(), "the dashboard's font has moved"
    assert FONT.read_bytes() == dashboard_font.read_bytes(), (
        "the two hosts are serving different builds of Inter"
    )


def test_the_page_still_reads_if_the_font_never_arrives():
    """`font-display: swap` plus a real fallback stack is what makes this safe
    rather than merely defensible: the worst case is the system stack, which is
    what this site looked like before the font was added.

    A `block` display, or a stack of one name, would make a slow font a blank
    page on a legal document.
    """
    css = STYLE.read_text(encoding="utf-8")
    face = re.search(r"@font-face \{(.*?)\}", css, re.S)
    assert "font-display: swap" in face.group(1), (
        "a legal page must not block paint on a font"
    )
    stack = re.search(r"--font:\s*([^;]+);", css)
    assert stack, "the site names no font stack"
    assert "system-ui" in stack.group(1) and stack.group(1).count(",") >= 3, (
        "the fallback stack is too thin to survive a missing woff2"
    )


def test_the_non_prose_rows_outrank_the_measure_that_would_wrap_them():
    """Phase 1 shipped this override as a bare `.trust-strip, .flow-note`,
    which is (0,1,0) against the (0,2,2) of `.home main.wrap > p`. It lost, so
    both elements kept wrapping -- the trust strip orphaning "OVERRIDE" onto a
    second line and the flow note orphaning "no." under its accent rule.

    Nothing caught it: a characters-per-line check passes happily on a wrapped
    strip, because its lines really are short. Only the render showed it.

    So this asserts the shape of the selector rather than the declaration.
    """
    css = STYLE.read_text(encoding="utf-8")
    rule = re.search(
        r"([^\n{]*\.trust-strip[^{]*)\{\s*max-width:\s*none", css
    )
    assert rule, "nothing exempts the trust strip from the measure"
    selector = rule.group(1)
    assert "main.wrap" in selector, (
        "the exemption is not scoped to the container whose rule it must "
        f"outrank, so the measure wins again: {selector.strip()!r}"
    )


def test_system_is_stamped_as_itself_and_not_as_an_absent_attribute():
    """THE BUG THIS PHASE NEARLY SHIPPED, and it would have been silent.

    The dashboard represents "System" as the ABSENCE of `data-theme`, because
    its server always knows what to stamp before first paint. This site cannot:
    absence is also what a reader sees before theme.js runs, and forever with
    JavaScript off, so absence has to mean the default -- dark -- and System
    has to be an explicit `data-theme="system"` for the fourth cascade block in
    style.css to match.

    Porting the dashboard's control brought its rule with it for one commit.
    Mapping "system" to null here pins every System reader to Dark on a
    light-OS machine, and nothing renders wrong enough to notice: the page is
    simply always dark, which is also what it looks like when it is working.
    """
    script = THEME_JS.read_text(encoding="utf-8")
    assert '"system" ? null' not in script.replace(" ", ""), (
        "theme.js maps System to no attribute, which is the dashboard's rule "
        "and the opposite of this site's"
    )
    # And the cascade block it depends on still exists.
    css = STYLE.read_text(encoding="utf-8")
    assert '[data-theme="system"]' in css, (
        "style.css has no rule for an explicit System, so stamping it does "
        "nothing"
    )


def test_the_control_is_built_from_the_placeholder_the_pages_ship():
    """The pages carry `<div class="theme-picker" hidden></div>` and nothing
    else. If the script ever stops looking for that class, six pages ship an
    empty div and no theme control, with no error anywhere."""
    script = THEME_JS.read_text(encoding="utf-8")
    assert ".theme-picker" in script, "theme.js no longer finds the placeholder"
    assert "hidden = false" in script, "theme.js never reveals the control"


class TestTheChangelogOffersTheDiscord:
    """#138: the public changelog invites the reader to follow the channel.

    Hardcoded in the generator, unlike the bot and the dashboard which read
    SUPPORT_INVITE_URL from the environment. These are static files behind a
    CDN -- no server renders them, so there is no environment to read at the
    moment anyone loads the page, and injecting at generation time would make
    the committed HTML depend on whose shell ran the script.
    """

    def test_the_generated_page_offers_the_invite(self):
        from gen_changelog import SUPPORT_INVITE_URL, render

        page = render()
        assert "entry-follow" in page
        assert SUPPORT_INVITE_URL in page

    def test_the_invite_is_a_real_url(self):
        """A schemeless href on a static page resolves against vrcverify.com
        and 404s there instead of reaching Discord."""
        from gen_changelog import SUPPORT_INVITE_URL

        assert SUPPORT_INVITE_URL.startswith("https://")

    def test_it_opens_off_site_safely(self):
        from gen_changelog import render

        page = render()
        row = page.split('class="entry-follow"', 1)[1].split("</p>", 1)[0]
        assert 'rel="noopener noreferrer"' in row

    def test_the_committed_page_carries_it(self):
        """The drift test next door regenerates and compares; this one asserts
        the committed file is the version with the row, so a stale commit
        cannot pass by being self-consistent."""
        page = (SITE / "changelog.html").read_text()
        assert "entry-follow" in page


# ---------------------------------------------------------------
# "On this page" contents list (#190)
# ---------------------------------------------------------------
PAGES_WITH_TOC = POLICY_NAMES


@pytest.mark.parametrize("name", POLICY_NAMES)
def test_every_policy_page_has_a_contents_list(name):
    """Phase 2 settled this: all three agreements get one, or a reader who
    learns the pattern on two of them reads the third as shorter than it is.

    Written against POLICY_NAMES rather than PAGES_WITH_TOC on purpose. The
    tests below all parametrize over PAGES_WITH_TOC, so shortening that list is
    a way to make any of them stop failing without fixing anything. This one
    does not move when that list does.
    """
    assert '<nav class="toc"' in read(SITE / name), (
        f"{name} is a legal agreement with no contents list"
    )


def _toc_entries(page):
    """(id, text) for every link in the page's contents list, in order."""
    text = read(page)
    block = re.search(r'<nav class="toc"[^>]*>(.*?)</nav>', text, re.S)
    assert block, f"{page.name} has no contents list"
    return re.findall(r'<a href="#([^"]+)">(.*?)</a>', block.group(1), re.S)


@pytest.mark.parametrize("name", PAGES_WITH_TOC)
def test_the_contents_list_matches_the_document(name):
    """THE WHOLE REASON THIS IS A TEST.

    There is no template engine here, so a hand-written contents list is a
    second copy of every section name sitting a few hundred lines above the
    first. Rename a heading and the list keeps the old wording, pointing at an
    id that no longer exists, and nothing notices -- on the page a payment
    dispute turns on.

    Derived from the document rather than from a list written down twice: the
    expectation IS the headings, so the two cannot drift by construction. This
    also settles the copy question, because link text that must equal the
    heading leaves no room to paraphrase, and a paraphrase is a second wording
    of a legal section title.
    """
    page = SITE / name
    expected = [(i, t) for level, i, t in _headings(page) if level == "2"]
    assert _toc_entries(page) == expected, (
        f"{name}: the contents list and the headings disagree. Regenerate it "
        f"from the document rather than editing it by hand."
    )


@pytest.mark.parametrize("name", PAGES_WITH_TOC)
def test_every_entry_points_at_a_heading_that_exists(name):
    """Belt and braces against the equality above being loosened later."""
    page = SITE / name
    ids = {i for _, i, _ in _headings(page)}
    dead = [i for i, _ in _toc_entries(page) if i not in ids]
    assert not dead, f"{name}: entries pointing nowhere: {dead}"


@pytest.mark.parametrize("name", PAGES_WITH_TOC)
def test_no_section_is_left_out(name):
    """A partial list is worse than none: a reader who cannot find a clause in
    it reasonably concludes the document does not contain one."""
    page = SITE / name
    listed = {i for i, _ in _toc_entries(page)}
    missing = [i for level, i, _ in _headings(page) if level == "2" and i not in listed]
    assert not missing, f"{name}: sections missing from the contents list: {missing}"


@pytest.mark.parametrize("name", PAGES_WITH_TOC)
def test_it_is_announced_as_navigation(name):
    """It must not be mistakeable for a summary of the terms. A reader who
    believes they have read the agreement because they read the list has been
    misled by the layout, so it is marked up and labelled as a signpost."""
    text = read(SITE / name)
    block = re.search(r'<nav class="toc"[^>]*>(.*?)</nav>', text, re.S).group(1)
    assert 'aria-label="On this page"' in text
    assert "On this page" in block
    for word in ("summary of", "overview", "at a glance", "in short"):
        assert word not in block.lower(), (
            f"{name}: the contents list calls itself {word!r}, which invites "
            f"a reader to treat it as the agreement"
        )


@pytest.mark.parametrize("name", PAGES_WITH_TOC)
def test_it_carries_no_descriptions(name):
    """The list says where things are, not what they say. Anything other than
    a link in an <li> is prose creeping into navigation."""
    block = re.search(
        r'<nav class="toc"[^>]*>(.*?)</nav>', read(SITE / name), re.S
    ).group(1)
    for item in re.findall(r"<li>(.*?)</li>", block, re.S):
        stripped = re.sub(r"<a href=\"#[^\"]+\">.*?</a>", "", item, flags=re.S)
        assert not stripped.strip(), f"{name}: an entry carries more than a link"


@pytest.mark.parametrize("name", PAGES_WITH_TOC)
def test_it_needs_no_javascript(name):
    """These pages must work with scripting off like every other page here.
    There is no widget at all now, which is the strongest form of that."""
    block = re.search(
        r'<nav class="toc"[^>]*>(.*?)</nav>', read(SITE / name), re.S
    ).group(1)
    assert "<script" not in block
    assert "onclick" not in block


def test_the_list_adds_no_ordered_markers():
    """Terms carries its numbering inside the heading text itself ("1. What the
    service does"), so a marker would render it twice. Privacy and Refunds do
    not number their sections at all, and numbering them here would invent an
    ordering the documents do not claim. The <ol> stays on all three because
    document order is meaningful; only the markers go."""
    css = (SITE / "style.css").read_text()
    block = re.search(r"\.toc-list \{[^}]*\}", css).group(0)
    assert "list-style: none" in block


# --------------------------------------------------------------------------
# The header stops carrying the legal links (#200).
# --------------------------------------------------------------------------

LEGAL_HREFS = ("/terms", "/privacy", "/refunds")


@pytest.mark.parametrize("page", PAGES, ids=PAGE_NAMES)
def test_the_header_carries_no_legal_links(page):
    """The ask in #200. Terms, Privacy and Refunds sat in the top nav on every
    page, above the fold, competing with the two links a visitor is actually
    looking for.

    Asserted per page rather than once on index.html even though the chrome is
    byte-identical, because `changelog.html` is generated: it gets its header
    copied out of `terms.html` by `scripts/gen_changelog.py` and only picks
    this up if somebody re-ran the script.
    """
    header = re.search(r'<header class="site">(.*?)</header>', read(page), re.S)
    assert header, f"{page.name} has no site header"
    hrefs = re.findall(r'<a href="([^"]+)"', header.group(1))
    assert not [h for h in hrefs if h in LEGAL_HREFS], (
        f"{page.name} still links a policy from its header: {hrefs}"
    )


@pytest.mark.parametrize("page", PAGES, ids=PAGE_NAMES)
def test_the_footer_still_carries_all_three(page):
    """The other half of #200, and the half that matters more. Removing the
    links from the header is only acceptable while the footer keeps them --
    Stripe and Discord both send people here expecting to find a policy, and
    `REQUIRED_PAGES` above records that an external configuration nothing in
    this repo can see points at these paths."""
    footer = re.search(r'<footer class="site">(.*?)</footer>', read(page), re.S)
    assert footer, f"{page.name} has no site footer"
    for href in LEGAL_HREFS:
        assert f'<a href="{href}">' in footer.group(1), (
            f"{page.name} lost {href} from its footer"
        )


def test_the_nav_is_down_to_three_links():
    """The claim the CSS makes where the 26rem override used to be: three
    links, so the phone-width special case has nothing left to fix. If a
    fourth is ever added back, that comment is wrong and the 360px case needs
    re-measuring rather than assuming."""
    nav = re.search(r"<nav>(.*?)</nav>", read(SITE / "index.html"), re.S)
    assert nav, "the landing page has no header nav"
    assert len(re.findall(r'<a href="', nav.group(1))) == 3

    css = (SITE / "style.css").read_text(encoding="utf-8")
    assert "FIVE LINKS ON A PHONE" not in css, (
        "the removed override's comment is back without the links it describes"
    )


# ---------------------------------------------------------------------------
# The header's status dot (#170).
#
# It is a link that happens to know something, not a widget. Everything below
# is about keeping it in that order: the link works with no script, the dot
# never claims health it did not read, and the colours are the status page's
# own rather than a second green that drifts from it.
# ---------------------------------------------------------------------------

STATUS_ORIGIN = "https://status.vrcverify.com"


@pytest.mark.parametrize("page", PAGES, ids=PAGE_NAMES)
def test_every_page_carries_the_status_pill(page):
    """Identical on every page, which test_the_header_nav_is_identical_on_every_page
    already enforces -- this says it must be there at all."""
    text = read(page)
    pill = re.search(r'<a class="status-pill"([^>]*)>', text)
    assert pill, f"{page.name} has no status pill"
    assert f'href="{STATUS_ORIGIN}/"' in pill.group(1), f"{page.name}'s pill does not link to the status page"


@pytest.mark.parametrize("page", PAGES, ids=PAGE_NAMES)
def test_the_pill_ships_neutral_and_is_not_hidden(page):
    """The two halves of "never green from missing data", at the markup level.

    Ships `unknown`, so a reader whose script never runs is told nothing
    rather than told everything is fine. And ships VISIBLE, unlike the theme
    picker: that control cannot work without JavaScript, this one is a plain
    link that works perfectly without it.
    """
    pill = re.search(r'<a class="status-pill"([^>]*)>', read(page))
    assert 'data-state="unknown"' in pill.group(1), f"{page.name}'s pill does not ship neutral"
    assert "hidden" not in pill.group(1), f"{page.name}'s pill ships hidden, but it works without a script"


@pytest.mark.parametrize("page", PAGES, ids=PAGE_NAMES)
def test_every_page_loads_the_status_script_deferred(page):
    """Deferred, unlike the theme script, and for the opposite reason.

    theme.js must block: it stamps an attribute before the first paint or a
    stored Light choice flashes dark. Nothing paints differently while this
    one is in flight -- the pill is already drawn, already neutral, already a
    working link -- so blocking on it would buy nothing and cost the render.
    """
    tag = re.search(r'<script[^>]*src="/status\.js"[^>]*>', read(page))
    assert tag, f"{page.name} does not load /status.js"
    assert "defer" in tag.group(0), f"{page.name} loads the status script blocking"


def test_the_status_script_talks_to_exactly_one_host():
    """The site's own rule is that nothing is loaded from a third party, and
    this script is the first thing here that opens a connection at runtime,
    where no markup test can see it. So the host is pinned: the status origin,
    which is ours, and nothing else. A second one has to come through here."""
    js = (SITE / "status.js").read_text(encoding="utf-8")
    hosts = set(re.findall(r"https?://[a-z0-9.-]+", js))
    assert hosts == {STATUS_ORIGIN}, f"status.js reaches for {sorted(hosts)}"


def test_the_status_script_sends_nothing_about_the_reader():
    """A public document read publicly. Credentials would make a request that
    identifies this reader to another origin, out of a page that otherwise
    has no idea who is looking at it."""
    js = (SITE / "status.js").read_text(encoding="utf-8")
    assert 'credentials: "omit"' in js


def test_the_status_colours_are_the_status_pages_own():
    """One product, one green. The dot and the page it opens are read within a
    click of each other, and two greens that nearly match look like a bug in
    whichever one the reader sees second."""
    site = _status_tokens(SITE / "style.css")
    status = _status_tokens(ROOT / "status" / "public" / "style.css")
    for token in ("--ok", "--notice", "--down", "--light-ok", "--light-notice", "--light-down"):
        assert site[token] == status[token], (
            f"{token} is {site[token]} on the site and {status[token]} on the status page"
        )


@pytest.mark.parametrize("token", ["ok", "notice", "down"])
@pytest.mark.parametrize("theme", ["", "light-"])
def test_the_dot_clears_the_graphical_floor_on_the_header_bar(token, theme):
    """--chrome is a surface the status page's own suite deliberately does not
    check, because nothing there draws a status colour on it. Here something
    does, and it is exactly the "measured on one surface, drawn on another"
    mistake that has moved --ok three times in this project.

    A dot is a graphical object: WCAG 1.4.11 asks 3:1. All six clear 4.5:1 as
    well, so the label beside it could take the colour without another pass.
    """
    palette = _status_tokens(SITE / "style.css")
    ratio = _contrast(palette[f"--{theme}{token}"], palette[f"--{theme}chrome"])
    assert ratio >= 3.0, f"--{theme}{token} on --{theme}chrome is {ratio:.2f}:1"


def test_state_is_not_carried_by_colour_alone():
    """The fix for a real defect, pinned so it cannot come back as a tidy-up.

    The first version of this pill was a coloured dot. --ok and --down have
    relative luminance 0.3312 and 0.3407 -- 0.01 apart -- so "everything is
    working" and "something is down" were the same grey dot to a red-green
    colourblind reader, in the two states where being wrong costs most. The
    tick, the exclamation and the cross are what actually tell them apart, and
    they are the status page's own glyphs so both surfaces draw one mark.
    """
    js = (SITE / "status.js").read_text(encoding="utf-8")
    glyphs = dict(re.findall(r"^\s+(up|degraded|down):\s+'([^']+)'", js, re.M))
    assert set(glyphs) == {"up", "degraded", "down"}, f"missing a glyph: {sorted(glyphs)}"
    assert len(set(glyphs.values())) == 3, "two states are drawn with the same shape"

    status_render = (ROOT / "status" / "src" / "render.js").read_text(encoding="utf-8")
    for state, shape in glyphs.items():
        assert shape in status_render, (
            f"the {state} glyph has drifted from the status page's own"
        )


def test_the_pill_ships_a_mark_that_claims_nothing():
    """Before any reading arrives -- and forever, with no script -- the mark is
    a plain dot rather than a tick. A tick that means "not checked yet" is the
    same lie as a green row drawn from missing data."""
    for page in PAGES:
        pill = re.search(r'<a class="status-pill".*?</a>', read(page), re.S)
        assert pill, f"{page.name} has no status pill"
        assert "<circle" in pill.group(0), f"{page.name} ships no neutral mark"
        for verdict in ("M6.2 10.4", "M6.5 6.5"):
            assert verdict not in pill.group(0), (
                f"{page.name} ships a verdict glyph before anything has been read"
            )
