"""Reading a compiled gettext catalog. Shared by the bot and the dashboard.

WHY THIS FILE EXISTS AS A THIRD THING
-------------------------------------
#97 gave the dashboard gettext catalogs and left the bot's twelve languages
in the `localizations` dict in locales.py. #231 converts the bot too, and the
forty lines that open a `.mo`, cache it and hand back a `gettext` callable are
identical for both. The question #231 had to answer explicitly was whether to
share them or duplicate them.

**Neither side can import the other.** `dashboard/i18n.py` refuses to import
`locales.py` on purpose, and says why: the dashboard image ships api_tokens.py,
log_safety.py and the dashboard package, and nothing else, so importing a bot
module there would drag the bot's locales into the internet-facing image to
satisfy an import. The reverse -- the bot importing `src/dashboard/` -- is the
same trade and worse, because Dockerfile-dashboard's tree is deliberately
minimal and making it a dependency of the bot invites people to add to it.

**Duplication was rejected over what a test can pin.** The alternative on the
table was forty copied lines in the bot with a test asserting the two behave
alike. That test can only pin the behavior the copy has on the day it is
written. It cannot pin the fallback somebody adds to one copy a year later,
which is exactly the divergence that would matter and exactly the one nobody
would notice: both halves would still work, and only one of them the way the
other's reader expected.

So: a flat module, alongside api_tokens.py and log_safety.py, which is the
shape the dashboard image already knows how to ship. Dockerfile-dashboard
gains one COPY line. Dockerfile-bot already copies all of src/.

WHAT BELONGS HERE, AND WHAT DOES NOT
------------------------------------
Here: turning a language code into a `gettext` object, caching it, and the
`N_` no-op the extractor keys on. That is the part both callers need and the
part where a divergence would be silent.

Not here: everything about *choosing* a language. `negotiate`,
`parse_accept_language`, `direction`, `ENDONYMS`, `choices` and the Babel date
and number formatting stay in `dashboard/i18n.py` because they answer a web
question -- a cookie, an `Accept-Language` header, a `dir` attribute on
`<html>`. The bot is handed a locale by Discord and has nothing to negotiate.
Moving those here to make this file "the i18n module" would put HTTP header
parsing into the bot's import graph to no purpose.

NO NEW DEPENDENCY, EITHER SIDE
------------------------------
`gettext` is standard library. `pybabel` extracts, merges and compiles, and
what ships is the compiled `.mo` read from here -- which is why the `.mo` files
are committed rather than built in the images. Babel is in
requirements-dashboard.txt for #230's date and number formatting, and that is a
dashboard runtime need this module does not share: requirements-bot.txt gains
nothing from #231.
"""

from __future__ import annotations

import gettext as _gettext
from typing import Callable, Iterable


def N_(text: str) -> str:
    """Mark a string for translation without translating it here and now.

    The standard gettext no-op. Both callers have the same problem it solves:
    their strings live in module-level tables evaluated once at import, long
    before anything has said which language it wants. The dashboard's view
    modules hold labels that way; after #231 so does locales.py, where every
    bot string is an `N_()` constant.

    Calling a real `_()` in those tables would freeze whichever language the
    first import happened to see into every later render. So the table holds
    the msgid unchanged -- which `pybabel extract -k N_` can see -- and the
    lookup happens at the point of use, against the language that request or
    that interaction actually asked for.

    Returns its argument. The whole of its work is being a name the extractor
    recognizes. `-k N_` in scripts/i18n.sh is not optional: without it every
    one of these msgids is silently missing from the .pot, and the only symptom
    is a string that stays in English.
    """
    return text


# The source language, and the one the public site serves unprefixed.
#
# Both callers spell English this way -- `locales.LANGUAGE_CODES[0]` and
# `dashboard.i18n.DEFAULT_LANGUAGE` -- and neither has a catalog for it,
# because its "catalog" is the msgids themselves. Defined here so that the two
# cannot drift into disagreeing about which language needs no translating.
DEFAULT_LANGUAGE = "en-US"


def localized_path(code: str, slug: str = "") -> str:
    """Where a page of the public site lives, in one language.

    `localized_path("ja", "/terms")` -> "/ja/terms"
    `localized_path("en-US", "/terms")` -> "/terms"
    `localized_path("ja")` -> "/ja/"

    WHY THIS IS SHARED RATHER THAN WRITTEN AT EACH CALL SITE, which is the
    question this module's docstring asks of everything in it.

    Three surfaces link at those pages -- the status Worker (#311), the
    dashboard (#313) and the bot (#314) -- and the rule they need has a trap in
    it that only bites one language. English is `en-US` on both Python sides
    and the apex serves it UNPREFIXED, with no `site/en-US/` directory and
    never one. The other eleven codes are identical everywhere. So
    `f"/{code}{slug}"` is correct for eleven languages out of twelve and sends
    every default-language reader, who are most of them, to a 404 on a Worker
    with no code on its request path to catch it.

    That is exactly the divergence this module exists to prevent: a copy that
    works the day it is written, and a fallback added to one copy a year later
    that nobody notices because both halves still work.

    THE REST OF THE SHAPE IS NOT OURS EITHER. It has to match `picker()` in
    scripts/gen_site_locales.py: the code keeps its own case, because
    `/pt-br/terms` is a 404 rather than a redirect on an assets-only Worker,
    and the index keeps a trailing slash the other pages do not have.

    The ORIGIN is deliberately not here. The dashboard always means our apex;
    the bot means whatever `WEBSITE_URL` is set to, which for a self-hoster is
    their own site. Whether an origin should be localized at all is a
    deployment question, and it is answered at the call site.
    """
    prefix = "" if code == DEFAULT_LANGUAGE else f"/{code}"
    return f"{prefix}{slug or '/'}"


class Catalogs:
    """The compiled catalogs for one domain, read once and kept.

    One instance per domain: the dashboard's `dashboard.mo` files and the bot's
    `bot.mo` files stay separate, so neither image carries the other's strings.

    Catalogs are read from disk on first use and cached for the life of the
    process. There are eleven of them per domain, they are small, and they
    cannot change without a deploy -- so the alternative is re-reading the same
    file on every request and every interaction forever.
    """

    def __init__(
        self,
        *,
        domain: str,
        localedir: str,
        languages: Iterable[str],
        default: str = "en-US",
    ) -> None:
        self._domain = domain
        self._localedir = localedir
        self._languages = frozenset(languages)
        self._default = default
        self._cache: dict = {}

    def supports(self, code) -> bool:
        """Whether this domain can render `code`.

        The floor under the callers rather than the check itself -- the
        dashboard validates untrusted cookies and headers in its own
        `is_supported`, and the bot only ever passes a code Discord gave it.
        What this guarantees is that an unexpected value degrades to English
        instead of reaching `gettext.translation` and, through it, the
        filesystem.
        """
        return isinstance(code, str) and code in self._languages

    def catalog(self, code: str):
        """The catalog for one language, as a `gettext` translations object.

        Exposed as well as `translator()` because Jinja's i18n extension wants
        the object -- `NullTranslations` and its GNU subclass already carry
        both `gettext` and `ngettext`, so handing it over directly saves an
        adapter class whose only job would be to forward two methods.

        An unsupported code returns the English no-op rather than raising.
        """
        if not self.supports(code):
            code = self._default

        cached = self._cache.get(code)
        if cached is None:
            cached = self._load(code)
            self._cache[code] = cached
        return cached

    def translator(self, code: str) -> Callable[[str], str]:
        """The `gettext` callable for one language.

        A separate thing from `catalog()` because both callers want a plain
        callable rather than an object whose shape they would have to know: the
        dashboard's view modules take one as an argument, which is what keeps
        them free of Flask globals, and the bot's `get_message` calls it and
        then `.format()`s the result.

        Returns the msgid unchanged for the default language, for a language
        with no catalog yet, and for any string not yet translated in the
        catalog it does have. That last one is the property worth naming:
        every gap renders in English rather than rendering blank, so a
        half-translated surface is worse than a fully English one and much
        better than an empty one.
        """
        return self.catalog(code).gettext

    def _load(self, code: str):
        """Open one compiled catalog, or a no-op stand-in if there is not one."""
        if code == self._default:
            # The source language's "catalog" is the msgids themselves. There
            # is no en-US directory under translations/ and there should never
            # be one: it would be a file full of entries translating English
            # into the same English, with every one of them a chance to drift.
            return _gettext.NullTranslations()
        # gettext names directories with an underscore (`pt_BR`); Discord, the
        # bot and the dashboard all use a hyphen (`pt-BR`). The hyphen is what
        # everything outside this method speaks; the translation is done here
        # and nowhere else.
        return _gettext.translation(
            self._domain,
            localedir=self._localedir,
            languages=[code.replace("-", "_")],
            # A missing catalog renders English. The alternative is a surface
            # that raises because a language was added to the list before its
            # file was compiled, and a deploy that half-lands should degrade to
            # English rather than to nothing.
            fallback=True,
        )
