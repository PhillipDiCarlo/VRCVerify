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

THE STRINGS NEED NOTHING FROM BABEL. THE DATES DO
-------------------------------------------------
`pybabel` extracts, updates and compiles; what ships is the compiled `.mo`,
read by `gettext` from the standard library. That is why the `.mo` files are
committed rather than built in the image -- no compiler is installed for them,
and copying `src/dashboard/` is the whole of what it takes. None of that
changed, and translating a *string* still costs this host no dependency.

Formatting a *date* does, and #230 paid it. `strftime('%B')` answers from the
process-global C locale, which four gunicorn threads cannot each set to a
different language; the only other option was our own month-name table, which
means our own copy of CLDR, wrong in the genitive in Russian and wrong about
lakh grouping in three of the twelve. `Babel` is in requirements-dashboard.txt
now and the note there argues the reversal properly.

So: `gettext` decides what a label says, Babel decides what a date and a
number look like, and `format_date`/`format_number` below are the only two
places in the dashboard that call it.

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

import os
import re
from datetime import date as _date, datetime as _datetime, timezone as _timezone
from typing import Callable, Iterable, Optional

from babel import Locale as _Locale
from babel.dates import (
    format_date as _babel_format_date,
    format_skeleton,
    format_time as _babel_format_time,
)
from babel.numbers import format_decimal as _babel_format_decimal

from i18n_core import Catalogues as _Catalogues, N_ as _N_

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
#
# NO `\s*` AT EITHER END, AND THAT IS THE POINT. This pattern used to be
# anchored `^\s*...\s*$`, which CodeQL flagged as a polynomial regular
# expression on uncontrolled data, correctly. On input like `"en" + " " * n`
# followed by one character that cannot match, the trailing `\s*` gives its
# spaces back one at a time and each attempt rescans the rest: measured at
# 7us for 50 spaces and 562us for 500, which is the shape of n**2.
#
# `MAX_ACCEPT_LANGUAGE` capped the damage at about half a millisecond per
# request and never removed it, and half a millisecond of free CPU on an
# unauthenticated endpoint is a thing an attacker gets to multiply by their
# request rate. The caller strips each part instead, so the ends are already
# clean and there is nothing left for the engine to backtrack over. Same
# accepted language tags, same rejections -- verified across 200,000 inputs --
# in constant time rather than quadratic.
#
# The `\s*` before `;` stays. It is followed by a literal that cannot itself
# be whitespace, so there is no ambiguity for the engine to explore.
_TAG = re.compile(
    r"^([A-Za-z]{1,8}(?:-[A-Za-z0-9]{1,8})*|\*)(?:\s*;\s*q\s*=\s*([01](?:\.\d{0,3})?))?$"
)

# Base language to the code we actually have. Consulted only when the exact tag
# missed, so `pt-PT` and `pt` both land on Brazilian Portuguese: one Portuguese
# catalogue read by a Portuguese speaker beats English.
_BY_BASE = {}
for _code in UI_LANGUAGES:
    _BY_BASE.setdefault(_code.split("-")[0].lower(), _code)


# `N_` is re-exported rather than redefined. The view modules and the templates
# import it from here and always have; moving the definition to i18n_core so
# the bot can share it (#231) is not a reason to make forty call sites say
# where it lives now.
N_ = _N_


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
        # Stripped here rather than absorbed into the pattern: see the note on
        # `_TAG`. `str.strip` is a single linear scan; the `\s*` anchors it
        # replaces were quadratic.
        match = _TAG.match(part.strip())
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


# Reading the compiled catalogues is the part the bot does identically, so it
# lives in i18n_core and this is the dashboard's instance of it (#231). The
# domain, the directory and the language list are what make it the dashboard's;
# everything about *choosing* a language stays here, because it is all a web
# question -- a cookie, an Accept-Language header, a dir attribute.
_CATALOGUES = _Catalogues(
    domain=DOMAIN,
    localedir=LOCALE_DIR,
    languages=UI_LANGUAGES,
    default=DEFAULT_LANGUAGE,
)


def catalogue(code: str):
    """The compiled catalogue for one language, as a `gettext` translations
    object.

    Exposed as well as `translator()` because Jinja's i18n extension wants the
    object rather than a callable. See `i18n_core.Catalogues.catalogue`.
    """
    return _CATALOGUES.catalogue(code)


def translator(code: str) -> Callable[[str], str]:
    """The `gettext` callable for one language.

    Passed into the view modules as an argument, which is the whole reason it
    exists as a separate thing from `catalogue()`: those modules take a
    callable, not a Flask global and not a translations object they would then
    have to know the shape of. That is what lets the page that takes money have
    its states tested without a request, and it is why a test can pass
    `lambda s: s` or a catalogue for a language it wants to assert on.

    Returns the msgid unchanged for `en-US`, for a language with no catalogue
    yet, and for any string not yet translated in the catalogue it does have.
    That last one is the property that let #97 land in phases: the payment
    pages were translated first, because that is where a misunderstanding costs
    money, and every string not reached yet rendered in English rather than
    rendering blank.
    """
    return _CATALOGUES.translator(code)


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


# Our language codes to Babel's. Same twelve, hyphen to underscore -- but
# resolved once at import and held, because `Locale.parse` reads and validates
# CLDR data and there is no reason to do that again on every render.
#
# Built from `UI_LANGUAGES` rather than written out, so a thirteenth language
# cannot be added to the list above and forgotten here. If Babel does not know
# a code, that is a real problem with the language being added and it should
# surface at import on the next deploy rather than as a 500 on one page.
_LOCALES = {code: _Locale.parse(code.replace("-", "_")) for code in UI_LANGUAGES}


def _locale(code: Optional[str]):
    """The Babel locale for one of our codes, English for anything else.

    The same floor `catalogue()` puts under itself, for the same reason: the
    callers have all validated, and a formatting helper is the wrong place to
    raise on a language that should never have got this far.

    GATED THROUGH `is_supported` RATHER THAN `_LOCALES.get`, which is the same
    check `catalogue()` makes and is not the same thing. `.get` hashes its
    argument, so an unhashable one -- a list, a dict -- raises `TypeError`
    from inside the floor that exists to stop exactly that. `is_supported`
    tests membership of a tuple by equality and has no such edge.

    Nothing can currently reach here with one: `negotiate()` returns a string
    from `UI_LANGUAGES` or the default. That is an argument about every
    caller, and this is the line that means nobody has to make it.
    """
    if not is_supported(code):
        return _LOCALES[DEFAULT_LANGUAGE]
    return _LOCALES[code]


def to_date(value) -> Optional[_date]:
    """A `date`, an ISO string or an ISO instant as a plain `date` -- or None.

    The one place the dashboard turns what the bot sent into something to
    format. Everything here arrives over the wire as a string, so nothing
    raises: a field the bot has never sent, or has started sending in a shape
    this image does not know, renders as an absent date rather than as a 500 on
    the page that takes money.

    UTC IS APPLIED BEFORE THE DATE IS TAKEN, not after. Stripe's period end is
    an instant, and `datetime.date()` on it would silently use whatever offset
    the string carried; two readers would then see two different days for one
    renewal. The bot decides what day it is and it decides in UTC.
    """
    if isinstance(value, _datetime):
        parsed = value
    elif isinstance(value, _date):
        return value
    elif isinstance(value, str) and value:
        try:
            parsed = _datetime.fromisoformat(value.replace("Z", "+00:00"))
        except (TypeError, ValueError):
            try:
                # A bare `2026-08-24`, which is what the daily series and
                # `collecting_since` send. `fromisoformat` handles this on its
                # own; the branch exists for the value that is neither.
                return _date.fromisoformat(value)
            except (TypeError, ValueError):
                return None
    else:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_timezone.utc)
    return parsed.astimezone(_timezone.utc).date()


def format_date(value, code: str, *, form: str = "long") -> Optional[str]:
    """One date, written the way `code` writes dates. None if there isn't one.

    `form` is Babel's, and the two the dashboard uses mean different things:

    * **`long`** -- "3 February 2027", "2027年2月3日", "3 февраля 2027 г.". The
      month is a word, so there is no chance of a reader taking 3/2 for the
      second of March. Everything about money uses this.
    * **`medium`** -- "24 Aug 2026", and fully numeric in the locales that
      write a compact date that way. For the changelog's date badge, where the
      surrounding context already says what it is and the space is a line.

    NOT `%-d` AND NOT A PATTERN OF OURS. The old code built "3 February 2026"
    by hand to dodge `%-d`, which is a glibc extension the standard does not
    promise -- see the git history of `subscription_view._format_date`. This
    has the same property and one more: the field ORDER is the locale's, so
    Japanese gets year-first and American English gets the month first,
    neither of which a single hand-written pattern can do.
    """
    parsed = to_date(value)
    if parsed is None:
        return None
    return _babel_format_date(parsed, format=form, locale=_locale(code))


def format_day(value, code: str) -> Optional[str]:
    """A day and month with no year: "Aug 24", "8月24日", "24 авг.".

    For the chart's per-day rows, where thirty of these are read down a column
    and every one of them is in the same year the heading already gave.

    A skeleton rather than a pattern, which is the whole reason this is a
    separate function: `"MMM d"` written out would put the month first in
    Japanese too. `format_skeleton` asks CLDR which order this locale actually
    uses for those two fields and returns that.
    """
    parsed = to_date(value)
    if parsed is None:
        return None
    return format_skeleton("MMMd", parsed, locale=_locale(code))


def format_number(value, code: str) -> str:
    """An integer with the group separators `code` uses.

    Not everywhere is a comma every three digits. German and Spanish group with
    a full stop, Russian with a non-breaking space, and Hindi, Bengali and
    Punjabi group by lakh -- 1234567 is "12,34,567", which is not a rounding of
    the same shape but a different shape. A hardcoded `f"{n:,}"` is wrong in
    five of the twelve languages this dashboard speaks.

    None is "", never the word "None". Every caller guards a missing count
    before it gets here -- a tile checks its state, the chart's table checks
    `bar.count is none` -- so this is the floor under those guards and not a
    substitute for them. It exists because the failure it prevents is the
    string `None` appearing on the page, which is the one wrong answer worse
    than a blank.

    Anything else that is not an int comes back as `str(value)`, so a tile
    renders an odd value rather than failing.
    """
    if value is None:
        return ""
    if isinstance(value, bool) or not isinstance(value, int):
        return str(value)
    return _babel_format_decimal(value, locale=_locale(code))


def format_timestamp(value, code: str) -> Optional[str]:
    """An ISO instant as a date and a time in `code`, always marked UTC.

    "Aug 11, 2026 7:11 AM UTC", "11.08.2026 07:11 UTC", "2026/08/11 7:11 UTC".

    THE "UTC" IS NOT DECORATION AND IS NOT LOCALISED. The audit trail is what
    an admin reads to work out who changed what and when, and the bot records
    those instants in UTC. Rendering the clock time without naming the zone
    would invite every reader to subtract their own offset from a number that
    had not had it added -- so the marker stays, in the one form that is the
    same three letters on every one of these twelve pages.

    Only the *shape* is the locale's: whether the day or the month leads, and
    whether 07:11 is written that way or as 7:11 AM. Both are genuine
    differences between readers and neither changes the instant.

    None for anything unparseable, which the caller renders as no timestamp at
    all. The value comes from the bot over the wire.
    """
    if isinstance(value, _datetime):
        parsed = value
    elif isinstance(value, str) and value:
        try:
            parsed = _datetime.fromisoformat(value.replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None
    else:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_timezone.utc)
    parsed = parsed.astimezone(_timezone.utc)
    locale = _locale(code)
    day = _babel_format_date(parsed.date(), format="medium", locale=locale)
    clock = _babel_format_time(parsed.timetz(), format="short", locale=locale)
    return f"{day} {clock} UTC"


def _warm() -> None:
    """Resolve every format this module uses, for all twelve, at import.

    NOT A CACHE FOR SPEED. Babel's locale data is lazy in a way that matters
    here: `LocaleDataDict.__getitem__` resolves aliases on first read and
    WRITES THE RESULT BACK into a dict shared by every thread that asked for
    the same locale. The image runs `gunicorn --threads 4`.

    That particular write is benign -- the value each thread computes is
    equivalent, and storing it is atomic under the GIL -- so this is not the
    bug `install_gettext_callables` in app.py was written to fix, where two
    threads wrote *different* languages into one dict. It is the same shape
    though, and the cost of never having to make that argument again at a
    later reading is 23 milliseconds of worker startup, measured.

    It also flattens the first request. Without it the first person to load a
    page in a given language pays that language's resolution; with it, every
    request is the warm path, which is about 1.6 microseconds a call.

    Failures are deliberately not caught. A locale in `UI_LANGUAGES` that
    Babel cannot format is a broken deploy, and it should be broken at import
    on the host rather than on one page in one language that nobody tests in.
    """
    sample_date = _date(2027, 2, 3)
    sample_time = _datetime(2027, 2, 3, 7, 11, tzinfo=_timezone.utc)
    for code in UI_LANGUAGES:
        locale = _LOCALES[code]
        _babel_format_date(sample_date, format="long", locale=locale)
        _babel_format_date(sample_date, format="medium", locale=locale)
        format_skeleton("MMMd", sample_date, locale=locale)
        _babel_format_time(sample_time.timetz(), format="short", locale=locale)
        _babel_format_decimal(1234567, locale=locale)


_warm()
