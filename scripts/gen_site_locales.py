"""Generate the apex site's translated pages (#300).

    python scripts/gen_site_locales.py            # rewrite site/<code>/*.html
    python scripts/gen_site_locales.py --extract  # refresh the msgid inventory
    python scripts/gen_site_locales.py --check    # exit 1 if the output is stale

WHY GENERATED AND COMMITTED
---------------------------
Same answer `gen_changelog.py` gives, for the same reason. The apex is an
assets-only Cloudflare Worker with no code on the request path, deliberately:
it is a separate failure domain, so the legal pages resolve when the dashboard
does not. There is nothing there to negotiate a locale or render a template, so
the twelve versions of each page have to exist as files.

The cost is that committed output can go stale. That is what `--check` is for,
and what `tests/test_site.py` runs.

WHY NOT TEMPLATES
-----------------
Converting six hand-written, heavily commented pages into Jinja would produce
the same HTML and throw away every comment explaining why the markup is what it
is. `site_i18n.py` splices by byte offset instead, so English stays the
editable source of truth and the translated copies are derived from it.

THE ENGLISH PAGES ARE NOT WRITTEN BY THIS. `site/index.html` and its five
siblings stay hand-edited. This only ever writes `site/<code>/`.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import re
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from site_i18n import collect, from_msgid, to_msgid  # noqa: E402

REPO = pathlib.Path(__file__).resolve().parent.parent
SITE = REPO / "site"
LOCALES_DIR = REPO / "site_locales"
INVENTORY = LOCALES_DIR / "messages.json"

# The eleven, in the dashboard's order and with its codes. Kept equal to
# `UI_LANGUAGES` in src/dashboard/i18n.py and to `LOCALES` in
# status/src/i18n.js by a test, because a reader who sets a language on one
# surface and loses it on another has been told the product speaks a language
# half of it does not.
DEFAULT_LOCALE = "en"
LOCALES = ["es-ES", "zh-CN", "ja", "de", "nl", "hi-IN", "ar", "bn", "pt-BR", "ru", "pa-IN"]
RTL = {"ar"}

ENDONYMS = {
    "en": "English", "es-ES": "Español", "zh-CN": "简体中文", "ja": "日本語",
    "de": "Deutsch", "nl": "Nederlands", "hi-IN": "हिन्दी", "ar": "العربية",
    "bn": "বাংলা", "pt-BR": "Português (Brasil)", "ru": "Русский", "pa-IN": "ਪੰਜਾਬੀ",
}

ORIGIN = "https://vrcverify.com"

# Strings this generator writes rather than finds. They still have to reach the
# catalogs, so `extract()` merges them into the inventory.
#
# "Language" IS TRANSLATED HERE AND NOT IN THE MENU BELOW, which looks
# inconsistent and is exactly what the dashboard does. The `<p>` label sits
# above a list somebody opened BECAUSE the page is in a language they cannot
# read, so it stays English. The `aria-label` is spoken to somebody already
# using the page in that language, so it does not.
CHROME_MSGIDS = ["Language", "Choose a different one."]
PAGES = ["index.html", "terms.html", "privacy.html", "refunds.html", "changelog.html", "404.html"]

# `/terms` -> `/ja/terms`. Absolute URLs, anchors and mailto: are left alone;
# so is `/logo.svg` and the other assets, which are one copy shared by all
# twelve and must not be prefixed.
ASSETS = re.compile(r"^/(?:style\.css|theme\.js|status\.js|logo\.svg|fonts/)")
INTERNAL_HREF = re.compile(r'(href|action)="(/[^"#]*)"')


def catalog_path(locale: str) -> pathlib.Path:
    return LOCALES_DIR / f"{locale}.json"


def load_catalog(locale: str) -> dict[str, str]:
    path = catalog_path(locale)
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def msgids_for(source: str) -> list[str]:
    out = []
    for _start, _end, kind, raw in collect(source):
        out.append(to_msgid(raw) if kind == "block" else " ".join(raw.split()))
    return out


def extract() -> dict[str, list[str]]:
    """The inventory: every msgid, and which pages it appears on."""
    where: dict[str, set[str]] = {}
    for name in PAGES:
        for msgid in msgids_for((SITE / name).read_text(encoding="utf-8")):
            where.setdefault(msgid, set()).add(name)
    for msgid in CHROME_MSGIDS:
        where.setdefault(msgid, set()).add("(the language picker)")
    return {k: sorted(v) for k, v in sorted(where.items())}


def localize_links(html: str, locale: str) -> str:
    """Point internal links at this locale's copy of the page."""
    if locale == DEFAULT_LOCALE:
        return html

    def fix(m: re.Match) -> str:
        attr, path = m.group(1), m.group(2)
        if ASSETS.match(path):
            return m.group(0)
        return f'{attr}="/{locale}{path}"'

    return INTERNAL_HREF.sub(fix, html)


def esc(value: str) -> str:
    """For an attribute. A translation may legitimately contain a quote."""
    return value.replace("&", "&amp;").replace('"', "&quot;").replace("<", "&lt;")


def picker(locale: str, page: str, catalog: dict[str, str]) -> str:
    """Twelve links, one per language, pointing at this same page.

    LINKS AND NOTHING ELSE. The apex has no code on the request path, so a
    control that posts a preference could not work here at all -- and the legal
    pages are held to rendering with scripting off, which rules out a
    client-side swap. A link is the only affordance that satisfies both, and it
    is the same one the status page uses.
    """
    slug = "" if page == "index.html" else "/" + page.removesuffix(".html")
    items = []
    for code in [DEFAULT_LOCALE, *LOCALES]:
        href = (slug or "/") if code == DEFAULT_LOCALE else f"/{code}{slug or '/'}"
        here = code == locale
        items.append(
            f'<li><a href="{href}" lang="{code}"'
            f' class="menu-item{" current" if here else ""}"'
            f'{" aria-current=\"true\"" if here else ""}>'
            f'<span class="menu-item-label">{ENDONYMS[code]}</span>'
            + (
                '<svg class="menu-tick" viewBox="0 0 16 16" width="13" height="13" '
                'aria-hidden="true" fill="none" stroke="currentColor" stroke-width="2.2" '
                'stroke-linecap="round" stroke-linejoin="round"><path d="M3 8.5l3.5 3.5L13 4.5"/>'
                "</svg>"
                if here
                else ""
            )
            + "</a></li>"
        )
    name = ENDONYMS[locale]
    word = catalog.get("Language", "Language")
    choose = catalog.get("Choose a different one.", "Choose a different one.")
    return (
        '<details class="langpick">'
        f'<summary class="lang-button" title="{esc(word)}: {esc(name)}"'
        f' aria-label="{esc(word)}: {esc(name)}. {esc(choose)}">'
        '<svg class="menu-mark" viewBox="0 0 16 16" width="16" height="16" aria-hidden="true" '
        'fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" '
        'stroke-linejoin="round"><circle cx="8" cy="8" r="6.2"/><path d="M1.8 8h12.4"/>'
        '<path d="M8 1.8a9.2 9.2 0 0 1 0 12.4a9.2 9.2 0 0 1 0-12.4z"/></svg></summary>'
        # Not translated, and the only string on the site that is not. This
        # label sits above a list somebody is reading *because* the page is in
        # a language they do not want. Same call the dashboard and the status
        # page made.
        '<div class="lang-panel"><p class="menu-label" id="lang-menu-label">Language</p>'
        f'<ul aria-labelledby="lang-menu-label">{"".join(items)}</ul></div></details>'
    )


def alternates(page: str, locale: str) -> str:
    slug = "" if page == "index.html" else "/" + page.removesuffix(".html")
    def href(code: str) -> str:
        return f"{ORIGIN}{slug or '/'}" if code == DEFAULT_LOCALE else f"{ORIGIN}/{code}{slug or '/'}"
    lines = [f'<link rel="canonical" href="{href(locale)}">']
    for code in [DEFAULT_LOCALE, *LOCALES]:
        lines.append(f'<link rel="alternate" hreflang="{code}" href="{href(code)}">')
    lines.append(f'<link rel="alternate" hreflang="x-default" href="{href(DEFAULT_LOCALE)}">')
    return "\n".join(lines)


def render(page: str, locale: str, catalog: dict[str, str]) -> str:
    source = (SITE / page).read_text(encoding="utf-8")

    # 1. Translate, right to left so earlier offsets stay valid.
    replacements = []
    for start, end, kind, raw in collect(source):
        msgid = to_msgid(raw) if kind == "block" else " ".join(raw.split())
        translated = catalog.get(msgid)
        if not translated or translated == msgid:
            continue
        replacements.append(
            (start, end, from_msgid(translated, raw) if kind == "block" else translated)
        )
    out = source
    for start, end, new in sorted(replacements, reverse=True):
        out = out[:start] + new + out[end:]

    # 2. The document's own language, which a screen reader picks its voice
    #    from and the browser picks hyphenation and quote marks from.
    direction = "rtl" if locale in RTL else "ltr"
    out = out.replace('<html lang="en">', f'<html lang="{locale}" dir="{direction}">', 1)

    # 3. hreflang for all twelve. Without them, twelve URLs carrying the same
    #    page are twelve competing duplicates to a crawler.
    out = out.replace('<link rel="stylesheet" href="/style.css">',
                      alternates(page, locale) + '\n<link rel="stylesheet" href="/style.css">', 1)

    # 4. Internal links point at this locale's copies.
    out = localize_links(out, locale)

    # 5. The picker, beside the theme control in the header.
    out = out.replace('<div class="theme-picker" hidden></div>',
                      picker(locale, page, catalog) + '\n    <div class="theme-picker" hidden></div>', 1)

    # 6. Dates in the reader's convention. "Sep 11, 2026" is a US spelling of
    #    a value already in the markup: `<time datetime=...>` carries the ISO
    #    date, so this reformats from THAT rather than parsing the English.
    #    Babel is already a dashboard dependency and runs at build time only,
    #    so nothing is added to what the site serves.
    out = localize_dates(out, locale)
    return out


_TIME = re.compile(r'<time datetime="(\d{4})-(\d{2})-(\d{2})">[^<]*</time>')


def localize_dates(html: str, locale: str) -> str:
    import datetime

    from babel.dates import format_date

    def one(m: re.Match) -> str:
        day = datetime.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        shown = format_date(day, format="medium", locale=locale.replace("-", "_"))
        return f'<time datetime="{day.isoformat()}">{shown}</time>'

    return _TIME.sub(one, html)


def build() -> dict[pathlib.Path, str]:
    """Every file this generator is responsible for, and its content."""
    wanted: dict[pathlib.Path, str] = {}
    for locale in LOCALES:
        catalog = load_catalog(locale)
        for page in PAGES:
            wanted[SITE / locale / page] = render(page, locale, catalog)
    return wanted


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="exit 1 if the output is stale")
    ap.add_argument("--extract", action="store_true", help="refresh the msgid inventory")
    args = ap.parse_args()

    if args.extract:
        LOCALES_DIR.mkdir(exist_ok=True)
        INVENTORY.write_text(
            json.dumps(extract(), ensure_ascii=False, indent=1) + "\n", encoding="utf-8"
        )
        print(f"wrote {INVENTORY.relative_to(REPO)} ({len(extract())} msgids)")
        return 0

    wanted = build()
    if args.check:
        stale = [p for p, text in wanted.items() if not p.exists() or p.read_text("utf-8") != text]
        extra = [
            p for locale in LOCALES for p in (SITE / locale).glob("*.html")
            if p not in wanted
        ]
        if stale or extra:
            for p in stale:
                print(f"  stale: {p.relative_to(REPO)}")
            for p in extra:
                print(f"  orphan: {p.relative_to(REPO)}")
            print("\nRun: python scripts/gen_site_locales.py")
            return 1
        print(f"{len(wanted)} generated pages are up to date")
        return 0

    for path, text in wanted.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    print(f"wrote {len(wanted)} pages under site/<locale>/")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
