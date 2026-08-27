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

    In the footer rather than the header nav: the header is the contested
    space -- it already carries three legal links and the dashboard, and #188
    adds Pricing, which has a far stronger claim on a visitor's attention than
    a changelog does. A changelog in the footer is where readers look for one.
    """
    assert '<a href="/changelog">' in read(page), (
        f"{page.name} cannot reach the changelog"
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
