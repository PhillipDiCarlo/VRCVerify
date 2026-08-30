"""The dashboard's twelve languages (issue #97).

Split into three kinds of test, and the split is the point:

* **The negotiation** is pure, so it is asserted directly against
  `i18n.negotiate` rather than through a test client. Precedence between a
  cookie, a guild's setting and an `Accept-Language` header is the thing most
  likely to be got wrong later, and it deserves to fail in one line rather
  than in a page render.
* **The catalogues** are files on disk, and what is worth pinning about them
  is not their wording -- that is a translator's -- but that they exist, that
  they compile, and that they agree with the bot about which languages there
  are.
* **The request** is where the parts meet: the `lang` attribute, the picker,
  and the one thing that could go quietly wrong on the page that takes money.
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from dashboard import i18n  # noqa: E402


def _elapsed(call) -> float:
    """Seconds one call took. Used only by the backtracking guard below."""
    import time

    start = time.perf_counter()
    call()
    return time.perf_counter() - start


class TestTheLanguageListMatchesTheBot:
    """#97's first constraint, and the one a second system can violate.

    The issue is explicit that two translation systems disagreeing about what
    "verified" is called in Japanese is worse than either alone. The wording is
    a review problem and cannot be tested. WHICH LANGUAGES EXIST can be, and
    this is where the two systems are held together.
    """

    def test_the_dashboard_offers_exactly_what_the_bot_speaks(self):
        # Imported here rather than at module scope: the dashboard image does
        # not ship locales.py, and this test runs from the repo root where it
        # is importable. That asymmetry is the whole reason UI_LANGUAGES is a
        # literal instead of an import -- see the comment on it.
        from locales import LANGUAGE_CODES

        assert list(i18n.UI_LANGUAGES) == list(LANGUAGE_CODES)

    def test_every_language_has_a_name_in_its_own_script(self):
        """A picker labelled in English is no use to the person opening it."""
        for code in i18n.UI_LANGUAGES:
            assert code in i18n.ENDONYMS, code
            assert i18n.ENDONYMS[code].strip()

    def test_english_is_the_default_and_has_no_catalogue_directory(self):
        """Its catalogue is the msgids. A directory for it would be a file of
        entries translating English into the same English, each one a chance
        to drift."""
        assert i18n.DEFAULT_LANGUAGE == "en-US"
        assert not os.path.isdir(os.path.join(i18n.LOCALE_DIR, "en_US"))


class TestEveryCatalogueIsCompiledAndLoadable:
    """A .po that was never compiled is a translation that silently does not
    ship: gettext has no way to say "there is a newer translation you did not
    compile", it just serves the English. That failure is invisible in review
    and obvious to a reader, which is the worst way round."""

    @pytest.mark.parametrize(
        "code", [c for c in i18n.UI_LANGUAGES if c != i18n.DEFAULT_LANGUAGE]
    )
    def test_the_compiled_catalogue_is_in_the_tree(self, code):
        path = os.path.join(
            i18n.LOCALE_DIR, code.replace("-", "_"), "LC_MESSAGES", "dashboard.mo"
        )
        assert os.path.isfile(path), f"run scripts/i18n.sh: {path} is missing"

    @pytest.mark.parametrize(
        "code", [c for c in i18n.UI_LANGUAGES if c != i18n.DEFAULT_LANGUAGE]
    )
    def test_the_money_sentences_are_actually_translated(self, code):
        """The four #97 named as the expensive ones to misunderstand.

        Not a spot check on wording -- it cannot be, from here -- but on the
        thing that would make the whole feature a no-op: a catalogue that
        loads, reports success, and hands back the English for every string.
        """
        gettext = i18n.translator(code)
        for english in (
            "Renews",
            "Premium until",
            "Manage billing",
            "All plans renew automatically. Cancel any time from this page.",
        ):
            assert gettext(english) != english, f"{code}: {english!r} untranslated"

    @pytest.mark.parametrize(
        "code", [c for c in i18n.UI_LANGUAGES if c != i18n.DEFAULT_LANGUAGE]
    )
    def test_no_catalogue_hides_english_behind_a_fuzzy_flag(self, code):
        """The trap `pybabel --statistics` sets, and the one that caught us.

        A fuzzy entry is Babel's guess that an old translation still fits a
        changed English string. `--statistics` counts it as translated;
        `compile` without `--use-fuzzy` drops it. So a catalogue can report
        "154 of 154 (100%)" and serve English for four of them -- which is
        what happened when the sidebar's labels were added and "Settings" was
        fuzzy-matched against "Settings sections". The only symptom was an
        English word in a German sidebar, in a screenshot.

        Not shipping the guess is the right policy on a site where the changed
        strings are disproportionately about money. Being told is the part
        that was missing.
        """
        pytest.importorskip("babel", reason="Babel is a dev-only dependency")
        from babel.messages.pofile import read_po

        path = os.path.join(
            i18n.LOCALE_DIR, code.replace("-", "_"), "LC_MESSAGES", "dashboard.po"
        )
        with open(path, "rb") as handle:
            catalog = read_po(handle)
        fuzzy = [m.id for m in catalog if m.id and "fuzzy" in m.flags]
        untranslated = [m.id for m in catalog if m.id and not m.string]
        assert not fuzzy, f"{code}: fuzzy entries render English: {fuzzy}"
        assert not untranslated, f"{code}: untranslated: {untranslated}"

    @pytest.mark.parametrize(
        "code", [c for c in i18n.UI_LANGUAGES if c != i18n.DEFAULT_LANGUAGE]
    )
    def test_every_translation_keeps_its_placeholders_and_markup(self, code):
        """The one class of translation error that is not a matter of taste.

        A msgstr that drops `%(name)s` renders a sentence with a hole in it; one
        that renames it to `%(nombre)s` raises KeyError at render time, on the
        page that takes money. A dropped `</strong>` leaks bold through the rest
        of the card, and a mangled `href` produces a link that goes nowhere.

        None of these is something a native reviewer is looking for -- they are
        reading the words -- and all of them are mechanically checkable, so they
        are checked here rather than hoped about. This matters most for the
        languages whose script makes an unbalanced tag hard to spot by eye.

        Compared as SETS and multisets rather than in order: a translator
        moving `%(days)s` to the front of the sentence, or the `<strong>` run
        to a different clause, is doing their job. Only losing or inventing one
        is an error.
        """
        pytest.importorskip("babel", reason="Babel is a dev-only dependency")
        import re

        from babel.messages.pofile import read_po

        placeholder = re.compile(r"%\([a-z_]+\)s")
        tag = re.compile(r"</?([a-z]+)(?:\s[^>]*)?>")
        href = re.compile(r'href="([^"]*)"')

        path = os.path.join(
            i18n.LOCALE_DIR, code.replace("-", "_"), "LC_MESSAGES", "dashboard.po"
        )
        with open(path, "rb") as handle:
            catalog = read_po(handle)

        for message in catalog:
            if not message.id or not message.string:
                continue

            # A PLURAL ENTRY IS A TUPLE ON BOTH SIDES, and the rule for it is
            # looser in one direction on purpose.
            #
            # Its msgid is (singular, plural); its msgstr has one form per the
            # language's nplurals -- one for Japanese, three for Russian, six
            # for Arabic. A form is allowed to OMIT `%(count)s`: Arabic's zero
            # and one forms read "no member verified" and "one member
            # verified", where spelling the digit out again would be wrong.
            # That is safe at runtime, because `%`-formatting with a dict
            # ignores keys the string does not use.
            #
            # What is never allowed is a form INTRODUCING a placeholder the
            # English does not define. That is a KeyError at render time, on
            # the Overview page.
            if isinstance(message.id, (list, tuple)):
                english_forms = list(message.id)
                allowed = set()
                for form in english_forms:
                    allowed |= set(placeholder.findall(form))
                for form in message.string:
                    if not form:
                        continue
                    unknown = set(placeholder.findall(form)) - allowed
                    assert not unknown, (
                        f"{code}: plural form introduces {sorted(unknown)}, "
                        f"which the English never defines"
                    )
                continue

            english, translated = message.id, message.string
            assert set(placeholder.findall(english)) == set(
                placeholder.findall(translated)
            ), f"{code}: placeholders differ in {english[:60]!r}"
            assert sorted(tag.findall(english)) == sorted(
                tag.findall(translated)
            ), f"{code}: markup differs in {english[:60]!r}"
            assert sorted(href.findall(english)) == sorted(
                href.findall(translated)
            ), f"{code}: link target differs in {english[:60]!r}"

    def test_english_hands_back_the_msgid(self):
        gettext = i18n.translator("en-US")
        assert gettext("Renews") == "Renews"

    def test_an_unknown_language_degrades_to_english_rather_than_raising(self):
        """A stale cookie naming a language since dropped, or a bot running
        ahead of a dashboard deploy. Both are normal on two hosts."""
        assert i18n.translator("xx-YY")("Renews") == "Renews"


class TestPrecedence:
    """Which of the three inputs wins, and in what order.

    Asserted against the pure function. The order is a product decision and
    reads as one here, rather than being inferred from a page render.
    """

    def test_the_picker_beats_everything(self):
        """#97 calls the guild's language "the least discoverable if it is
        wrong". This is the answer to that: an explicit choice always wins."""
        assert i18n.negotiate(cookie="ja", guild_locale="de", accept_language="ru") == "ja"

    def test_the_guild_beats_the_browser(self):
        """A server configured in German gets a German dashboard with nobody
        doing anything, which is the whole consistency argument with the bot."""
        assert i18n.negotiate(guild_locale="de", accept_language="ru") == "de"

    def test_the_browser_is_the_floor_before_english(self):
        """The only one of the three available on the sign-in page."""
        assert i18n.negotiate(accept_language="ru,en;q=0.5") == "ru"

    def test_nothing_at_all_is_english(self):
        assert i18n.negotiate() == "en-US"

    def test_an_unsupported_value_falls_THROUGH_rather_than_failing(self):
        """A cookie naming a language we cannot render must show the guild's,
        not an error and not English-when-German-was-available."""
        assert i18n.negotiate(cookie="xx", guild_locale="de") == "de"
        assert i18n.negotiate(cookie="../../etc/passwd", accept_language="de") == "de"


class TestAcceptLanguage:
    """Parsed here rather than taken from Werkzeug, so it can be asserted
    without a request -- and because this header is attacker-controlled and
    arrives on every single request."""

    def test_whitespace_is_stripped_not_backtracked_over(self):
        """The tag pattern must stay linear in the length of its input.

        CodeQL flagged the first version of `_TAG` as a polynomial regular
        expression on uncontrolled data, and it was right. Anchored
        `^\\s*...\\s*$`, an input like `"en" + " " * n + "!"` made the trailing
        `\\s*` hand its spaces back one at a time, rescanning the rest on every
        attempt: 7us at 50 spaces, 562us at 500. `MAX_ACCEPT_LANGUAGE` bounded
        that at roughly half a millisecond per request and never removed it,
        and this endpoint needs no session, so an attacker multiplies it by
        their request rate.

        The bound below is three orders of magnitude above what the current
        pattern needs (about 5us) and three below what the old one needed
        (about 5.5ms), so it separates the two without being a benchmark.
        """
        import time

        hostile = "en" + " " * 1600 + "!"

        best = min(
            _elapsed(lambda: i18n._TAG.match(hostile)) for _ in range(15)
        )
        assert best < 0.002, (
            f"matching one Accept-Language tag took {best * 1e6:.0f}us; the "
            "pattern has picked up an ambiguity and is backtracking again"
        )

    def test_surrounding_whitespace_is_still_accepted(self):
        """Stripping replaced the `\\s*` anchors and must not have narrowed
        what a real browser can send."""
        assert i18n.parse_accept_language("  de  ") == ["de"]
        assert i18n.parse_accept_language("ja , de") == ["ja", "de"]
        assert i18n.parse_accept_language("de ; q = 0.9 , ja") == ["ja", "de"]

    def test_quality_values_order_the_result(self):
        assert i18n.parse_accept_language("en;q=0.2, ja;q=0.9, de") == ["de", "ja", "en-US"]

    def test_a_base_language_matches_the_variant_we_carry(self):
        """One Portuguese catalogue read by a Portuguese speaker beats
        English."""
        assert i18n.parse_accept_language("pt-PT") == ["pt-BR"]
        assert i18n.parse_accept_language("ja-JP") == ["ja"]

    def test_q_zero_is_a_refusal_not_a_weak_preference(self):
        assert i18n.parse_accept_language("de;q=0") == []

    def test_one_malformed_tag_does_not_discard_the_good_ones(self):
        assert i18n.parse_accept_language("!!!,ja") == ["ja"]

    def test_a_wildcard_is_not_a_preference(self):
        """Otherwise `*` would outrank the guild's configured language."""
        assert i18n.parse_accept_language("*") == []

    def test_an_absurd_header_is_refused_before_it_is_parsed(self):
        assert i18n.parse_accept_language("de," * 5000) == []

    def test_nothing_unsupported_ever_comes_back(self):
        """The caller never has to re-check, which is what stops an unchecked
        value reaching a `lang` attribute or a catalogue path."""
        for code in i18n.parse_accept_language("fr, ja, kl, de, xx-YY"):
            assert i18n.is_supported(code)


class TestDirection:
    """`dir` ships from the first day; the stylesheet catches up separately.

    The two are not the same job. `dir` decides which end of the line a
    sentence starts at and how a mixed string like "VRCVerify Premium" is
    ordered inside an Arabic one. Getting that wrong is unreadable in a way an
    unmirrored sidebar is not.
    """

    def test_arabic_is_rtl_and_nothing_else_is(self):
        assert i18n.direction("ar") == "rtl"
        for code in i18n.UI_LANGUAGES:
            if code != "ar":
                assert i18n.direction(code) == "ltr", code


class TestTheNoOpMarker:
    def test_it_returns_its_argument(self):
        """Its entire job is being a name `pybabel extract -k N_` recognises,
        so that a table built at import can hold msgids and be looked up per
        request."""
        assert i18n.N_("Renews") == "Renews"


class TestNoTranslationBreaksItsOwnFormatting:
    """A translation that survives the placeholder check can still raise.

    `test_every_translation_keeps_its_placeholders_and_markup` compares the SET
    of `%(name)s` placeholders on each side, which catches a translation that
    invents one or drops one. It does not catch a stray `%`: "Neu in deinem
    Tarif: %(title)s, 50% mehr" has exactly the right placeholder set and
    raises ValueError the moment anything formats it.

    That failure lands at render time, on one page, in one language -- the
    hardest kind of bug to notice from an English desk. So this does the
    formatting rather than reasoning about it.
    """

    @pytest.mark.parametrize(
        "code", [c for c in i18n.UI_LANGUAGES if c != i18n.DEFAULT_LANGUAGE]
    )
    def test_every_translation_formats(self, code):
        pytest.importorskip("babel", reason="Babel is a dev-only dependency")
        import re

        from babel.messages.pofile import read_po

        placeholder = re.compile(r"%\((\w+)\)[sdif]")

        path = os.path.join(
            i18n.LOCALE_DIR, code.replace("-", "_"), "LC_MESSAGES", "dashboard.po"
        )
        with open(path, "rb") as handle:
            catalog = read_po(handle)

        for message in catalog:
            if not message.id or not message.string:
                continue
            ids = (
                message.id
                if isinstance(message.id, (list, tuple))
                else [message.id]
            )
            forms = (
                message.string
                if isinstance(message.string, (list, tuple))
                else [message.string]
            )

            keys = set()
            for english in ids:
                keys |= set(placeholder.findall(english))
            if not keys:
                # Nothing formats this one, so a bare `%` in it is just a per
                # cent sign. "Save about 10%" is exactly that, in twelve
                # languages.
                continue

            arguments = {key: "x" for key in keys}
            for form in forms:
                if not form:
                    continue
                try:
                    form % arguments
                except Exception as error:  # noqa: BLE001 -- reporting it
                    raise AssertionError(
                        f"{code}: {type(error).__name__} formatting "
                        f"{form[:70]!r} -- this is a 500 on the page that "
                        f"uses it, in one language only"
                    ) from error


class TestDatesAndNumbersFollowTheLanguage:
    """#230: the labels were translated and the values under them were not.

    Asserted against `i18n` directly, because these four functions are the only
    place the dashboard calls Babel and every surface that formats anything
    goes through one of them. A regression here is a regression on all of them.
    """

    def test_a_date_is_written_the_way_each_language_writes_it(self):
        """The bug in one line: twelve languages, twelve renderings, and no
        two of the interesting ones alike.

        Not an exhaustive table -- these are CLDR's answers and pinning all
        twelve would be asserting Babel's data rather than our use of it. These
        four differ from English in a different way each: field order, script,
        separator, and a genitive month.
        """
        iso = "2027-02-03T00:41:00Z"
        assert i18n.format_date(iso, "en-US") == "February 3, 2027"
        assert i18n.format_date(iso, "ja") == "2027年2月3日"
        assert i18n.format_date(iso, "de") == "3. Februar 2027"
        # The case that decided against a month-name table of our own: Russian
        # needs "февраля" here and not the nominative "февраль", and knowing
        # that is exactly what a hand-rolled table would not have known.
        assert i18n.format_date(iso, "ru").startswith("3 февраля 2027")

    def test_the_day_the_bot_sent_is_the_day_that_is_shown(self):
        """An instant just before midnight UTC is still that day.

        `to_date` applies UTC before taking the date rather than after. If it
        did not, a period ending at 23:41Z would render as the following day
        for anyone whose string carried an offset, and two admins on one
        server would read two different renewal dates.
        """
        assert i18n.format_date("2027-02-03T23:41:00Z", "en-US") == "February 3, 2027"
        assert i18n.format_date("2027-02-03T00:41:00+00:00", "en-US") == (
            "February 3, 2027"
        )
        # Naive: treated as UTC, which is what the bot means by it.
        assert i18n.format_date("2027-02-03T00:41:00", "en-US") == "February 3, 2027"

    def test_a_bare_iso_day_formats_too(self):
        """`collecting_since` and the daily series send `2026-08-24`, not an
        instant. Both shapes arrive from the same bot and both must render."""
        assert i18n.format_date("2026-08-24", "en-US") == "August 24, 2026"
        assert i18n.format_day("2026-08-24", "ja") == "8月24日"

    def test_nothing_the_bot_could_send_raises(self):
        """These format values that arrived over the wire, on pages that must
        not 500. Every one of these is None rather than an exception."""
        for bad in (None, "", "not a date", "2026-13-45", 17, [], {}, True):
            assert i18n.format_date(bad, "de") is None
            assert i18n.format_day(bad, "de") is None
            assert i18n.format_timestamp(bad, "de") is None

    def test_thousands_are_not_always_a_comma_every_three(self):
        """The other half of #230, and the one that is easy to assume away.

        Five of the twelve disagree with `f"{n:,}"`, and Hindi disagrees about
        the grouping itself rather than about the character: 1234567 is seven
        digits split 2-2-3, not 1-3-3.
        """
        assert i18n.format_number(1234567, "en-US") == "1,234,567"
        assert i18n.format_number(1234567, "hi-IN") == "12,34,567"
        assert i18n.format_number(1234567, "de") == "1.234.567"
        # Zero is a real number and must not become blank -- the falsy-zero
        # rule `Tile.display` is built around, at the formatting layer.
        assert i18n.format_number(0, "de") == "0"
        # ...and a missing one is blank, never the word "None". Every caller
        # guards before it reaches here; this is the floor under them.
        assert i18n.format_number(None, "de") == ""

    def test_a_timestamp_always_says_utc(self):
        """The audit trail's instants are UTC and the marker is not optional.

        The shape around it is the locale's; those three letters are not, in
        any of the twelve. A reader who subtracts their own offset from a time
        that never had one added gets a wrong answer about who changed what.
        """
        stamp = "2026-08-11T07:11:36+00:00"
        for code in i18n.UI_LANGUAGES:
            assert i18n.format_timestamp(stamp, code).endswith(" UTC"), code
        assert i18n.format_timestamp(stamp, "de").startswith("11.08.2026")

    def test_an_unsupported_language_falls_back_rather_than_raising(self):
        """The same floor `catalogue()` puts under itself. Callers have all
        validated; this is not the check, it is what happens if one is missed."""
        assert i18n.format_date("2027-02-03", "xx-YY") == "February 3, 2027"
        assert i18n.format_number(1000, "xx-YY") == "1,000"

    def test_every_language_in_the_list_can_actually_format(self):
        """The list and Babel's data are two sources of truth about which
        languages exist, and a thirteenth added to `UI_LANGUAGES` without a
        CLDR locale behind it would be a 500 on one page in one language.

        `_LOCALES` is built at import, so this mostly asserts the import
        succeeded -- which is the point: it fails on deploy, not on a render.
        """
        for code in i18n.UI_LANGUAGES:
            assert i18n.format_date("2027-02-03", code), code
            assert i18n.format_date("2027-02-03", code, form="medium"), code
            assert i18n.format_day("2027-02-03", code), code
            assert i18n.format_number(1234, code), code
            assert i18n.format_timestamp("2027-02-03T07:11:00Z", code), code


class TestFormattingIsSafeOnFourThreads:
    """The image runs `gunicorn --threads 4`, and this module now holds shared
    Babel locale objects that the formatters read on every request.

    The sibling of `TestTwoLanguagesAtOnce` in test_dashboard.py, one layer
    down. That one proved a request in one language could not change the page
    another was being served; this proves the same for the values, which
    arrived with #230 and go through data structures Babel resolves lazily.
    """

    def test_twelve_languages_at_once_all_get_their_own_answer(self):
        import threading

        results = {}
        errors = []
        start = threading.Barrier(len(i18n.UI_LANGUAGES))

        def render(code):
            try:
                # Every thread waits, then they all hit the formatters at once.
                start.wait(timeout=5)
                results[code] = (
                    i18n.format_date("2027-02-03T00:41:00Z", code),
                    i18n.format_day("2027-02-03", code),
                    i18n.format_number(1234567, code),
                    i18n.format_timestamp("2027-02-03T07:11:00Z", code),
                )
            except Exception as error:  # noqa: BLE001 -- reporting it
                errors.append((code, error))

        threads = [
            threading.Thread(target=render, args=(code,))
            for code in i18n.UI_LANGUAGES
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)

        assert not errors, errors
        assert len(results) == len(i18n.UI_LANGUAGES)

        # Each answer is the one that language gives on its own, with nothing
        # else running -- which is the whole claim.
        for code, concurrent in results.items():
            alone = (
                i18n.format_date("2027-02-03T00:41:00Z", code),
                i18n.format_day("2027-02-03", code),
                i18n.format_number(1234567, code),
                i18n.format_timestamp("2027-02-03T07:11:00Z", code),
            )
            assert concurrent == alone, code

    def test_the_warm_up_really_did_run_at_import(self):
        """`_warm()` is called at module import and takes the lazy resolution
        off the threaded request path. If it were removed, the test above would
        still pass -- the race it guards is benign -- so this asserts the thing
        itself rather than its effect."""
        locale = i18n._LOCALES["ja"]
        # Resolved data, not an Alias waiting to be resolved on first read.
        assert locale.date_formats["long"]
        assert locale.datetime_skeletons["MMMd"]
