"""Which language a dashboard page renders in, and where its strings come from.

The bot has spoken twelve languages since long before this website existed.
Configuration moved here in #65 and the payment page landed with #88, so the
two things a non-English-speaking admin now has to do are both done on pages
that only spoke English. This module is the half of #97 that decides *which*
language; the strings themselves live in `translations/` as gettext catalogues.

WHY GETTEXT AND NOT ANOTHER DICT
--------------------------------
`locales.py` is a dict-of-dicts, and copying that shape here was the obvious
move. It was rejected for one reason: the dashboard's strings need translating
by people, in a tool translators already own, and nobody is opening a 1,200
line Python literal in Poedit. `.po` files are the format that entire industry
speaks, and `pybabel extract` re-reads the templates rather than trusting
somebody to remember to add the key.

The cost of two systems is real -- #97 says two systems that disagree about
what "verified" is called in Japanese is worse than either alone -- and it is
paid down deliberately: the language *list* is pinned against the bot's by a
test (see tests/test_i18n.py), so the two can never drift apart on which
languages exist. What they say inside a language is reviewed by the same
person either way.

NOTHING FROM BABEL RUNS IN THE IMAGE
------------------------------------
`pybabel` extracts, updates and compiles, and it is a *dev* dependency: see
requirements-dev.txt. What ships is the compiled `.mo`, read by `gettext` from
the standard library. requirements-dashboard.txt argues that every dependency
on this host is something an attacker gets to probe, and that argument does not
stop being true because a feature would find a library convenient. So the
runtime cost of this whole module is zero new packages.

That is also why the `.mo` files are committed rather than compiled at build
time. The image installs no compiler for them and copying `src/dashboard/`
is the whole of what it takes.

WHY THE VIEW MODULES ARE NOT TOUCHED BY THIS
--------------------------------------------
`flask_babel.gettext` reads the request context, and `subscription_view.py`,
`settings_view.py` and `overview_view.py` are pure by policy: no Flask, no
network, no clock. That policy is what lets the page that takes money have its
states tested without a request, so a translation mechanism that quietly
required one would be trading the most valuable property those modules have
for the convenience of a global.

They take a `gettext` callable as an argument instead. `translator()` below
returns one, `app.py` passes it in, and a test can pass `lambda s: s` or a
catalogue for a language it wants to assert on.
"""

from __future__ import annotations

import gettext as _gettext
import os
import re
from typing import Callable, Iterable, Optional

# The languages this dashboard has catalogues for.
#
# Deliberately a literal rather than `from locales import LANGUAGE_CODES`: the
# dashboard image ships api_tokens.py, log_safety.py and this package, and
# nothing else. Importing a bot module here would mean copying the bot's
# locales into the internet-facing image to satisfy an import, which is a lot
# of blast radius for a list of twelve strings.
#
# tests/test_i18n.py pins this equal to `locales.LANGUAGE_CODES`. The test runs
# from the repo root where both are importable, so drift is caught at the one
# moment it can be -- when somebody adds a thirteenth language to the bot.
UI_LANGUAGES = (
    "en-US", "es-ES", "zh-CN", "ja", "de", "nl",
    "hi-IN", "ar", "bn", "pt-BR", "ru", "pa-IN",
)

# The source language. Its "catalogue" is the msgids themselves, so there is no
# en-US directory under translations/ and there should never be one.
DEFAULT_LANGUAGE = "en-US"

# Names in the language they name, not in English.
#
# `settings_view.LOCALE_NAMES` says "Japanese" because it labels a setting an
# English-reading admin is choosing *for their members*. This picker is a
# different question -- it is read by the person who cannot read the page --
# and "Japanese" is no help to somebody looking for the word they would
# recognise. So: 日本語. The English name is not shown alongside; a picker that
# reads "日本語 (Japanese)" is twice the width to say the same thing to the one
# person who does not need the second half.
ENDONYMS = {
    "en-US": "English",
    "es-ES": "Español",
    "zh-CN": "简体中文",
    "ja": "日本語",
    "de": "Deutsch",
    "nl": "Nederlands",
    "hi-IN": "हिन्दी",
    "ar": "العربية",
    "bn": "বাংলা",
    "pt-BR": "Português (Brasil)",
    "ru": "Русский",
    "pa-IN": "ਪੰਜਾਬੀ",
}

# Written right to left. Only `dir` on <html> is set from this; making the
# *layout* mirror is a stylesheet job that style.css has never been asked to do
# and is tracked separately.
#
# `dir` is set from the first day anyway rather than waiting for that work,
# because it is not the same question. `dir` governs the reading order of the
# text itself -- which end of the line a sentence starts at, where the cursor
# goes in an input, how a mixed Arabic-and-Latin string like "VRCVerify
# Premium" is ordered. Getting that wrong is unreadable in a way an
# unmirrored sidebar is not.
RTL_LANGUAGES = frozenset({"ar"})

# Where the compiled catalogues live, and what they are called. One domain for
# the whole dashboard: splitting per page would mean deciding which file a
# string in base.html belongs to, and base.html is on every page.
DOMAIN = "dashboard"
LOCALE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "translations")

# An Accept-Language header is attacker-controlled and arrives on every single
# request, so it gets a ceiling before it gets a regex. A real one from a real
# browser is well under 100 characters; this allows for the unusual and refuses
# to spend time on the absurd.
MAX_ACCEPT_LANGUAGE = 512

# One tag out of an Accept-Language header: `en-GB`, `zh-Hant-TW`, `*`, with an
# optional `;q=0.8`. Anything that does not match this shape is dropped rather
# than repaired -- a header we cannot parse is a header we render en-US for,
# which is exactly what would have happened before this module existed.
_TAG = re.compile(
    r"^\s*([A-Za-z]{1,8}(?:-[A-Za-z0-9]{1,8})*|\*)\s*(?:;\s*q\s*=\s*([01](?:\.\d{0,3})?))?\s*$"
)

# Base language to the code we actually have. Consulted only when the exact tag
# missed, so `pt-PT` and `pt` both land on Brazilian Portuguese: one Portuguese
# catalogue read by a Portuguese speaker beats English.
_BY_BASE = {}
for _code in UI_LANGUAGES:
    _BY_BASE.setdefault(_code.split("-")[0].lower(), _code)


def N_(text: str) -> str:
    """Mark a string for translation without translating it here and now.

    The standard gettext no-op, and the answer to a problem the view modules
    all have: their labels live in module-level tables, evaluated once at
    import, long before any request has said which language it wants. Calling
    `_()` there would freeze whichever language the first import happened to
    see into every later render.

    So the table holds `N_("Renews")` -- which is the msgid, unchanged, and
    which `pybabel extract -k N_` can see -- and the *lookup* happens at the
    point of use, against the callable that request was given.

    Returns its argument. The whole of its work is being a name the extractor
    recognises.
    """
    return text


def is_supported(code: Optional[str]) -> bool:
    """Whether `code` is a language this dashboard can render.

    The gate every untrusted value passes through. A cookie is edited by hand
    as easily as it is written by a click, and the chosen code ends up in a
    `lang` attribute and in a filesystem path, so nothing reaches either
    without having been found in `UI_LANGUAGES` first.
    """
    return code in UI_LANGUAGES


def parse_accept_language(header: Optional[str]) -> list:
    """The languages this browser asked for, best first, as codes we support.

    Returns only codes in `UI_LANGUAGES`, so the caller never has to re-check.
    Unparseable tags are skipped individually: one bad entry in a header should
    not throw away the good ones after it.
    """
    if not header or len(header) > MAX_ACCEPT_LANGUAGE:
        return []

    ranked = []
    for part in header.split(","):
        match = _TAG.match(part)
        if not match:
            continue
        tag, quality = match.group(1), match.group(2)
        weight = 1.0 if quality is None else float(quality)
        # `q=0` means "explicitly not this one", which is a refusal rather
        # than a weak preference.
        if weight <= 0:
            continue
        ranked.append((weight, len(ranked), tag))

    # Sort by quality descending, then by the order they arrived. Browsers send
    # equal-q tags in preference order and a stable tiebreak is what preserves
    # it; Python's sort is stable, and the index makes that explicit rather
    # than incidental.
    ranked.sort(key=lambda item: (-item[0], item[1]))

    chosen = []
    for _weight, _index, tag in ranked:
        code = _match_tag(tag)
        if code and code not in chosen:
            chosen.append(code)
    return chosen


def _match_tag(tag: str) -> Optional[str]:
    """One Accept-Language tag to a supported code, exactly then by base."""
    if tag == "*":
        # "anything you like" is not a preference, and treating it as one would
        # let a wildcard outrank the guild's configured language.
        return None
    lowered = tag.lower()
    for code in UI_LANGUAGES:
        if code.lower() == lowered:
            return code
    return _BY_BASE.get(lowered.split("-")[0])


def negotiate(
    cookie: Optional[str] = None,
    guild_locale: Optional[str] = None,
    accept_language: Optional[str] = None,
) -> str:
    """Which language to render, from the three things that get a say.

    Pure, and takes three values rather than a request, so the precedence can
    be asserted directly instead of through a test client.

    The order, and why it is this order:

    1. **The picker.** An explicit choice beats an inference, always. #97 notes
       that the guild's language is "the least discoverable if it is wrong",
       and this is the answer to that: whatever else decides, a person who
       disagrees has a control that wins.
    2. **The guild's `instructions_locale`.** The language the admin already
       chose for their members. Consistent with the bot, and it means a server
       configured in German gets a German dashboard with nobody doing anything.
       Absent on the pages that have no guild in scope -- login and the picker
       -- which is exactly why it cannot be the only input.
    3. **`Accept-Language`.** What the browser says. Covers the first visit, and
       covers signing in, which happens before any guild is known.
    4. **English**, which is what every page did before this.

    An unsupported value at any level falls through to the next rather than
    failing: a stale cookie naming a language that has since been dropped
    should show the guild's language, not an error.
    """
    if is_supported(cookie):
        return cookie
    if is_supported(guild_locale):
        return guild_locale
    for code in parse_accept_language(accept_language):
        return code
    return DEFAULT_LANGUAGE


# Catalogues are read from disk once and kept. There are twelve of them, they
# are small, and they cannot change without a deploy -- so the alternative is
# re-reading the same file on every request for the rest of the process's life.
_catalogues: dict = {}


def catalogue(code: str):
    """The compiled catalogue for one language, as a `gettext` translations
    object.

    Exposed as well as `translator()` because Jinja's i18n extension wants the
    object -- `NullTranslations` and its GNU subclass already carry both
    `gettext` and `ngettext`, so handing it over directly saves an adapter
    class whose only job would be to forward two methods.

    An unsupported code returns the English no-op rather than raising. The
    callers have all validated already; this is the floor under them, not the
    check itself.
    """
    if not is_supported(code):
        code = DEFAULT_LANGUAGE

    cached = _catalogues.get(code)
    if cached is None:
        cached = _load(code)
        _catalogues[code] = cached
    return cached


def translator(code: str) -> Callable[[str], str]:
    """The `gettext` callable for one language.

    Passed into the view modules as an argument, which is the whole reason it
    exists as a separate thing from `catalogue()`: those modules take a
    callable, not a Flask global and not a translations object they would then
    have to know the shape of.

    Returns the msgid unchanged for `en-US`, for a language with no catalogue
    yet, and for any string not yet translated in the catalogue it does have.

    That last one is the property that lets #97 land in phases: the payment
    pages are translated first because that is where a misunderstanding costs
    money, and every string not reached yet renders in English rather than
    rendering blank. A half-translated page is a worse page than a fully
    English one and a much better page than an empty one.
    """
    return catalogue(code).gettext


def _load(code: str):
    """Open one compiled catalogue, or a no-op stand-in if there is not one."""
    if code == DEFAULT_LANGUAGE:
        return _gettext.NullTranslations()
    # gettext names directories with an underscore (`pt_BR`), Discord and the
    # bot use a hyphen (`pt-BR`). The hyphen is what everything outside this
    # function speaks; the translation is done here and nowhere else.
    return _gettext.translation(
        DOMAIN,
        localedir=LOCALE_DIR,
        languages=[code.replace("-", "_")],
        # A missing catalogue renders English. The alternative is a page that
        # 500s because a language was added to the list before its file was
        # compiled, and a deploy that half-lands should degrade to English
        # rather than to nothing.
        fallback=True,
    )


def direction(code: str) -> str:
    """The `dir` attribute for <html>: "rtl" for Arabic, "ltr" for the rest."""
    return "rtl" if code in RTL_LANGUAGES else "ltr"


def choices() -> list:
    """`(code, endonym)` for the picker, in `UI_LANGUAGES` order.

    Not sorted alphabetically, because alphabetical by *what*: sorting 简体中文
    and Español into one order that reads correctly for both readers is not a
    thing that exists. The bot's order is at least a stable order somebody
    chose, and English first matches the fallback.
    """
    return [(code, ENDONYMS.get(code, code)) for code in UI_LANGUAGES]
