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

CONTACT = "support@vrcverify.com"
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
        # "/" is the landing page, which Pages serves from index.html.
        target = href.lstrip("/") or "index.html"
        if target not in available:
            dead.append(href)
    assert not dead, f"{page.name}: {dead}"


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
    """
    text = read(page)
    loaders = re.findall(r'(?:src|href)="(https?://[^"]+)"', text)
    stylesheets_and_scripts = [
        url
        for url in loaders
        if re.search(rf'<(?:link|script)[^>]*"{re.escape(url)}"', text)
    ]
    assert not stylesheets_and_scripts, f"{page.name}: {stylesheets_and_scripts}"
    assert "<script" not in text, f"{page.name} has a script tag"


@pytest.mark.parametrize(
    "page", [p for p in PAGES if p.name != "index.html"],
    ids=[p.name for p in PAGES if p.name != "index.html"],
)
def test_every_policy_says_when_it_was_last_updated(page):
    """A policy with no date cannot be shown to have been in force on a given
    day, which is the only question that ever gets asked about one."""
    assert re.search(r'class="updated">Last updated \d{1,2} \w+ \d{4}', read(page)), (
        f"{page.name} has no 'Last updated' line"
    )
